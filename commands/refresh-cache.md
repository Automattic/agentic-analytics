---
description: Blow away the local data cache for the configured customer and rebuild it from S3. Use after a bad pull, a partial fetch, schema drift, or any time a report is returning stale or weird-looking numbers and you want to start clean. Leaves config (bucket.json, AWS profile) alone.
---

# Refresh the data cache

Clears the configured customer's local cache, then re-pulls a fresh 60-day window so the next report runs against clean data. Two-step on purpose:

1. **Delete** (`refresh_cache.py`) is stdlib-only and offline-safe – it never touches the network, so it can't fail on auth lapses, network errors, or S3 hiccups. It will exit non-zero if `bucket.json` is missing (nothing to scope the deletion to); that's the only failure mode.
2. **Re-pull** (`pull_dpl.py`) requires fresh AWS SSO. Splitting the steps means a stale auth at re-pull time doesn't leave the user in a half-state – the cache is gone either way, and the next report run (or a retry of this command) will fill it back in.

If config itself looks broken (wrong bucket, missing profile), use `/agentic-analytics:clear-configs` instead and re-run `/agentic-analytics:init`.

## Output discipline

This is housekeeping. Be terse. Announce, run, stop. Don't narrate the script output line-by-line – the scripts print their own progress.

## Steps

1. **Announce.** Output verbatim:

   > Refreshing the local cache. Config (bucket.json, AWS profile) will be left alone.

2. **Load bucket config.** Read `${XDG_CONFIG_HOME:-~/.config}/agentic-analytics/bucket.json`. It contains:

   - `bucket` – S3 bucket holding the customer's DPL events.
   - `profile` – AWS profile to authenticate with.
   - `cache_dir` – subdirectory under `${XDG_CACHE_HOME:-~/.cache}/agentic-analytics/dpl/` (e.g. `acme`).

   If the file is missing, tell the user to run `/agentic-analytics:init` first and stop. Do not guess values.

3. **Delete the cache.** Runs the deletion script. Reports which dirs it cleared and the bytes freed, or `(nothing to clear)`:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/refresh_cache.py"
   ```

4. **Re-pull a fresh 60 days.** Same window the staircase report targets. `pull_dpl.py` is incremental and idempotent; after a wipe it pulls the full window once.

   ```bash
   read target_start target_end < <(python3 -c "
   from datetime import date, timedelta
   start = date.today() - timedelta(days=60)
   end = date.today() - timedelta(days=1)
   print(start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
   ")
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/staircase-report/scripts/pull_dpl.py" \
     --cache-dir <cache_dir> --bucket <bucket> --profile <profile> \
     --start "$target_start" --end "$target_end"
   ```

   Substitute `<cache_dir>`, `<bucket>`, `<profile>` with the values held from step 2. Do NOT add `|| true` here – unlike the report path, a failed re-pull is the headline result of this command; surface it.

5. **Report done.** One short line: "Cache rebuilt. Next report run uses fresh data."

   If the re-pull failed (auth lapse, S3 error, no events for the window), say so plainly and tell the user the cache is now empty – the next report run will try to pull again, or they can re-run this command once auth is fresh.

## Notes

- Only the **currently configured** customer's DPL cache is affected. If multiple customers' caches exist on disk (different `cache_dir` slugs), the others are untouched – switch `bucket.json` first if you need to refresh a different one.
- The shared `duckdb-tmp/` working dir is always cleared too. It's derived state any report can regenerate.
