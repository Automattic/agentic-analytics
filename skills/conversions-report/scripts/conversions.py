#!/usr/bin/env python3
"""Conversion attribution code lifted out of the staircase report.

Lifted from `plugin/skills/staircase-report/scripts/staircase.py` at git tag
`pre-staircase-climbing-revision`. The staircase report is being recentered
on climbing tier transitions; conversion-shaped analysis moves here.

This file is a **seed**, not a runnable report. It holds the symbols the
old staircase report used (`ConversionGroup`, `ConversionPaths`,
`compute_conversion_paths`, the renderers, the value-tier heuristic, the
sticky-unconverted cohort) so they survive the deletion that PR C will
make to staircase.py, and so PR-C reviewers can verify "moved, not lost"
with a side-by-side diff.

A future PR (tracked separately) will reshape this into a standalone
conversions report with its own CLI, end-to-end pipeline, and renderer.
Until then:

- Shared helpers (acquisition_channel, _page_for, _section_for, _short_page,
  _esc, _delta_html, the cohort builder primitive, tier constants) are
  imported from `staircase.py` via a sys.path shim. That coupling goes
  away when this file is reshaped into a real report.
- The renderers take a `StaircaseResult` (typed via TYPE_CHECKING) because
  that's the shape the lifted code currently consumes. The reshape will
  give this report its own result dataclass.
"""
from __future__ import annotations

import dataclasses
import os
import sys
from collections import Counter, defaultdict
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TYPE_CHECKING,
)

_STAIRCASE_SCRIPTS = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        os.pardir,
        os.pardir,
        "staircase-report",
        "scripts",
    )
)
if _STAIRCASE_SCRIPTS not in sys.path:
    sys.path.insert(0, _STAIRCASE_SCRIPTS)

from staircase import (  # noqa: E402
    BRAND_LOVER,
    ONE_TIME,
    RETURNING,
    SILENT,
    CohortMember,
    _cohort_member_from_event,
    _delta_html,
    _esc,
    _page_for,
    _section_for,
    _short_page,
    acquisition_channel,
)

if TYPE_CHECKING:
    from staircase import StaircaseResult


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclasses.dataclass
class ConversionGroup:
    conversion_type: Optional[str]
    conversion_label: Optional[str]
    current_count: int
    prior_count: int
    top_pages: List[Tuple[str, int]]  # (page, count) over both windows

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ConversionPaths:
    """Upstream attribution for one high-value conversion type in the current
    window: each conversion joined to its visitor's first pageview in that
    same window, aggregated by that pageview's channel, landing page, and
    section. `unmatched` covers conversions that couldn't be attributed –
    visitor has no first pageview in the window, no `visitor_site_id` on
    the event, or visitor was filtered as a bot – all excluded from the
    three aggregations. Section rows carry a fallback flag so the renderer
    can label URL-derived rows distinctly."""
    conversion_type: Optional[str]
    conversion_label: Optional[str]
    total_current: int
    unmatched: int
    by_channel: List[Tuple[str, int]]
    by_landing: List[Tuple[str, int]]
    by_section: List[Tuple[str, int, bool]]


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------


CONVERSION_PATH_LANDING_FLOOR = 5
CONVERSION_PATH_SECTION_FLOOR = 5
CONVERSION_PATH_TOP_N = 10


# Default conversion-value heuristic. The fallback when no per-customer
# override is supplied:
# - lead_capture and newsletter_signup represent committed audience signals
#   (handing over an email or filling a form); high-value by default.
# - link_click is mostly navigation/exit clicks; low-value.
# - Everything else (including untyped conversions) defaults to medium.
DEFAULT_CONVERSION_VALUE = {
    "lead_capture": "high",
    "newsletter_signup": "high",
    "link_click": "low",
}


# --------------------------------------------------------------------------
# Conversion-paths attribution
# --------------------------------------------------------------------------


