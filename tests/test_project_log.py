"""Each project's day in the #projects forum.

Repository names and titles here are invented; nothing is fetched.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import httpx
import pytest

from alliegent import project_log
from alliegent.agenda import AgendaItem, AgendaService, Project, ProjectService, parse_repos
from alliegent.config import Config
from alliegent.integrations import github as gh
from alliegent.integrations.github import Activity, GitHub, GitHubError, Line
from alliegent.jobs import Jobs

from .conftest import FakeNotionClient, make_page

SEOUL = ZoneInfo("Asia/Seoul")
DAY = date(2026, 9, 28)


# -- naming the repositories -----------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("acme/app", ("acme/app",)),
        ("https://github.com/acme/app/pulls", ("acme/app",)),
        ("https://github.com/acme/app.git", ("acme/app",)),
        ("acme/web, https://github.com/acme/api/pulls", ("acme/web", "acme/api")),
        ("acme/app acme/app", ("acme/app",)),
        ("", ()),
    ],
)
def test_repositories_are_read_however_they_were_pasted(text, expected):
    assert parse_repos(text) == expected


# -- reading a day from GitHub ---------------------------------------------


def commit(sha, message, parents=1):
    return {
        "sha": sha,
        "html_url": f"https://github.com/acme/app/commit/{sha}",
        "commit": {"message": message},
        "parents": [{"sha": f"p{i}"} for i in range(parents)],
    }


def pull(number, title, merged=True):
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/acme/app/pull/{number}",
        "merged_at": "2026-09-28T03:00:00Z" if merged else None,
    }


def fake_github(routes: dict[str, object]) -> GitHub:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path not in routes:
            return httpx.Response(404)
        body = routes[path]
        if isinstance(body, int):
            return httpx.Response(body)
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )
    return GitHub(client=client)


def day_routes(**overrides):
    routes = {
        "/repos/acme/app": {"default_branch": "main"},
        # Newest first, as the API returns them.
        "/repos/acme/app/commits": [
            commit("m1", "Merge branch 'x'", parents=2),
            commit("c3", "Fix a typo straight on main\n\nbody"),
            commit("c2", "Second commit of the PR"),
            commit("c1", "First commit of the PR"),
        ],
        "/repos/acme/app/commits/c1/pulls": [pull(5, "Add the thing")],
        "/repos/acme/app/commits/c2/pulls": [pull(5, "Add the thing")],
        "/repos/acme/app/commits/c3/pulls": [],
        "/repos/acme/app/commits/m1/pulls": [],
        "/repos/acme/app/pulls": [pull(7, "Work in review", merged=False)],
        "/repos/acme/app/issues": [
            {"number": 3, "title": "A real issue", "html_url": "https://x/3"},
            {"number": 7, "title": "Work in review", "html_url": "https://x/7",
             "pull_request": {}},
        ],
    }
    routes.update(overrides)
    return routes


async def test_a_pull_request_is_one_line_however_many_commits_it_had():
    activity = await fake_github(day_routes()).day("acme/app", DAY, SEOUL)
    texts = [line.text for line in activity.done]
    assert texts.count("Add the thing (#5)") == 1


async def test_a_direct_push_gets_its_own_line_by_its_subject():
    activity = await fake_github(day_routes()).day("acme/app", DAY, SEOUL)
    assert "Fix a typo straight on main" in [line.text for line in activity.done]


async def test_a_merge_commit_with_no_pull_request_says_nothing():
    activity = await fake_github(day_routes()).day("acme/app", DAY, SEOUL)
    assert not any("Merge branch" in line.text for line in activity.done)


async def test_work_is_listed_in_the_order_it_happened():
    activity = await fake_github(day_routes()).day("acme/app", DAY, SEOUL)
    assert [line.text for line in activity.done] == [
        "Add the thing (#5)",
        "Fix a typo straight on main",
    ]


async def test_open_pull_requests_are_in_review_not_issues():
    """The issues endpoint lists pull requests too."""
    activity = await fake_github(day_routes()).day("acme/app", DAY, SEOUL)
    assert [line.text for line in activity.in_review] == ["Work in review (#7)"]
    assert [line.text for line in activity.issues] == ["A real issue (#3)"]


async def test_a_repository_it_cannot_see_says_why():
    """A private repository answers 404 without a token, which must not read
    as a quiet day."""
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        await fake_github({}).day("acme/secret", DAY, SEOUL)


# -- the message -----------------------------------------------------------


def project(*repos):
    return Project(id="p1", title="App", status="In progress", next_action="", url="",
                   repos=tuple(repos))


def item(title, *, done=False, day=DAY, canceled=False):
    return AgendaItem(id=title, title=title, day=day, status=None, done=done, url="",
                      canceled=canceled)


def activity(repo="acme/app", done=(), review=(), issues=()):
    def lines(texts):
        return [Line(t, f"https://x/{i}") for i, t in enumerate(texts)]

    return Activity(repo, lines(done), lines(review), lines(issues))


def test_a_day_with_nothing_done_posts_nothing():
    """Review and to-do change slowly; repeating them nightly stops being read."""
    text = project_log.log_message(
        project("acme/app"), DAY, [activity(review=["open PR"], issues=["issue"])], [], [],
        [item("later")],
    )
    assert text is None


def test_a_day_with_work_has_all_three_sections():
    text = project_log.log_message(
        project("acme/app"),
        DAY,
        [activity(done=["Add the thing (#5)"], review=["Open PR (#7)"], issues=["Bug (#3)"])],
        [],
        [item("Wrote the spec", done=True)],
        [item("Ship it", day=date(2026, 9, 30))],
    )
    assert "**Work done**" in text and "[Add the thing (#5)](https://x/0)" in text
    assert "• Wrote the spec" in text
    assert "**In review**" in text and "Open PR (#7)" in text
    assert "**To-do**" in text
    # Agenda first, then issues: what is scheduled is what is next.
    assert text.index("Ship it") < text.index("Bug (#3)")


def test_one_repository_is_not_named_on_every_line():
    text = project_log.log_message(
        project("acme/app"), DAY, [activity(done=["x"])], [], [], []
    )
    assert "· app" not in text


def test_several_repositories_are_told_apart():
    text = project_log.log_message(
        project("acme/web", "acme/api"),
        DAY,
        [activity("acme/web", done=["front"]), activity("acme/api", done=["back"])],
        [], [], [],
    )
    assert "· web" in text and "· api" in text


def test_a_repository_that_cannot_be_read_is_said_even_on_a_quiet_day():
    """It will stay quiet until someone fixes it."""
    text = project_log.log_message(
        project("acme/app"), DAY, [], ["acme/app not found — private without GITHUB_TOKEN"],
        [], [],
    )
    assert "⚠️ Couldn't read acme/app" in text


def test_to_do_is_capped():
    text = project_log.log_message(
        project("acme/app"), DAY, [activity(done=["x"], issues=[f"i{n}" for n in range(9)])],
        [], [], [item(f"t{n}") for n in range(3)],
    )
    todo = text.split("**To-do**")[1]
    assert todo.count("•") == project_log.TODO_LIMIT


# -- the job ---------------------------------------------------------------

DS = "ds_agenda-db"
PROJ_DS = "ds_proj-db"


def project_page(pid, title, repos="", thread=""):
    props = {
        "Name": {"type": "title", "title": [{"plain_text": title, "type": "text"}]},
        "Status": {"type": "status", "status": {"name": "In progress"}},
        "GitHub": {"type": "rich_text", "rich_text": [{"plain_text": repos}] if repos else []},
        "Discord Thread": {
            "type": "rich_text", "rich_text": [{"plain_text": thread}] if thread else []
        },
    }
    return {"object": "page", "id": pid, "url": "", "properties": props}


def build(project_pages, agenda_pages=(), posted=None):
    posted = posted if posted is not None else []

    async def post_project(thread_id, title, message):
        posted.append((thread_id, title, message))
        return thread_id or 999

    async def notify(message, kind):
        pass

    client = FakeNotionClient({DS: list(agenda_pages), PROJ_DS: project_pages})
    config = Config()
    config.agenda.props.project = "Project"
    jobs = Jobs(
        AgendaService(client, config, "agenda-db"),
        ProjectService(client, config, "proj-db"),
        config,
        notify,
        clock=lambda: DAY,
        post_project=post_project,
    )
    return jobs, client, posted


@pytest.fixture
def github_day(monkeypatch):
    """Replace the network with a fixed day per repository."""
    days: dict[str, Activity | Exception] = {}

    async def day(self, repo, on, tz):
        result = days.get(repo, Activity(repo))
        if isinstance(result, Exception):
            raise result
        return result

    async def aclose(self):
        pass

    monkeypatch.setattr(gh.GitHub, "day", day)
    monkeypatch.setattr(gh.GitHub, "aclose", aclose)
    return days


async def test_only_projects_with_a_repository_are_logged(github_day):
    github_day["acme/app"] = activity(done=["x"])
    jobs, _, posted = build(
        [project_page("p1", "App", repos="acme/app"), project_page("p2", "Other")]
    )
    await jobs.run_project_log()
    assert [title for _, title, _ in posted] == ["App"]


async def test_the_first_post_is_remembered_in_notion(github_day):
    github_day["acme/app"] = activity(done=["x"])
    jobs, client, _ = build([project_page("p1", "App", repos="acme/app")])
    await jobs.run_project_log()
    assert ("p1", {"Discord Thread": {"rich_text": [
        {"type": "text", "text": {"content": "999"}}
    ]}}) in client.updated


async def test_a_known_post_is_written_to_and_not_stored_again(github_day):
    github_day["acme/app"] = activity(done=["x"])
    jobs, client, posted = build(
        [project_page("p1", "App", repos="acme/app", thread="555")]
    )
    await jobs.run_project_log()
    assert posted[0][0] == 555
    assert client.updated == []


async def test_a_quiet_day_posts_nothing(github_day):
    jobs, _, posted = build([project_page("p1", "App", repos="acme/app")])
    await jobs.run_project_log()
    assert posted == []


async def test_one_repository_down_does_not_cost_the_project_its_day(github_day):
    github_day["acme/web"] = activity("acme/web", done=["front"])
    github_day["acme/api"] = GitHubError("acme/api not found — private without GITHUB_TOKEN")
    jobs, _, posted = build([project_page("p1", "App", repos="acme/web, acme/api")])
    await jobs.run_project_log()
    message = posted[0][2]
    assert "front" in message and "⚠️ Couldn't read acme/api" in message


async def test_agenda_work_on_the_project_counts_as_done(github_day):
    jobs, _, posted = build(
        [project_page("p1", "App", repos="acme/app")],
        [make_page("a1", "Wrote the spec", day=DAY.isoformat(), status="Done",
                   projects=["p1"])],
    )
    await jobs.run_project_log()
    assert "• Wrote the spec" in posted[0][2]


async def test_a_quiet_night_still_brings_the_projects_database_up_to_date(github_day):
    """The seven-day count and the next item move on days nothing is posted."""
    jobs, client, posted = build([project_page("p1", "App", repos="acme/app")])
    jobs.config.projects.props.done_week = "Done (7d)"
    await jobs.run_project_log()
    assert posted == []
    assert ("p1", {"Done (7d)": {"number": 0}}) in client.updated


# -- the morning: what is open ---------------------------------------------


def test_the_morning_lists_what_is_open_in_each_project():
    text = project_log.open_message(
        DAY,
        [
            (project("acme/app"), [activity(review=["Open PR (#7)"], issues=["Bug (#3)"])],
             [item("Ship it", day=date(2026, 9, 30))], []),
        ],
    )
    assert text.startswith("☀️ **Open — Mon 9/28**")
    assert "In review" in text and "Open PR (#7)" in text
    assert "To-do" in text and text.index("Ship it") < text.index("Bug (#3)")


def test_a_project_with_nothing_open_is_left_out_of_the_morning():
    quiet = Project(id="q", title="Quiet", status=None, next_action="", url="")
    text = project_log.open_message(
        DAY,
        [(quiet, [], [], []),
         (project("acme/app"), [activity(review=["Open PR (#7)"])], [], [])],
    )
    assert "Quiet" not in text and "App" in text


def test_a_morning_with_nothing_open_anywhere_posts_nothing():
    assert project_log.open_message(DAY, [(project("acme/app"), [activity()], [], [])]) is None


def test_a_repository_that_cannot_be_read_is_said_in_the_morning_too():
    text = project_log.open_message(
        DAY, [(project("acme/app"), [], [], ["acme/app not found"])]
    )
    assert "⚠️ Couldn't read acme/app not found" in text


async def test_the_morning_job_covers_projects_without_a_repository(monkeypatch):
    """To-do comes from the agenda too, so a project with no code still has
    a morning."""
    async def open_items(self, repo):
        return Activity(repo)

    async def aclose(self):
        pass

    monkeypatch.setattr(gh.GitHub, "open_items", open_items)
    monkeypatch.setattr(gh.GitHub, "aclose", aclose)
    jobs, _, _ = build(
        [project_page("p1", "App", repos="acme/app"), project_page("p2", "Paper")],
        [make_page("a1", "Draft the brief", day="2026-09-30", projects=["p2"])],
    )
    text = await jobs.build_project_open()
    assert "**Paper**" in text and "Draft the brief" in text
    assert "**App**" not in text


async def test_a_log_can_be_asked_for_one_project(github_day):
    github_day["acme/app"] = activity(done=["x"])
    github_day["acme/web"] = activity("acme/web", done=["y"])
    jobs, _, _ = build(
        [project_page("p1", "App", repos="acme/app"), project_page("p2", "Web", repos="acme/web")]
    )
    logs, _ = await jobs.build_project_logs(only="p2")
    assert [p.title for p, _ in logs] == ["Web"]
