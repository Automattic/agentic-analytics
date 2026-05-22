"""Unit tests for the bug-prone pure functions in staircase.py.

Run from the repo root:
    python3 plugin/skills/staircase-report/scripts/test_staircase.py

Stdlib only on purpose, matching staircase.py.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import staircase  # noqa: E402


class TierForTest(unittest.TestCase):
    def test_zero_visits_is_silent(self):
        self.assertEqual(staircase.tier_for(0), staircase.SILENT)

    def test_one_visit_is_one_time(self):
        self.assertEqual(staircase.tier_for(1), staircase.ONE_TIME)

    def test_two_to_four_is_returning(self):
        for n in (2, 3, 4):
            self.assertEqual(staircase.tier_for(n), staircase.RETURNING, f"n={n}")

    def test_five_or_more_is_brand_lover(self):
        for n in (5, 6, 100):
            self.assertEqual(staircase.tier_for(n), staircase.BRAND_LOVER, f"n={n}")


class SessionCountTest(unittest.TestCase):
    def _ts(self, *minutes):
        base = dt.datetime(2026, 4, 17, 10, 0, 0)
        return [base + dt.timedelta(minutes=m) for m in minutes]

    def test_empty_is_zero(self):
        self.assertEqual(staircase.session_count([], staircase.SESSION_GAP), 0)

    def test_single_event_is_one_session(self):
        self.assertEqual(staircase.session_count(self._ts(0), staircase.SESSION_GAP), 1)

    def test_two_events_within_gap_collapse(self):
        # 25 min apart, gap is 30 min -> one session
        self.assertEqual(staircase.session_count(self._ts(0, 25), staircase.SESSION_GAP), 1)

    def test_two_events_at_exactly_gap_split(self):
        # The implementation uses >= gap as the split point; two events
        # exactly 30 min apart are two sessions. Lock that in.
        self.assertEqual(staircase.session_count(self._ts(0, 30), staircase.SESSION_GAP), 2)

    def test_two_events_past_gap_split(self):
        self.assertEqual(staircase.session_count(self._ts(0, 31), staircase.SESSION_GAP), 2)

    def test_mixed_close_and_far(self):
        # 0, 10, 50, 60 -> sessions: {0,10}, {50,60} (gap 30)
        self.assertEqual(staircase.session_count(self._ts(0, 10, 50, 60), staircase.SESSION_GAP), 2)

    def test_custom_gap(self):
        # 0, 5 with a 4-min gap -> two sessions
        self.assertEqual(staircase.session_count(self._ts(0, 5), dt.timedelta(minutes=4)), 2)


class AcquisitionChannelTest(unittest.TestCase):
    """Lock in the GA-style channel grouping rules.

    The rules are a priority chain (Email > Paid > customer-configured
    Network > Social > Search > Internal links > AI > Referral > Direct),
    so most of these tests assert that the *right* rule wins when multiple
    signals are present.
    """

    def test_email_via_utm_medium(self):
        self.assertEqual(
            staircase.acquisition_channel({"utm_medium": "email"}),
            "Email",
        )

    def test_email_via_known_utm_source_no_referrer(self):
        # The hs_email trap: utm_medium is empty, sref_category is "direct"
        # because email clicks have no HTTP referrer. Must still bucket as
        # Email, not Direct.
        ev = {"utm_medium": "", "utm_source": "hs_email", "sref_category": "direct"}
        self.assertEqual(staircase.acquisition_channel(ev), "Email")

    def test_email_utm_source_is_case_insensitive(self):
        self.assertEqual(
            staircase.acquisition_channel({"utm_source": "HS_Email"}),
            "Email",
        )

    def test_paid_medium(self):
        for medium in ("ppc", "paid", "cpc", "paid_search"):
            self.assertEqual(
                staircase.acquisition_channel({"utm_medium": medium}),
                "Paid",
                f"medium={medium}",
            )

    def test_network_rule_disabled_by_default(self):
        # With no network_label configured, the rule is inert. utm values
        # that would otherwise match a network shouldn't claim a channel.
        ev = {"utm_medium": "my_internal_referral"}
        self.assertEqual(staircase.acquisition_channel(ev), "Direct")

    def test_network_rule_matches_utm_medium(self):
        ev = {"utm_medium": "my_internal_referral"}
        self.assertEqual(
            staircase.acquisition_channel(
                ev,
                network_label="My Network",
                network_sources=("my_internal_referral",),
            ),
            "My Network",
        )

    def test_network_rule_matches_utm_source(self):
        ev = {"utm_source": "sister_site"}
        self.assertEqual(
            staircase.acquisition_channel(
                ev,
                network_label="My Network",
                network_sources=("sister_site",),
            ),
            "My Network",
        )

    def test_social_via_sref_category(self):
        self.assertEqual(
            staircase.acquisition_channel({"sref_category": "social"}),
            "Social Referrers",
        )

    def test_search_via_sref_category(self):
        self.assertEqual(
            staircase.acquisition_channel({"sref_category": "search"}),
            "Search Referrers",
        )

    def test_internal_same_domain_returns_none(self):
        # Same-domain "internal" referrer = session continuation. Not real
        # acquisition; return None so the visitor is excluded from the
        # channel mix entirely.
        ev = {
            "sref_category": "internal",
            "sref_domain": "example.com",
            "url_domain": "example.com",
        }
        self.assertIsNone(staircase.acquisition_channel(ev))

    def test_internal_cross_subdomain_returns_none(self):
        # Cross-subdomain "internal" referrer (ir.example.com → example.com)
        # tells us the visitor was on a sister subdomain, but we don't know
        # what brought them THERE. Drop from channel mix rather than claim a
        # source we don't have.
        ev = {
            "sref_category": "internal",
            "sref_domain": "ir.example.com",
            "url_domain": "example.com",
        }
        self.assertIsNone(staircase.acquisition_channel(ev))

    def test_internal_no_sref_domain_returns_none(self):
        # sref_category=internal with no sref_domain: still not real acquisition.
        ev = {"sref_category": "internal"}
        self.assertIsNone(staircase.acquisition_channel(ev))

    def test_ai_referrers(self):
        self.assertEqual(
            staircase.acquisition_channel({"sref_category": "ai"}),
            "AI Referrers",
        )

    def test_referral_with_domain(self):
        ev = {"sref_domain": "example.com"}
        self.assertEqual(staircase.acquisition_channel(ev), "Referral: example.com")

    def test_direct_when_nothing_set(self):
        self.assertEqual(staircase.acquisition_channel({}), "Direct")

    def test_email_beats_paid(self):
        # utm_medium=email and a paid utm_source shouldn't matter; Email wins.
        ev = {"utm_medium": "email", "utm_source": "google"}
        self.assertEqual(staircase.acquisition_channel(ev), "Email")

    def test_paid_beats_social_when_medium_is_paid(self):
        ev = {"utm_medium": "cpc", "sref_category": "social"}
        self.assertEqual(staircase.acquisition_channel(ev), "Paid")

    def test_email_beats_network(self):
        # Email is higher priority than the customer's network rule. An
        # event that matches both an email utm_medium AND a configured
        # network source still buckets as Email.
        ev = {"utm_medium": "email", "utm_source": "my_sister_site"}
        self.assertEqual(
            staircase.acquisition_channel(
                ev,
                network_label="My Network",
                network_sources=("my_sister_site",),
            ),
            "Email",
        )

    def test_network_beats_social(self):
        # The network rule sits between Paid and Social in the priority
        # chain. An event matching both a configured network source and
        # sref_category=social should bucket as the network channel, not
        # as Social Referrers.
        ev = {"utm_source": "my_sister_site", "sref_category": "social"}
        self.assertEqual(
            staircase.acquisition_channel(
                ev,
                network_label="My Network",
                network_sources=("my_sister_site",),
            ),
            "My Network",
        )

    def test_referral_only_used_when_no_category(self):
        # A search referrer with sref_category=search should bucket as Search
        # Referrers, not "Referral: google.com".
        ev = {"sref_category": "search", "sref_domain": "google.com"}
        self.assertEqual(staircase.acquisition_channel(ev), "Search Referrers")


class UrlFirstSegmentTest(unittest.TestCase):
    def test_extracts_first_segment(self):
        self.assertEqual(
            staircase._url_first_segment("https://example.com/insights/foo"),
            "/insights",
        )

    def test_strips_query_and_fragment(self):
        self.assertEqual(
            staircase._url_first_segment("https://example.com/events?x=1#top"),
            "/events",
        )

    def test_no_scheme(self):
        self.assertEqual(
            staircase._url_first_segment("example.com/about/team"),
            "/about",
        )

    def test_root_returns_none(self):
        self.assertIsNone(staircase._url_first_segment("https://example.com/"))

    def test_no_path_returns_none(self):
        self.assertIsNone(staircase._url_first_segment("https://example.com"))

    def test_empty_returns_none(self):
        self.assertIsNone(staircase._url_first_segment(""))
        self.assertIsNone(staircase._url_first_segment(None))


class SectionForTest(unittest.TestCase):
    def test_populated_section_used_as_is(self):
        ev = {
            "metadata_section": "Events",
            "metadata_canonical_url": "https://example.com/insights/foo",
        }
        self.assertEqual(staircase._section_for(ev), ("Events", False))

    def test_uncategorized_falls_back_to_url(self):
        ev = {
            "metadata_section": "Uncategorized",
            "metadata_canonical_url": "https://example.com/insights/foo",
        }
        self.assertEqual(staircase._section_for(ev), ("/insights", True))

    def test_uncategorized_case_insensitive(self):
        ev = {
            "metadata_section": "uncategorized",
            "metadata_canonical_url": "https://example.com/events/x",
        }
        self.assertEqual(staircase._section_for(ev), ("/events", True))

    def test_empty_section_falls_back_to_url(self):
        ev = {
            "metadata_section": "",
            "metadata_canonical_url": "https://example.com/about/team",
        }
        self.assertEqual(staircase._section_for(ev), ("/about", True))

    def test_uncategorized_with_no_url_path_stays_uncategorized(self):
        ev = {
            "metadata_section": "Uncategorized",
            "metadata_canonical_url": "https://example.com",
        }
        self.assertEqual(staircase._section_for(ev), ("Uncategorized", False))


class AuthorsForTest(unittest.TestCase):
    def test_list_with_entries(self):
        self.assertEqual(
            staircase._authors_for({"metadata_authors": ["dianne", "gmanrique"]}),
            ["dianne", "gmanrique"],
        )

    def test_string_single_author(self):
        self.assertEqual(
            staircase._authors_for({"metadata_authors": "Staff"}),
            ["Staff"],
        )

    def test_empty_list(self):
        self.assertEqual(staircase._authors_for({"metadata_authors": []}), [])

    def test_missing(self):
        self.assertEqual(staircase._authors_for({}), [])

    def test_skips_blank_entries(self):
        self.assertEqual(
            staircase._authors_for({"metadata_authors": ["Nora", "", None, "  "]}),
            ["Nora"],
        )


class StrongChannelBreakdownTest(unittest.TestCase):
    """Cover the volume floor, the 1.5x climb-rate ratio, the section URL
    fallback, multi-author handling, and the per-row floors. The site climb
    rate is fixed at 10% across these scenarios so the math is easy to
    reason about: rate of 15% gives ratio 1.5x; 10% gives 1x. The third
    tuple slot is the prior-active visitor's `climbed` flag (True iff
    current_tier rank > prior_tier rank)."""

    def _ev(self, *, url="https://example.com/insights/foo", section="Events",
            authors=None):
        return {
            "metadata_canonical_url": url,
            "metadata_section": section,
            "metadata_authors": authors if authors is not None else ["dianne"],
        }

    def test_channel_below_volume_floor_excluded(self):
        # 19 sticky-15%-equivalent visitors: too few to surface even though
        # the ratio is high.
        visitors = [("Search Referrers", self._ev(), True)] * 3 + [
            ("Search Referrers", self._ev(), False)
        ] * 16
        out = staircase.compute_strong_channel_breakdowns(
            visitors,
            site_climb_rate=0.10,
            volume_floor=20,
        )
        self.assertEqual(out, [])

    def test_channel_below_ratio_threshold_excluded(self):
        # 30 visitors, 3 sticky -> 10% stickiness, ratio 1.0x. Should not
        # surface.
        visitors = [("Direct", self._ev(), True)] * 3 + [
            ("Direct", self._ev(), False)
        ] * 27
        out = staircase.compute_strong_channel_breakdowns(
            visitors, site_climb_rate=0.10,
        )
        self.assertEqual(out, [])

    def test_channel_above_thresholds_surfaces(self):
        # 30 visitors, 6 sticky -> 20% stickiness, ratio 2.0x. Surfaces.
        # All 6 sticky visitors land on /insights/foo (one page); the other
        # 24 split across other pages with too few visitors per row to clear
        # the page floor.
        visitors = [
            ("Search Referrers", self._ev(url="https://example.com/insights/foo"), True)
        ] * 6 + [
            ("Search Referrers", self._ev(url=f"https://example.com/p{i}"), False)
            for i in range(24)
        ]
        out = staircase.compute_strong_channel_breakdowns(
            visitors, site_climb_rate=0.10,
        )
        self.assertEqual(len(out), 1)
        b = out[0]
        self.assertEqual(b.channel, "Search Referrers")
        self.assertEqual(b.visitors, 30)
        self.assertEqual(b.climbers, 6)
        self.assertAlmostEqual(b.ratio, 2.0, places=2)

    def test_section_fallback_to_url_segment(self):
        # 30 visitors all under /insights/* with metadata_section=Uncategorized.
        # The section table should show a single "(by URL: /insights)" row.
        visitors = []
        for i in range(10):
            visitors.append((
                "Search Referrers",
                self._ev(
                    url=f"https://example.com/insights/page{i}",
                    section="Uncategorized",
                ),
                True,
            ))
        for i in range(20):
            visitors.append((
                "Search Referrers",
                self._ev(
                    url=f"https://example.com/insights/page{i+10}",
                    section="Uncategorized",
                ),
                False,
            ))
        out = staircase.compute_strong_channel_breakdowns(
            visitors,
            site_climb_rate=0.10,
            section_floor=20,
            landing_page_floor=100,
            author_floor=100,
        )
        self.assertEqual(len(out), 1)
        sections = out[0].top_sections
        self.assertEqual(len(sections), 1)
        label, count, _rate, is_fallback = sections[0]
        self.assertEqual(label, "/insights")
        self.assertEqual(count, 30)
        self.assertTrue(is_fallback)

    def test_section_real_name_not_marked_fallback(self):
        visitors = []
        for i in range(10):
            visitors.append((
                "Search Referrers",
                self._ev(section="Events"),
                True,
            ))
        for i in range(20):
            visitors.append((
                "Search Referrers",
                self._ev(section="Events"),
                False,
            ))
        out = staircase.compute_strong_channel_breakdowns(
            visitors,
            site_climb_rate=0.10,
            section_floor=20,
            landing_page_floor=100,
            author_floor=100,
        )
        self.assertEqual(len(out), 1)
        sections = out[0].top_sections
        self.assertEqual(len(sections), 1)
        label, _count, _rate, is_fallback = sections[0]
        self.assertEqual(label, "Events")
        self.assertFalse(is_fallback)

    def test_multi_author_event_counts_each_author(self):
        # Each event lists two authors; both should count toward both
        # authors' visitor counts.
        ev = self._ev(authors=["dianne", "gmanrique"])
        visitors = [("Search Referrers", ev, True)] * 10 + [
            ("Search Referrers", ev, False)
        ] * 20
        out = staircase.compute_strong_channel_breakdowns(
            visitors,
            site_climb_rate=0.10,
            author_floor=10,
            section_floor=100,
            landing_page_floor=100,
        )
        self.assertEqual(len(out), 1)
        authors = {a: v for a, v, _r in out[0].top_authors}
        self.assertEqual(authors, {"dianne": 30, "gmanrique": 30})

    def test_landing_page_floor_enforced(self):
        # 30 channel-sticky visitors split across pages with most pages
        # below the page floor; only pages with >=10 visitors surface.
        visitors = []
        # /a: 10 visitors, 6 sticky -> clears floor.
        for i in range(6):
            visitors.append(("Search Referrers", self._ev(url="https://example.com/a"), True))
        for i in range(4):
            visitors.append(("Search Referrers", self._ev(url="https://example.com/a"), False))
        # /b: 9 visitors, 1 sticky -> below floor, excluded.
        for i in range(1):
            visitors.append(("Search Referrers", self._ev(url="https://example.com/b"), True))
        for i in range(8):
            visitors.append(("Search Referrers", self._ev(url="https://example.com/b"), False))
        # Pad to clear channel volume floor of 20 with /c (also below page floor).
        for i in range(9):
            visitors.append(("Search Referrers", self._ev(url="https://example.com/c"), False))
        # /d: 2 more to clear the channel floor; below the page floor too.
        for i in range(2):
            visitors.append(("Search Referrers", self._ev(url="https://example.com/d"), False))
        out = staircase.compute_strong_channel_breakdowns(
            visitors,
            site_climb_rate=0.10,
            landing_page_floor=10,
            section_floor=100,
            author_floor=100,
        )
        self.assertEqual(len(out), 1)
        pages = {_short_page(p): v for p, v, _r in out[0].top_pages}
        # Only /a clears the page floor.
        self.assertIn("/a", pages)
        self.assertEqual(pages["/a"], 10)
        self.assertNotIn("/b", pages)
        self.assertNotIn("/c", pages)
        self.assertNotIn("/d", pages)


def _short_page(url):
    return staircase._short_page(url)


class EventSlugForUrlTest(unittest.TestCase):
    def test_extracts_slug_from_events_path(self):
        self.assertEqual(
            staircase._event_slug_for_url(
                "https://example.com/events/fintech-conference-2/something"
            ),
            "fintech-conference-2",
        )

    def test_extracts_slug_with_trailing_slash(self):
        self.assertEqual(
            staircase._event_slug_for_url(
                "https://example.com/events/global-healthcare-conference/"
            ),
            "global-healthcare-conference",
        )

    def test_extracts_slug_no_trailing_path(self):
        self.assertEqual(
            staircase._event_slug_for_url(
                "https://example.com/events/digital-assets"
            ),
            "digital-assets",
        )

    def test_strips_query_and_fragment(self):
        self.assertEqual(
            staircase._event_slug_for_url(
                "https://example.com/events/foo-bar/?utm=x#top"
            ),
            "foo-bar",
        )

    def test_non_events_path_returns_none(self):
        self.assertIsNone(
            staircase._event_slug_for_url("https://example.com/insights/foo")
        )

    def test_events_root_returns_none(self):
        self.assertIsNone(
            staircase._event_slug_for_url("https://example.com/events/")
        )
        self.assertIsNone(
            staircase._event_slug_for_url("https://example.com/events")
        )

    def test_no_path_returns_none(self):
        self.assertIsNone(staircase._event_slug_for_url("https://example.com"))

    def test_empty_returns_none(self):
        self.assertIsNone(staircase._event_slug_for_url(""))
        self.assertIsNone(staircase._event_slug_for_url(None))

    def test_no_scheme(self):
        self.assertEqual(
            staircase._event_slug_for_url("example.com/events/my-event/x"),
            "my-event",
        )


class ClimbingBreakdownTest(unittest.TestCase):
    """Cover per-transition aggregation, prior-channel attribution, event-URL
    extraction, the per-row floors, and the empty-table skip behaviour for the
    'What pulls visitors up your staircase' section."""

    def _pv(self, url="https://example.com/insights/foo", section="Events",
            authors=None, **extras):
        ev = {
            "metadata_canonical_url": url,
            "metadata_section": section,
            "metadata_authors": authors if authors is not None else ["dianne"],
        }
        ev.update(extras)
        return ev

    def _climber(self, *, prior_ev, current_pages):
        return {
            "first_prior_ev": prior_ev,
            "current_pages": list(current_pages),
        }

    def test_empty_input_returns_empty(self):
        out = staircase.compute_climbing_breakdowns({}, {})
        self.assertEqual(out, [])

    def test_one_time_up_aggregates_combined(self):
        # 6 climbers all of whom arrived via Search Referrers in prior and hit
        # the same page in current. The "one-time → returning or brand lover"
        # group is the union of two transitions; the function takes pre-grouped
        # buckets so this test verifies the rendering shape, not the grouping.
        climbers = []
        for _ in range(6):
            climbers.append(self._climber(
                prior_ev=self._pv(**{"sref_category": "search"}),
                current_pages=[
                    self._pv(url="https://example.com/insights/main", section="Insights"),
                ],
            ))
        out = staircase.compute_climbing_breakdowns(
            {"one_time_up": climbers}, {"Search Referrers": 1000},
        )
        self.assertEqual(len(out), 1)
        b = out[0]
        self.assertEqual(b.key, "one_time_up")
        self.assertEqual(b.visitors, 6)
        self.assertEqual(b.top_prior_channels, [("Search Referrers", 6, 1000, 0.006)])
        self.assertEqual(b.top_pages, [("https://example.com/insights/main", 6)])

    def test_channel_floor_enforced(self):
        # 4 climbers via Direct (below floor of 5), 5 via Search Referrers.
        # Direct should be filtered out.
        climbers = []
        for _ in range(4):
            climbers.append(self._climber(
                prior_ev=self._pv(),
                current_pages=[self._pv(url="https://example.com/p1")],
            ))
        for _ in range(5):
            climbers.append(self._climber(
                prior_ev=self._pv(**{"sref_category": "search"}),
                current_pages=[self._pv(url="https://example.com/p2")],
            ))
        out = staircase.compute_climbing_breakdowns(
            {"one_time_up": climbers},
            {"Direct": 100, "Search Referrers": 100},
        )
        self.assertEqual(len(out), 1)
        channels = {c: cnt for c, cnt, _d, _y in out[0].top_prior_channels}
        self.assertIn("Search Referrers", channels)
        self.assertNotIn("Direct", channels)

    def test_climbing_yield_uses_prior_channel_totals(self):
        climbers = []
        for _ in range(10):
            climbers.append(self._climber(
                prior_ev=self._pv(**{"sref_category": "search"}),
                current_pages=[self._pv()],
            ))
        out = staircase.compute_climbing_breakdowns(
            {"returning_to_bl": climbers},
            {"Search Referrers": 500},
        )
        self.assertEqual(len(out), 1)
        chan, climbers_n, denom, yield_rate = out[0].top_prior_channels[0]
        self.assertEqual(chan, "Search Referrers")
        self.assertEqual(climbers_n, 10)
        self.assertEqual(denom, 500)
        self.assertAlmostEqual(yield_rate, 0.02, places=4)

    def test_climbing_yield_handles_zero_prior_total(self):
        # If a channel has climbers but no prior-window total recorded (edge
        # case: prior_ev not a real arrival), yield is 0.0 and denom is 0.
        climbers = []
        for _ in range(5):
            climbers.append(self._climber(
                prior_ev=self._pv(**{"sref_category": "search"}),
                current_pages=[self._pv()],
            ))
        out = staircase.compute_climbing_breakdowns(
            {"one_time_up": climbers}, {},
        )
        self.assertEqual(len(out), 1)
        chan, climbers_n, denom, yield_rate = out[0].top_prior_channels[0]
        self.assertEqual(denom, 0)
        self.assertEqual(yield_rate, 0.0)

    def test_page_floor_enforced(self):
        # 10 climbers across two pages: /a hit 6 times, /b hit 4 times.
        # With floor=5, only /a surfaces.
        climbers = []
        for _ in range(6):
            climbers.append(self._climber(
                prior_ev=self._pv(),
                current_pages=[self._pv(url="https://example.com/a")],
            ))
        for _ in range(4):
            climbers.append(self._climber(
                prior_ev=self._pv(),
                current_pages=[self._pv(url="https://example.com/b")],
            ))
        out = staircase.compute_climbing_breakdowns(
            {"one_time_up": climbers}, {"Search Referrers": 100},
        )
        self.assertEqual(len(out), 1)
        pages = {p: c for p, c in out[0].top_pages}
        self.assertEqual(pages, {"https://example.com/a": 6})

    def test_page_count_is_distinct_visitors_not_pageviews(self):
        # One climber hits /a five times in the current window. The page
        # should count one climber, not five.
        climbers = [self._climber(
            prior_ev=self._pv(),
            current_pages=[self._pv(url="https://example.com/a") for _ in range(5)],
        )]
        # Add 4 more climbers each hitting /a once so the page clears the floor.
        for _ in range(4):
            climbers.append(self._climber(
                prior_ev=self._pv(),
                current_pages=[self._pv(url="https://example.com/a")],
            ))
        out = staircase.compute_climbing_breakdowns(
            {"one_time_up": climbers}, {"Search Referrers": 100},
        )
        self.assertEqual(len(out), 1)
        pages = {p: c for p, c in out[0].top_pages}
        self.assertEqual(pages, {"https://example.com/a": 5})

    def test_section_url_fallback(self):
        # 5 climbers each hit a different /insights/* page, all with
        # metadata_section="Uncategorized". Section table should collapse them
        # under "(by URL: /insights)" with is_fallback=True.
        climbers = []
        for i in range(5):
            climbers.append(self._climber(
                prior_ev=self._pv(),
                current_pages=[self._pv(
                    url=f"https://example.com/insights/p{i}",
                    section="Uncategorized",
                )],
            ))
        out = staircase.compute_climbing_breakdowns(
            {"one_time_up": climbers}, {"Search Referrers": 100},
        )
        self.assertEqual(len(out), 1)
        sections = out[0].top_sections
        self.assertEqual(len(sections), 1)
        label, count, is_fallback = sections[0]
        self.assertEqual(label, "/insights")
        self.assertEqual(count, 5)
        self.assertTrue(is_fallback)

    def test_events_aggregated_by_slug(self):
        # 3 climbers visit /events/conf-a/{home,agenda,speakers}; each event
        # subtree counts one climber per event, not three.
        climbers = []
        for _ in range(3):
            climbers.append(self._climber(
                prior_ev=self._pv(),
                current_pages=[
                    self._pv(url="https://example.com/events/conf-a/"),
                    self._pv(url="https://example.com/events/conf-a/agenda"),
                    self._pv(url="https://example.com/events/conf-a/speakers/jane"),
                ],
            ))
        out = staircase.compute_climbing_breakdowns(
            {"one_time_up": climbers}, {"Search Referrers": 100},
        )
        self.assertEqual(len(out), 1)
        events = {slug: c for slug, c in out[0].top_events}
        self.assertEqual(events, {"conf-a": 3})

    def test_event_floor_enforced(self):
        # 2 climbers attend conf-a (below event floor of 3), 3 attend conf-b.
        # Only conf-b surfaces.
        climbers = []
        for _ in range(2):
            climbers.append(self._climber(
                prior_ev=self._pv(),
                current_pages=[self._pv(url="https://example.com/events/conf-a/x")],
            ))
        for _ in range(3):
            climbers.append(self._climber(
                prior_ev=self._pv(),
                current_pages=[self._pv(url="https://example.com/events/conf-b/y")],
            ))
        out = staircase.compute_climbing_breakdowns(
            {"one_time_up": climbers}, {"Search Referrers": 100},
        )
        self.assertEqual(len(out), 1)
        events = {slug: c for slug, c in out[0].top_events}
        self.assertEqual(events, {"conf-b": 3})

    def test_empty_table_skipped(self):
        # 5 climbers all hit non-event pages. The events sub-table should be
        # empty (no event clears the floor), so top_events is [].
        climbers = []
        for _ in range(5):
            climbers.append(self._climber(
                prior_ev=self._pv(),
                current_pages=[self._pv(url="https://example.com/insights/x")],
            ))
        out = staircase.compute_climbing_breakdowns(
            {"one_time_up": climbers}, {"Search Referrers": 100},
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].top_events, [])

    def test_group_with_zero_climbers_skipped(self):
        out = staircase.compute_climbing_breakdowns(
            {"one_time_up": [], "returning_to_bl": []}, {},
        )
        self.assertEqual(out, [])


class UaSignatureTest(unittest.TestCase):
    def test_all_fields(self):
        ev = {"ua_browser": "Chrome", "ua_browserversion": "121.0", "ua_os": "Mac OS X"}
        self.assertEqual(staircase._ua_signature(ev), "Chrome/121.0/Mac OS X")

    def test_missing_fields_become_question_mark(self):
        # Locks in the placeholder behavior so a refactor can't silently
        # turn missing UA into None or "" and break grouping.
        self.assertEqual(staircase._ua_signature({}), "?/?/?")

    def test_partial_fields(self):
        ev = {"ua_browser": "Safari"}
        self.assertEqual(staircase._ua_signature(ev), "Safari/?/?")


def _make_meta(
    *,
    current_tier=staircase.ONE_TIME,
    prior_tier=staircase.SILENT,
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


class SilentBrandLoverCohortTest(unittest.TestCase):
    def test_only_silent_from_brand_lover_included(self):
        meta = {
            "bl_silent": _make_meta(
                current_tier=staircase.SILENT,
                prior_tier=staircase.BRAND_LOVER,
            ),
            "still_brand_lover": _make_meta(
                current_tier=staircase.BRAND_LOVER,
                prior_tier=staircase.BRAND_LOVER,
            ),
            "returning_silent": _make_meta(
                current_tier=staircase.SILENT,
                prior_tier=staircase.RETURNING,
            ),
            "one_time_silent": _make_meta(
                current_tier=staircase.SILENT,
                prior_tier=staircase.ONE_TIME,
            ),
        }
        rows = staircase._build_cohort_silent_brand_lovers(meta)
        self.assertEqual([r.visitor_site_id for r in rows], ["bl_silent"])

    def test_last_engaged_comes_from_prior_window(self):
        meta = {
            "vsid": _make_meta(
                current_tier=staircase.SILENT,
                prior_tier=staircase.BRAND_LOVER,
                last_prior_page="https://example.com/insights/x/",
                last_prior_section="Insights",
                last_prior_ts=dt.datetime(2026, 4, 10, 9, 30),
                last_cur_ts=None,
                lifetime_pageviews=7,
            ),
        }
        rows = staircase._build_cohort_silent_brand_lovers(meta)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.last_engaged_page, "https://example.com/insights/x/")
        self.assertEqual(r.last_engaged_section, "Insights")
        self.assertEqual(r.last_engaged_at, "2026-04-10T09:30:00")
        self.assertEqual(r.lifetime_pageviews_in_window, 7)


class ClimbedToBrandLoverCohortTest(unittest.TestCase):
    def test_includes_one_time_and_returning_origins(self):
        meta = {
            "from_one_time": _make_meta(
                current_tier=staircase.BRAND_LOVER,
                prior_tier=staircase.ONE_TIME,
            ),
            "from_returning": _make_meta(
                current_tier=staircase.BRAND_LOVER,
                prior_tier=staircase.RETURNING,
            ),
            "from_silent": _make_meta(
                current_tier=staircase.BRAND_LOVER,
                prior_tier=staircase.SILENT,
            ),
            "stayed_brand_lover": _make_meta(
                current_tier=staircase.BRAND_LOVER,
                prior_tier=staircase.BRAND_LOVER,
            ),
        }
        rows = staircase._build_cohort_climbed_to_brand_lover(meta)
        ids = {r.visitor_site_id for r in rows}
        self.assertEqual(ids, {"from_one_time", "from_returning"})
        for r in rows:
            self.assertIn(r.prior_tier, (staircase.ONE_TIME, staircase.RETURNING))


def _meta_with_prior_ev(
    *,
    section=None,
    section_url="https://example.com/insights/foo/",
    authors=None,
    prior_tier=None,
    current_tier=None,
):
    """Build a visitor_meta entry whose first_prior_ev has the given section
    and authors. Used by section/author climb-yield tests."""
    import staircase as _st
    pt = prior_tier if prior_tier is not None else _st.ONE_TIME
    ct = current_tier if current_tier is not None else _st.RETURNING
    first_prior_ev = {
        "metadata_canonical_url": section_url,
        "metadata_section": section if section is not None else "Insights",
    }
    if authors is not None:
        first_prior_ev["metadata_authors"] = authors
    return {
        "current_tier": ct,
        "prior_tier": pt,
        "channel": "Direct",
        "last_cur_ev": None,
        "last_cur_ts": None,
        "last_prior_ev": first_prior_ev,
        "last_prior_ts": None,
        "first_prior_ev": first_prior_ev,
        "lifetime_pageviews": 3,
    }


class SectionClimbYieldTest(unittest.TestCase):
    def test_climbers_over_prior_visitors_in_section(self):
        # Section A: 12 prior visitors, 3 climbed. Section B: 8 prior, 4
        # climbed (below floor, excluded).
        meta = {}
        for i in range(12):
            climbed = i < 3
            meta[f"a{i}"] = _meta_with_prior_ev(
                section="Insights",
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING if climbed else staircase.ONE_TIME,
            )
        for i in range(8):
            climbed = i < 4
            meta[f"b{i}"] = _meta_with_prior_ev(
                section="Careers",
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING if climbed else staircase.ONE_TIME,
            )
        rows = staircase.compute_section_climb_yield(meta, volume_floor=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].section, "Insights")
        self.assertEqual(rows[0].visitors, 12)
        self.assertEqual(rows[0].climbers, 3)
        self.assertAlmostEqual(rows[0].rate, 0.25)
        self.assertFalse(rows[0].is_fallback)

    def test_url_fallback_when_section_uncategorized(self):
        meta = {}
        for i in range(11):
            meta[f"v{i}"] = _meta_with_prior_ev(
                section="Uncategorized",
                section_url="https://example.com/about/conferences/",
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING,
            )
        rows = staircase.compute_section_climb_yield(meta, volume_floor=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].section, "/about")
        self.assertTrue(rows[0].is_fallback)

    def test_silent_prior_excluded_from_denominator(self):
        # 10 visitors whose prior tier was silent (acquired this period only).
        # They should not contribute to climb yield -- not part of the
        # denominator. Only the 10 real prior-tier visitors count.
        meta = {}
        for i in range(10):
            meta[f"new{i}"] = _meta_with_prior_ev(
                section="Insights",
                prior_tier=staircase.SILENT,
                current_tier=staircase.ONE_TIME,
            )
        for i in range(10):
            meta[f"real{i}"] = _meta_with_prior_ev(
                section="Insights",
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING if i < 5 else staircase.ONE_TIME,
            )
        rows = staircase.compute_section_climb_yield(meta, volume_floor=10)
        self.assertEqual(rows[0].visitors, 10)
        self.assertEqual(rows[0].climbers, 5)


class AuthorClimbYieldTest(unittest.TestCase):
    def test_per_author_with_multi_author_post(self):
        # 11 visitors all read a post by ["a", "b"]; 4 climbed.
        meta = {}
        for i in range(11):
            climbed = i < 4
            meta[f"v{i}"] = _meta_with_prior_ev(
                authors=["a", "b"],
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING if climbed else staircase.ONE_TIME,
            )
        rows = staircase.compute_author_climb_yield(meta, volume_floor=10)
        self.assertEqual(len(rows), 2)
        # Both authors get all 11 visitors as denom and 4 climbers each.
        for row in rows:
            self.assertEqual(row.visitors, 11)
            self.assertEqual(row.climbers, 4)

    def test_below_floor_excluded(self):
        meta = {}
        for i in range(5):
            meta[f"v{i}"] = _meta_with_prior_ev(
                authors=["solo"],
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING,
            )
        rows = staircase.compute_author_climb_yield(meta, volume_floor=10)
        self.assertEqual(rows, [])


def _meta_with_current_sessions(
    *,
    deltas_days,
    prior_tier=None,
    current_tier=None,
    page_per_session=None,
    first_ev=None,
):
    """Build a visitor_meta entry with synthetic current_ts/current_pages
    drawn from session-start deltas (in days from the first session).
    Each "session" is one pageview at the start; that matches what the
    second-visit and return-cadence code needs."""
    import staircase as _st
    base = dt.datetime(2026, 5, 1, 12, 0)
    ts_list = [base + dt.timedelta(days=d) for d in deltas_days]
    page_per_session = page_per_session or [
        f"https://example.com/page{i}/" for i in range(len(deltas_days))
    ]
    pv_list = [
        {"metadata_canonical_url": p, "metadata_section": "Insights"}
        for p in page_per_session
    ]
    return {
        "current_tier": current_tier or _st.RETURNING,
        "prior_tier": prior_tier or _st.ONE_TIME,
        "channel": "Direct",
        "last_cur_ev": pv_list[-1],
        "last_cur_ts": ts_list[-1],
        "last_prior_ev": None,
        "last_prior_ts": None,
        "first_prior_ev": None,
        "first_ev": first_ev or pv_list[0],
        "current_ts": ts_list,
        "current_pages": pv_list,
        "lifetime_pageviews": len(ts_list),
    }


class FirstTouchContentTest(unittest.TestCase):
    def test_aggregates_one_time_climbers_by_first_ev(self):
        # 6 climbers one_time -> returning, all entered via different pages
        # but same section.
        meta = {}
        for i in range(6):
            meta[f"v{i}"] = _meta_with_current_sessions(
                deltas_days=[0],
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING,
                first_ev={
                    "metadata_canonical_url": "https://example.com/insights/post-a/" if i < 5 else "https://example.com/insights/post-b/",
                    "metadata_section": "Insights",
                    "sref_category": "search",
                },
            )
        rows = staircase.compute_first_touch_content(meta, floor=5)
        self.assertEqual(len(rows), 1)
        ft = rows[0]
        self.assertEqual(ft.transition_key, "one_time_up")
        self.assertEqual(ft.climbers, 6)
        # post-a met the floor; post-b (only 1) didn't.
        self.assertEqual(ft.by_page, [("https://example.com/insights/post-a/", 5)])
        self.assertEqual(ft.by_channel, [("Search Referrers", 6)])

    def test_skips_visitors_with_no_first_ev(self):
        meta = {}
        for i in range(6):
            m = _meta_with_current_sessions(
                deltas_days=[0],
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING,
            )
            m["first_ev"] = None
            meta[f"v{i}"] = m
        rows = staircase.compute_first_touch_content(meta, floor=5)
        self.assertEqual(rows, [])


class SecondVisitHookTest(unittest.TestCase):
    def test_returns_second_session_first_pageview(self):
        meta = {}
        for i in range(6):
            meta[f"v{i}"] = _meta_with_current_sessions(
                deltas_days=[0, 1],  # session 1 at day 0, session 2 at day 1
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING,
                page_per_session=[
                    "https://example.com/landing/",
                    "https://example.com/hook/",
                ],
            )
        result = staircase.compute_second_visit_content(meta, floor=5)
        self.assertEqual(result.rows, [("https://example.com/hook/", 6)])

    def test_skips_visitors_with_single_session(self):
        meta = {
            "v1": _meta_with_current_sessions(
                deltas_days=[0],  # only one session
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING,
            ),
        }
        result = staircase.compute_second_visit_content(meta, floor=1)
        self.assertEqual(result.rows, [])

    def test_excludes_non_one_time_to_returning_climbers(self):
        # Returning -> brand-lover with a second session should NOT be in
        # the second-visit hook (that's a different climbing transition).
        meta = {
            "v1": _meta_with_current_sessions(
                deltas_days=[0, 1],
                prior_tier=staircase.RETURNING,
                current_tier=staircase.BRAND_LOVER,
            ),
        }
        result = staircase.compute_second_visit_content(meta, floor=1)
        self.assertEqual(result.rows, [])


class ReturnCadenceTest(unittest.TestCase):
    def test_buckets_visitors_by_session_delta(self):
        meta = {
            "v_fast": _meta_with_current_sessions(deltas_days=[0, 1]),  # 0-2d
            "v_week": _meta_with_current_sessions(deltas_days=[0, 5]),  # 3-7d
            "v_month": _meta_with_current_sessions(deltas_days=[0, 15]),  # 8-30d
            "v_long": _meta_with_current_sessions(deltas_days=[0, 60]),  # 30+
        }
        buckets = staircase.compute_return_cadence(meta)
        label_to_count = {
            b.label: b.visitors_with_second_visit for b in buckets
        }
        self.assertEqual(label_to_count["0-2 days"], 1)
        self.assertEqual(label_to_count["3-7 days"], 1)
        self.assertEqual(label_to_count["8-30 days"], 1)
        self.assertEqual(label_to_count["30+ days"], 1)

    def test_climb_rate_per_bucket(self):
        meta = {
            "climber_fast": _meta_with_current_sessions(
                deltas_days=[0, 1],
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING,
            ),
            "nonclimber_fast": _meta_with_current_sessions(
                deltas_days=[0, 1],
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.ONE_TIME,
            ),
        }
        buckets = staircase.compute_return_cadence(meta)
        fast_bucket = next(b for b in buckets if b.label == "0-2 days")
        self.assertEqual(fast_bucket.visitors_with_second_visit, 2)
        self.assertEqual(fast_bucket.climbers, 1)
        self.assertAlmostEqual(fast_bucket.rate, 0.5)


class RelationshipMarkerLiftTest(unittest.TestCase):
    def test_lift_above_one_when_firers_climb_more(self):
        # 10 firers, 5 climbed (50%). 10 non-firers, 1 climbed (10%).
        # Lift = 5.0x.
        meta = {}
        relationship_markers = {
            "newsletter_signup": set(),
            "account_creation": set(),
            "login": set(),
        }
        for i in range(10):
            vsid = f"fired_{i}"
            meta[vsid] = _meta_with_current_sessions(
                deltas_days=[0],
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING if i < 5 else staircase.ONE_TIME,
            )
            relationship_markers["newsletter_signup"].add(vsid)
        for i in range(10):
            vsid = f"unfired_{i}"
            meta[vsid] = _meta_with_current_sessions(
                deltas_days=[0],
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING if i == 0 else staircase.ONE_TIME,
            )
        rows = staircase.compute_marker_lift(meta, relationship_markers)
        ml_nl = next(r for r in rows if r.marker == "newsletter_signup")
        self.assertEqual(ml_nl.fired_count, 10)
        self.assertEqual(ml_nl.fired_climbers, 5)
        self.assertAlmostEqual(ml_nl.fired_climb_rate, 0.5)
        self.assertEqual(ml_nl.unfired_count, 10)
        self.assertEqual(ml_nl.unfired_climbers, 1)
        self.assertAlmostEqual(ml_nl.unfired_climb_rate, 0.1)
        self.assertAlmostEqual(ml_nl.lift_ratio, 5.0)

    def test_no_unfired_climbers_yields_none_lift(self):
        meta = {
            "v1": _meta_with_current_sessions(
                deltas_days=[0],
                prior_tier=staircase.ONE_TIME,
                current_tier=staircase.RETURNING,
            ),
        }
        markers = {"newsletter_signup": {"v1"}, "account_creation": set(), "login": set()}
        rows = staircase.compute_marker_lift(meta, markers)
        ml = next(r for r in rows if r.marker == "newsletter_signup")
        self.assertEqual(ml.fired_count, 1)
        self.assertEqual(ml.unfired_count, 0)
        self.assertIsNone(ml.lift_ratio)

    def test_silent_prior_excluded(self):
        # A visitor who was silent in the prior window can't "climb" in the
        # tier-rank sense, so they shouldn't be in either the fired or
        # unfired pool.
        meta = {
            "new_visitor": _meta_with_current_sessions(
                deltas_days=[0],
                prior_tier=staircase.SILENT,
                current_tier=staircase.ONE_TIME,
            ),
        }
        markers = {"newsletter_signup": {"new_visitor"}, "account_creation": set(), "login": set()}
        rows = staircase.compute_marker_lift(meta, markers)
        ml = next(r for r in rows if r.marker == "newsletter_signup")
        self.assertEqual(ml.fired_count, 0)
        self.assertEqual(ml.unfired_count, 0)


class NetMovementTest(unittest.TestCase):
    def test_up_down_net_basic(self):
        # 5 climbed up (one-time -> returning), 3 climbed down (BL -> returning).
        # 10 silent transitions should NOT count toward up/down.
        transitions = {
            (staircase.ONE_TIME, staircase.RETURNING): 5,
            (staircase.BRAND_LOVER, staircase.RETURNING): 3,
            (staircase.BRAND_LOVER, staircase.SILENT): 10,
            (staircase.SILENT, staircase.ONE_TIME): 7,
            (staircase.ONE_TIME, staircase.ONE_TIME): 4,
        }
        nm = staircase.compute_net_movement(transitions)
        self.assertEqual(nm.up, 5)
        self.assertEqual(nm.down, 3)
        self.assertEqual(nm.net, 2)
        self.assertEqual(nm.by_transition[(staircase.ONE_TIME, staircase.RETURNING)], 5)
        self.assertEqual(nm.by_transition[(staircase.BRAND_LOVER, staircase.RETURNING)], 3)
        # Same-tier and silent transitions should not be recorded.
        self.assertNotIn((staircase.ONE_TIME, staircase.ONE_TIME), nm.by_transition)
        self.assertNotIn((staircase.BRAND_LOVER, staircase.SILENT), nm.by_transition)
        self.assertNotIn((staircase.SILENT, staircase.ONE_TIME), nm.by_transition)

    def test_no_movement_yields_zeros(self):
        nm = staircase.compute_net_movement({
            (staircase.ONE_TIME, staircase.ONE_TIME): 100,
            (staircase.RETURNING, staircase.RETURNING): 50,
        })
        self.assertEqual((nm.up, nm.down, nm.net), (0, 0, 0))

    def test_only_down_yields_negative_net(self):
        nm = staircase.compute_net_movement({
            (staircase.BRAND_LOVER, staircase.ONE_TIME): 4,
        })
        self.assertEqual(nm.up, 0)
        self.assertEqual(nm.down, 4)
        self.assertEqual(nm.net, -4)


class AtRiskNowTest(unittest.TestCase):
    def test_bl_to_returning_and_returning_to_one_time_included(self):
        meta = {
            "bl_to_ret": _make_meta(
                current_tier=staircase.RETURNING,
                prior_tier=staircase.BRAND_LOVER,
            ),
            "ret_to_one": _make_meta(
                current_tier=staircase.ONE_TIME,
                prior_tier=staircase.RETURNING,
            ),
            "bl_to_silent": _make_meta(
                current_tier=staircase.SILENT,
                prior_tier=staircase.BRAND_LOVER,
            ),
            "climbing": _make_meta(
                current_tier=staircase.BRAND_LOVER,
                prior_tier=staircase.RETURNING,
            ),
        }
        ar = staircase._build_cohort_at_risk_now(meta)
        self.assertEqual(ar.bl_to_returning, 1)
        self.assertEqual(ar.returning_to_one_time, 1)
        ids = {m.visitor_site_id for m in ar.members}
        self.assertEqual(ids, {"bl_to_ret", "ret_to_one"})

    def test_prior_tier_populated_for_each_member(self):
        meta = {
            "v1": _make_meta(
                current_tier=staircase.RETURNING,
                prior_tier=staircase.BRAND_LOVER,
            ),
        }
        ar = staircase._build_cohort_at_risk_now(meta)
        self.assertEqual(ar.members[0].prior_tier, staircase.BRAND_LOVER)

    def test_empty_when_no_backsliders(self):
        meta = {
            "stayed": _make_meta(
                current_tier=staircase.BRAND_LOVER,
                prior_tier=staircase.BRAND_LOVER,
            ),
        }
        ar = staircase._build_cohort_at_risk_now(meta)
        self.assertEqual(ar.bl_to_returning, 0)
        self.assertEqual(ar.returning_to_one_time, 0)
        self.assertEqual(ar.members, [])


class WriteCohortCsvsTest(unittest.TestCase):
    def _make_result(self, **fields):
        return staircase.StaircaseResult(
            site_label="test",
            current_window=(dt.date(2026, 5, 1), dt.date(2026, 5, 17)),
            prior_window=(dt.date(2026, 4, 13), dt.date(2026, 4, 30)),
            site_id_filter="example.com",
            tier_counts_current={},
            tier_counts_prior={},
            transitions={},
            climb_yield_by_channel={},
            daily_visitors={},
            total_visitors_seen=0,
            pageviews_loaded=0,
            bot_threshold=0,
            bot_visitors_filtered=0,
            bot_groups=[],
            **fields,
        )

    def test_silent_brand_lovers_csv_has_expected_columns(self):
        member = staircase.CohortMember(
            visitor_site_id="pid=abc",
            last_engaged_page="https://example.com/x",
            last_engaged_section="Insights",
            last_engaged_at="2026-04-15T10:00:00",
            lifetime_pageviews_in_window=8,
        )
        result = self._make_result(silent_brand_lovers=[member])
        with tempfile.TemporaryDirectory() as td:
            written = staircase.write_cohort_csvs(
                result, cohort_dir=td, slug="test",
            )
            self.assertIn("silent-brand-lovers", written)
            path = written["silent-brand-lovers"]
            self.assertTrue(path.endswith(
                "staircase-test-cohort-silent-brand-lovers.csv"
            ))
            with open(path, encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
            self.assertEqual(rows[0], [
                "visitor_site_id",
                "last_engaged_page",
                "last_engaged_section",
                "last_engaged_at",
                "lifetime_pageviews_in_window",
            ])
            self.assertEqual(rows[1][0], "pid=abc")
            self.assertEqual(rows[1][4], "8")
            # The result should also remember where it wrote things so the
            # callouts can link to them.
            self.assertEqual(
                result.cohort_paths["silent-brand-lovers"], path,
            )

    def test_empty_cohorts_skip_file_creation(self):
        result = self._make_result()
        with tempfile.TemporaryDirectory() as td:
            written = staircase.write_cohort_csvs(
                result, cohort_dir=td, slug="test",
            )
            self.assertEqual(written, {})
            self.assertEqual(os.listdir(td), [])


class CalloutsTest(unittest.TestCase):
    """Spot-check that the rewritten callouts name a concrete thing to act on
    rather than a 'figure out X' hand-wave."""

    def test_strong_channel_callout_references_strong_channels_section(self):
        rows = [
            staircase.ChannelClimbYield(
                channel="Search Referrers",
                visitors=1000,
                climbers=200,
                rate=0.20,
            ),
        ]
        out = staircase._callouts(
            channel_climb_yield=rows,
            site_climb_rate=0.10,
            silent_from_bl=0,
        )
        text = "\n".join(out)
        self.assertIn("Search Referrers", text)
        self.assertIn("Top levers to grow relationships", text)
        # Strong-channel callout itself must lead with a verb. (When
        # join-ID wiring is unset, a separate "Make these cohorts
        # email-actionable" callout is prepended; we don't care where
        # the strong-channel one falls, only that it's still verb-led.)
        strong = next(c for c in out if "Search Referrers" in c)
        self.assertTrue(
            strong.startswith("**Commission"),
            f"expected verb-led callout, got: {strong!r}",
        )
        # The old "figure out what makes this channel work and lean in"
        # text should NOT come back.
        self.assertNotIn("figure out what", text)

    def test_silent_brand_lover_callout_links_cohort_csv(self):
        out = staircase._callouts(
            channel_climb_yield=[],
            site_climb_rate=0.10,
            silent_from_bl=42,
            silent_brand_lovers_count=42,
            cohort_paths={
                "silent-brand-lovers": "/abs/path/cohort.csv",
            },
        )
        text = "\n".join(out)
        # Action-led: commission-against verb at the top.
        self.assertIn("Commission against what your churned brand lovers", text)
        self.assertIn("42 went silent", text)
        self.assertIn("file:///abs/path/cohort.csv", text)
        self.assertIn("Cohort: 42 visitors", text)
        # Honest about the gap that blocks individual targeting; points
        # the reader at Method & caveats for the full pattern.
        self.assertIn("per-recipient identifier", text)
        self.assertIn("Method & caveats", text)
        self.assertNotIn("targeted re-engagement against those", text)


class JoinIdWiringTest(unittest.TestCase):
    """Behavior of the join-ID-wiring callout and re-engagement callouts."""

    def _callouts(self, *, join_id_key=None, join_id_populated=False, at_risk=None):
        return staircase._callouts(
            channel_climb_yield=[],
            site_climb_rate=0.10,
            silent_from_bl=42,
            silent_brand_lovers_count=42,
            cohort_paths={
                "silent-brand-lovers": "/abs/path/silent.csv",
                "at-risk-now": "/abs/path/at-risk.csv",
            },
            join_id_key=join_id_key,
            join_id_populated=join_id_populated,
            at_risk_now=at_risk,
        )

    def test_wiring_callout_first_when_key_unset(self):
        out = self._callouts()
        self.assertTrue(out[0].startswith("**Make these cohorts email-actionable**"))

    def test_wiring_callout_still_shown_when_key_set_but_empty(self):
        out = self._callouts(join_id_key="pid", join_id_populated=False)
        self.assertTrue(any("Make these cohorts email-actionable" in c for c in out))
        self.assertFalse(any("Build a re-engagement send" in c for c in out))

    def test_wiring_callout_drops_when_populated(self):
        ar = staircase.AtRiskNow(bl_to_returning=0, returning_to_one_time=0, members=[])
        out = self._callouts(join_id_key="pid", join_id_populated=True, at_risk=ar)
        self.assertFalse(any("Make these cohorts email-actionable" in c for c in out))

    def test_silent_bl_flips_to_re_engagement_when_wired(self):
        out = self._callouts(join_id_key="pid", join_id_populated=True)
        silent = next(c for c in out if "Commission against" in c)
        self.assertIn("Build a re-engagement send", silent)
        self.assertIn("`pid`", silent)

    def test_at_risk_callout_appears_only_when_wired_and_nonempty(self):
        # Empty at-risk: no callout even when wired.
        ar_empty = staircase.AtRiskNow(
            bl_to_returning=0, returning_to_one_time=0, members=[],
        )
        out = self._callouts(join_id_key="pid", join_id_populated=True, at_risk=ar_empty)
        self.assertFalse(any("Re-engage your at-risk" in c for c in out))

        # Populated at-risk: callout appears, names the column.
        members = [
            staircase.CohortMember(
                visitor_site_id="v1",
                last_engaged_page="/x",
                last_engaged_section="s",
                last_engaged_at="2026-05-01T00:00:00",
                lifetime_pageviews_in_window=4,
                prior_tier=staircase.BRAND_LOVER,
                join_id_value="alice@example.com",
            ),
        ]
        ar = staircase.AtRiskNow(
            bl_to_returning=1, returning_to_one_time=0, members=members,
        )
        out = self._callouts(join_id_key="pid", join_id_populated=True, at_risk=ar)
        at_risk = next(c for c in out if "Re-engage your at-risk" in c)
        self.assertIn("`pid`", at_risk)
        self.assertIn("file:///abs/path/at-risk.csv", at_risk)


if __name__ == "__main__":
    unittest.main()
