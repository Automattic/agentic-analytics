#!/usr/bin/env python3
"""Relationship Staircase Report from Parsely DPL events.

Reads gzipped DPL JSON events for two adjacent time windows, classifies each
visitor into tiers (1 visit / 2-4 visits / 5+ visits) by session count within
each window, and emits markdown and/or HTML reports covering: tier counts,
deltas, transitions, climb yield by channel, and the relationship-marker tap
(newsletter signup / account creation / login events that act as climbing
signals). Revenue conversions live in plugin/skills/conversions-report/.

Stdlib-only on purpose so we can throw it away cheaply.

Cache layout:
    DPL events are read from a per-site cache under
        $XDG_CACHE_HOME/agentic-analytics/dpl/<site-id>/
    (defaults to ~/.cache/agentic-analytics/dpl/<site-id>/). Pass --events-dir
    to override. The cache persists across sessions so daily runs only need
    to pull new days, not re-download the whole window.

Usage:
    # cache lives at ~/.cache/agentic-analytics/dpl/<site-id>/ by default
    python3 staircase.py \
        --site-id your-site.com \
        --current 2026-04-23 2026-04-29 \
        --prior 2026-04-17 2026-04-22 \
        --site-label "Your Site" \
        --output-md /tmp/staircase-yoursite.md \
        --output-html /tmp/staircase-yoursite.html
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import glob
import gzip
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit


SESSION_GAP = dt.timedelta(minutes=30)
# `conversion` stays in the filter so the narrow relationship-markers tap
# (compute_staircase) can see newsletter_signup / account_creation / login
# events. Revenue conversions are NOT consumed here; they move to the
# conversions-report sibling skill.
KEEP_ACTIONS = ("pageview", "conversion")

SMALL_VOLUME_FLOOR = 20
STRONG_CHANNEL_CLIMB_RATIO = 1.5
STRONG_CHANNEL_LANDING_PAGE_FLOOR = 10
STRONG_CHANNEL_SECTION_FLOOR = 20
STRONG_CHANNEL_AUTHOR_FLOOR = 10
STRONG_CHANNEL_TOP_N = 5

CLIMBING_CHANNEL_FLOOR = 5
CLIMBING_PAGE_FLOOR = 5
CLIMB_YIELD_VOLUME_FLOOR = 10
CLIMB_YIELD_TOP_N = 10
CLIMBING_SECTION_FLOOR = 5
CLIMBING_EVENT_FLOOR = 3
CLIMBING_CHANNEL_TOP_N = 6
CLIMBING_PAGE_TOP_N = 10
CLIMBING_SECTION_TOP_N = 5
CLIMBING_EVENT_TOP_N = 5


def default_cache_root() -> str:
    """Persistent cache root for DPL events, honoring XDG_CACHE_HOME."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = xdg if xdg else os.path.expanduser("~/.cache")
    return os.path.join(base, "agentic-analytics", "dpl")

# Bot/scraper detection: any (first_pv_date, ip_city, ua_signature) tuple
# with more than this many distinct visitor_site_ids is treated as a single
# bot run and excluded from all downstream metrics. 100 catches the patterns
# we've seen on real customer data (sustained scrapers from a single city,
# single-day visitor-id bursts from a single UA fingerprint). Pass 0 to
# disable filtering.
DEFAULT_BOT_THRESHOLD = 100


# --------------------------------------------------------------------------
# Tiering
# --------------------------------------------------------------------------

ONE_TIME = "one_time"
RETURNING = "returning"
BRAND_LOVER = "brand_lover"
SILENT = "silent"

TIER_RANK = {SILENT: 0, ONE_TIME: 1, RETURNING: 2, BRAND_LOVER: 3}


def tier_for(visit_count: int) -> str:
    if visit_count == 0:
        return SILENT
    if visit_count == 1:
        return ONE_TIME
    if visit_count <= 4:
        return RETURNING
    return BRAND_LOVER


def session_count(sorted_ts: Sequence[dt.datetime], gap: dt.timedelta) -> int:
    if not sorted_ts:
        return 0
    count = 1
    last = sorted_ts[0]
    for ts in sorted_ts[1:]:
        if ts - last >= gap:
            count += 1
        last = ts
    return count


def session_starts(
    sorted_ts: Sequence[dt.datetime], gap: dt.timedelta,
) -> List[int]:
    """Return the indices in `sorted_ts` that start a new session. Index 0
    is always a session start when sorted_ts is non-empty. PR F uses this
    to identify the second session's first event for second-visit content
    and return-cadence analysis."""
    if not sorted_ts:
        return []
    starts = [0]
    for i in range(1, len(sorted_ts)):
        if sorted_ts[i] - sorted_ts[i - 1] >= gap:
            starts.append(i)
    return starts


# --------------------------------------------------------------------------
# Event reading
# --------------------------------------------------------------------------


# Channel grouping rules. Priority order: first match wins. Combines UTM
# tagging (intentional marketer signal) with Parsely's sref_category
# (referrer-based) so visitors aren't split across "utm:X" and "category:Y"
# buckets that often refer to the same source.
#
# The Email rule matters because email clicks typically have no HTTP referrer
# and would otherwise collapse into Direct; UTM is the only signal we have.
#
# The Network rule (when configured) covers cross-promotion traffic between
# sites the customer owns or considers part of their network. Customers
# declare a label and a list of utm_source / utm_medium values via the
# --network-label and --network-sources CLI flags; without those, the rule
# is disabled and no traffic is bucketed under a network channel.

_EMAIL_UTM_SOURCES = {
    "hs_email", "mailchimp", "sendgrid", "klaviyo", "marketo", "pardot",
    "constant_contact", "campaignmonitor",
}
_PAID_MEDIUMS = {"ppc", "paid", "cpc", "paid_search"}


def _matches_suffix(domain: Optional[str], suffixes: Tuple[str, ...]) -> bool:
    if not domain:
        return False
    d = domain.lower()
    for s in suffixes:
        if d == s or d.endswith("." + s):
            return True
    return False


# Known AI/LLM-powered search and chat referrers. Parsely's `sref_category=ai`
# isn't reliably set on all of these (chatgpt.com / claude.ai / perplexity / etc.
# often arrive without it), so we match by domain too. AI is a real human
# acquisition channel and should be treated as one, not split across per-domain
# referral lines.
_AI_REFERRER_SUFFIXES: Tuple[str, ...] = (
    "chatgpt.com",
    "chat.openai.com",
    "openai.com",
    "claude.ai",
    "perplexity.ai",
    "gemini.google.com",
    "bard.google.com",
    "copilot.microsoft.com",
    "you.com",
    "phind.com",
)


def _is_ai_referrer(domain: Optional[str]) -> bool:
    return _matches_suffix(domain, _AI_REFERRER_SUFFIXES)


# Domains we silently skip from referral recommendations. The visitor isn't
# necessarily the customer's own staff -- could be enterprise users at any
# M365 / Slack-using firm -- but a recommendation to "invest in growing
# Microsoft Teams referrals" is useless either way: the click is incidental,
# not an investable marketing channel. Kept as a small built-in list;
# customers also supply their own corp domain(s) via --internal-domains.
_SKIP_RECOMMENDATION_SUFFIXES: Tuple[str, ...] = (
    "microsoftonline.com",
    "teams.microsoft.com",
    "office.net",
    "office365.com",
    "slack.com",
    "webex.com",
)


def _skip_referral_recommendation(
    channel: str, customer_corp_domains: Iterable[str]
) -> bool:
    """True for `Referral: <domain>` channels that should NOT trigger
    recommendations: known corporate-tool referrers (Teams, M365 SSO, Slack,
    Webex) and any customer-supplied corp domain. Channel still appears in
    the per-domain table; only the callout loop skips it."""
    if not channel.startswith("Referral: "):
        return False
    domain = channel[len("Referral: "):]
    if _matches_suffix(domain, _SKIP_RECOMMENDATION_SUFFIXES):
        return True
    d = domain.lower()
    for corp in customer_corp_domains:
        c = (corp or "").lower().strip()
        if not c:
            continue
        if d == c or d.endswith("." + c):
            return True
    return False


def acquisition_channel(
    ev: Mapping,
    *,
    network_label: Optional[str] = None,
    network_sources: Tuple[str, ...] = (),
) -> Optional[str]:
    """Map a pageview to one customer-facing channel bucket.

    Channel names use "X Referrers" when the detection is referrer-based
    (sref_category=…) and a plain name when the detection is UTM-based,
    so the customer can read the table without guessing which signal
    drove which bucket. Returns one of:

        Email, Paid, <network_label> (if configured),
        Social Referrers, Search Referrers, AI Referrers,
        Referral: <domain>,
        Direct

    The optional network rule is configured via `network_label` (the channel
    name) and `network_sources` (a tuple of values matched against either
    `utm_source` or `utm_medium`). Customers declare both via the CLI flags
    `--network-label` and `--network-sources`. When `network_label` is not
    set, the rule is disabled and no events route to a network channel.

    Returns None when the first-pageview-in-window is NOT real acquisition:

    - sref_category=internal, same domain: the visitor's prior session
      timed out mid-trip; this isn't a new arrival, it's a continuation.
    - sref_category=internal, different subdomain (e.g. ir.example.com →
      example.com): we don't know what brought the visitor to the
      sister subdomain originally, so we can't honestly call the
      subdomain a "source." Drop from the channel mix.

    Visitors classified None still count toward tier totals; they just
    don't get attributed to any acquisition channel.
    """
    utm_medium = (ev.get("utm_medium") or "").lower()
    utm_source_raw = ev.get("utm_source") or ""
    utm_source_lc = utm_source_raw.lower()
    sref_cat = ev.get("sref_category") or ""
    sref_domain = ev.get("sref_domain")

    if utm_medium == "email" or utm_source_lc in _EMAIL_UTM_SOURCES:
        return "Email"
    if utm_medium in _PAID_MEDIUMS:
        return "Paid"
    if network_label and (utm_source_lc in network_sources or utm_medium in network_sources):
        return network_label
    if sref_cat == "social" or utm_medium == "social":
        return "Social Referrers"
    if sref_cat == "search":
        return "Search Referrers"
    if sref_cat == "internal":
        # Not real acquisition: either same-domain session continuation
        # or cross-subdomain navigation whose original source we can't
        # recover from this event. Don't claim a source we don't have.
        return None
    if sref_cat == "ai" or _is_ai_referrer(sref_domain):
        return "AI Referrers"
    if sref_domain:
        return f"Referral: {sref_domain}"
    return "Direct"


def parse_ts(raw) -> Optional[dt.datetime]:
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            return dt.datetime.utcfromtimestamp(float(raw))
        s = str(raw).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is not None:
            d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return d
    except Exception:
        return None


def iter_events(
    files: Iterable[str],
    *,
    site_id_filter: Optional[str],
    keep_actions: Sequence[str] = KEEP_ACTIONS,
    employee_filter_key: Optional[str] = None,
    employee_filter_value: Any = None,
):
    """Yield (action, ts, ev) tuples for events matching the site ID filter.

    The DPL event schema uses the legacy field name `apikey` for what
    Parse.ly's customer-facing docs call site ID; the field is read by that
    name below.

    When employee_filter_key is set, events where
    `extra_data[employee_filter_key] == employee_filter_value` are excluded.
    The filter is configured by /agentic-analytics:identify-employees and
    written to bucket.json; staircase.py just consumes it."""
    keep_set = set(keep_actions)
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
                    action = ev.get("action")
                    if action not in keep_set:
                        continue
                    if site_id_filter and ev.get("apikey") != site_id_filter:
                        continue
                    if employee_filter_key is not None and employee_filter_value is not None:
                        ed = ev.get("extra_data")
                        if isinstance(ed, dict) and ed.get(employee_filter_key) == employee_filter_value:
                            continue
                    ts = parse_ts(ev.get("ts_action"))
                    if ts is None:
                        continue
                    yield action, ts, ev
        except OSError:
            continue


# --------------------------------------------------------------------------
# Compute
# --------------------------------------------------------------------------


@dataclasses.dataclass
class BotGroup:
    first_pv_date: dt.date
    ip_city: str
    ua_signature: str
    visitor_count: int


@dataclasses.dataclass
class StrongChannelBreakdown:
    """Per-channel relationship-growth view, restricted to channels whose
    prior-window arrivals climbed a tier at meaningfully above the site-
    wide rate. `visitors` is prior-window visitors via the channel,
    `climbers` is how many of those climbed, `rate` is climbers / visitors,
    `ratio` is `rate` vs the site's overall climb rate."""
    channel: str
    visitors: int
    climbers: int
    rate: float
    ratio: float
    top_pages: List[Tuple[str, int, float]]
    top_sections: List[Tuple[str, int, float, bool]]
    top_authors: List[Tuple[str, int, float]]


@dataclasses.dataclass
class ClimbingBreakdown:
    key: str
    header: str
    visitors: int
    top_prior_channels: List[Tuple[str, int, int, float]]
    top_pages: List[Tuple[str, int]]
    top_sections: List[Tuple[str, int, bool]]
    top_events: List[Tuple[str, int]]


@dataclasses.dataclass
class SectionClimbYield:
    """Per-section view of climbing: of visitors whose first prior-window
    pageview landed on this section, what fraction climbed to a higher tier
    in the current window. is_fallback flags rows where the section label
    came from URL-first-segment because metadata_section was empty or
    Uncategorized."""
    section: str
    is_fallback: bool
    visitors: int
    climbers: int
    rate: float


@dataclasses.dataclass
class AuthorClimbYield:
    """Per-author view of climbing, computed on the prior-window first
    pageview's `metadata_authors`. Multi-author posts: a visitor whose
    first prior pageview lists two authors contributes once to each
    author's denominator and (if they climbed) once to each author's
    climbers count. Same visitor across multiple posts: each post's
    authors are counted independently if those posts were the first
    prior-window pageview."""
    author: str
    visitors: int
    climbers: int
    rate: float


@dataclasses.dataclass
class ChannelClimbYield:
    """Per-channel view of climbing: of visitors whose first prior-window
    pageview was attributed to this acquisition channel, the fraction who
    climbed to a higher tier in the current window. Same transition math
    as SectionClimbYield and AuthorClimbYield (prior-active denominator,
    real tier-rank climb in numerator), so the three tables compose."""
    channel: str
    visitors: int
    climbers: int
    rate: float


@dataclasses.dataclass
class FirstTouchContent:
    """For one climbing transition, where its climbers entered the property
    originally (their earliest pageview in cached DPL data). Each list is
    [(label, count)] sorted by count desc, trimmed to top_n. Channel rows
    use the `acquisition_channel` taxonomy, page rows use the full URL,
    section rows are tuples (label, count, is_fallback)."""
    transition_key: str
    header: str
    climbers: int
    by_page: List[Tuple[str, int]]
    by_section: List[Tuple[str, int, bool]]
    by_channel: List[Tuple[str, int]]


@dataclasses.dataclass
class SecondVisitContent:
    """The page (URL) read on the second visit by one-time -> returning
    climbers. The "second visit" is the first pageview of the second
    session (sessions defined by SESSION_GAP). Sorted by count desc,
    trimmed to top_n."""
    rows: List[Tuple[str, int]]


@dataclasses.dataclass
class ReturnCadenceBucket:
    """For climbers with at least two sessions in the current window, the
    delta between session 1 and session 2 bucketed into a fixed set of
    ranges (0-2d / 3-7d / 8-30d / 30d+). Climb rate is climbers in this
    bucket divided by total visitors-with-second-visit in this bucket."""
    label: str
    visitors_with_second_visit: int
    climbers: int
    rate: float


@dataclasses.dataclass
class RelationshipMarkerLift:
    """For one marker type (newsletter_signup / account_creation / login),
    does firing the event in the current window correlate with climbing?

    fired_climb_rate = (visitors who fired AND climbed) / (visitors who fired)
    unfired_climb_rate = (visitors who climbed but didn't fire) / (visitors who didn't fire)
    lift_ratio = fired_climb_rate / unfired_climb_rate (None if unfired_climb_rate == 0)

    A lift_ratio > 1 means firing the marker is associated with higher
    climbing odds than not firing. PR F's test of Bob's hypothesis that
    these events are climbing levers."""
    marker: str
    fired_count: int
    fired_climbers: int
    fired_climb_rate: float
    unfired_count: int
    unfired_climbers: int
    unfired_climb_rate: float
    lift_ratio: Optional[float]


@dataclasses.dataclass
class NetMovement:
    """Headline summary of tier-to-tier flow over the comparison window.

    `up` counts transitions where the destination tier outranks the source
    (one-time -> returning, one-time -> brand-lover, returning -> brand-lover).
    `down` counts transitions where the destination tier underranks the source
    AND the source was a real tier (brand-lover -> returning,
    brand-lover -> one-time, returning -> one-time). Net silence (any tier
    -> SILENT) is NOT a backslide for this headline; it surfaces as "Gone
    silent" instead. Same for net-new visitors (SILENT -> any tier) -- those
    are net-new acquisitions, not movement up the staircase.

    by_transition is the raw (prior, current) -> count map filtered to the
    transitions that contributed to up + down (useful for explainability
    in the renderer).

    eligible is the count of prior-window-active visitors (those with a real
    prior tier, i.e. prior != SILENT) regardless of where they ended up in
    the current window -- this is the denominator for the headline climb
    rate. It includes climbers, descenders, flat-stayers, AND visitors who
    went silent. climb_rate = up / eligible (or 0.0 if no prior-active
    visitors); this is the % of last period's active visitors who climbed
    a tier this period, the metric that drives the report headline."""
    up: int
    down: int
    net: int
    by_transition: Dict[Tuple[str, str], int]
    eligible: int = 0
    climb_rate: float = 0.0


@dataclasses.dataclass
class AtRiskNow:
    """In-window backsliders. Distinct from `silent_brand_lovers` (which is
    out-of-window churn). These visitors are still active but moving the
    wrong direction: a brand lover who slipped to returning, or a returning
    visitor who slipped to one-time. The action is re-engagement before
    they're gone."""
    bl_to_returning: int
    returning_to_one_time: int
    members: List["CohortMember"]


@dataclasses.dataclass
class CohortMember:
    """One row in a cohort CSV.

    prior_tier and arrival_channel are populated only for the cohorts that
    need them (currently just climbed-to-brand-lover, which uses prior_tier).
    arrival_channel is retained for compatibility with the cohort CSV writer;
    cohorts added by later PRs may use it.

    join_id_value carries the captured value of the configured URL query
    parameter (the customer's per-recipient or per-send identifier from
    their email campaigns). Empty string when no value was seen for this
    visitor. The CSV writer surfaces it under the customer-configured
    column name (e.g. `pid`), not as `join_id_value`.
    """
    visitor_site_id: str
    last_engaged_page: str
    last_engaged_section: str
    last_engaged_at: str
    lifetime_pageviews_in_window: int
    prior_tier: Optional[str] = None
    arrival_channel: Optional[str] = None
    join_id_value: str = ""


