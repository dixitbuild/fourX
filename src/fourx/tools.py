"""Tools for the supplied invoices, vendor registry and ledger.

The read tools run through execute(). Posting is separate: the runner calls
check_posting() and post() itself, and only after a human approves. Ledger reads
use the supplied entries() API, which initializes its opening entry on first use.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import re
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from collections.abc import Iterator
from typing import Any, Literal, cast

from .models import PROVIDER_NAMES, LedgerEntry, Proposal, ToolDefinition, Vendor

DEFAULT_ASSIGNMENT_DIR = Path(__file__).resolve().parent  # ledger.py, vendors.json and invoices/ sit beside this file

LEDGER_COLUMNS = {
    "entry_id": "Ledger entry identifier, for example JE-000001.",
    "vendor_id": "Registered vendor identifier, for example V-002.",
    "invoice_number": "Vendor invoice number, for example BL-88190; not the filename invoice-05.",
    "invoice_date": "Invoice date as YYYY-MM-DD.",
    "currency": "Currency code, for example INR.",
    "amount": "Exact decimal text, for example 25960.00; use the stored format for equality filters.",
    "reference": "Optional free-text posting reference; opening entries may have null references.",
    "posted_at": "Posting timestamp, including timezone. Filtering uses exact text equality, not a date range.",
}


def _definition(name: str, description: str, properties: dict, required: list[str]) -> ToolDefinition:
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False,
    }}


READ_SOURCES = ("invoices", "vendors", "ledger")
READ_MODES = ("single", "filter", "all")
INVOICE_FIELDS = ("invoice_id", "invoice_number", "vendor_name", "vendor_id", "invoice_date", "currency", "total")
VENDOR_FIELDS = ("vendor_id", "name", "status")

READ_TOOL = _definition(
    "read",
    "Read-only; changes nothing. source=invoices: id is a filename such as invoice-06; single returns the full invoice, "
    "all/filter return summaries. source=vendors: id is a vendor ID or full name; single returns status and blocked reason. "
    "source=ledger: id is an entry_id such as JE-000001. mode=all returns everything, mode=filter returns items matching "
    "every field in filters. Invoice filter fields: " + ", ".join(INVOICE_FIELDS) + ". Vendor filter fields: " + ", ".join(VENDOR_FIELDS)
    + ". Invoice and vendor filters ignore case, and vendor_name/name match part of a name. Ledger filters are exact equality "
    "on these columns, oldest first by posted_at: " + "; ".join(f"{k}: {v}" for k, v in LEDGER_COLUMNS.items()),
    {
        "source": {"type": "string", "enum": list(READ_SOURCES)},
        "mode": {"type": "string", "enum": list(READ_MODES),
                 "description": "single needs id; filter needs filters; all needs neither."},
        "id": {"type": "string", "description": "Only for mode=single."},
        "filters": {"type": "object", "additionalProperties": {"type": "string"},
                    "description": "Only for mode=filter: field name to wanted value, all must match."},
    }, ["source", "mode"])

POST_TOOL = _definition("post", "Ask to post an invoice to the ledger. The program checks it, then a human must approve it. A result is only final when it says so; never claim an invoice was posted without an entry_id.", {
    "invoice_id": {"type": "string", "description": "Filename without .json, for example invoice-01."},
}, ["invoice_id"])

# Run by the program itself when the clerk types "switch model"; the model is never offered it,
# so a model that is out of quota cannot block a switch.
SWITCH_TOOL = _definition("switch_model", "Change which model answers next.", {
    "model": {"type": "string", "enum": list(PROVIDER_NAMES)},
}, ["model"])

MODEL_TOOLS = [READ_TOOL, POST_TOOL]
ALL_TOOLS = [READ_TOOL, POST_TOOL, SWITCH_TOOL]


class ToolError(ValueError):
    """An invalid tool request or unusable source data; safe to explain to the clerk."""


def resolve_assignment_dir(directory: str | Path | None = None) -> Path:
    directory = Path(directory) if directory is not None else DEFAULT_ASSIGNMENT_DIR
    directory = directory.expanduser().resolve()
    if not (directory / "invoices").is_dir() or any(
        not (directory / name).is_file() for name in ("vendors.json", "ledger.py")
    ):
        raise ToolError("Assignment directory must contain invoices/, vendors.json and the supplied ledger.py.")
    return directory


def _read_json(path: Path, description: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ToolError(f"Cannot read {description}: file missing, unreadable or invalid JSON.") from None


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{field} must be a nonempty string.")
    return value


def _money(value: Any, field: str, *, positive: bool = False) -> Decimal:
    _text(value, field)
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ToolError(f"{field} must be a decimal string.") from None
    if not amount.is_finite() or amount < 0 or (positive and amount == 0):
        raise ToolError(f"{field} must be {'positive' if positive else 'nonnegative'} and finite.")
    return amount


def _check_arguments(name: str, arguments: Any, parameters: dict[str, Any]) -> None:
    if not isinstance(arguments, dict) or not set(parameters["required"]).issubset(arguments) or set(arguments) - set(parameters["properties"]):
        raise ToolError(f"Invalid arguments for {name}.")


def _same_number(left: str, right: str) -> bool:
    return left.strip().casefold() == right.strip().casefold()


def _match(rows: list[Any], filters: dict[str, str], fields: tuple[str, ...]) -> list[Any]:
    if any(key not in fields or not isinstance(value, str) or not value.strip() for key, value in filters.items()):
        raise ToolError("Filter fields must be among: " + ", ".join(fields) + "; values must be nonempty text.")

    def same(row: Any, key: str, value: str) -> bool:
        have, want = str(row.get(key) or "").casefold(), value.strip().casefold()
        return want in have if key in ("vendor_name", "name") else have == want

    return [row for row in rows if all(same(row, key, value) for key, value in filters.items())]


class ReadTools:
    def __init__(self, assignment_dir: str | Path | None = None):
        self.assignment_dir = resolve_assignment_dir(assignment_dir)
        self._ledger: ModuleType | None = None

    def read_invoice(self, invoice_id: str) -> dict[str, Any]:
        if not isinstance(invoice_id, str) or not re.fullmatch(r"invoice-[0-9]{2}", invoice_id):
            raise ToolError("Use an invoice filename ID such as invoice-01, without .json.")
        invoice_dir = (self.assignment_dir / "invoices").resolve()
        path = (invoice_dir / f"{invoice_id}.json").resolve()
        if path.parent != invoice_dir:
            raise ToolError("Invoice file must stay inside the assignment invoices directory.")
        invoice = _read_json(path, invoice_id)
        if not isinstance(invoice, dict):
            raise ToolError(f"{invoice_id} must contain a JSON object.")
        for field in ("invoice_number", "vendor_name", "currency", "invoice_date", "due_date"):
            _text(invoice.get(field), field)
        for field in ("invoice_date", "due_date"):
            try:
                if date.fromisoformat(invoice[field]).isoformat() != invoice[field]:
                    raise ValueError
            except ValueError:
                raise ToolError(f"{field} must be a valid YYYY-MM-DD date.") from None
        if not re.fullmatch(r"[A-Z]{3}", invoice["currency"]):
            raise ToolError("currency must be a three-letter uppercase code.")
        _money(invoice.get("total"), "total", positive=True)
        _money(invoice.get("subtotal"), "subtotal")
        for field in ("lines", "taxes"):
            if not isinstance(invoice.get(field), list):
                raise ToolError(f"{field} must be a list.")
            for item in invoice[field]:
                if not isinstance(item, dict):
                    raise ToolError(f"Each item in {field} must be an object.")
                _money(item.get("amount"), f"{field}.amount")
                if field == "lines":
                    _money(item.get("unit_price"), "lines.unit_price")
        # Return the entire source object: no rounding or invented accounting rules.
        return invoice

    def list_invoices(self) -> list[dict[str, Any]]:
        summaries = []
        for path in sorted((self.assignment_dir / "invoices").glob("invoice-*.json")):
            invoice = self.read_invoice(path.stem)
            summaries.append({"invoice_id": path.stem, **{
                key: invoice[key] for key in ("invoice_number", "vendor_name", "invoice_date", "currency", "total")
            }})
        return summaries

    def _load_vendors(self) -> list[Vendor]:
        vendors = _read_json(self.assignment_dir / "vendors.json", "vendor registry")
        if not isinstance(vendors, list):
            raise ToolError("Vendor registry must be a JSON list.")
        seen_ids, seen_names = set(), set()
        for vendor in vendors:
            if not isinstance(vendor, dict):
                raise ToolError("Each vendor must be an object.")
            vid = _text(vendor.get("vendor_id"), "vendor_id").strip().casefold()
            name = _text(vendor.get("name"), "vendor name").strip().casefold()
            if vendor.get("status") not in ("active", "blocked"):
                raise ToolError("Vendor status must be active or blocked.")
            if vendor["status"] == "blocked":
                _text(vendor.get("blocked_reason"), "blocked_reason")
            if vid in seen_ids or name in seen_names:
                raise ToolError("Vendor registry contains duplicate IDs or ambiguous vendor names.")
            seen_ids.add(vid)
            seen_names.add(name)
        return cast(list[Vendor], vendors)

    def lookup_vendor(self, query: str) -> Vendor:
        query = _text(query, "query").strip().casefold()
        matches = [vendor for vendor in self._load_vendors()
                   if query in (vendor["vendor_id"].strip().casefold(), vendor["name"].strip().casefold())]
        if len(matches) != 1:
            raise ToolError("Vendor must match exactly one full registered name or vendor ID.")
        return matches[0]

    def vendor_for_invoice(self, invoice_id: str) -> Vendor:
        return self.lookup_vendor(self.read_invoice(invoice_id)["vendor_name"])

    def _load_ledger(self) -> ModuleType:
        if self._ledger is None:
            path = self.assignment_dir / "ledger.py"
            spec = importlib.util.spec_from_file_location("fourx_supplied_ledger", path)
            if spec is None or spec.loader is None:
                raise ToolError("Cannot load the supplied ledger module.")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._ledger = module
        return self._ledger

    def read_ledger(self, action: Literal["last", "all", "filter"], filters: dict[str, str] | None = None) -> list[LedgerEntry]:
        if not isinstance(action, str) or action not in ("last", "all", "filter"):
            raise ToolError("Ledger action must be last, all or filter.")
        if action == "filter":
            if not isinstance(filters, dict) or not filters:
                raise ToolError("filter requires at least one column/value pair.")
            if any(not isinstance(key, str) or key not in LEDGER_COLUMNS for key in filters):
                raise ToolError("Unknown ledger column. Allowed: " + ", ".join(LEDGER_COLUMNS))
            if any(not isinstance(value, str) for value in filters.values()):
                raise ToolError("Ledger filter values must be strings; comparisons use exact equality.")
        elif filters is not None:
            raise ToolError("Only the filter action accepts filters.")
        entries = self._load_ledger().entries(**(filters or {}))
        return entries[-1:] if action == "last" else entries

    def check_posting(self, arguments: dict[str, Any]) -> Proposal:
        """Build the proposal from disk, or raise ToolError if it must not be posted."""
        _check_arguments("post_invoice", arguments, POST_TOOL["parameters"])
        invoice_id = arguments["invoice_id"]
        invoice = self.read_invoice(invoice_id)
        vendor = self.lookup_vendor(invoice["vendor_name"])
        if vendor["status"] != "active":
            raise ToolError(f"Vendor {vendor['vendor_id']} ({vendor['name']}) is blocked: {vendor.get('blocked_reason')}.")
        for entry in self._load_ledger().entries(vendor_id=vendor["vendor_id"]):
            if _same_number(entry["invoice_number"], invoice["invoice_number"]):
                raise ToolError(f"{invoice['invoice_number']} from {vendor['vendor_id']} is already in the ledger as {entry['entry_id']}.")
        return {"invoice_id": invoice_id, "invoice": invoice, "vendor": vendor, "posting": {
            "vendor_id": vendor["vendor_id"], "invoice_number": invoice["invoice_number"],
            "invoice_date": invoice["invoice_date"], "currency": invoice["currency"], "amount": invoice["total"],
        }}

    def post(self, proposal: Proposal, reference: str) -> str:
        return self._load_ledger().post(**proposal["posting"], reference=reference)

    def entries_with_reference(self, reference: str) -> list[LedgerEntry]:
        return self._load_ledger().entries(reference=reference)

    @contextmanager
    def posting_lock(self) -> Iterator[None]:
        """Hold across the final check and post so two processes cannot both pass the check."""
        with open(self.assignment_dir / "ledger.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _invoice_rows(self) -> list[dict[str, Any]]:
        rows = []
        for summary in self.list_invoices():
            try:
                vendor_id = self.lookup_vendor(summary["vendor_name"])["vendor_id"]
            except ToolError:
                vendor_id = None
            rows.append({**summary, "vendor_id": vendor_id})
        return rows

    def read(self, source: str, mode: str, id: str | None = None, filters: dict[str, str] | None = None) -> Any:
        id, filters = id or None, filters or None  # models often send empty values for unused fields
        if source not in READ_SOURCES or mode not in READ_MODES:
            raise ToolError(f"source must be one of {', '.join(READ_SOURCES)}; mode one of {', '.join(READ_MODES)}.")
        if (mode == "single") != (id is not None) or (mode == "filter") != (filters is not None):
            raise ToolError("single needs only id, filter needs only filters, all needs neither.")
        if source == "ledger":
            if mode == "single":
                found = self.read_ledger("filter", {"entry_id": _text(id, "id")})
                if not found:
                    raise ToolError(f"No ledger entry {id}.")
                return found[0]
            return self.read_ledger(mode, filters)
        if source == "invoices":
            if mode == "single":
                return self.read_invoice(id)
            rows, fields = self._invoice_rows(), INVOICE_FIELDS
        else:
            if mode == "single":
                return self.lookup_vendor(id)
            rows, fields = self._load_vendors(), VENDOR_FIELDS
        return rows if mode == "all" else _match(rows, filters, fields)

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        """The one read tool the model can call; post is separate and switch_model is the program's."""
        if name != READ_TOOL["name"]:
            raise ToolError("Unknown read tool. Allowed: read")
        _check_arguments(name, arguments, READ_TOOL["parameters"])
        return self.read(**arguments)
