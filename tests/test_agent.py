"""Harness behaviour with a scripted provider, real invoices and a temporary ledger."""
import shutil
import threading
import time
from types import SimpleNamespace

import pytest

from fourx.providers import ProviderError
from fourx.runner import Agent, open_calls
from fourx.storage import Store
from fourx.tools import DEFAULT_ASSIGNMENT_DIR, ReadTools


def call(name, call_id="c1", **arguments):
    return {"call_id": call_id, "name": name, "arguments": arguments}


def calls(*tool_calls):
    return {"role": "assistant", "content": "", "tool_calls": list(tool_calls)}


def text(content="Done."):
    return {"role": "assistant", "content": content}


class Scripted:
    """Plays back replies, and keeps every history it was sent."""
    name = "scripted"

    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def complete(self, system, messages, tools):
        self.seen.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply()
        return reply


@pytest.fixture
def tools(tmp_path):
    folder = tmp_path / "assignment"
    folder.mkdir()
    for name in ("vendors.json", "ledger.py"):
        shutil.copy2(DEFAULT_ASSIGNMENT_DIR / name, folder / name)
    shutil.copytree(DEFAULT_ASSIGNMENT_DIR / "invoices", folder / "invoices")
    tools = ReadTools(folder)
    tools._load_ledger().time = SimpleNamespace(sleep=lambda seconds: None)  # skip the 2-second post
    return tools


def agent_for(tools, tmp_path, *replies, **options):
    provider = Scripted(*replies)
    store = Store(tmp_path / "chat.db")
    store.open_conversation("t", "scripted")
    said = []
    agent = Agent(store, tools, "t", "scripted", say=said.append, make_provider=lambda name: provider, **options)
    return agent, provider, said


def ledger_numbers(tools):
    return [entry["invoice_number"] for entry in tools.read_ledger("all")]


def result_of(messages, call_id="c1"):
    return next(m["tool_result"] for m in messages if m["role"] == "tool" and m["tool_result"]["call_id"] == call_id)


def test_approve_posts_once_with_exact_values_and_reports_back(tools, tmp_path):
    agent, provider, said = agent_for(tools, tmp_path, calls(call("post", invoice_id="invoice-01")), text("Posted."))
    agent.send("post invoice-01")
    assert agent.pending()["proposal"]["posting"] == {
        "vendor_id": "V-001", "invoice_number": "SGP/26-27/0412", "invoice_date": "2026-10-03",
        "currency": "INR", "amount": "16862.20"}
    assert ledger_numbers(tools) == ["BL-88190"]
    agent.approve()
    assert ledger_numbers(tools) == ["BL-88190", "SGP/26-27/0412"]
    assert result_of(provider.seen[-1])["output"]["entry_id"].startswith("JE-")
    assert agent.pending() is None and said[-1] == "Posted."


def test_waiting_approval_survives_restart(tools, tmp_path):
    agent, _, _ = agent_for(tools, tmp_path, calls(call("post", invoice_id="invoice-02")))
    agent.send("post invoice-02")
    agent.store.conn.close()
    again, provider, _ = agent_for(tools, tmp_path, text("Posted."))
    again.recover()
    assert again.pending()["proposal"]["posting"]["invoice_number"] == "BL-88213"
    again.approve()
    assert ledger_numbers(tools) == ["BL-88190", "BL-88213"]
    assert provider.seen[0][0] == {"role": "user", "content": "post invoice-02"}


def test_decline_tells_the_model_the_reason_and_posts_nothing(tools, tmp_path):
    agent, provider, _ = agent_for(tools, tmp_path, calls(call("post", invoice_id="invoice-03")), text("Not posted."))
    agent.send("post invoice-03")
    agent.decline("PO not received yet")
    result = result_of(provider.seen[-1])
    assert result["status"] == "declined" and result["output"]["reason"] == "PO not received yet"
    assert ledger_numbers(tools) == ["BL-88190"]


@pytest.mark.parametrize("invoice_id,expected", [("invoice-04", "Bank details under verification"), ("invoice-05", "JE-000001")])
def test_blocked_and_duplicate_are_refused_without_approval(tools, tmp_path, invoice_id, expected):
    agent, provider, _ = agent_for(tools, tmp_path, calls(call("post", invoice_id=invoice_id)), text("Refused."))
    agent.send(f"post {invoice_id}")
    result = result_of(provider.seen[-1])
    assert result["status"] == "refused" and expected in result["output"]["reason"]
    assert agent.pending() is None and ledger_numbers(tools) == ["BL-88190"]


def test_approval_rechecks_what_changed_while_it_waited(tools, tmp_path):
    agent, provider, said = agent_for(tools, tmp_path, calls(call("post", invoice_id="invoice-01")), text("Not posted."))
    agent.send("post invoice-01")
    tools.post(agent.pending()["proposal"], "someone-else")
    agent.approve()
    assert result_of(provider.seen[-1])["status"] == "refused"
    assert ledger_numbers(tools).count("SGP/26-27/0412") == 1


