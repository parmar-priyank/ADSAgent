# Tests

## Running them

From the project root, with the venv's Python:

```bash
venv/Scripts/python.exe -m pytest          # Windows
venv/bin/python -m pytest                  # Linux (the server)
```

Useful variations:

```bash
python -m pytest -v                        # one line per test
python -m pytest tests/test_auth_guards.py # a single file
python -m pytest -k "admin"                # tests matching a name
python -m pytest -x                        # stop at the first failure
```

The whole suite takes about 25 seconds.

## Safety: these tests never touch the real database

`tests/conftest.py` sets the `DB_PATH` environment variable to a throwaway
file in a temp directory **before any application module is imported**, which
is what `db/connection.py` reads. Three independent checks enforce this:

1. `conftest._isolate_database()` asserts the temp path is not named
   `extractions.db` and lives outside the project directory.
2. The `app_module` fixture asserts `db.connection.DB_PATH` really is the temp
   path — if the app somehow picked up the default, the run fails immediately.
3. A session-wide fixture records `extractions.db`'s size and mtime before the
   suite and re-checks them afterwards, failing the run if it changed.

`tests/test_isolation.py` additionally writes a probe table through the app's
own `get_db()` and proves it appears in the temp DB and **not** in the real one.

If anything in `tests/test_isolation.py` ever fails, do not trust the rest of
the run.

## What is covered

| File | Covers |
|---|---|
| `test_isolation.py` | The safety guarantees above |
| `test_auth_guards.py` | `require_login`, `require_admin`, `require_superadmin`, `require_qc_access` — including role separation, deactivated accounts, tampered cookies, and the documented admin-cookie-wins precedence |
| `test_ai_parsing.py` | Verdict validation and AI-reply parsing. The central property: a malformed or hostile reply degrades to a safe `N/A` and never becomes a false `Yes` |
| `test_routes.py` | Over-HTTP checks that protected pages deny anonymous visitors, security headers are present, login rejects bad credentials, and passwords are stored as bcrypt hashes |
| `test_audit_log.py` | Every event is recorded (the insert is never sampled), retention still prunes, and `client_ip` trusts the right header |

## Design notes

- Tests assert behaviour **as it actually is today**, verified against the
  source — including deliberate decisions like `require_qc_access` not
  requiring `email_verified`. Those are marked in the test docstrings so they
  are not "tightened" back by accident.
- The `make_user` fixture goes through the app's own `create_user()`, so real
  bcrypt hashing and real column defaults are exercised rather than
  hand-written SQL.
- `session_cookie` uses `config._make_token()`, so the real signing and expiry
  logic is under test rather than a reimplementation.
- `TestClient(raise_server_exceptions=False)` means an unhandled exception
  shows up as a 500 response, letting tests assert "this must not crash"
  rather than the exception escaping into pytest.

## Verified to actually catch regressions

The suite was checked by deliberately breaking the code and confirming it
fails, rather than assuming green means covered:

- Making `_clean_status()` return `"Yes"` for unknown values → **8 failures**
- Making `require_admin()` accept a plain user session → **2 failures**, at
  both the guard level and over HTTP

Both were reverted afterwards; the file diffs were confirmed empty.