def compute_conversion_paths(
    current_conv_events: Iterable[Tuple[str, Optional[str], Optional[str]]],
    by_visitor: Mapping[str, Mapping],
    *,
    bot_visitors: Optional[set] = None,
    network_label: Optional[str] = None,
    network_sources: Tuple[str, ...] = (),
    type_filter: Optional[Iterable[Optional[str]]] = None,
    landing_floor: int = CONVERSION_PATH_LANDING_FLOOR,
    section_floor: int = CONVERSION_PATH_SECTION_FLOOR,
    top_n: int = CONVERSION_PATH_TOP_N,
) -> List[ConversionPaths]:
    """Join each in-current-window conversion to its visitor's first pageview
    in that same window, then aggregate by that pageview's channel, landing
    page, and section.

    `current_conv_events` is an iterable of `(vsid, conv_type, conv_label)`
    tuples (one per conversion event in the current window).

    `by_visitor` is the visitor pivot built in `compute_staircase()`; we
    only read each bucket's `first_cur_ev`.

    `type_filter`, when provided, restricts output to conversion types whose
    `conv_type` is in the set (used to surface only high-value types).

    `bot_visitors` are routed to the unmatched bucket: they count toward the
    type's total (so the displayed Y matches the rest of the report) but
    don't shape any of the three attribution aggregations.

    Returns one `ConversionPaths` per (conv_type, conv_label) seen, sorted
    by total volume desc. Channel rows skip zero counts; landing and section
    rows are floored at `landing_floor` / `section_floor` and trimmed to
    `top_n`."""
    bots = bot_visitors or set()
    type_filter_set = set(type_filter) if type_filter is not None else None

    groups: Dict[
        Tuple[Optional[str], Optional[str]],
        Dict[str, Any],
    ] = defaultdict(lambda: {
        "total": 0,
        "unmatched": 0,
        "channels": Counter(),
        "landings": Counter(),
        "sections": Counter(),
    })

    for vsid, ctype, clabel in current_conv_events:
        if type_filter_set is not None and ctype not in type_filter_set:
            continue
        g = groups[(ctype, clabel)]
        g["total"] += 1
        if vsid in bots:
            g["unmatched"] += 1
            continue
        bucket = by_visitor.get(vsid)
        first_ev = bucket.get("first_cur_ev") if bucket else None
        if first_ev is None:
            g["unmatched"] += 1
            continue
        chan = acquisition_channel(
            first_ev,
            network_label=network_label,
            network_sources=network_sources,
        )
        if chan is not None:
            g["channels"][chan] += 1
        g["landings"][_page_for(first_ev)] += 1
        g["sections"][_section_for(first_ev)] += 1

    out: List[ConversionPaths] = []
    for (ctype, clabel), g in groups.items():
        channel_rows = [
            (chan, n) for chan, n in g["channels"].most_common() if n > 0
        ]
        landing_rows = [
            (page, n)
            for page, n in g["landings"].most_common()
            if n >= landing_floor
        ][:top_n]
        section_rows = [
            (label, n, is_fallback)
            for (label, is_fallback), n in g["sections"].most_common()
            if n >= section_floor
        ][:top_n]
        out.append(ConversionPaths(
            conversion_type=ctype,
            conversion_label=clabel,
            total_current=g["total"],
            unmatched=g["unmatched"],
            by_channel=channel_rows,
            by_landing=landing_rows,
            by_section=section_rows,
        ))
    out.sort(key=lambda c: -c.total_current)
    return out


# --------------------------------------------------------------------------
# Sticky-but-unconverted strong-channel cohort
# --------------------------------------------------------------------------


def _build_cohort_sticky_unconverted(
    visitor_meta: Mapping[str, Mapping[str, Any]],
    strong_channels: Sequence[str],
    converters_current: Iterable[str],
) -> List[CohortMember]:
    """Visitors who arrived via a strong-stickiness channel in the current
    window but did NOT fire a conversion. Highest-priority targets to push
    over the line."""
    strong = set(strong_channels)
    converters = set(converters_current)
    out: List[CohortMember] = []
    for vsid, m in visitor_meta.items():
        if m["current_tier"] == SILENT:
            continue
        if m["channel"] not in strong:
            continue
        if vsid in converters:
            continue
        row = _cohort_member_from_event(
            vsid,
            m["last_cur_ev"],
            m["last_cur_ts"],
            m["lifetime_pageviews"],
            arrival_channel=m["channel"],
        )
        if row is not None:
            out.append(row)
    out.sort(key=lambda r: r.last_engaged_at, reverse=True)
    return out


# --------------------------------------------------------------------------
# Value-tier heuristic and rollup
# --------------------------------------------------------------------------


def _conversion_value_tier(c: "ConversionGroup") -> str:
    """Return 'high' / 'medium' / 'low' for a conversion group.

    Default heuristic keyed by conversion_type; customers can override per
    label via their own config when that surface lands.
    """
    return DEFAULT_CONVERSION_VALUE.get(c.conversion_type or "", "medium")


def _conversion_rollup(
    conversions: List["ConversionGroup"],
) -> Dict[str, Dict[str, int]]:
    """Aggregate conversions by value tier (high/medium/low) so the section
    can lead with a category summary instead of every individual label."""
    rollup: Dict[str, Dict[str, int]] = {
        tier: {"count": 0, "current": 0, "prior": 0}
        for tier in ("high", "medium", "low")
    }
    for c in conversions:
        tier = _conversion_value_tier(c)
        rollup[tier]["count"] += 1
        rollup[tier]["current"] += c.current_count
        rollup[tier]["prior"] += c.prior_count
    return rollup


