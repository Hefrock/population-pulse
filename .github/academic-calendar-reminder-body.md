**Status:** Recurring reminder, opened automatically once a year by `.github/workflows/academic-calendar-reminder.yml`. This is a periodic check-in, not blocked on an external party the way the CHIA issue is — the schools' calendars are public, this just needs someone to go look.

## Why this exists

`data/boston_academic_calendar.csv` has no API behind it — schools publish term dates as PDFs or HTML pages that change layout every year, so scraping was ruled out as too fragile (see `src/ingestion/academic_calendar.py`'s docstring). The file needs a ~10-minute refresh whenever schools publish their next academic year, and that manual step is easy to forget since it only comes up once a year. This issue exists specifically to stop that from happening quietly — see the 2026-08 sitrep, where this drifted 68 days stale before anyone noticed.

## What to check

For each school, confirm the file has rows through at least the *next* two terms (Fall + Spring), using these conventions:
- `start_date` = first day of classes (not move-in, not orientation)
- `end_date` = last day of final exams (not last day of classes, not commencement)
- `source` = `verified-YYYY-MM` if cross-checked directly against the school's own registrar page, `estimate` if derived some other way (e.g. search-engine summaries without direct page access)

| School | Registrar calendar page |
|---|---|
| Boston University | https://www.bu.edu/reg/calendars/semester/ |
| Northeastern University | https://registrar.northeastern.edu/article/academic-calendar/ |
| Harvard University | https://registrar.fas.harvard.edu/calendars |
| Boston College | https://www.bc.edu/bc-web/offices/student-services/registrar/academic-calendar.html |
| UMass Boston | https://www.umb.edu/registrar/academic-calendar/ |
| Tufts University | https://students.tufts.edu/registrar/courses-and-calendars/academic-calendar |
| MIT | https://registrar.mit.edu/calendar |
| Suffolk University | https://www.suffolk.edu/academics/academic-calendar |

## Status as of the 2026-08 refresh (for context on what's already covered)

All 8 schools have Fall 2026 (`estimate`, derived via web research — not independently cross-checked against each registrar page directly, so treat as a candidate to upgrade to `verified-*` if you confirm it directly). Spring 2027 is only filled in for Northeastern, Harvard, Boston College, and MIT — **Boston University, UMass Boston, Tufts, and Suffolk still need Spring 2027** (research hit real gaps finding their exact first-day-of-classes dates).

## Possible automation lead — not yet built, don't assume it works

Confirmed (via web research, not direct page access) that **Harvard** and **MIT** both publish a real ICS/iCalendar feed for their academic calendar, not just PDFs — this could eventually replace their manual entries with real automated ingestion (`icalendar` is already a well-supported Python library for parsing it). A candidate live URL for Harvard's feed:

```
http://events.college.harvard.edu/live/ical/events/group/Academic Calendar/exclude_tag/GSAS/exclude_tag/TAP/start_date/06-01-2026/end_date/2050-12-31
```

This was **not** built into the pipeline: nobody has actually viewed this feed's real content (WebFetch is blocked on this domain from the sandboxed dev environment), so the exact event-naming conventions needed to reliably identify "first day of classes" vs. other calendar entries are unconfirmed. Building a parser against guessed field names would be exactly the kind of unverified code this project avoids shipping. If you have real browser access when you see this: worth 10 minutes to check whether this is buildable, since it would shrink this checklist to 6 schools instead of 8.

## Next action

1. Go through the table above, add/update rows in `data/boston_academic_calendar.csv` for any school missing upcoming terms.
2. Commit with a clear message noting which schools were updated and their `source` labels.
3. If nothing needs updating (unlikely, but possible if a previous refresh already got far enough ahead), close this issue with a note saying so.
