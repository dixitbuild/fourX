"""All the agent's tools in one place: plain Python methods, called straight from the loop.

The model is offered three: `read`, `post` and `switch_model`. Posting is split in two methods so the clerk's
approval can sit between them: propose_post checks and builds the proposal, execute_post
posts it after approval. switch_model has two ways in: the CLI calls it directly when the clerk
types "switch model" (no model involved, so it works when the model is out of quota), and the
model calls it when the clerk asks in plain words.
Every method returns {"status": "ok" | "issue", ...}; an issue carries the reason in "message",
so the model can explain it to the clerk.
"""
from typing import Any

from .llms import get_llm
from .models import PROVIDER_NAMES, ToolDefinition
from .providers import ProviderError
from .storage import Store
from .tools import LEDGER_COLUMNS, ReadTools, ToolError

ALL_LIMIT = 100  # read(ledger, all) returns at most this many entries, the newest

READ_TOOL: ToolDefinition = {
    "name": "read",
    "description": (
        "Read-only; changes nothing. source=ledger: action last (newest entry), all (newest "
        f"{ALL_LIMIT} entries, oldest first) or filter (every column/value in filters must match exactly). "
        "Ledger columns: " + ", ".join(LEDGER_COLUMNS) + ". source=invoices or vendors: action single "
        "(id is a filename like invoice-06, or a vendor ID or full name), all, or filter."),
    "parameters": {"type": "object", "properties": {
        "source": {"type": "string", "enum": ["ledger", "invoices", "vendors"]},
        "action": {"type": "string", "enum": ["last", "all", "filter", "single"]},
        "id": {"type": "string", "description": "Only for action=single."},
        "filters": {"type": "object", "additionalProperties": {"type": "string"},
                    "description": "Only for action=filter: column name to wanted value."},
    }, "required": ["source", "action"], "additionalProperties": False},
}

POST_TOOL: ToolDefinition = {
    "name": "post",
    "description": "Ask to post an invoice to the ledger. The program checks it, then the clerk must approve it. "
                   "Never say an invoice was posted unless the result gives an entry_id.",
    "parameters": {"type": "object", "properties": {"invoice_id": {
        "type": "string", "description": "Filename without .json, for example invoice-01."}},
        "required": ["invoice_id"], "additionalProperties": False},
}

SWITCH_TOOL: ToolDefinition = {
    "name": "switch_model",
    "description": "Change which model answers from now on. Call it only when the clerk explicitly asks to switch.",
    "parameters": {"type": "object", "properties": {"model": {"type": "string", "enum": list(PROVIDER_NAMES)}},
                   "required": ["model"], "additionalProperties": False},
}


def issue(message: str) -> dict[str, Any]:
    return {"status": "issue", "message": message}


class AgentTools:
    def __init__(self, reader: ReadTools, store: Store):
        self.reader, self.store = reader, store
        self.definitions = [READ_TOOL, POST_TOOL, SWITCH_TOOL]  # what the model is offered

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a tool by name. A bad name or bad arguments come back as an issue, never a crash."""
        methods = {"read": self.read, "propose_post": self.propose_post,
                   "execute_post": self.execute_post, "switch_model": self.switch_model}
        if name not in methods:
            return issue(f"Unknown tool {name}.")
        try:
            return methods[name](**arguments)
        except ToolError as exc:  # a refusal with a reason the clerk can read
            return issue(str(exc))
        except (TypeError, OSError, ValueError) as exc:  # e.g. the model sent malformed arguments
            return issue(f"The {name} tool could not run: {exc}")

    def switch_model(self, conversation_id: str, model: str) -> dict[str, Any]:
        """Change which model (openai, gemini or groq) answers in this conversation."""
        name = model.strip().casefold()
        try:
            get_llm(name)  # unknown name, or its key or model missing in .env
        except ProviderError as exc:
            return issue(f"Not switched: {exc}")
        if self.store.provider(conversation_id) == name:
            return {"status": "ok", "message": f"Already using {name}; nothing changed."}
        self.store.set_provider(conversation_id, name)
        return {"status": "ok", "message": f"Switched to {name}."}

    def read(self, source: str, action: str, id: str | None = None, filters: dict[str, str] | None = None) -> dict[str, Any]:
        if source == "ledger":
            rows = self.reader.read_ledger(action, filters or None)  # type: ignore[arg-type]
            if action == "all" and len(rows) > ALL_LIMIT:
                return {"status": "ok", "rows": rows[-ALL_LIMIT:], "truncated": True, "total": len(rows),
                        "message": f"Showing the newest {ALL_LIMIT} of {len(rows)}; use filter to narrow down."}
        else:
            result = self.reader.read(source, action, id, filters)
            rows = result if isinstance(result, list) else [result]
        if not rows:
            return {"status": "ok", "rows": [], "message": "No matching entries found."}
        return {"status": "ok", "rows": rows}

    def propose_post(self, invoice_id: str) -> dict[str, Any]:
        """Step 1. Checks, in order: the invoice file exists and is complete, the vendor is registered
        and active, and the vendor's invoice number is not already in the ledger. Writes nothing."""
        return {"status": "ok", "proposal": self.reader.check_posting({"invoice_id": invoice_id})}

    def execute_post(self, invoice_id: str, reference: str, approved_proposal: dict[str, Any]) -> dict[str, Any]:
        """Step 2, after the clerk approved. Re-checks under a lock, refuses if anything changed, then posts."""
        with self.reader.posting_lock():
            proposal = self.reader.check_posting({"invoice_id": invoice_id})
            if proposal != approved_proposal:
                return issue("The invoice or vendor changed after approval; nothing was posted.")
            entry_id = self.reader.post(proposal, reference)
        return {"status": "ok", "entry_id": entry_id, "message": f"Posted as {entry_id}."}
