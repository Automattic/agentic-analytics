---
description: Walk a new user through first-time setup of the agentic-analytics plugin. This command asks for the bucket name, walks the user through `aws configure` for their AWS credentials, verifies bucket access, and persists the runtime config. Idempotent, safe to rerun – picks up the bucket from `bucket.json` on subsequent runs.
---

# First-run onboarding

This is the first thing a new user runs after installing the plugin. Init asks for the bucket name in chat (the only piece of customer data the plugin needs that isn't already on disk), walks the user through `aws configure` for their AWS credentials, verifies, and persists the runtime config to `bucket.json`.

The plugin gives the user direct access to their Parse.ly Data Pipeline (DPL) so they can ask questions of their raw event firehose in chat. More data sources land in future versions; v1 is DPL only.

## Output discipline

This is a customer-facing first-run flow. Keep it quiet and on-script.

- **Verbatim where shown.** Blockquoted text (`> like this`) prescribes the exact words Claude says to the user. Output those words and nothing else for that step.
- **Silent steps stay silent.** Steps tagged `(silent on success)` produce no chat output when they succeed. Run the Bash, move on. Don't summarize Bash results, don't announce upcoming actions, don't recap state.
- **No TodoWrite.** Don't surface this flow as a task list.
- **Off-script questions get short answers, then resume.** If the customer interrupts with a question, answer briefly and pick up at the next step.
- **Bucket name lives in `bucket.json`.** If it's already there from a previous run, reuse it silently. Only ask the customer for it if it isn't.
- **AWS credentials live in `~/.aws/credentials`.** They're set up by the user via `aws configure --profile agentic-analytics` in step 5. Subsequent `aws` invocations pick them up via `--profile agentic-analytics`. Never ask for them in chat.

## Tone

Welcoming, not robust. The user is a Parse.ly customer, so don't explain Parse.ly or the DPL to them. The goal is a productive first run, not a tutorial.

## Steps

1. **Greet briefly.** Output this verbatim, then immediately continue to step 2:

   > Setting up your Parse.ly Data Pipeline access – about thirty seconds.

   Do not wait for the user to respond. The customer's "yes" was their `/agentic-analytics:init` invocation; nothing destructive happens here so no further confirmation is needed.

2. **Get the bucket name.** Look at `${XDG_CONFIG_HOME:-$HOME/.config}/agentic-analytics/bucket.json`. If it exists and has a `bucket` field, hold that value and continue silently (subsequent steps reuse it; this is the idempotent re-run path).

   If the file doesn't exist or the field is missing, output verbatim and wait for a reply:

   > What's the **bucket name** holding your DPL events? Your Parse.ly contact should have provided this – it usually looks like `parsely-dw-<publisher>`.

   Record the user's reply as the bucket value. Don't validate the format (step 6's connectivity check is the real test). If the user doesn't have a bucket name, tell them to reach back out to their Parse.ly contact and stop here.

3. **Detect platform and check prerequisites** (silent on success). Run `uname -s`, `python3 --version`, and `aws --version`. If platform is `Darwin` or Linux-family and both tools resolve, continue without comment. If platform is native Windows (no WSL) or a prerequisite is missing, surface the issue and follow the install guidance in "Cross-platform notes" below.

4. **Ensure the cache directory exists** (silent). Safe to run unconditionally:

   ```bash
   mkdir -p "${XDG_CACHE_HOME:-$HOME/.cache}/agentic-analytics/dpl"
   ```

5. **Set up AWS credentials.** Pre-set region and output format silently (idempotent – safe on re-runs):

   ```bash
   aws configure set region us-east-1 --profile agentic-analytics
   aws configure set output json --profile agentic-analytics
   ```

   Then check whether an access key is already configured for the profile:

   ```bash
   aws configure get aws_access_key_id --profile agentic-analytics 2>/dev/null
   ```

   If that returns a non-empty value, the user already set up credentials on a previous run. Continue silently to step 6 – no verbatim instruction needed.

   If it returns empty (no access key set), the user needs to set up credentials. Output verbatim:

   > Now your access key and secret. Open a new terminal window and run:
   >
   > ```
   > aws configure --profile agentic-analytics
   > ```
   >
   > You'll see four prompts in order. Here's what to do at each:
   >
   > ```
   > AWS Access Key ID [None]:        # paste your access key, then Enter
   > AWS Secret Access Key [None]:    # paste your secret, then Enter (input is hidden – that's normal)
   > Default region name [us-east-1]: # just press Enter (already set)
   > Default output format [json]:    # just press Enter (already set)
   > ```
   >
   > When your shell prompt comes back, let me know.

   Wait for the user to confirm completion before continuing.

6. **Verify connectivity** (silent on success). List one recent day of partitions, substituting the bucket value held from step 2 for `<bucket>`:

   ```bash
   yesterday=$(python3 -c "from datetime import date, timedelta; print((date.today() - timedelta(days=1)).strftime('%Y/%m/%d'))")
   aws --profile agentic-analytics s3 ls "s3://<bucket>/events/$yesterday/" | head
   ```

   If files come back, continue without comment. If the listing is empty or errors, output verbatim:

   > I couldn't list files in your bucket. The most likely causes:
   >
   > - The bucket name has a typo. Want to re-enter it? (Re-run `/agentic-analytics:clear-configs` then `/agentic-analytics:init`.)
   > - Your AWS access key or secret has a typo. Re-run `aws configure --profile agentic-analytics` in your terminal to redo them.
   > - The AWS keys haven't propagated on Parse.ly's side yet (can take a minute). Want to wait and retry?
   >
   > Let me know how you'd like to proceed.

   Do not continue until verify passes.

7. **Offer to wire up the email-campaign join key.** Reports include "cohort CSVs" – lists of visitors who churned, are at risk of churning, or just became brand lovers. By default these are keyed on a Parse.ly cookie hash that's opaque to your CRM, so they're useful for diagnosis but not for direct re-engagement sends. The fix is for the customer to mint a per-recipient identifier and embed it in URLs in their email campaigns (e.g. `?pid=<id>`); the report can then surface that identifier in cohort CSVs and the customer merges results back into their CRM list.

   If `bucket.json` from step 2 already has a `join_id_key`, hold that value and continue silently.

   Otherwise, output verbatim and wait for a reply:

   > Optional: do your email campaigns embed a per-recipient identifier in URLs (e.g. `?pid=...`)? If so, what's the parameter name? Type the name (e.g. `pid`) or "skip" if you don't have this set up yet – you can configure it later by re-running this command.

   Treat any reply of `skip`, `no`, `none`, or empty as "no key configured" and continue without setting one. Otherwise record the reply as the `join_id_key` value. Don't validate further (the report harmlessly emits empty values until matching `?<key>=...` URLs land in cached events).

8. **Persist the runtime config** (silent on success). Derive a `cache_dir` slug from the bucket name by stripping the `parsely-dw-` prefix (e.g. `parsely-dw-acme` → `acme`, `parsely-dw-acme-co` → `acme-co`). If the bucket doesn't start with `parsely-dw-`, use the full bucket name as the slug. Then write `${XDG_CONFIG_HOME:-~/.config}/agentic-analytics/bucket.json` using the Write tool (`mkdir -p` the directory first if needed), substituting the bucket value held from step 2 for `<bucket>`. Include `join_id_key` only when step 7 captured a value:

   ```json
   {
     "bucket": "<bucket>",
     "profile": "agentic-analytics",
     "cache_dir": "<slug>",
     "join_id_key": "<key>"
   }
   ```

   Safe to overwrite on re-runs: this is the file step 2 reads from on subsequent inits.

9. **Show what's available.** Output verbatim:

   > You're all set. Here's what the plugin can do today:
   >
   > **Staircase report** – visitor-tier audience report (1 visit / 2-4 / 5+) over a 60-day window, comparing the most recent 30 days against the prior 30. Shows tier counts, deltas, tier-to-tier transitions, and per-channel climb yield (the fraction of each channel's prior-window arrivals whose tier rank rose in the current window).
   >
   > Two ways to run it:
   >
   > - `/agentic-analytics:staircase` runs across every site in your bucket.
   > - `/agentic-analytics:staircase <site>` filters to one site (e.g. `your-site.com`).
   >
   > Or just ask in chat: "Run the staircase report."
   >
   > **Identify employee traffic** – `/agentic-analytics:identify-employees` checks whether your tracker tags employees via `extra_data` (e.g., `extra_data['Internal'] = true`). If so, saves the filter so reports exclude that traffic. Works on cached events, so run it after your first staircase report has populated the cache.

10. **Suggest a first thing to try.** Output verbatim:

   > Want to try it? Just say:
   >
   > ```
   > Run the staircase report
   > ```

## Cross-platform notes

- **macOS (Darwin):** primary supported platform. All steps verified there.
  - Python 3 missing: offer to run `xcode-select --install` via Bash. The user has to click through the GUI dialog. Wait for them to confirm.
  - AWS CLI missing: point to [aws.amazon.com/cli](https://aws.amazon.com/cli/) for the installer.
- **Linux:** supported via the same flow with distro-specific package commands.
  - Python 3 missing: `sudo apt install python3` (Debian/Ubuntu), `sudo dnf install python3` (Fedora/RHEL), `sudo apk add python3` (Alpine). User runs it themselves.
  - AWS CLI missing: distro package (`sudo apt install awscli` etc.) or Amazon's curl-based installer per [aws.amazon.com/cli](https://aws.amazon.com/cli/).
- **Native Windows:** not officially supported in v1. WSL users can follow the Linux path.

## Out of scope for v1

- No synthetic data fallback. v1 assumes the user has real DPL access; the "first thing to try" runs against their data.
- No support for non-S3 DPL delivery formats. v1 assumes S3-direct access.
- No multi-bucket configuration. A customer with multiple Parse.ly buckets (rare) can run `/agentic-analytics:clear-configs` then `/agentic-analytics:init` to switch, but only one bucket is active at a time.
