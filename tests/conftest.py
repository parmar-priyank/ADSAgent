"""
tests/conftest.py — shared test setup.

SAFETY: the app reads its database path from the DB_PATH environment variable
(db/connection.py). That variable is set to a throwaway file in a temp
directory BEFORE any application module is imported, so importing the app can
never open, read, or write the real extractions.db. The assertion in
_isolate_database() enforces that and fails the whole run if it is ever untrue.
"""
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

# Project root on sys.path so "import config" works when pytest is run from
# anywhere in the repo.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_REAL_DB_NAMES = {"extractions.db"}


def _isolate_database() -> str:
    """Point DB_PATH at a fresh temp file and prove it is not the real DB.

    Runs at import time, before any app module is loaded, because
    db/connection.py captures DB_PATH once at its own import.
    """
    tmp_dir = tempfile.mkdtemp(prefix="adsagent_tests_")
    tmp_db = os.path.join(tmp_dir, f"test_{uuid.uuid4().hex}.db")

    os.environ["DB_PATH"] = tmp_db
    # A SECRET_KEY is mandatory (config.py raises without one). Use a
    # test-only value rather than whatever is in the developer's .env.
    os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production")

    assert Path(tmp_db).name not in _REAL_DB_NAMES, "refusing to run against the real DB"
    assert str(_ROOT) not in tmp_db, "test DB must live outside the project directory"
    return tmp_db


TEST_DB_PATH = _isolate_database()


@pytest.fixture(scope="session", autouse=True)
def _assert_real_db_untouched():
    """Fail the run if the real extractions.db is modified by the tests.

    Belt-and-braces alongside the DB_PATH isolation above: records the real
    file's size and mtime before the suite and re-checks afterwards.
    """
    real = _ROOT / "extractions.db"
    before = (real.stat().st_size, real.stat().st_mtime) if real.exists() else None

    yield

    if before is not None and real.exists():
        after = (real.stat().st_size, real.stat().st_mtime)
        assert after == before, (
            "THE REAL DATABASE WAS MODIFIED BY THE TEST SUITE — "
            f"before={before} after={after}"
        )


@pytest.fixture(scope="session")
def app_module():
    """The imported FastAPI app, loaded once against the isolated DB."""
    import db.connection as conn_mod

    # Confirm the app really did pick up the temp path, not the default.
    assert conn_mod.DB_PATH == TEST_DB_PATH, (
        f"db.connection.DB_PATH is {conn_mod.DB_PATH!r}, expected the temp DB. "
        "DB_PATH must be set before any app module is imported."
    )
    import main
    return main


@pytest.fixture
def client(app_module):
    """A TestClient that returns error responses instead of raising.

    raise_server_exceptions=False makes an unhandled exception surface as a
    500 response, which is what a real user would see — so a test can assert
    "this must not 500" rather than the exception escaping into pytest.
    """
    from fastapi.testclient import TestClient
    return TestClient(app_module.app, raise_server_exceptions=False)


@pytest.fixture
def make_user():
    """Create a user directly in the test DB and return its row.

    Uses the app's own repo functions so the test exercises real password
    hashing and real column defaults rather than hand-written SQL.
    """
    import db.user_repo as adb
    from db.connection import get_db

    created: list[int] = []

    def _make(username=None, password="TestPassw0rd!x", role="user", **columns):
        username = username or f"u_{uuid.uuid4().hex[:8]}"
        assert adb.create_user(username, password, role), f"create_user failed for {username!r}"
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            uid = row["id"]
            # Apply any extra column values (is_active, can_pre_qc, ...) via a
            # parameterised UPDATE; column names are restricted to real columns
            # so a typo in a test fails loudly instead of being ignored.
            if columns:
                valid = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
                unknown = set(columns) - valid
                assert not unknown, f"unknown users columns: {sorted(unknown)}"
                sets = ", ".join(f"{c} = ?" for c in columns)
                conn.execute(f"UPDATE users SET {sets} WHERE id = ?",
                             (*columns.values(), uid))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        created.append(uid)
        out = dict(row)
        out["_plain_password"] = password
        return out

    yield _make

    with get_db() as conn:
        for uid in created:
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))


@pytest.fixture
def session_cookie(app_module):
    """Build a signed session cookie for a user dict, as login would.

    Uses config's own _make_token so the test covers the real signing and
    expiry logic rather than a reimplementation of it.
    """
    import config

    def _cookie(user: dict) -> dict:
        token = config._make_token({
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
        })
        name = config.COOKIE_ADMIN if user["role"] == "admin" else config.COOKIE
        return {name: token}

    return _cookie
