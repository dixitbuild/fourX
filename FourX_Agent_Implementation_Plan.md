# FourX Agent — Step-by-Step Implementation Plan

## Current stage

Milestone 1 is implemented: read-only data tools, their JSON tool definitions,
canonical message/action contracts, an inspection CLI and isolated tests. The full
agent loop, persistence and posting are not implemented yet. Installed dependencies,
`.venv`, `pyproject.toml` and `uv.lock` remain unchanged.

The authoritative requirements and original inputs are in:

- `assignment/take-home/README.md`
- `assignment/take-home/ledger.py` — use unchanged
- `assignment/take-home/vendors.json`
- `assignment/take-home/invoices/invoice-01.json` through `invoice-06.json`

Build one milestone at a time. Check off a milestone only after its behavior has
been demonstrated. Do not treat the old prototype or its tests as completed work.

## 1. Required behavior

Build a command-line assistant for an accounts-payable clerk at Acme Foods that:

1. Runs an explicit model/tool loop until the model answers or approval is needed.
2. Lists invoices, reads a complete invoice, looks up vendors, and posts invoices.
3. Requires human approval for every exact proposed posting.
4. Returns approval/decline outcomes to the model and continues the loop.
5. Switches between Gemini and Groq while retaining conversation and tool history.
6. Saves progress as it goes and restores a waiting approval after restart.
7. Stops during model/tool work within a few seconds; accepts the next request normally.
8. Refuses blocked vendors and duplicate `(vendor_id, invoice_number)` entries in code,
   before asking for approval and again before posting.

Use each provider's official SDK and native function calling. Do not use agent
frameworks or a library that hides provider conversion. Do not replace the ledger
or invent invoice fields, account mappings, or tax rules.

## 2. End-to-end flow

### Simple flow overview

![Simple agent flow diagram](mermaid-diagram.png)

The **CLI** accepts human input, the **runner** controls the workflow, and the
**model** chooses tools or writes an answer. Every box marked **Save** writes to
the conversation SQLite database before the runner advances. Accounting entries
are written only through the supplied `ledger.py`.

### Main conversation and approval flow

```mermaid
flowchart TD
    Start([Start CLI]) --> Load[Select explicit conversation ID or latest active open conversation; create one if none exists]
    Load --> Restore[Restore conversation history, provider, model ID and workflow state]
    Restore --> Recover[Reconcile interrupted posting attempts and close interrupted calls]
    Recover --> Pending{Saved approval waiting?}
    Pending -->|Yes| Display
    Pending -->|No| Input[Wait for human input in async CLI prompt]

    Input --> Kind{Input type?}
    Kind -->|Natural-language request| SaveUser[Save user message]
    Kind -->|Switch provider| Switch[Save selected provider; keep the same history]
    Switch --> Input
    Kind -->|Quit| Exit([Save checkpoints and exit])
    Kind -->|Finish while idle| Finish[Mark conversation completed and exit]
    Kind -->|New conversation| New[Preserve current conversation; create a new open conversation]
    New --> Input
    SaveUser --> Model[Convert history and tool definitions; call Gemini or Groq]
    Model --> SaveReply[Save assistant response and structured tool calls]
    SaveReply --> HasCalls{Tool calls requested?}
    HasCalls -->|No| Answer[Display model answer]
    Answer --> Input
    HasCalls -->|Yes| Queue{Another unresolved call in this batch?}
    Queue -->|No| Model
    Queue -->|Yes| Tool{Tool type?}

    Tool -->|List or read invoices, look up vendor, or read ledger| Read[Run read-only tool]
    Tool -->|Explicitly requested provider switch| SwitchTool[Validate target; save provider and model selection]
    SwitchTool --> Result
    Read --> Result[Save tool result with its matching call ID]
    Result --> Queue

    Tool -->|Post invoice| Check[Resolve vendor; check status and existing ledger entries]
    Check --> Allowed{Posting allowed?}
    Allowed -->|No: blocked, duplicate, or invalid| Refuse[Save refusal as the tool result; do not ask for approval]
    Refuse --> Queue
    Allowed -->|Yes| Snapshot[Save exact proposal, action ID, and pending tool call]
    Snapshot --> Display[Display vendor, invoice number, date, currency, and amount]
    Display --> Wait[Pause model loop; wait for human input in async CLI prompt]
    Wait --> Decision{Human command?}
    Decision -->|Approve| Recheck[Recheck current invoice, vendor status, and duplicates]
    Recheck --> Unchanged{Still matches and is allowed?}
    Unchanged -->|No| Refuse
    Unchanged -->|Yes| Intent[Save approval and posting intent with a stable reference]
    Intent --> Post[Run supplied ledger.post with the saved reference outside the terminal event loop]
    Post --> Outcome[Reconcile if needed; save and report actual ledger outcome]
    Outcome --> Queue
    Decision -->|Decline with optional reason| Decline[Save decline and reason as the tool result; no posting]
    Decline --> Queue
    Decision -->|Switch provider| SwitchPending[Save provider; preserve the waiting proposal]
    SwitchPending --> Wait
    Decision -->|Quit| Preserve[Keep approval pending on disk]
    Preserve --> Exit
    Exit -.->|Restart the same conversation| Start
```

