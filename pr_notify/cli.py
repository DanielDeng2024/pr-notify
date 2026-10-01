from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import agent, github, notify, state
from .events import Event, all_events, pr_ref


def _diff(prev: dict[str, set[str]] | None, ref: str, events: list[Event]) -> list[Event]:
    if prev is None:  # first run: record baseline only
        return []
    if ref not in prev:  # newly seen PR: skip backlog, keep current conditions
        return [e for e in events if e.stateful]
    return [e for e in events if e.key not in prev[ref]]


# Re-armable: a new review request after the old one cleared is new activity.
_REARM = {"review_requested"}


def seen_after(prev_keys: set[str], events: list[Event]) -> set[str]:
    """Keys to remember for a PR. Seen keys persist while the PR stays open, so a
    condition that clears and returns (CI rerun failing again on the same commit)
    does not notify twice."""
    return {e.key for e in events} | (prev_keys - _REARM)


def poll(path: Path, *, dry_run: bool = False, quiet: bool = False) -> dict:
    me = github.viewer_login()
    events_by_pr = all_events(github.fetch(me), me)
    prev = state.load(path)

    saved: dict[str, set[str]] = {}
    notified = notify_errors = 0
    items = []
    for ref, (pr, events) in events_by_pr.items():
        saved[ref] = seen_after((prev or {}).get(ref, set()), events)
        now = [e for e in events if e.stateful]
        items.append({"ref": ref, "title": pr["title"], "url": pr["url"], "draft": pr["isDraft"],
                      "mine": (pr["author"] or {}).get("login") == me,
                      "text": "; ".join(dict.fromkeys(e.text for e in now))})
        fresh = [e for e in _diff(prev, ref, events) if e.notify]
        if not fresh:
            continue
        body = notify.summarize(fresh)
        title = f"{ref} {pr['title']}"
        print(f"{time.strftime('%H:%M:%S')} {title}\n  {body.replace(chr(10), chr(10) + '  ')}\n  {pr['url']}", flush=True)
        if dry_run or quiet:
            continue
        try:
            notify.send(title, body, pr["url"], ident=ref)
            notified += 1
        except (notify.NotifyError, subprocess.TimeoutExpired) as e:
            # Leave these keys out of saved state so the next poll retries them.
            print(f"  notification failed: {e}", file=sys.stderr, flush=True)
            notify_errors += 1
            saved[ref] -= {e.key for e in fresh}

    if not dry_run:
        state.save(path, saved)
    if prev is None:
        print(f"baseline recorded for {len(events_by_pr)} open PRs; notifying on changes from now on")
    return {"prs": len(events_by_pr), "notified": notified, "notify_errors": notify_errors, "items": items}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tracked_poll(path: Path, **kw) -> dict | None:
    """poll() plus a status.json next to the state file, read by the menu bar app."""
    status_file = path.with_name("status.json")
    try:
        prev = json.loads(status_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        prev = {}
    started = time.monotonic()
    record = {"last_run": _now(), "last_success": prev.get("last_success"), "ok": True, "error": None,
              "prs": prev.get("prs", 0), "notified": 0, "notify_errors": 0, "items": prev.get("items", [])}
    result = None
    try:
        result = poll(path, **kw)
        record.update(result, last_success=record["last_run"])
    except Exception as e:  # health must be reported even for unexpected failures
        record.update(ok=False, error=str(e) or type(e).__name__)
    record["duration"] = round(time.monotonic() - started, 1)
    if not kw.get("dry_run"):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(record))
        tmp.replace(status_file)
    if not record["ok"]:
        raise github.GhError(record["error"])
    return result


def status() -> None:
    """Print conditions that currently need action (no history, no state)."""
    me = github.viewer_login()
    shown = False
    for ref, (pr, events) in all_events(github.fetch(me), me).items():
        now = [e for e in events if e.stateful]
        if now:
            shown = True
            print(f"{ref} {pr['title']}\n  {notify.summarize(now).replace(chr(10), chr(10) + '  ')}\n  {pr['url']}")
    if not shown:
        print("nothing needs you right now")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pr-notify", description="Notify on GitHub PR activity that needs you (via gh).")
    p.add_argument("--state", type=Path, default=state.default_path(), help="state file (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    once = sub.add_parser("once", help="poll once (use from cron/launchd)")
    once.add_argument("--dry-run", action="store_true", help="print what would notify; don't notify or save state")
    once.add_argument("--quiet", action="store_true", help="update state and print, but no desktop notification")

    watch = sub.add_parser("watch", help="poll forever")
    watch.add_argument("--interval", type=int, default=120, help="seconds between polls (default: %(default)s)")

    sub.add_parser("install-notifier", help="build PRNotify.app and request notification permission")
    sub.add_parser("test-notify", help="send a test notification")
    sub.add_parser("install-agent", help="install the menu bar app as a login item (launchd)")
    sub.add_parser("uninstall-agent", help="remove the login item")
    sub.add_parser("status", help="show what needs you right now")
    sub.add_parser("reset", help="forget state; next poll re-baselines")

    args = p.parse_args(argv)
    try:
        if args.cmd == "once":
            tracked_poll(args.state, dry_run=args.dry_run, quiet=args.quiet)
        elif args.cmd == "watch":
            while True:
                try:
                    tracked_poll(args.state)
                except github.GhError as e:
                    print(f"{time.strftime('%H:%M:%S')} poll failed: {e}", file=sys.stderr, flush=True)
                time.sleep(args.interval)
        elif args.cmd == "install-notifier":
            notify.install_helper()
            code, out = notify.check()
            print(out)
            if code != 0:
                print("Enable PRNotify in System Settings > Notifications, then re-run.", file=sys.stderr)
            return code
        elif args.cmd == "test-notify":
            notify.send("pr-notify", "Test notification. Click to open GitHub.", "https://github.com/pulls", ident="test")
        elif args.cmd == "install-agent":
            agent.install(args.state)
        elif args.cmd == "uninstall-agent":
            agent.uninstall()
        elif args.cmd == "status":
            status()
        elif args.cmd == "reset":
            args.state.unlink(missing_ok=True)
    except (github.GhError, notify.NotifyError, agent.AgentError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
