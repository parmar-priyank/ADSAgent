# Multi-Tenancy Implementation Plan

**Status:** Proposal — no code written yet. Awaiting approval.
**Written:** 2026-09-16
**Trigger:** Second client signed. Audit item: "Multi-Tenancy 0/10 Critical —
single-tenant by design; no data isolation; client-specific config/prompts
require code changes."

**Guiding constraint:** No existing functionality changes or is removed. ADS
Solar users must see exactly what they see today, with the same behaviour,
after every phase.

---

## 1. What the audit is actually pointing at

Three separate problems get lumped under "multi-tenancy":

| # | Problem | Severity | Fix size |
|---|---------|----------|----------|
| A | **No data isolation** — nothing in the schema says which company a row belongs to | Critical | Large |
| B | **Client-specific values hardcoded** in prompts/branding | Medium | Small |
| C | **Per-client config needs a code change** to alter | Medium | Small |

B and C are cheap and carry near-zero risk. A is the real work, and it is the
only one that can leak one company's customer data to another.

---

## 2. Current-state audit (measured, not estimated)

### Tables (11 across 4 repo files)

| Table | Rows (live) | Needs `tenant_id`? | Notes |
|-------|------------:|--------------------|-------|
| `quotes` | 155 | **Yes** | Customer PII. Core record. |
| `qc_versions` | 241 | **Yes** | Inherits via `quote_id`, but needs its own column for safe direct queries |
| `line_items` | 1,634 | Via `quote_id` | FK-cascaded child; no own column needed |
| `users` | 7 | **Yes** | `username` is globally UNIQUE — blocker, see §4 |
| `templates` | 2 | **Yes** | Checklists differ per client |
| `checklist_items` | 83 | Via `template_id` | FK-cascaded child |
| `settings` | 2 | **Yes** | Currently global key→value; needs to become per-tenant |
| `audit_log` | 297 | **Yes** | Must not show one client another's actions |
| `post_qc_assignments` | 142 | Via `quote_id` | FK-cascaded child |
| `pending_results`, `run_checkpoints`, `pending_pdfs` | transient | Via parent | Short-lived working state |

**Total live data is small** (155 quotes / 241 versions). This is the single
biggest thing in our favour: the data migration is fast, easy to verify
row-by-row, and trivially reversible from a backup.

### Query surface (the real risk metric)

123 SQL statements in `db/`:

- **67 SELECT** ← every one is a potential cross-tenant leak if missed
- 29 UPDATE
- 15 DELETE
- 12 INSERT

Plus **11 raw SQL statements outside `db/`**, in routers:

- `routers/admin.py` — lines 477, 545, 719
- `routers/qc_checks.py` — lines 1280, 1366, 2376, 2607, 2730, 2828, 2863, 2930

These bypass the repo layer entirely and must each be scoped by hand. This is
the architecture issue (audit item 4: business logic in routers) making a
tenancy retrofit harder than it would otherwise be.

### Highest-risk queries: full-table scans with no WHERE

These return *everything* today and would return *every tenant's* rows:

- `db/checklist_repo.py:181` — lists all templates
- `db/quote_repo.py:479` — all quotes
- `db/quote_repo.py:816`, `db/user_repo.py:129` — all users
- `db/quote_repo.py:356, 385, 555, 741, 756, 778, 902, 1032, 1140` — QC version
  aggregates, dashboard stats, calendar data

### Hardcoded client-specific values

Concentrated in **one file**, `services/ai_service.py` — better than feared:

- Line 102: `"the retailer is 'ADS Pty Ltd t/as ADS Solar'"`
- Line 103: `"its contact person is 'Nik'"`, `"PO Box 6208 / Norwest"`
- Line 104: `"Solent Circuit, Baulkham Hills"`
- Line 105: `"sales@adssolar.com.au"`, `"its phone is a 1300 number"`
- Lines 15–25: the same values repeated as schema field hints

Branding strings appear in 4 templates: `admin_base.html`, `user_base.html`,
`login_admin.html`, `login_user.html`.

### Things already in our favour

- **Session token** (`config.py:246`) is a clean dict — adding `tenant_id` is a
  one-line change.
- **Every auth guard** already re-reads the user via `adb.get_user(user["id"])`
  (`config.py:397–430`). One chokepoint to attach tenant to the request.
- **File storage** (`db/blob_store.py`) already uses generated relative paths
  under `qc_files/`, with a path-traversal guard. Needs a tenant subdirectory.
- **SQLite FKs are ON** with `ON DELETE CASCADE` already used correctly.

---

## 3. Two options

### Option 1 — Row-level tenancy (one DB, `tenant_id` column)

Add `tenant_id` to every root table; filter every query.

- Good: one deployment, one backup, cross-tenant admin reporting possible
- Bad: **one missed WHERE clause = a data leak**; 67 SELECTs to audit; the
  11 router-level queries are easy to overlook

### Option 2 — Database-per-tenant (recommended)

One SQLite file per client: `data/ads_solar/extractions.db`,
`data/client_two/extractions.db`. Resolve the DB path per request from the
session's tenant.

- Good: **isolation is structural, not a code discipline** — a missed WHERE
  clause cannot leak across tenants because the other tenant's rows are not in
  the file. Existing ADS Solar DB becomes the ADS Solar tenant file *untouched*.
  Per-tenant backup/restore. Almost all 123 queries stay exactly as written.
