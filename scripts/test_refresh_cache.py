"""Unit tests for refresh_cache.py.

Run from the repo root:
    python3 plugin/scripts/test_refresh_cache.py

Stdlib only on purpose, matching the rest of the project.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import refresh_cache  # noqa: E402


class ReadCacheDirTest(unittest.TestCase):
    def test_reads_cache_dir_from_bucket_json(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "agentic-analytics").mkdir()
            (tdp / "agentic-analytics" / "bucket.json").write_text(
                json.dumps({"bucket": "parsely-dw-acme", "cache_dir": "acme"})
            )
            self.assertEqual(refresh_cache.read_cache_dir(tdp), "acme")

    def test_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(refresh_cache.read_cache_dir(Path(td)))

    def test_returns_none_on_malformed_json(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "agentic-analytics").mkdir()
            (tdp / "agentic-analytics" / "bucket.json").write_text("not-json{{{")
            self.assertIsNone(refresh_cache.read_cache_dir(tdp))

    def test_returns_none_when_cache_dir_field_absent(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "agentic-analytics").mkdir()
            (tdp / "agentic-analytics" / "bucket.json").write_text(
                json.dumps({"bucket": "parsely-dw-acme"})
            )
            self.assertIsNone(refresh_cache.read_cache_dir(tdp))


class ClearDirTest(unittest.TestCase):
    def test_removes_dir_and_reports_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            target = tdp / "victim"
            (target / "a" / "b").mkdir(parents=True)
            (target / "a" / "b" / "f.gz").write_bytes(b"x" * 100)
            (target / "a" / "g.gz").write_bytes(b"y" * 50)
            ok, freed = refresh_cache.clear_dir(target)
            self.assertTrue(ok)
            self.assertEqual(freed, 150)
            self.assertFalse(target.exists())

    def test_returns_false_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            ok, freed = refresh_cache.clear_dir(Path(td) / "nope")
            self.assertFalse(ok)
            self.assertEqual(freed, 0)

    def test_returns_false_when_path_is_a_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "f"
            f.write_text("hi")
            ok, freed = refresh_cache.clear_dir(f)
            self.assertFalse(ok)
            self.assertEqual(freed, 0)
            self.assertTrue(f.exists())


class FmtSizeTest(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(refresh_cache._fmt_size(0), "0 B")
        self.assertEqual(refresh_cache._fmt_size(1), "1 B")
        self.assertEqual(refresh_cache._fmt_size(1023), "1023 B")

    def test_kilobytes(self):
        self.assertEqual(refresh_cache._fmt_size(1024), "1.0 KB")
        self.assertEqual(refresh_cache._fmt_size(1536), "1.5 KB")

    def test_megabytes(self):
        self.assertEqual(refresh_cache._fmt_size(1024 * 1024), "1.0 MB")
        self.assertEqual(refresh_cache._fmt_size(int(2.5 * 1024 * 1024)), "2.5 MB")

    def test_gigabytes(self):
        self.assertEqual(refresh_cache._fmt_size(1024 ** 3), "1.0 GB")
        self.assertEqual(refresh_cache._fmt_size(int(3.7 * 1024 ** 3)), "3.7 GB")

    def test_handles_values_larger_than_a_terabyte(self):
        # Bytes freed could plausibly exceed 1024 GB for big customers; the
        # formatter should still produce something reasonable rather than
        # raise.
        self.assertEqual(refresh_cache._fmt_size(2048 * 1024 ** 3), "2048.0 GB")


class MainTest(unittest.TestCase):
    def _fake_homes(self, td: Path) -> dict:
        return {
            "XDG_CONFIG_HOME": str(td / "config"),
            "XDG_CACHE_HOME": str(td / "cache"),
        }

    def test_exits_nonzero_when_no_bucket_json(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            env = self._fake_homes(tdp)
            with mock.patch.dict(os.environ, env, clear=False):
                buf_out, buf_err = io.StringIO(), io.StringIO()
                with redirect_stdout(buf_out), redirect_stderr(buf_err):
                    rc = refresh_cache.main()
                self.assertEqual(rc, 1)
                self.assertIn("init", buf_err.getvalue())

    def test_clears_dpl_and_duckdb_for_configured_customer(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            env = self._fake_homes(tdp)

            cfg_dir = tdp / "config" / "agentic-analytics"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "bucket.json").write_text(
                json.dumps({"bucket": "parsely-dw-acme", "cache_dir": "acme"})
            )

            dpl = tdp / "cache" / "agentic-analytics" / "dpl" / "acme" / "2026" / "05" / "14"
            dpl.mkdir(parents=True)
            (dpl / "evt.gz").write_bytes(b"z" * 1024)

            other = tdp / "cache" / "agentic-analytics" / "dpl" / "other-customer"
            other.mkdir(parents=True)
            (other / "evt.gz").write_bytes(b"q" * 10)

            duckdb = tdp / "cache" / "agentic-analytics" / "duckdb-tmp"
            duckdb.mkdir(parents=True)
            (duckdb / "scratch.db").write_bytes(b"w" * 2048)

            with mock.patch.dict(os.environ, env, clear=False):
                buf_out = io.StringIO()
                with redirect_stdout(buf_out):
                    rc = refresh_cache.main()
                self.assertEqual(rc, 0)

            self.assertFalse((tdp / "cache" / "agentic-analytics" / "dpl" / "acme").exists())
            self.assertFalse(duckdb.exists())
            # Other customers are untouched.
            self.assertTrue(other.exists())
            self.assertTrue((other / "evt.gz").exists())

            output = buf_out.getvalue()
            # Reports the customer scope.
            self.assertIn("Cleared cache for 'acme'", output)
            # Reports total bytes freed (1024 dpl + 2048 duckdb = 3072 B = 3.0 KB).
            self.assertIn("Freed: 3.0 KB", output)
            # Lists both paths cleared.
            self.assertIn("dpl/acme", output)
            self.assertIn("duckdb-tmp", output)

    def test_runs_clean_when_caches_already_gone(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            env = self._fake_homes(tdp)
            cfg_dir = tdp / "config" / "agentic-analytics"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "bucket.json").write_text(
                json.dumps({"cache_dir": "acme"})
            )
            with mock.patch.dict(os.environ, env, clear=False):
                buf_out = io.StringIO()
                with redirect_stdout(buf_out):
                    rc = refresh_cache.main()
                self.assertEqual(rc, 0)
                self.assertIn("nothing to clear", buf_out.getvalue())


if __name__ == "__main__":
    unittest.main()
