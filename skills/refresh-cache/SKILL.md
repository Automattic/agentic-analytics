---
name: refresh-cache
description: Blow away the local data cache and rebuild it. Use when the user asks to refresh the cache, rebuild the cache, clear cached data, start clean, recover from a bad pull, or says reports are returning stale or weird-looking numbers. Leaves config alone.
---

To refresh the cache, invoke `/agentic-analytics:refresh-cache` with no arguments. The slash command holds the full flow (read bucket config, delete the customer's DPL cache and the shared duckdb-tmp dir, re-pull a fresh 60-day window). This skill exists so plain-language requests route to the same code path without the user needing to remember the slash command name.
