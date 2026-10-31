"""Repository entry point: activate .venv, then run `python agent.py`."""
import sys
from pathlib import Path

if __name__ == "__main__":
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11+ is required. Activate .venv or use: uv run python agent.py")
    # Support this src-layout checkout without requiring an editable installation.
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from fourx import main
    main()
