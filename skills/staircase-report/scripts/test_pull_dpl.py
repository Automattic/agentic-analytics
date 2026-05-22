"""Unit tests for pull_dpl.py CLI surface.

Run from the repo root:
    python3 plugin/skills/staircase-report/scripts/test_pull_dpl.py

Stdlib only on purpose, matching pull_dpl.py.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pull_dpl  # noqa: E402


def _base_args(**overrides):
    """Build a minimal valid argv list, override as needed."""
    args = {
        "--cache-dir": "test-customer",
        "--bucket": "test-bucket",
        "--start": "2026-01-01",
        "--end": "2026-01-01",
    }
    args.update(overrides)
    out = []
    for k, v in args.items():
        out.append(k)
        out.append(v)
    return out


class PrefixDefaultTest(unittest.TestCase):
    def test_prefix_defaults_to_events_slash(self):
        # All Parse.ly customer buckets use 'events/' as the partition prefix,
        # so the script should default there. Legacy buckets without a prefix
        # can pass --prefix "" explicitly.
        args = pull_dpl.make_parser().parse_args(_base_args())
        self.assertEqual(args.prefix, "events/")

    def test_prefix_can_be_overridden_to_empty(self):
        args = pull_dpl.make_parser().parse_args(_base_args() + ["--prefix", ""])
        self.assertEqual(args.prefix, "")

    def test_prefix_can_be_overridden_to_arbitrary(self):
        args = pull_dpl.make_parser().parse_args(_base_args() + ["--prefix", "events-v2/"])
        self.assertEqual(args.prefix, "events-v2/")


class CacheDirTest(unittest.TestCase):
    def test_cache_dir_is_required(self):
        # Should error without --cache-dir.
        argv = ["--bucket", "b", "--start", "2026-01-01", "--end", "2026-01-01"]
        with self.assertRaises(SystemExit):
            pull_dpl.make_parser().parse_args(argv)

    def test_cache_dir_drives_day_dir_path(self):
        # day_dir() should slot the value under <cache_root>/<cache_dir>/YYYY/MM/DD.
        # The cache_dir parameter is just a directory name; the script doesn't
        # interpret it as an apikey.
        d = dt.date(2026, 5, 15)
        path = pull_dpl.day_dir("/tmp/cache", "acme", d)
        self.assertEqual(path, "/tmp/cache/acme/2026/05/15")

    def test_multiple_apikeys_in_one_bucket_share_cache_dir(self):
        # Two apikeys in the same bucket should resolve to the same cache
        # path when called with the same cache_dir, so events aren't
        # downloaded twice. This is the whole point of the cache-dir rename.
        d = dt.date(2026, 5, 15)
        path_a = pull_dpl.day_dir("/tmp/cache", "acme", d)
        path_b = pull_dpl.day_dir("/tmp/cache", "acme", d)
        self.assertEqual(path_a, path_b)


class BucketRequiredTest(unittest.TestCase):
    def test_bucket_is_required(self):
        argv = ["--cache-dir", "c", "--start", "2026-01-01", "--end", "2026-01-01"]
        with self.assertRaises(SystemExit):
            pull_dpl.make_parser().parse_args(argv)


class S3SourceTest(unittest.TestCase):
    def test_default_prefix_produces_events_path(self):
        d = dt.date(2026, 5, 15)
        url = pull_dpl.s3_source("my-bucket", "events/", d)
        self.assertEqual(url, "s3://my-bucket/events/2026/05/15/")

    def test_empty_prefix_produces_bare_path(self):
        d = dt.date(2026, 5, 15)
        url = pull_dpl.s3_source("my-bucket", "", d)
        self.assertEqual(url, "s3://my-bucket/2026/05/15/")


if __name__ == "__main__":
    unittest.main()
