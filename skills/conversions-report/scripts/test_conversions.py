"""Unit tests for the lifted conversion code in conversions.py.

Run from the repo root:
    python3 plugin/skills/conversions-report/scripts/test_conversions.py

Mirrors the conversion-shaped tests from test_staircase.py, but targets
the new `conversions` module so we can verify the lift kept behavior
intact. PR C will delete the original copies in test_staircase.py.

Stdlib only on purpose, matching the source.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import conversions  # noqa: E402


def _make_meta(
    *,
    current_tier="one_time",
    prior_tier="silent",
    channel="Direct",
    last_cur_page="https://example.com/insights/post/",
    last_cur_section="Insights",
    last_cur_ts=dt.datetime(2026, 5, 1, 12, 0),
    last_prior_page="https://example.com/about/",
    last_prior_section="About",
    last_prior_ts=dt.datetime(2026, 4, 1, 12, 0),
    lifetime_pageviews=5,
):
    """Build a `visitor_meta` entry matching what `compute_staircase` emits.

    Mirrors the internal shape so cohort-builder tests can stay tiny and
    not have to construct full event streams."""
    def _ev(page, section):
        return {
            "metadata_canonical_url": page,
            "metadata_section": section,
        }
    return {
        "current_tier": current_tier,
        "prior_tier": prior_tier,
        "channel": channel,
        "last_cur_ev": _ev(last_cur_page, last_cur_section) if last_cur_ts else None,
        "last_cur_ts": last_cur_ts,
        "last_prior_ev": _ev(last_prior_page, last_prior_section) if last_prior_ts else None,
        "last_prior_ts": last_prior_ts,
        "lifetime_pageviews": lifetime_pageviews,
    }


class ComputeConversionPathsTest(unittest.TestCase):
    """Aggregation rules for ConversionPaths."""

    def _pv(self, **overrides):
        # Minimal first_cur_ev for attribution. acquisition_channel reads
        # sref_category, sref_domain, utm_*; _page_for reads canonical_url;
        # _section_for reads metadata_section.
        ev = {
            "metadata_canonical_url": "https://example.com/careers/",
            "metadata_section": "Careers",
        }
        ev.update(overrides)
        return ev

    def test_per_channel_aggregation_and_share(self):
        # 3 search, 1 direct, 1 missing first pageview.
        by_visitor = {
            "v1": {"first_cur_ev": self._pv(sref_category="search")},
            "v2": {"first_cur_ev": self._pv(sref_category="search")},
            "v3": {"first_cur_ev": self._pv(sref_category="search")},
            "v4": {"first_cur_ev": self._pv()},
        }
        events = [
            ("v1", "lead_capture", "Form"),
            ("v2", "lead_capture", "Form"),
            ("v3", "lead_capture", "Form"),
            ("v4", "lead_capture", "Form"),
            ("v5", "lead_capture", "Form"),
        ]
        paths = conversions.compute_conversion_paths(events, by_visitor)
        self.assertEqual(len(paths), 1)
        p = paths[0]
        self.assertEqual(p.total_current, 5)
        self.assertEqual(p.unmatched, 1)
        # Sorted desc; Search Referrers wins.
        self.assertEqual(p.by_channel[0], ("Search Referrers", 3))
        self.assertEqual(p.by_channel[1], ("Direct", 1))

    def test_landing_page_floor_and_top_n(self):
        # 6 conversions landing on /a, 4 on /b. Floor = 5, top_n = 1.
        by_visitor = {}
        events = []
        for i in range(6):
            vsid = f"a{i}"
            by_visitor[vsid] = {"first_cur_ev": self._pv(
                metadata_canonical_url="https://example.com/a/", metadata_section="A")}
            events.append((vsid, "lead_capture", "Form"))
        for i in range(4):
            vsid = f"b{i}"
            by_visitor[vsid] = {"first_cur_ev": self._pv(
                metadata_canonical_url="https://example.com/b/", metadata_section="B")}
            events.append((vsid, "lead_capture", "Form"))
        paths = conversions.compute_conversion_paths(
            events, by_visitor, landing_floor=5, top_n=10
        )
        self.assertEqual(len(paths[0].by_landing), 1)
        # Floor drops /b (count=4); /a (count=6) is the only row.
        self.assertEqual(paths[0].by_landing[0][1], 6)
        self.assertIn("/a", paths[0].by_landing[0][0])

    def test_section_url_fallback_in_attribution(self):
        # Section is Uncategorized; the URL-fallback flag should appear in
        # the by_section tuple.
        ev = self._pv(
            metadata_section="Uncategorized",
            metadata_canonical_url="https://example.com/about/conferences-events/",
        )
        by_visitor = {f"v{i}": {"first_cur_ev": ev} for i in range(5)}
        events = [(f"v{i}", "lead_capture", "Form") for i in range(5)]
        paths = conversions.compute_conversion_paths(
            events, by_visitor, section_floor=5
        )
        self.assertEqual(paths[0].by_section, [("/about", 5, True)])

    def test_unmatched_count(self):
        # Two visitors with first_cur_ev, two with bucket-but-no-first_cur_ev,
        # and one vsid not in by_visitor at all. All three of those land in
        # unmatched.
        by_visitor = {
            "v1": {"first_cur_ev": self._pv()},
            "v2": {"first_cur_ev": self._pv()},
            "v3": {"first_cur_ev": None},
            "v4": {"first_cur_ev": None},
        }
        events = [(f"v{i}", "lead_capture", "Form") for i in range(1, 6)]
        paths = conversions.compute_conversion_paths(events, by_visitor)
        self.assertEqual(paths[0].total_current, 5)
        self.assertEqual(paths[0].unmatched, 3)

    def test_bot_conversions_counted_as_unmatched(self):
        # Bot conversions count toward total (so Y matches the rest of the
        # report) but don't get attributed.
        by_visitor = {
            "real": {"first_cur_ev": self._pv(sref_category="search")},
            "bot1": {"first_cur_ev": self._pv(sref_category="search")},
        }
        events = [
            ("real", "lead_capture", "Form"),
            ("bot1", "lead_capture", "Form"),
        ]
        paths = conversions.compute_conversion_paths(
            events, by_visitor, bot_visitors={"bot1"}
        )
        self.assertEqual(paths[0].total_current, 2)
        self.assertEqual(paths[0].unmatched, 1)
        # Only the real visitor's search referral made it into by_channel.
        self.assertEqual(paths[0].by_channel, [("Search Referrers", 1)])

    def test_type_filter_restricts_groups(self):
        by_visitor = {"v1": {"first_cur_ev": self._pv()}}
        events = [
            ("v1", "lead_capture", "A"),
            ("v1", "link_click", "B"),
        ]
        paths = conversions.compute_conversion_paths(
            events, by_visitor, type_filter={"lead_capture"}
        )
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].conversion_type, "lead_capture")


class StickyUnconvertedCohortTest(unittest.TestCase):
    def test_only_strong_channel_non_converters_in_period(self):
        meta = {
            "search_sticky": _make_meta(
                current_tier="returning",
                channel="Search Referrers",
            ),
            "search_converted": _make_meta(
                current_tier="returning",
                channel="Search Referrers",
            ),
            "direct_visitor": _make_meta(
                current_tier="returning",
                channel="Direct",
            ),
            "search_silent": _make_meta(
                current_tier="silent",
                channel="Search Referrers",
            ),
        }
        rows = conversions._build_cohort_sticky_unconverted(
            meta,
            strong_channels=("Search Referrers",),
            converters_current=("search_converted",),
        )
        self.assertEqual(
            [r.visitor_site_id for r in rows], ["search_sticky"]
        )
        self.assertEqual(rows[0].arrival_channel, "Search Referrers")

    def test_empty_strong_channels_yields_empty_cohort(self):
        meta = {
            "v1": _make_meta(
                current_tier="returning",
                channel="Search Referrers",
            ),
        }
        rows = conversions._build_cohort_sticky_unconverted(
            meta, strong_channels=(), converters_current=(),
        )
        self.assertEqual(rows, [])


class ConversionValueTierTest(unittest.TestCase):
    def test_lead_capture_is_high(self):
        c = conversions.ConversionGroup(
            conversion_type="lead_capture", conversion_label="Form",
            current_count=1, prior_count=0, top_pages=[],
        )
        self.assertEqual(conversions._conversion_value_tier(c), "high")

    def test_newsletter_signup_is_high(self):
        c = conversions.ConversionGroup(
            conversion_type="newsletter_signup", conversion_label="Newsletter",
            current_count=1, prior_count=0, top_pages=[],
        )
        self.assertEqual(conversions._conversion_value_tier(c), "high")

    def test_link_click_is_low(self):
        c = conversions.ConversionGroup(
            conversion_type="link_click", conversion_label="Click",
            current_count=1, prior_count=0, top_pages=[],
        )
        self.assertEqual(conversions._conversion_value_tier(c), "low")

    def test_unknown_type_is_medium(self):
        c = conversions.ConversionGroup(
            conversion_type="something_else", conversion_label="Other",
            current_count=1, prior_count=0, top_pages=[],
        )
        self.assertEqual(conversions._conversion_value_tier(c), "medium")

    def test_none_type_is_medium(self):
        c = conversions.ConversionGroup(
            conversion_type=None, conversion_label=None,
            current_count=1, prior_count=0, top_pages=[],
        )
        self.assertEqual(conversions._conversion_value_tier(c), "medium")


class HighValueConversionCalloutsTest(unittest.TestCase):
    def test_significant_delta_surfaces_callout(self):
        c = conversions.ConversionGroup(
            conversion_type="lead_capture", conversion_label="Pardot Form",
            current_count=1383, prior_count=1260,
            top_pages=[("/about/conferences-events/", 308)],
        )
        out = conversions._high_value_conversion_callouts([c])
        self.assertEqual(len(out), 1)
        self.assertIn("Pardot Form", out[0])
        self.assertIn("1,383", out[0])
        self.assertIn("/about/conferences-events/", out[0])

    def test_low_value_conversion_no_callout(self):
        c = conversions.ConversionGroup(
            conversion_type="link_click", conversion_label="Footer",
            current_count=5000, prior_count=1000, top_pages=[],
        )
        out = conversions._high_value_conversion_callouts([c])
        self.assertEqual(out, [])

    def test_high_value_held_steady_callout_when_no_movement(self):
        c = conversions.ConversionGroup(
            conversion_type="newsletter_signup", conversion_label="Newsletter",
            current_count=100, prior_count=99, top_pages=[],
        )
        out = conversions._high_value_conversion_callouts([c])
        self.assertEqual(len(out), 1)
        self.assertIn("held steady", out[0])

    def test_no_high_value_no_callouts(self):
        c = conversions.ConversionGroup(
            conversion_type="link_click", conversion_label="Footer",
            current_count=50, prior_count=10, top_pages=[],
        )
        out = conversions._high_value_conversion_callouts([c])
        self.assertEqual(out, [])


class ConversionRollupTest(unittest.TestCase):
    def test_groups_by_value_tier(self):
        groups = [
            conversions.ConversionGroup(
                conversion_type="lead_capture", conversion_label="A",
                current_count=10, prior_count=5, top_pages=[],
            ),
            conversions.ConversionGroup(
                conversion_type="newsletter_signup", conversion_label="B",
                current_count=20, prior_count=15, top_pages=[],
            ),
            conversions.ConversionGroup(
                conversion_type="link_click", conversion_label="C",
                current_count=100, prior_count=80, top_pages=[],
            ),
            conversions.ConversionGroup(
                conversion_type="purchase", conversion_label="D",
                current_count=3, prior_count=2, top_pages=[],
            ),
        ]
        rollup = conversions._conversion_rollup(groups)
        self.assertEqual(rollup["high"]["count"], 2)
        self.assertEqual(rollup["high"]["current"], 30)
        self.assertEqual(rollup["high"]["prior"], 20)
        self.assertEqual(rollup["medium"]["count"], 1)
        self.assertEqual(rollup["medium"]["current"], 3)
        self.assertEqual(rollup["low"]["count"], 1)
        self.assertEqual(rollup["low"]["current"], 100)


if __name__ == "__main__":
    unittest.main()
