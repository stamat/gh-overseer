# /// script
# requires-python = ">=3.9"
# dependencies = ["PyGithub"]
# ///
"""Tests for the pure logic in overseer.py — no network, no subprocess."""

import unittest

from overseer import build_prompt, find_events

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


if __name__ == "__main__":
    unittest.main()
