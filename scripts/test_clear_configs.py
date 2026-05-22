"""Unit tests for clear_configs.py.

Run from the repo root:
    python3 plugin/scripts/test_clear_configs.py

Stdlib only on purpose, matching the rest of the project.
"""
from __future__ import annotations

import configparser
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clear_configs  # noqa: E402


class ClearBucketJsonTest(unittest.TestCase):
    def test_removes_existing_file(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "agentic-analytics").mkdir()
            target = tdp / "agentic-analytics" / "bucket.json"
            target.write_text('{"bucket": "parsely-dw-acme"}')
            self.assertTrue(clear_configs.clear_bucket_json(tdp))
            self.assertFalse(target.exists())

    def test_returns_false_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(clear_configs.clear_bucket_json(Path(td)))


class ClearAwsProfileTest(unittest.TestCase):
    def _write_cfg(self, path: Path, sections: dict) -> None:
        cfg = configparser.ConfigParser()
        for section, kv in sections.items():
            cfg[section] = kv
        with open(path, "w") as h:
            cfg.write(h)

    def test_removes_section_from_credentials(self):
        # ~/.aws/credentials uses bare section names ([agentic-analytics]).
        with tempfile.TemporaryDirectory() as td:
            cred = Path(td) / "credentials"
            conf = Path(td) / "config"
            self._write_cfg(cred, {
                "agentic-analytics": {"aws_access_key_id": "x", "aws_secret_access_key": "y"},
                "other": {"aws_access_key_id": "a"},
            })
            removed = clear_configs.clear_aws_profile(cred, conf)
            self.assertEqual(len(removed), 1)
            self.assertIn("[agentic-analytics]", removed[0])

            cfg = configparser.ConfigParser()
            cfg.read(cred)
            self.assertNotIn("agentic-analytics", cfg)
            # Other profiles preserved.
            self.assertIn("other", cfg)

    def test_removes_section_from_config(self):
        # ~/.aws/config uses [profile <name>].
        with tempfile.TemporaryDirectory() as td:
            cred = Path(td) / "credentials"
            conf = Path(td) / "config"
            self._write_cfg(conf, {
                "profile agentic-analytics": {"region": "us-east-1", "output": "json"},
                "profile other": {"region": "us-west-2"},
            })
            removed = clear_configs.clear_aws_profile(cred, conf)
            self.assertEqual(len(removed), 1)
            self.assertIn("[profile agentic-analytics]", removed[0])

            cfg = configparser.ConfigParser()
            cfg.read(conf)
            self.assertNotIn("profile agentic-analytics", cfg)
            # Other profiles preserved.
            self.assertIn("profile other", cfg)

    def test_removes_from_both_files(self):
        with tempfile.TemporaryDirectory() as td:
            cred = Path(td) / "credentials"
            conf = Path(td) / "config"
            self._write_cfg(cred, {"agentic-analytics": {"aws_access_key_id": "x"}})
            self._write_cfg(conf, {"profile agentic-analytics": {"region": "us-east-1"}})
            removed = clear_configs.clear_aws_profile(cred, conf)
            self.assertEqual(len(removed), 2)

    def test_handles_missing_files(self):
        with tempfile.TemporaryDirectory() as td:
            cred = Path(td) / "credentials"
            conf = Path(td) / "config"
            # Neither file exists.
            removed = clear_configs.clear_aws_profile(cred, conf)
            self.assertEqual(removed, [])

    def test_no_op_when_section_absent(self):
        with tempfile.TemporaryDirectory() as td:
            cred = Path(td) / "credentials"
            self._write_cfg(cred, {"other": {"aws_access_key_id": "x"}})
            removed = clear_configs.clear_aws_profile(cred, Path(td) / "config")
            self.assertEqual(removed, [])
            # Other profiles still intact.
            cfg = configparser.ConfigParser()
            cfg.read(cred)
            self.assertIn("other", cfg)


class ConfigHomeResolutionTest(unittest.TestCase):
    def test_honors_xdg_config_home(self):
        # When XDG_CONFIG_HOME is set, _config_home() should return it; when
        # unset, fall back to ~/.config.
        original = os.environ.pop("XDG_CONFIG_HOME", None)
        try:
            os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg-test"
            self.assertEqual(clear_configs._config_home(), Path("/tmp/xdg-test"))
            del os.environ["XDG_CONFIG_HOME"]
            self.assertEqual(clear_configs._config_home(), Path(os.path.expanduser("~/.config")))
        finally:
            if original is not None:
                os.environ["XDG_CONFIG_HOME"] = original


if __name__ == "__main__":
    unittest.main()