@dataclasses.dataclass
class StaircaseResult:
    site_label: str
    current_window: Tuple[dt.date, dt.date]
    prior_window: Tuple[dt.date, dt.date]
    site_id_filter: Optional[str]
    tier_counts_current: Dict[str, int]
    tier_counts_prior: Dict[str, int]
    transitions: Dict[Tuple[str, str], int]
    # Per-channel climb yield: {channel: {"prior_visitors": N, "climbers": M}}
    # Computed on prior-window first pageviews, climbed flag = current_tier
    # rank > prior_tier rank. Substrate for the channel climb yield table,
    # strong channels section, and referral sources breakdown.
    climb_yield_by_channel: Dict[str, Dict[str, int]]
    daily_visitors: Dict[dt.date, int]
    total_visitors_seen: int
    pageviews_loaded: int
    bot_threshold: int
    bot_visitors_filtered: int
    bot_groups: List[BotGroup]
    strong_channels: List[StrongChannelBreakdown] = dataclasses.field(default_factory=list)
    climbing_breakdowns: List[ClimbingBreakdown] = dataclasses.field(default_factory=list)
    silent_brand_lovers: List[CohortMember] = dataclasses.field(default_factory=list)
    climbed_to_brand_lover: List[CohortMember] = dataclasses.field(default_factory=list)
    # PR D: climbing-headline summary and in-window backslider cohort. Both
    # are populated from the transitions matrix in compute_staircase.
    net_movement: Optional[NetMovement] = None
    at_risk_now: Optional[AtRiskNow] = None
    # PR E: per-section and per-author climb yield, computed against each
    # visitor's first prior-window pageview. Volume-floored at
    # CLIMB_YIELD_VOLUME_FLOOR (10) prior-window visitors per row.
    section_climb_yield: List[SectionClimbYield] = dataclasses.field(default_factory=list)
    author_climb_yield: List[AuthorClimbYield] = dataclasses.field(default_factory=list)
    channel_climb_yield: List[ChannelClimbYield] = dataclasses.field(default_factory=list)
    # PR F: climber content + cadence + relationship-marker lift.
    first_touch_content: List[FirstTouchContent] = dataclasses.field(default_factory=list)
    second_visit_content: Optional[SecondVisitContent] = None
    return_cadence: List[ReturnCadenceBucket] = dataclasses.field(default_factory=list)
    marker_lift: List[RelationshipMarkerLift] = dataclasses.field(default_factory=list)
    cohort_paths: Dict[str, str] = dataclasses.field(default_factory=dict)
    # relationship_markers maps marker type (newsletter_signup, account_creation,
    # login) to the set of visitor_site_ids that fired the event in the current
    # window. Consumed by PR F's marker-lift table; until then the field is
    # populated but unused by the report renderers.
    relationship_markers: Dict[str, set] = dataclasses.field(default_factory=dict)
    internal_domains: Tuple[str, ...] = ()
    coverage_note: Optional[str] = None
    network_label: Optional[str] = None
    employee_filter_key: Optional[str] = None
    employee_filter_value: Any = None
    # join_id_key names the URL query parameter the customer uses to
    # carry an opaque per-recipient (or per-send) identifier in email
    # campaign links. When set, cohort CSVs gain a column with that
    # name, populated from the query string of any pageview where the
    # parameter appears. join_id_populated is True when at least one
    # visitor in the report ended up with a non-empty captured value;
    # the Recommendations and caveats logic uses both to decide when
    # to nudge the customer to wire the pattern up vs. when to surface
    # actionable re-engagement callouts.
    join_id_key: Optional[str] = None
    join_id_populated: bool = False


def _within(ts: dt.datetime, w: Tuple[dt.datetime, dt.datetime]) -> bool:
    return w[0] <= ts <= w[1]


def _ua_signature(ev: Mapping) -> str:
    """Coarse fingerprint of a visitor's first pageview client.

    Used in combination with date and ip_city to detect single-source
    scrapers that fake new visitor_site_ids per request.
    """
    return "{}/{}/{}".format(
        ev.get("ua_browser") or "?",
        ev.get("ua_browserversion") or "?",
        ev.get("ua_os") or "?",
    )


def _page_for(ev: Mapping) -> str:
    return (
        ev.get("metadata_canonical_url")
        or ev.get("url_clean")
        or ev.get("url")
        or "(unknown)"
    )


def _url_first_segment(url: Optional[str]) -> Optional[str]:
    """Return the URL's first path segment as `/segment`, or None when there is
    none (root path or unparseable). Used as the section fallback when
    `metadata_section` is empty or 'Uncategorized'."""
    if not url:
        return None
    s = url
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    idx = s.find("/")
    if idx < 0:
        return None
    path = s[idx:]
    path = path.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    return "/" + parts[0]


def _event_slug_for_url(url: Optional[str]) -> Optional[str]:
    """Return the event slug from a URL whose first path segment is `/events`.

    `/events/<slug>/...` collapses every sub-page (`/something`, `/agenda`,
    `/speakers/...`) into one event identifier so a single conference visit
    isn't double-counted as multiple climbing-evidence rows."""
    if not url:
        return None
    s = url
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    idx = s.find("/")
    if idx < 0:
        return None
    path = s[idx:]
    path = path.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() != "events":
        return None
    return parts[1]


def _section_for(ev: Mapping) -> Tuple[str, bool]:
    """Return (section_label, is_fallback). When `metadata_section` is empty
    or 'Uncategorized', falls back to the URL first segment; the second tuple
    element flags that case so the renderer can label rows distinctly."""
    section = (ev.get("metadata_section") or "").strip()
    if section and section.lower() != "uncategorized":
        return section, False
    fallback = _url_first_segment(_page_for(ev))
    if fallback:
        return fallback, True
    return "Uncategorized", False


def _authors_for(ev: Mapping) -> List[str]:
    raw = ev.get("metadata_authors")
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out: List[str] = []
    for a in raw:
        if a is None:
            continue
        s = str(a).strip()
        if s:
            out.append(s)
    return out


def compute_strong_channel_breakdowns(
    visitors: Iterable[Tuple[str, Mapping, bool]],
    *,
    site_climb_rate: float,
    climb_ratio_threshold: float = STRONG_CHANNEL_CLIMB_RATIO,
    volume_floor: int = SMALL_VOLUME_FLOOR,
    landing_page_floor: int = STRONG_CHANNEL_LANDING_PAGE_FLOOR,
    section_floor: int = STRONG_CHANNEL_SECTION_FLOOR,
    author_floor: int = STRONG_CHANNEL_AUTHOR_FLOOR,
    top_n: int = STRONG_CHANNEL_TOP_N,
) -> List[StrongChannelBreakdown]:
    """For each prior-active visitor's first-prior-window pageview, group
    by acquisition channel and (within channel) by landing page, section,
    and author. Return one `StrongChannelBreakdown` per channel that
    clears both the climb-rate ratio threshold and the volume floor.

    `visitors` is an iterable of `(channel, first_prior_ev, climbed)` tuples
    where `climbed` is True iff the visitor's current_tier rank exceeds
    their prior_tier rank. `site_climb_rate` is the site-wide tier-rank
    climb rate (climbers ÷ prior-active visitors), used to compute each
    channel's `ratio` for the threshold check."""
    chan_totals: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"prior_visitors": 0, "climbers": 0}
    )
    chan_pages: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"visitors": 0, "climbers": 0})
    )
    chan_sections: Dict[str, Dict[Tuple[str, bool], Dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"visitors": 0, "climbers": 0})
    )
    chan_authors: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"visitors": 0, "climbers": 0})
    )

    for channel, ev, climbed in visitors:
        if channel is None or ev is None:
            continue
        chan_totals[channel]["prior_visitors"] += 1
        if climbed:
            chan_totals[channel]["climbers"] += 1
        page = _page_for(ev)
        chan_pages[channel][page]["visitors"] += 1
        if climbed:
            chan_pages[channel][page]["climbers"] += 1
        section_label, is_fallback = _section_for(ev)
        section_key = (section_label, is_fallback)
        chan_sections[channel][section_key]["visitors"] += 1
        if climbed:
            chan_sections[channel][section_key]["climbers"] += 1
        for author in _authors_for(ev):
            chan_authors[channel][author]["visitors"] += 1
            if climbed:
                chan_authors[channel][author]["climbers"] += 1

    breakdowns: List[StrongChannelBreakdown] = []
    for channel, totals in chan_totals.items():
        prior_n = totals["prior_visitors"]
        climbers = totals["climbers"]
        if prior_n < volume_floor:
            continue
        rate = climbers / prior_n if prior_n else 0.0
        ratio = rate / site_climb_rate if site_climb_rate > 0 else 0.0
        if ratio < climb_ratio_threshold:
            continue

        page_rows: List[Tuple[str, int, float]] = []
        for page, s in chan_pages[channel].items():
            if s["visitors"] < landing_page_floor:
                continue
            page_rate = s["climbers"] / s["visitors"] if s["visitors"] else 0.0
            page_rows.append((page, s["visitors"], page_rate))
        page_rows.sort(key=lambda r: (-r[2], -r[1], r[0]))

        section_rows: List[Tuple[str, int, float, bool]] = []
        for (label, is_fallback), s in chan_sections[channel].items():
            if s["visitors"] < section_floor:
                continue
            section_rate = s["climbers"] / s["visitors"] if s["visitors"] else 0.0
            section_rows.append((label, s["visitors"], section_rate, is_fallback))
        section_rows.sort(key=lambda r: (-r[2], -r[1], r[0]))

        author_rows: List[Tuple[str, int, float]] = []
        for author, s in chan_authors[channel].items():
            if s["visitors"] < author_floor:
                continue
            author_rate = s["climbers"] / s["visitors"] if s["visitors"] else 0.0
            author_rows.append((author, s["visitors"], author_rate))
        author_rows.sort(key=lambda r: (-r[2], -r[1], r[0]))

        breakdowns.append(StrongChannelBreakdown(
            channel=channel,
            visitors=prior_n,
            climbers=climbers,
            rate=rate,
            ratio=ratio,
            top_pages=page_rows[:top_n],
            top_sections=section_rows[:top_n],
            top_authors=author_rows[:top_n],
        ))

    breakdowns.sort(key=lambda b: -b.visitors)
    return breakdowns


RELATIONSHIP_MARKER_TYPES = ("newsletter_signup", "account_creation", "login")


def _cohort_member_from_event(
    vsid: str,
    ev: Optional[Mapping],
    ts: Optional[dt.datetime],
    lifetime_pageviews: int,
    *,
    prior_tier: Optional[str] = None,
    arrival_channel: Optional[str] = None,
    join_id_value: str = "",
) -> Optional[CohortMember]:
    if ev is None or ts is None:
        return None
    section_label, _ = _section_for(ev)
    return CohortMember(
        visitor_site_id=vsid,
        last_engaged_page=_page_for(ev),
        last_engaged_section=section_label,
        last_engaged_at=ts.isoformat(timespec="seconds"),
        lifetime_pageviews_in_window=lifetime_pageviews,
        prior_tier=prior_tier,
        arrival_channel=arrival_channel,
        join_id_value=join_id_value,
    )


def _build_cohort_silent_brand_lovers(
    visitor_meta: Mapping[str, Mapping[str, Any]],
) -> List[CohortMember]:
    """Visitors who were brand lovers in the prior window and went silent
    this window. Last-engaged fields come from the prior window since they
    don't have a current-window event."""
    out: List[CohortMember] = []
    for vsid, m in visitor_meta.items():
        if m["prior_tier"] == BRAND_LOVER and m["current_tier"] == SILENT:
            row = _cohort_member_from_event(
                vsid,
                m["last_prior_ev"],
                m["last_prior_ts"],
                m["lifetime_pageviews"],
                join_id_value=m.get("join_id_value", ""),
            )
            if row is not None:
                out.append(row)
    out.sort(key=lambda r: r.last_engaged_at, reverse=True)
    return out


def _build_cohort_climbed_to_brand_lover(
    visitor_meta: Mapping[str, Mapping[str, Any]],
) -> List[CohortMember]:
    """Visitors who reached brand-lover this window from a lower prior tier.
    Includes the (ONE_TIME, BRAND_LOVER) and (RETURNING, BRAND_LOVER) cells
    of the transition matrix."""
    out: List[CohortMember] = []
    for vsid, m in visitor_meta.items():
        if m["current_tier"] != BRAND_LOVER:
            continue
        if m["prior_tier"] not in (ONE_TIME, RETURNING):
            continue
        row = _cohort_member_from_event(
            vsid,
            m["last_cur_ev"],
            m["last_cur_ts"],
            m["lifetime_pageviews"],
            prior_tier=m["prior_tier"],
            join_id_value=m.get("join_id_value", ""),
        )
        if row is not None:
            out.append(row)
    out.sort(key=lambda r: r.last_engaged_at, reverse=True)
    return out


def _build_cohort_at_risk_now(
    visitor_meta: Mapping[str, Mapping[str, Any]],
) -> "AtRiskNow":
    """In-window backsliders: visitors whose tier dropped between the prior
    and current windows but who are still active (not silent). Brand lovers
    who slipped to returning; returning visitors who slipped to one-time."""
    bl_to_returning = 0
    returning_to_one_time = 0
    members: List[CohortMember] = []
    for vsid, m in visitor_meta.items():
        prior = m["prior_tier"]
        current = m["current_tier"]
        if prior == BRAND_LOVER and current == RETURNING:
            bl_to_returning += 1
        elif prior == RETURNING and current == ONE_TIME:
            returning_to_one_time += 1
        else:
            continue
        row = _cohort_member_from_event(
            vsid,
            m["last_cur_ev"],
            m["last_cur_ts"],
            m["lifetime_pageviews"],
            prior_tier=prior,
            join_id_value=m.get("join_id_value", ""),
        )
        if row is not None:
            members.append(row)
    members.sort(key=lambda r: r.last_engaged_at, reverse=True)
    return AtRiskNow(
        bl_to_returning=bl_to_returning,
        returning_to_one_time=returning_to_one_time,
        members=members,
    )


def compute_net_movement(
    transitions: Mapping[Tuple[str, str], int],
) -> NetMovement:
    """Net climbing flow over the window. See `NetMovement` for what counts
    as up vs down vs neither (silent transitions are out of scope) and how
    `eligible` / `climb_rate` are derived."""
    up = 0
    down = 0
    eligible = 0
    by_transition: Dict[Tuple[str, str], int] = {}
    for (prior, current), n in transitions.items():
        if prior == SILENT:
            # Net-new visitors (no prior-window tier) are not part of the
            # climbing cohort. They show up under "Who moved up" as net-new.
            continue
        eligible += n
        if current == SILENT:
            # Gone-silent visitors counted in eligible (they were prior-active)
            # but contribute to neither up nor down here; they surface in the
            # "Gone silent" section instead.
            continue
        prior_rank = TIER_RANK.get(prior, 0)
        current_rank = TIER_RANK.get(current, 0)
        if current_rank > prior_rank:
            up += n
            by_transition[(prior, current)] = n
        elif current_rank < prior_rank:
            down += n
            by_transition[(prior, current)] = n
    climb_rate = up / eligible if eligible else 0.0
    return NetMovement(
        up=up,
        down=down,
        net=up - down,
        by_transition=by_transition,
        eligible=eligible,
        climb_rate=climb_rate,
    )


def compute_section_climb_yield(
    visitor_meta: Mapping[str, Mapping[str, Any]],
    *,
    volume_floor: int = CLIMB_YIELD_VOLUME_FLOOR,
    top_n: int = CLIMB_YIELD_TOP_N,
) -> List[SectionClimbYield]:
    """Per-section climbing rate, computed against each visitor's first
    prior-window pageview. Numerator: visitors whose current_tier rank is
    higher than their prior_tier rank. Denominator: all prior-window
    visitors who landed on the section. Rows floored at `volume_floor`
    prior-window visitors and trimmed to `top_n` by visitor count desc."""
    denom: Dict[Tuple[str, bool], int] = defaultdict(int)
    climbers: Dict[Tuple[str, bool], int] = defaultdict(int)
    for vsid, m in visitor_meta.items():
        first_prior_ev = m.get("first_prior_ev")
        if first_prior_ev is None:
            continue
        prior_rank = TIER_RANK.get(m["prior_tier"], 0)
        current_rank = TIER_RANK.get(m["current_tier"], 0)
        if prior_rank == 0:
            continue  # No real prior tier; not part of the climb denominator.
        key = _section_for(first_prior_ev)
        denom[key] += 1
        if current_rank > prior_rank:
            climbers[key] += 1
    rows: List[SectionClimbYield] = []
    for (section, is_fallback), visitors in denom.items():
        if visitors < volume_floor:
            continue
        c = climbers.get((section, is_fallback), 0)
        rows.append(SectionClimbYield(
            section=section,
            is_fallback=is_fallback,
            visitors=visitors,
            climbers=c,
            rate=c / visitors,
        ))
    rows.sort(key=lambda r: (-r.visitors, r.section))
    return rows[:top_n]


def compute_author_climb_yield(
    visitor_meta: Mapping[str, Mapping[str, Any]],
    *,
    volume_floor: int = CLIMB_YIELD_VOLUME_FLOOR,
    top_n: int = CLIMB_YIELD_TOP_N,
) -> List[AuthorClimbYield]:
    """Per-author climbing rate. Multi-author posts contribute the visitor
    to each author's denominator (and climbers count, if applicable)."""
    denom: Dict[str, int] = defaultdict(int)
    climbers: Dict[str, int] = defaultdict(int)
    for vsid, m in visitor_meta.items():
        first_prior_ev = m.get("first_prior_ev")
        if first_prior_ev is None:
            continue
        prior_rank = TIER_RANK.get(m["prior_tier"], 0)
        current_rank = TIER_RANK.get(m["current_tier"], 0)
        if prior_rank == 0:
            continue
        authors = _authors_for(first_prior_ev)
        climbed = current_rank > prior_rank
        for author in authors:
            denom[author] += 1
            if climbed:
                climbers[author] += 1
    rows: List[AuthorClimbYield] = []
    for author, visitors in denom.items():
        if visitors < volume_floor:
            continue
        c = climbers.get(author, 0)
        rows.append(AuthorClimbYield(
            author=author,
            visitors=visitors,
            climbers=c,
            rate=c / visitors,
        ))
    rows.sort(key=lambda r: (-r.visitors, r.author))
    return rows[:top_n]


def compute_channel_climb_yield(
    visitor_meta: Mapping[str, Mapping[str, Any]],
    *,
    network_label: Optional[str] = None,
    network_sources: Tuple[str, ...] = (),
    volume_floor: int = CLIMB_YIELD_VOLUME_FLOOR,
    top_n: int = CLIMB_YIELD_TOP_N,
) -> List[ChannelClimbYield]:
    """Per-channel climbing rate, keyed on the acquisition channel of each
    visitor's first prior-window pageview. Numerator: visitors whose
    current_tier rank is higher than their prior_tier rank. Denominator:
    prior-window visitors attributed to the channel. Visitors whose first
    prior pageview maps to no channel (internal continuations, cross-
    subdomain navigation) are dropped, matching the channel-mix denominator
    everywhere else in the report."""
    denom: Dict[str, int] = defaultdict(int)
    climbers: Dict[str, int] = defaultdict(int)
    for vsid, m in visitor_meta.items():
        first_prior_ev = m.get("first_prior_ev")
        if first_prior_ev is None:
            continue
        prior_rank = TIER_RANK.get(m["prior_tier"], 0)
        current_rank = TIER_RANK.get(m["current_tier"], 0)
        if prior_rank == 0:
            continue
        channel = acquisition_channel(
            first_prior_ev,
            network_label=network_label,
            network_sources=network_sources,
        )
        if channel is None:
            continue
        denom[channel] += 1
        if current_rank > prior_rank:
            climbers[channel] += 1
    rows: List[ChannelClimbYield] = []
    for channel, visitors in denom.items():
        if visitors < volume_floor:
            continue
        c = climbers.get(channel, 0)
        rows.append(ChannelClimbYield(
            channel=channel,
            visitors=visitors,
            climbers=c,
            rate=c / visitors,
        ))
    rows.sort(key=lambda r: (-r.visitors, r.channel))
    return rows[:top_n]


