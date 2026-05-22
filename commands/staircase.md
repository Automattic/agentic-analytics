---
description: Run the audience-relationship (staircase) report against the configured Parse.ly bucket. Optional site ID argument filters to a single site within the bucket; without one, the report runs across every site in the bucket. Outputs the report markdown only, with a file:// link to the HTML version at the bottom.
argument-hint: "[<site-id>] (optional; leave blank to run against all sites in your bucket)"
---

Run the staircase audience-relationship report.

Customer's optional site ID filter from `$ARGUMENTS`. Empty means "run across all sites in the configured bucket"; non-empty means "filter to that one site (e.g. `your-site.com`)."

**Output ONLY the report contents** (the markdown body of the generated report file) followed by a single `file://` link line at the end. NO preamble. NO postscript. NO commentary. NO explanation. The Recommendations section at the top of the report IS the analysis. Don't re-narrate it.

You are a pipe, not an analyst. The customer reads the report directly.

**This rule applies to conversational invocations too.** When the user asks for "the staircase report" / "run the report" / "run the staircase report for <site-id>" without typing the slash command, follow the same contract: render the markdown report verbatim in chat, then the `file://` HTML link, no narration. A summary instead of the report is the wrong shape. The user wants to read the report in the terminal. The HTML version is allowed to carry more detail than the in-chat markdown (e.g., the collapsible full-tail referrer list); that asymmetry is intentional, don't try to mirror everything into the chat output.

## Steps

1. **Load the bucket config and resolve the site ID filter.** Read `${XDG_CONFIG_HOME:-~/.config}/agentic-analytics/bucket.json`, written by `/agentic-analytics:init`. It contains:

   - `bucket`: S3 bucket holding the customer's DPL events.
   - `profile`: AWS profile to authenticate with.
   - `cache_dir`: subdirectory under `${XDG_CACHE_HOME:-~/.cache}/agentic-analytics/dpl/` for the events cache (e.g. `acme`).
   - `employee_filter` (optional): `{ "extra_data_key": "...", "extra_data_value": ... }`. Written by `/agentic-analytics:identify-employees` when an unambiguous employee-traffic tag was detected. If present, pass it through to `staircase.py` (step 3); the report excludes those events and renders a one-line note up top.
   - `join_id_key` (optional): the URL query parameter the customer carries in email-campaign links to identify each recipient (e.g. `"pid"`). When set, cohort CSVs gain a column with that name, populated from the query string of any pageview where the parameter appears. The customer joins the cohort back to their CRM list on that column. If present, pass it through to `staircase.py` (step 3) as `--join-id-key <value>`.

   If the file is missing, tell the user to run `/agentic-analytics:init` first and stop.

   `$ARGUMENTS` is the optional site ID filter:

   - If empty: the report runs across every site in the bucket. Pass NO `--site-id` flag to `staircase.py`.
   - If non-empty: filter events to that site. Pass `--site-id <value>` to `staircase.py`. The site ID itself shows up in the report's `Site ID filter:` line, so the report stays identifiable without a separate header label.

