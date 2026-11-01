# FourX: accounts payable agent

A command-line agent for an Acme Foods accounts payable clerk. It reads invoices, checks
vendors, reads the ledger, and posts an invoice only after the clerk approves it.

The agent loop is written by hand on the OpenAI, Gemini and Groq SDKs. There is no agent
framework. The tools are plain Python methods that the loop calls directly.

## 👉 Start here: the architecture

# [Read the architecture and flow diagrams in FourX_Agent_Implementation_Plan.md](FourX_Agent_Implementation_Plan.md)

> The diagrams there show how a conversation starts, how the agent loop runs, and how each
> tool works: **posting** (checks and approval), **reading the ledger**, and **switching
> models**. Read it before the code. It is the quickest way to understand the project.

## Run

```sh
uv sync                              # or: python -m venv .venv && pip install -e .[dev]
cp .env.example .env                 # fill in a key and a model name for each model you use
python ledger.py reset              # optional: start from a clean demo ledger
uv run python agent.py               # or: python agent.py, with the .venv active
```

### What to set in `.env`

```
OPENAI_API_KEY=
OPENAI_MODEL=
GEMINI_API_KEY=
GEMINI_MODEL=
GROQ_API_KEY=
GROQ_MODEL=
SQLITE_PATH=data/conversations.db
```

Fill in the key and the model name for each model you want to use. You need at least one pair.
New conversations start on OpenAI, so set the OpenAI pair unless you plan to switch right away.
`SQLITE_PATH` is optional.

To look at the ledger yourself (use `python3` if `python` is not found):

```sh
python ledger.py          # list every posted entry
python ledger.py reset    # delete everything except the one opening entry
```

The root `ledger.py` only runs the supplied `src/fourx/ledger.py`, so the commands work from the
repo root. The ledger data is in `src/fourx/ledger.db`.

`pytest -q` runs the tests. They use a scripted model and a temporary ledger, so they need
no API key and never touch the demo ledger.

### Commands

| Type | Effect |
|---|---|
| any text | A request for the agent, for example `post invoice-01` or `last ledger entry?` |
| `switch model` or `switch model groq` | Switches this conversation's model, with no model call. Without a name it asks `gemini, groq or openai?`. |
| `/approve`, `/decline <reason>` | Answer a waiting approval. |
| `/quit` | Exit. The conversation stays open, so the next launch can continue it. |
| `/finish` | Mark the conversation completed and exit. The next launch starts fresh. |
| Ctrl-C | Stop the agent while it is working. |

## Configuration and limits

**Set in `.env`** (the only things you need to configure):

| Variable | Needed | Meaning |
|---|---|---|
| `OPENAI_API_KEY`, `OPENAI_MODEL` | one pair at least | OpenAI key and the model name to use. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | optional | Google Gemini key and model name. |
| `GROQ_API_KEY`, `GROQ_MODEL` | optional | Groq key and model name. |
| `SQLITE_PATH` | optional | Where conversations are saved. Default `data/conversations.db`. |

A model whose key or model name is missing cannot be used or switched to.

**Fixed in code** (change them in the file named, they are not environment settings):

| Setting | Value | File | Meaning |
|---|---|---|---|
| Retries | **4 retries**, so up to 5 attempts | `runner.py` `MAX_RETRIES` | Per model request. |
| Wait between retries | 2, 4, 8, then 16 seconds | `runner.py` | Doubles each time. |
| What is retried | rate limit (429), server error (5xx), timeout or connection error, an empty Gemini reply, and Groq's `tool_use_failed` | `providers.py` | Anything else fails at once. A Gemini per-day quota error is not retried, since waiting seconds won't clear it. |
| Model request timeout | 60 seconds | `providers.py` `TIMEOUT_SECONDS` | Per attempt. |
| Max tool steps per turn | 20 | `runner.py` `MAX_STEPS` | Then the turn is closed with a note. |
| Ledger `all` rows | newest 100 | `agent_tools.py` `ALL_LIMIT` | Flagged `truncated`; use `filter` for older ones. |
| Default model | `openai` | `providers.py` `DEFAULT_PROVIDER` | For new conversations. |

While a retry wait is running, Ctrl-C stops it.

## How it works

The plan has the full diagrams. In short:

```
Clerk -> CLI -> Main agent <-> model (OpenAI / Gemini / Groq)
                    |
                    +-- tools (AgentTools): switch_model, read, propose_post, execute_post
```

**Startup.** If SQLite holds an unfinished conversation, the CLI asks `continue` or `new`.
`continue` reloads the saved history, and the agent carries on if it stopped mid-turn.
`new` creates a separate conversation, and the old one stays open. With nothing unfinished,
a new conversation starts without asking.

