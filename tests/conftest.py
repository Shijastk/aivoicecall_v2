"""
Shared test configuration.

Puts `scripts/` on the import path so the test suite can drive the same
fake-Vobiz protocol implementation that the CLI stub uses -- one model of
the wire format, not two that can drift apart.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

for path in (REPO_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
