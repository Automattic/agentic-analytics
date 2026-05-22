# Agentic Analytics: Getting Started

Agentic Analytics lets you ask questions about your audience data in plain English, directly inside Claude Code. Instead of clicking through a dashboard, you ask Claude what you want to know, and it gives you back tables, summaries, and shareable reports.

This first release connects to your **Parse.ly Data Pipeline**. Support for more data sources (Google Analytics, Jetpack Stats, your own first-party data) is on the way.

This guide walks you through one-time setup. Plan on about 30 minutes the first time. After that, you can ask Claude for a report in a few words and get one back within a minute or two.

---

## Before you start

### Your computer

This first release supports:

- **macOS 13 (Ventura) or later.** To check, click the Apple logo in the top-left corner of your screen and choose "About This Mac."
- **Linux** (Ubuntu 20.04+, Debian 10+, Fedora, RHEL, Alpine, and most modern distros).
- **Windows 10 or later, via WSL** (Windows Subsystem for Linux). If you're on Windows, set up WSL first using [Microsoft's WSL guide](https://learn.microsoft.com/en-us/windows/wsl/install), then follow the Linux path below.

### A paid Claude account

You need a paid Claude plan: **Pro, Max, Team, Enterprise, or Console**. The free Claude.ai plan does not include access to Claude Code (the tool the plugin runs inside). If you don't have a paid plan, sign up at [claude.com](https://claude.com) before continuing.

### Comfort with Terminal

You'll be running a few commands in the Terminal app. If you've never used Terminal, that's fine. Every step is spelled out. If you'd prefer not to do it yourself, the whole setup is about 30 minutes and most teammates with technical experience can sit with you.

**A few Terminal basics that'll help:**

- **To paste a command:** click in the Terminal window and press **Command + V** on Mac (or **Ctrl + Shift + V** on Linux/WSL).
- **To run a command:** type it (or paste it), then press **Return** (or **Enter**).
- **To copy a result:** click and drag to select, then press **Command + C** on Mac (or **Ctrl + Shift + C** on Linux/WSL).

### What setup will ask for

You'll need three pieces of information from your Parse.ly contact. Email them ahead of time and ask for **"Data Pipeline access credentials."**

| What you'll need | What it is |
|---|---|
| **A bucket name** | Where your raw Parse.ly data lives. Looks like `parsely-dw-<publisher>`. |
| **An access key** | The first of two access codes. Treat it like a password. |
| **A secret access key** | The second of two access codes. Treat it like a password. |

Throughout this guide, "your Parse.ly contact" means whoever sent you the credentials; if you don't have a direct contact, `support@parsely.com` reaches the same team.

Keep them somewhere safe (your password manager is the right spot). The setup process will ask you for them.

### Security around your DPL credentials

The access codes give the plugin **read-only access** to the Parse.ly Data Pipeline bucket holding your event data. They cannot delete data, write to your AWS account, or reach anything outside that one bucket. Even so, they're powerful enough that someone who got hold of them could read your raw visitor data, so treat them like passwords.

**Three practices to follow:**

1. **Secure delivery.** Have your Parse.ly contact send the credentials via a password-manager share link or another secure channel. Plain email and chat tools (Slack, Teams) are not secure for this.

2. **Local storage.** During setup, your access codes are written to a standard AWS credentials file on your laptop (`~/.aws/credentials`) in plain text. This is how the AWS tools store credentials; the file never leaves your machine. The plugin never asks for your access codes in chat – you enter them directly into the AWS tool. Don't share the credentials file, don't copy your codes into chat, and don't commit them to version control.

3. **Rotation.** Ask your Parse.ly contact to issue new access codes if:
   - your laptop is lost or stolen,
   - someone with credential access leaves your team, or
   - you have any reason to think the codes were exposed.
   Rotation is fast and Parse.ly can do it without disrupting the plugin.

---

## Step 1: Install Claude Code

Claude Code is Anthropic's command-line tool. It is a different product from the Claude desktop app and the Claude.ai chat website. You need Claude Code specifically.

1. **Open the Terminal app.**
   - On Mac: press **Command + Space**, type `Terminal`, and press **Return**.
   - On Linux: open the Terminal app from your application menu.
   - On Windows: open your WSL distribution (e.g., Ubuntu).

2. **Paste this command** and press **Return**:
   ```
   curl -fsSL https://claude.ai/install.sh | bash
   ```
   The installer runs for about a minute. You'll see progress messages. It works on macOS, Linux, and WSL.

   > **If your company's security policy blocks the command above** (some IT departments do not allow piping `curl` to `bash`), alternative installs include:
   > - **macOS Homebrew:** `brew install --cask claude-code` (install Homebrew first from [brew.sh](https://brew.sh) if you don't have it).
   > - **Linux package managers:** Anthropic publishes signed apt, dnf, and apk repositories. See [Anthropic's setup guide](https://code.claude.com/docs/en/setup) for the exact commands.

3. **Confirm it worked.** Type:
   ```
   claude --version
   ```
   You should see a version number.

**How you'll know it worked:** `claude --version` prints a version number. You'll sign in the first time you launch Claude Code in the next step.

---

## Step 2: Install the Agentic Analytics plugin

These steps run inside Claude Code, and they're the same across platforms.

1. **Open Claude Code.** In Terminal, type `claude` and press **Return**. The first launch opens your browser to sign in – use your paid Claude account.

2. **Add the plugin source.** At Claude Code's prompt, type:
   ```
   /plugin marketplace add Automattic/agentic-analytics
   ```
   Press Return. Claude Code fetches the catalog (takes a few seconds) and may ask you to confirm you trust this source – it's published by Automattic, so answer yes.

3. **Install the plugin:**
   ```
   /plugin install agentic-analytics@automattic-agentic-analytics
   ```
   Press Return. If prompted to choose an installation scope, pick **User scope** so the plugin works in any folder.

4. **Reload to activate.** Type:
   ```
   /reload-plugins
   ```

**How you'll know it worked:** Claude Code shows a confirmation, and the new commands `/agentic-analytics:init` and `/agentic-analytics:staircase` are available. Type `/help` to confirm they're listed.

The plugin updates automatically – when Automattic publishes a new version, Claude Code picks it up at the next launch.

---

## Step 3: Run setup

Still in Claude Code, type:

```
/agentic-analytics:init
```

Press **Return**. From here you have **two options**, which differ only in who installs the prerequisites (Python 3 and the AWS command-line tool). The init flow itself is the same either way.

### Option A: Claude installs prerequisites for you (default)

Claude takes you through setup conversationally and handles the technical work for you:

- Greets you and confirms you're ready to start.
- Checks whether **Python 3** and the **AWS command-line tool** are installed. If either is missing, Claude shows you the exact command to install it for your platform (e.g., `xcode-select --install` on Mac, `sudo apt install python3` on Debian/Ubuntu) and waits for you to confirm before continuing.
- Asks you for the **bucket name** in chat.
- Walks you through running `aws configure --profile agentic-analytics` in a separate terminal window to enter your **access key** and **secret access key**. Region and output format are pre-set, so you just press Return at those two prompts. Your access codes are typed into the AWS tool directly, not into the chat – they don't appear in Claude's transcript.
- Tests the connection to your data.
- Saves your bucket setting so you never have to enter it again.
- Shows you the reports available today and suggests a first prompt to try.

Each time Claude wants to run a command on your machine or write a file, Claude Code asks for your approval. You'll click "Yes" a handful of times during setup. After the first run you can choose "Yes, always for this kind of command" to reduce the prompts.

### Option B: Install prerequisites yourself first

If your security policy doesn't allow Claude to install software on your behalf – or you'd just rather handle Python and the AWS CLI yourself – follow ["Setting up manually"](#setting-up-manually) below to install both and run `aws configure` yourself. Then come back and run `/agentic-analytics:init`; Claude will detect your existing setup, ask only for the bucket name, and verify the connection.

**How you'll know it worked:** Claude tells you setup is complete and suggests what to try next.

**If something goes wrong:** Claude will tell you what to check. If the connection test fails, the three usual causes are: a typo in the bucket name, a typo in your access key or secret, or a brief delay while Parse.ly's side propagates new credentials. Claude will spell out which fix to try. `/agentic-analytics:init` is safe to re-run as many times as you need. To start fully over from scratch, see ["If you get stuck"](#if-you-get-stuck) below.

---

## Step 4: Run your first report

After setup, ask Claude:

```
Run the staircase report
```

Press **Return**. The first time you run a report, the plugin downloads about two months of your data. How long that takes depends on the size of your bucket and your connection – small sites finish in a minute or two, larger ones can take longer. Reports after that run much faster because the data is cached locally, though larger buckets will still take a bit to process.

**What the staircase report shows you:**

- How many of your visitors came once, came back a few times, or are visiting regularly.
- Whether you're gaining or losing regular visitors compared to the previous month.
- Which traffic sources (Google, email, social, etc.) bring you visitors who come back.
- Any conversions your site is tracking.

You'll see the report in Terminal, plus a link to an HTML version you can open in a browser or share with colleagues.

---

## What's possible today

This is the first release of the plugin. Today it ships with one report (the staircase report described above). More capabilities are coming.

**Tell us what you want next.** If there's a question you wish you could ask Claude about your data, let your Parse.ly contact know. We're prioritizing what to build next based on what real customers ask for.

---

## Setting up manually

If your security policy doesn't allow Claude to install software on your behalf, or if you simply prefer to do these steps yourself, you can install the prerequisites manually before running `/agentic-analytics:init`. Claude will detect what's already in place and skip ahead.

### Install Python 3

- **macOS:** in Terminal, run `xcode-select --install`. Click **Install** on the dialog that appears. When it finishes, run `python3 --version` to confirm.
- **Linux (Debian/Ubuntu):** `sudo apt install python3`
- **Linux (Fedora/RHEL):** `sudo dnf install python3`
- **Linux (Alpine):** `sudo apk add python3`

### Install the AWS command-line tool

- **macOS:** download the official installer from [aws.amazon.com/cli](https://aws.amazon.com/cli/) and run it.
- **Linux (Debian/Ubuntu):** `sudo apt install awscli` (or use Amazon's installer from the link above for the latest version).
- **Linux (other distros):** see Amazon's [Linux install instructions](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).

### Configure AWS credentials manually

In Terminal, run:

```
aws configure --profile agentic-analytics
```

When prompted, enter your access key, your secret access key, region `us-east-1`, and output format `json`. The profile name `agentic-analytics` matches what the plugin expects.

After this, run `/agentic-analytics:init` in Claude Code. Claude will detect the existing setup, ask you for your bucket name, and verify the connection.

---

## If you get stuck

| Problem | Try this |
|---|---|
| Setup didn't complete | Re-run `/agentic-analytics:init` – safe to run as many times as you need |
| You entered the wrong bucket name | Run `/agentic-analytics:clear-configs`, then `/agentic-analytics:init` to start over |
| Your access key or secret is wrong | Re-run `aws configure --profile agentic-analytics` in your terminal to redo just those |
| Your credentials aren't working at all | Email your Parse.ly contact to double-check the values they sent |
| The report fails with an error | Note the error message and email your Parse.ly contact |
| The report runs but shows no data | Confirm with your Parse.ly contact that your site is sending events to the DPL bucket. If the bucket is brand new, allow a day or so for events to accumulate before retrying. |
| You want to start completely over | Run `/agentic-analytics:clear-configs` to wipe the bucket and AWS profile, then run `/agentic-analytics:init` again. Your cached data stays put. |
| `command not found: claude` | Re-run the Claude Code installer from Step 1 |
| `command not found: python3` or `command not found: aws` | Install the missing tool from the [Setting up manually](#setting-up-manually) section |
| Anything else | Email your Parse.ly contact, or `support@parsely.com` if you don't have one – they'll route you to the right person |