2. **Try to ensure a full 60 days of coverage, then compute windows from what's actually on disk.** The DPL pull is incremental, and a customer's cache may start well after the date the slash command was invoked. Using a fixed `today - 30d` window can land the prior window entirely before the data starts, producing a report where every prior count is 0 and every row is labeled "new". Always anchor on what's on disk.

   First, attempt to pull the target 60-day range. `pull_dpl.py` is incremental: it checks the local cache for each day and only fetches missing days, so already-cached customers pay milliseconds, fresh onboards pay the full pull once.

   ```bash
   cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/agentic-analytics/dpl/<cache_dir>"
   # Target range: 60 days ending yesterday. Use Python for cross-platform date math.
   read target_start target_end < <(python3 -c "
   from datetime import date, timedelta
   start = date.today() - timedelta(days=60)
   end = date.today() - timedelta(days=1)
   print(start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
   ")
   python3 "${CLAUDE_PLUGIN_ROOT:-plugin}/skills/staircase-report/scripts/pull_dpl.py" \
     --cache-dir <cache_dir> --bucket <bucket> --profile <profile> \
     --start "$target_start" --end "$target_end" || true
   ```

   The trailing `|| true` is intentional: don't fail the report on auth lapse, S3 hiccup, or no-such-day. Fall through to whatever's cached. `pull_dpl.py` defaults `--prefix` to `events/`, which matches every Parse.ly customer bucket; no need to pass it explicitly.

   Then probe the cache for days with at least one `.gz` file (empty directories from partial pulls don't count) and split the available span into two equal-size windows:

   ```bash
   days=$(find "$cache_dir" -name '*.gz' -type f 2>/dev/null | \
     sed -E "s|.*/([0-9]{4})/([0-9]{2})/([0-9]{2})/.*|\1-\2-\3|" | sort -u)
   if [ -z "$days" ]; then echo "ERROR: no .gz files found under $cache_dir" >&2; exit 1; fi
   earliest=$(echo "$days" | head -1)
   latest=$(echo "$days" | tail -1)

   # Compute window in Python for cross-platform date arithmetic.
   # Prefer 30/30; fall back to N/N where 2N fits the available span.
   read available window current_start current_end prior_start prior_end < <(python3 -c "
   from datetime import date, timedelta
   import sys
   earliest = date.fromisoformat('$earliest')
   latest = date.fromisoformat('$latest')
   available = (latest - earliest).days + 1
   window = 30 if available >= 60 else available // 2
   if window < 1:
       sys.stderr.write(f'ERROR: not enough cache coverage (only {available} day(s))\n')
       sys.exit(1)
   current_end = latest
   current_start = latest - timedelta(days=window - 1)
   prior_end = latest - timedelta(days=window)
   prior_start = prior_end - timedelta(days=window - 1)
   print(available, window, current_start, current_end, prior_start, prior_end)
   ")
   if [ -z "$window" ] || [ "$window" -lt 1 ]; then exit 1; fi
   echo "available=$available days  window=$window  current=$current_start..$current_end  prior=$prior_start..$prior_end"
   ```

   If `window < 30` after the pull attempt, build a one-line coverage caveat string and pass it as `--data-coverage-note` in the next step. It renders into the report markdown and HTML so it travels with shared reports. Don't add chat-only narration on top. Example:

   ```bash
   note=""
   if [ "$window" -lt 30 ]; then
     note="Only $available days of data were available, so windows are ${window}/${window} days instead of the usual 30/30. Trends from shorter windows are noisier."
   fi
   ```

3. **Run the report.** Slug the output filename from `cache_dir` plus the site ID filter (or `all` when none):

   ```bash
   slug="<cache_dir>$([ -n "<site-id>" ] && echo "-<site-id>" || echo "-all")"
   python3 "${CLAUDE_PLUGIN_ROOT:-plugin}/skills/staircase-report/scripts/staircase.py" \
     --events-dir "${XDG_CACHE_HOME:-$HOME/.cache}/agentic-analytics/dpl/<cache_dir>" \
     [--site-id <site-id>] \
     --current <current_start> <current_end> \
     --prior <prior_start> <prior_end> \
     [--internal-domains <corp-domain>] \
     [--network-label "<network-label>" --network-sources "<network-sources>"] \
     [--data-coverage-note "$note"] \
     [--employee-filter-key <key> --employee-filter-value <json-value>] \
     [--join-id-key <key>] \
     --output-md /tmp/staircase-$slug.md \
     --output-html /tmp/staircase-$slug.html
   ```

   `--site-label` is omitted in the public flow – reports just say "Relationship Staircase Report" at the top. Callers that want a labeled header (the dev wrapper) can still pass `--site-label "<value>"`.

   `--network-label` and `--network-sources` are paired: pass both or neither. They classify cross-promotion traffic from the customer's own properties under a named channel. Source values for the customer come from the wrapper (when invoked via `/staircase`) or future plugin config; the public init flow doesn't set them.

   `--internal-domains` only applies when the wrapper supplies a corp domain or the user has configured one. Plain customer installs typically omit it.

   `--employee-filter-key` and `--employee-filter-value` are paired: pass both or neither. Read from `bucket.json["employee_filter"]` when present (step 1). The key passes through as-is. The value must be JSON-encoded on the command line: a Python boolean becomes `true` or `false`, a string gets wrapped in quotes (e.g. `'"yes"'` with single-quoted shell escaping), and numbers stay bare (`1`). `staircase.py` decodes it with `json.loads` and compares with `==` against `extra_data[<key>]`. When set, matching events are excluded from the report and a one-line note appears at the top.

   Pass `--site-id` only when `$ARGUMENTS` is non-empty. Pass `--data-coverage-note` only when `$note` is non-empty (i.e. windows are short). Pass `--employee-filter-*` only when `employee_filter` is present in `bucket.json`. Pass `--join-id-key` only when `join_id_key` is present in `bucket.json` (this flag is plumbing between the slash command and the script; customers don't type it).

4. **Output the report**: read `/tmp/staircase-$slug.md` and emit its contents verbatim. Don't add formatting, don't add framing.

5. **Append the HTML link** as the last line, with one blank line separating it from the report body:

   ```
   [Open the HTML version in browser](file:///tmp/staircase-$slug.html)
   ```

   The `file://` protocol matters: a bare path can be intercepted by an editor.

## Constraints (re-stated for emphasis)

- No "Running staircase for…" preamble.
- No "Two things stand out…" / "Let me know if…" postscript.
- No re-narration of the report's findings.
- The pull-then-probe step in §2 handles short caches. Pass the caveat via `--data-coverage-note` so it lands inside the report itself (visible in both the in-chat verbatim render and the shared HTML). Don't add chat-only narration on top of that.
- If `bucket.json` is missing, tell the user to run `/agentic-analytics:init` first; do not try to guess values.
- If the cache directory doesn't exist at all for the configured bucket, surface that as an error (one short line) and stop.
