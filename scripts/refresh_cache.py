#!/usr/bin/env python3
"""Blow away the local data cache so the next report rebuilds it from scratch.

Backs /agentic-analytics:refresh-cache. Removes:

- The configured customer's DPL events cache at
  ``${XDG_CACHE_HOME:-~/.cache}/agentic-analytics/dpl/<cache_dir>/``.
- The shared DuckDB working directory at
  ``${XDG_CACHE_HOME:-~/.cache}/agentic-analytics/duckdb-tmp/`` (derived state
  any report can regenerate; safe to wipe).

Leaves config alone. If the bucket / profile / cache-dir are gone too, use
``/agentic-analytics:clear-configs`` instead.

The customer the cache belongs to is read from
``${XDG_CONFIG_HOME:-~/.config}/agentic-analytics/bucket.json`` (the file
``/agentic-analytics:init`` writes). The slash command is responsible for
re-pulling fresh data after this script runs; deletion and re-pull are split
so this script stays stdlib-only and offline-safe.

Stdlib only on purpose so it runs anywhere Python 3 is installed.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple


def _config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path(os.path.expanduser("~/.config"))


def _cache_root() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path(os.path.expanduser("~/.cache"))
    return base / "agentic-analytics"


def read_cache_dir(config_home: Optional[Path] = None) -> Optional[str]:
    """Return the ``cache_dir`` field from bucket.json, or None if absent."""
    root = config_home if config_home is not None else _config_home()
    path = root / "agentic-analytics" / "bucket.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    val = data.get("cache_dir")
    return val if isinstance(val, str) and val else None


def clear_dir(path: Path) -> Tuple[bool, int]:
    """Remove a directory tree. Returns (removed, bytes_freed).

    bytes_freed is best-effort: computed before deletion. If the dir doesn't
    exist or isn't a directory, returns (False, 0).
    """
    if not path.exists() or not path.is_dir():
        return False, 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    shutil.rmtree(path)
    return True, total


def _fmt_size(n: float) -> str:
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n / 1024:.1f} GB"


def main() -> int:
    cache_dir_name = read_cache_dir()
    if not cache_dir_name:
        print(
            "No bucket.json found; nothing to refresh. "
            "Run /agentic-analytics:init first.",
            file=sys.stderr,
        )
        return 1

    cache_root = _cache_root()
    print(f"Cleared cache for '{cache_dir_name}':")
    total_bytes = 0
    any_cleared = False
    for path in (cache_root / "dpl" / cache_dir_name, cache_root / "duckdb-tmp"):
        ok, freed = clear_dir(path)
        if ok:
            print(f"  - {path} ({_fmt_size(freed)})")
            total_bytes += freed
            any_cleared = True
    if any_cleared:
        print(f"  Freed: {_fmt_size(total_bytes)}")
    else:
        print("  (nothing to clear)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