# Per-row floor for first-touch / second-visit content tables. Same shape
# as the climb-yield floors above; PR F's tables sit at the visitor level
# so the floors keep noise out of the recommendations.
FIRST_TOUCH_FLOOR = 5
FIRST_TOUCH_TOP_N = 10
SECOND_VISIT_FLOOR = 5
SECOND_VISIT_TOP_N = 10


# Return-cadence buckets in days. Tuples are (label, lower_inclusive_days,
# upper_inclusive_days). Last bucket has upper=None for unbounded.
RETURN_CADENCE_BUCKETS: Tuple[Tuple[str, int, Optional[int]], ...] = (
    ("0-2 days", 0, 2),
    ("3-7 days", 3, 7),
    ("8-30 days", 8, 30),
    ("30+ days", 31, None),
)


def compute_first_touch_content(
    visitor_meta: Mapping[str, Mapping[str, Any]],
    *,
    network_label: Optional[str] = None,
    network_sources: Tuple[str, ...] = (),
    floor: int = FIRST_TOUCH_FLOOR,
    top_n: int = FIRST_TOUCH_TOP_N,
) -> List[FirstTouchContent]:
    """For each climbing-transition group, aggregate climbers' `first_ev`
    (earliest pageview in cached DPL data) by page, section, and channel.

    The transition groups mirror CLIMBING_TRANSITION_GROUPS so the climber
    first-touch table aligns row-for-row with `compute_climbing_breakdowns`."""
    out: List[FirstTouchContent] = []
    for key, header, transitions in CLIMBING_TRANSITION_GROUPS:
        transition_set = set(transitions)
        page_counter: Counter = Counter()
        section_counter: Counter = Counter()
        channel_counter: Counter = Counter()
        climbers = 0
        for vsid, m in visitor_meta.items():
            if (m["prior_tier"], m["current_tier"]) not in transition_set:
                continue
            first_ev = m.get("first_ev")
            if first_ev is None:
                continue
            climbers += 1
            page_counter[_page_for(first_ev)] += 1
            section_counter[_section_for(first_ev)] += 1
            chan = acquisition_channel(
                first_ev,
                network_label=network_label,
                network_sources=network_sources,
            )
            if chan is not None:
                channel_counter[chan] += 1
        page_rows = [
            (p, n) for p, n in page_counter.most_common() if n >= floor
        ][:top_n]
        section_rows = [
            (label, n, is_fallback)
            for (label, is_fallback), n in section_counter.most_common()
            if n >= floor
        ][:top_n]
        channel_rows = [
            (c, n) for c, n in channel_counter.most_common() if n > 0
        ]
        if climbers == 0:
            continue
        out.append(FirstTouchContent(
            transition_key=key,
            header=header,
            climbers=climbers,
            by_page=page_rows,
            by_section=section_rows,
            by_channel=channel_rows,
        ))
    return out


def compute_second_visit_content(
    visitor_meta: Mapping[str, Mapping[str, Any]],
    *,
    session_gap: dt.timedelta = SESSION_GAP,
    floor: int = SECOND_VISIT_FLOOR,
    top_n: int = SECOND_VISIT_TOP_N,
) -> SecondVisitContent:
    """The page read on the second visit by one-time -> returning climbers.
    Requires the `current_ts` and `current_pages` lists on visitor_meta.

    Skips climbers whose current_pages list is empty or has fewer events
    than the second session would need."""
    page_counter: Counter = Counter()
    for vsid, m in visitor_meta.items():
        if m["prior_tier"] != ONE_TIME or m["current_tier"] not in (RETURNING, BRAND_LOVER):
            continue
        ts_list = m.get("current_ts")
        pv_list = m.get("current_pages")
        if not ts_list or not pv_list or len(pv_list) != len(ts_list):
            continue
        starts = session_starts(ts_list, session_gap)
        if len(starts) < 2:
            continue  # Visitor has only one session in current window.
        second_visit_idx = starts[1]
        second_visit_ev = pv_list[second_visit_idx]
        page_counter[_page_for(second_visit_ev)] += 1
    rows = [
        (page, n) for page, n in page_counter.most_common() if n >= floor
    ][:top_n]
    return SecondVisitContent(rows=rows)


def compute_return_cadence(
    visitor_meta: Mapping[str, Mapping[str, Any]],
    *,
    session_gap: dt.timedelta = SESSION_GAP,
) -> List[ReturnCadenceBucket]:
    """For visitors with >=2 sessions in the current window, the time
    between session 1 (first event) and session 2 (first event). Bucket
    by `RETURN_CADENCE_BUCKETS`; per-bucket compute climbers /
    total-with-second-visit."""
    bucket_total: Dict[str, int] = {b[0]: 0 for b in RETURN_CADENCE_BUCKETS}
    bucket_climbers: Dict[str, int] = {b[0]: 0 for b in RETURN_CADENCE_BUCKETS}
    for vsid, m in visitor_meta.items():
        ts_list = m.get("current_ts")
        if not ts_list:
            continue
        starts = session_starts(ts_list, session_gap)
        if len(starts) < 2:
            continue
        delta = ts_list[starts[1]] - ts_list[starts[0]]
        delta_days = delta.total_seconds() / 86400.0
        prior_rank = TIER_RANK.get(m["prior_tier"], 0)
        current_rank = TIER_RANK.get(m["current_tier"], 0)
        climbed = current_rank > prior_rank
        for label, lo, hi in RETURN_CADENCE_BUCKETS:
            if delta_days >= lo and (hi is None or delta_days <= hi):
                bucket_total[label] += 1
                if climbed:
                    bucket_climbers[label] += 1
                break
    out: List[ReturnCadenceBucket] = []
    for label, _lo, _hi in RETURN_CADENCE_BUCKETS:
        total = bucket_total[label]
        climbers = bucket_climbers[label]
        rate = climbers / total if total else 0.0
        out.append(ReturnCadenceBucket(
            label=label,
            visitors_with_second_visit=total,
            climbers=climbers,
            rate=rate,
        ))
    return out


def compute_marker_lift(
    visitor_meta: Mapping[str, Mapping[str, Any]],
    relationship_markers: Mapping[str, set],
) -> List[RelationshipMarkerLift]:
    """For each marker type, compare climb rate of visitors who fired the
    event vs visitors who did not. A lift_ratio > 1 means the marker is
    associated with higher climbing odds. Used to test Bob's hypothesis
    that newsletter / account / login events are climbing levers."""
    out: List[RelationshipMarkerLift] = []
    # Pool the universe of real-prior-tier visitors. Visitors who didn't
    # have a prior tier (SILENT) can't "climb" in the tier-rank sense, so
    # the lift test excludes them.
    real_prior_vsids = {
        vsid for vsid, m in visitor_meta.items()
        if TIER_RANK.get(m["prior_tier"], 0) > 0
    }
    climber_vsids = {
        vsid for vsid in real_prior_vsids
        if TIER_RANK.get(visitor_meta[vsid]["current_tier"], 0)
        > TIER_RANK.get(visitor_meta[vsid]["prior_tier"], 0)
    }
    for marker in RELATIONSHIP_MARKER_TYPES:
        fired = set(relationship_markers.get(marker, set())) & real_prior_vsids
        unfired = real_prior_vsids - fired
        fired_climbers = fired & climber_vsids
        unfired_climbers = unfired & climber_vsids
        fired_rate = len(fired_climbers) / len(fired) if fired else 0.0
        unfired_rate = (
            len(unfired_climbers) / len(unfired) if unfired else 0.0
        )
        lift = fired_rate / unfired_rate if unfired_rate > 0 else None
        out.append(RelationshipMarkerLift(
            marker=marker,
            fired_count=len(fired),
            fired_climbers=len(fired_climbers),
            fired_climb_rate=fired_rate,
            unfired_count=len(unfired),
            unfired_climbers=len(unfired_climbers),
            unfired_climb_rate=unfired_rate,
            lift_ratio=lift,
        ))
    return out


def _cohort_columns(
    name: str,
    *,
    join_id_key: Optional[str] = None,
) -> List[str]:
    base = [
        "visitor_site_id",
        "last_engaged_page",
        "last_engaged_section",
        "last_engaged_at",
        "lifetime_pageviews_in_window",
    ]
    if name == "climbed-to-brand-lover" or name == "at-risk-now":
        base.append("prior_tier")
    if join_id_key:
        base.append(join_id_key)
    return base


def write_cohort_csvs(
    result: "StaircaseResult",
    *,
    cohort_dir: str,
    slug: str,
) -> Dict[str, str]:
    """Write the cohort CSVs as siblings of the report files.

    Skip cohorts with zero members. Returns a mapping of cohort name to the
    written file path so callers (callouts, dev wrappers) can link them."""
    written: Dict[str, str] = {}
    cohorts: Dict[str, List[CohortMember]] = {
        "silent-brand-lovers": result.silent_brand_lovers,
        "climbed-to-brand-lover": result.climbed_to_brand_lover,
        "at-risk-now": result.at_risk_now.members if result.at_risk_now else [],
    }
    os.makedirs(cohort_dir, exist_ok=True)
    join_key = result.join_id_key
    for name, rows in cohorts.items():
        if not rows:
            continue
        path = os.path.join(cohort_dir, f"staircase-{slug}-cohort-{name}.csv")
        cols = _cohort_columns(name, join_id_key=join_key)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(cols)
            for row in rows:
                writer.writerow([
                    row.join_id_value if join_key and c == join_key
                    else getattr(row, c)
                    for c in cols
                ])
        written[name] = path
    result.cohort_paths = dict(written)
    return written


CLIMBING_TRANSITION_GROUPS: Tuple[Tuple[str, str, Tuple[Tuple[str, str], ...]], ...] = (
    (
        "one_time_up",
        "Climbed from one-time to returning or brand lover",
        ((ONE_TIME, RETURNING), (ONE_TIME, BRAND_LOVER)),
    ),
    (
        "returning_to_bl",
        "Climbed from returning to brand lover",
        ((RETURNING, BRAND_LOVER),),
    ),
)


def compute_climbing_breakdowns(
    climbers_by_group: Mapping[str, List[Mapping]],
    prior_channel_totals: Mapping[str, int],
    *,
    network_label: Optional[str] = None,
    network_sources: Tuple[str, ...] = (),
    channel_floor: int = CLIMBING_CHANNEL_FLOOR,
    page_floor: int = CLIMBING_PAGE_FLOOR,
    section_floor: int = CLIMBING_SECTION_FLOOR,
    event_floor: int = CLIMBING_EVENT_FLOOR,
    channel_top_n: int = CLIMBING_CHANNEL_TOP_N,
    page_top_n: int = CLIMBING_PAGE_TOP_N,
    section_top_n: int = CLIMBING_SECTION_TOP_N,
    event_top_n: int = CLIMBING_EVENT_TOP_N,
) -> List[ClimbingBreakdown]:
    """Build one ClimbingBreakdown per transition group.

    `climbers_by_group` maps group key → list of climber bucket dicts (each
    carries `first_prior_ev` and `current_pages`). `prior_channel_totals`
    maps channel → total prior-window visitor count, used as the climbing-
    yield denominator."""
    out: List[ClimbingBreakdown] = []
    for key, header, _ in CLIMBING_TRANSITION_GROUPS:
        climbers = climbers_by_group.get(key, [])
        n = len(climbers)
        if n == 0:
            continue

        chan_counts: Counter = Counter()
        for b in climbers:
            ev = b.get("first_prior_ev")
            if ev is None:
                continue
            chan = acquisition_channel(
                ev,
                network_label=network_label,
                network_sources=network_sources,
            )
            if chan is None:
                continue
            chan_counts[chan] += 1
        chan_rows: List[Tuple[str, int, int, float]] = []
        for chan, c in chan_counts.items():
            if c < channel_floor:
                continue
            denom = prior_channel_totals.get(chan, 0)
            yield_rate = (c / denom) if denom > 0 else 0.0
            chan_rows.append((chan, c, denom, yield_rate))
        chan_rows.sort(key=lambda r: (-r[1], r[0]))
        chan_rows = chan_rows[:channel_top_n]

        page_visitors: Dict[str, int] = defaultdict(int)
        section_visitors: Dict[Tuple[str, bool], int] = defaultdict(int)
        event_visitors: Dict[str, int] = defaultdict(int)
        for b in climbers:
            seen_pages: set = set()
            seen_sections: set = set()
            seen_events: set = set()
            for ev in b.get("current_pages", []):
                page = _page_for(ev)
                if page not in seen_pages:
                    seen_pages.add(page)
                    page_visitors[page] += 1
                section_label, is_fallback = _section_for(ev)
                section_key = (section_label, is_fallback)
                if section_key not in seen_sections:
                    seen_sections.add(section_key)
                    section_visitors[section_key] += 1
                event_slug = _event_slug_for_url(page)
                if event_slug and event_slug not in seen_events:
                    seen_events.add(event_slug)
                    event_visitors[event_slug] += 1

        page_rows: List[Tuple[str, int]] = [
            (p, c) for p, c in page_visitors.items() if c >= page_floor
        ]
        page_rows.sort(key=lambda r: (-r[1], r[0]))
        page_rows = page_rows[:page_top_n]

        section_rows: List[Tuple[str, int, bool]] = []
        for (label, is_fallback), c in section_visitors.items():
            if c < section_floor:
                continue
            section_rows.append((label, c, is_fallback))
        section_rows.sort(key=lambda r: (-r[1], r[0]))
        section_rows = section_rows[:section_top_n]

        event_rows: List[Tuple[str, int]] = [
            (slug, c) for slug, c in event_visitors.items() if c >= event_floor
        ]
        event_rows.sort(key=lambda r: (-r[1], r[0]))
        event_rows = event_rows[:event_top_n]

        out.append(ClimbingBreakdown(
            key=key,
            header=header,
            visitors=n,
            top_prior_channels=chan_rows,
            top_pages=page_rows,
            top_sections=section_rows,
            top_events=event_rows,
        ))
    return out


