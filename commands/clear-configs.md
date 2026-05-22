---
description: Remove the local config files written by /agentic-analytics:init. Leaves the events cache alone. After running this, /agentic-analytics:init walks the user through setup again from scratch (asks for the bucket, walks them through aws configure).
---

# Clear local configs

Removes two things to put the user back at first-run state:

- `${XDG_CONFIG_HOME:-~/.config}/agentic-analytics/bucket.json` – runtime config init writes.
- The `agentic-analytics` profile in `~/.aws/credentials` and `~/.aws/config` – the AWS creds the user set up via `aws configure`.

Leaves the events cache at `${XDG_CACHE_HOME:-~/.cache}/agentic-analytics/dpl/` alone. Re-pulling DPL is slow; the cache is data, not config.

## Output discipline

This is a one-shot housekeeping command. Output the verbatim line below at the start, run the script, then stop. Don't narrate the script output.

## Steps

1. **Announce.** Output verbatim:

   > Clearing local agentic-analytics configs. Events cache stays put.

2. **Run the script.** It prints what was cleared (or `(nothing to clear)` if everything was already gone):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/clear_configs.py"
   ```
