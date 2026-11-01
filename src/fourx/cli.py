"""The chat prompt: pick a conversation, then read the clerk's lines and hand them to the agent."""
import os
from collections.abc import Callable

from .mcp_client import McpClient
from .providers import DEFAULT_PROVIDER, PROVIDERS
from .runner import Agent, Quit
from .storage import DEFAULT_DB, Store

HELP = (f'Commands: "switch model" ({", ".join(PROVIDERS)}), /finish, /quit. '
        "Press Ctrl-C while the agent is working to stop it.")


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


def switch_model(agent: Agent, ask: Callable[[str], str] = input) -> None:
    """Ask which model, compare the answer in code, and let the switch_model tool do it."""
    answer = ask(f"{', '.join(PROVIDERS[:-1])} or {PROVIDERS[-1]}? ").strip().casefold()
    print(agent.switch(answer) if answer in PROVIDERS else f"Not switched: {answer!r} is not one of {', '.join(PROVIDERS)}.")


def chat() -> None:
    store = Store(os.environ.get("SQLITE_PATH") or DEFAULT_DB)
    conversation_id, resumed = choose_conversation(store)
    mcp = McpClient()
    try:
        agent = Agent(store, mcp, conversation_id, say=lambda text: print(f"agent> {text}"))
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
                if " ".join(line.split()).casefold() == "switch model":
                    switch_model(agent)
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
    finally:
        mcp.close()


def main() -> None:
    chat()
