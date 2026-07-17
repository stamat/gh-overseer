# /// script
# requires-python = ">=3.9"
# dependencies = ["PyGithub"]
# ///
"""Tests for the pure logic in overseer.py — no network, no subprocess."""

import unittest

import overseer
from overseer import build_prompt, find_events, pick_runner

OWNER, BOT = "stamat", "stamat-bot"


def issue(number=1, author=OWNER, assignees=(BOT,), body="do it", is_pr=False,
          mentioned=False):
    return {"repo": "o/r", "number": number, "author": author,
            "assignees": list(assignees), "body": body, "is_pr": is_pr,
            "mentioned": mentioned}


def comment(cid=10, number=1, author=OWNER, body="also this", is_pr=False, prefix="c"):
    return {"key": f"o/r/{prefix}{cid}", "repo": "o/r", "number": number,
            "author": author, "body": body, "is_pr": is_pr}


class TestFindEvents(unittest.TestCase):
    def find(self, issues=(), comments=(), processed=()):
        return find_events(OWNER, BOT, list(issues), list(comments), set(processed))

    def test_assigned_issue_by_owner_becomes_work(self):
        events = self.find(issues=[issue()])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "work")
        self.assertEqual(events[0]["keys"], ["o/r#1"])
        self.assertEqual(events[0]["directives"], ["do it"])

    def test_issue_by_stranger_ignored(self):
        self.assertEqual(self.find(issues=[issue(author="stranger")]), [])

    def test_issue_not_assigned_to_bot_ignored(self):
        self.assertEqual(self.find(issues=[issue(assignees=["someone"])]), [])

    def test_mentioned_issue_becomes_work_without_assignment(self):
        events = self.find(issues=[issue(assignees=[], mentioned=True)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "work")

    def test_mentioned_issue_by_stranger_ignored(self):
        self.assertEqual(self.find(issues=[issue(author="stranger", assignees=[],
                                                 mentioned=True)]), [])

    def test_processed_issue_skipped(self):
        self.assertEqual(self.find(issues=[issue()], processed={"o/r#1"}), [])

    def test_owner_comment_becomes_followup(self):
        events = self.find(comments=[comment()])
        self.assertEqual(events[0]["kind"], "followup")
        self.assertEqual(events[0]["directives"], ["also this"])

    def test_bot_own_comment_ignored(self):
        self.assertEqual(self.find(comments=[comment(author=BOT)]), [])

    def test_stranger_comment_ignored(self):
        self.assertEqual(self.find(comments=[comment(author="stranger")]), [])

    def test_processed_comment_skipped(self):
        self.assertEqual(self.find(comments=[comment()], processed={"o/r/c10"}), [])

    def test_events_grouped_per_thread(self):
        events = self.find(issues=[issue()],
                           comments=[comment(cid=10), comment(cid=11, prefix="rc")])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "work")
        self.assertEqual(sorted(events[0]["keys"]),
                         ["o/r#1", "o/r/c10", "o/r/rc11"])
        self.assertEqual(len(events[0]["directives"]), 3)

    def test_separate_threads_separate_events(self):
        events = self.find(comments=[comment(number=1), comment(cid=11, number=2)])
        self.assertEqual(len(events), 2)

    def test_null_body_handled(self):
        events = self.find(issues=[issue(body=None)], comments=[comment(body=None)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["directives"], [])


class TestBuildPrompt(unittest.TestCase):
    def event(self, **kw):
        e = {"repo": "o/r", "number": 1, "is_pr": False, "kind": "work",
             "directives": ["add tests"],
             "target": {"title": "my issue", "body": "body text"},
             "thread": [{"author": OWNER, "body": "context here"}]}
        e.update(kw)
        return e

    def test_contains_title_directive_and_thread(self):
        p = build_prompt(self.event(), OWNER)
        self.assertIn("my issue", p)
        self.assertIn("add tests", p)
        self.assertIn("context here", p)
        self.assertIn("Do NOT push", p)
        self.assertIn(f"@{OWNER}", p)

    def test_pr_includes_review_comments(self):
        p = build_prompt(self.event(is_pr=True, review_comments=[
            {"author": OWNER, "path": "a.py", "line": 3, "body": "rename this"}]), OWNER)
        self.assertIn("PR #1", p)
        self.assertIn("a.py:3: rename this", p)


class TestPickRunner(unittest.TestCase):
    CONFIG = {"runners": {"claude": ["claude"], "mini": ["mini"]}}

    def event(self, body="", thread=(), directives=()):
        return {"target": {"body": body}, "thread": list(thread),
                "directives": list(directives)}

    def pick(self, **kw):
        return pick_runner(self.CONFIG, self.event(**kw), OWNER)

    def test_no_line_picks_first_runner(self):
        self.assertEqual(self.pick(body="do stuff", directives=["more stuff"]),
                         ["claude"])

    def test_no_line_no_runners_map_returns_none(self):
        self.assertIsNone(pick_runner({}, self.event(body="do stuff"), OWNER))

    def test_line_in_body(self):
        self.assertEqual(self.pick(body="fix it\n\nrun_agent: mini"), ["mini"])

    def test_last_line_wins_across_thread(self):
        self.assertEqual(self.pick(
            body="run_agent: mini",
            thread=[{"author": OWNER, "body": "run_agent: claude"}]), ["claude"])

    def test_non_owner_thread_comment_ignored(self):
        self.assertEqual(self.pick(
            thread=[{"author": "stranger", "body": "run_agent: mini"}]), ["claude"])

    def test_unknown_name_raises_with_configured_list(self):
        with self.assertRaisesRegex(RuntimeError, "calude.*claude, mini"):
            self.pick(directives=["run_agent: calude"])

    def test_mid_prose_mention_ignored(self):
        self.assertEqual(self.pick(body="we could run_agent: mini someday"),
                         ["claude"])

    def test_trailing_text_ignored(self):
        self.assertEqual(self.pick(body="run_agent: mini please"), ["claude"])

    def test_own_line_in_multiline_body_matches(self):
        self.assertEqual(self.pick(body="fix the bug\nrun_agent: mini\nthanks"),
                         ["mini"])

    def test_list_item_form_matches(self):
        self.assertEqual(self.pick(body="- run_agent: mini"), ["mini"])


class TestRedact(unittest.TestCase):
    def test_scrubs_every_registered_secret(self):
        overseer.SECRETS[:] = ["ghp_secret", "sk-ant-oat-x"]
        try:
            self.assertEqual(
                overseer.redact("push to https://x:ghp_secret@x failed, sk-ant-oat-x"),
                "push to https://x:***@x failed, ***")
        finally:
            overseer.SECRETS.clear()


class TestAckMessage(unittest.TestCase):
    EVENT = {"is_pr": False, "target": {"title": "fix login"}}

    def test_no_model_uses_canned_line(self):
        self.assertIn(overseer.ack_message({}, self.EVENT), overseer.ACKS)

    def test_ollama_failure_falls_back_to_canned_line(self):
        # unroutable url → urlopen raises → canned ack, no exception
        cfg = {"ack_model": "llama3.2", "ollama_url": "http://127.0.0.1:1"}
        self.assertIn(overseer.ack_message(cfg, self.EVENT), overseer.ACKS)


class TestHumanize(unittest.TestCase):
    TEXT = "opened PR: https://github.com/o/r/pull/7"

    def test_no_model_returns_text_unchanged(self):
        self.assertEqual(overseer.humanize({}, self.TEXT), self.TEXT)

    def test_ollama_failure_returns_text_unchanged(self):
        cfg = {"ack_model": "llama3.2", "ollama_url": "http://127.0.0.1:1"}
        self.assertEqual(
            overseer.humanize(cfg, self.TEXT, keep=("https://github.com/o/r/pull/7",)),
            self.TEXT)

    def test_dropped_fact_rejects_rewrite(self):
        # model reply that lost the URL → original text must post
        cfg = {"ack_model": "m"}
        orig, overseer.ollama = overseer.ollama, lambda c, p: "All done, boss!"
        try:
            self.assertEqual(
                overseer.humanize(cfg, self.TEXT,
                                  keep=("https://github.com/o/r/pull/7",)),
                self.TEXT)
        finally:
            overseer.ollama = orig


if __name__ == "__main__":
    unittest.main()
