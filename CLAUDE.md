# Voss & Thorne LLP

Django 5.2 site for a shipping/logistics firm: public tracking pages, a staff Inbox inside the Django admin, and (in progress) a private investor portal. Deployed on Render, Postgres on Supabase, email via Resend.

## Commands
**The project `venv/` is stale: Python 3.9 with Django 4.2.30, but `requirements.txt` and production use Django 5.2 on Python 3.11.** Behaviour differs (e.g. 4.2 allows GET on logout, 5.x doesn't), so a green run in `venv/` proves little. Run tests against a production-matching env:
`uv venv --python 3.11 <dir>/venv311 && uv pip install --python <dir>/venv311/bin/python -r requirements.txt` (use the scratchpad for `<dir>`), then use that interpreter below. Don't modify `venv/` without asking.
- Run: `<py> manage.py runserver` (set `DEBUG=True` locally, since cookies are HTTPS-only otherwise)
- Test: `<py> manage.py test` (one app: `... test investors`)
- Migrations: `venv/bin/python manage.py makemigrations <app>`, then `makemigrations --check --dry-run` must report no changes
- Pre-deploy: `venv/bin/python manage.py check --deploy`

## Layout
- `vossandthornellp/`: settings, root URLs. Config is env-driven (`DATABASE_URL`, `RESEND_API_KEY`, `SITE_URL`, ...). Never hardcode secrets.
- `tracking/`: shipments, events, the email Inbox (in `admin.py` via `get_urls()`), Resend sending (`emails.py`), inbound webhook, signals.
- `investors/`: investor portal. Models, `services.py` (all growth maths), auth (`decorators.py`, `forms.py`, `throttle.py`), views/templates (dashboard, investment, performance). Documents (private Supabase bucket via `storage.py`, signed 60s links, local `private_media/` fallback) and notifications (`signals.py` -> `notifications.py`: created-only, 45-day recency, 30-min cooldown per kind, generic emails with no figures) are done; hardening pass done (no-store headers on portal, reset/login throttling, 12-char passwords, deleted docs removed from storage, opt-in `SECURE_SSL_REDIRECT`/`SECURE_HSTS_SECONDS`). Run `uvx pip-audit` against the installed env periodically. `manage.py seed_investor_demo [--years-back N] [--reset]` creates FAKE local demo data (SQLite + DEBUG only). The engine is `demo_data.py` (`DemoSeeder`); production uses the superuser-only 'Demo data' admin page instead, with one-time random passwords. Demo profiles are flagged `is_demo`, never emailed, shown with a banner, and removal only deletes flagged profiles that also have a demo username and an example.com email. Never let demo tooling modify a non-demo user. `importer.py` + the admin 'Import history (CSV)' page load REAL history (preview, then all-or-nothing confirm; never emails). History written with `bulk_create` deliberately bypasses notification signals. The plan is in `~/.claude/plans/i-need-to-add-temporal-cascade.md`.
- Deploy files (`build.sh`, `render.yaml`, `Procfile`) must stay in sync with anything that changes how the app starts, migrates or needs env vars.

## Python and compatibility
- Production runs Python 3.11 (`runtime.txt`) but the local venv is 3.9. Write code that works on both: no `X | Y` type unions, no `match`, no 3.10+ only features.
- Match surrounding style: function views in `tracking/`, plain Django templates, no JS build step. Frontend libraries load from a pinned-version CDN.

## Money and investor data (high stakes)
- Money is `Decimal` only, never `float`. Round once, at the end, with `ROUND_HALF_UP` to cents (`services._q`).
- Growth maths lives only in `investors/services.py` and is pure and unit-tested. Views and templates must not recompute values. Any change to the maths needs a test with a hand-computed expected value (double-check the arithmetic by hand, since a wrong test is as bad as wrong code).
- Conventions are documented at the top of `services.py` ("value" is total value including paid-out interest; periodic payouts accrue as simple interest). Keep them consistent in admin help text and UI copy.
- Isolation is mandatory: every investor query filters by `request.user.investor_profile`; never look up an investor object by URL ID alone, and return 404 (not 403) for someone else's object. Add a test for each new investor-facing view proving investor A cannot reach investor B's data.
- Investor documents are never served from `/media/`. Use an authenticated view that checks ownership.
- Notification emails to investors carry no financial figures, only a link to the portal.
- Investors are non-staff users. They must never be able to reach `/admin/`.

## Withdrawals and bank details
- Requests never move money; staff pay outside the system and mark them completed (`withdrawals.apply_status` is the only place that writes the ledger row).
- Initiation is US-only. `geo.py` trusts `CF-IPCountry` only when the `X-Origin-Verify` header matches `CF_ORIGIN_SECRET` (set by a Cloudflare Transform Rule); unknown or unverifiable = blocked with the same message. Never add a code path that allows an unknown country.
- Bank numbers exist only encrypted (`crypto.py`, `FIELD_ENCRYPTION_KEY`; losing the key loses them). Show `masked`/last4 everywhere; full numbers only via the logged `reveal` admin view, which needs the `reveal_bank_details` permission. Never log, email or render full numbers elsewhere.
- Typed destinations need a staff call-back (`callback_verified`) before approval; declines need a real reason shown to the investor.
- Investors start with a generated temporary password (`must_change_password`); `investor_required` redirects every page to change-password until it is changed.

## Demo data shape
- Default window is 6 years: every demo account has a plan that started exactly 6 years back, years of old withdrawal requests (paid and declined, with realistic reasons and matching ledger rows), annual + quarterly statements, and at least ~570,000 in its main currency. `test_seed.py` enforces all of this; if you change principals or rates, re-run `calibrate` by seeding into a scratch DB and checking the floor still holds. Shorter `--years-back` windows skip entries that would predate their plan and will have lower balances.
- THREE accounts are created every time the demo data is seeded (button or command): **colin-firth** (the flagship: over $1M, record anchored to 15 Jan 2020 via the spec's `since`), **demo** (the fullest) and **demo-chidi** (temporary password, forced change). `demo-elena`, `demo-astrid` and `demo-other` are `seed=False`: they exist only as styles for the 'Fill with demo history' action and are still removable. No PDFs are generated (`INCLUDE_DOCUMENTS = False`); each is a storage upload and the button runs inside one request. Tests enforce all of this.
- An existing demo-flagged account that carries a superuser's own email (e.g. the hand-made colin-firth) is ADOPTED: only its password is refreshed; email, name, phone and data are kept. Normal reset and the Remove button never touch accounts without an example.com email; only the explicit 'Rebuild existing demo accounts' checkbox does.
- Superusers can fill any demo-flagged account via the 'Fill with multi-year demo history' admin action (confirmation page, typed phrase, real clients and staff refused outright, no overwrite unless 'replace' is ticked).

## Performance of request-time tools (learned the hard way)
- Admin tools such as the Demo data button run inside ONE web request against a NETWORK database (Supabase) with gunicorn killing workers after `--timeout 90`. Every query is a round trip. A first version re-read snapshots/transactions once per computed value (~1,700 queries) and timed out in production. Rules: load investments with `prefetch_related('snapshots', 'transactions')` before looping `services.value_on`; never call it per-iteration on un-prefetched instances; upload/delete stored files through `demo_data._in_parallel`; keep `test_demo_performance.py`'s query budgets passing (a full seed is ~90 queries). Seeding also has a time guard (`DEMO_SEED_MAX_SECONDS`, default 75): it aborts cleanly, rolled back, with a message about `--timeout 90`, rather than being killed.
- `render.yaml`/`Procfile` set `--timeout 90`; if the Render service's Start Command is set in the dashboard it must include the same flag.

- Demo-data writers (`run`, `remove`, `fill_profile`) take a Postgres transaction-level advisory lock (`_take_lock`, `DEMO_LOCK_ID`) so a double click / two tabs cannot race into a duplicate-username 500; a second run is refused with a message. The admin forms also disable their submit button on first click, and an `IntegrityError` is shown as a message. Keep any new writer behind the same lock.

## Accounting and demo access
- Displayed value is NET of withdrawals (proportional: interest continues only on what remains); gain = value + withdrawn - invested, so withdrawing is never shown as a loss. Never reintroduce a second subtraction of paid withdrawals in availability checks.
- Superusers (e.g. made with `createsuperuser`) have unrestricted control of DEMO data only: free editing of demo withdrawals, deleting demo investors/investments with their withdrawals, and 'Open the portal as this demo investor' (`impersonation.py`, demo + superuser only, always reversible). Real clients keep every rule, superuser or not. The `is_demo` checkbox is what grants all of this: only superusers can change it, the admin history records every change, and it must never be ticked for a real client. Test both sides whenever touching admin permissions.

## Security
- Do not log or echo passwords, tokens, API keys or investor financial data. Do not commit `.env`, `db.sqlite3` or `media/`.
- `build.sh` no longer has default admin credentials; it only creates a superuser when `DJANGO_SUPERUSER_USERNAME` and `_PASSWORD` are both set. An admin created by the old default may still exist in production with password `admin123`: verify and change it.
- Render's disk is ephemeral. Anything that must survive deploys (investor statements) needs durable storage before it is relied on.

## Working agreements
- Plan first for anything non-trivial and get approval before building. Follow the build order in the plan.
- Verify before saying "done": run the tests and `makemigrations --check`. Report failures as they are, with output.
- Don't commit or push unless asked. Don't run migrations against anything but the local SQLite DB unless told to; `DATABASE_URL` may point at production Supabase, so check it is unset before running `migrate`.
- Keep changes focused. No drive-by refactors of `tracking/`, and no new dependencies without saying why (add them to `requirements.txt`).
- When a test fails, work out whether the code or the test expectation is wrong before editing either.