def test_duplicate_call_in_one_batch_posts_once(tools, tmp_path):
    agent, provider, _ = agent_for(tools, tmp_path, calls(
        call("post", "c1", invoice_id="invoice-01"), call("post", "c2", invoice_id="invoice-01")), text("Done."))
    agent.send("post invoice-01 twice")
    agent.approve()
    assert result_of(provider.seen[-1], "c2")["status"] == "refused"
    assert ledger_numbers(tools).count("SGP/26-27/0412") == 1


def test_model_cannot_pass_its_own_amount(tools, tmp_path):
    agent, provider, _ = agent_for(tools, tmp_path, calls(call("post", invoice_id="invoice-01", amount="1.00")), text("No."))
    agent.send("post invoice-01 for 1 rupee")
    assert result_of(provider.seen[-1])["status"] == "refused" and agent.pending() is None


def test_crash_after_ledger_commit_recovers_the_entry_without_reposting(tools, tmp_path):
    agent, _, _ = agent_for(tools, tmp_path, calls(call("post", invoice_id="invoice-01")))
    agent.send("post invoice-01")
    posting = agent.pending()
    agent.store.set_state(posting["action_id"], "approved")   # as if killed mid-post
    entry_id = tools.post(posting["proposal"], posting["reference"])
    agent.store.conn.close()
    again, provider, said = agent_for(tools, tmp_path, text("Yes, posted."))
    again.recover()
    assert ledger_numbers(tools).count("SGP/26-27/0412") == 1 and again.pending() is None
    again.send("did it post?")
    assert result_of(provider.seen[-1])["output"]["entry_id"] == entry_id


def test_crash_before_ledger_commit_needs_fresh_approval(tools, tmp_path):
    agent, _, _ = agent_for(tools, tmp_path, calls(call("post", invoice_id="invoice-01")))
    agent.send("post invoice-01")
    agent.store.set_state(agent.pending()["action_id"], "approved")
    agent.store.conn.close()
    again, _, _ = agent_for(tools, tmp_path)
    again.recover()
    assert again.pending() is not None and ledger_numbers(tools) == ["BL-88190"]


def test_crash_recovery_with_mismatched_entry_needs_review(tools, tmp_path):
    agent, _, _ = agent_for(tools, tmp_path, calls(call("post", invoice_id="invoice-01")))
    agent.send("post invoice-01")
    posting = agent.pending()
    agent.store.set_state(posting["action_id"], "approved")
    tools._load_ledger().post(**{**posting["proposal"]["posting"], "amount": "1.00"}, reference=posting["reference"])
    agent.store.conn.close()
    again, _, said = agent_for(tools, tmp_path)
    again.recover()
    assert again.store.postings("t", "conflict") and "review" in said[0]


def test_ledger_failure_that_still_saved_is_reported_as_posted(tools, tmp_path, monkeypatch):
    agent, provider, _ = agent_for(tools, tmp_path, calls(call("post", invoice_id="invoice-01")), text("Posted."))
    agent.send("post invoice-01")
    real = tools.post

    def dies_after_saving(proposal, reference):
        real(proposal, reference)
        raise TimeoutError("connection lost")

    monkeypatch.setattr(tools, "post", dies_after_saving)
    agent.approve()
    assert result_of(provider.seen[-1])["output"]["posted"] is True
    assert ledger_numbers(tools).count("SGP/26-27/0412") == 1


def run_in_thread(function, *args):
    worker = threading.Thread(target=function, args=args)
    worker.start()
    return worker


def test_stop_during_tools_keeps_finished_results_and_closes_the_rest(tools, tmp_path):
    batch = calls(*[call("read", f"c{n}", source="invoices", mode="single", id=f"invoice-0{n}") for n in (1, 2, 3)])
    agent, provider, _ = agent_for(tools, tmp_path, batch, text("Only invoice-01."), tool_delay=0.2)
    worker = run_in_thread(agent.send, "read everything")
    time.sleep(0.3)
    agent.stop.set()
    worker.join(3)
    assert not worker.is_alive()
    history = agent.messages()
    statuses = [m["tool_result"]["status"] for m in history if m["role"] == "tool"]
    assert statuses[0] == "completed" and statuses[-1] == "cancelled" and len(statuses) == 3
    assert open_calls(history) == []
    agent.stop.clear()
    agent.send("which did you finish?")
    assert provider.replies == []


def test_stop_abandons_a_slow_model_call_and_discards_its_reply(tools, tmp_path):
    def slow():
        time.sleep(0.5)
        return calls(call("post", invoice_id="invoice-01"))
    agent, _, said = agent_for(tools, tmp_path, slow)
    worker = run_in_thread(agent.send, "post invoice-01")
    time.sleep(0.1)
    started = time.monotonic()
    agent.stop.set()
    worker.join(3)
    assert time.monotonic() - started < 0.4
    time.sleep(0.6)
    assert agent.pending() is None and said == ["The clerk stopped this turn."]