**While approval waits:** no model request or ledger write is running. The prompt
waits for Enter, while commands such as `/status`, `/model`, and `/quit` remain
available. Approval is accepted only from human command handling; a model's text
cannot approve its own tool call. A provider switch affects the next model request.

**Multiple tool calls:** save the complete batch and resolve every call before
asking the model again. A posting call pauses at its place in that batch; after
approval or decline, its saved result lets the runner continue the remaining calls.

### Stop, failure, and recovery flow

```mermaid
flowchart TD
    Stop([Ctrl+C or /cancel]) --> Phase{Current phase?}
    Phase -->|Model request or read-only tools| Cancel[Cancel active task; discard late model responses]
    Cancel --> Checkpoint[Keep completed results; save cancelled results for unfinished calls]
    Phase -->|Waiting for approval| Invalidate[Invalidate pending approval; save cancellation without posting]
    Invalidate --> Checkpoint
    Phase -->|Approved ledger call already started| Supervise[Stop further processing; supervise the short ledger call until it finishes]
    Supervise --> Record[Save and report whether the invoice was posted]
    Record --> Checkpoint
    Checkpoint --> Ready([Return to prompt; accept the next request])

    Failure([API or tool failure]) --> Saved[Save failure and completed results; close unfinished calls safely]
    Saved --> Ready

    Crash([Process killed during posting]) --> Restart[On restart, load saved posting intent]
    Restart --> Inspect[Query ledger entries by stable reference and business key]
    Inspect --> Exists{Matching ledger entry exists?}
    Exists -->|Yes| Posted[Save recovered entry ID; do not repost]
    Exists -->|No| Fresh[Record no entry found; require fresh approval before another attempt]
    Exists -->|Conflicting entry| Conflict[Record conflict; refuse retry and show details for review]
    Posted --> Ready
    Fresh --> Ready
    Conflict --> Ready
```

A stop cannot undo a committed ledger entry. If a posting error leaves its outcome
uncertain, reconcile it using the posting-recovery path before allowing a retry.
Quitting at a waiting approval preserves it; cancelling that approval invalidates it.

## 3. Agreed state, tools and recovery decisions

### Conversation identity and lifecycle

- Give each conversation a stable `conversation_id`; link every message, call,
  result, approval and posting attempt to it.
- Keep `conversation_status` (`open` or `completed`) separate from
  `workflow_status` (`idle`, `working`, `approval_pending`, `posting`,
  `interrupted` or `failed`). A model answer does not complete a conversation.
- Save `active_provider`, the exact `active_model`, and `last_active_at` alongside
  that state. The runner selects them from saved state before each model request.
- On startup, an explicit conversation ID takes precedence. Otherwise resume the
  most recently active open conversation; create one when no open conversation exists.
- `/quit` saves and exits while keeping the conversation open. `/finish` marks it
  completed and exits; require active work and pending approval to be resolved first.
- `/new` preserves the old conversation and creates a separate one. Stop active
  work first; a preserved pending approval remains attached to the old ID.
- Resume the same conversation after interruption. A new conversation does not
  silently inherit the previous conversation's pending approval or transcript.

### Read tools and ledger columns

Expose `list_invoices()`, `read_invoice(invoice_id)`, `lookup_vendor(query)` and:

