"""Thin wrapper over the `gh` CLI: one GraphQL round-trip per poll."""
from __future__ import annotations

import json
import subprocess
import time

_PR_FIELDS = """
  ... on PullRequest {
    number title url isDraft reviewDecision mergeable mergeStateStatus headRefOid
    repository { nameWithOwner }
    author { login __typename }
    reviewRequests { totalCount }
    commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
    reviews(last: 30) {
      nodes { id state body submittedAt author { login __typename } commit { oid } }
    }
    comments(last: 30) {
      nodes { id author { login __typename } }
    }
    reviewThreads(first: 50) {
      nodes {
        id isResolved
        comments(last: 20) { nodes { id author { login __typename } } }
      }
    }
  }
"""

_QUERY = f"""
query($q: String!) {{
  search(query: $q, type: ISSUE, first: 50) {{ nodes {{ {_PR_FIELDS} }} }}
}}
"""


class GhError(RuntimeError):
    pass


def _gh(*args: str) -> str:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:
        raise GhError("gh CLI not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise GhError("gh timed out") from e
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    return proc.stdout


def viewer_login() -> str:
    return _gh("api", "user", "--jq", ".login").strip()


def fetch(me: str) -> dict[str, list[dict]]:
    """Return open PRs grouped by my relationship to them.

    `authored`: I opened it. `requested`: my review is pending.
    `reviewed`: I already reviewed (review-request is cleared on submit) and
    I'm not the author, so author replies can still be routed to me.
    """
    searches = {
        "authored": f"is:pr is:open archived:false author:{me}",
        "requested": f"is:pr is:open archived:false review-requested:{me}",
        "reviewed": f"is:pr is:open archived:false reviewed-by:{me} -author:{me}",
    }
    # One search per request: a combined query exceeds GitHub's resource limits (502).
    return {name: _search(q) for name, q in searches.items()}


def _search(q: str) -> list[dict]:
    # GitHub intermittently returns 502s or empty bodies for these heavy queries.
    for attempt in range(3):
        try:
            data = json.loads(_gh("api", "graphql", "-f", f"query={_QUERY}", "-f", f"q={q}"))
            break
        except (GhError, json.JSONDecodeError):
            if attempt == 2:
                raise
            time.sleep(3)
    if data.get("errors"):
        raise GhError(json.dumps(data["errors"]))
    return [n for n in data["data"]["search"]["nodes"] if n]
