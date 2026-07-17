# gh-overseer

Self-hosted daemon that runs a coding agent as a **bot GitHub account**. No
repo list to maintain: work happens wherever the **owner** (you, one trusted
human) **assigns the bot an issue** or **@mentions it** in an issue you
authored — your own repos or any public repo.

One Python file, one dependency (PyGithub), polling — no webhooks, no server,
no queue. State (processed events, last poll time, retry queue) lives in a
JSON file next to the config.

## The full flow

**1. Trigger.** Every poll (default 60 s) the daemon looks for open issues
assigned to the bot (any repo it can access) and open issues/PRs where you
@mentioned it (GitHub search, all public repos). Only issues _you_ authored
count; everyone else is invisible.

**2. Clone & branch.** The repo is cloned to a temp dir and a branch
(`overseer/issue-N`) is created — or an existing branch is checked out on
follow-ups.

**3. Direct push or fork?** Decided per job:

- Bot has **write access** (you invited it as collaborator) → the branch
  lives on the repo itself, pushed directly.
- **No write access** (e.g. a public repo the bot isn't a member of) → the
  bot **forks the repo to its own account**, pushes the branch to the
  fork, and opens a **cross-repo PR** (`bot:overseer/issue-N` →
  `upstream:main`) — the standard open-source contribution flow. The fork
  is created automatically on first use and reused afterwards; you never
  manage it. Requires a classic PAT (see Requirements).

**4. Agent runs.** Claude Code headless (or any runner you configure) works
in the checkout: splits the task into subtasks, commits each separately, runs
tests. It never pushes; the orchestrator does.

**5. Push & PR.** Commits are pushed, a PR (`Closes #N`) is opened — or an
existing PR updated — and the bot comments on the issue @mentioning you.
If the agent needed more information instead, it makes no commits and its
question is posted as the comment.

**6. Iterate.** The conversation continues where it lives: your comments on
the issue or PR, PR reviews, and inline review comments each re-prompt the
agent with the whole thread as context, on the same branch. The bot talks
only to you and ignores its own comments (different account, different
login).

**Failure safety, in order:**

- agent hangs → killed at `job_timeout` (with all child processes),
  committed subtasks still pushed
- usage/rate limit → partial work pushed, job auto-resumes when the limit
  window resets (max 5 attempts)
- push fails (no access, network) → commits preserved locally as a git
  bundle in `salvage/`; the next job for that issue restores them and
  continues — no agent work is ever lost
- GitHub outage → cycle logged and skipped; `last_poll` only advances on
  success, so the next good poll sweeps the gap

## Requirements

- [uv](https://docs.astral.sh/uv/) (deps are inline PEP 723 metadata; `uv run`
  handles them) — or plain Python 3.9+ with `pip install PyGithub`
- A **dedicated bot GitHub account** with a PAT. Two modes:
  - **Collaborator mode** (private repos, direct pushes): invite the bot as
    collaborator (write) — needed to be assignable and to push. A
    fine-grained PAT works: contents, pull requests, issues (read/write),
    scoped to **"All repositories"**. Caveat: fine-grained PATs can't
    _accept_ invites (GitHub only offers read-only invitation permission) —
    accept manually in the bot account; the daemon logs a reminder.
  - **Contribution mode** (public repos, no membership): the bot forks the
    repo, pushes the branch to its fork, and opens a cross-repo PR. This
    requires a **classic PAT** (`public_repo` scope, or `repo` to cover
    private collaborator repos too) — fine-grained PATs cannot fork,
    comment, or open PRs on repositories they weren't explicitly granted.
- [Claude Code](https://claude.com/claude-code) installed and authenticated
  (or any other runner, see below)

## Setup

```sh
cp config.example.json config.json
# edit config.json
export GH_BOT_TOKEN=github_pat_...   # or put it in config.json as bot_token
```

| Key             | Meaning                                                                                                                                                                                                                                                 |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `owner`         | **your** GitHub username — the only human the bot listens and talks to                                                                                                                                                                                  |
| `bot_token`     | the bot account's PAT (or set `GH_BOT_TOKEN`)                                                                                                                                                                                                           |
| `poll_interval` | seconds between polls (default 60)                                                                                                                                                                                                                      |
| `allowed_tools` | substituted for `{allowed_tools}` in the runner                                                                                                                                                                                                         |
| `job_timeout`   | max seconds per job (default 3600). A hung agent is killed (with everything it spawned); subtasks committed before the kill are still pushed and the timeout is reported in the comment                                                                 |
| `retry_delay`   | seconds before auto-resuming a job that hit a usage/rate limit (default 3600), used when the limit error doesn't carry a reset time. Partial work is pushed, the job re-queues itself (max 5 attempts), and the resume picks up from the pushed commits |
| `context_limit` | how many of the most recent thread comments (and inline review comments) go into the agent's prompt (default 50)                                                                                                                                        |
| `salvage_dir`   | where unpushable work is preserved as git bundles (default `salvage/` next to the script)                                                                                                                                                               |
| `runner`        | agent command; `{prompt}` and `{allowed_tools}` are substituted                                                                                                                                                                                         |
| `env`           | extra env vars for the runner; empty values are ignored                                                                                                                                                                                                 |

The bot's login is derived from the token — no need to configure it.

## Start it

```sh
uv run overseer.py            # runs forever, polls every poll_interval
uv run overseer.py --once     # single poll cycle (testing / cron)
```

Then: **create an issue and assign the bot or @mention it**, and watch the PR
appear (see The full flow above). Reply on the issue or review the PR to keep
going — every owner comment/review triggers a follow-up job on the same
branch.

Only events _after_ the first start are picked up (no backfill), except
already-open issues assigned to or mentioning the bot, which are picked up on
the first poll.

### Run as a service (macOS)

#### LaunchAgent (runs while logged in)

Place `~/Library/LaunchAgents/info.stamat.gh-overseer.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>info.stamat.gh-overseer</string>
  <key>ProgramArguments</key><array>
    <string>/opt/homebrew/bin/uv</string>
    <string>run</string>
    <string>/Users/stamat/Sites/localhost/gh-overseer/overseer.py</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>GH_BOT_TOKEN</key><string>github_pat_...</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/gh-overseer.log</string>
  <key>StandardErrorPath</key><string>/tmp/gh-overseer.log</string>
</dict></plist>
```

```sh
launchctl load ~/Library/LaunchAgents/info.stamat.gh-overseer.plist
```

A LaunchAgent runs only while you are logged in and stops on logout.

#### LaunchDaemon (runs always, even when logged out)

For 24/7 operation (survives logout / reboot), install as a system LaunchDaemon
at `/Library/LaunchDaemons/info.stamat.gh-overseer.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>info.stamat.gh-overseer</string>
  <key>ProgramArguments</key><array>
    <string>/opt/homebrew/bin/uv</string>
    <string>run</string>
    <string>/Users/stamat/Sites/localhost/gh-overseer/overseer.py</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>GH_BOT_TOKEN</key><string>github_pat_...</string>
    <key>HOME</key><string>/Users/stamat</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/gh-overseer.log</string>
  <key>StandardErrorPath</key><string>/tmp/gh-overseer.log</string>
  <key>UserName</key><string>your_user</string>
</dict></plist>
```

```sh
sudo cp info.stamat.gh-overseer.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/info.stamat.gh-overseer.plist
sudo launchctl load /Library/LaunchDaemons/info.stamat.gh-overseer.plist
```

Replace `your_user` with your macOS username and `github_pat_...` with the
actual token. The extra `HOME` and `PATH` variables are required because
LaunchDaemons run in a minimal environment.

| Feature              | LaunchAgent         | LaunchDaemon     |
| -------------------- | ------------------- | ---------------- |
| Runs while logged in | ✅ Yes              | ✅ Yes           |
| Runs after logout    | ❌ No               | ✅ Yes           |
| Starts on boot       | ❌ No (after login) | ✅ Yes           |
| During sleep         | ❌ Paused           | ❌ Paused        |
| Resume after wake    | ✅ Via KeepAlive    | ✅ Via KeepAlive |

#### Prevent macOS from sleeping

The daemon pauses during sleep and resumes when the Mac wakes. If you need it
to run uninterrupted (e.g. for long-running agent jobs at night), prevent sleep:

**Temporarily** — keep the Mac awake until `Ctrl + C`:

```sh
caffeinate
```

Or run the daemon under `caffeinate`:

```sh
caffeinate -s uv run overseer.py
```

**Permanently** — disable automatic sleep on all power sources:

```sh
sudo pmset -a sleep 0
sudo pmset -a displaysleep 0
sudo pmset -a disksleep 0
```

Re-enable later (e.g. sleep after 30 minutes):

```sh
sudo pmset -a sleep 30
sudo pmset -a displaysleep 15
```

## Concurrency

Jobs run **serially**, one at a time, in poll order — multiple mentions in one
cycle just queue up. Same-thread events collapse into a single job. Nothing is
dropped while a job runs; the next poll sweeps everything that happened
meanwhile. Run exactly one daemon instance — two would share `state.json` and
double-process events.

> **Possible upgrade:** parallel jobs via `ThreadPoolExecutor(max_workers=N)`
> around `run_and_maybe_retry` in `cycle()` (~10 lines). Costs: N× concurrent
> token burn (usage limits hit faster), interleaved logs, and state-file
> writes need a lock. Worth it only when serial queueing measurably delays
> work you'd actually review in parallel.

## Custom models / runners

Two independent knobs:

**1. Keep Claude Code, swap the model** — Claude Code respects
`ANTHROPIC_BASE_URL`. Point it at a [LiteLLM proxy](https://docs.litellm.ai/)
(speaks the Anthropic `/v1/messages` format, routes to any provider):

```json
"env": {
  "ANTHROPIC_BASE_URL": "http://localhost:4000",
  "ANTHROPIC_AUTH_TOKEN": "sk-litellm-...",
  "ANTHROPIC_MODEL": "openai/gpt-5.2"
}
```

**2. Swap the whole harness** — `runner` is just a command template run in
the checkout; `{prompt}` is substituted. Anything that takes a task, edits
files, and commits works (e.g. [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent),
[aider](https://aider.chat/) `aider --message "{prompt}" --yes`, or your own
LiteLLM tool-loop script.)

### mini-swe-agent with DeepSeek

[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) is a lightweight
agent that works great with DeepSeek models — no API proxy needed.

**Setup** — configure DeepSeek once (sets the global config file):

```sh
uvx --with fastapi --with orjson mini-swe-agent --model deepseek/deepseek-chat --yolo \
  --exit-immediately -t "hello, this is a test task"

# or manually:
cat > "$(uvx mini-swe-agent --help 2>&1 | grep -oP "'([^']*\.env)'" | head -1)" <<'ENVEOF'
MSWEA_MODEL_NAME='deepseek/deepseek-chat'
MSWEA_CONFIGURED='true'
ENVEOF
```

**Runner config** for `config.json`:

```json
"runner": ["uvx", "--with", "fastapi", "--with", "orjson", "mini-swe-agent", "-t", "{prompt}", "-y", "--exit-immediately", "--cost-limit", "0"]
```

| Flag                 | Why                                                      |
| -------------------- | -------------------------------------------------------- |
| `--with fastapi`     | workaround for litellm bug (imports proxy code)          |
| `--with orjson`      | workaround for litellm bug (imports http parsing utils)  |
| `-t "{prompt}"`      | task text (substituted by gh-overseer)                   |
| `-y`                 | skip confirmation prompts                                |
| `--exit-immediately` | exit when done instead of dropping into interactive mode |
| `--cost-limit 0`     | unlimited cost (omit or set a dollar cap)                |

mini-swe-agent runs in the cloned repo directory using its built-in "local"
environment — it edits files, runs commands, and commits directly. Because it
is piped through `uvx`, it auto-installs its dependencies on first run with no
setup required.

## Security

- **Owner authorship is the security boundary.** Jobs start only from issues
  _authored by you_ and assigned to / mentioning the bot; follow-ups only
  from _your_ comments/reviews on the bot's threads. Everyone else is
  invisible to it. Third-party comments never trigger anything, but they do
  appear in the thread context the agent reads — treat public-repo threads
  as untrusted input (prompt injection surface).
- Jobs run with `--permission-mode acceptEdits` and a restricted
  `--allowed-tools` list (no arbitrary `Bash` by default). Widen it
  (e.g. `Bash(npm test:*)`) deliberately. Never use
  `--dangerously-skip-permissions` here.
- PAT choice is a scope trade-off: fine-grained (collaborator mode only) is
  tighter; contribution mode needs a classic PAT, whose reach is the bot
  account's reach — which is still only repos you invited it to plus its own
  forks. Revoke access by removing the collaborator, or revoke the PAT for a
  full kill switch.
- Collaborator invite handling: invites from anyone but the owner are ignored.
  Owner invites are auto-accepted only when the token can do so (classic PAT);
  with a fine-grained PAT the accept fails and the daemon logs a reminder to
  accept manually in the bot account.

## Tests

```sh
uv run test_overseer.py -v
```

Covers the pure logic (event detection, owner/bot filtering, grouping,
prompt building). Network/git paths are thin wrappers over PyGithub and git.
