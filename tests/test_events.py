import unittest

from pr_notify.cli import _diff, seen_after
from pr_notify.events import authored_events, reviewing_events


def user(login, typename="User"):
    return {"login": login, "__typename": typename}


def make_pr(**over):
    pr = {
        "number": 1, "title": "t", "url": "u", "isDraft": False,
        "reviewDecision": None, "mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED",
        "headRefOid": "sha1",
        "reviewRequests": {"totalCount": 0},
        "repository": {"nameWithOwner": "o/r"},
        "author": user("me"),
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "PENDING"}}}]},
        "reviews": {"nodes": []},
        "comments": {"nodes": []},
        "reviewThreads": {"nodes": []},
    }
    pr.update(over)
    return pr


def keys(events):
    return {e.key for e in events}


class AuthoredTests(unittest.TestCase):
    def test_approval_and_changes_requested(self):
        pr = make_pr(reviews={"nodes": [
            {"id": "r1", "state": "APPROVED", "body": "", "author": user("bob")},
            {"id": "r2", "state": "CHANGES_REQUESTED", "body": "", "author": user("amy")},
        ]})
        texts = {e.text for e in authored_events(pr, "me")}
        self.assertEqual(texts, {"bob approved", "amy requested changes"})

    def test_ignores_self_bots_and_empty_commented_reviews(self):
        pr = make_pr(
            reviews={"nodes": [
                {"id": "r1", "state": "COMMENTED", "body": "", "author": user("bob")},
                {"id": "r2", "state": "APPROVED", "body": "", "author": user("me")},
            ]},
            comments={"nodes": [
                {"id": "c1", "author": user("me")},
                {"id": "c2", "author": user("ci", "Bot")},
                {"id": "c3", "author": None},
            ]},
        )
        self.assertEqual(authored_events(pr, "me"), [])

    def test_thread_and_issue_comments(self):
        pr = make_pr(
            comments={"nodes": [{"id": "c1", "author": user("bob")}]},
            reviewThreads={"nodes": [{"id": "t1", "isResolved": False,
                                      "comments": {"nodes": [{"id": "c2", "author": user("amy")}]}}]},
        )
        self.assertEqual(keys(authored_events(pr, "me")), {"comment:c1", "comment:c2"})

    def test_ci_conflict_ready(self):
        failing = make_pr(commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]},
                          mergeable="CONFLICTING")
        self.assertEqual(keys(authored_events(failing, "me")), {"ci_failed:sha1", "conflict:sha1"})

        ready = make_pr(reviewDecision="APPROVED", mergeStateStatus="CLEAN",
                        commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]})
        self.assertEqual(keys(authored_events(ready, "me")), {"ready:sha1"})
        ready["isDraft"] = True
        self.assertEqual(authored_events(ready, "me"), [])


class NeedsReviewerTests(unittest.TestCase):
    def test_needs_reviewer_only_when_required_and_nobody_requested(self):
        pr = make_pr(reviewDecision="REVIEW_REQUIRED")
        events = authored_events(pr, "me")
        self.assertEqual(keys(events), {"needs_reviewer:sha1"})
        self.assertFalse(events[0].notify)  # menu/badge only
        pr["reviewRequests"]["totalCount"] = 1
        self.assertEqual(authored_events(pr, "me"), [])
        pr["reviewRequests"]["totalCount"] = 0
        pr["isDraft"] = True
        self.assertEqual(authored_events(pr, "me"), [])
        pr["isDraft"] = False
        pr["reviewDecision"] = None  # repo doesn't require reviews
        self.assertEqual(authored_events(pr, "me"), [])


class ReviewingTests(unittest.TestCase):
    def test_review_requested(self):
        pr = make_pr(author=user("bob"))
        self.assertEqual(keys(reviewing_events(pr, "me", requested=True)), {"review_requested"})

    def test_author_reply_only_in_my_threads(self):
        threads = [
            {"id": "mine", "isResolved": False, "comments": {"nodes": [
                {"id": "c1", "author": user("me")}, {"id": "c2", "author": user("bob")}]}},
            {"id": "other", "isResolved": False, "comments": {"nodes": [
                {"id": "c3", "author": user("amy")}, {"id": "c4", "author": user("bob")}]}},
        ]
        pr = make_pr(author=user("bob"), reviewThreads={"nodes": threads})
        self.assertEqual(keys(reviewing_events(pr, "me", requested=False)), {"comment:c2"})

    def test_rereview_after_push(self):
        review = {"id": "r", "state": "APPROVED", "body": "", "submittedAt": "2026-01-01T00:00:00Z",
                  "author": user("me"), "commit": {"oid": "old"}}
        pr = make_pr(author=user("bob"), reviews={"nodes": [review]})
        self.assertEqual(keys(reviewing_events(pr, "me", requested=False)), {"rereview:sha1"})
        review["commit"]["oid"] = "sha1"
        self.assertEqual(reviewing_events(pr, "me", requested=False), [])


class OneShotTests(unittest.TestCase):
    def test_flapping_condition_notifies_once(self):
        failing = make_pr(commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]})
        pending = make_pr()
        ref = "o/r#1"
        seen = seen_after(set(), authored_events(failing, "me"))
        # CI reruns (condition clears), then fails again on the same sha.
        seen = seen_after(seen, authored_events(pending, "me"))
        again = _diff({ref: seen}, ref, authored_events(failing, "me"))
        self.assertEqual(again, [])
        # A new push is new activity.
        newer = make_pr(headRefOid="sha2", commits=failing["commits"])
        self.assertEqual(keys(_diff({ref: seen}, ref, authored_events(newer, "me"))), {"ci_failed:sha2"})

    def test_review_request_rearms(self):
        pr = make_pr(author=user("bob"))
        ev = reviewing_events(pr, "me", requested=True)
        seen = seen_after(set(), ev)
        seen = seen_after(seen, reviewing_events(pr, "me", requested=False))  # cleared
        self.assertEqual(keys(_diff({"o/r#1": seen}, "o/r#1", ev)), {"review_requested"})

    def test_ready_to_merge_is_silent(self):
        ready = make_pr(reviewDecision="APPROVED", mergeStateStatus="CLEAN",
                        commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]})
        ev = authored_events(ready, "me")
        self.assertEqual(keys(ev), {"ready:sha1"})
        self.assertFalse(ev[0].notify)


class DiffTests(unittest.TestCase):
    def test_first_run_is_silent(self):
        pr = make_pr(comments={"nodes": [{"id": "c1", "author": user("bob")}]})
        self.assertEqual(_diff(None, "o/r#1", authored_events(pr, "me")), [])

    def test_new_pr_keeps_only_stateful(self):
        pr = make_pr(comments={"nodes": [{"id": "c1", "author": user("bob")}]},
                     commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]})
        fresh = _diff({}, "o/r#1", authored_events(pr, "me"))
        self.assertEqual(keys(fresh), {"ci_failed:sha1"})

    def test_known_pr_fires_only_new_keys(self):
        pr = make_pr(comments={"nodes": [{"id": "c1", "author": user("bob")},
                                         {"id": "c2", "author": user("bob")}]})
        fresh = _diff({"o/r#1": {"comment:c1"}}, "o/r#1", authored_events(pr, "me"))
        self.assertEqual(keys(fresh), {"comment:c2"})


if __name__ == "__main__":
    unittest.main()
