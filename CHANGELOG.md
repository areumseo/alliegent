# Changelog

What changed in each release, newest first. A release is a tag on `main`;
the Mac mini still follows `main` itself, so tagging is how a working state
gets a name worth going back to, not a gate on deployment.

New entries are written by `scripts/release.sh` from the commit subjects
since the previous tag, and are worth editing afterwards: a good entry says
what is different for the person using the bot, which is not always what the
commit was about.

## v1.0.0 — 2026-09-20

The first tagged state, after a month of daily use. Everything below was
already running; this is the point it is given a name.

### Agenda

- A weekly agenda in Notion, read and written from Discord: `/add`, `/done`,
  `/delete`, `/move`, `/time`, `/list`, `/tomorrow`, `/status`, `/overdue`.
- A day is ordered by the clock, and items without a time go to its end.
- Categories are inferred from how the same activity was filed before, or
  chosen by hand on `/add`.
- Cancelled items count as finished, and work in progress is marked apart
  from work not yet begun.
- A daily brief in the morning, incomplete-item reminders at 14:00 and 19:30,
  a weekly planning nudge, and a Sunday review.
- Timed items are added to the calendar as well, and a calendar that stops
  answering says so in the brief instead of quietly going missing.

### AI news

- A morning digest of the previous day's articles, gathered from RSS feeds
  and written by Claude, posted to its own channel and optionally emailed.

### Karrot

- Listings tracked through Not listed → Listed → Reserved → Sent → Sold.
- Revenue by week, month and total; a Saturday sales report and a Monday
  list of what is decided on but not yet listed.

### Assets

- Weekly balance snapshots, split into liquid, locked and expected.
- The bonus moves into Savings after it is paid, and ESPP into Vested once
  it is bought.

### English review

- Lesson material dropped into the channel is read and kept, and an evening
  quiz asks about it; answers are graded as replies.

### Running it

- Runs on the Mac mini under launchd, updating itself from `main` but never
  onto a commit whose tests fail.
- A pre-commit hook refuses secrets and personal data, since the repo is
  public.
- Scheduled jobs announce their own failures to the channel they belong to.