def compute_staircase(
    *,
    files: Sequence[str],
    current_window: Tuple[dt.datetime, dt.datetime],
    prior_window: Tuple[dt.datetime, dt.datetime],
    site_label: str,
    site_id_filter: Optional[str] = None,
    session_gap: dt.timedelta = SESSION_GAP,
    bot_threshold: int = DEFAULT_BOT_THRESHOLD,
    internal_domains: Iterable[str] = (),
    network_label: Optional[str] = None,
    network_sources: Tuple[str, ...] = (),
    employee_filter_key: Optional[str] = None,
    employee_filter_value: Any = None,
    join_id_key: Optional[str] = None,
) -> StaircaseResult:
    by_visitor: Dict[str, Dict] = {}
    pv_total = 0

    # Daily UV (pageviews only, both windows). Stores all visitors; we'll
    # filter bots at aggregation time so daily_visitors reflects real audience.
    daily_uv_sets: Dict[dt.date, set] = defaultdict(set)

    # Relationship-marker tap: which visitors fired newsletter_signup,
    # account_creation, or login events in the current window. Bob's
    # Relationship Intelligence framing treats these as climbing levers,
    # tested against tier transitions in PR F's marker-lift table. Revenue
    # conversions are NOT tracked here; they moved to the conversions report
    # in PR B. See `RELATIONSHIP_MARKER_TYPES` near the top of this file.
    relationship_markers: Dict[str, set] = {m: set() for m in RELATIONSHIP_MARKER_TYPES}

    for action, ts, ev in iter_events(
        files,
        site_id_filter=site_id_filter,
        employee_filter_key=employee_filter_key,
        employee_filter_value=employee_filter_value,
    ):
        in_cur = _within(ts, current_window)
        in_prior = _within(ts, prior_window)
        if not (in_cur or in_prior):
            continue

        if action == "pageview":
            pv_total += 1
            vsid = ev.get("visitor_site_id")
            if not vsid:
                continue
            bucket = by_visitor.setdefault(
                vsid,
                {
                    "current_ts": [],
                    "prior_ts": [],
                    "first_cur_ts": None,
                    "first_cur_ev": None,
                    "first_prior_ts": None,
                    "first_prior_ev": None,
                    "first_ts": None,
                    "first_ev": None,
                    "last_cur_ts": None,
                    "last_cur_ev": None,
                    "last_prior_ts": None,
                    "last_prior_ev": None,
                    "current_pages": [],
                    "join_id_value": "",
                },
            )
            if bucket["first_ts"] is None or ts < bucket["first_ts"]:
                bucket["first_ts"] = ts
                bucket["first_ev"] = ev
            if join_id_key:
                # The customer mints an opaque per-recipient (or per-send)
                # ID and embeds it in email URLs (e.g. `?pid=...`). The DPL
                # `url` field carries the query string verbatim. We capture
                # any non-empty value we see for this visitor; last-write
                # wins, which surfaces the most recent campaign for a
                # given recipient.
                raw_url = ev.get("url") or ""
                if "?" in raw_url:
                    try:
                        qs = urlsplit(raw_url).query
                    except ValueError:
                        qs = ""
                    if qs:
                        captured = parse_qs(qs).get(join_id_key)
                        if captured:
                            val = captured[-1]
                            if val:
                                bucket["join_id_value"] = val
            if in_cur:
                bucket["current_ts"].append(ts)
                bucket["current_pages"].append(ev)
                if bucket["first_cur_ts"] is None or ts < bucket["first_cur_ts"]:
                    bucket["first_cur_ts"] = ts
                    bucket["first_cur_ev"] = ev
                if bucket["last_cur_ts"] is None or ts > bucket["last_cur_ts"]:
                    bucket["last_cur_ts"] = ts
                    bucket["last_cur_ev"] = ev
            else:
                bucket["prior_ts"].append(ts)
                if bucket["last_prior_ts"] is None or ts > bucket["last_prior_ts"]:
                    bucket["last_prior_ts"] = ts
                    bucket["last_prior_ev"] = ev
                if bucket["first_prior_ts"] is None or ts < bucket["first_prior_ts"]:
                    bucket["first_prior_ts"] = ts
                    bucket["first_prior_ev"] = ev
            daily_uv_sets[ts.date()].add(vsid)

        elif action == "conversion":
            # Narrow relationship-marker tap. Revenue conversions are NOT
            # consumed by the staircase report; they live in the conversions
            # report. We only read the three event types that Bob's
            # Relationship Intelligence framing names as climbing levers, and
            # only for the current window (the marker-lift test compares
            # within the current-window cohort).
            if not in_cur:
                continue
            ed = ev.get("extra_data") or {}
            ctype = ed.get("_conversion_type") if isinstance(ed, dict) else None
            if ctype not in RELATIONSHIP_MARKER_TYPES:
                continue
            vsid = ev.get("visitor_site_id")
            if not vsid:
                continue
            relationship_markers[ctype].add(vsid)

    # Bot detection: group visitors by (first_pv_date, ip_city, ua_signature).
    # Tuples with > bot_threshold distinct visitors are treated as a single
    # scraper run and all their visitors are excluded from downstream metrics.
    bot_visitors: set = set()
    bot_groups_list: List[BotGroup] = []
    if bot_threshold > 0:
        sig_to_vsids: Dict[Tuple[dt.date, str, str], List[str]] = defaultdict(list)
        for vsid, b in by_visitor.items():
            if b["first_ev"] is None:
                continue
            sig = (
                b["first_ts"].date(),
                b["first_ev"].get("ip_city") or "(none)",
                _ua_signature(b["first_ev"]),
            )
            sig_to_vsids[sig].append(vsid)
        for sig, vsids in sig_to_vsids.items():
            if len(vsids) > bot_threshold:
                bot_visitors.update(vsids)
                bot_groups_list.append(BotGroup(
                    first_pv_date=sig[0],
                    ip_city=sig[1],
                    ua_signature=sig[2],
                    visitor_count=len(vsids),
                ))
        bot_groups_list.sort(key=lambda g: -g.visitor_count)

    # Classify (non-bot) visitors into tiers per window.
    tier_counts_current: Counter = Counter()
    tier_counts_prior: Counter = Counter()
    transitions: Counter = Counter()
    # Per-channel climb yield computed on prior-window arrivals: of visitors
    # who arrived via this channel in the prior window, how many climbed
    # a tier rank into the current window. Same shape as the section /
    # author climb-yield tables; used as the substrate for the channel
    # climb yield table, the strong-channels section, and the top
    # referral sources breakdown.
    climb_yield: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"prior_visitors": 0, "climbers": 0}
    )
    prior_visitor_arrivals: List[Tuple[str, Mapping, bool]] = []
    prior_channel_totals: Counter = Counter()
    transition_to_group: Dict[Tuple[str, str], str] = {}
    for grp_key, _hdr, trs in CLIMBING_TRANSITION_GROUPS:
        for tr in trs:
            transition_to_group[tr] = grp_key
    climbers_by_group: Dict[str, List[Dict]] = defaultdict(list)

    # Per-visitor enrichment kept around for cohort assembly after the
    # strong-channel set has been resolved.
    visitor_meta: Dict[str, Dict[str, Any]] = {}

    for vsid, b in by_visitor.items():
        if vsid in bot_visitors:
            continue
        b["current_ts"].sort()
        b["prior_ts"].sort()
        sc = session_count(b["current_ts"], session_gap)
        sp = session_count(b["prior_ts"], session_gap)
        ct = tier_for(sc)
        pt = tier_for(sp)
        if ct != SILENT:
            tier_counts_current[ct] += 1
        if pt != SILENT:
            tier_counts_prior[pt] += 1
        transitions[(pt, ct)] += 1
        chan: Optional[str] = None
        if ct != SILENT and b["first_cur_ev"] is not None:
            chan = acquisition_channel(
                b["first_cur_ev"],
                network_label=network_label,
                network_sources=network_sources,
            )
        visitor_meta[vsid] = {
            "current_tier": ct,
            "prior_tier": pt,
            "channel": chan,
            "last_cur_ev": b["last_cur_ev"],
            "last_cur_ts": b["last_cur_ts"],
            "last_prior_ev": b["last_prior_ev"],
            "last_prior_ts": b["last_prior_ts"],
            "first_prior_ev": b.get("first_prior_ev"),
            # PR F: first_ev (earliest pageview in cached data) for
            # first-touch; current_ts + current_pages for session-splitting
            # used by second-visit and return-cadence.
            "first_ev": b.get("first_ev"),
            "current_ts": list(b["current_ts"]),
            "current_pages": list(b.get("current_pages", [])),
            "lifetime_pageviews": len(b["current_ts"]) + len(b["prior_ts"]),
            "join_id_value": b.get("join_id_value", ""),
        }
        if b["first_prior_ev"] is not None and pt != SILENT:
            prior_chan = acquisition_channel(
                b["first_prior_ev"],
                network_label=network_label,
                network_sources=network_sources,
            )
            if prior_chan is not None:
                prior_channel_totals[prior_chan] += 1
                climbed_flag = (
                    TIER_RANK.get(ct, 0) > TIER_RANK.get(pt, 0)
                )
                climb_yield[prior_chan]["prior_visitors"] += 1
                if climbed_flag:
                    climb_yield[prior_chan]["climbers"] += 1
                prior_visitor_arrivals.append(
                    (prior_chan, b["first_prior_ev"], climbed_flag),
                )
        grp_key = transition_to_group.get((pt, ct))
        if grp_key is not None:
            climbers_by_group[grp_key].append(b)

    # Daily UVs excluding bot visitors – chart should reflect real audience.
    daily_visitors = {
        d: len(s - bot_visitors) for d, s in daily_uv_sets.items()
    }

    net_movement = compute_net_movement(transitions)
    site_climb_rate = net_movement.climb_rate
    strong_channels = compute_strong_channel_breakdowns(
        prior_visitor_arrivals,
        site_climb_rate=site_climb_rate,
    )

    silent_brand_lovers = _build_cohort_silent_brand_lovers(visitor_meta)
    climbed = _build_cohort_climbed_to_brand_lover(visitor_meta)
    at_risk_now = _build_cohort_at_risk_now(visitor_meta)
    section_climb_yield = compute_section_climb_yield(visitor_meta)
    author_climb_yield = compute_author_climb_yield(visitor_meta)
    channel_climb_yield = compute_channel_climb_yield(
        visitor_meta,
        network_label=network_label,
        network_sources=network_sources,
    )
    first_touch_content = compute_first_touch_content(
        visitor_meta,
        network_label=network_label,
        network_sources=network_sources,
    )
    second_visit_content = compute_second_visit_content(visitor_meta, session_gap=session_gap)
    return_cadence = compute_return_cadence(visitor_meta, session_gap=session_gap)
    marker_lift = compute_marker_lift(visitor_meta, relationship_markers)

    climbing_breakdowns = compute_climbing_breakdowns(
        climbers_by_group,
        prior_channel_totals,
        network_label=network_label,
        network_sources=network_sources,
    )

    join_id_populated = bool(join_id_key) and any(
        m.get("join_id_value") for m in visitor_meta.values()
    )

    return StaircaseResult(
        site_label=site_label,
        current_window=(current_window[0].date(), current_window[1].date()),
        prior_window=(prior_window[0].date(), prior_window[1].date()),
        site_id_filter=site_id_filter,
        tier_counts_current=dict(tier_counts_current),
        tier_counts_prior=dict(tier_counts_prior),
        transitions=dict(transitions),
        climb_yield_by_channel={k: dict(v) for k, v in climb_yield.items()},
        daily_visitors=daily_visitors,
        total_visitors_seen=len(by_visitor),
        pageviews_loaded=pv_total,
        bot_threshold=bot_threshold,
        bot_visitors_filtered=len(bot_visitors),
        bot_groups=bot_groups_list,
        strong_channels=strong_channels,
        climbing_breakdowns=climbing_breakdowns,
        silent_brand_lovers=silent_brand_lovers,
        climbed_to_brand_lover=climbed,
        net_movement=net_movement,
        at_risk_now=at_risk_now,
        section_climb_yield=section_climb_yield,
        author_climb_yield=author_climb_yield,
        channel_climb_yield=channel_climb_yield,
        first_touch_content=first_touch_content,
        second_visit_content=second_visit_content,
        return_cadence=return_cadence,
        marker_lift=marker_lift,
        relationship_markers=relationship_markers,
        internal_domains=tuple(d for d in internal_domains if d),
        network_label=network_label,
        employee_filter_key=employee_filter_key,
        employee_filter_value=employee_filter_value,
        join_id_key=join_id_key,
        join_id_populated=join_id_populated,
    )


# --------------------------------------------------------------------------
# Render: markdown
# --------------------------------------------------------------------------


def _fmt_delta(curr: int, prev: int) -> str:
    if prev == 0 and curr == 0:
        return "–"
    if prev == 0:
        return "new"
    pct = (curr - prev) / prev * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}%"


def _fmt_window(w: Tuple[dt.date, dt.date]) -> str:
    a, b = w
    if a.year == b.year:
        return f"{a.strftime('%b %-d')} – {b.strftime('%b %-d, %Y')}"
    return f"{a.strftime('%b %-d, %Y')} – {b.strftime('%b %-d, %Y')}"


def _short_page(url: str, max_len: int = 60) -> str:
    """Strip scheme+host for compactness, leaving the path."""
    s = url
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if "/" in s:
        s = s[s.index("/"):]
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s or url


def _baseline_climb_rate(
    climb_yield: Mapping[str, Mapping[str, int]],
) -> Tuple[int, float]:
    """Site-wide climb rate across all real acquisition channels:
    sum(climbers) / sum(prior_visitors). Used as the baseline for the
    "vs site avg" ratios in the strong-channels and referral-sources
    sections, alongside the top-line `net_movement.climb_rate` figure."""
    total_prior = sum(d["prior_visitors"] for d in climb_yield.values())
    total_climbers = sum(d["climbers"] for d in climb_yield.values())
    rate = total_climbers / total_prior if total_prior else 0
    return total_prior, rate


def _top_referral_sources(
    climb_yield: Mapping[str, Mapping[str, int]],
    *,
    min_visitors: int = 10,
    top_n: int = 25,
) -> List[Tuple[str, int, int, float]]:
    """Per-domain referral rows (domain, prior_visitors, climbers, rate),
    sorted by climb rate DESC. Surfaces the long tail of
    `Referral: <domain>` entries that the channel-mix table rolls up."""
    rows: List[Tuple[str, int, int, float]] = []
    for chan, d in climb_yield.items():
        if not chan.startswith("Referral: "):
            continue
        if d["prior_visitors"] < min_visitors:
            continue
        domain = chan[len("Referral: "):]
        rate = d["climbers"] / d["prior_visitors"] if d["prior_visitors"] else 0
        rows.append((domain, d["prior_visitors"], d["climbers"], rate))
    rows.sort(key=lambda r: -r[3])
    return rows[:top_n]


def _render_strong_channels_markdown(
    strong_channels: List[StrongChannelBreakdown],
    site_climb_rate: float,
) -> List[str]:
    lines: List[str] = []
    lines.append("## Top levers to grow relationships")
    lines.append("")
    lines.append(
        "_For each channel whose prior-window arrivals climbed at "
        f"≥ {STRONG_CHANNEL_CLIMB_RATIO:.1f}× the site climb rate (with "
        f"≥ {SMALL_VOLUME_FLOOR} prior visitors), the landing pages, "
        "sections, and authors doing the climbing work. Climb rate = of "
        "prior-window visitors who arrived via this channel and landed "
        "here, the fraction whose tier rank rose in the current window._"
    )
    lines.append("")
    for b in strong_channels:
        lines.append(
            f"### {b.channel} ({b.visitors:,} prior visitors, "
            f"{b.rate:.1%} climb rate, {b.ratio:.1f}× site avg)"
        )
        lines.append("")
        any_rendered = False
        if b.top_pages:
            any_rendered = True
            lines.append(
                f"**Top landing pages** (≥ {STRONG_CHANNEL_LANDING_PAGE_FLOOR} "
                "prior visitors, by climb rate):"
            )
            lines.append("")
            lines.append("| Page | Prior visitors | Climb rate | vs site avg |")
            lines.append("|---|---:|---:|---:|")
            for page, visitors, rate in b.top_pages:
                ratio = rate / site_climb_rate if site_climb_rate > 0 else 0
                lines.append(
                    f"| {_short_page(page)} | {visitors:,} | {rate:.1%} | "
                    f"{ratio:.1f}× |"
                )
            lines.append("")
        if b.top_sections:
            any_rendered = True
            lines.append(
                f"**Top sections** (≥ {STRONG_CHANNEL_SECTION_FLOOR} "
                "prior visitors, by climb rate):"
            )
            lines.append("")
            lines.append("| Section | Prior visitors | Climb rate | vs site avg |")
            lines.append("|---|---:|---:|---:|")
            for label, visitors, rate, is_fallback in b.top_sections:
                ratio = rate / site_climb_rate if site_climb_rate > 0 else 0
                display = f"(by URL: {label})" if is_fallback else label
                lines.append(
                    f"| {display} | {visitors:,} | {rate:.1%} | {ratio:.1f}× |"
                )
            lines.append("")
        if b.top_authors:
            any_rendered = True
            lines.append(
                f"**Top authors** (≥ {STRONG_CHANNEL_AUTHOR_FLOOR} "
                "prior visitors, by climb rate):"
            )
            lines.append("")
            lines.append("| Author | Prior visitors | Climb rate | vs site avg |")
            lines.append("|---|---:|---:|---:|")
            for author, visitors, rate in b.top_authors:
                ratio = rate / site_climb_rate if site_climb_rate > 0 else 0
                lines.append(
                    f"| {author} | {visitors:,} | {rate:.1%} | {ratio:.1f}× |"
                )
            lines.append("")
        if not any_rendered:
            lines.append(
                "_No landing pages, sections, or authors cleared the per-row "
                "volume floor in this channel._"
            )
            lines.append("")
    return lines


def _render_climbing_markdown(
    breakdowns: List[ClimbingBreakdown],
) -> List[str]:
    lines: List[str] = []
    lines.append("## What pulls visitors up your staircase")
    lines.append("")
    lines.append(
        "_For each climbing transition, the prior-window channels that recruited "
        "the climbers, the current-window pages and sections they consumed, and "
        "the events they attended. Tells you what content and events to "
        "commission more of to keep visitors climbing._"
    )
    lines.append("")
    for b in breakdowns:
        lines.append(f"### {b.header} ({b.visitors:,} visitors)")
        lines.append("")
        if b.top_prior_channels:
            lines.append(
                "**Prior-window acquisition channel** "
                f"(≥ {CLIMBING_CHANNEL_FLOOR} climbers, top {CLIMBING_CHANNEL_TOP_N} by count):"
            )
            lines.append("")
            lines.append(
                "| Channel | Climbers | Prior visitors | Climbing yield |"
            )
            lines.append("|---|---:|---:|---:|")
            for chan, climbers, denom, yield_rate in b.top_prior_channels:
                denom_str = f"{denom:,}" if denom > 0 else "–"
                yield_str = f"{yield_rate:.1%}" if denom > 0 else "–"
                lines.append(
                    f"| {chan} | {climbers:,} | {denom_str} | {yield_str} |"
                )
            lines.append("")
        if b.top_pages:
            lines.append(
                "**Current-window content consumed** "
                f"(≥ {CLIMBING_PAGE_FLOOR} climbers, top {CLIMBING_PAGE_TOP_N} by count):"
            )
            lines.append("")
            lines.append("| Page | Climbers |")
            lines.append("|---|---:|")
            for page, c in b.top_pages:
                lines.append(f"| {_short_page(page)} | {c:,} |")
            lines.append("")
        if b.top_sections:
            lines.append(
                "**Current-window sections consumed** "
                f"(≥ {CLIMBING_SECTION_FLOOR} climbers, top {CLIMBING_SECTION_TOP_N} by count):"
            )
            lines.append("")
            lines.append("| Section | Climbers |")
            lines.append("|---|---:|")
            for label, c, is_fallback in b.top_sections:
                display = f"(by URL: {label})" if is_fallback else label
                lines.append(f"| {display} | {c:,} |")
            lines.append("")
        if b.top_events:
            lines.append(
                "**Events climbers attended** "
                f"(≥ {CLIMBING_EVENT_FLOOR} climbers, top {CLIMBING_EVENT_TOP_N} by count):"
            )
            lines.append("")
            lines.append("| Event | Climbers |")
            lines.append("|---|---:|")
            for slug, c in b.top_events:
                lines.append(f"| /events/{slug}/ | {c:,} |")
            lines.append("")
    return lines


def _file_uri(path: str) -> str:
    """Best-effort file:// URI. Relative paths stay relative."""
    if os.path.isabs(path):
        return "file://" + path
    return path


_COHORT_LINK_LABELS: Mapping[str, str] = {
    "silent-brand-lovers": "Silent Brand-Lovers Cohort CSV",
    "climbed-to-brand-lover": "Climbed-to-Brand-Lover Cohort CSV",
    "at-risk-now": "At-Risk Cohort CSV",
}


def _cohort_link_label(name: str) -> str:
    """Human-readable label for a cohort CSV link. Falls back to a
    title-cased version of the slug for cohorts not in the explicit table,
    so new cohorts still render with a sensible name."""
    if name in _COHORT_LINK_LABELS:
        return _COHORT_LINK_LABELS[name]
    return name.replace("-", " ").title() + " Cohort CSV"


def _cohort_link_phrase(name: str, count: int, path: Optional[str]) -> str:
    """Trailing sentence linking a cohort CSV from a callout. Emits a real
    markdown link so the .md output is clickable in any markdown viewer
    and the HTML output (via `_md_inline_to_html`) gets a real <a> tag.
    Falls back to 'cohort: N visitors' (no download) when the CSV path
    isn't available yet."""
    if path:
        label = _cohort_link_label(name)
        return f" Cohort: {count:,} visitors. [{label}]({_file_uri(path)})."
    return f" Cohort: {count:,} visitors (no CSV path passed; see `--cohort-dir`)."


