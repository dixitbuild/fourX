# FourX: accounts payable agent

A command-line agent for an Acme Foods clerk: it reads invoices, checks vendors, and
posts to the supplied ledger only after a person approves. The agent loop, storage and
provider conversion are written by hand on the Gemini, Groq and OpenAI SDKs; no agent framework.

## Run

```sh
uv sync                                    # or: python -m venv .venv && pip install -e .[dev]
cp .env.example .env                       # set the API key and model for each provider you use
python assignment/take-home/ledger.py reset
python agent.py --conversation demo
```

New conversations start on OpenAI. Commands: type `switch model` (then gemini, groq or openai), `/approve`, `/decline <reason>`, `/quit`. Ctrl-C while the
agent is working stops it. Models used: `openai/gpt-oss-120b` (Groq), `gpt-4o-mini` (OpenAI) and `gemini-3.8-flash`.
`pytest -q` runs 63 tests with a scripted provider and a temporary ledger copy.
`FOURX_TOOL_DELAY=3` slows each read tool so test case 5 can be stopped by hand.

**Layout.** The brief suggests putting the code next to `ledger.py`. It lives in `src/fourx/`
instead and loads the untouched `assignment/take-home/ledger.py` by path, so there is one
ledger and one `ledger.db` (`assignment/take-home/ledger.db`).

## What is saved, and when

One SQLite file, `data/fourx.db`, separate from the ledger. Each write commits before the
agent moves on: the user message, each model reply (with its tool calls), each tool result,
the provider choice, and each posting attempt (exact proposal, state, reference, entry id).
A tool result and the posting state it belongs to are written in one transaction.
Quit anywhere and restart with the same `--conversation`: the history, provider and any
waiting approval come back.

## Tools, providers and switching

The model is offered two tools: `read` (source `invoices`, `vendors` or `ledger`; mode `single`,
`filter` or `all`) and `post` (one invoice, checked and approved before anything is written).
A third tool, `switch_model`, is defined but only the program runs it: when the clerk types exactly
`switch model`, the program asks `gemini, groq or openai?` and compares the answer in code. No model
is called, so it works even when the current model is out of quota or tokens. Anything else is
refused. These are local Python tools, not a separate MCP server.

History is stored in our own format (role, text, tool calls, tool results), not either SDK's.
Each adapter converts it on every request (Groq and OpenAI share one, since their formats match),
so switching just changes which adapter reads the same history, including earlier tool calls and
results. Conversations saved before this change may contain the older tool names in their history.
Gemini needs its thought signatures sent back exactly, so a Gemini reply also keeps its raw
parts, used only when Gemini reads its own turn. Tool calls made by Groq or OpenAI are sent to Gemini
with Google's documented placeholder signature; I confirmed live that Gemini rejects such a
call without one (HTTP 400) and accepts it with one. A 429, 5xx or timeout is retried with a
wait that Ctrl-C can interrupt; a Gemini per-day quota error is not retried.

## Approval, safety and stop

- `post` does not write by itself. Code builds the proposal from the invoice and
  vendor files and shows vendor, invoice number, date and amount. The model cannot supply
  an amount.
- A blocked vendor, or the same vendor and invoice number already in the ledger, is refused
  before any approval is asked. Both checks run again after `/approve`, under a file lock,
  because an approval can wait and two terminals could race.
- Before calling `ledger.post`, the intent is saved with reference `fourx:<conversation>:<id>`.
  The ledger commits partway through its call, so after a kill or error the agent looks the
  entry up by that reference, records it if it exists, and never reposts. If nothing was
  saved, it asks for approval again. A mismatching entry is flagged for a person.
- Typing a normal message while an approval waits just shows the approval again; `switch model` still works.
- Ctrl-C asks the worker thread to stop. A model call is abandoned (its late reply is
  discarded) and a tool loop stops between calls. Every unfinished call gets a "cancelled"
  result, so the next request is valid for either provider. A post that has started is
  allowed to finish, so the outcome is known and recorded.

## Verified live

Earlier build (Groq, with tools named differently): cases 1, 2 (quit at the approval, restart,
approve), 3, 6 and 7 behaved as expected and the ledger ended with exactly the 3 expected entries.
Case 5 stopped at once with the tools slowed, and the next answer said only invoice-01 was read.
Case 4 went Groq -> OpenAI -> Groq with correct answers (Shree Ganesh Packaging Pvt Ltd,
INR 21948.00, one line).

After the `read`/`post`/`switch model` redesign, live and by hand: `read` filter on invoices and
vendors, `read` single on the ledger, `post invoice-04` refused as blocked, `switch model` to Groq,
to Gemini (which then failed fast on its exhausted daily quota) and back to OpenAI, and a bad name
refused. Cases 1, 2, 3, 5 and 7 were **not** re-run live after the redesign; the automated tests
cover them with the new tool names. Gemini's leg of case 4 is not verified live (its free quota
ran out); its conversion is unit-tested and the signature behaviour was checked directly.

## Not handled / next

- No process-wide cancel of a hung SDK call; stop abandons it, and its thread lingers.
- The model can still write a wrong summary; only posting is enforced in code.
- The lock is a local file, so it only protects processes on one machine.
- Next: run a switch through Gemini once quota resets; a test for crashing between save and post.
