"""The agent loop with a scripted model, the real tool functions and a temporary ledger."""
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fourx.agent_tools import AgentTools
from fourx.cli import choose_conversation, switch_model
from fourx.providers import ProviderError
from fourx.runner import Agent, Quit, open_calls
from fourx.storage import Store
from fourx.tools import DEFAULT_ASSIGNMENT_DIR, ReadTools


def call(name, call_id="c1", **arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{"call_id": call_id, "name": name, "arguments": arguments}]}


def text(content="Done."):
    return {"role": "assistant", "content": content}


class Scripted:
    """Plays back replies in order and remembers every history it was sent."""
    name = "scripted"

    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def complete(self, system, messages, tools):
        self.seen.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def setup(tmp_path, monkeypatch):
    folder = tmp_path / "assignment"
    folder.mkdir()
    shutil.copy2(DEFAULT_ASSIGNMENT_DIR / "vendors.json", folder)
    shutil.copy2(DEFAULT_ASSIGNMENT_DIR / "ledger.py", folder)
    shutil.copytree(DEFAULT_ASSIGNMENT_DIR / "invoices", folder / "invoices")
    reader = ReadTools(folder)
    reader._load_ledger().time = SimpleNamespace(sleep=lambda seconds: None)  # skip the 2-second post
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GEMINI_MODEL", "m")
    return reader, Store(tmp_path / "chat.db")


def make_agent(setup, *replies, answers=()):
    reader, store = setup
    llm, said, asked = Scripted(*replies), [], iter(answers)
    agent = Agent(store, AgentTools(reader, store), store.create("openai"), say=said.append, ask=lambda prompt: next(asked),
                  make_llm=lambda name: llm)
    return agent, llm, said, reader


def ledger_numbers(reader):
    return [entry["invoice_number"] for entry in reader.read_ledger("all")]


def test_plain_question_gets_an_answer_without_tools(setup):
    agent, llm, said, _ = make_agent(setup, text("Hello."))
    agent.send("hi")
    assert said == ["Hello."]
    assert [m["role"] for m in agent.messages()] == ["user", "assistant"]


def test_read_tool_result_goes_back_to_the_model(setup):
    agent, llm, said, _ = make_agent(setup, call("read", source="ledger", action="last"), text("Last is JE-000001."))
    agent.send("last entry?")
    result = llm.seen[1][-1]["tool_result"]
    assert result["status"] == "completed" and result["output"]["rows"][0]["entry_id"] == "JE-000001"
    assert said == ["Last is JE-000001."]


def test_read_with_no_match_says_so(setup):
    agent, llm, _, _ = make_agent(setup, call("read", source="ledger", action="filter", filters={"vendor_id": "V-999"}), text("None."))
    agent.send("anything for V-999?")
    assert llm.seen[1][-1]["tool_result"]["output"]["message"] == "No matching entries found."


def test_approved_post_writes_one_ledger_entry(setup):
    agent, llm, said, reader = make_agent(setup, call("post", invoice_id="invoice-01"), text("Posted."), answers=["/approve"])
    agent.send("post invoice-01")
    result = llm.seen[1][-1]["tool_result"]
    assert result["status"] == "completed" and result["output"]["posted"] is True
    assert ledger_numbers(reader).count("SGP/26-27/0412") == 1
    assert any(line.startswith("Ledger entry saved: JE-") for line in said)


def test_declined_post_writes_nothing_and_tells_the_model_why(setup):
    agent, llm, _, reader = make_agent(setup, call("post", invoice_id="invoice-03"), text("Not posted."),
                                       answers=["/decline PO not received yet"])
    agent.send("post invoice-03")
    result = llm.seen[1][-1]["tool_result"]
    assert result["status"] == "declined" and result["output"]["reason"] == "PO not received yet"
    assert len(ledger_numbers(reader)) == 1


@pytest.mark.parametrize("invoice, reason", [("invoice-04", "blocked"), ("invoice-05", "already in the ledger"),
                                             ("invoice-99", "Cannot read")])
def test_refused_post_never_asks_for_approval(setup, invoice, reason):
    agent, llm, _, reader = make_agent(setup, call("post", invoice_id=invoice), text("Refused."), answers=[])
    agent.send(f"post {invoice}")  # an approval prompt would call next(answers) and fail the test
    result = llm.seen[1][-1]["tool_result"]
    assert result["status"] == "refused" and reason in result["output"]["reason"]
    assert len(ledger_numbers(reader)) == 1