def _callouts(
    channel_climb_yield: Sequence[ChannelClimbYield],
    site_climb_rate: float,
    silent_from_bl: int,
    internal_domains: Tuple[str, ...] = (),
    *,
    silent_brand_lovers_count: int = 0,
    climbed_count: int = 0,
    cohort_paths: Optional[Mapping[str, str]] = None,
    join_id_key: Optional[str] = None,
    join_id_populated: bool = False,
    at_risk_now: Optional["AtRiskNow"] = None,
) -> List[str]:
    """Bob's rule: each callout leads with a verb (an action the customer
    can take), then the supporting numbers. All channel callouts use the
    transition math from the Channel climb yield table (prior-active
    visitors who climbed), not single-window stickiness."""
    callouts: List[str] = []
    cohort_paths = cohort_paths or {}
    # The join-ID pattern is "wired up" only when the customer has named
    # a key AND the report saw at least one captured value. A configured
    # key with zero captures still gets the wiring nudge: the customer
    # likely set it before adding the param to live email URLs.
    join_wired = bool(join_id_key) and join_id_populated

    # Volume split: "big" channels get a positional callout; small channels
    # only get one if they're notably high-yield or zero-yield.
    total_prior = sum(row.visitors for row in channel_climb_yield)
    big_floor = max(50, int(total_prior * 0.01))

    for row in channel_climb_yield:
        if row.visitors < SMALL_VOLUME_FLOOR:
            continue
        if _skip_referral_recommendation(row.channel, internal_domains):
            continue
        if site_climb_rate == 0:
            continue
        ratio = row.rate / site_climb_rate
        is_referral = row.channel.startswith("Referral: ")

        if is_referral:
            # External referrers the customer doesn't control. The action
            # isn't pause-spend or grow-volume; it's manual review of the
            # originating page (what context brings visitors over) and the
            # landing pages (what they see on arrival).
            if row.rate == 0 or ratio <= 0.5:
                count_phrase = (
                    f"{row.visitors:,} prior-window visitors and zero climbed"
                    if row.rate == 0
                    else (
                        f"{row.visitors:,} prior-window visitors climbed at "
                        f"only {row.rate:.1%} ({ratio:.1f}× site avg)"
                    )
                )
                callouts.append(
                    f"**Manually review {row.channel}** – {count_phrase}. "
                    f"Review the originating page (what context sends "
                    f"visitors your way) and the landing pages they hit on "
                    f"arrival; if intent doesn't match, the landing "
                    f"experience is the piece you can rework."
                )
            elif ratio >= STRONG_CHANNEL_CLIMB_RATIO:
                callouts.append(
                    f"**Study what's working in {row.channel}** – "
                    f"{row.visitors:,} prior-window visitors climbed at "
                    f"{row.rate:.1%} ({ratio:.1f}× site avg). The high "
                    f"climb rate is a signal: check what those visitors are "
                    f"landing on and look for ways to replicate that "
                    f"experience for similar-audience traffic from channels "
                    f"you do control."
                )
        elif row.visitors >= big_floor:
            if ratio >= STRONG_CHANNEL_CLIMB_RATIO:
                callouts.append(
                    f"**Commission more content like the pages driving "
                    f"{row.channel}** – {row.visitors:,} prior-window "
                    f"visitors climbed at {row.rate:.1%} ({ratio:.1f}× "
                    f"site avg). See [Top levers to grow relationships]"
                    f"(#top-levers-to-grow-relationships) below for the "
                    f"specific pages, sections, and authors doing the work."
                )
            elif ratio <= 0.5:
                callouts.append(
                    f"**Pull {row.channel}'s top landing pages and check "
                    f"intent match before scaling spend** – "
                    f"{row.visitors:,} prior-window visitors climbed at "
                    f"only {row.rate:.1%} ({ratio:.1f}× site avg). If the "
                    f"landing pages don't match what visitors came looking "
                    f"for, pause spend or rework them; if they do, the "
                    f"channel is structurally low-yield and worth less "
                    f"investment than its volume suggests."
                )
        else:
            if row.rate == 0:
                callouts.append(
                    f"**Pause or rework {row.channel} before sending more "
                    f"traffic this way** – {row.visitors:,} prior-window "
                    f"visitors and zero climbed. Pull the specific "
                    f"campaign driving this bucket and check whether the "
                    f"experience matches visitor intent."
                )
            elif ratio >= STRONG_CHANNEL_CLIMB_RATIO:
                callouts.append(
                    f"**Grow volume against {row.channel}** – small but "
                    f"high-yield ({row.visitors:,} prior-window visitors "
                    f"climbed at {row.rate:.1%}, {ratio:.1f}× site avg). "
                    f"See [Top levers to grow relationships]"
                    f"(#top-levers-to-grow-relationships) below for the "
                    f"pages working for this audience."
                )

    if silent_from_bl > 0:
        path = cohort_paths.get("silent-brand-lovers")
        if join_wired:
            tail = (
                f" Build a re-engagement send from this cohort: each row "
                f"in the CSV carries the `{join_id_key}` value you set "
                f"on that recipient's email links, so you can upload the "
                f"list directly to your CRM and target the visitors "
                f"individually."
            )
        else:
            tail = (
                " Individual re-engagement needs a per-recipient identifier "
                "in your email URLs (see [Method & caveats](#method-caveats))."
            )
        callouts.append(
            f"**Commission against what your churned brand lovers last "
            f"cared about** – {silent_from_bl:,} went silent this period "
            f"(highest-value churn). Aggregate the cohort CSV's "
            f"*last_engaged_section* and *last_engaged_page* columns to "
            f"see which sections and pieces still pulled them in before "
            f"they disappeared, and weight what you publish next against "
            f"those."
            + tail
            + _cohort_link_phrase(
                "silent-brand-lovers", silent_brand_lovers_count, path,
            )
        )

    if join_wired and at_risk_now is not None:
        ar_members = len(at_risk_now.members)
        if ar_members > 0:
            path = cohort_paths.get("at-risk-now")
            bl_slipped = at_risk_now.bl_to_returning
            bl_phrase = "brand lover" if bl_slipped == 1 else "brand lovers"
            highest_value_clause = (
                f" {bl_slipped:,} are {bl_phrase} slipping to returning – "
                f"the highest-value slice."
                if bl_slipped > 0
                else ""
            )
            visitor_word = "visitor is" if ar_members == 1 else "visitors are"
            callouts.append(
                f"**Re-engage your at-risk visitors before they go silent** – "
                f"{ar_members:,} {visitor_word} slipping but still active "
                f"this period.{highest_value_clause} Upload this cohort "
                f"to your CRM using the `{join_id_key}` column and send a "
                f"relevant message while the relationship is still live."
                + _cohort_link_phrase(
                    "at-risk-now", ar_members, path,
                )
            )

    if climbed_count > 0:
        path = cohort_paths.get("climbed-to-brand-lover")
        callouts.append(
            f"**Study what turned this period's brand lovers into brand "
            f"lovers** – {climbed_count:,} visitors climbed to the top "
            f"tier. The cohort CSV lists each visitor's prior tier and "
            f"last-engaged section, so you can name the visitor profiles "
            f"and sections doing the climbing work, then commission more "
            f"of both."
            + _cohort_link_phrase(
                "climbed-to-brand-lover", climbed_count, path,
            )
        )

    if not callouts:
        callouts.append(
            "**Watch the brand-lover transition rate next period** – no "
            "strong action signals fired this period. Early indicators of "
            "relationship deepening or churn show up in that rate first."
        )
    # Promote the join-ID wiring nudge to the top of the list when it isn't
    # set up yet. This is the highest-leverage thing the customer can do:
    # without it, the cohort CSVs identify visitors by an opaque cookie
    # hash and can't be merged back into a CRM send. With it, every
    # cohort below becomes an email-actionable list. The callout drops
    # off silently once the key is configured AND at least one visitor's
    # captured value lands in the report.
    if not join_wired:
        callouts.insert(
            0,
            "**Make these cohorts email-actionable** – add an opaque "
            "per-recipient identifier to URLs in your email campaigns "
            "(e.g. `?pid=<id>` – `pid` is just the default; any param "
            "name works), tell us the parameter name, and re-run "
            "`/staircase`. The ID then appears as a column in cohort CSVs "
            "alongside `visitor_site_id`, and you can merge it back into "
            "your CRM list. The ID can be per-recipient (durable across "
            "sends) or per-send (unique per campaign-recipient pair); "
            "either shape works. See [Method & caveats](#method-caveats) "
            "for the full pattern."
        )
    return callouts


