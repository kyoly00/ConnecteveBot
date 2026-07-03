"""Connecteve ConnBot FastAPI application package."""

from __future__ import annotations

import sys
from pathlib import Path

_CONN_BOT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _CONN_BOT_ROOT / "scripts"
for _p in (_CONN_BOT_ROOT, _SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