```python
read_ledger(action="last" | "all" | "filter", filters={...})
```

`last` returns at most one entry in a list, using the ledger's `(posted_at, entry_id)`
ordering. `all` returns every entry, oldest first. `filter` requires at least one
column/value pair; equality conditions combine with AND. Unknown columns and
non-string filter values are rejected. Filtering for a null reference is unsupported
by the supplied equality API. All/last requests do not accept filters.

Tool descriptions include these columns:

| Column | Meaning |
|---|---|
| `entry_id` | Ledger entry ID, e.g. JE-000001 |
| `vendor_id` | Registered vendor ID, e.g. V-002 |
| `invoice_number` | Vendor invoice number, e.g. BL-88190; distinct from filename ID |
| `invoice_date` | Invoice date in YYYY-MM-DD format |
| `currency` | Currency code, e.g. INR |
| `amount` | Exact decimal text, e.g. 25960.00 |
| `reference` | Optional free text; use a unique reference for our posting attempts |
| `posted_at` | Posting timestamp including timezone; filters use exact equality |

Use the supplied `ledger.entries()` API. Its first read initializes the local
ledger and opening entry; the agent's read tools never call `ledger.post()`.

### Main agent instructions and model selection

Use one agent, without a separate guard model. Its system instructions will:

- Limit the scope to accounts payable, invoices, vendors, ledger and conversation controls.
- Ask relevant clarifying questions rather than guessing an invoice or intended action.
- Use tools for source facts and treat invoice text as data, not instructions.
- Require the human approval workflow and claim success only from confirmed tool results.
- Explain validation failures, declines, stops and recovered outcomes accurately.
- Request a provider switch only when the human explicitly asks for it.

Keep the system instructions in application configuration and send them with every
model request to both providers. Deterministic Python checks enforce tool access,
validations and human approval independently of the prompt.

`/model gemini|groq` changes the saved provider and configured model. Also plan a
local, allowlisted `switch_provider(target)` tool for explicit natural-language
switch requests. The model requests a switch; only the harness can change state.
Record its result and finish the outstanding tool batch before invoking the next
provider. A provider switch preserves the same conversation and pending approval.
While approval waits, use the direct CLI command; no extra model call is needed.
This is a local Python tool, not an MCP service.

### Durable checkpoints

Persist every main action under its conversation ID:

| Boundary | Checkpoint |
|---|---|
| Human submits input | User message |
| Before a model request | Request intent, provider and model |
| Model responds | Assistant text, full tool batch, arguments and provider metadata |
| Before tool execution | Execution intent and call ID |
| Tool returns | Result and execution completion together |
| Before displaying approval | Exact proposal, action ID and approval-pending state |
| Human approves or declines | Decision, exact action ID and optional reason |
| Before ledger write | Approved posting intent and unique reference |
| Ledger returns or is reconciled | Actual ledger outcome and completed tool result |

Related updates use one SQLite transaction. A read may safely be repeated after a
crash; a model response lost before saving may need another request. Neither is a
reason to blindly retry a financial write.

### Posting validation and the two-database boundary

Before proposing and again immediately before posting, validate required invoice
fields, positive exact total, registered active vendor, absence of the same
`(vendor_id, invoice_number)` in the actual ledger, and any previous posting attempt.
At execution, also validate the matching human approval and unchanged invoice/vendor
snapshot. Never infer approval from model output or reuse a consumed approval.
Do not invent GST recalculation, account mapping or additional accounting rules.

Save this reference in the posting intent and pass it to `ledger.post()`:

```text
fourx:<conversation_id>:<action_id>
```

The action ID distinguishes multiple invoices posted in the same conversation.
After an interrupted write, query `ledger.entries(reference=saved_reference)` and
cross-check the approved business key and details. Existing matching entries are
recorded as success, without reposting; missing entries require fresh approval;
conflicting entries require review. Duplicate checks also use vendor and invoice
number independently of reference, including the opening entry with a null reference.

The conversation DB and ledger DB commit independently. The unchanged `ledger.post()`
controls its own connection and transaction, so wrapping our call in a transaction
cannot make both databases all-or-nothing. Save durable intent before the call and
reconcile afterward for recoverable consistency. A thread keeps the terminal
responsive but cannot survive a process kill or undo a committed write.

