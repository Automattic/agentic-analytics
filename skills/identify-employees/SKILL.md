---
name: identify-employees
description: Detect whether the configured customer's tracker tags employee/internal pageviews via `extra_data`, and if so, save the filter so reports exclude that traffic. Use when the user asks to identify employees, exclude employee traffic, filter internal users, or says results look skewed by their own staff.
---

To run detection, invoke `/agentic-analytics:identify-employees` with no arguments. The slash command holds the full flow (read bucket config, sample cached events, write the discovered filter to `bucket.json` if unambiguous). This skill exists so plain-language requests route to the same code path without the user needing to remember the slash command name.
