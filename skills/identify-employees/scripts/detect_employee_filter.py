#!/usr/bin/env python3
"""Detect employee-traffic tagging in cached DPL events.

Scans recent gzipped DPL events for `extra_data` keys matching the
internal-traffic whitelist. Auto-writes the discovered filter into
bucket.json when (a) the key matches the whitelist, (b) the value is
boolean-ish, and (c) the tagged share is between 0.1% and 25% of sampled
pageviews. Ambiguous setups are a silent no-op.

Stdlib-only on purpose so we can throw it away cheaply.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, Optional, Tuple


# Customer-defined keys that conventionally tag employee / internal traffic.
# Matched case-insensitively against `extra_data` keys.
INTERNAL_KEY_WHITELIST = (
    "internal",
    "is_internal",
    "is_employee",
    "employee",
    "staff",
    "internal_user",
    "internaluser",
    "internal_traffic",
)

# Values that count as the "tagged-internal" side of a boolean-ish flag.
TRUTHY_VALUES = (True, 1, "1", "true", "yes")

# Share-of-events guardrails. Below 0.1% is probably noise / a different
# concept; above 25% is too much to be employee traffic and is more likely a
# generic flag with a confusingly-named key.
MIN_SHARE_PCT = 0.1
MAX_SHARE_PCT = 25.0


def _is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return value in TRUTHY_VALUES


def collect_extra_data_stats(
    cache_dir: str, max_files: Optional[int] = None
) -> Tuple[Counter, int]:
    """Walk gzipped DPL events under cache_dir, return ((key, value-json) -> count, total pageviews).

    Most recent files first; max_files caps the read for speed.
    """
    files = []
    for root, _, fns in os.walk(cache_dir):
        for fn in fns:
            if fn.endswith(".gz"):
                files.append(os.path.join(root, fn))
    files.sort(reverse=True)
    if max_files:
        files = files[:max_files]

    counts: Counter = Counter()
    total = 0
    for path in files:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("action") != "pageview":
                        continue
                    total += 1
                    ed = ev.get("extra_data")
                    if not isinstance(ed, dict):
                        continue
                    for k, v in ed.items():
                        if not isinstance(k, str):
                            continue
                        try:
                            v_json = json.dumps(v, sort_keys=True)
                        except (TypeError, ValueError):
                            continue
                        counts[(k, v_json)] += 1
        except OSError:
            continue
    return counts, total


def detect_filter(counts: Counter, total: int) -> Optional[Dict[str, Any]]:
    """Return a filter dict if a single unambiguous candidate is found, else None."""
    if total == 0:
        return None

    whitelist_lower = {k.lower() for k in INTERNAL_KEY_WHITELIST}

    # Group by exact key so we can detect multi-value keys (e.g. an enum
    # carrying several distinct values) and reject them.
    by_key: Dict[str, Dict[str, int]] = {}
    for (k, v_json), n in counts.items():
        if k.lower() not in whitelist_lower:
            continue
        by_key.setdefault(k, {})[v_json] = n

    candidates = []
    for key, value_counts in by_key.items():
        truthy_total = 0
        truthy_value = None
        non_truthy_total = 0
        for v_json, n in value_counts.items():
            v = json.loads(v_json)
            if _is_truthy(v):
                truthy_total += n
                # Keep the first canonical truthy value; if there are several
                # (e.g. true and "true"), the key is ambiguous and we drop it.
                if truthy_value is None:
                    truthy_value = v
                elif v != truthy_value:
                    truthy_total = -1
                    break
            else:
                non_truthy_total += n
        if truthy_total <= 0:
            continue
        # If the same key carries multiple non-truthy meanings on top of the
        # truthy one, treat as ambiguous: probably a multi-valued enum (e.g.
        # userType: subscriber/guest/employee), not a simple internal flag.
        if non_truthy_total > 0 and len(value_counts) > 2:
            continue
        share = 100.0 * truthy_total / total
        if not (MIN_SHARE_PCT <= share <= MAX_SHARE_PCT):
            continue
        candidates.append((key, truthy_value, share, truthy_total))

    if len(candidates) != 1:
        return None
    key, value, share, tagged = candidates[0]
    return {
        "extra_data_key": key,
        "extra_data_value": value,
        "detected_share_pct": round(share, 2),
        "tagged_event_count": tagged,
        "sample_event_count": total,
    }


def write_filter_to_bucket_config(bucket_config_path: str, filt: Dict[str, Any]) -> None:
    with open(bucket_config_path, "r", encoding="utf-8") as fh:
        config = json.load(fh)
    config["employee_filter"] = {
        "extra_data_key": filt["extra_data_key"],
        "extra_data_value": filt["extra_data_value"],
        "detected_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "detected_share_pct": filt["detected_share_pct"],
        "sample_event_count": filt["sample_event_count"],
    }
    with open(bucket_config_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")


def clear_filter_from_bucket_config(bucket_config_path: str) -> bool:
    with open(bucket_config_path, "r", encoding="utf-8") as fh:
        config = json.load(fh)
    if "employee_filter" not in config:
        return False
    del config["employee_filter"]
    with open(bucket_config_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")
    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache-dir", default=None,
                   help="Directory containing gzipped DPL event files (recursive). Required unless --clear is set.")
    p.add_argument("--bucket-config", required=True,
                   help="Path to bucket.json (read for current state, written when a filter is detected).")
    p.add_argument("--max-files", type=int, default=200,
                   help="Maximum number of .gz files to scan, most-recent-first. Default 200.")
    p.add_argument("--clear", action="store_true",
                   help="Remove any existing employee_filter from bucket.json and exit without scanning.")
    args = p.parse_args(argv)

    if not os.path.isfile(args.bucket_config):
        print(f"bucket.json not found at {args.bucket_config}. "
              f"Run /agentic-analytics:init first.", file=sys.stderr)
        return 1

    if args.clear:
        cleared = clear_filter_from_bucket_config(args.bucket_config)
        if cleared:
            print("Cleared employee_filter from bucket.json.")
        else:
            print("No employee_filter was set; nothing to clear.")
        return 0

    if not args.cache_dir:
        print("--cache-dir is required unless --clear is set.", file=sys.stderr)
        return 1

    if not os.path.isdir(args.cache_dir):
        print(f"Cache directory not found: {args.cache_dir}. "
              f"Run /agentic-analytics:staircase or /agentic-analytics:refresh-cache "
              f"to populate it first.", file=sys.stderr)
        return 1

    counts, total = collect_extra_data_stats(args.cache_dir, max_files=args.max_files)
    if total == 0:
        print(f"No pageview events found in {args.cache_dir}. "
              f"Run /agentic-analytics:staircase or /agentic-analytics:refresh-cache first.",
              file=sys.stderr)
        return 0

    filt = detect_filter(counts, total)
    if filt is None:
        print(f"No unambiguous employee-traffic tag detected in {total:,} sampled pageviews. "
              f"No filter applied.")
        return 0

    write_filter_to_bucket_config(args.bucket_config, filt)
    key_repr = filt["extra_data_key"]
    val_repr = json.dumps(filt["extra_data_value"])
    print(
        f"Detected employee traffic tagged with extra_data['{key_repr}'] = {val_repr} "
        f"({filt['detected_share_pct']:.1f}% of {total:,} sampled pageviews). "
        f"Will filter this out of audience reports so it doesn't skew your results. "
        f"Tell me if this is incorrect."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
