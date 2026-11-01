"""Exercise real assignment data and ledger code, only in temporary directories."""
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from fourx.tools import DEFAULT_ASSIGNMENT_DIR, LEDGER_COLUMNS, MODEL_TOOLS, READ_TOOL, ReadTools, ToolError


@pytest.fixture
def tools(tmp_path):
    assignment = tmp_path / "assignment"
    assignment.mkdir()
    for name in ("vendors.json", "ledger.py"):
        shutil.copy2(DEFAULT_ASSIGNMENT_DIR / name, assignment / name)
    shutil.copytree(DEFAULT_ASSIGNMENT_DIR / "invoices", assignment / "invoices")
    return ReadTools(assignment)


def test_complete_invoice_and_vendor_match_preserve_original_data(tools):
    invoice = tools.execute("read", {"source": "invoices", "mode": "single", "id": "invoice-06"})
    assert invoice == json.loads((tools.assignment_dir / "invoices/invoice-06.json").read_text())
    assert invoice["total"] == "21948.00"
    assert invoice["taxes"] == [
        {"type": "CGST", "rate_percent": 9, "amount": "1674.00"},
        {"type": "SGST", "rate_percent": 9, "amount": "1674.00"},
    ]
    assert len(invoice["lines"]) == 1
    assert tools.vendor_for_invoice("invoice-06")["vendor_id"] == "V-001"
    assert tools.lookup_vendor("  bluefin logistics llp  ")["vendor_id"] == "V-002"
    assert tools.lookup_vendor("v-004") == {
        "vendor_id": "V-004", "name": "Orbit Facility Services", "status": "blocked",
        "blocked_reason": "Bank details under verification", "payment_terms_days": 30,
    }
    assert not (tools.assignment_dir / "ledger.db").exists()


def test_list_contains_all_six_filename_ids_and_exact_totals(tools):
    summaries = tools.execute("read", {"source": "invoices", "mode": "all"})
    assert [item["invoice_id"] for item in summaries] == [f"invoice-{i:02}" for i in range(1, 7)]
    assert [item["total"] for item in summaries] == ["16862.20", "70800.00", "31978.00", "100300.00", "25960.00", "21948.00"]


def test_default_assignment_path_is_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ReadTools().read_invoice("invoice-01")["invoice_number"] == "SGP/26-27/0412"


@pytest.mark.parametrize("iid", ["../vendors", "invoice-01.json", "/tmp/invoice-01", "invoice-01/../invoice-02", None, "invoice-99"])
def test_invalid_or_missing_invoice_is_rejected(tools, iid):
    with pytest.raises(ToolError):
        tools.read_invoice(iid)


def test_valid_filename_cannot_escape_through_a_symlink(tools):
    path = tools.assignment_dir / "invoices/invoice-01.json"
    path.unlink()
    path.symlink_to(tools.assignment_dir / "vendors.json")
    with pytest.raises(ToolError, match="inside"):
        tools.read_invoice("invoice-01")


@pytest.mark.parametrize("change", [{"total": 21948.0}, {"total": "NaN"}, {"total": "-1"},
                                  {"invoice_date": "2026-02-30"}, {"lines": None}, {"currency": "inr"}])
def test_invalid_financial_source_data_is_rejected_without_posting(tools, change):
    path = tools.assignment_dir / "invoices/invoice-06.json"
    invoice = json.loads(path.read_text())
    path.write_text(json.dumps({**invoice, **change}))
    with pytest.raises(ToolError):
        tools.read_invoice("invoice-06")
    assert not (tools.assignment_dir / "ledger.db").exists()


def test_unknown_or_ambiguous_vendor_is_not_guessed(tools):
    with pytest.raises(ToolError, match="exactly one"):
        tools.lookup_vendor("Bluefin")
    path = tools.assignment_dir / "vendors.json"
    vendors = json.loads(path.read_text())
    vendors.append({**vendors[0], "vendor_id": "V-005"})
    path.write_text(json.dumps(vendors))
    with pytest.raises(ToolError, match="ambiguous"):
        tools.vendor_for_invoice("invoice-01")


def test_invalid_json_and_incomplete_assignment_have_clear_errors(tools, tmp_path):
    with pytest.raises(ToolError, match="Assignment directory"):
        ReadTools(tmp_path / "missing")
    (tools.assignment_dir / "invoices/invoice-06.json").write_text("{broken")
    with pytest.raises(ToolError, match="invalid JSON"):
        tools.read_invoice("invoice-06")


def seed_entries(tools):
    # The supplied read API initializes its opening entry. Additional test entries
    # go directly into this disposable database; the agent exposes no write tool.
    tools.read_ledger("all")
    with sqlite3.connect(tools.assignment_dir / "ledger.db") as conn:
        conn.executemany(
            "INSERT INTO entries(entry_id,vendor_id,invoice_number,invoice_date,currency,amount,reference,posted_at) VALUES (?,?,?,?,?,?,?,?)",
            [
                ("JE-TEST-2", "V-001", "SGP/26-27/0412", "2026-10-03", "INR", "16862.20", "fourx:conv-1:action-1", "2026-10-03T10:00:00+00:00"),
                ("JE-TEST-3", "V-002", "BL-88213", "2026-10-07", "INR", "70800.00", "fourx:conv-1:action-2", "2026-10-07T10:00:00+00:00"),
            ],
        )


