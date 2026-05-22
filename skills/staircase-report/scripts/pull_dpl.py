#!/usr/bin/env python3
"""Incremental DPL puller for the persistent local cache.

Checks the cache under XDG_CACHE_HOME (default
~/.cache/agentic-analytics/dpl/<cache-dir>/<YYYY>/<MM>/<DD>/) and downloads
only the days missing from a requested window. Same cache layout staircase.py
reads, so a daily run becomes "pull yesterday, re-render."

One Parse.ly bucket commonly holds events for many sites mixed together at
the file level, so the cache is keyed by customer (or any string the caller
chooses), not by site ID. Use the same --cache-dir for every pull against
the same bucket so events aren't double-downloaded.

The caller is responsible for telling the script which S3 bucket the events
live in and which AWS profile (if any) to authenticate with. There is no
built-in customer-to-bucket map; the slash command, the wrapper, or future
plugin config supplies those per customer.

Usage:
    python3 pull_dpl.py --cache-dir acme \\
        --bucket parsely-dw-acme \\
        --start 2026-04-17 --end 2026-04-30

    python3 pull_dpl.py --cache-dir acme \\
        --bucket parsely-dw-acme --profile agentic-analytics \\
        --start 2026-04-17 --end 2026-04-30 --force
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
from typing import Iterable, List, Optional


def default_cache_root() -> str:
    """Match staircase.py: $XDG_CACHE_HOME/agentic-analytics/dpl, else ~/.cache."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = xdg if xdg else os.path.expanduser("~/.cache")
    return os.path.join(base, "agentic-analytics", "dpl")


def daterange(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def day_dir(cache_root: str, cache_dir: str, d: dt.date) -> str:
    return os.path.join(
        cache_root, cache_dir, f"{d.year:04d}", f"{d.month:02d}", f"{d.day:02d}"
    )


def day_has_data(path: str) -> bool:
    """Cheap completeness check: does this day's local dir contain any .gz files?

    Doesn't catch partial-day pulls (e.g., S3 still flushing the latest hour at
    pull time). Good enough for backfills and full-day re-runs; pass --force
    when you suspect a day is incomplete.
    """
    if not os.path.isdir(path):
        return False
    for root, _, files in os.walk(path):
        for f in files:
            if f.endswith(".gz"):
                return True
    return False


def s3_source(bucket: str, prefix: str, d: dt.date) -> str:
    """s3://<bucket>/<prefix><YYYY>/<MM>/<DD>/"""
    base = f"s3://{bucket}/{prefix}"
    return f"{base}{d.year:04d}/{d.month:02d}/{d.day:02d}/"


def aws_sync(src: str, dst: str, profile: Optional[str], dry_run: bool) -> int:
    cmd: List[str] = ["aws", "s3", "sync", src, dst, "--exclude", "*", "--include", "*.gz"]
    if profile:
        cmd += ["--profile", profile]
    if dry_run:
        cmd += ["--dryrun"]
    print("  $ " + " ".join(cmd), file=sys.stderr)
    return subprocess.call(cmd)


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache-dir", required=True,
                   help="Subdirectory under --cache-root that holds this "
                        "customer's events. Typically the customer's name or "
                        "slug (e.g. 'acme'). Should be the same for every "
                        "pull against the same bucket to avoid double-downloads.")
    p.add_argument("--bucket", required=True,
                   help="S3 bucket holding the customer's DPL events "
                        "(e.g. 'parsely-dw-<publisher>').")
    p.add_argument("--start", required=True, help="Start date YYYY-MM-DD (inclusive).")
    p.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive).")
    p.add_argument("--cache-root", default=default_cache_root(),
                   help="Cache root. Default: $XDG_CACHE_HOME/agentic-analytics/dpl "
                        "(falls back to ~/.cache/agentic-analytics/dpl).")
    p.add_argument("--prefix", default="events/",
                   help="S3 prefix inside the bucket. Default 'events/' "
                        "(the Parse.ly convention). Pass an empty string for "
                        "legacy buckets without a prefix.")
    p.add_argument("--profile", default=None,
                   help="AWS profile to authenticate with. Omit to use the "
                        "default credential chain.")
    p.add_argument("--force", action="store_true",
                   help="Re-pull days even if cache already has files for them.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be pulled without copying anything.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = make_parser().parse_args(argv)

    if shutil.which("aws") is None:
        print("aws CLI not found on PATH. Install awscli or activate the right env.",
              file=sys.stderr)
        return 2

    try:
        start = dt.datetime.strptime(args.start, "%Y-%m-%d").date()
        end = dt.datetime.strptime(args.end, "%Y-%m-%d").date()
    except ValueError as e:
        print(f"Bad date: {e}", file=sys.stderr)
        return 2
    if end < start:
        print("--end must be >= --start", file=sys.stderr)
        return 2

    bucket = args.bucket
    prefix = args.prefix
    profile = args.profile or None  # empty string => no profile

    days = list(daterange(start, end))
    cached: List[dt.date] = []
    missing: List[dt.date] = []
    for d in days:
        path = day_dir(args.cache_root, args.cache_dir, d)
        if not args.force and day_has_data(path):
            cached.append(d)
        else:
            missing.append(d)

    print(
        f"cache-dir={args.cache_dir} bucket=s3://{bucket}/{prefix} "
        f"profile={profile or '<default>'}",
        file=sys.stderr,
    )
    print(
        f"window={start}..{end}  cached={len(cached)}  to_pull={len(missing)}",
        file=sys.stderr,
    )
    if cached and not args.force:
        print(f"  cached: {', '.join(str(d) for d in cached)}", file=sys.stderr)
    if not missing:
        print("Nothing to pull.", file=sys.stderr)
        return 0

    failures: List[dt.date] = []
    for d in missing:
        dst = day_dir(args.cache_root, args.cache_dir, d)
        os.makedirs(dst, exist_ok=True)
        src = s3_source(bucket, prefix, d)
        print(f"Pulling {d} -> {dst}", file=sys.stderr)
        rc = aws_sync(src, dst, profile, args.dry_run)
        if rc != 0:
            failures.append(d)
            print(f"  ! aws s3 sync exited {rc} for {d}", file=sys.stderr)

    if failures:
        print(
            f"Done with {len(failures)} failure(s): "
            f"{', '.join(str(d) for d in failures)}",
            file=sys.stderr,
        )
        return 1
    print("Done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
