"""Turn a PR snapshot into the set of actionable events that are currently true.

Pure functions, no I/O. Each event has a stable `key`; the caller diffs keys
between polls, so an event fires exactly once when its key first appears.

`stateful` events describe a condition (CI red, ready to merge, ...) and are
allowed to fire for a PR we haven't seen before. Activity events (comments,
reviews, new pushes) describe history and are suppressed on a PR's first
sighting so we don't replay its backlog.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    key: str
    text: str
    stateful: bool = False
    notify: bool = True  # False: shown in the menu/badge but never sent as a notification


def pr_ref(pr: dict) -> str:
    return f"{pr['repository']['nameWithOwner']}#{pr['number']}"


def _human(author: dict | None) -> str | None:
    if not author or author.get("__typename") == "Bot":
        return None
    return author["login"]


def _thread_comments(pr: dict) -> list[tuple[dict, dict]]:
    return [(t, c) for t in pr["reviewThreads"]["nodes"] for c in t["comments"]["nodes"]]


def _ci_state(pr: dict) -> str | None:
    nodes = pr["commits"]["nodes"]
    rollup = nodes[0]["commit"]["statusCheckRollup"] if nodes else None
    return rollup["state"] if rollup else None


def authored_events(pr: dict, me: str) -> list[Event]:
    """Things that need me because I own this PR."""
    out: list[Event] = []
    sha = pr["headRefOid"]

    for r in pr["reviews"]["nodes"]:
        who = _human(r["author"])
        if not who or who == me:
            continue
        if r["state"] == "APPROVED":
            out.append(Event(f"review:{r['id']}", f"{who} approved"))
        elif r["state"] == "CHANGES_REQUESTED":
            out.append(Event(f"review:{r['id']}", f"{who} requested changes"))
        elif r["state"] == "COMMENTED" and (r["body"] or "").strip():
            # Body-less COMMENTED reviews are just wrappers for inline
            # comments, which are reported below as comments.
            out.append(Event(f"review:{r['id']}", f"{who} reviewed"))

    comments = pr["comments"]["nodes"] + [c for _, c in _thread_comments(pr)]
    for c in comments:
        who = _human(c["author"])
        if who and who != me:
            out.append(Event(f"comment:{c['id']}", f"{who} commented"))

    ci = _ci_state(pr)
    if ci in ("FAILURE", "ERROR"):
        out.append(Event(f"ci_failed:{sha}", "CI failed", stateful=True))
    if pr["mergeable"] == "CONFLICTING":
        out.append(Event(f"conflict:{sha}", "merge conflict", stateful=True))
    if (
        not pr["isDraft"]
        and pr["reviewDecision"] == "REVIEW_REQUIRED"
        and pr["reviewRequests"]["totalCount"] == 0
    ):
        # e.g. a push dismissed earlier approvals and nobody was re-requested.
        out.append(Event(f"needs_reviewer:{sha}", "needs a reviewer", stateful=True, notify=False))
    if (
        not pr["isDraft"]
        and pr["reviewDecision"] == "APPROVED"
        and ci == "SUCCESS"
        and pr["mergeStateStatus"] == "CLEAN"
    ):
        out.append(Event(f"ready:{sha}", "ready to merge", stateful=True, notify=False))
    return out


def reviewing_events(pr: dict, me: str, requested: bool) -> list[Event]:
    """Things that need me because I'm a reviewer on someone else's PR."""
    out: list[Event] = []
    if pr["isDraft"]:
        return out
    owner = _human(pr["author"])
    sha = pr["headRefOid"]

    if requested:
        out.append(Event("review_requested", f"{owner or 'someone'} requested your review", stateful=True))

    my_reviews = [
        r for r in pr["reviews"]["nodes"]
        if r["author"] and r["author"]["login"] == me and r["state"] != "PENDING"
    ]
    my_thread_ids = {
        t["id"] for t, c in _thread_comments(pr)
        if c["author"] and c["author"]["login"] == me
    }
    engaged = bool(my_reviews or my_thread_ids)

    if engaged and owner:
        for t, c in _thread_comments(pr):
            if t["id"] in my_thread_ids and c["author"] and c["author"]["login"] == owner:
                out.append(Event(f"comment:{c['id']}", f"{owner} replied to your thread"))
        for c in pr["comments"]["nodes"]:
            if c["author"] and c["author"]["login"] == owner:
                out.append(Event(f"comment:{c['id']}", f"{owner} commented"))

    if my_reviews and not requested:
        last = max(my_reviews, key=lambda r: r["submittedAt"] or "")
        if last["commit"] and last["commit"]["oid"] != sha:
            out.append(Event(f"rereview:{sha}", "new commits since your review"))
    return out


def all_events(snapshot: dict[str, list[dict]], me: str) -> dict[str, tuple[dict, list[Event]]]:
    """Map PR ref -> (pr, events) across every relationship, merging duplicates."""
    result: dict[str, tuple[dict, list[Event]]] = {}
    requested_refs = {pr_ref(p) for p in snapshot["requested"]}

    for pr in snapshot["authored"]:
        result[pr_ref(pr)] = (pr, authored_events(pr, me))
    for pr in snapshot["requested"] + snapshot["reviewed"]:
        ref = pr_ref(pr)
        if ref not in result:
            result[ref] = (pr, reviewing_events(pr, me, ref in requested_refs))
    return result
