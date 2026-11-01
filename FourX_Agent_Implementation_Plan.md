# FourX Agent — Step-by-Step Implementation Plan

## Current stage

The agent is built in a trimmed form (see "Scope decisions"): read tools, SQLite
conversations, the Gemini/Groq/OpenAI loop, approval, `switch model` command, stop and posting
recovery. `python agent.py` opens the chat; if SQLite holds an incomplete conversation it asks the clerk
to continue it or start new, otherwise it starts a new one (planned redesign, see "Scope decisions"); the inspection
commands (`fourx invoices`, `fourx ledger all`, ...) still work without API keys.
Acceptance runs against live providers are recorded in the README, not here.

The authoritative requirements and original inputs are in:

- `assignment/take-home/README.md`
- `assignment/take-home/ledger.py` — use unchanged
- `assignment/take-home/vendors.json`
- `assignment/take-home/invoices/invoice-01.json` through `invoice-06.json`

Build one milestone at a time. Check off a milestone only after its behavior has
been demonstrated. Do not treat the old prototype or its tests as completed work.

## Scope decisions (supersede the detailed sections below where they differ)

The brief says to keep it small and to explain every line, so the build is trimmed:

- Plain `input()`; the agent runs on a worker thread and Ctrl-C asks it to stop. No
  `prompt-toolkit`, no `/cancel`, no asyncio.
- The main agent has three jobs, and nothing else:
  1. **Switch model** when the clerk asks. The CLI does it in code (`switch model`, then
     `gemini, groq or openai?`), so the model is not involved and quota errors cannot block it.
  2. **Read data** with the read tool. For the ledger it has three actions: `last` (the latest
     entry), `all` (every entry), and `filter` (equality on any ledger column, combined with
     AND). It also reads invoices and vendors by ID or name.
  3. **Post an invoice** with the post tool, after code checks and human approval.
- The model sees two tools: `read(source, mode, id, filters)` over invoices, vendors and the
  ledger (last/all/filter) and `post(invoice_id)`. `switch_model(model)` is the third MCP tool, run only
  when the clerk asks (typing `switch model`), never offered to the model. Typing exactly `switch model` makes the CLI ask
  `gemini, groq or openai?` and compare the answer, so a model out of quota cannot block a switch.
  The tools live in an MCP server (`mcp_server.py`, official `mcp` Python SDK, stdio, started
  by the CLI). The runner is the MCP client: it lists the server's tools, gives the model
  their schemas, and sends each tool call over MCP. The server exposes `read`, and `post`
  split into `propose_post` (runs `check_posting`, returns the exact proposal) and `execute_post`
  (re-checks under the lock, then `ledger.post`). The model sees only `read` and `post`; the
  runner turns `post` into `propose_post`, and calls `execute_post` itself, only after the
  clerk's `/approve`. Approval, saving and recovery stay in the runner, never in the server.
  `/model` is gone. Default provider is OpenAI.
- Conversation choice is a startup question. Each conversation has one status, `open` or `completed`.
  When the CLI opens, the system looks in SQLite for an incomplete (`open`) conversation. If one
  exists, the CLI asks `continue last conversation or new?` and compares the answer in code (no
  LLM call). `continue` restores the most recently active one: history, saved provider and any
  waiting approval. `new` creates a separate conversation with a generated ID; the old one stays
  `open` and untouched, so the question is asked again next launch. Anything else re-asks. If no
  open conversation exists, a new one is created without asking.
  Either way, the result is handed to the main agent (the runner): the loaded history for
  `continue`, an empty conversation for `new`. The main agent owns everything after that.
  A continued conversation is not just reopened: once recovery has settled any interrupted
  posting, the saved history goes to the model like any other turn, with a short system note
  that the session was resumed, and the model decides what to do next (answer the unanswered
  request, continue the tool work, or ask the clerk). It does this only when the conversation
  stops mid-work; if the last item is a finished answer, it waits at the prompt. A waiting
  approval is never decided by the model: it stays for `/approve` or `/decline`. Code checks
  (blocked vendor, duplicate, approval) still apply to whatever the model asks for.
  `/quit` leaves the conversation `open`, so the next launch continues it. `/finish` marks it
  `completed` (refused while an approval waits or a posting is unsettled), so the next launch
  starts fresh. `--conversation NAME` stays only as an optional override (resume or create that
  name). No `/new`, and no separate workflow-status field.
- After each finished task (posted, declined, refused, or a question answered) the agent ends
  its reply with a short follow-up asking whether to work on the next invoice or help with
  anything else. This comes from the system prompt, not code; the loop itself is unchanged.
- Ctrl-C never cancels a waiting approval; only `/approve` or `/decline` ends it. A
  normal message typed while an approval waits re-shows the approval.
