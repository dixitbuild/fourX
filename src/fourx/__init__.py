"""FourX invoice agent, built one milestone at a time."""


def main() -> None:
    from .cli import main as cli_main
    cli_main()