def render_markdown(r: StaircaseResult) -> str:
    cur = r.tier_counts_current
    prior = r.tier_counts_prior
    trans = r.transitions

    cur_one = cur.get(ONE_TIME, 0)
    cur_ret = cur.get(RETURNING, 0)
    cur_bl = cur.get(BRAND_LOVER, 0)
    cur_total = cur_one + cur_ret + cur_bl

    p_one = prior.get(ONE_TIME, 0)
    p_ret = prior.get(RETURNING, 0)
    p_bl = prior.get(BRAND_LOVER, 0)
    p_total = p_one + p_ret + p_bl

    moved_one_to_ret = trans.get((ONE_TIME, RETURNING), 0)
    moved_one_to_bl = trans.get((ONE_TIME, BRAND_LOVER), 0)
    moved_ret_to_bl = trans.get((RETURNING, BRAND_LOVER), 0)
    silent_from_bl = trans.get((BRAND_LOVER, SILENT), 0)
    silent_from_ret = trans.get((RETURNING, SILENT), 0)
    silent_from_one = trans.get((ONE_TIME, SILENT), 0)
    new_in_period = sum(
        v for (pt, ct), v in trans.items() if pt == SILENT and ct != SILENT
    )

    total_prior, site_climb_rate = _baseline_climb_rate(r.climb_yield_by_channel)

    site_climb_rate_for_callouts = (
        r.net_movement.climb_rate if r.net_movement else 0.0
    )
    callouts = _callouts(
        r.channel_climb_yield,
        site_climb_rate_for_callouts,
        silent_from_bl,
        internal_domains=r.internal_domains,
        silent_brand_lovers_count=len(r.silent_brand_lovers),
        climbed_count=len(r.climbed_to_brand_lover),
        cohort_paths=r.cohort_paths,
        join_id_key=r.join_id_key,
        join_id_populated=r.join_id_populated,
        at_risk_now=r.at_risk_now,
    )

    lines: List[str] = []
    title_suffix = f": {r.site_label}" if r.site_label else ""
    lines.append(f"# Relationship Staircase Report{title_suffix}")
    lines.append("")
    lines.append(
        "The Relationship Staircase Report surfaces the signals, events, and "
        "content that deepen your relationship with your audience. It segments "
        "visitors into one-time visitors, returning visitors, and brand lovers "
        "– then highlights what's moving people up the staircase. "
        "Relationship depth matters because a brand lover is worth orders of "
        "magnitude more than a one-time visitor – they consume more, convert "
        "more, and retain longer. Optimizing for depth changes what you "
        "commission, where you invest, and who you target next."
    )
    lines.append("")
    lines.append(
        f"**Current window:** {_fmt_window(r.current_window)} • "
        f"**Prior window:** {_fmt_window(r.prior_window)}"
    )
    if r.site_id_filter:
        lines.append(f"**Site ID filter:** `{r.site_id_filter}`")
    lines.append("")

    if r.coverage_note:
        lines.append(f"> **Note on data coverage:** {r.coverage_note}")
        lines.append("")

    if r.employee_filter_key:
        val_repr = json.dumps(r.employee_filter_value)
        lines.append(
            f"> **Employee filter active:** events tagged with "
            f"`extra_data['{r.employee_filter_key}'] = {val_repr}` are excluded. "
            f"Run `/agentic-analytics:identify-employees --clear` to remove."
        )
        lines.append("")

    if r.net_movement is not None:
        nm = r.net_movement
        if nm.net >= 0:
            net_sign = f"net +{nm.net:,}"
        else:
            net_sign = f"net {nm.net:,}"
        rate_pct = nm.climb_rate * 100
        lines.append(
            f"**{rate_pct:.1f}% of last period's active visitors climbed a tier** "
            f"({nm.up:,} of {nm.eligible:,})."
        )
        lines.append(
            f"_Detail: +{nm.up:,} up · -{nm.down:,} down · {net_sign}._"
        )
        lines.append("")

    lines.append("## Recommendations")
    lines.append("")
    for c in callouts:
        lines.append(f"- {c}")
    lines.append("")
    lines.append("_Supporting evidence for these recommendations is in the sections below._")
    lines.append("")

    if r.bot_threshold > 0 and (r.bot_visitors_filtered or r.bot_groups):
        real_visitors = r.total_visitors_seen - r.bot_visitors_filtered
        pct = (
            r.bot_visitors_filtered / r.total_visitors_seen * 100
            if r.total_visitors_seen else 0
        )
        lines.append("## Traffic quality")
        lines.append("")
        lines.append(
            f"Filtered **{r.bot_visitors_filtered:,}** of "
            f"**{r.total_visitors_seen:,}** observed visitors ({pct:.0f}%) as "
            f"bot-suspect. All metrics below are computed on the remaining "
            f"**{real_visitors:,}** real-audience visitors."
        )
        lines.append("")
        lines.append(
            f"_Rule: any single (first-pageview date, ip_city, browser/version/os) "
            f"signature with more than {r.bot_threshold} distinct `visitor_site_id`s "
            f"is treated as one scraper run. Pass `--bot-threshold 0` to disable._"
        )
        lines.append("")
        if r.bot_groups:
            lines.append("Top filtered signatures:")
            lines.append("")
            lines.append("| First-PV date | City | Browser signature | Visitors |")
            lines.append("|---|---|---|---:|")
            for g in r.bot_groups[:8]:
                lines.append(
                    f"| {g.first_pv_date.isoformat()} | {g.ip_city} | "
                    f"`{g.ua_signature}` | {g.visitor_count:,} |"
                )
            if len(r.bot_groups) > 8:
                rest = sum(g.visitor_count for g in r.bot_groups[8:])
                lines.append(
                    f"| ({len(r.bot_groups) - 8} more) | | | {rest:,} |"
                )
            lines.append("")

    lines.append("## Where your audience sits today")
    lines.append("")
    lines.append("| Tier | Definition | Visitors | vs prior period |")
    lines.append("|---|---|---:|---:|")
    lines.append(
        f"| Brand lovers | 5+ visits | {cur_bl:,} | {_fmt_delta(cur_bl, p_bl)} |"
    )
    lines.append(
        f"| Returning | 2-4 visits | {cur_ret:,} | {_fmt_delta(cur_ret, p_ret)} |"
    )
    lines.append(
        f"| One-time | 1 visit | {cur_one:,} | {_fmt_delta(cur_one, p_one)} |"
    )
    lines.append(
        f"| **All visitors** | | **{cur_total:,}** | "
        f"**{_fmt_delta(cur_total, p_total)}** |"
    )
    lines.append("")

    lines.append("## Who moved up")
    lines.append("")
    lines.append(
        f"- **{moved_one_to_ret + moved_one_to_bl:,}** one-time visitors became "
        f"returning or jumped to brand lover."
    )
    lines.append(
        f"- **{moved_ret_to_bl:,}** returning visitors crossed into brand lovers."
    )
    lines.append(
        f"- **{new_in_period:,}** visitors are net-new this period (no prior visit)."
    )
    lines.append("")

    at_risk = r.at_risk_now
    lines.append("## At risk now")
    lines.append("")
    lines.append(
        "_In-window backsliders: still active, but moving the wrong direction. "
        "Re-engagement target list before they're gone._"
    )
    lines.append("")
    if at_risk is None or (at_risk.bl_to_returning == 0 and at_risk.returning_to_one_time == 0):
        lines.append("- _No in-window backsliders this period._")
    else:
        lines.append(
            f"- **{at_risk.bl_to_returning:,}** brand lovers slipped to returning. "
            f"Highest-value group to re-engage; the relationship is still alive."
        )
        lines.append(
            f"- **{at_risk.returning_to_one_time:,}** returning visitors slipped to "
            f"one-time. Bigger volume, lower per-visitor value. Same recommended "
            f"action, scaled."
        )
    at_risk_path = r.cohort_paths.get("at-risk-now")
    if at_risk_path:
        label = _cohort_link_label("at-risk-now")
        lines.append(f"- [{label}]({_file_uri(at_risk_path)}).")
    lines.append("")

    lines.append("## Gone silent")
    lines.append("")
    lines.append(
        "_Out-of-window churn: visitors with a prior-period tier and zero "
        "sessions in the current period._"
    )
    lines.append("")
    lines.append(
        f"- **{silent_from_bl:,}** brand lovers went silent. Highest-value churn."
    )
    lines.append(f"- **{silent_from_ret:,}** returning visitors went silent.")
    lines.append(
        f"- **{silent_from_one:,}** one-time visitors didn't return this period."
    )
    lines.append("")

    site_climb_rate = r.net_movement.climb_rate if r.net_movement else 0.0
    if r.channel_climb_yield:
        lines.append("## Channel climb yield")
        lines.append("")
        lines.append(
            "_Of visitors whose first prior-window pageview was attributed "
            "to this acquisition channel, the fraction who climbed to a "
            "higher tier in the current window. Same transition math as "
            "the section and author tables below. Rows floored at "
            f"{CLIMB_YIELD_VOLUME_FLOOR} prior-window visitors. Site climb "
            f"rate: {site_climb_rate:.1%}._"
        )
        lines.append("")
        lines.append("| Channel | Prior visitors | Climbers | Climb rate | vs site avg |")
        lines.append("|---|---:|---:|---:|---:|")
        for row in r.channel_climb_yield:
            ratio = row.rate / site_climb_rate if site_climb_rate > 0 else 0
            lines.append(
                f"| {row.channel} | {row.visitors:,} | {row.climbers:,} | "
                f"{row.rate:.1%} | {ratio:.1f}× |"
            )
        lines.append("")

    if r.section_climb_yield:
        lines.append("## Section climb yield")
        lines.append("")
        lines.append(
            "_Of visitors whose first prior-window pageview landed on this "
            "section, the fraction who climbed to a higher tier in the "
            f"current window. Rows floored at {CLIMB_YIELD_VOLUME_FLOOR} "
            "prior-window visitors._"
        )
        lines.append("")
        lines.append("| Section | Prior visitors | Climbers | Climb rate |")
        lines.append("|---|---:|---:|---:|")
        for row in r.section_climb_yield:
            label = f"(by URL: {row.section})" if row.is_fallback else row.section
            lines.append(
                f"| {label} | {row.visitors:,} | {row.climbers:,} | {row.rate:.1%} |"
            )
        lines.append("")

    if r.author_climb_yield:
        lines.append("## Author climb yield")
        lines.append("")
        lines.append(
            "_Of visitors whose first prior-window pageview was by this "
            "author, the fraction who climbed to a higher tier in the "
            f"current window. Rows floored at {CLIMB_YIELD_VOLUME_FLOOR} "
            "prior-window visitors._"
        )
        lines.append("")
        lines.append("| Author | Prior visitors | Climbers | Climb rate |")
        lines.append("|---|---:|---:|---:|")
        for row in r.author_climb_yield:
            lines.append(
                f"| `{row.author}` | {row.visitors:,} | "
                f"{row.climbers:,} | {row.rate:.1%} |"
            )
        lines.append("")

    # Per-domain referral table: surfaces the long tail of referrer domains
    # with their per-domain climb rate.
    referral_rows = _top_referral_sources(r.climb_yield_by_channel)
    if referral_rows:
        lines.append("## Top referral sources")
        lines.append("")
        lines.append(
            "_Per-domain breakdown of referrer traffic (≥10 prior visitors), "
            "sorted by climb rate. Climb rate = of prior-window visitors who "
            "arrived from this domain, the fraction whose tier rank rose in "
            "the current window._"
        )
        lines.append("")
        lines.append("| Source | Prior visitors | Climbers | Climb rate | vs site avg |")
        lines.append("|---|---:|---:|---:|---:|")
        for domain, prior_n, climbers, rate in referral_rows:
            ratio = rate / site_climb_rate if site_climb_rate > 0 else 0
            lines.append(
                f"| {domain} | {prior_n:,} | {climbers:,} | {rate:.1%} | "
                f"{ratio:.1f}× |"
            )
        lines.append("")

    if r.strong_channels:
        lines.extend(_render_strong_channels_markdown(r.strong_channels, site_climb_rate))

    if r.climbing_breakdowns:
        lines.extend(_render_climbing_markdown(r.climbing_breakdowns))

    if r.first_touch_content:
        lines.append("## Climber first-touch content")
        lines.append("")
        lines.append(
            "_For each climbing transition, the page, section, and channel "
            "of each climber's earliest pageview in cached data. Names the "
            "entry points that recruit climbing relationships._"
        )
        lines.append("")
        for ft in r.first_touch_content:
            lines.append(f"### {ft.header} ({ft.climbers:,} climbers)")
            lines.append("")
            if ft.by_channel:
                lines.append("**By acquisition channel** (first pageview):")
                lines.append("")
                lines.append("| Channel | Climbers |")
                lines.append("|---|---:|")
                for chan, n in ft.by_channel:
                    lines.append(f"| {chan} | {n:,} |")
                lines.append("")
            if ft.by_page:
                lines.append(
                    f"**By first-touch page** (>= {FIRST_TOUCH_FLOOR} climbers):"
                )
                lines.append("")
                lines.append("| Page | Climbers |")
                lines.append("|---|---:|")
                for page, n in ft.by_page:
                    lines.append(f"| `{_short_page(page)}` | {n:,} |")
                lines.append("")
            if ft.by_section:
                lines.append(
                    f"**By first-touch section** (>= {FIRST_TOUCH_FLOOR} climbers):"
                )
                lines.append("")
                lines.append("| Section | Climbers |")
                lines.append("|---|---:|")
                for label, n, is_fallback in ft.by_section:
                    display = f"(by URL: {label})" if is_fallback else label
                    lines.append(f"| {display} | {n:,} |")
                lines.append("")

    if r.second_visit_content and r.second_visit_content.rows:
        lines.append("## Second-visit hook content")
        lines.append("")
        lines.append(
            "_For one-time -> returning climbers, the page read on the second "
            f"visit (the first pageview of the second session). At least "
            f"{SECOND_VISIT_FLOOR} climbers per row. Names the content that "
            "turned curiosity into a relationship._"
        )
        lines.append("")
        lines.append("| Page | Climbers |")
        lines.append("|---|---:|")
        for page, n in r.second_visit_content.rows:
            lines.append(f"| `{_short_page(page)}` | {n:,} |")
        lines.append("")

    if r.return_cadence and any(b.visitors_with_second_visit > 0 for b in r.return_cadence):
        lines.append("## Return cadence")
        lines.append("")
        lines.append(
            "_For visitors with two or more sessions in the current window, "
            "the time between session 1 and session 2. Tells you when to "
            "time newsletter prompts, re-engagement campaigns, and "
            "registration walls._"
        )
        lines.append("")
        lines.append(
            "| Cadence bucket | Visitors with 2nd visit | Climbers | Climb rate |"
        )
        lines.append("|---|---:|---:|---:|")
        for b in r.return_cadence:
            lines.append(
                f"| {b.label} | {b.visitors_with_second_visit:,} | "
                f"{b.climbers:,} | {b.rate:.1%} |"
            )
        lines.append("")

    if r.marker_lift and any(m.fired_count > 0 for m in r.marker_lift):
        lines.append("## Relationship-marker lift")
        lines.append("")
        lines.append(
            "_For each marker event (newsletter signup, account creation, "
            "login), the climb rate of visitors who fired the event vs "
            "visitors who didn't. Lift > 1 means the event is associated "
            "with higher climbing odds. Tests the hypothesis that these "
            "events are climbing levers worth promoting earlier in the "
            "journey._"
        )
        lines.append("")
        lines.append(
            "| Marker | Fired | Climb rate (fired) | Climb rate (didn't fire) | Lift |"
        )
        lines.append("|---|---:|---:|---:|---:|")
        for ml in r.marker_lift:
            if ml.fired_count == 0:
                continue
            lift_str = f"{ml.lift_ratio:.1f}×" if ml.lift_ratio is not None else "n/a"
            lines.append(
                f"| {ml.marker} | {ml.fired_count:,} | "
                f"{ml.fired_climb_rate:.1%} | {ml.unfired_climb_rate:.1%} | "
                f"{lift_str} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("### Method & caveats")
    lines.append("")
    lines.append(
        "- **Visit** = a session. Two pageviews more than 30 minutes apart "
        "count as two visits."
    )
    lines.append(
        "- **Tiers**: one-time = 1 visit in window; returning = 2-4; brand "
        "lover = 5+. Tier is computed independently for each window."
    )
    lines.append(
        "- **Identity** = `visitor_site_id` (Parsely first-party cookie hash). "
        "Cookie clears and browser switches inflate the one-time count and "
        "fragment a single human across multiple IDs, so climb rates are a "
        "lower bound: visitors who really did climb in the data can look "
        "like net-new strangers on the other side of a cookie reset."
    )
    if r.join_id_key and r.join_id_populated:
        lines.append(
            f"- **Cohort CSVs** key on `visitor_site_id`, a Parsely "
            f"first-party cookie hash that's opaque to your CRM. Each row "
            f"also carries the `{r.join_id_key}` value captured from the "
            f"URL of any email-campaign click – your per-recipient join "
            f"key. Upload a cohort CSV to your CRM, merge on "
            f"`{r.join_id_key}`, and you have a targetable list. The "
            f"identifier can be per-recipient (durable across sends) or "
            f"per-send (unique per campaign-recipient pair); either shape "
            f"works."
        )
    else:
        lines.append(
            "- **Cohort CSVs** key on `visitor_site_id`, a Parsely "
            "first-party cookie hash that's opaque to your CRM. To make "
            "cohorts email-actionable, add an opaque per-recipient "
            "identifier to URLs in your email campaigns (e.g. `?pid=<id>` "
            "– `pid` is just the default; any param name works), tell us "
            "the parameter name (`/agentic-analytics:init` will prompt, "
            "or edit `bucket.json` directly), and re-run `/staircase`. "
            "The ID column then appears in cohort CSVs alongside "
            "`visitor_site_id`, so you can merge results back into your "
            "CRM lists. The ID can be per-recipient (durable across "
            "sends) or per-send (unique per campaign-recipient pair); "
            "either shape works. No tracker setup is needed; the query "
            "string is already captured in the event's `url` field."
        )
    network_chan = f"{r.network_label}, " if r.network_label else ""
    lines.append(
        "- **Channel** = a GA-style grouping of the first pageview in the "
        "current window. Combines `utm_medium`, `utm_source`, and Parsely's "
        "`sref_category` into one bucket per visitor: "
        f"Email, Paid, {network_chan}Social Referrers, Search Referrers, "
        "AI Referrers, Referral: <domain>, Direct. Names like "
        "*Search Referrers* indicate the bucket was detected via referrer; "
        "*Email* and *Paid* are UTM-based. Visitors arriving via session "
        "continuation or cross-subdomain navigation are excluded from the "
        "channel mix "
        "(no honest acquisition source available)."
    )
    lines.append(
        f"- **Coverage**: {r.pageviews_loaded:,} pageviews loaded across "
        f"{r.total_visitors_seen:,} unique visitor IDs in the union of both windows."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Render: HTML (generated file with chart)
# --------------------------------------------------------------------------


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>__TITLE__</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    table.report { width: 100%; border-collapse: collapse; }
    table.report th, table.report td { padding: 6px 10px; border-bottom: 1px solid #e2e8f0; text-align: left; vertical-align: top; }
    table.report th { background: #f1f5f9; font-weight: 600; }
    table.report td.num, table.report th.num { text-align: right; font-variant-numeric: tabular-nums; }
    .delta-pos { color: #047857; }
    .delta-neg { color: #b91c1c; }
    .muted { color: #64748b; }
    code { background: #f1f5f9; padding: 1px 6px; border-radius: 3px; font-size: 0.9em; }
    h2[id], h3[id] { scroll-margin-top: 1rem; }
    @media print {
      body { background: white !important; }
      main { max-width: none !important; margin: 0 !important; padding: 0.5in !important; }
      table.report, table.report tr { page-break-inside: avoid; break-inside: avoid; }
      h2, h3 { page-break-after: avoid; break-after: avoid; }
      a[href^="http"]:after, a[href^="file:"]:after {
        content: " (" attr(href) ")";
        font-size: 0.85em;
        color: #475569;
        word-break: break-all;
      }
      .delta-pos { color: #047857 !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
      .delta-neg { color: #b91c1c !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    }
  </style>
</head>
<body class="bg-slate-50 text-slate-900">
  <main class="max-w-4xl mx-auto p-8 space-y-8">
    __HEADER_HTML__

    __CLIMBING_HEADLINE_HTML__

    <section>
      <h2 class="text-lg font-semibold mb-3" id="recommendations">Recommendations</h2>
      __CALLOUTS_HTML__
      <p class="text-sm muted mt-3"><em>Supporting evidence for these recommendations is in the sections below.</em></p>
    </section>

    <section>
      <h2 class="text-lg font-semibold mb-3" id="where-your-audience-sits-today">Where your audience sits today</h2>
      __TIERS_TABLE__
    </section>

    <section>
      <h2 class="text-lg font-semibold mb-3" id="who-moved-up">Who moved up</h2>
      __WHO_MOVED_UP_HTML__
    </section>

    <section>
      <h2 class="text-lg font-semibold mb-3" id="at-risk-now">At risk now</h2>
      __AT_RISK_NOW_HTML__
    </section>

    <section>
      <h2 class="text-lg font-semibold mb-3" id="gone-silent">Gone silent</h2>
      __GONE_SILENT_HTML__
    </section>

    __CHANNEL_CLIMB_YIELD_SECTION__

    __SECTION_CLIMB_YIELD_SECTION__

    __AUTHOR_CLIMB_YIELD_SECTION__

    __REFERRAL_SOURCES_SECTION__

    __STRONG_CHANNELS_SECTION__

    __CLIMBING_SECTION__

    __FIRST_TOUCH_CONTENT_SECTION__

    __SECOND_VISIT_CONTENT_SECTION__

    __RETURN_CADENCE_SECTION__

    __MARKER_LIFT_SECTION__

    <hr class="my-6 border-slate-200" />

    <section class="text-sm muted">
      <h3 class="font-semibold text-slate-700 mb-2" id="method-caveats">Method &amp; caveats</h3>
      __CAVEATS_HTML__
    </section>
  </main>
</body>
</html>
"""


def _delta_html(curr: int, prev: int) -> str:
    if prev == 0 and curr == 0:
        return "<span class='muted'>–</span>"
    if prev == 0:
        return "<span class='delta-pos'>new</span>"
    pct = (curr - prev) / prev * 100
    cls = "delta-pos" if pct >= 0 else "delta-neg"
    sign = "+" if pct >= 0 else ""
    return f"<span class='{cls}'>{sign}{pct:.0f}%</span>"


def _esc(s: Any) -> str:
    return html.escape(str(s)) if s is not None else ""


def _page_link_html(url: str) -> str:
    """Render a page URL as a hover-discoverable link in HTML tables. The
    visible text is `_short_page(url)` wrapped in `<code>` so the cell
    still reads as a plain path; the anchor is styled with hover-only
    Tailwind classes so the link affordance appears on cursor-over and
    long URLs don't visually clutter the table at rest."""
    return (
        f"<a class=\"hover:text-blue-700 hover:underline\" "
        f"href=\"{html.escape(url, quote=True)}\">"
        f"<code>{_esc(_short_page(url))}</code></a>"
    )


_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_ANCHOR_STRIP_RE = re.compile(r"[^a-z0-9\- ]")


def _anchor(text: str) -> str:
    """GitHub-flavored anchor slug for a heading. Lowercase, strip
    apostrophes / punctuation, collapse spaces to hyphens. Matches the IDs
    GitHub renders automatically for markdown H2s/H3s so the same link
    target works in both the .md and .html outputs (after the HTML render
    emits matching id="..." attributes on each heading)."""
    s = text.lower()
    s = _ANCHOR_STRIP_RE.sub("", s)
    s = re.sub(r"\s+", "-", s.strip())
    return s


def _md_inline_to_html(text: str) -> str:
    """HTML-escape, then convert inline markdown to HTML for callout strings:
    **bold**, *italic*, `code`, and [text](url) links. Callouts are
    authored as markdown for the .md output; this is what threads the
    same string into the HTML render without losing formatting or links."""
    placeholders: List[Tuple[str, str]] = []

    def _link_sub(m: "re.Match[str]") -> str:
        label = m.group(1)
        url = m.group(2)
        token = f"\x00LINK{len(placeholders)}\x00"
        placeholders.append(
            (token, f"<a class=\"text-blue-700 underline\" href=\"{html.escape(url, quote=True)}\">{html.escape(label)}</a>")
        )
        return token

    text = _LINK_RE.sub(_link_sub, text)
    text = html.escape(text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    text = _CODE_RE.sub(r"<code>\1</code>", text)
    for token, html_repl in placeholders:
        text = text.replace(token, html_repl)
    return text


# Backwards-compat alias; callers still use the old name.
_md_bold_to_html = _md_inline_to_html


def _render_tiers_table_html(r: StaircaseResult) -> str:
    cur = r.tier_counts_current
    prior = r.tier_counts_prior
    cur_one = cur.get(ONE_TIME, 0)
    cur_ret = cur.get(RETURNING, 0)
    cur_bl = cur.get(BRAND_LOVER, 0)
    p_one = prior.get(ONE_TIME, 0)
    p_ret = prior.get(RETURNING, 0)
    p_bl = prior.get(BRAND_LOVER, 0)
    cur_total = cur_one + cur_ret + cur_bl
    p_total = p_one + p_ret + p_bl

    rows = [
        ("Brand lovers", "5+ visits", cur_bl, p_bl, False),
        ("Returning", "2-4 visits", cur_ret, p_ret, False),
        ("One-time", "1 visit", cur_one, p_one, False),
        ("All visitors", "", cur_total, p_total, True),
    ]
    body = []
    for tier, defn, curr, prev, is_total in rows:
        cls = " style='font-weight: 600;'" if is_total else ""
        body.append(
            f"<tr{cls}><td>{_esc(tier)}</td><td class='muted'>{_esc(defn)}</td>"
            f"<td class='num'>{curr:,}</td><td class='num'>{_delta_html(curr, prev)}</td></tr>"
        )
    return (
        "<table class='report'><thead><tr><th>Tier</th><th>Definition</th>"
        "<th class='num'>Visitors</th><th class='num'>vs prior period</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


def _render_climbing_headline_html(r: StaircaseResult) -> str:
    if r.net_movement is None:
        return ""
    nm = r.net_movement
    net_sign = f"+{nm.net:,}" if nm.net >= 0 else f"{nm.net:,}"
    rate_pct = nm.climb_rate * 100
    return (
        "<section>"
        "<p class='text-lg font-semibold'>"
        f"{rate_pct:.1f}% of last period's active visitors climbed a tier "
        f"<span class='muted'>({nm.up:,} of {nm.eligible:,})</span>."
        "</p>"
        "<p class='text-sm muted'>Detail: "
        f"<span class='delta-pos'>+{nm.up:,}</span> up &middot; "
        f"<span class='delta-neg'>-{nm.down:,}</span> down &middot; "
        f"net {net_sign}."
        "</p></section>"
    )


def _render_who_moved_up_html(r: StaircaseResult) -> str:
    trans = r.transitions
    items = [
        (
            f"{trans.get((ONE_TIME, RETURNING), 0) + trans.get((ONE_TIME, BRAND_LOVER), 0):,} one-time visitors became "
            "returning or jumped to brand lover."
        ),
        f"{trans.get((RETURNING, BRAND_LOVER), 0):,} returning visitors crossed into brand lovers.",
        f"{sum(v for (pt, ct), v in trans.items() if pt == SILENT and ct != SILENT):,} visitors are net-new this period (no prior visit).",
    ]
    return "<ul class='list-disc pl-6 space-y-1'>" + "".join(
        f"<li>{i}</li>" for i in items
    ) + "</ul>"


def _render_at_risk_now_html(r: StaircaseResult) -> str:
    intro = (
        "<p class='text-sm muted mb-3'>In-window backsliders: still active, "
        "but moving the wrong direction. Re-engagement target list before "
        "they're gone.</p>"
    )
    ar = r.at_risk_now
    if ar is None or (ar.bl_to_returning == 0 and ar.returning_to_one_time == 0):
        return intro + "<p class='muted'>No in-window backsliders this period.</p>"
    items = [
        f"<strong>{ar.bl_to_returning:,}</strong> brand lovers slipped to returning. "
        f"Highest-value group to re-engage; the relationship is still alive.",
        f"<strong>{ar.returning_to_one_time:,}</strong> returning visitors slipped to "
        f"one-time. Bigger volume, lower per-visitor value. Same recommended "
        f"action, scaled.",
    ]
    at_risk_path = r.cohort_paths.get("at-risk-now")
    if at_risk_path:
        label = _cohort_link_label("at-risk-now")
        items.append(
            f"<a class=\"text-blue-700 underline\" "
            f"href=\"{html.escape(_file_uri(at_risk_path), quote=True)}\">"
            f"{_esc(label)}</a>"
        )
    return intro + "<ul class='list-disc pl-6 space-y-1'>" + "".join(
        f"<li>{i}</li>" for i in items
    ) + "</ul>"


def _render_gone_silent_html(r: StaircaseResult) -> str:
    trans = r.transitions
    intro = (
        "<p class='text-sm muted mb-3'>Out-of-window churn: visitors with a "
        "prior-period tier and zero sessions in the current period.</p>"
    )
    items = [
        f"{trans.get((BRAND_LOVER, SILENT), 0):,} brand lovers went silent. Highest-value churn.",
        f"{trans.get((RETURNING, SILENT), 0):,} returning visitors went silent.",
        f"{trans.get((ONE_TIME, SILENT), 0):,} one-time visitors didn't return this period.",
    ]
    return intro + "<ul class='list-disc pl-6 space-y-1'>" + "".join(
        f"<li>{i}</li>" for i in items
    ) + "</ul>"


def _render_channel_climb_yield_html(
    rows: List[ChannelClimbYield],
    site_climb_rate: float,
) -> str:
    body = []
    for row in rows:
        ratio = row.rate / site_climb_rate if site_climb_rate > 0 else 0
        body.append(
            f"<tr><td>{_esc(row.channel)}</td>"
            f"<td class='num'>{row.visitors:,}</td>"
            f"<td class='num'>{row.climbers:,}</td>"
            f"<td class='num'>{row.rate:.1%}</td>"
            f"<td class='num'>{ratio:.1f}×</td></tr>"
        )
    return (
        "<table class='report'><thead><tr><th>Channel</th>"
        "<th class='num'>Prior visitors</th><th class='num'>Climbers</th>"
        "<th class='num'>Climb rate</th><th class='num'>vs site avg</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


def _render_section_climb_yield_html(r: StaircaseResult) -> str:
    if not r.section_climb_yield:
        return ""
    rows = []
    for row in r.section_climb_yield:
        label = (
            f"<span class='muted'>(by URL: <code>{_esc(row.section)}</code>)</span>"
            if row.is_fallback else _esc(row.section)
        )
        rows.append(
            f"<tr><td>{label}</td>"
            f"<td class='num'>{row.visitors:,}</td>"
            f"<td class='num'>{row.climbers:,}</td>"
            f"<td class='num'>{row.rate:.1%}</td></tr>"
        )
    return (
        "<section>"
        "<h2 class='text-lg font-semibold mb-3' id='section-climb-yield'>Section climb yield</h2>"
        "<p class='text-sm muted mb-3'>Of visitors whose first prior-window "
        "pageview landed on this section, the fraction who climbed to a "
        f"higher tier in the current window. Rows floored at "
        f"{CLIMB_YIELD_VOLUME_FLOOR} prior-window visitors.</p>"
        "<table class='report'><thead><tr>"
        "<th>Section</th><th class='num'>Prior visitors</th>"
        "<th class='num'>Climbers</th><th class='num'>Climb rate</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></section>"
    )


def _render_author_climb_yield_html(r: StaircaseResult) -> str:
    if not r.author_climb_yield:
        return ""
    rows = []
    for row in r.author_climb_yield:
        rows.append(
            f"<tr><td><code>{_esc(row.author)}</code></td>"
            f"<td class='num'>{row.visitors:,}</td>"
            f"<td class='num'>{row.climbers:,}</td>"
            f"<td class='num'>{row.rate:.1%}</td></tr>"
        )
    return (
        "<section>"
        "<h2 class='text-lg font-semibold mb-3' id='author-climb-yield'>Author climb yield</h2>"
        "<p class='text-sm muted mb-3'>Of visitors whose first prior-window "
        "pageview was by this author, the fraction who climbed to a higher "
        f"tier in the current window. Rows floored at "
        f"{CLIMB_YIELD_VOLUME_FLOOR} prior-window visitors.</p>"
        "<table class='report'><thead><tr>"
        "<th>Author</th><th class='num'>Prior visitors</th>"
        "<th class='num'>Climbers</th><th class='num'>Climb rate</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></section>"
    )


def _render_strong_channels_html(
    strong_channels: List[StrongChannelBreakdown],
    site_climb_rate: float,
) -> str:
    if not strong_channels:
        return ""

    sections: List[str] = []
    for b in strong_channels:
        sub_tables: List[str] = []

        if b.top_pages:
            rows = []
            for page, visitors, rate in b.top_pages:
                ratio = rate / site_climb_rate if site_climb_rate > 0 else 0
                rows.append(
                    f"<tr><td>{_page_link_html(page)}</td>"
                    f"<td class='num'>{visitors:,}</td>"
                    f"<td class='num'>{rate:.1%}</td>"
                    f"<td class='num'>{ratio:.1f}×</td></tr>"
                )
            sub_tables.append(
                "<h4 class='text-sm font-semibold mt-3 mb-1'>Top landing "
                f"pages (&ge;&nbsp;{STRONG_CHANNEL_LANDING_PAGE_FLOOR} prior "
                "visitors, by climb rate)</h4>"
                "<table class='report'><thead><tr><th>Page</th>"
                "<th class='num'>Prior visitors</th><th class='num'>Climb rate</th>"
                "<th class='num'>vs site avg</th></tr></thead><tbody>"
                + "".join(rows)
                + "</tbody></table>"
            )

        if b.top_sections:
            rows = []
            for label, visitors, rate, is_fallback in b.top_sections:
                ratio = rate / site_climb_rate if site_climb_rate > 0 else 0
                display = (
                    f"<span class='muted'>(by URL: <code>{_esc(label)}</code>)</span>"
                    if is_fallback
                    else _esc(label)
                )
                rows.append(
                    f"<tr><td>{display}</td>"
                    f"<td class='num'>{visitors:,}</td>"
                    f"<td class='num'>{rate:.1%}</td>"
                    f"<td class='num'>{ratio:.1f}×</td></tr>"
                )
            sub_tables.append(
                "<h4 class='text-sm font-semibold mt-3 mb-1'>Top "
                f"sections (&ge;&nbsp;{STRONG_CHANNEL_SECTION_FLOOR} prior "
                "visitors, by climb rate)</h4>"
                "<table class='report'><thead><tr><th>Section</th>"
                "<th class='num'>Prior visitors</th><th class='num'>Climb rate</th>"
                "<th class='num'>vs site avg</th></tr></thead><tbody>"
                + "".join(rows)
                + "</tbody></table>"
            )

        if b.top_authors:
            rows = []
            for author, visitors, rate in b.top_authors:
                ratio = rate / site_climb_rate if site_climb_rate > 0 else 0
                rows.append(
                    f"<tr><td>{_esc(author)}</td>"
                    f"<td class='num'>{visitors:,}</td>"
                    f"<td class='num'>{rate:.1%}</td>"
                    f"<td class='num'>{ratio:.1f}×</td></tr>"
                )
            sub_tables.append(
                "<h4 class='text-sm font-semibold mt-3 mb-1'>Top "
                f"authors (&ge;&nbsp;{STRONG_CHANNEL_AUTHOR_FLOOR} prior "
                "visitors, by climb rate)</h4>"
                "<table class='report'><thead><tr><th>Author</th>"
                "<th class='num'>Prior visitors</th><th class='num'>Climb rate</th>"
                "<th class='num'>vs site avg</th></tr></thead><tbody>"
                + "".join(rows)
                + "</tbody></table>"
            )

        if not sub_tables:
            sub_tables.append(
                "<p class='text-sm muted'>No landing pages, sections, or "
                "authors cleared the per-row volume floor in this channel.</p>"
            )

        sections.append(
            f"<h3 class='text-base font-semibold mt-4 mb-1'>{_esc(b.channel)} "
            f"<span class='muted font-normal'>({b.visitors:,} prior visitors, "
            f"{b.rate:.1%} climb rate, {b.ratio:.1f}× site avg)</span></h3>"
            + "".join(sub_tables)
        )

    return (
        "<section>"
        "<h2 class='text-lg font-semibold mb-3' id='top-levers-to-grow-relationships'>"
        "Top levers to grow relationships</h2>"
        "<p class='text-sm muted mb-3'>For each channel whose prior-window "
        "arrivals climbed at "
        f"&ge;&nbsp;{STRONG_CHANNEL_CLIMB_RATIO:.1f}&times; the site climb "
        f"rate (with &ge;&nbsp;{SMALL_VOLUME_FLOOR} prior visitors), the "
        "landing pages, sections, and authors doing the climbing work. "
        "Climb rate = of prior-window visitors who arrived via this channel "
        "and landed here, the fraction whose tier rank rose in the current "
        "window.</p>"
        + "".join(sections)
        + "</section>"
    )


def _render_first_touch_content_html(r: StaircaseResult) -> str:
    if not r.first_touch_content:
        return ""
    sections: List[str] = []
    for ft in r.first_touch_content:
        sub: List[str] = []
        if ft.by_channel:
            chan_rows = "".join(
                f"<tr><td>{_esc(chan)}</td><td class='num'>{n:,}</td></tr>"
                for chan, n in ft.by_channel
            )
            sub.append(
                "<h4 class='text-sm font-semibold mt-3 mb-1'>By acquisition channel</h4>"
                "<table class='report'><thead><tr><th>Channel</th>"
                "<th class='num'>Climbers</th></tr></thead><tbody>"
                + chan_rows + "</tbody></table>"
            )
        if ft.by_page:
            page_rows = "".join(
                f"<tr><td>{_page_link_html(p)}</td>"
                f"<td class='num'>{n:,}</td></tr>"
                for p, n in ft.by_page
            )
            sub.append(
                f"<h4 class='text-sm font-semibold mt-3 mb-1'>By first-touch page "
                f"<span class='muted font-normal'>(&ge; {FIRST_TOUCH_FLOOR} climbers)</span></h4>"
                "<table class='report'><thead><tr><th>Page</th>"
                "<th class='num'>Climbers</th></tr></thead><tbody>"
                + page_rows + "</tbody></table>"
            )
        if ft.by_section:
            sec_rows = []
            for label, n, is_fallback in ft.by_section:
                display = (
                    f"<span class='muted'>(by URL: <code>{_esc(label)}</code>)</span>"
                    if is_fallback else _esc(label)
                )
                sec_rows.append(
                    f"<tr><td>{display}</td><td class='num'>{n:,}</td></tr>"
                )
            sub.append(
                f"<h4 class='text-sm font-semibold mt-3 mb-1'>By first-touch section "
                f"<span class='muted font-normal'>(&ge; {FIRST_TOUCH_FLOOR} climbers)</span></h4>"
                "<table class='report'><thead><tr><th>Section</th>"
                "<th class='num'>Climbers</th></tr></thead><tbody>"
                + "".join(sec_rows) + "</tbody></table>"
            )
        sections.append(
            f"<h3 class='text-base font-semibold mt-4 mb-1'>{_esc(ft.header)} "
            f"<span class='muted font-normal'>({ft.climbers:,} climbers)</span></h3>"
            + "".join(sub)
        )
    return (
        "<section>"
        "<h2 class='text-lg font-semibold mb-3' id='climber-first-touch-content'>Climber first-touch content</h2>"
        "<p class='text-sm muted mb-3'>For each climbing transition, the "
        "page, section, and channel of each climber's earliest pageview in "
        "cached data. Names the entry points that recruit climbing "
        "relationships.</p>"
        + "".join(sections)
        + "</section>"
    )


def _render_second_visit_content_html(r: StaircaseResult) -> str:
    if not r.second_visit_content or not r.second_visit_content.rows:
        return ""
    rows = "".join(
        f"<tr><td>{_page_link_html(p)}</td>"
        f"<td class='num'>{n:,}</td></tr>"
        for p, n in r.second_visit_content.rows
    )
    return (
        "<section>"
        "<h2 class='text-lg font-semibold mb-3' id='second-visit-hook-content'>Second-visit hook content</h2>"
        "<p class='text-sm muted mb-3'>For one-time &rarr; returning climbers, "
        "the page read on the second visit (the first pageview of the "
        f"second session). At least {SECOND_VISIT_FLOOR} climbers per row. "
        "Names the content that turned curiosity into a relationship.</p>"
        "<table class='report'><thead><tr><th>Page</th>"
        "<th class='num'>Climbers</th></tr></thead><tbody>"
        + rows + "</tbody></table></section>"
    )


def _render_return_cadence_html(r: StaircaseResult) -> str:
    if not r.return_cadence or not any(b.visitors_with_second_visit > 0 for b in r.return_cadence):
        return ""
    rows = []
    for b in r.return_cadence:
        rows.append(
            f"<tr><td>{_esc(b.label)}</td>"
            f"<td class='num'>{b.visitors_with_second_visit:,}</td>"
            f"<td class='num'>{b.climbers:,}</td>"
            f"<td class='num'>{b.rate:.1%}</td></tr>"
        )
    return (
        "<section>"
        "<h2 class='text-lg font-semibold mb-3' id='return-cadence'>Return cadence</h2>"
        "<p class='text-sm muted mb-3'>For visitors with two or more "
        "sessions in the current window, the time between session 1 and "
        "session 2. Tells you when to time newsletter prompts, "
        "re-engagement campaigns, and registration walls.</p>"
        "<table class='report'><thead><tr>"
        "<th>Cadence bucket</th><th class='num'>Visitors with 2nd visit</th>"
        "<th class='num'>Climbers</th><th class='num'>Climb rate</th>"
        "</tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></section>"
    )


def _render_marker_lift_html(r: StaircaseResult) -> str:
    if not r.marker_lift or not any(m.fired_count > 0 for m in r.marker_lift):
        return ""
    rows = []
    for ml in r.marker_lift:
        if ml.fired_count == 0:
            continue
        lift_str = f"{ml.lift_ratio:.1f}&times;" if ml.lift_ratio is not None else "n/a"
        rows.append(
            f"<tr><td><code>{_esc(ml.marker)}</code></td>"
            f"<td class='num'>{ml.fired_count:,}</td>"
            f"<td class='num'>{ml.fired_climb_rate:.1%}</td>"
            f"<td class='num'>{ml.unfired_climb_rate:.1%}</td>"
            f"<td class='num'>{lift_str}</td></tr>"
        )
    return (
        "<section>"
        "<h2 class='text-lg font-semibold mb-3' id='relationship-marker-lift'>Relationship-marker lift</h2>"
        "<p class='text-sm muted mb-3'>For each marker event (newsletter "
        "signup, account creation, login), the climb rate of visitors who "
        "fired the event vs visitors who didn't. Lift &gt; 1 means the "
        "event is associated with higher climbing odds.</p>"
        "<table class='report'><thead><tr>"
        "<th>Marker</th><th class='num'>Fired</th>"
        "<th class='num'>Climb rate (fired)</th>"
        "<th class='num'>Climb rate (didn't fire)</th>"
        "<th class='num'>Lift</th>"
        "</tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></section>"
    )


def _render_climbing_html(breakdowns: List[ClimbingBreakdown]) -> str:
    if not breakdowns:
        return ""

    sections: List[str] = []
    for b in breakdowns:
        sub_tables: List[str] = []
        if b.top_prior_channels:
            rows = []
            for chan, climbers, denom, yield_rate in b.top_prior_channels:
                denom_str = f"{denom:,}" if denom > 0 else "<span class='muted'>&ndash;</span>"
                yield_str = (
                    f"{yield_rate:.1%}" if denom > 0
                    else "<span class='muted'>&ndash;</span>"
                )
                rows.append(
                    f"<tr><td>{_esc(chan)}</td>"
                    f"<td class='num'>{climbers:,}</td>"
                    f"<td class='num'>{denom_str}</td>"
                    f"<td class='num'>{yield_str}</td></tr>"
                )
            sub_tables.append(
                "<h4 class='text-sm font-semibold mt-3 mb-1'>Prior-window "
                f"acquisition channel (&ge;&nbsp;{CLIMBING_CHANNEL_FLOOR} climbers)</h4>"
                "<table class='report'><thead><tr><th>Channel</th>"
                "<th class='num'>Climbers</th><th class='num'>Prior visitors</th>"
                "<th class='num'>Climbing yield</th></tr></thead><tbody>"
                + "".join(rows)
                + "</tbody></table>"
            )

        if b.top_pages:
            rows = []
            for page, c in b.top_pages:
                rows.append(
                    f"<tr><td>{_page_link_html(page)}</td>"
                    f"<td class='num'>{c:,}</td></tr>"
                )
            sub_tables.append(
                "<h4 class='text-sm font-semibold mt-3 mb-1'>Current-window "
                f"content consumed (&ge;&nbsp;{CLIMBING_PAGE_FLOOR} climbers)</h4>"
                "<table class='report'><thead><tr><th>Page</th>"
                "<th class='num'>Climbers</th></tr></thead><tbody>"
                + "".join(rows)
                + "</tbody></table>"
            )

        if b.top_sections:
            rows = []
            for label, c, is_fallback in b.top_sections:
                display = (
                    f"<span class='muted'>(by URL: <code>{_esc(label)}</code>)</span>"
                    if is_fallback else _esc(label)
                )
                rows.append(
                    f"<tr><td>{display}</td>"
                    f"<td class='num'>{c:,}</td></tr>"
                )
            sub_tables.append(
                "<h4 class='text-sm font-semibold mt-3 mb-1'>Current-window "
                f"sections consumed (&ge;&nbsp;{CLIMBING_SECTION_FLOOR} climbers)</h4>"
                "<table class='report'><thead><tr><th>Section</th>"
                "<th class='num'>Climbers</th></tr></thead><tbody>"
                + "".join(rows)
                + "</tbody></table>"
            )

        if b.top_events:
            rows = []
            for slug, c in b.top_events:
                rows.append(
                    f"<tr><td><code>/events/{_esc(slug)}/</code></td>"
                    f"<td class='num'>{c:,}</td></tr>"
                )
            sub_tables.append(
                "<h4 class='text-sm font-semibold mt-3 mb-1'>Events climbers "
                f"attended (&ge;&nbsp;{CLIMBING_EVENT_FLOOR} climbers)</h4>"
                "<table class='report'><thead><tr><th>Event</th>"
                "<th class='num'>Climbers</th></tr></thead><tbody>"
                + "".join(rows)
                + "</tbody></table>"
            )

        sections.append(
            f"<h3 class='text-base font-semibold mt-4 mb-1'>{_esc(b.header)} "
            f"<span class='muted font-normal'>({b.visitors:,} visitors)</span></h3>"
            + "".join(sub_tables)
        )

    return (
        "<section>"
        "<h2 class='text-lg font-semibold mb-3' id='what-pulls-visitors-up-your-staircase'>What pulls visitors up your staircase</h2>"
        "<p class='text-sm muted mb-3'>For each climbing transition, the "
        "prior-window channels that recruited the climbers, the current-window "
        "pages and sections they consumed, and the events they attended. Tells "
        "you what content and events to commission more of to keep visitors "
        "climbing.</p>"
        + "".join(sections)
        + "</section>"
    )


def _render_referral_sources_html(r: StaircaseResult, site_climb_rate: float) -> str:
    rows = _top_referral_sources(r.climb_yield_by_channel)
    if not rows:
        return ""
    body = []
    for domain, prior_n, climbers, rate in rows:
        ratio = rate / site_climb_rate if site_climb_rate > 0 else 0
        body.append(
            f"<tr><td><code>{_esc(domain)}</code></td>"
            f"<td class='num'>{prior_n:,}</td>"
            f"<td class='num'>{climbers:,}</td>"
            f"<td class='num'>{rate:.1%}</td>"
            f"<td class='num'>{ratio:.1f}×</td></tr>"
        )
    return (
        "<section>"
        "<h2 class='text-lg font-semibold mb-3' id='top-referral-sources'>Top referral sources</h2>"
        "<p class='text-sm muted mb-3'>Per-domain breakdown of referrer "
        "traffic (&ge;10 prior visitors), sorted by climb rate. Climb rate "
        "= of prior-window visitors who arrived from this domain, the "
        "fraction whose tier rank rose in the current window.</p>"
        "<table class='report'><thead><tr>"
        "<th>Source</th><th class='num'>Prior visitors</th>"
        "<th class='num'>Climbers</th><th class='num'>Climb rate</th>"
        "<th class='num'>vs site avg</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></section>"
    )


def _render_staircase_visual_svg(r: StaircaseResult) -> str:
    """Header staircase visual: three steps with current-window tier counts,
    plus climber callouts above each riser. Climbers are strict – a visitor
    counts only if they were in a lower tier in the prior window (SILENT
    excluded; new-at-tier visitors don't count)."""
    cur = r.tier_counts_current
    trans = r.transitions
    n_one = cur.get(ONE_TIME, 0)
    n_ret = cur.get(RETURNING, 0)
    n_bl = cur.get(BRAND_LOVER, 0)
    climbers_to_ret = trans.get((ONE_TIME, RETURNING), 0)
    climbers_to_bl = (
        trans.get((ONE_TIME, BRAND_LOVER), 0)
        + trans.get((RETURNING, BRAND_LOVER), 0)
    )
    return (
        "<svg viewBox='0 0 720 260' xmlns='http://www.w3.org/2000/svg' "
        "role='img' aria-label='Relationship staircase: one-time, returning, "
        "brand lover tiers with climbers between them' "
        "style='width:100%;height:auto;max-width:720px;display:block;margin:0 auto;' "
        "class='mt-4 mb-2'>"
        "<g>"
        "<rect x='60' y='190' width='180' height='50' rx='4' fill='#cbd5e1'/>"
        "<rect x='240' y='150' width='180' height='90' rx='4' fill='#94a3b8'/>"
        "<rect x='420' y='100' width='180' height='140' rx='4' fill='#475569'/>"
        "</g>"
        "<g font-family='ui-sans-serif, system-ui, sans-serif'>"
        f"<text x='150' y='213' text-anchor='middle' font-size='18' font-weight='700' fill='#0f172a'>{n_one:,}</text>"
        "<text x='150' y='229' text-anchor='middle' font-size='12' font-weight='600' fill='#1e293b'>"
        "One-time <tspan font-weight='400' opacity='0.7'>&middot; 1 visit</tspan></text>"
        f"<text x='330' y='184' text-anchor='middle' font-size='18' font-weight='700' fill='#ffffff'>{n_ret:,}</text>"
        "<text x='330' y='200' text-anchor='middle' font-size='12' font-weight='600' fill='#ffffff'>"
        "Returning <tspan font-weight='400' opacity='0.85'>&middot; 2&ndash;4 visits</tspan></text>"
        f"<text x='510' y='134' text-anchor='middle' font-size='18' font-weight='700' fill='#ffffff'>{n_bl:,}</text>"
        "<text x='510' y='150' text-anchor='middle' font-size='12' font-weight='600' fill='#ffffff'>"
        "Brand lover <tspan font-weight='400' opacity='0.85'>&middot; 5+ visits</tspan></text>"
        "</g>"
        "<g transform='translate(240, 110)' font-family='ui-sans-serif, system-ui, sans-serif'>"
        f"<text x='0' y='0' text-anchor='middle' font-size='13' font-weight='600' fill='#047857'>+{climbers_to_ret:,} climbed</text>"
        "<line x1='0' y1='8' x2='0' y2='35' stroke='#047857' stroke-width='2' stroke-linecap='round'/>"
        "<polyline points='-7,15 0,8 7,15' stroke='#047857' stroke-width='2' fill='none' stroke-linecap='round' stroke-linejoin='round'/>"
        "</g>"
        "<g transform='translate(420, 60)' font-family='ui-sans-serif, system-ui, sans-serif'>"
        f"<text x='0' y='0' text-anchor='middle' font-size='13' font-weight='600' fill='#047857'>+{climbers_to_bl:,} climbed</text>"
        "<line x1='0' y1='8' x2='0' y2='35' stroke='#047857' stroke-width='2' stroke-linecap='round'/>"
        "<polyline points='-7,15 0,8 7,15' stroke='#047857' stroke-width='2' fill='none' stroke-linecap='round' stroke-linejoin='round'/>"
        "</g>"
        "</svg>"
    )


def render_html(r: StaircaseResult) -> str:
    cur = r.tier_counts_current
    prior = r.tier_counts_prior
    trans = r.transitions

    total_prior, site_climb_rate = _baseline_climb_rate(r.climb_yield_by_channel)

    silent_from_bl = trans.get((BRAND_LOVER, SILENT), 0)
    site_climb_rate_for_callouts = (
        r.net_movement.climb_rate if r.net_movement else 0.0
    )
    callouts = _callouts(
        r.channel_climb_yield,
        site_climb_rate_for_callouts,
        silent_from_bl,
        internal_domains=r.internal_domains,
        silent_brand_lovers_count=len(r.silent_brand_lovers),
        climbed_count=len(r.climbed_to_brand_lover),
        cohort_paths=r.cohort_paths,
        join_id_key=r.join_id_key,
        join_id_populated=r.join_id_populated,
        at_risk_now=r.at_risk_now,
    )

    h1_suffix = f": {_esc(r.site_label)}" if r.site_label else ""
    header_html = (
        f"<header><h1 class='text-2xl font-semibold'>Relationship Staircase "
        f"Report{h1_suffix}</h1>"
        f"{_render_staircase_visual_svg(r)}"
        f"<p class='mt-2'>The Relationship Staircase Report surfaces the "
        f"signals, events, and content that deepen your relationship with "
        f"your audience. It segments visitors into one-time visitors, "
        f"returning visitors, and brand lovers &ndash; then highlights "
        f"what's moving people up the staircase. Relationship depth matters "
        f"because a brand lover is worth orders of magnitude more than a "
        f"one-time visitor &ndash; they consume more, convert more, and "
        f"retain longer. Optimizing for depth changes what you commission, "
        f"where you invest, and who you target next.</p>"
        f"<p class='text-sm muted mt-2'><strong>Current window:</strong> "
        f"{_fmt_window(r.current_window)} • <strong>Prior window:</strong> "
        f"{_fmt_window(r.prior_window)}"
    )
    if r.site_id_filter:
        header_html += f" • <strong>Site ID:</strong> <code>{_esc(r.site_id_filter)}</code>"
    header_html += "</p>"
    if r.coverage_note:
        header_html += (
            f"<p class='text-sm mt-2' style='padding:0.5rem 0.75rem;"
            f"border-left:3px solid #d97706;background:#fef3c7;color:#78350f;'>"
            f"<strong>Note on data coverage:</strong> {_esc(r.coverage_note)}</p>"
        )
    if r.employee_filter_key:
        val_repr = json.dumps(r.employee_filter_value)
        header_html += (
            f"<p class='text-sm mt-2' style='padding:0.5rem 0.75rem;"
            f"border-left:3px solid #2563eb;background:#dbeafe;color:#1e3a8a;'>"
            f"<strong>Employee filter active:</strong> events tagged with "
            f"<code>extra_data['{_esc(r.employee_filter_key)}'] = "
            f"{_esc(val_repr)}</code> are excluded. Run "
            f"<code>/agentic-analytics:identify-employees --clear</code> to remove."
            f"</p>"
        )
    header_html += "</header>"

    callouts_html = "<ul class='list-disc pl-6 space-y-2'>" + "".join(
        f"<li>{_md_bold_to_html(c)}</li>" for c in callouts
    ) + "</ul>"

    network_chan_html = f"{_esc(r.network_label)}, " if r.network_label else ""
    if r.join_id_key and r.join_id_populated:
        cohort_caveat_html = (
            f"<li><strong>Cohort CSVs</strong> key on "
            f"<code>visitor_site_id</code>, a Parsely first-party cookie "
            f"hash that's opaque to your CRM. Each row also carries the "
            f"<code>{_esc(r.join_id_key)}</code> value captured from the "
            f"URL of any email-campaign click &ndash; your per-recipient "
            f"join key. Upload a cohort CSV to your CRM, merge on "
            f"<code>{_esc(r.join_id_key)}</code>, and you have a "
            f"targetable list. The identifier can be per-recipient "
            f"(durable across sends) or per-send (unique per "
            f"campaign-recipient pair); either shape works.</li>"
        )
    else:
        cohort_caveat_html = (
            "<li><strong>Cohort CSVs</strong> key on "
            "<code>visitor_site_id</code>, a Parsely first-party cookie "
            "hash that's opaque to your CRM. To make cohorts "
            "email-actionable, add an opaque per-recipient identifier to "
            "URLs in your email campaigns (e.g. <code>?pid=&lt;id&gt;</code> "
            "&ndash; <code>pid</code> is just the default; any param "
            "name works), tell us the parameter name "
            "(<code>/agentic-analytics:init</code> will prompt, or edit "
            "<code>bucket.json</code> directly), and re-run "
            "<code>/staircase</code>. The ID column then appears in "
            "cohort CSVs alongside <code>visitor_site_id</code>, so you "
            "can merge results back into your CRM lists. The ID can be "
            "per-recipient (durable across sends) or per-send (unique "
            "per campaign-recipient pair); either shape works. No "
            "tracker setup is needed; the query string is already "
            "captured in the event's <code>url</code> field.</li>"
        )
    caveats_html = (
        "<ul class='list-disc pl-6 space-y-1'>"
        "<li><strong>Visit</strong> = a session. Two pageviews more than 30 "
        "minutes apart count as two visits.</li>"
        "<li><strong>Tiers</strong>: one-time = 1 visit in window; returning = "
        "2–4; brand lover = 5+. Tier is computed independently for each window.</li>"
        "<li><strong>Identity</strong> = <code>visitor_site_id</code> "
        "(Parsely first-party cookie hash). Cookie clears and browser "
        "switches inflate the one-time count and fragment a single human "
        "across multiple IDs, so climb rates are a lower bound: visitors "
        "who really did climb in the data can look like net-new strangers "
        "on the other side of a cookie reset.</li>"
        + cohort_caveat_html +
        "<li><strong>Channel</strong> = a GA-style grouping of the first "
        "pageview in the current window. Combines <code>utm_medium</code>, "
        "<code>utm_source</code>, and Parsely's <code>sref_category</code> "
        "into one bucket per visitor: "
        f"Email, Paid, {network_chan_html}"
        "Social Referrers, Search Referrers, AI Referrers, "
        "Referral: &lt;domain&gt;, Direct. "
        "Names like <em>Search Referrers</em> "
        "indicate the bucket was detected via referrer; Email and Paid are "
        "UTM-based. Visitors arriving via session continuation or "
        "cross-subdomain navigation are excluded from the channel mix.</li>"
        f"<li><strong>Coverage</strong>: {r.pageviews_loaded:,} pageviews across "
        f"{r.total_visitors_seen:,} unique visitor IDs in the union of both windows.</li>"
        "</ul>"
    )

    out = _HTML_TEMPLATE
    title_text = f"Relationship Staircase Report: {r.site_label}" if r.site_label else "Relationship Staircase Report"
    out = out.replace("__TITLE__", _esc(title_text))
    out = out.replace("__HEADER_HTML__", header_html)
    out = out.replace("__CLIMBING_HEADLINE_HTML__", _render_climbing_headline_html(r))
    out = out.replace("__TIERS_TABLE__", _render_tiers_table_html(r))
    out = out.replace("__WHO_MOVED_UP_HTML__", _render_who_moved_up_html(r))
    out = out.replace("__AT_RISK_NOW_HTML__", _render_at_risk_now_html(r))
    out = out.replace("__GONE_SILENT_HTML__", _render_gone_silent_html(r))
    site_climb_rate = r.net_movement.climb_rate if r.net_movement else 0.0
    if r.channel_climb_yield:
        channel_climb_yield_section = (
            "<section>"
            "<h2 class='text-lg font-semibold mb-3' id='channel-climb-yield'>"
            "Channel climb yield</h2>"
            "<p class='text-sm muted mb-3'>Of visitors whose first prior-window "
            "pageview was attributed to this acquisition channel, the fraction "
            "who climbed to a higher tier in the current window. Same transition "
            "math as the section and author tables below. Rows floored at "
            f"{CLIMB_YIELD_VOLUME_FLOOR} prior-window visitors. Site climb rate: "
            f"<strong>{site_climb_rate:.1%}</strong>.</p>"
            + _render_channel_climb_yield_html(
                r.channel_climb_yield, site_climb_rate,
            )
            + "</section>"
        )
    else:
        channel_climb_yield_section = ""
    out = out.replace(
        "__CHANNEL_CLIMB_YIELD_SECTION__",
        channel_climb_yield_section,
    )
    out = out.replace(
        "__SECTION_CLIMB_YIELD_SECTION__",
        _render_section_climb_yield_html(r),
    )
    out = out.replace(
        "__AUTHOR_CLIMB_YIELD_SECTION__",
        _render_author_climb_yield_html(r),
    )
    out = out.replace(
        "__REFERRAL_SOURCES_SECTION__",
        _render_referral_sources_html(r, site_climb_rate),
    )
    out = out.replace(
        "__STRONG_CHANNELS_SECTION__",
        _render_strong_channels_html(r.strong_channels, site_climb_rate),
    )
    out = out.replace(
        "__FIRST_TOUCH_CONTENT_SECTION__",
        _render_first_touch_content_html(r),
    )
    out = out.replace(
        "__SECOND_VISIT_CONTENT_SECTION__",
        _render_second_visit_content_html(r),
    )
    out = out.replace(
        "__RETURN_CADENCE_SECTION__",
        _render_return_cadence_html(r),
    )
    out = out.replace(
        "__MARKER_LIFT_SECTION__",
        _render_marker_lift_html(r),
    )
    out = out.replace(
        "__CLIMBING_SECTION__",
        _render_climbing_html(r.climbing_breakdowns),
    )
    out = out.replace("__CALLOUTS_HTML__", callouts_html)
    out = out.replace("__CAVEATS_HTML__", caveats_html)
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _parse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--events-dir", default=None,
                   help="Directory containing gzipped DPL event files (recursive). "
                        "Defaults to <cache-root>/<site-id>/ (see --cache-root).")
    p.add_argument("--cache-root", default=default_cache_root(),
                   help="Persistent cache root for DPL events. "
                        "Default: $XDG_CACHE_HOME/agentic-analytics/dpl "
                        "(falls back to ~/.cache/agentic-analytics/dpl).")
    p.add_argument("--current", nargs=2, metavar=("START", "END"), required=True,
                   help="Current window as YYYY-MM-DD YYYY-MM-DD (inclusive).")
    p.add_argument("--prior", nargs=2, metavar=("START", "END"), required=True,
                   help="Prior window as YYYY-MM-DD YYYY-MM-DD (inclusive).")
    p.add_argument(
        "--site-label", default="",
        help="Optional site label appended to the report header "
             "('Relationship Staircase Report: <label>'). Omit for a bare "
             "'Relationship Staircase Report' title – that's the default "
             "for plain customer installs. The dev wrapper and other "
             "callers can still pass a value when they want one.",
    )
    p.add_argument("--site-id", default=None,
                   help="Site ID filter (e.g. 'your-site.com'). Also drives the "
                        "default --events-dir. Omit only if --events-dir is set.")
    p.add_argument("--output-md", default=None, help="Output markdown file (omit to skip).")
    p.add_argument("--output-html", default=None, help="Output HTML file (omit to skip).")
    p.add_argument(
        "--internal-domains", default="",
        help="Comma-separated customer corp domains (e.g. 'your-corp.com'). "
             "`Referral: <domain>` channels matching these (or known corp-tool "
             "suffixes like microsoftonline.com / teams.microsoft.com / slack.com) "
             "are silently excluded from the recommendations loop. They still "
             "appear in the per-domain referral table.",
    )
    p.add_argument(
        "--network-label", default=None,
        help="Label for the customer's internal-network channel (e.g. "
             "'Internal Network'). When set, events whose utm_source or "
             "utm_medium matches one of --network-sources are bucketed under "
             "this channel instead of as a generic referral. Omit to disable.",
    )
    p.add_argument(
        "--network-sources", default="",
        help="Comma-separated values matched against utm_source OR utm_medium "
             "to drive the --network-label channel. Has no effect without "
             "--network-label.",
    )
    p.add_argument(
        "--data-coverage-note", default=None,
        help="One-line caveat about data coverage to render at the top of "
             "the report (e.g. 'only 26 days of data available; windows are "
             "13/13'). Appears in both the markdown and HTML output so it "
             "travels with shared reports.",
    )
    p.add_argument(
        "--employee-filter-key", default=None,
        help="extra_data key to exclude employee/internal traffic on (e.g. "
             "'Internal'). Events where extra_data[<key>] == "
             "--employee-filter-value are dropped. Configured by "
             "/agentic-analytics:identify-employees and read from bucket.json "
             "by the slash command; staircase.py just consumes it.",
    )
    p.add_argument(
        "--employee-filter-value", default=None,
        help="JSON-encoded value to filter on (e.g. 'true', '1', '\"true\"'). "
             "Decoded with json.loads and compared with == to "
             "extra_data[<key>]. Ignored unless --employee-filter-key is set.",
    )
    p.add_argument(
        "--join-id-key", default=None,
        help="URL query parameter the customer carries in email-campaign "
             "links to identify each recipient (e.g. 'pid'). When set, the "
             "report extracts that parameter from each event's url field "
             "and writes it as a column in the cohort CSVs so the customer "
             "can merge the cohort back into their CRM list. No Parse.ly-side "
             "tracker configuration is needed; the query string is already "
             "captured in the event's url field. Read from bucket.json by "
             "the slash command; staircase.py just consumes it.",
    )
    p.add_argument(
        "--cohort-dir", default=None,
        help="Directory to write cohort CSVs into. Defaults to the directory "
             "of --output-md or --output-html. Pass an empty string to skip "
             "cohort CSV generation entirely.",
    )
    p.add_argument(
        "--cohort-slug", default=None,
        help="Slug used in cohort CSV filenames "
             "(staircase-<slug>-cohort-<name>.csv). Defaults to the basename "
             "of --output-md or --output-html (without extension); falls back "
             "to the site-id when no output file is set.",
    )
    args = p.parse_args(argv)
    employee_filter_value: Any = None
    if args.employee_filter_key:
        if args.employee_filter_value is None:
            print("--employee-filter-key requires --employee-filter-value.", file=sys.stderr)
            return 1
        try:
            employee_filter_value = json.loads(args.employee_filter_value)
        except json.JSONDecodeError as e:
            print(f"--employee-filter-value must be valid JSON: {e}", file=sys.stderr)
            return 1
        if employee_filter_value is None:
            # Refuse json `null`: dict.get(missing_key) == None would match
            # every event lacking the key and silently empty the report.
            print("--employee-filter-value must not decode to null.", file=sys.stderr)
            return 1
    internal_domains = tuple(
        d.strip().lower() for d in args.internal_domains.split(",") if d.strip()
    )
    network_sources = tuple(
        s.strip().lower() for s in args.network_sources.split(",") if s.strip()
    )

    if args.events_dir:
        events_dir = args.events_dir
    elif args.site_id:
        events_dir = os.path.join(args.cache_root, args.site_id)
    else:
        print("Must pass --events-dir or --site-id (used to resolve the default cache path).",
              file=sys.stderr)
        return 1

    files = sorted(glob.glob(os.path.join(events_dir, "**", "*.gz"), recursive=True))
    if not files:
        print(
            f"No .gz files found under {events_dir}.\n"
            f"Cache convention: drop gzipped DPL events into {events_dir}/ "
            f"(or pass --events-dir).",
            file=sys.stderr,
        )
        return 1

    cur_start = dt.datetime.combine(_parse_date(args.current[0]), dt.time.min)
    cur_end = dt.datetime.combine(_parse_date(args.current[1]), dt.time.max)
    prior_start = dt.datetime.combine(_parse_date(args.prior[0]), dt.time.min)
    prior_end = dt.datetime.combine(_parse_date(args.prior[1]), dt.time.max)

    print(f"Loading {len(files):,} files from {events_dir} ...", file=sys.stderr)
    result = compute_staircase(
        files=files,
        current_window=(cur_start, cur_end),
        prior_window=(prior_start, prior_end),
        site_label=args.site_label,
        site_id_filter=args.site_id,
        internal_domains=internal_domains,
        network_label=args.network_label,
        network_sources=network_sources,
        employee_filter_key=args.employee_filter_key,
        employee_filter_value=employee_filter_value,
        join_id_key=args.join_id_key,
    )
    result.coverage_note = args.data_coverage_note
    print(
        f"  loaded {result.pageviews_loaded:,} pageviews, "
        f"{result.total_visitors_seen:,} visitors.",
        file=sys.stderr,
    )

    # Resolve cohort-CSV destination + slug from the output args. Empty
    # string for --cohort-dir is the documented skip signal.
    cohort_skip = args.cohort_dir == ""
    if not cohort_skip:
        cohort_dir = args.cohort_dir
        if cohort_dir is None:
            for ref in (args.output_md, args.output_html):
                if ref:
                    cohort_dir = os.path.dirname(os.path.abspath(ref)) or "."
                    break
        slug = args.cohort_slug
        if slug is None:
            for ref in (args.output_md, args.output_html):
                if ref:
                    slug = os.path.splitext(os.path.basename(ref))[0]
                    # Strip a leading "staircase-" so the cohort filename
                    # doesn't end up like staircase-staircase-<slug>-...
                    if slug.startswith("staircase-"):
                        slug = slug[len("staircase-"):]
                    break
            if slug is None:
                slug = args.site_id or "report"
        if cohort_dir:
            written = write_cohort_csvs(result, cohort_dir=cohort_dir, slug=slug)
            for path in written.values():
                print(f"Wrote cohort CSV: {path}", file=sys.stderr)
            if not written:
                print("No cohort CSVs written (all cohorts empty).", file=sys.stderr)

    if not args.output_md and not args.output_html:
        # Default: markdown to stdout.
        print(render_markdown(result))
        return 0

    if args.output_md:
        with open(args.output_md, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(result) + "\n")
        print(f"Wrote {args.output_md}", file=sys.stderr)
    if args.output_html:
        with open(args.output_html, "w", encoding="utf-8") as fh:
            fh.write(render_html(result))
        print(f"Wrote {args.output_html}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
