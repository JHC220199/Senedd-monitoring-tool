"""Publish the briefing where a person can actually read it.

THE PROBLEM THIS SOLVES
-----------------------
Three attempts at "somewhere to read the output" had each failed for the same
underlying reason — I kept choosing places that were technically outputs rather
than places a person would go:

1. `out/index.html` — gitignored, so it never left the runner.
2. A build artifact — a zip file behind three clicks at the foot of a log page.
3. The Actions job summary — better, but it is still a CI log page. You cannot
   bookmark it, the URL changes every run, and nobody opens Actions to read a
   policy briefing.

And the email never arrived because it needs five SMTP secrets that require an
IT request, so "it will work once you configure it" was not a working system.

WHAT THIS DOES INSTEAD — three places, none needing any credential
-----------------------------------------------------------------
* **`BRIEFING.md` in the repository root.** GitHub renders markdown, so this is
  a permanent, bookmarkable, always-current page. It works on a PRIVATE
  repository, which GitHub Pages does not. One URL, forever.

* **A GitHub issue per weekday.** This is the email. GitHub emails watchers when
  an issue is opened, so the briefing lands in Outlook with no SMTP server, no
  app password and no IT ticket. The issue is also a bookmarkable page with a
  date on it, and GitHub's issue search gives full-text search across every
  briefing ever sent, for free.

* **`briefings/2026-W32.md`** — the weekly archive, committed and permanent.

All three use `GITHUB_TOKEN`, which GitHub injects into every workflow run
automatically. There is nothing to configure. That is the point: a system that
needs a credential nobody has yet is not a system.

The SMTP path is still there for when a service mailbox exists — this does not
replace it, it removes the dependency on it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date

API = "https://api.github.com"

# A hidden marker in every issue body, so we can find the previous briefing
# without relying on title matching (titles contain dates that change).
MARKER = "<!-- nrla-senedd-monitor-briefing -->"


class GitHubError(RuntimeError):
    pass


def _request(method: str, path: str, token: str, payload: dict | None = None):
    url = path if path.startswith("http") else f"{API}{path}"
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Content-Type", "application/json")
    # Honest identification, same principle as the collectors.
    request.add_header("User-Agent", "NRLA-PolicyMonitor/1.0")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        raise GitHubError(f"{method} {url} -> {error.code}: {detail}") from error


def find_previous_briefing(repo: str, token: str) -> dict | None:
    """The most recent open briefing issue, or None."""
    issues = _request("GET", f"/repos/{repo}/issues?state=open&per_page=50",
                      token)
    for issue in issues:
        if MARKER in (issue.get("body") or ""):
            return issue
    return None


def ensure_label(repo: str, token: str, name: str = "briefing",
                 colour: str = "113B54") -> None:
    """Create the label if absent. Colour is NRLA dark blue."""
    try:
        _request("GET", f"/repos/{repo}/labels/{name}", token)
    except GitHubError:
        try:
            _request("POST", f"/repos/{repo}/labels", token, {
                "name": name, "color": colour,
                "description": "Automated Senedd policy briefing",
            })
        except GitHubError:
            pass          # a label is a nicety; never fail the run over one


def publish_issue(repo: str, token: str, title: str, body: str,
                  close_previous: bool = True) -> dict:
    """Open a new briefing issue, optionally closing the previous one.

    A new issue per weekday is what produces the daily email — GitHub notifies
    watchers on issue creation, not on issue edit. Closing yesterday's keeps the
    open list showing today only; closed issues stay searchable forever, so
    nothing is lost. Nothing is ever deleted.
    """
    ensure_label(repo, token)
    previous = find_previous_briefing(repo, token) if close_previous else None

    created = _request("POST", f"/repos/{repo}/issues", token, {
        "title": title,
        "body": MARKER + "\n\n" + body,
        "labels": ["briefing"],
    })

    if previous and previous["number"] != created["number"]:
        try:
            _request("PATCH", f"/repos/{repo}/issues/{previous['number']}",
                     token, {"state": "closed"})
        except GitHubError:
            pass          # tidiness only — never fail a delivered briefing
    return created


def issue_title(today: date | None = None) -> str:
    today = today or date.today()
    return f"Senedd briefing — {today.strftime('%d %B %Y')}"


def env_repo_and_token() -> tuple[str, str]:
    """Read the repository and token GitHub provides to every workflow run."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not repo:
        raise GitHubError(
            "GITHUB_REPOSITORY is not set — this command is meant to run "
            "inside GitHub Actions.")
    if not token:
        raise GitHubError(
            "GITHUB_TOKEN is not set. In the workflow, pass it explicitly:\n"
            "  env:\n    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n"
            "and give the job `issues: write` permission.")
    return repo, token