- Bad: no cross-tenant reporting in one query (not currently a feature);
  schema migrations must run per file; `get_db()` becomes context-aware

**Recommendation: Option 2.** With zero automated tests (audit item 2) and real
customer PII, I would rather the isolation be enforced by the filesystem than by
remembering to add `WHERE tenant_id = ?` in 67 places. It also means ADS Solar's
live data is never rewritten — the lowest-risk path to "nothing changed for
existing users."

---

## 4. Blockers that must be resolved first

1. **`users.username TEXT UNIQUE`** (`db/user_repo.py:36`)
   Two companies cannot both have an "admin". Under Option 2 this resolves
   itself (separate files). Under Option 1 it needs
   `UNIQUE(tenant_id, username)` — a table rebuild in SQLite.

2. **`idx_quotes_quote_number` UNIQUE** (`db/quote_repo.py:42`)
   Same: fine under Option 2, needs a composite index under Option 1.

3. **No migration framework** (audit item 9)
   Schema today evolves via `ALTER TABLE` + `PRAGMA table_info` guards, with no
   version history and no rollback. Under Option 2 the same schema code must now
   run against N files. **This should be fixed before or alongside tenancy**, not
   after — otherwise we are hand-writing ALTERs against multiple live DBs.

4. **No tests** (audit item 2)
   I strongly recommend a minimal isolation test suite as part of this work —
   not full coverage, just: "tenant A's session cannot read tenant B's quotes,
   versions, users, templates, or files." That is the one thing that must never
   regress, and it is the one thing we currently cannot verify automatically.

---

## 5. Phased plan (each phase independently deployable and revertable)

### Phase 0 — Migration framework *(prerequisite)*
Introduce versioned migrations (Alembic, or a small `schema_version` runner
matching the current style). Baseline migration must reproduce the **existing
live schema exactly**, verified by diffing against production.
**Risk: Low.** No behaviour change. **Deployable alone.**

### Phase 1 — De-hardcode client values *(the "safe groundwork" option)*
Move retailer identity out of `services/ai_service.py` into a config object,
with the current ADS Solar values as the default. Same for the 4 branded
templates (brand name/logo via a template variable).
**Risk: Very low.** Identical output for ADS Solar — verifiable by diffing a
generated prompt string before/after. **Deployable alone.**

### Phase 2 — Tenant registry + session plumbing
Add a `tenants` table (id, slug, display name, config JSON, is_active). Insert
ADS Solar as tenant 1. Add `tenant_id` to the session token and resolve it on
every request through the existing `require_*` guards. Nothing reads it yet.
**Risk: Low.** With one tenant, every lookup resolves to ADS Solar.

### Phase 3 — Tenant-aware DB routing *(the core change)*
`get_db()` resolves the tenant's DB file from request context. ADS Solar's
existing `extractions.db` is *moved, not rewritten*, to its tenant path.
Per-tenant `qc_files/` subdirectory, keeping the existing traversal guard.
**Risk: Medium — this is the one to be careful with.** Mitigated by: full
backup first, ADS Solar data byte-identical, and a rollback that is just moving
one file back.

### Phase 4 — Isolation tests
Automated checks that a tenant-A session cannot reach tenant-B quotes, QC
versions, users, templates, settings, audit log, or files.
**Risk: None.** Test-only.

### Phase 5 — Per-tenant onboarding
Admin flow (or a script) to create a tenant: new DB from migrations, first admin
user, branding/prompt config, template upload.
**Risk: Low.** Purely additive; ADS Solar untouched.

### Phase 6 — Second client go-live
Onboard the real second client, verify isolation with both accounts live.

---

## 6. What I will NOT do without separate approval

- Change any existing user-visible behaviour, layout, or wording
- Rewrite ADS Solar's live data (Phase 3 moves the file; it does not rewrite rows)
- Split `routers/qc_checks.py` (audit item 4) — unrelated refactor, own risk
- Add MFA or change the DB-download permission (audit items 10/13) — separate items
- Touch the CSP `unsafe-inline` issue — needs removing inline JS from templates

---

## 7. Open questions for you

1. **Option 1 or Option 2?** I recommend Option 2 (database-per-tenant).
2. **Does the second client need their own checklist templates and prompts**, or
   do they use ADS Solar's as a starting point?
3. **Should any admin see across both companies** (a super-admin cross-tenant
   view), or is each company fully sealed? This is the main thing that would
   argue for Option 1.
4. **How do tenants resolve at login** — separate subdomain
   (`clienttwo.qc-agent...`), a tenant picker on the login page, or inferred
   from the user account? Subdomain is cleanest; the login-page approach needs
   no DNS/TLS work.
5. **Timeline for the second client's go-live?** Determines whether we do
   Phase 0 properly or take a documented shortcut.

---

## 8. Honest assessment

This is the largest item on the audit list and the only one that can cause a
**data breach between two paying customers** if done carelessly. The good news:
the live dataset is small, the hardcoding is concentrated in one file, and the
session/auth layer already has clean chokepoints.

Phases 0–2 and 4 are low-risk and can proceed as soon as you pick an option.
Phase 3 is the one that deserves a maintenance window, a fresh backup, and a
verification pass with both tenants before anyone relies on it.

I would not attempt all of this in one commit or one evening.
