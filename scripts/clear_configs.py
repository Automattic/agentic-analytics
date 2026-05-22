#!/usr/bin/env python3
"""Clear the local config files written by /agentic-analytics:init.

Backs /agentic-analytics:clear-configs. Removes two things to put the user
back at first-run state:

- ``${XDG_CONFIG_HOME:-~/.config}/agentic-analytics/bucket.json`` — the runtime
  config file init writes.
- ``agentic-analytics`` profile sections from ``~/.aws/credentials`` and
  ``~/.aws/config`` — the AWS profile the user set up via ``aws configure``.

Does not touch the events cache at
``${XDG_CACHE_HOME:-~/.cache}/agentic-analytics/dpl/`` — re-pulling DPL is
slow; the cache is data, not config.

Stdlib only on purpose so it runs anywhere Python 3 is installed.
"""
from __future__ import annotations

import configparser
import os
import sys
from pathlib import Path
from typing import List, Optional

PROFILE = "agentic-analytics"


def _config_home() -> Path:
    """Match init.md: ${XDG_CONFIG_HOME:-~/.config}."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path(os.path.expanduser("~/.config"))
    return base


def clear_bucket_json(config_home: Optional[Path] = None) -> bool:
    """Delete bucket.json. Returns True if a file was removed."""
    root = config_home if config_home is not None else _config_home()
    path = root / "agentic-analytics" / "bucket.json"
    if path.exists():
        path.unlink()
        return True
    return False


def clear_aws_profile(
    credentials_path: Optional[Path] = None,
    config_path: Optional[Path] = None,
) -> List[str]:
    """Remove ``[agentic-analytics]`` and ``[profile agentic-analytics]`` from
    the AWS credentials and config files.

    Returns a list of ``"<path>: <section>"`` strings describing what was
    removed. Empty if nothing was removed.
    """
    cred = credentials_path if credentials_path is not None else Path(os.path.expanduser("~/.aws/credentials"))
    conf = config_path if config_path is not None else Path(os.path.expanduser("~/.aws/config"))
    removed: List[str] = []
    for path in (cred, conf):
        if not path.exists():
            continue
        cfg = configparser.ConfigParser()
        cfg.read(path)
        changed = False
        # Both `[agentic-analytics]` (credentials) and
        # `[profile agentic-analytics]` (config) variants — same profile,
        # different file convention.
        for section in (PROFILE, f"profile {PROFILE}"):
            if section in cfg:
                cfg.remove_section(section)
                changed = True
                removed.append(f"{path}: [{section}]")
        if changed:
            with open(path, "w") as h:
                cfg.write(h)
    return removed


def main() -> int:
    bucket = clear_bucket_json()
    aws = clear_aws_profile()

    print("Cleared:")
    if bucket:
        print("  - bucket.json")
    for line in aws:
        print(f"  - {line}")
    if not (bucket or aws):
        print("  (nothing to clear)")

    print()
    print("You're back at first-run state. Run /agentic-analytics:init to rebuild.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
