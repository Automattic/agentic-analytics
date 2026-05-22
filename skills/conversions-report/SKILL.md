---
name: conversions-report
description: Seed for a future standalone conversions report. Currently holds conversion-attribution code lifted out of the staircase report (PR B of the staircase climbing revision); not yet runnable end-to-end. Use when the user asks to build out the standalone conversions report; do not invoke for "run the conversions report" until the file is reshaped.
---

This skill exists so the conversion code that was removed from `staircase-report` doesn't get lost. The script at `scripts/conversions.py` is the lifted code; a future PR (tracked separately) will turn it into a real report skill with its own slash command, CLI, and report renderer. Until then, the skill description above tells Claude not to invoke this skill at runtime.
