"""The chat prompt: pick a conversation, then read the clerk's lines and hand them to the agent."""
import os
from collections.abc import Callable

from .agent_tools import AgentTools
from .providers import DEFAULT_PROVIDER, PROVIDERS
from .runner import Agent, Quit
from .storage import DEFAULT_DB, Store
from .tools import ReadTools

HELP = (f'Commands: "switch model" or "switch model <{"|".join(PROVIDERS)}>", /finish, /quit. '
        "You can also ask the agent in words to switch. Press Ctrl-C while it is working to stop it.")


def choose_conversation(store: Store, ask: Callable[[str], str] = input) -> tuple[str, bool]:
    """Return (conversation id, resumed). With an unfinished conversation, the clerk chooses."""
    last = store.latest_open()
    if last is None:
        return store.create(DEFAULT_PROVIDER), False
    while True:
        answer = ask(f'Unfinished conversation "{last}". Continue it or start new? (continue/new) ').strip().casefold()
        if answer == "continue":
            return last, True
        if answer == "new":
            return store.create(DEFAULT_PROVIDER), False


def work(agent: Agent, task: Callable[..., None], *args: str) -> None:
    """Run one pass of the agent. Ctrl-C stops it; an /quit at an approval leaves it for next time."""
    try:
        task(*args)
    except KeyboardInterrupt:
        agent.cancel("The clerk stopped this turn.")


def switch_model(agent: Agent, name: str = "", ask: Callable[[str], str] = input) -> None:
    """The clerk's own switch, handled before any model is called. Ask which model unless it was typed."""
    answer = name or ask(f"{', '.join(PROVIDERS[:-1])} or {PROVIDERS[-1]}? ").strip().casefold()
    print(agent.switch(answer) if answer in PROVIDERS else f"Not switched: {answer!r} is not one of {', '.join(PROVIDERS)}.")


def chat() -> None:
    store = Store(os.environ.get("SQLITE_PATH") or DEFAULT_DB)
    try:
        conversation_id, resumed = choose_conversation(store)
    except (EOFError, KeyboardInterrupt):
        print()
        return
    agent = Agent(store, AgentTools(ReadTools(), store), conversation_id, say=lambda text: print(f"agent> {text}"))
    state = f"resumed, {len(agent.messages())} messages" if resumed else "new"
    print(f'Conversation "{conversation_id}" ({state}). Model: {store.provider(conversation_id)}.\n{HELP}')
    try:
        if resumed and agent.needs_work():
            work(agent, agent.run)  # the conversation stopped mid-turn: the agent carries on
        while True:
            try:
                line = input("you> ").strip()
            except KeyboardInterrupt:
                print("\nNothing is running. Use /quit to exit.")
                continue
            if not line:
                continue
            words = line.casefold().split()
            if words[:2] == ["switch", "model"]:  # checked on every input, so it never needs a model
                switch_model(agent, " ".join(words[2:]))
            elif line == "/quit":
                return
            elif line == "/finish":
                store.finish(conversation_id)
                print("Conversation finished. The next launch starts a new one.")
                return
            elif line.startswith("/"):
                print(HELP)
            else:
                work(agent, agent.send, line)
    except (Quit, EOFError):
        print("\nGoodbye. An approval that was waiting will be asked again when you continue.")


def main() -> None:
    chat()