def test_ledger_last_all_and_filters_use_actual_supplied_api(tools):
    seed_entries(tools)
    entries = tools.execute("read", {"source": "ledger", "mode": "all"})
    assert [entry["entry_id"] for entry in entries] == ["JE-000001", "JE-TEST-2", "JE-TEST-3"]
    assert tools.read_ledger("last") == [entries[-1]]
    assert tools.read_ledger("filter", {"vendor_id": "V-002"}) == [entries[0], entries[2]]
    assert tools.read_ledger("filter", {"vendor_id": "V-002", "invoice_number": "BL-88190"}) == [entries[0]]
    assert tools.read_ledger("filter", {"reference": "fourx:conv-1:action-1"}) == [entries[1]]
    assert tools.read_ledger("filter", {"invoice_number": "missing"}) == []
    assert tools.read_ledger("filter", {"invoice_number": "BL-88190' OR 1=1 --"}) == []
    assert len(tools.read_ledger("all")) == 3


def test_last_returns_empty_list_if_the_ledger_api_has_no_entries(tools, monkeypatch):
    module = tools._load_ledger()
    monkeypatch.setattr(module, "entries", lambda **filters: [])
    assert tools.read_ledger("last") == []


@pytest.mark.parametrize("action,filters", [
    ("delete", None), ("all", {"vendor_id": "V-002"}), ("filter", None), ("filter", {}),
    ("filter", {"amount": 25960}), ("filter", {"reference": None}),
    ("filter", {"vendor_id OR 1=1": "V-002"}), ("filter", {1: "V-002"}),
])
def test_bad_ledger_requests_fail_before_loading_or_initializing_ledger(tools, action, filters):
    with pytest.raises(ToolError):
        tools.read_ledger(action, filters)
    assert tools._ledger is None
    assert not (tools.assignment_dir / "ledger.db").exists()


@pytest.mark.parametrize("name,args", [("post", {"invoice_id": "invoice-01"}),
                                       ("switch_model", {"model": "groq"}),
                                       ("_load_ledger", {}), ("read_invoice", {"invoice_id": "invoice-01"}),
                                       ("read", {}), ("read", {"source": "invoices", "mode": "all", "sql": "DROP TABLE entries"})])
def test_dispatcher_only_allows_the_read_tool(tools, name, args):
    with pytest.raises(ToolError):
        tools.execute(name, args)
    assert not (tools.assignment_dir / "ledger.db").exists()


def test_model_is_offered_read_and_post_but_never_switch_model():
    assert [tool["name"] for tool in MODEL_TOOLS] == ["read", "post"]


def test_schema_describes_modes_and_ledger_columns_and_is_json_serializable():
    properties = READ_TOOL["parameters"]["properties"]
    assert properties["mode"]["enum"] == ["single", "filter", "all"]
    assert properties["source"]["enum"] == ["invoices", "vendors", "ledger"]
    assert all(column in READ_TOOL["description"] for column in LEDGER_COLUMNS)
    json.dumps(MODEL_TOOLS)


def read(tools, source, mode, **arguments):
    return tools.execute("read", {"source": source, "mode": mode, **arguments})


def test_read_invoices_single_all_and_filter(tools):
    assert read(tools, "invoices", "single", id="invoice-05")["invoice_number"] == "BL-88190"
    assert {row["invoice_id"] for row in read(tools, "invoices", "filter", filters={"vendor_name": "bluefin"})} == {"invoice-02", "invoice-05"}
    assert [row["invoice_id"] for row in read(tools, "invoices", "filter", filters={"vendor_id": "V-001", "currency": "inr"})] == ["invoice-01", "invoice-06"]
    assert read(tools, "invoices", "filter", filters={"invoice_number": "nope"}) == []


def test_read_vendors_single_all_and_filter(tools):
    assert read(tools, "vendors", "single", id="V-004")["blocked_reason"] == "Bank details under verification"
    assert len(read(tools, "vendors", "all")) == 4
    assert [v["vendor_id"] for v in read(tools, "vendors", "filter", filters={"status": "blocked"})] == ["V-004"]


def test_read_ledger_single_by_entry_id(tools):
    assert read(tools, "ledger", "single", id="JE-000001")["invoice_number"] == "BL-88190"
    with pytest.raises(ToolError, match="No ledger entry"):
        read(tools, "ledger", "single", id="JE-NOPE")
    assert [e["entry_id"] for e in read(tools, "ledger", "filter", filters={"vendor_id": "V-002"})] == ["JE-000001"]


def test_read_accepts_empty_unused_fields_but_rejects_wrong_combinations(tools):
    assert len(read(tools, "vendors", "all", id="", filters={})) == 4
    for bad in [dict(mode="single"), dict(mode="all", id="x"), dict(mode="filter"), dict(mode="filter", filters={"nope": "x"}),
                dict(mode="single", id="invoice-01", filters={"currency": "INR"}), dict(mode="delete")]:
        with pytest.raises(ToolError):
            tools.read(source="invoices", **bad)
    with pytest.raises(ToolError):
        tools.read(source="payroll", mode="all")


def test_read_tools_never_call_post_and_preserve_supplied_code(tools, monkeypatch):
    code_path = tools.assignment_dir / "ledger.py"
    before = hashlib.sha256(code_path.read_bytes()).hexdigest()
    def forbidden(**kwargs):
        pytest.fail("A read tool attempted posting")
    monkeypatch.setattr(tools._load_ledger(), "post", forbidden)
    tools.list_invoices()
    tools.lookup_vendor("V-004")
    tools.read_ledger("all")
    tools.read_ledger("filter", {"invoice_number": "BL-88190"})
    assert len(tools.read_ledger("all")) == 1
    assert hashlib.sha256(code_path.read_bytes()).hexdigest() == before