# --------------------------------------------------------------------------
# High-value conversion callouts
# --------------------------------------------------------------------------


def _high_value_conversion_callouts(conversions: List["ConversionGroup"]) -> List[str]:
    """Generate specific callouts for high-value conversion changes.

    Surfaces meaningful changes (>=20% delta or >=10 absolute count) on
    high-value conversion types. Each callout names the top contributing
    page so the customer has somewhere concrete to start. If high-value
    conversions are present but none changed meaningfully, surface a
    'held steady' callout: when lead-capture events are the customer's
    top-value signal, steady IS the headline.
    """
    out: List[str] = []
    high_value_seen = False
    for c in conversions:
        if _conversion_value_tier(c) != "high":
            continue
        high_value_seen = True
        cur, prior = c.current_count, c.prior_count
        if cur == 0 and prior == 0:
            continue
        change = abs(cur - prior)
        pct_abs = abs((cur - prior) / prior * 100) if prior else 100.0
        if pct_abs < 20 and change < 10:
            continue
        label = c.conversion_label or c.conversion_type or "(unlabeled)"
        if prior == 0:
            pct_str = "new"
        else:
            pct = (cur - prior) / prior * 100
            pct_str = f"{pct:+.0f}%"
        top_page_clause = ""
        if c.top_pages:
            top_page, top_n = c.top_pages[0]
            top_page_clause = (
                f" Top contributor across both windows: `{_short_page(top_page)}` "
                f"({top_n:,} events)."
            )
        if cur > prior:
            verb = "rose to"
            action = (
                "Check whether that page got a campaign, a redesign, or a traffic "
                "spike, and replicate the move on the next-closest pages. "
                "See *Conversion paths* below for the upstream channel mix."
            )
        elif cur < prior:
            verb = "dropped to"
            action = (
                "Start there: check whether the page changed (form removed, "
                "URL moved, layout regression) or whether upstream traffic to "
                "it fell. See *Conversion paths* below for which channel cooled."
            )
        else:
            verb = "held at"
            action = "Steady high-value signal; protect what's working."
        out.append(
            f"**{label} {verb} {cur:,} ({pct_str} vs {prior:,} prior).**"
            f"{top_page_clause} {action}"
        )

    if high_value_seen and not out:
        out.append(
            "**High-value conversions held steady this period.** "
            "Pardot Form Submissions, Newsletter Signups, and similar "
            "lead-capture events didn't move materially. When lead capture "
            "is your top-value signal, steady IS the headline – protect it."
        )
    return out


# --------------------------------------------------------------------------
# Markdown renderers
# --------------------------------------------------------------------------


def _conversion_paths_heading(cp: ConversionPaths) -> str:
    label = cp.conversion_label or cp.conversion_type or "(unlabeled)"
    return f"{label} ({cp.total_current:,} conversions)"


def _render_conversion_paths_md(lines: List[str], r: "StaircaseResult") -> None:
    """Append the 'Conversion paths' section to `lines`. No-op when no
    high-value conversion type matched in the current window – the section
    is upstream attribution for high-value conversions, so without those
    there's nothing to attribute."""
    paths = [p for p in r.conversion_paths if p.total_current > 0]
    if not paths:
        return

    lines.append("## Conversion paths")
    lines.append("")
    lines.append(
        "_For each high-value conversion, where it came from: the channel, "
        "landing page, and section of the visitor's first pageview in the "
        "current window. Names the upstream levers._"
    )
    lines.append("")

    for cp in paths:
        lines.append(f"### {_conversion_paths_heading(cp)}")
        lines.append("")

        matched = cp.total_current - cp.unmatched

        lines.append("**By upstream channel:**")
        lines.append("")
        lines.append("| Channel | Conversions | Share |")
        lines.append("|---|---:|---:|")
        if cp.by_channel and matched > 0:
            for chan, n in cp.by_channel:
                share = n / matched * 100
                lines.append(f"| {chan} | {n:,} | {share:.0f}% |")
        else:
            lines.append("| _(no attributable channels)_ | – | – |")
        lines.append("")

        lines.append(
            f"**By landing page** (first pageview in window, ≥ "
            f"{CONVERSION_PATH_LANDING_FLOOR} conversions):"
        )
        lines.append("")
        if cp.by_landing:
            lines.append("| Landing page | Conversions |")
            lines.append("|---|---:|")
            for page, n in cp.by_landing:
                lines.append(f"| `{_short_page(page)}` | {n:,} |")
        else:
            lines.append(
                f"_No landing page reached the {CONVERSION_PATH_LANDING_FLOOR}"
                f"-conversion floor._"
            )
        lines.append("")

        lines.append(
            f"**By section** (first pageview in window, ≥ "
            f"{CONVERSION_PATH_SECTION_FLOOR} conversions):"
        )
        lines.append("")
        if cp.by_section:
            lines.append("| Section | Conversions |")
            lines.append("|---|---:|")
            for label, n, is_fallback in cp.by_section:
                display = f"(by URL: {label})" if is_fallback else label
                lines.append(f"| {display} | {n:,} |")
        else:
            lines.append(
                f"_No section reached the {CONVERSION_PATH_SECTION_FLOOR}"
                f"-conversion floor._"
            )
        lines.append("")

        lines.append(
            f"_{cp.unmatched:,} of {cp.total_current:,} conversions had no "
            f"matching first pageview in the window and are excluded from "
            f"this attribution._"
        )
        lines.append("")


