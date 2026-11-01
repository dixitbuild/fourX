"""The agent. Read run() first: it is the whole idea, a loop that asks the model and runs its tools."""
import signal
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from .agent_tools import AgentTools
from .llms import get_llm
from .models import Message, ToolCall
from .prompts import SYSTEM_PROMPT
from .providers import ProviderError
from .storage import Store

MAX_STEPS = 20
MAX_RETRIES = 4


class Quit(Exception):
    """The clerk typed /quit while an approval was waiting. The call stays open and is asked again next launch."""


def tool_message(call: ToolCall, status: str, output: Any) -> Message:
    return {"role": "tool", "content": "", "tool_result": {
        "call_id": call["call_id"], "name": call["name"], "status": status, "output": output}}


def open_calls(messages: list[Message]) -> list[ToolCall]:
    """Tool calls in the latest assistant message that have no result yet, in order."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index]["role"] == "assistant":
            answered = {m["tool_result"]["call_id"] for m in messages[index + 1:] if m["role"] == "tool"}
            return [call for call in messages[index].get("tool_calls", []) if call["call_id"] not in answered]
    return []


@contextmanager
def ignoring_ctrl_c():
    """A post that has started is never abandoned: Ctrl-C waits until the short call returns."""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


class Agent:
    def __init__(self, store: Store, toolbox: AgentTools, conversation_id: str, say: Callable[[str], None] = print,
                 ask: Callable[[str], str] = input, make_llm: Callable = get_llm):
        self.store, self.toolbox, self.conversation_id = store, toolbox, conversation_id
        self.say, self.ask, self.make_llm = say, ask, make_llm

    def messages(self) -> list[Message]:
        return self.store.messages(self.conversation_id)

    def save(self, message: Message) -> None:
        self.store.add_message(self.conversation_id, message)

    # ---- the loop -------------------------------------------------------------------------

    def run(self) -> None:
        """Ask the model. If it wants tools, run them, save the results and ask again.
        Stop when the model answers in plain text."""
        try:
            for _ in range(MAX_STEPS):
                calls = open_calls(self.messages())           # calls left over, e.g. after a restart
                if not calls:
                    reply = self.ask_model()
                    self.save(reply)
                    if not reply.get("tool_calls"):
                        self.say(reply["content"])            # a plain answer: the turn is over
                        return
                    calls = reply["tool_calls"]
                for call in calls:
                    self.save(self.run_tool(call))            # saved at once, so a restart loses nothing
            self.cancel(f"Stopped after {MAX_STEPS} steps without a final answer.")
        except ProviderError as exc:
            self.cancel(f"The model request failed: {str(exc)[:300]}\nYour message is saved. Try again, or type 'switch model'.")

    def needs_work(self) -> bool:
        """True when the saved conversation stopped mid-turn, so the agent should carry on."""
        messages = self.messages()
        return bool(messages) and not (messages[-1]["role"] == "assistant" and not messages[-1].get("tool_calls"))

    def send(self, text: str) -> None:
        self.save({"role": "user", "content": text})
        self.run()

    # ---- the model ------------------------------------------------------------------------

    def ask_model(self) -> Message:
        llm = self.make_llm(self.store.provider(self.conversation_id))
        for attempt in range(MAX_RETRIES + 1):
            try:
                return llm.complete(SYSTEM_PROMPT, self.messages(), self.toolbox.definitions)
            except ProviderError as exc:
                if not exc.retryable or attempt == MAX_RETRIES:
                    raise
                delay = 2 ** (attempt + 1)
                self.say(f"{llm.name} is busy; retrying in {delay}s.")
                time.sleep(delay)
        raise AssertionError("unreachable")

    # ---- the tools ------------------------------------------------------------------------

    def run_tool(self, call: ToolCall) -> Message:
        if call["name"] == "post":
            return self.post(call)
        if call["name"] == "switch_model":
            result = self.toolbox.call("switch_model", {"conversation_id": self.conversation_id,
                                                        "model": str(call["arguments"].get("model", ""))})
            return tool_message(call, "completed" if result["status"] == "ok" else "refused", result)
        if call["name"] != "read":
            return tool_message(call, "error", {"reason": f"Unknown tool {call['name']}."})
        result = self.toolbox.call("read", call["arguments"])
        return tool_message(call, "completed" if result["status"] == "ok" else "refused", result)

    def post(self, call: ToolCall) -> Message:
        """Check, wait for the clerk, then post. The model never approves."""
        invoice_id = call["arguments"].get("invoice_id", "")
        proposal = self.toolbox.call("propose_post", {"invoice_id": invoice_id})
        if proposal["status"] != "ok":
            return tool_message(call, "refused", {"posted": False, "reason": proposal["message"]})
        approved, reason = self.wait_for_clerk(proposal["proposal"])
        if not approved:
            return tool_message(call, "declined", {"posted": False, "declined_by_clerk": True, "reason": reason})
        with ignoring_ctrl_c():
            result = self.toolbox.call("execute_post", {
                "invoice_id": invoice_id, "reference": f"fourx:{self.conversation_id}:{call['call_id']}",
                "approved_proposal": proposal["proposal"]})
        if result["status"] != "ok":
            return tool_message(call, "refused", {"posted": False, "reason": result["message"]})
        self.say(f"Ledger entry saved: {result['entry_id']}")
        return tool_message(call, "completed", {"posted": True, "entry_id": result["entry_id"], **proposal["proposal"]["posting"]})

    def wait_for_clerk(self, proposal: dict[str, Any]) -> tuple[bool, str]:
        """Show the exact proposal and wait. Only /approve or /decline ends it; Ctrl-C does not."""
        print(describe(proposal))
        while True:
            try:
                line = self.ask("approval> ").strip()
            except KeyboardInterrupt:
                print("\nAn approval is waiting. Type /approve, /decline <reason> or /quit.")
                continue
            except EOFError:
                raise Quit from None
            command, _, reason = line.partition(" ")
            if command == "/approve":
                return True, ""
            if command == "/decline":
                return False, reason.strip() or "No reason given."
            if command == "/quit":
                raise Quit
            print(describe(proposal))

    # ---- stopping -------------------------------------------------------------------------

    def cancel(self, note: str) -> None:
        """End the turn cleanly: answer every open call, so either model accepts the next message."""
        messages = self.messages()
        calls = open_calls(messages)
        for call in calls:
            self.save(tool_message(call, "cancelled", {"reason": note}))
        if calls or messages[-1]["role"] != "assistant":
            self.save({"role": "assistant", "content": note})
        self.say(note)

    def switch(self, model: str) -> str:
        """The clerk's typed switch, with no model involved. Returns the tool's message."""
        return self.toolbox.call("switch_model", {"conversation_id": self.conversation_id, "model": model})["message"]


def describe(proposal: dict[str, Any]) -> str:
    details, vendor = proposal["posting"], proposal["vendor"]
    return (f"\n  Approval needed: post to the ledger\n"
            f"    vendor    {vendor['vendor_id']}  {vendor['name']}\n"
            f"    invoice   {details['invoice_number']}  dated {details['invoice_date']}\n"
            f"    amount    {details['currency']} {details['amount']}\n"
            f"  Type /approve, /decline <reason> or /quit\n")