def test_quit_at_approval_keeps_the_call_and_resume_asks_again(setup):
    agent, llm, said, reader = make_agent(setup, call("post", invoice_id="invoice-02"), answers=["/quit"])
    with pytest.raises(Quit):
        agent.send("post invoice-02")
    assert len(open_calls(agent.messages())) == 1 and agent.needs_work()
    llm.replies = [text("Posted.")]
    agent.ask = lambda prompt: "/approve"
    agent.run()
    assert ledger_numbers(reader).count("BL-88213") == 1


def test_same_invoice_cannot_be_posted_twice(setup):
    agent, llm, _, reader = make_agent(setup, call("post", "a", invoice_id="invoice-01"), text("ok"),
                                       call("post", "b", invoice_id="invoice-01"), text("refused"), answers=["/approve"])
    agent.send("post invoice-01")
    agent.send("post invoice-01 again")
    assert llm.seen[3][-1]["tool_result"]["status"] == "refused"
    assert ledger_numbers(reader).count("SGP/26-27/0412") == 1


def test_stop_closes_open_calls_so_the_next_message_works(setup):
    agent, llm, _, _ = make_agent(setup, call("read", source="ledger", action="all"), text("Back."))
    agent.save({"role": "user", "content": "read everything"})
    agent.save(call("read", source="ledger", action="all"))
    agent.cancel("The clerk stopped this turn.")
    assert open_calls(agent.messages()) == []
    agent.send("hello again")  # accepted: no call is left without a result
    assert agent.messages()[-1]["content"] == "Back."


def test_model_failure_ends_the_turn_and_keeps_the_message(setup):
    agent, llm, said, _ = make_agent(setup, ProviderError("quota", retryable=False))
    agent.send("hi")
    assert "model request failed" in said[0] and agent.messages()[0]["content"] == "hi"
    assert not agent.needs_work()


def test_busy_model_is_retried(setup, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    agent, llm, said, _ = make_agent(setup, ProviderError("busy", retryable=True), text("Hello."))
    agent.send("hi")
    assert said[-1] == "Hello." and any("retrying" in line for line in said)


def test_loop_stops_after_too_many_steps(setup):
    agent, llm, said, _ = make_agent(setup, *[call("read", f"c{i}", source="ledger", action="last") for i in range(25)])
    agent.send("loop forever")
    assert "steps" in said[-1]


def test_switch_model_changes_the_saved_provider_and_keeps_history(setup):
    agent, llm, _, _ = make_agent(setup, text("Hello."))
    agent.send("hi")
    assert agent.switch("gemini") == "Switched to gemini."
    assert agent.store.provider(agent.conversation_id) == "gemini"
    assert len(agent.messages()) == 2
    assert agent.switch("gemini") == "Already using gemini; nothing changed."


def test_switch_to_an_unconfigured_model_is_refused(setup, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr("fourx.providers.os.environ", {})  # nothing configured
    agent, *_ = make_agent(setup)
    assert agent.switch("groq").startswith("Not switched")
    assert agent.store.provider(agent.conversation_id) == "openai"


def test_new_conversation_when_nothing_is_open(setup):
    _, store = setup
    conversation_id, resumed = choose_conversation(store, ask=lambda prompt: pytest.fail("should not ask"))
    assert not resumed and store.latest_open() == conversation_id


def test_clerk_chooses_to_continue_or_start_new(setup):
    _, store = setup
    old = store.create("openai")
    assert choose_conversation(store, ask=lambda prompt: "continue") == (old, True)
    answers = iter(["maybe", "NEW"])
    new_id, resumed = choose_conversation(store, ask=lambda prompt: next(answers))
    assert not resumed and store.latest_open() in (old, new_id)


def test_finished_conversation_is_not_offered_again(setup):
    _, store = setup
    old = store.create("openai")
    store.finish(old)
    assert store.latest_open() is None


def test_clerk_can_switch_without_any_model_call(setup, capsys):
    agent, llm, _, _ = make_agent(setup)  # the scripted model has no replies: any call would fail
    switch_model(agent, "gemini")
    assert agent.store.provider(agent.conversation_id) == "gemini"
    switch_model(agent, "", ask=lambda prompt: "claude")
    assert "Not switched: 'claude'" in capsys.readouterr().out
    assert llm.seen == []


def test_model_can_switch_when_the_clerk_asks_and_the_next_call_uses_the_new_model(setup):
    agent, llm, said, _ = make_agent(setup, call("switch_model", model="gemini"), text("Switched."))
    agent.send("please use gemini")
    assert llm.seen[1][-1]["tool_result"]["output"]["message"] == "Switched to gemini."
    assert agent.store.provider(agent.conversation_id) == "gemini"


def test_model_is_offered_read_post_and_switch(setup):
    agent, *_ = make_agent(setup)
    assert [tool["name"] for tool in agent.toolbox.definitions] == ["read", "post", "switch_model"]
