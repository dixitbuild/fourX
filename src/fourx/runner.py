"""The agent loop: model call, tool calls, approval pauses, stop and crash recovery."""
import queue
import threading
from collections.abc import Callable
from typing import Any

from .models import Message, Posting, ToolCall
from .prompts import SYSTEM_PROMPT
from .providers import ProviderError, make_adapter
from .storage import Store
from .tools import MODEL_TOOLS, POST_TOOL, ReadTools, ToolError

MAX_STEPS = 20
MAX_RETRIES = 4


class Stopped(Exception):
    """The clerk stopped the turn."""


def tool_message(call: ToolCall, status: str, output: Any) -> Message:
    return {"role": "tool", "content": "", "tool_result": {
        "call_id": call["call_id"], "name": call["name"], "status": status, "output": output}}


def open_calls(messages: list[Message]) -> list[ToolCall]:
    """Calls in the latest assistant message that have no result yet, in order."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index]["role"] == "assistant":
            answered = {m["tool_result"]["call_id"] for m in messages[index + 1:] if m["role"] == "tool"}
            return [call for call in messages[index].get("tool_calls", []) if call["call_id"] not in answered]
    return []


class Agent:
    def __init__(self, store: Store, tools: ReadTools, conversation_id: str, provider: str,
                 say: Callable[[str], None] = print, tool_delay: float = 0,
                 make_provider: Callable = make_adapter):
        self.store, self.tools, self.conversation_id = store, tools, conversation_id
        self.say, self.tool_delay, self.make_provider = say, tool_delay, make_provider
        self.provider = provider
        self.stop = threading.Event()

    def pending(self) -> Posting | None:
        waiting = self.store.postings(self.conversation_id, "awaiting_approval")
        return waiting[0] if waiting else None

    def messages(self) -> list[Message]:
        return self.store.messages(self.conversation_id)

    def switch(self, provider: str) -> None:
        self.make_provider(provider)  # fail here, not on the next message, if it has no key
        self.store.set_provider(self.conversation_id, provider)
        self.provider = provider

    def send(self, text: str) -> None:
        self.store.add_message(self.conversation_id, {"role": "user", "content": text})
        self._run()

    def approve(self) -> None:
        posting = self.pending()
        if posting is None:
            return
        self._post(posting)
        self._run()

    def decline(self, reason: str) -> None:
        posting = self.pending()
        if posting is None:
            return
        reason = reason or "No reason given."
        self.store.finish_posting(posting, "declined", self._result(posting, "declined", {
            "posted": False, "declined_by_clerk": True, "reason": reason}))
        self._run()

    def recover(self) -> None:
        """Settle whatever a killed process left behind, before the clerk's next message."""
        for posting in self.store.postings(self.conversation_id, "approved"):
            self._reconcile(posting, after_crash=True)
        if self.pending() is None:
            self._close_turn("The previous turn was interrupted before it finished.", announce=False)

    def _result(self, posting: Posting, status: str, output: Any) -> Message:
        call = {"call_id": posting["call_id"], "name": POST_TOOL["name"], "arguments": {}}
        return tool_message(call, status, output)

    def _post(self, posting: Posting) -> None:
        with self.tools.posting_lock():
            try:
                # An approval can wait for days: check against today's files and ledger.
                current = self.tools.check_posting({"invoice_id": posting["proposal"]["invoice_id"]})
                if current != posting["proposal"]:
                    raise ToolError("The invoice or vendor changed after it was shown for approval.")
            except ToolError as exc:
                self.store.finish_posting(posting, "refused", self._result(posting, "refused", {"posted": False, "reason": str(exc)}))
                self.say(f"Not posted: {exc}")
                return
            # After this commit a crash can no longer lose track of the attempt.
            self.store.set_state(posting["action_id"], "approved")
            try:
                entry_id = self.tools.post(posting["proposal"], posting["reference"])
            except Exception:
                self._reconcile(posting, after_crash=False)
                return
        self._finish_posted(posting, entry_id)

    def _finish_posted(self, posting: Posting, entry_id: str) -> None:
        details = posting["proposal"]["posting"]
        self.store.finish_posting(posting, "posted", self._result(posting, "completed", {
            "posted": True, "entry_id": entry_id, "invoice_number": details["invoice_number"],
            "currency": details["currency"], "amount": details["amount"]}), entry_id)
        self.say(f"Ledger entry saved: {entry_id}")

    def _reconcile(self, posting: Posting, *, after_crash: bool) -> None:
        """Find out from the ledger what really happened. Never post again from here."""
        details = posting["proposal"]["posting"]
        entries = self.tools.entries_with_reference(posting["reference"])
        if len(entries) == 1 and all(entries[0][key] == value for key, value in details.items()):
            self._finish_posted(posting, entries[0]["entry_id"])
        elif entries:
            reason = f"Ledger entries with reference {posting['reference']} don't match what was approved; a person must review them."
            self.store.finish_posting(posting, "conflict", self._result(posting, "error", {"posted": False, "reason": reason}))
            self.say(reason)
        elif after_crash:
            # Nothing reached the ledger, and that approval was spent: ask again.
            self.store.set_state(posting["action_id"], "awaiting_approval")
            self.say("An approved post never reached the ledger. It needs approving again.")
        else:
            reason = "The ledger call failed and no entry was saved."
            self.store.finish_posting(posting, "failed", self._result(posting, "error", {"posted": False, "reason": reason}))
            self.say(f"Not posted: {reason}")

    def _run(self) -> None:
        try:
            for _ in range(MAX_STEPS):
                if self._work_through_calls():
                    return
                reply = self._call_model()
                self.store.add_message(self.conversation_id, reply)
                if not reply.get("tool_calls"):
                    self.say(reply["content"])
                    return
            self._close_turn(f"Stopped after {MAX_STEPS} steps without a final answer.")
        except Stopped:
            self._close_turn("The clerk stopped this turn.")
        except ProviderError as exc:
            self._close_turn(f"The model request failed: {str(exc)[:300]}\nYour message is saved. Try again, or type 'switch model'.")

    def _close_turn(self, note: str, announce: bool = True) -> None:
        """Answer every open call, so the next request to either provider is valid."""
        messages = self.messages()
        calls = open_calls(messages)
        for call in calls:
            self.store.add_message(self.conversation_id, tool_message(call, "cancelled", {"reason": note}))
        if calls or (messages and messages[-1]["role"] != "assistant"):
            self.store.add_message(self.conversation_id, {"role": "assistant", "content": note})
            if announce:
                self.say(note)

    def _work_through_calls(self) -> bool:
        """Run open calls in order. Return True on pausing for an approval."""
        for call in open_calls(self.messages()):
            self._check_stop()
            if call["name"] == POST_TOOL["name"]:
                if self._propose(call):
                    return True
            else:
                self.store.add_message(self.conversation_id, self._read(call))
        return False

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise Stopped

    def _read(self, call: ToolCall) -> Message:
        if self.stop.wait(self.tool_delay):
            raise Stopped
        try:
            return tool_message(call, "completed", self.tools.execute(call["name"], call["arguments"]))
        except (ToolError, OSError, TypeError, ValueError) as exc:
            return tool_message(call, "error", {"reason": str(exc)})

    def _propose(self, call: ToolCall) -> bool:
        try:
            proposal = self.tools.check_posting(call["arguments"])
        except (ToolError, OSError, ValueError) as exc:
            self.store.add_message(self.conversation_id, tool_message(call, "refused", {"posted": False, "reason": str(exc)}))
            return False
        self.store.propose(self.conversation_id, call["call_id"], proposal)
        return True

    def _call_model(self) -> Message:
        adapter = self.make_provider(self.provider)
        messages = self.messages()
        for attempt in range(MAX_RETRIES + 1):
            try:
                return self._call_once(adapter, messages)
            except ProviderError as exc:
                if not exc.retryable or attempt == MAX_RETRIES:
                    raise
                delay = 2 ** (attempt + 1)
                self.say(f"{adapter.name} is busy; retrying in {delay}s.")
                if self.stop.wait(delay):
                    raise Stopped from None
        raise AssertionError("unreachable")

    def _call_once(self, adapter: Any, messages: list[Message]) -> Message:
        """The SDK call can't be interrupted, so stop abandons it; its late reply goes nowhere."""
        outcome: queue.Queue = queue.Queue()

        def call() -> None:
            try:
                outcome.put((adapter.complete(SYSTEM_PROMPT, messages, MODEL_TOOLS), None))
            except Exception as exc:
                outcome.put((None, exc))

        threading.Thread(target=call, daemon=True).start()
        while True:
            self._check_stop()
            try:
                reply, error = outcome.get(timeout=0.1)
            except queue.Empty:
                continue
            if error is not None:
                raise error if isinstance(error, ProviderError) else ProviderError(str(error)) from error
            return reply


def describe(posting: Posting) -> str:
    details, vendor = posting["proposal"]["posting"], posting["proposal"]["vendor"]
    return (f"\n  Approval needed: post to the ledger\n"
            f"    vendor    {vendor['vendor_id']}  {vendor['name']}\n"
            f"    invoice   {details['invoice_number']}  dated {details['invoice_date']}\n"
            f"    amount    {details['currency']} {details['amount']}\n"
            f"  Type /approve or /decline <reason>\n")