# --------------------------------------------------------------------------
# HTML renderers
# --------------------------------------------------------------------------


def _render_conversions_html(r: "StaircaseResult") -> str:
    if not r.conversions:
        return (
            "<p class='muted'>No conversion events fired in either window. "
            "If the site owner expects conversions to be tracked, check the "
            "Parsely conversion tracking setup.</p>"
        )

    rollup = _conversion_rollup(r.conversions)

    intro = (
        "<p class='text-sm muted mb-3'>Grouped by default value heuristic "
        "(<strong>high</strong> = lead capture / signups; <strong>low</strong> "
        "= navigation / link clicks; <strong>medium</strong> = everything else). "
        "High-value events are surfaced first; the full per-label list is at "
        "the bottom of this section.</p>"
    )

    rollup_rows = []
    for tier_key, tier_name in (
        ("high", "High value"),
        ("medium", "Medium value"),
        ("low", "Low value"),
    ):
        stats = rollup[tier_key]
        if stats["count"] == 0 and stats["current"] == 0 and stats["prior"] == 0:
            continue
        rollup_rows.append(
            f"<tr><td>{tier_name}</td>"
            f"<td class='num'>{stats['count']}</td>"
            f"<td class='num'>{stats['current']:,}</td>"
            f"<td class='num'>{stats['prior']:,}</td>"
            f"<td class='num'>{_delta_html(stats['current'], stats['prior'])}</td></tr>"
        )
    rollup_html = (
        "<table class='report'><thead><tr>"
        "<th>Value tier</th><th class='num'># labels</th>"
        "<th class='num'>Current</th><th class='num'>Prior</th>"
        "<th class='num'>Change</th></tr></thead><tbody>"
        + "".join(rollup_rows)
        + "</tbody></table>"
    )

    high_value = [c for c in r.conversions if _conversion_value_tier(c) == "high"]
    high_value_html = ""
    if high_value:
        rows = []
        for c in high_value:
            clabel = c.conversion_label or "<em class='muted'>(unlabeled)</em>"
            pages = "<br/>".join(
                f"<code>{_esc(_short_page(p))}</code> <span class='muted'>({n})</span>"
                for p, n in c.top_pages
            ) or "<span class='muted'>&ndash;</span>"
            rows.append(
                f"<tr><td>{clabel}</td>"
                f"<td class='num'>{c.current_count:,}</td>"
                f"<td class='num'>{c.prior_count:,}</td>"
                f"<td class='num'>{_delta_html(c.current_count, c.prior_count)}</td>"
                f"<td>{pages}</td></tr>"
            )
        high_value_html = (
            "<h3 class='text-base font-semibold mt-4 mb-2'>High-value conversions</h3>"
            "<table class='report'><thead><tr>"
            "<th>Label</th><th class='num'>Current</th>"
            "<th class='num'>Prior</th><th class='num'>Change</th>"
            "<th>Top pages</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )

    detail_rows = []
    for c in r.conversions:
        ctype = c.conversion_type or "<em class='muted'>(untyped)</em>"
        clabel = c.conversion_label or "<em class='muted'>(unlabeled)</em>"
        pages = "<br/>".join(
            f"<code>{_esc(_short_page(p))}</code> <span class='muted'>({n})</span>"
            for p, n in c.top_pages
        ) or "<span class='muted'>&ndash;</span>"
        detail_rows.append(
            f"<tr><td>{ctype}</td><td>{clabel}</td>"
            f"<td class='num'>{c.current_count}</td>"
            f"<td class='num'>{c.prior_count}</td>"
            f"<td>{pages}</td></tr>"
        )
    detail_html = (
        "<details class='mt-4'><summary class='cursor-pointer text-sm font-semibold'>"
        "All conversions detail (full list)</summary>"
        "<table class='report mt-2'><thead><tr><th>Type</th><th>Label</th>"
        "<th class='num'>Current</th><th class='num'>Prior</th>"
        "<th>Top pages</th></tr></thead><tbody>"
        + "".join(detail_rows)
        + "</tbody></table></details>"
    )

    return intro + rollup_html + high_value_html + detail_html


def _render_conversion_paths_html(r: "StaircaseResult") -> str:
    paths = [p for p in r.conversion_paths if p.total_current > 0]
    if not paths:
        return ""
    parts: List[str] = [
        "<section>",
        "<h2 class='text-lg font-semibold mb-3'>Conversion paths</h2>",
        "<p class='text-sm muted mb-3'>For each high-value conversion, where "
        "it came from: the channel, landing page, and section of the "
        "visitor's first pageview in the current window. Names the upstream "
        "levers.</p>",
    ]

    for cp in paths:
        label = cp.conversion_label or cp.conversion_type or "(unlabeled)"
        parts.append(
            f"<h3 class='text-base font-semibold mt-4 mb-2'>{_esc(label)} "
            f"<span class='muted font-normal'>({cp.total_current:,} "
            f"conversions)</span></h3>"
        )

        matched = cp.total_current - cp.unmatched

        parts.append(
            "<h4 class='text-sm font-semibold mt-3 mb-1'>By upstream channel</h4>"
        )
        if cp.by_channel and matched > 0:
            chan_rows = []
            for chan, n in cp.by_channel:
                share = n / matched * 100
                chan_rows.append(
                    f"<tr><td>{_esc(chan)}</td>"
                    f"<td class='num'>{n:,}</td>"
                    f"<td class='num'>{share:.0f}%</td></tr>"
                )
            parts.append(
                "<table class='report'><thead><tr>"
                "<th>Channel</th><th class='num'>Conversions</th>"
                "<th class='num'>Share</th></tr></thead><tbody>"
                + "".join(chan_rows)
                + "</tbody></table>"
            )
        else:
            parts.append(
                "<p class='text-sm muted'>No attributable channels.</p>"
            )

        parts.append(
            f"<h4 class='text-sm font-semibold mt-3 mb-1'>By landing page "
            f"<span class='muted font-normal'>(first pageview in window, "
            f"&ge; {CONVERSION_PATH_LANDING_FLOOR} conversions)</span></h4>"
        )
        if cp.by_landing:
            land_rows = [
                f"<tr><td><code>{_esc(_short_page(p))}</code></td>"
                f"<td class='num'>{n:,}</td></tr>"
                for p, n in cp.by_landing
            ]
            parts.append(
                "<table class='report'><thead><tr>"
                "<th>Landing page</th><th class='num'>Conversions</th>"
                "</tr></thead><tbody>"
                + "".join(land_rows)
                + "</tbody></table>"
            )
        else:
            parts.append(
                f"<p class='text-sm muted'>No landing page reached the "
                f"{CONVERSION_PATH_LANDING_FLOOR}-conversion floor.</p>"
            )

        parts.append(
            f"<h4 class='text-sm font-semibold mt-3 mb-1'>By section "
            f"<span class='muted font-normal'>(first pageview in window, "
            f"&ge; {CONVERSION_PATH_SECTION_FLOOR} conversions)</span></h4>"
        )
        if cp.by_section:
            sec_rows = []
            for sec_label, n, is_fallback in cp.by_section:
                display = (
                    f"(by URL: <code>{_esc(sec_label)}</code>)"
                    if is_fallback
                    else _esc(sec_label)
                )
                sec_rows.append(
                    f"<tr><td>{display}</td>"
                    f"<td class='num'>{n:,}</td></tr>"
                )
            parts.append(
                "<table class='report'><thead><tr>"
                "<th>Section</th><th class='num'>Conversions</th>"
                "</tr></thead><tbody>"
                + "".join(sec_rows)
                + "</tbody></table>"
            )
        else:
            parts.append(
                f"<p class='text-sm muted'>No section reached the "
                f"{CONVERSION_PATH_SECTION_FLOOR}-conversion floor.</p>"
            )

        parts.append(
            f"<p class='text-sm muted mt-2'><em>{cp.unmatched:,} of "
            f"{cp.total_current:,} conversions had no matching first "
            f"pageview in the window and are excluded from this "
            f"attribution.</em></p>"
        )

    parts.append("</section>")
    return "".join(parts)