## 4. Proposed layout

Keep the assignment folder as the authoritative input; avoid duplicate copies.

```text
fourX/
├── FourX_Agent_Implementation_Plan.md
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── assignment/take-home/
│   ├── README.md
│   ├── ledger.py
│   ├── vendors.json
│   └── invoices/
├── src/fourx/
│   ├── __init__.py        # existing fourx entry point
│   ├── cli.py            # read-only inspection now; interactive controls later
│   ├── models.py         # canonical messages, tool calls, and action snapshots
│   ├── tools.py          # supplied data access and ledger bridge
│   ├── storage.py        # durable conversations, tool results, approvals
│   ├── providers.py      # explicit Gemini/Groq SDK adapters
│   ├── prompts.py        # main agent scope and tool-use instructions (future)
│   └── runner.py         # model/tool loop and approval transitions
├── tests/
└── data/                 # local conversation database; git-ignored
```

The assignment recommends putting the agent next to `ledger.py` for easy imports.
For this package layout, use a small explicit loader for that exact supplied file,
resolved from a configured assignment directory. Never modify the supplied module.
Its ledger database will remain beside it: `assignment/take-home/ledger.db`.

## 5. Milestones

### Milestone 1 — Data access and read-only tools

- [x] Define canonical message/tool/action types and resolve the assignment path.
- [x] Read complete invoice JSON by filename ID, e.g. `invoice-01`.
- [x] Implement list-invoices and vendor lookup tools.
- [x] Match invoice `vendor_name` to the vendor registry; fail clearly if unmatched.
- [x] Preserve monetary strings; use `Decimal` for numeric validation.
- [x] Reject invalid invoice IDs and path traversal.
- [x] Add last/all/filter ledger reads and describe all allowed columns in tool schemas.
- [x] Provide allowlisted read-tool dispatch and a small inspection CLI.
- [x] Verify behavior using the actual data and temporary copies of the unchanged ledger.

**Done when:** invoice-06 returns the original vendor, INR 21948.00, its taxes and
one line; vendor V-004 returns its blocked status and reason. No posting occurs.

**Verification:** 36 isolated tests pass. Inspection commands are documented in README.md.

### Milestone 2 — Persistence and conversation state

- [ ] Create SQLite conversation, transcript and approval/attempt storage.
- [ ] Persist ordered user/assistant messages, calls, arguments and tool results.
- [ ] Save provider and model selection, pending calls, exact approval snapshots and decisions.
- [ ] Open/resume by conversation ID; auto-select the latest active open conversation.
- [ ] Separate conversation lifecycle from workflow status; define recovery transitions.
- [ ] Define persisted lifecycle operations for quit, finish and new conversation.

**Done when:** closing and reopening the store restores the exact saved state.
Keep this database separate from the supplied accounting ledger database.

### Milestone 3 — First provider and read-only agent loop

- [ ] Add Gemini native tool declarations using the official `google-genai` SDK.
- [ ] Add main-agent system instructions for scope, clarification, tool facts and approval.
- [ ] Send those instructions on each request; no separate guard model.
- [ ] Implement async model call → persist response → execute tools → persist results
  → model call, with a bounded number of steps.
- [ ] Handle multiple calls in one response and correlate every result with its call.
- [ ] Add the interactive CLI with concurrent input and an owned background task.
- [ ] Keep post requests disabled until the approval milestone is complete.
- [ ] Handle missing configuration, malformed responses and API failures safely.

**Done when:** a read-only conversation can inspect real invoice data through the
loop, with deterministic fake-provider tests and a separately recorded live check.

### Milestone 4 — Human approval and supplied ledger integration

- [ ] Add the `post_invoice` tool as a durable pause in the loop.
- [ ] Check vendor status and the actual ledger for duplicates before proposing.
- [ ] Display vendor ID/name, invoice number/date, currency and total.
- [ ] Save the exact invoice/vendor snapshot before displaying approval.
- [ ] Support `/approve` and `/decline <optional reason>` from human input only.
- [ ] Revalidate the snapshot, vendor status and ledger immediately before posting.
- [ ] Save posting intent and `fourx:<conversation_id>:<action_id>` before calling `ledger.post()`.
- [ ] Return success/refusal/decline as a tool result, then continue the model loop.
- [ ] Restore pending approval after quit without automatically approving it.

