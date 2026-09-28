"""A project's day, for its post in the #projects forum.

Three sections, each from what already records it rather than from anything
typed for the purpose:

- **Work done** -- what reached the default branch of each repository, grouped
  by pull request, and any agenda item linked to the project finished that
  day, for the work that is not code.
- **In review** -- open pull requests.
- **To-do** -- agenda items linked to the project and not yet finished, then
  open issues.

A day with nothing done posts nothing. Review and to-do change slowly, and a
post every night repeating the same open list is one that stops being read;
they ride along with a day that moved. A repository that could not be read is
the exception: that is worth saying on a quiet day too, because it will stay
quiet until someone fixes it.
"""

from __future__ import annotations

from datetime import date, timedelta

from . import reports
from .agenda import AgendaItem, Project
from .integrations.github import Activity, Line

# Past this a section is a backlog, not a note, and the post is for a glance.
TODO_LIMIT = 5


def _bullet(line: Line, repo: str | None) -> str:
    suffix = f" · {repo.split('/')[-1]}" if repo else ""
    return f"• [{line.text}]({line.url}){suffix}"


def log_message(
    project: Project,
    day: date,
    activities: list[Activity],
    errors: list[str],
    done_items: list[AgendaItem],
    todo_items: list[AgendaItem],
) -> str | None:
    # The repository is named only when there is more than one to tell apart.
    several = len(project.repos) > 1

    def label(activity: Activity) -> str | None:
        return activity.repo if several else None

    done = [_bullet(line, label(a)) for a in activities for line in a.done]
    done += [f"• {item.title}" for item in done_items]
    if not done and not errors:
        return None

    out = [f"📅 **{reports.fmt_date(day)}**"]
    if done:
        out += ["**Work done**", *done]

    review = [_bullet(line, label(a)) for a in activities for line in a.in_review]
    if review:
        out += ["**In review**", *review]

    todo = [
        f"• {item.title}" + (f" ({reports.fmt_date(item.day)})" if item.day else "")
        for item in todo_items[:TODO_LIMIT]
    ]
    todo += [
        _bullet(line, label(a)) for a in activities for line in a.issues
    ][: max(0, TODO_LIMIT - len(todo))]
    if todo:
        out += ["**To-do**", *todo]

    out += [f"⚠️ Couldn't read {reason}" for reason in errors]
    return "\n".join(out)


def _short(day: date | None) -> str:
    return f"{day.month}/{day.day}" if day else "-"


def week_message(
    projects: list[Project],
    code_done: dict[str, int],
    today: date,
    *,
    stale_after_days: int,
) -> str | None:
    """Every open project's week, in one table, for the Overview post.

    This replaced a check that spoke only about the projects that had
    stopped: the ones moving were invisible, and a project's standing only
    means something beside the others'. The stalled still get their line,
    under the table where they cannot be missed.

    Done counts linked agenda items finished in the last seven days and the
    GitHub work that reached a default branch in them -- the same two sources
    the nightly post reads.
    """
    if not projects:
        return None
    first = today - timedelta(days=6)
    rows = [
        (
            project.title,
            str(project.done_week + code_done.get(project.id, 0)),
            _short(project.last_activity),
            project.next_action,
        )
        for project in projects
    ]
    out = [
        f"🗂️ **Projects — {_short(first)}–{_short(today)}**",
        *reports._table(("Project", "Done", "Last", "Next"), rows, flex=3, right=(1,)),
    ]
    cutoff = today - timedelta(days=stale_after_days)
    for project in projects:
        if project.last_activity is None:
            out.append(f"⚠️ {project.title}: nothing linked to it yet")
        elif project.last_activity < cutoff:
            out.append(
                f"⚠️ {project.title}: nothing since {_short(project.last_activity)} — "
                "still in progress, or time to put it on hold?"
            )
    return "\n".join(out)


def open_message(
    today: date,
    entries: list[tuple[Project, list[Activity], list[AgendaItem], list[str]]],
) -> str | None:
    """What is open in every project, for the Overview post each morning.

    The morning half of the log: the nightly post records what was done,
    this is what is left to do -- in review, then to-do -- read when the day
    is being planned. A project with nothing open is left out, and a morning
    with nothing open anywhere posts nothing.
    """
    blocks: list[str] = []
    for project, activities, todo_items, errors in entries:
        # The repository is named only when there is more than one to tell apart.
        several = len(project.repos) > 1
        review = [
            _bullet(line, a.repo if several else None)
            for a in activities
            for line in a.in_review
        ]
        todo = [
            f"• {item.title}" + (f" ({reports.fmt_date(item.day)})" if item.day else "")
            for item in todo_items[:TODO_LIMIT]
        ]
        todo += [
            _bullet(line, a.repo if several else None) for a in activities for line in a.issues
        ][: max(0, TODO_LIMIT - len(todo))]
        problems = [f"⚠️ Couldn't read {reason}" for reason in errors]
        if not (review or todo or problems):
            continue
        block = [f"**{project.title}**"]
        if review:
            block += ["In review", *review]
        if todo:
            block += ["To-do", *todo]
        blocks.append("\n".join(block + problems))
    if not blocks:
        return None
    return "\n\n".join([f"☀️ **Open — {reports.fmt_date(today)}**", *blocks])
