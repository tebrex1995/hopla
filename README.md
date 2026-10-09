# Hopla

**Svi polasci. Bez iznenađenja.**

Hopla shows every train and bus between two Serbian cities in one time-sorted list, and tells you when a departure you follow changes: cancelled, gone from sale, moved to a new time, or the whole line suspended. It checks the official booking searches regularly and compares the results, so a change is noticed even when nobody announces it.

The first route is **Novi Sad ↔ Beograd**.

> **Status:** early development. Nothing is running publicly yet.

## How Hopla reads public sources

Hopla reads public timetable and booking-search pages the way a careful person would:

- It identifies itself with a User-Agent that includes a contact address.
- It sends at most one request every 2 seconds per site, within a small daily budget.
- It respects `robots.txt` and each site's terms.
- It **never** gets around CAPTCHAs, logins or bot protection. If a site blocks automated access, that source is switched off.
- It doesn't scrape other aggregators.
- Every departure links back to the operator's own site for tickets.

The timetable data belongs to the transport operators. Hopla doesn't sell tickets.

If you run one of the sources and have a question or a concern, write to **kontakt@hopla.rs**. We'll answer, and we'll stop reading your site if you ask.

## Stack

Python 3.14 with uv, httpx, FastAPI and Postgres. Raw responses are stored before parsing, so they can be re-parsed later. Change detection is a pure, tested function. See `CONTRIBUTING.md` for the development commands.

## License

The code is under the [MIT](LICENSE) license. The timetable data remains the property of its operators.
