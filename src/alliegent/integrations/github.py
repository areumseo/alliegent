"""A day of a repository: what landed, what is in review, what is open.

**What landed is read from the default branch, then grouped by pull request.**
Listing merged pull requests alone would miss what was pushed straight to the
branch; listing commits alone would repeat a pull request once per commit and,
worse, miss one merged with a merge commit, whose commits keep the dates they
were written on -- often a day earlier, when they were not yet on the branch.
So: every commit that reached the branch in the day's window, each asked which
merged pull request it came in with. A pull request is one line however many
commits it had; a commit with none is a direct push and gets its own line; a
merge commit with none says nothing a line would.

Public repositories need no token. A private one returns 404 without it,
which is reported as that rather than as an empty day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

API = "https://api.github.com"


class GitHubError(Exception):
    """A repository that could not be read, with the reason in plain words."""


@dataclass(frozen=True)
class Line:
    text: str
    url: str


@dataclass
class Activity:
    repo: str
    done: list[Line] = field(default_factory=list)
    in_review: list[Line] = field(default_factory=list)
    issues: list[Line] = field(default_factory=list)


class GitHub:
    def __init__(self, token: str = "", *, client: httpx.AsyncClient | None = None) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "alliegent",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(base_url=API, headers=headers, timeout=20)
        if client is not None:
            self._client.headers.update(headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, repo: str, path: str, **params) -> list | dict:
        response = await self._client.get(f"/repos/{repo}{path}", params=params or None)
        if response.status_code == 404:
            raise GitHubError(f"{repo} not found — private without GITHUB_TOKEN, or renamed")
        if response.status_code in (403, 429):
            raise GitHubError(f"{repo}: GitHub refused the request ({response.status_code})")
        response.raise_for_status()
        return response.json()

    async def day(self, repo: str, day: date, tz: ZoneInfo) -> Activity:
        """Everything worth a line for one repository on one local day."""
        activity = Activity(repo)
        branch = (await self._get(repo, ""))["default_branch"]

        start = datetime.combine(day, time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        commits = await self._get(
            repo,
            "/commits",
            sha=branch,
            since=start.isoformat(),
            until=end.isoformat(),
            per_page=100,
        )
        seen_prs: set[int] = set()
        for commit in reversed(commits):  # oldest first, the order work happened in
            pulls = await self._get(repo, f"/commits/{commit['sha']}/pulls")
            merged = [p for p in pulls if p.get("merged_at")]
            if merged:
                pr = merged[0]
                if pr["number"] not in seen_prs:
                    seen_prs.add(pr["number"])
                    activity.done.append(Line(f"{pr['title']} (#{pr['number']})", pr["html_url"]))
                continue
            if len(commit.get("parents", [])) > 1:
                continue  # a merge with no pull request: nothing a line would add
            subject = commit["commit"]["message"].splitlines()[0]
            activity.done.append(Line(subject, commit["html_url"]))

        for pr in await self._get(repo, "/pulls", state="open", per_page=50):
            activity.in_review.append(Line(f"{pr['title']} (#{pr['number']})", pr["html_url"]))
        for issue in await self._get(repo, "/issues", state="open", per_page=50):
            if "pull_request" in issue:
                continue  # the issues endpoint lists pull requests too
            activity.issues.append(
                Line(f"{issue['title']} (#{issue['number']})", issue["html_url"])
            )
        return activity
