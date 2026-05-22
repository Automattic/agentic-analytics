---
name: staircase-report
description: Run the staircase audience-relationship report for a customer. Use when the user asks to run the staircase report, requests it for a specific site, or asks for an audience-tier or relationship-intelligence report.
---

To run the report, invoke `/agentic-analytics:staircase` with the user's customer specifier as the argument. The slash command holds the full report-running instructions (cache probe, window selection, script invocation, output contract). This skill exists so plain-language requests route to the same code path without the user needing to remember the slash command name.
