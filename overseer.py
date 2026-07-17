#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["PyGithub"]
# ///
"""gh-overseer: GitHub bot-account agent orchestrator.

Runs as a dedicated bot GitHub account. When the owner (one trusted human)
creates an issue and assigns it to the bot, the bot clones the repo, runs a
headless coding agent (Claude Code by default) on the task, pushes a branch,
opens a PR, and comments back on the issue. Follow-up comments, PR reviews,
and review comments by the owner re-prompt the agent with the whole thread as
context. Activity from anyone else is ignored.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from github import Auth, Github

MARKER = "🤖 [gh-overseer]"

SECRETS = []  # tokens scrubbed from everything we log or post (filled in main)


def redact(s):
    for secret in SECRETS:
        s = s.replace(secret, "***")
    return s

DEFAULT_RUNNER = ["claude", "-p", "{prompt}",
                  "--model", "claude-opus-4-8",
                  "--effort", "xhigh",
                  "--output-format", "json",
                  "--permission-mode", "acceptEdits",
                  "--allowed-tools", "{allowed_tools}"]


def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
          f"{redact(str(msg))}", flush=True)


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def sh(args, **kw):
    """Run a command, raise on failure, return stdout."""
    kw.setdefault("timeout", 600)  # a wedged git must not freeze the daemon
    res = subprocess.run(args, capture_output=True, text=True, **kw)
    if res.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])}... failed: {res.stderr.strip()[:500]}")
    return res.stdout


class AgentTimeout(RuntimeError):
    pass


class UsageLimit(RuntimeError):
    """Agent hit a usage/rate limit; resume_at is an epoch timestamp or None."""

    def __init__(self, msg, resume_at=None):
        super().__init__(msg)
        self.resume_at = resume_at


# ---------------------------------------------------------------- pure logic

def find_events(owner, bot, issues, comments, processed):
    """Decide what to act on. Plain dicts in, plain dicts out. Pure, testable.

    issues:   {"repo","number","author","assignees","body","is_pr"}
    comments: {"key","repo","number","author","body","is_pr"} — issue comments,
              PR reviews and review comments, all pre-filtered for eligibility
              (parent thread belongs to the bot) by the poll layer.

    Events are grouped per (repo, number) so several comments in one poll
    become a single job.
    """
    events = {}

    def add(repo, number, key, kind, body, is_pr):
        e = events.setdefault((repo, number), {
            "repo": repo, "number": number, "kind": kind,
            "keys": [], "directives": [], "is_pr": is_pr})
        e["keys"].append(key)
        if body:
            e["directives"].append(body)
        if kind == "work":
            e["kind"] = "work"

    for i in issues:
        key = f"{i['repo']}#{i['number']}"
        if key in processed or i["author"] != owner:
            continue
        if bot not in i["assignees"] and not i.get("mentioned"):
            continue
        add(i["repo"], i["number"], key, "work", i.get("body"), i.get("is_pr", False))

    for c in comments:
        if c["key"] in processed or c["author"] != owner:
            continue
        add(c["repo"], c["number"], c["key"], "followup", c.get("body"), c["is_pr"])

    return list(events.values())


def build_prompt(event, owner, limit=50):
    t = event["target"]
    kind = "PR" if event["is_pr"] else "issue"
    parts = [
        f"You are working on {kind} #{event['number']} of {event['repo']}.",
        f"Title: {t['title']}",
        f"{kind} body:\n{t.get('body') or '(empty)'}",
    ]
    if event.get("thread"):
        parts.append("Discussion so far:\n" + "\n---\n".join(
            f"{c['author']}: {c['body']}" for c in event["thread"][-limit:]))
    if event.get("review_comments"):
        parts.append("Code review comments:\n" + "\n---\n".join(
            f"{c['author']} on {c.get('path', '?')}:{c.get('line', '?')}: {c['body']}"
            for c in event["review_comments"][-limit:]))
    if event["directives"]:
        parts.append("Latest request(s) from @" + owner + ":\n"
                     + "\n---\n".join(event["directives"]))
    parts.append(
        "Instructions:\n"
        "- Break the work into subtasks.\n"
        "- Implement each subtask and commit it separately: `subtask: <description>`.\n"
        "- Run the project's tests if present and your tools allow it;"
        " fix any failures you introduced.\n"
        "- Do NOT push; the orchestrator pushes.\n"
        f"- If you need more information from @{owner}, make NO code changes and"
        " end with your question — it will be posted as a comment.\n"
        "- Write your full report — what you did and why, or the full answer if"
        " a report/analysis was requested — to `.overseer-report.md` in the repo"
        " root. It is posted back as a comment and never committed."
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------- polling

def poll(gh, config, since):
    """Fetch owner-authored activity on bot threads as plain dicts.

    No repo list: work is wherever the owner assigned the bot an issue
    (/issues endpoint, all accessible repos) or the bot has an open PR.
    """
    bot, owner = config["bot_login"], config["owner"]
    me = gh.get_user()
    ok = True  # False = a time-windowed fetch failed; caller must not advance last_poll

    # accept owner's collaborator invites — must be a collaborator to be
    # assignable. Needs a classic PAT (repo scope); fine-grained PATs can only
    # list invitations, so there we just log a reminder to accept manually.
    try:
        for inv in me.get_invitations():
            if inv.inviter.login == owner:
                try:
                    me.accept_invitation(inv)
                    log(f"accepted invite to {inv.repository.full_name}")
                except Exception:
                    log(f"pending invite to {inv.repository.full_name} — "
                        "token can't accept it, accept manually in the bot account")
    except Exception as e:
        log(f"invitation check failed: {e}")

    # open issues (and PRs) assigned to the bot, across all repos
    assigned = {}
    issues = []

    def track(i, mentioned=False):
        repo_name = i.repository.full_name
        if (repo_name, i.number) in assigned:
            return
        assigned[(repo_name, i.number)] = i
        issues.append({
            "repo": repo_name, "number": i.number,
            "author": i.user.login,
            "assignees": [a.login for a in i.assignees],
            "body": i.body, "is_pr": i.pull_request is not None,
            "mentioned": mentioned,
        })

    for i in me.get_issues():
        track(i)

    # open issues (and PRs) where the owner @mentioned the bot, across all
    # repos. Search API requires an explicit is:issue / is:pull-request.
    for kind in ("is:issue", "is:pull-request"):
        try:
            for i in gh.search_issues(f"mentions:{bot} author:{owner} is:open {kind}"):
                track(i, mentioned=True)
        except Exception as e:
            log(f"mention search failed ({kind}): {e}")

    # open PRs authored by the bot, across all repos
    bot_prs = {}
    try:
        for i in gh.search_issues(f"is:pull-request is:open author:{bot}"):
            bot_prs[(i.repository.full_name, i.number)] = i
    except Exception as e:
        ok = False  # unknown bot PRs → their comments would be lost this window
        log(f"PR search failed: {e}")

    comments = []
    active_repos = {rn for rn, _ in assigned} | {rn for rn, _ in bot_prs}
    for repo_name in sorted(active_repos):
        try:
            repo = gh.get_repo(repo_name)

            def eligible(number):
                if (repo_name, number) in assigned:
                    return True, assigned[(repo_name, number)].pull_request is not None
                return (repo_name, number) in bot_prs, True

            # owner comments on eligible threads
            for c in repo.get_issues_comments(since=since):
                if c.user.login != owner:
                    continue
                number = int(c.issue_url.rsplit("/", 1)[1])
                ok, is_pr = eligible(number)
                if ok:
                    comments.append({"key": f"{repo_name}/c{c.id}", "repo": repo_name,
                                     "number": number, "author": c.user.login,
                                     "body": c.body, "is_pr": is_pr})

            # owner review comments on bot PRs
            for rc in repo.get_pulls_review_comments(since=since):
                if rc.user.login != owner:
                    continue
                number = int(rc.pull_request_url.rsplit("/", 1)[1])
                if (repo_name, number) in bot_prs:
                    comments.append({"key": f"{repo_name}/rc{rc.id}", "repo": repo_name,
                                     "number": number, "author": rc.user.login,
                                     "body": rc.body, "is_pr": True})
        except Exception as e:
            ok = False  # comments in this window would be lost if last_poll advances
            log(f"poll failed for {repo_name}: {e}")

    # owner reviews (approve / request changes) on bot PRs.
    # ponytail: no `since` filter on reviews API; processed-set dedupes.
    for (repo_name, number), issue in bot_prs.items():
        try:
            for r in issue.as_pull_request().get_reviews():
                if r.user and r.user.login == owner:
                    body = f"[review: {r.state}] {r.body or ''}".strip()
                    comments.append({"key": f"{repo_name}/r{r.id}", "repo": repo_name,
                                     "number": number, "author": r.user.login,
                                     "body": body, "is_pr": True})
        except Exception as e:
            # reviews are re-fetched every cycle (no since filter), so a
            # failure here loses nothing — don't hold last_poll back for it
            log(f"review poll failed for {repo_name}#{number}: {e}")

    return issues, comments, ok


def enrich(gh, event):
    """Attach full target + thread context for prompt building."""
    repo = gh.get_repo(event["repo"])
    issue = repo.get_issue(event["number"])
    event["target"] = {"title": issue.title, "body": issue.body}
    event["thread"] = [{"author": c.user.login, "body": c.body}
                       for c in issue.get_comments()]
    if event["is_pr"]:
        pr = repo.get_pull(event["number"])
        event["review_comments"] = [
            {"author": rc.user.login, "path": rc.path, "line": rc.line, "body": rc.body}
            for rc in pr.get_review_comments()]
    return event


# ---------------------------------------------------------------- job runner

def report(gh, config, repo_name, number, body):
    # GitHub caps comments at 65536 chars; keep the head, note the cut
    text = f"{MARKER} @{config['owner']} {redact(body)}"
    if len(text) > 65536:
        text = text[:65400] + "\n\n…(truncated)"
    gh.get_repo(repo_name).get_issue(number).create_comment(text)


def run_agent(config, prompt, cwd):
    runner = config.get("runner", DEFAULT_RUNNER)
    allowed = config.get("allowed_tools", "Read,Edit,Write,Grep,Glob,Bash(git:*)")
    cmd = [a.replace("{prompt}", prompt).replace("{allowed_tools}", allowed)
           for a in runner]
    env = {**os.environ, **{k: v for k, v in config.get("env", {}).items() if v}}
    timeout = config.get("job_timeout", 3600)
    # new session = own process group, so a timeout kills the agent AND
    # everything it spawned (tool calls, test runs), not just the top process
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise AgentTimeout(f"agent timed out after {timeout}s and was killed")
    if proc.returncode != 0:
        err = f"{stderr}\n{stdout}".strip()
        if "usage limit" in err.lower() or "rate limit" in err.lower():
            # claude's limit message sometimes carries the reset epoch
            m = re.search(r"\b(1[6-9]\d{8})\b", err)
            raise UsageLimit(f"usage limit reached: {err[:200]}",
                             int(m.group(1)) if m else None)
        raise RuntimeError(f"agent exited {proc.returncode}: {err[:500]}")
    out = stdout.strip()
    try:  # claude --output-format json; other runners fall through to raw text
        data = json.loads(out)
        if isinstance(data, dict) and data.get("result"):
            out = data["result"]
    except json.JSONDecodeError:
        pass
    return out.strip()[-2000:] or "(no output)"


def auth_url(config, repo_name):
    return f"https://x-access-token:{config['bot_token']}@github.com/{repo_name}.git"


def salvage_path(config, repo_name, number):
    d = Path(config.get("salvage_dir") or Path(__file__).parent / "salvage")
    return d / f"{repo_name.replace('/', '_')}#{number}.bundle"


def run_job(gh, config, event):
    repo_name, number = event["repo"], event["number"]
    repo = gh.get_repo(repo_name)

    # where does the branch live? direct push if collaborator, else a fork
    # (create_fork is idempotent — returns the existing fork if there is one)
    if event["is_pr"]:
        pr = repo.get_pull(number)
        if pr.head.repo is None:
            raise RuntimeError(f"head repository of PR #{number} is gone "
                               "(fork deleted?) — nowhere to push")
        branch, push_repo = pr.head.ref, pr.head.repo.full_name
    else:
        branch = f"overseer/issue-{number}"
        if repo.permissions and repo.permissions.push:
            push_repo = repo_name
        else:
            push_repo = repo.create_fork().full_name
            log(f"no push access to {repo_name}; contributing via fork {push_repo}")

    log(f"job start: {repo_name}#{number} ({event['kind']})")
    report(gh, config, repo_name, number, "🤖 agent has picked up this task and is working on it.")
    with tempfile.TemporaryDirectory() as tmp:
        sh(["git", "clone", "--depth", "50", auth_url(config, repo_name), tmp])
        sh(["git", "config", "user.name", config["bot_login"]], cwd=tmp)
        sh(["git", "config", "user.email",
            f"{config['bot_login']}@users.noreply.github.com"], cwd=tmp)
        # drop the token from .git/config: the agent runs in this checkout
        # with git access; all later fetch/push calls use explicit URLs
        sh(["git", "remote", "set-url", "origin",
            f"https://github.com/{repo_name}.git"], cwd=tmp)
        base = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=tmp).strip()

        push_url = auth_url(config, push_repo)
        bundle = salvage_path(config, repo_name, number)
        restored = False
        fetched = subprocess.run(["git", "fetch", push_url, branch], cwd=tmp,
                                 capture_output=True)
        if fetched.returncode == 0:  # branch exists remotely (PR or prior run)
            sh(["git", "checkout", "-B", branch, "FETCH_HEAD"], cwd=tmp)
        elif bundle.exists():  # work salvaged from a failed push last run
            sh(["git", "fetch", str(bundle), branch], cwd=tmp)
            sh(["git", "checkout", "-B", branch, "FETCH_HEAD"], cwd=tmp)
            restored = True
            log(f"restored salvaged work from {bundle}")
        else:
            sh(["git", "checkout", "-b", branch], cwd=tmp)
        start_ref = sh(["git", "rev-parse", "HEAD"], cwd=tmp).strip()

        resume_at = None
        report_md = Path(tmp) / ".overseer-report.md"
        try:
            summary = run_agent(config, build_prompt(event, config["owner"],
                                                     config.get("context_limit", 50)), tmp)
            if report_md.exists():  # full report beats truncated stdout
                summary = report_md.read_text().strip() or summary
        except UsageLimit as e:
            resume_at = e.resume_at or time.time() + config.get("retry_delay", 3600)
            when = datetime.fromtimestamp(resume_at, timezone.utc).strftime("%H:%M UTC")
            summary = f"⚠️ {e} — pushed any partial work; auto-resuming around {when}."
        except (AgentTimeout, RuntimeError) as e:
            # salvage: agent died (timeout, crash) — push whatever subtasks were
            # committed. Branch + issue thread are the durable state; a
            # follow-up comment resumes from the pushed commits.
            summary = (f"⚠️ {e} — pushed any partial work. "
                       "Reply here to resume from where it stopped.")
        report_md.unlink(missing_ok=True)  # never commit the report file

        # commit anything the agent left uncommitted, then count work
        subprocess.run(["git", "add", "-A"], cwd=tmp, capture_output=True)
        subprocess.run(["git", "commit", "-m", "overseer: uncommitted changes"],
                       cwd=tmp, capture_output=True)
        commits = int(sh(["git", "rev-list", "--count", f"{start_ref}..HEAD"],
                         cwd=tmp).strip())
        if commits == 0 and not restored:
            report(gh, config, repo_name, number, summary)
            return resume_at

        def try_push(url):
            for attempt in range(3):  # a fresh fork can take a moment to be pushable
                p = subprocess.run(["git", "push", url, f"{branch}:{branch}"],
                                   cwd=tmp, capture_output=True, text=True)
                if p.returncode == 0 or attempt == 2:
                    return p
                time.sleep(5 * (attempt + 1))

        pushed = try_push(push_url)
        if pushed.returncode != 0 and "403" in pushed.stderr and push_repo == repo_name:
            # no write access after all (revoked mid-job, stale permission
            # check) — fall back to contributing via a fork
            log(f"no push access to {repo_name}; attempting to contribute via fork")
            try:
                push_repo = repo.create_fork().full_name
                push_url = auth_url(config, push_repo)
                log(f"pushing to fork {push_repo}")
                pushed = try_push(push_url)
            except Exception as fork_err:
                log(f"fork-and-push failed: {fork_err}")
        if pushed.returncode != 0:
            # preserve the commits locally so the next run resumes from them
            bundle.parent.mkdir(parents=True, exist_ok=True)
            sh(["git", "bundle", "create", str(bundle), branch], cwd=tmp)
            raise RuntimeError(
                f"push to {push_repo} failed: {pushed.stderr.strip()[:300]} — "
                f"work preserved in {bundle.name}; fix access and re-trigger "
                "with a comment to resume")
        bundle.unlink(missing_ok=True)  # work is on the remote now

        head = f"{push_repo.split('/')[0]}:{branch}"
        existing = list(repo.get_pulls(state="open", head=head))
        if existing:
            report(gh, config, repo_name, number,
                   f"pushed {commits} commit(s) to {existing[0].html_url}\n\n{summary}")
        elif not event["is_pr"]:
            pr = repo.create_pull(
                base=base, head=head,
                title=f"overseer: {event['target']['title']}"[:100],
                body=f"Closes #{number}\n\n{redact(summary)}"[:65536])
            report(gh, config, repo_name, number, f"opened PR: {pr.html_url}")
        else:
            # is_pr job that ended up on a fork branch the PR doesn't use
            # (e.g. 403 fallback on an owner-authored PR) — say where the work is
            report(gh, config, repo_name, number,
                   f"pushed {commits} commit(s) to {push_repo}@{branch}, but no open "
                   f"PR uses that branch — merge or open a PR manually\n\n{summary}")
    log(f"job done: {repo_name}#{number}")
    return resume_at


# ---------------------------------------------------------------- main loop

def queue_retry(gh, config, state, event, resume_at, attempts):
    """Schedule a usage-limited job to re-run when the limit window resets."""
    repo, number = event["repo"], event["number"]
    if attempts > 5:
        log(f"giving up after {attempts - 1} limit retries: {repo}#{number}")
        try:  # the thread was promised auto-resume — say we stopped
            report(gh, config, repo, number,
                   f"⚠️ giving up after {attempts - 1} usage-limit retries — "
                   "reply here to try again.")
        except Exception:
            pass
        return
    if any(r["event"]["repo"] == repo and r["event"]["number"] == number
           for r in state.get("retries", [])):
        return
    slim = {k: event[k] for k in
            ("repo", "number", "kind", "keys", "directives", "is_pr")}
    state.setdefault("retries", []).append(
        {"event": slim, "due": resume_at, "attempts": attempts})
    log(f"retry queued for {repo}#{number} at {resume_at:.0f} (attempt {attempts})")


def run_and_maybe_retry(gh, config, state, event, attempts=0):
    try:
        resume_at = run_job(gh, config, enrich(gh, event))
    except Exception as e:
        log(f"job failed: {event['repo']}#{event['number']}: {e}")
        try:
            report(gh, config, event["repo"], event["number"], f"job failed: {e}")
        except Exception:
            pass
        return
    if resume_at:
        queue_retry(gh, config, state, event, resume_at, attempts + 1)


def cycle(gh, config, state, state_path):
    now = utcnow()
    processed = set(state["processed"])

    # re-run jobs whose usage-limit window should have reset
    pending = state.get("retries", [])
    due = [r for r in pending if r["due"] <= time.time()]
    state["retries"] = [r for r in pending if r["due"] > time.time()]
    for r in due:
        run_and_maybe_retry(gh, config, state, r["event"], r["attempts"])

    issues, comments, ok = poll(gh, config, parse_ts(state["last_poll"]))
    for event in find_events(config["owner"], config["bot_login"],
                             issues, comments, processed):
        processed.update(event["keys"])
        state["processed"] = sorted(processed)
        state_path.write_text(json.dumps(state, indent=1))
        run_and_maybe_retry(gh, config, state, event)
    if ok:  # partial poll failure: keep last_poll so the next poll re-sweeps
        state["last_poll"] = now
    state_path.write_text(json.dumps(state, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    ap.add_argument("--once", action="store_true", help="single poll cycle, then exit")
    args = ap.parse_args()
    config = json.loads(Path(args.config).read_text())
    config["bot_token"] = config.get("bot_token") or os.environ["GH_BOT_TOKEN"]
    SECRETS.append(config["bot_token"])
    SECRETS.extend(v for v in config.get("env", {}).values() if v)
    gh = Github(auth=Auth.Token(config["bot_token"]))
    config["bot_login"] = gh.get_user().login
    state_path = Path(config.get("state_file") or Path(args.config).parent / "state.json")
    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        state = {"last_poll": utcnow(), "processed": []}
    log(f"gh-overseer as @{config['bot_login']}, owner @{config['owner']}")
    while True:
        try:
            cycle(gh, config, state, state_path)
        except Exception as e:
            # GitHub outage or transient API failure — state is persisted,
            # next cycle re-polls the gap via last_poll. Just wait it out.
            log(f"cycle failed (GitHub down?): {e}")
        if args.once:
            break
        time.sleep(config.get("poll_interval", 60))


if __name__ == "__main__":
    main()