def test_stop_during_a_post_waits_for_the_true_outcome(tools, tmp_path, monkeypatch):
    agent, _, said = agent_for(tools, tmp_path, calls(call("post", invoice_id="invoice-01")))
    agent.send("post invoice-01")
    real = tools.post

    def post_then_stop(proposal, reference):
        agent.stop.set()
        return real(proposal, reference)

    monkeypatch.setattr(tools, "post", post_then_stop)
    agent.approve()
    assert ledger_numbers(tools).count("SGP/26-27/0412") == 1
    assert any(line.startswith("Ledger entry saved") for line in said)


def test_retries_when_busy_and_keeps_conversation_usable_after_failure(tools, tmp_path, monkeypatch):
    monkeypatch.setattr("fourx.runner.MAX_RETRIES", 1)
    agent, provider, said = agent_for(tools, tmp_path, ProviderError("busy", retryable=True), text("Hello."), ProviderError("bad key"), text("Back."))
    monkeypatch.setattr(agent.stop, "wait", lambda seconds: False)
    agent.send("hi")
    assert said[-1] == "Hello."
    agent.send("again")
    assert "bad key" in said[-1]
    agent.send("still there?")
    assert said[-1] == "Back."


HISTORY = [
    {"role": "user", "content": "what's on invoice-06?"},
    {"role": "assistant", "content": "", "tool_calls": [call("read_invoice", "a", invoice_id="invoice-06"), call("list_invoices", "b")]},
    {"role": "tool", "content": "", "tool_result": {"call_id": "a", "name": "read_invoice", "status": "completed", "output": {"total": "21948.00"}}},
    {"role": "tool", "content": "", "tool_result": {"call_id": "b", "name": "list_invoices", "status": "cancelled", "output": {"reason": "stopped"}}},
    {"role": "assistant", "content": "INR 21948.00"},
]


def test_chat_conversion_keeps_calls_and_matching_results():
    from fourx.providers import ChatAdapter
    converted = ChatAdapter.to_messages("sys", HISTORY)
    assert [m["role"] for m in converted] == ["system", "user", "assistant", "tool", "tool", "assistant"]
    assert [c["id"] for c in converted[2]["tool_calls"]] == ["a", "b"]
    assert [m["tool_call_id"] for m in converted[3:5]] == ["a", "b"]


def test_gemini_conversion_groups_a_batch_and_marks_foreign_calls():
    from fourx.providers import FOREIGN_THOUGHT_SIGNATURE, GeminiAdapter
    contents = GeminiAdapter.to_contents(HISTORY)
    assert [c.role for c in contents] == ["user", "model", "user", "model"]
    assert len(contents[2].parts) == 2 and contents[2].parts[0].function_response.name == "read_invoice"
    assert contents[1].parts[0].thought_signature == FOREIGN_THOUGHT_SIGNATURE
    assert contents[1].parts[1].thought_signature is None


def test_ctrl_c_waits_for_the_turn_to_finish_closing(tools, tmp_path):
    import os
    import signal
    from fourx.cli import run_interruptibly
    batch = calls(*[call("read", f"c{n}", source="invoices", mode="single", id=f"invoice-0{n}") for n in (1, 2, 3)])
    agent, _, _ = agent_for(tools, tmp_path, batch, text("unused"), tool_delay=0.5)
    threading.Timer(0.7, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    run_interruptibly(agent, agent.send, "read everything")
    assert threading.active_count() == 1 or all(t.name != "Thread-1 (target)" for t in threading.enumerate())
    history = agent.messages()
    assert open_calls(history) == [] and history[-1]["role"] == "assistant"
    assert [m["tool_result"]["status"] for m in history if m["role"] == "tool"][-1] == "cancelled"


def test_daily_gemini_quota_is_not_retried(monkeypatch):
    from google.genai import errors
    from fourx.providers import GeminiAdapter, ProviderError
    adapter = GeminiAdapter.__new__(GeminiAdapter)
    adapter.model = "m"

    def quota(**kwargs):
        raise errors.ClientError(429, {"error": {"message": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}})

    adapter.client = SimpleNamespace(models=SimpleNamespace(generate_content=quota))
    with pytest.raises(ProviderError) as caught:
        adapter.complete("s", [{"role": "user", "content": "hi"}], [])
    assert caught.value.retryable is False


def test_typing_switch_model_asks_and_compares_without_calling_any_model(tools, tmp_path, capsys):
    from fourx.cli import switch_model
    agent, provider, _ = agent_for(tools, tmp_path)
    switched = []
    agent.switch = switched.append
    for answer in ("  GROQ ", "OpenAI", "gemini", "claude", ""):
        switch_model(agent, ask=lambda prompt, answer=answer: answer)
    assert switched == ["groq", "openai", "gemini"]
    assert provider.seen == [] and "Not switched" in capsys.readouterr().out