**The loop** is `Agent.run()` in `src/fourx/runner.py`:

1. Ask the model.
2. If it answers in text, show the answer and stop.
3. If it asks for tools, run each one, save its result, and ask the model again.

Every message is saved to SQLite the moment it exists, so a restart loses nothing.

After each task, the system prompt has the agent ask whether to work on the next invoice
or help with anything else.

## The tools

The model is offered three tools. `switch_model` also has a second way in: the clerk can type it.

| Tool | What it does |
|---|---|
| `read` | Read-only. Ledger: `last`, `all` (newest 100) or `filter` by any column. Also reads invoices and vendors. A search with no match returns "No matching entries found." |
| `post` | Becomes `propose_post`, then `execute_post` after the clerk approves. |
| `switch_model` | Changes the model saved on this conversation. History and any waiting approval stay. Two ways in: typed by the clerk (checked on every input, so it works when the current model is out of quota), or called by the model when the clerk asks in plain words. |

### Posting

1. `propose_post(invoice_id)` checks, in order: the invoice file exists and is complete, the
   vendor is registered, the vendor is **active**, and the same vendor and invoice number
   are **not already in the ledger**. It writes nothing. Any failure is returned to the model
   as a reason, and the clerk is not asked.
2. The CLI shows the exact vendor, invoice number, date and amount, built from the files on
   disk. The model cannot supply an amount. It waits for `/approve` or `/decline <reason>`.
3. `execute_post` re-checks under a file lock, since an approval can wait and two terminals
   could race. If anything changed it refuses, otherwise it calls `ledger.post`.

Only the clerk's command approves. Model text never does.

## What is saved, and when

`data/conversations.db` holds two tables: conversations (model, `open` or `completed`,
last active time) and messages (user text, model replies with their tool calls, tool
results). It is separate from the ledger.

- **Quit during an approval.** The post call has no result yet, so the next launch asks for
  approval again. Nothing is posted in between.
- **Killed during a post.** On restart the post call is still open and is checked again. If
  the ledger already has the entry, the duplicate check refuses it, so it cannot be posted twice.
- **Stop (Ctrl-C).** Every unfinished call gets a "cancelled" result, so the next message
  is valid for any model. A post that has already started is allowed to finish.

## Switching models

There are two ways to switch, and they end in the same `switch_model` method.

**1. Typed, hard-coded, no model involved.** The CLI checks every line you type, before it
reaches the agent. If the line starts with `switch model` (any capitals or spacing), the CLI
switches itself:

```
you> switch model              ->  gemini, groq or openai?  (you type a name)
you> switch model groq         ->  Switched to groq.
you> switch model claude       ->  Not switched: 'claude' is not one of gemini, groq, openai.
```

No model is called, so this works even when the current model has run out of tokens or quota.
That is the reason it exists. Use it if the current model stops answering. It is available at the
`you>` prompt, not at a waiting `approval>` prompt.

**2. By the model.** `switch_model` is also a tool the model is offered. If you say "please use
gemini", the model calls it. This needs a working model, and the system prompt tells the model
to switch only when you explicitly ask.

Either way, a switch is refused if the name is not `openai`, `gemini` or `groq`, or if that
model's key or model name is missing in `.env`. If it is already the active model, nothing
changes. The switch applies to the current conversation only. The history stays.

**How history carries over.** History is stored in one neutral format (role, text, tool calls, tool results), not either
SDK's. Each adapter in `providers.py` converts it on every request, so switching only changes
which adapter reads the same history. Gemini needs its thought signatures sent back exactly,
so a Gemini reply also keeps its raw parts. Calls made by another model are sent to Gemini
with Google's documented placeholder signature. A 429, 5xx or timeout is retried with a wait.

## Layout

```
agent.py                  entry point
  ledger.py                runs src/fourx/ledger.py from the repo root
src/fourx/
  cli.py                  the prompt, continue-or-new question, commands
  runner.py               the agent loop, approval, stop
  agent_tools.py          the tools, all in one file
  tools.py                invoice, vendor and ledger logic behind the tools
  llms.py, providers.py   get_llm(name) and the OpenAI / Gemini / Groq adapters
  storage.py              SQLite conversations
  prompts.py              the system prompt
  ledger.py, vendors.json, invoices/    the supplied data (ledger.py is unchanged)
```

See [FourX_Agent_Implementation_Plan.md](FourX_Agent_Implementation_Plan.md) for the design,
the flow diagrams and the reasons behind the choices.
