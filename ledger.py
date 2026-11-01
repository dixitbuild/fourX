"""Run the supplied ledger from the repo root: `python ledger.py` lists entries, `python ledger.py reset` resets.

The real ledger is src/fourx/ledger.py, kept unchanged; this only runs it so the commands work from here.
"""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "src" / "fourx" / "ledger.py"), run_name="__main__")