**Done when:** invoice-01 posts once after approval; invoice-03 declines with its
reason; invoice-04 and invoice-05 are refused without approval prompts.

### Milestone 5 — Second provider and switching

- [ ] Add Groq native tool calling using the official `groq` SDK.
- [ ] Implement `/model gemini|groq` between requests, including while approval waits.
- [ ] Add the allowlisted switching tool for explicit human requests; persist provider/model state.
- [ ] Record the switching tool result and finish pending calls before the next provider request.
- [ ] Translate the canonical transcript separately for each provider.
- [ ] Preserve Gemini content parts/thought signatures for same-model replay.
- [ ] Convert foreign-provider history explicitly, retaining calls and results without
  inventing signatures or replaying incompatible raw SDK messages.
- [ ] Test switching both directions, including an unresolved approval and completed tools.

**Done when:** official scenario 4 works across Gemini → Groq → Gemini using facts
from the existing conversation, with no missing tool-result or signature errors.

### Milestone 6 — Stop, crash recovery and financial safety

- [ ] Connect Ctrl+C and `/cancel` to cancellation of the active async operation.
- [ ] Add cancellation checkpoints around read-tool execution; preserve completed results.
- [ ] Close unfinished calls with cancelled results before the next model request.
- [ ] Discard late responses from a cancelled model call.
- [ ] Run the synchronous ledger call outside the terminal event loop.
- [ ] Once approved posting begins, retain ownership until the short call completes;
  save/report the true outcome before stopping further processing.
- [ ] Reconcile an interrupted posting attempt with `ledger.entries()` before any retry.
- [ ] Serialize agent processes sharing the ledger, since it has no duplicate constraint.
- [ ] Keep `/quit` distinct from cancelling a waiting approval.

**Important:** the supplied ledger commits partway through its two-second call.
Cancelling a thread await does not stop the thread, and stopping cannot undo a
committed write. The agent and ledger use separate databases, so a shared atomic
transaction is unavailable. Recovery must inspect the ledger, not blindly repost.

**Done when:** stopping reads/model work leaves a usable conversation, and interrupted
posting recovery cannot create a duplicate or falsely report that nothing was posted.

### Milestone 7 — Assignment acceptance and handoff

- [ ] Exercise all seven scenarios below, in order, in one conversation.
- [ ] Automate harness checks using fake providers, real fixtures and temporary ledgers.
- [ ] Verify the live-provider scenarios separately using configured model IDs/keys.
- [ ] Cover stale approval, duplicate confirmation, interrupted batches and posting recovery.
- [ ] Keep the project README to the requested one-page explanation.
- [ ] Document actual models tested, setup, persistence, switching, approval, stop and limits.
- [ ] Keep keys, local databases, virtual environments and generated state out of Git.

## 6. Seven official acceptance scenarios

| # | Action | Expected |
|---|---|---|
| 1 | Post invoice-01; approve | Proposal: V-001, SGP/26-27/0412, 2026-10-03, INR 16862.20; one ledger entry |
| 2 | Post invoice-02; quit at approval; resume and approve | Pending approval survives; BL-88213, INR 70800.00, posted once |
| 3 | Post invoice-03; decline: `PO not received yet` | No posting; model explains the decline and reason |
| 4 | Read invoice-06; switch providers; ask vendor/total; switch back; ask line count | Shree Ganesh Packaging Pvt Ltd, INR 21948.00, one line |
| 5 | Read/summarise every invoice; stop during work; ask what completed | Stops in seconds; completed reads remain; next answer works; no posting |
| 6 | Post invoice-04 | Refused without approval: V-004 blocked, bank details under verification |
| 7 | Post invoice-05 | Refused without approval: BL-88190 already posted as JE-000001 |

Start acceptance runs with a deliberately reset demo ledger and a new conversation.
At the end the ledger must contain exactly three entries: BL-88190 (opening entry),
SGP/26-27/0412 and BL-88213. Test databases must not alter the user's demo ledger.

## 7. Next action

Milestone 1 is complete. Review the read-tool behavior, then proceed to Milestone 2:
SQLite conversation storage, lifecycle state and durable checkpoints.
Installed dependencies remain unchanged; later milestones are not yet implemented.
