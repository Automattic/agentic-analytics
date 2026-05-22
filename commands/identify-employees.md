---
description: Detect whether the configured customer's tracker tags employee/internal pageviews via `extra_data` (e.g., `extra_data['Internal'] = true`) and save the filter to `bucket.json` so audience reports exclude that traffic. Idempotent. Auto-applies only on unambiguous matches; otherwise a silent no-op.
argument-hint: "[--clear] (optional; pass --clear to remove an existing filter without re-detecting)"
---

# Identify employee traffic

Some Parse.ly customers tag their employees' pageviews in the tracker via `extra_data` (e.g., `extra_data['Internal'] = true` set on logged-in users). Parse.ly's dashboard uses that tag for segment filtering, but the DPL stream is raw – tagged events still arrive. This command finds that tag (when present and unambiguous) and saves it to `bucket.json`. Subsequent staircase runs apply the filter automatically and surface a one-line note in the report.

Customers who use Parse.ly's account-level IP blocklist instead (the more common pattern) need nothing from this command – their DPL events are already filtered upstream.

## Output discipline

Terse. The detection script prints its own one-line result; output it verbatim and stop.

## Steps

1. **Load bucket config.** Read `${XDG_CONFIG_HOME:-~/.config}/agentic-analytics/bucket.json`. It contains:

   - `bucket` – S3 bucket holding the customer's DPL events.
   - `cache_dir` – subdirectory under `${XDG_CACHE_HOME:-~/.cache}/agentic-analytics/dpl/` (e.g. `acme`).

   If the file is missing, tell the user to run `/agentic-analytics:init` first and stop.

2. **Handle `--clear`.** If `$ARGUMENTS` is `--clear`, remove any saved filter and stop:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/identify-employees/scripts/detect_employee_filter.py" \
     --bucket-config "${XDG_CONFIG_HOME:-$HOME/.config}/agentic-analytics/bucket.json" \
     --clear
   ```

   Output the script's one-line result and stop.

3. **Run detection.** Otherwise sample the cached events and write any unambiguous filter to `bucket.json`:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/identify-employees/scripts/detect_employee_filter.py" \
     --bucket-config "${XDG_CONFIG_HOME:-$HOME/.config}/agentic-analytics/bucket.json" \
     --cache-dir "${XDG_CACHE_HOME:-$HOME/.cache}/agentic-analytics/dpl/<cache_dir>"
   ```

   Substitute `<cache_dir>` with the value from step 1.

4. **Output the script's one-line result verbatim.** Three possibilities:

   - **Match found:** "Detected employee traffic tagged with `extra_data['<key>'] = <value>` (X% of N sampled pageviews). Will filter this out of audience reports so it doesn't skew your results. Tell me if this is incorrect."
   - **No match:** "No unambiguous employee-traffic tag detected in N sampled pageviews. No filter applied."
   - **Cache empty:** "Cache directory not found ..." – tell the user to run `/agentic-analytics:staircase` or `/agentic-analytics:refresh-cache` first to populate the cache, then re-run this command.

   Don't add framing or summary.

## Notes

- The filter is **auto-applied only when unambiguous** (matched case-insensitively): known key name in `internal`, `is_internal`, `is_employee`, `employee`, `staff`, `internal_user`/`internalUser`, `internal_traffic`; boolean-ish value (`true`/`1`/`yes`); and tagged share between 0.1% and 25% of sampled pageviews. Ambiguous setups (multiple distinct values on the same key, or a share outside that range) are silently skipped.
- Re-running this command after the customer changes their tracker is the right way to refresh the detected filter.
- `--clear` removes any saved filter and exits without scanning – useful when detection got the wrong key and you want to start over.
- This command does not touch Parse.ly's per-account IP blocklist, which is the canonical mechanism for customers who go that route (managed by Parse.ly Support, applied upstream of the DPL).
