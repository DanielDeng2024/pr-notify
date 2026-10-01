"""Persisted snapshot: PR ref -> event keys that were true at the last poll."""
from __future__ import annotations

import json
import os
from pathlib import Path


def default_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "pr-notify" / "state.json"


def load(path: Path) -> dict[str, set[str]] | None:
    """None means first run (no baseline yet)."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None  # corrupt state: re-baseline rather than replay everything
    return {ref: set(keys) for ref, keys in raw.get("prs", {}).items()}


def save(path: Path, prs: dict[str, set[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "prs": {r: sorted(k) for r, k in prs.items()}}, indent=1))
    tmp.replace(path)
