"""The chat prompt, plus read-only inspection commands that need no API key."""
import argparse
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from .providers import DEFAULT_PROVIDER, PROVIDERS, ProviderError
from .runner import Agent, describe
from .storage import Store
from .tools import ReadTools, ToolError

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "fourx.db"
HELP = (f'Commands: "switch model" ({", ".join(PROVIDERS)}), /approve, /decline <reason>, /quit. '
        "Press Ctrl-C while the agent is working to stop it.")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FourX accounts payable agent. With no command, opens the chat.")
    parser.add_argument("--conversation", default="default", help="Conversation to start or resume.")
    parser.add_argument("--assignment-dir", help="Directory containing the supplied ledger.py, vendors.json and invoices/.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("invoices", help="List invoice IDs and basic details.")
    invoice = commands.add_parser("invoice", help="Read a complete invoice.")
    invoice.add_argument("invoice_id")
    vendor = commands.add_parser("vendor", help="Look up a full vendor name or ID.")
    vendor.add_argument("query")
    ledger = commands.add_parser("ledger", help="Read the supplied ledger.")
    ledger.add_argument("action", choices=["last", "all", "filter"])
    ledger.add_argument("--where", action="append", metavar="COLUMN=VALUE", help="Exact equality; repeat for multiple columns (AND).")
    return parser


def execute_command(tools: ReadTools, args: argparse.Namespace) -> None:
    if args.command == "invoices":
        result = tools.list_invoices()
    elif args.command == "invoice":
        result = tools.read_invoice(args.invoice_id)
    elif args.command == "vendor":
        result = tools.lookup_vendor(args.query)
    else:
        filters = None
        if args.where:
            filters = {}
            for condition in args.where:
                key, separator, value = condition.partition("=")
                if not separator or key in filters:
                    raise ToolError("Use --where COLUMN=VALUE once per column.")
                filters[key] = value
        result = tools.read_ledger(args.action, filters)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def run_interruptibly(agent: Agent, work: Callable[..., None], *args: str) -> None:
    """Run a turn on a worker thread, so Ctrl-C reaches this one and can only ask it to stop."""
    agent.stop.clear()
    done = threading.Event()

    def target() -> None:
        try:
            work(*args)
        finally:
            done.set()

    threading.Thread(target=target).start()
    # Wait on an event, not join(): after Ctrl-C, join() can report the thread finished while
    # it is still running, and two threads must never use the database at once.
    while not done.is_set():
        try:
            done.wait(0.1)
        except KeyboardInterrupt:
            agent.stop.set()
            print("\nStopping...")


def say(text: str) -> None:
    print(f"agent> {text}")


def switch_model(agent: Agent, ask: Callable[[str], str] = input) -> None:
    """Run by the program, never by a model: ask which model, then compare the answer."""
    answer = ask(f"{', '.join(PROVIDERS[:-1])} or {PROVIDERS[-1]}? ").strip().casefold()
    for name in PROVIDERS:
        if answer == name:
            try:
                agent.switch(name)
            except ProviderError as exc:
                print(f"Error: {exc}")
            else:
                print(f"Switched to {name}.")
            return
    print(f"Not switched: {answer!r} is not one of {', '.join(PROVIDERS)}.")


def handle_command(agent: Agent, line: str) -> bool:
    """Run a slash command. Return False to quit."""
    command, _, rest = line.partition(" ")
    rest = rest.strip()
    if command == "/quit":
        return False
    if command in ("/approve", "/decline"):
        if agent.pending() is None:
            print("Nothing is waiting for approval.")
        elif command == "/approve":
            run_interruptibly(agent, agent.approve)
        else:
            run_interruptibly(agent, agent.decline, rest)
    else:
        print(HELP)
    return True


def chat(tools: ReadTools, conversation_id: str) -> None:
    store = Store(os.environ.get("SQLITE_PATH") or DEFAULT_DB)
    provider, resumed = store.open_conversation(conversation_id, DEFAULT_PROVIDER)
    agent = Agent(store, tools, conversation_id, provider, say=say,
                  tool_delay=float(os.environ.get("FOURX_TOOL_DELAY") or 0))
    agent.recover()
    state = f"resumed, {len(agent.messages())} messages" if resumed else "new"
    print(f'Conversation "{conversation_id}" ({state}). Model: {provider}.\n{HELP}')
    shown = None
    while True:
        posting = agent.pending()
        if posting and posting["action_id"] != shown:
            print(describe(posting))
            shown = posting["action_id"]
        try:
            line = input("you> ").strip()
        except KeyboardInterrupt:
            print("\nNothing is running. Use /quit to exit.")
            continue
        except EOFError:
            print()
            return
        if not line:
            continue
        if " ".join(line.split()).casefold() == "switch model":
            try:
                switch_model(agent)
            except (KeyboardInterrupt, EOFError):
                print("\nNot switched.")
        elif line.startswith("/"):
            if not handle_command(agent, line):
                return
        elif posting:
            # A provider won't accept a new message while a tool call has no result.
            print(describe(posting))
        else:
            run_interruptibly(agent, agent.send, line)


def main(argv: list[str] | None = None) -> None:
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        tools = ReadTools(args.assignment_dir)
        if args.command is None:
            chat(tools, args.conversation)
        else:
            execute_command(tools, args)
    except (ToolError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Error: {exc}\n")