- Duplicates are judged by the real ledger only. Saved posting attempts are used only
  to recover an interrupted post, so `python ledger.py reset` cannot wrongly block a rerun.
- The lock (`ledger.lock`, beside the ledger) is held only around the final re-check
  and post, so two terminals cannot both pass the duplicate check.
- Python >= 3.11. The conversation DB is `data/fourx.db`; old `data/` files are unused.
- Model 429/5xx/timeouts are retried with a wait that Ctrl-C can interrupt.
- `FOURX_TOOL_DELAY` (seconds) slows each read tool so scenario 5 can be stopped.
- Gemini history keeps the raw parts (with thought signatures) for Gemini's own turns.
  Calls from another provider are sent with Google's documented placeholder signature.
  Verify this live (scenario 4) before relying on it.

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

One diagram: how the CLI reaches the main agent, and how the main agent reaches its three
tools through the MCP server. Sections 3 onward are the original design notes; where they
differ from this diagram or the decisions above, the diagram and decisions win.

```mermaid
flowchart TD
    Clerk([Clerk]) --> Launch[Open the CLI]
    Launch --> Find{"Incomplete conversation in SQLite?"}
    Find -->|Yes| Ask{"Ask the clerk: continue last conversation or new?"}
    Ask -->|continue| Resume[Load the saved history]
    Ask -->|new| Create[Create a new conversation]
    Find -->|No| Create
    Resume --> Prompt
    Create --> Prompt

    subgraph Loop["Conversation loop - repeats until the clerk quits"]
        Prompt["you> prompt: clerk types a request"] --> Agent[["Main agent (runner)"]]
        Agent <--> Model{{Gemini / Groq / OpenAI}}
        Agent <-->|"call a tool, get its result"| MCP[[MCP server]]
        Agent -->|"checks the result, then answers"| Answer["Answer to the clerk, plus: next invoice, or anything else?"]
        Answer -->|"loop again: wait for the next message"| Prompt
    end

    MCP --- Switch["switch_model"]
    MCP --- Read["read: ledger last / all / filter, invoices, vendors"]
    MCP --- Post["post: checks, human approval, then ledger.post"]
    Answer -->|"/quit or /finish"| End([Exit])
```

The conversation is a loop. After every outcome, whether an invoice was posted, declined or
refused, or a question was answered, the main agent comes back to the clerk with a short
follow-up such as "Shall we work on the next invoice, or is there anything else I can help
with?" The clerk's next message starts the next pass through the same loop, in the same
conversation, until `/quit` or `/finish`.

## 3. Agreed state, tools and recovery decisions

### Conversation identity and lifecycle

- Give each conversation a stable `conversation_id`; link every message, call,
  result, approval and posting attempt to it.
- Keep one `conversation_status` (`open` or `completed`). The trimmed build has no
  separate `workflow_status`; waiting approvals and unsettled postings are read from the
  postings table. A model answer does not complete a conversation.
- Save `active_provider` and `last_active_at` on the conversation. The runner selects the
  provider from saved state before each model request.
- On startup the CLI queries SQLite for conversations with status `open`. If any, ask the
  clerk whether to continue the most recently active one or start new; if none, create a new
  one with a generated ID. An explicit
  `--conversation NAME` takes precedence.
- `/quit` saves and exits while keeping the conversation open. `/finish` marks it
  completed and exits; require pending approval and unsettled postings to be resolved first.
- Resume the same conversation after interruption or restart. A new conversation does not
  silently inherit a completed conversation's transcript or approval.

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
The MCP server exposes this as its `switch_model` tool; the runner calls it only for the clerk.

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
├── agent.py              # python agent.py opens the interactive prompt
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
│   ├── tools.py          # supplied data access and ledger bridge (served by the MCP server)
│   ├── mcp_server.py     # MCP server exposing read, propose_post, execute_post
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

Code for all milestones is written. Check a box only once its behavior has been shown.

- [x] **1. Read tools** — invoices, vendors, ledger reads, inspection CLI (tests pass).
- [x] **2. Persistence** — `storage.py`: conversations, messages, posting attempts, one DB.
- [x] **3. Loop** — `runner.py` with a scripted-provider test suite (`tests/test_agent.py`).
- [x] **4. Approval and posting** — checks before and after approval, intent saved before
  `ledger.post`, reference `fourx:<conversation>:<action>`, recovery from the ledger.
- [x] **5. Providers** — `providers.py` Gemini and Groq adapters; conversion unit-tested.
- [x] **6. Stop and recovery** — stop during model/tools; a started post is never abandoned.
- [ ] **7. Live acceptance** — all seven scenarios on real Gemini and Groq, then README.

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

Run the seven scenarios live (reset the ledger first), fix what they show, then write
the one-page README: how to run, models used, what is saved and when, how switching
works, how approval and stop work, and what was not handled.
