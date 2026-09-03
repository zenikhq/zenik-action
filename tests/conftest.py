"""Make the action's top-level modules importable from tests/ without an
install step (the action is run as scripts, not a package)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "vendor")):
    if p not in sys.path:
        sys.path.insert(0, p)
