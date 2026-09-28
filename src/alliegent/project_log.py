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

from datetime import date

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
