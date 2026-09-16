# External Audit — Status

Tracks the 14-row audit scorecard against what has actually been done.
**Scope decision: ADS Solar internal tool only** (not multi-tenant SaaS).
Every fact below was verified against the code, not assumed.

Last updated: 2026-09-16

---

## Done

| Audit row | What was wrong | Fix | Commit |
|---|---|---|---|
| Security | Login IP allowlist hardcoded in `config.py` | Now `LOGIN_ALLOWED_IPS` in `.env`; blank/malformed falls back to the old default so it can never end up empty | `901f942` |
| Maintainability | "single-client hardcoding" | ADS Solar's own details (name, contact person, email, addresses, phone hint) moved to `.env`. Assembled prompt + schema proved **byte-identical** (SHA `f312b15d7b20b990` / `8a0ac0f7576ebe81`) | `a8f78df` |
| AI/LLM | No schema validation post-parse | `_clean_status()` / `_clean_remark()` on all three verdict-building paths. Valid Yes/No/N/A pass through unchanged; garbage degrades to N/A, never to Yes | `6f3a891` |
| AI/LLM | Indirect prompt injection risk | `_NO_INJECTION` added to both QC system prompts; the attachment matcher had **no system prompt at all** and now has one. All 5 call sites now pass `system=` | `8b8b56d` |
| Database | "audit deletion on every insert" | Retention DELETE now runs on ~1 in 100 inserts. Insert itself is never sampled, so no audit event is lost | `eda5f90` |
| Code Quality / Reliability | (not on the scorecard) Unguarded AI reply parsing crashed PDF upload with a blank 500 | `_response_text()` / `_parse_extraction_json()` + retry-once-then-degrade in `ai_service.py` | `e4e6326` |

### Correction to the audit's wording

The Database row reads "audit deletion on every insert", implying a scan.
`EXPLAIN QUERY PLAN` shows it was `SEARCH audit_log USING INDEX idx_audit_ts
(ts<?)` — an **indexed range probe, never a table scan**, matching zero rows on
live data. Real but minor: a wasted second statement per event, not a
performance bug. Fixed anyway.

---

## Not applicable — scope decision

| Audit row | Score | Why not applicable |
|---|---|---|
| Multi-Tenancy | 0/10 "Critical" | Single-tenant is the correct design for a one-company internal tool. Scored against a goal that does not exist here. A full plan exists in `docs/multi-tenancy-plan.md` if that ever changes. |
| Security / Maintainability | — | The "hardcoded for one retailer" parts of these rows are the same scope decision. The *configurability* concern was still fixed above. |

---

## Deferred — large refactors, documented rather than dropped

Each of these is real, but is a refactor with genuine regression risk and no
user-visible benefit. Deferred deliberately, not overlooked.

### Architecture — `routers/qc_checks.py` is ~3,180 lines
The audit said 3,074 and measured 3,073 at the time, so its figure was
accurate. It is now ~3,183, because the validation and prompt-hardening
commits above added guard code to this file — a deliberate trade: the file got
slightly longer in exchange for closing two AI-reliability findings. Business
logic sits in routers with no service layer, and 11 raw SQL statements live
outside `db/`
(`admin.py` lines 477/545/719; `qc_checks.py` lines 1280/1366/2376/2607/
2730/2828/2863/2930).
**Why deferred:** splitting it is pure code movement across the most
heavily-used file in the app. Highest chance of silently breaking a QC flow,
zero benefit a user would notice. Worth doing only with tests in place first.

### Security — CSP `script-src 'unsafe-inline'`
Real weakness at `config.py`'s `_CSP`. It is there because the templates use
inline `<script>` blocks throughout.
**Why deferred:** removing it means extracting inline JS from every template
into external files. Large, touches every page, and breaks every page if done
incompletely.

### Security — no CSRF tokens
Real. 46 POST routes plus AJAX `fetch` calls.
**Why deferred:** needs token generation, validation, and an update to every
form *and* every fetch call. Getting one wrong silently breaks a Save button.
Should be done as its own focused piece of work with each form clicked through
locally, not bundled with other changes.

### Testing 0/10 and Maintainability — zero automated tests
Confirmed: no `test_*.py`, no `conftest.py`, `pytest` not in
`requirements.txt`.
**Why deferred:** this is the correct *next* priority, and the prerequisite
for the three items above. Not a one-commit job.

### Database / Maintainability — no migration framework
Schema evolves via `ALTER TABLE` + `PRAGMA table_info` guards, with no version
history and no rollback path.
**Why deferred:** retrofitting Alembic onto a live DB needs a baseline
migration that reproduces production exactly. Medium risk, needs a
maintenance window.

### API/Integration 2/10
All routes are HTML-first; no JSON API, no versioning.
**Why deferred:** there is no consumer for an API. Building one for an
internal tool with no integration requirement is speculative work.

### Observability 5/10
File logging only; no structured logs, request IDs, metrics, or alerting.
**Why deferred:** low risk and additive — a reasonable next pick after tests.

### Data Privacy 4/10 — PII unencrypted, DB downloadable by any admin
Real. Restricting the download to super-admin only is a small change, **but it
removes an ability some admins have today**, so it needs an explicit decision
rather than being slipped in under "no functionality changed".

### Scalability / Performance / Deployment
Audit itself notes these are largely appropriate today ("SQLite WAL is
appropriate", "`_active_jobs` per-process by design", parallel AI with a
custom executor pool, PDF render caching, solid systemd/nginx). The genuine
gaps are no staging environment and weekly-only backups — both operational
choices rather than code defects.

---

## Recommended next steps, in order

1. **Automated tests** — fixes the only Critical row that applies, and unblocks
   the deferred refactors above.
2. **CSRF protection** — highest real security value; do it as its own piece of
   work.
3. **Migrations (Alembic)** — before the schema grows further.
4. **Observability** — low risk, additive, makes future debugging easier.
5. **DB-download restriction** — needs a decision first, since it takes an
   ability away from existing admins.

Deliberately not planned: multi-tenancy, a public API, splitting
`qc_checks.py`, and removing CSP `unsafe-inline`.
