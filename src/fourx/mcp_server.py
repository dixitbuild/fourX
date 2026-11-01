"""The MCP server: all agent tools in one file.

Tools: switch_model, read, and post (split in two so human approval can sit between the
steps: propose_post checks and builds the proposal, execute_post posts it after approval).
Every tool returns {"status": ..., ...}; a refused request carries the reason in "message",
so the model can explain it to the clerk. Run it with: python -m fourx.mcp_server
"""
import os
import signal
from typing import Any

from mcp.server.fastmcp import FastMCP

from .llms import get_llm
from .providers import ProviderError
from .storage import DEFAULT_DB, Store
from .tools import ReadTools, ToolError

ALL_LIMIT = 100  # read(ledger, all) returns at most this many entries, newest

mcp = FastMCP("fourx")
_tools: ReadTools | None = None


def tools() -> ReadTools:
    global _tools
    if _tools is None:
        _tools = ReadTools(os.environ.get("FOURX_ASSIGNMENT_DIR") or None)
    return _tools


def issue(message: str) -> dict[str, Any]:
    return {"status": "issue", "message": message}


@mcp.tool()
def switch_model(conversation_id: str, model: str) -> dict[str, Any]:
    """Change which model (openai, gemini or groq) answers in this conversation.
    Run only when the clerk asks. The conversation history stays the same."""
    store = Store(os.environ.get("SQLITE_PATH") or DEFAULT_DB)
    try:
        get_llm(model)  # unknown name, or key/model missing in .env
    except ProviderError as exc:
        return issue(f"Not switched: {exc}")
    name = model.strip().casefold()
    if store.provider(conversation_id) == name:
        return {"status": "ok", "message": f"Already using {name}; nothing changed."}
    store.set_provider(conversation_id, name)
    return {"status": "ok", "message": f"Switched to {name}."}


@mcp.tool()
def read(source: str, action: str, id: str | None = None, filters: dict[str, str] | None = None) -> dict[str, Any]:
    """Read-only; changes nothing.
    source=ledger: action last (newest entry), all (newest %d entries, oldest first) or filter
    (every column/value in filters must match exactly). Ledger columns: entry_id, vendor_id,
    invoice_number, invoice_date, currency, amount, reference, posted_at.
    source=invoices or vendors: action single (id is invoice-06 or a vendor ID/name), all, or filter."""
    try:
        if source == "ledger":
            rows = tools().read_ledger(action, filters or None)  # type: ignore[arg-type]
            total = len(rows)
            if action == "all" and total > ALL_LIMIT:
                return {"status": "ok", "rows": rows[-ALL_LIMIT:], "truncated": True, "total": total,
                        "message": f"Showing the newest {ALL_LIMIT} of {total}; use filter to narrow down."}
            if not rows:
                return {"status": "ok", "rows": [], "message": "No matching entries found."}
            return {"status": "ok", "rows": rows}
        result = tools().read(source, action, id, filters)
        rows = result if isinstance(result, list) else [result]
        return {"status": "ok", "rows": rows} if rows else {"status": "ok", "rows": [], "message": "No matching entries found."}
    except ToolError as exc:
        return issue(str(exc))


@mcp.tool()
def propose_post(invoice_id: str) -> dict[str, Any]:
    """Step 1 of posting an invoice. Checks, in order: the invoice file exists and is complete,
    the vendor is registered and active, and the vendor's invoice number is not already in the
    ledger. Writes nothing. On success it returns the exact proposal for the clerk to approve."""
    try:
        proposal = tools().check_posting({"invoice_id": invoice_id})
    except ToolError as exc:
        return issue(str(exc))
    return {"status": "ok", "proposal": proposal}


@mcp.tool()
def execute_post(invoice_id: str, reference: str, approved_proposal: dict[str, Any]) -> dict[str, Any]:
    """Step 2 of posting. The program calls it only after the clerk approved the proposal,
    never the model. Re-checks under a lock, refuses if anything changed, then posts."""
    reader = tools()
    try:
        with reader.posting_lock():
            proposal = reader.check_posting({"invoice_id": invoice_id})
            if proposal != approved_proposal:
                return issue("The invoice or vendor changed after approval; nothing was posted.")
            entry_id = reader.post(proposal, reference)
    except ToolError as exc:
        return issue(str(exc))
    return {"status": "ok", "entry_id": entry_id, "message": f"Posted as {entry_id}."}


if __name__ == "__main__":
    # Ctrl-C in the terminal reaches this process too; only the CLI decides what a stop means.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    mcp.run()
