"""
Tests that the suite itself is safe: it must run against a throwaway database
and never the real one. If these fail, do not trust any other test result.
"""
from pathlib import Path

from tests.conftest import TEST_DB_PATH

_ROOT = Path(__file__).resolve().parent.parent


def test_db_path_env_points_at_temp_file():
    import os
    assert os.environ["DB_PATH"] == TEST_DB_PATH
    assert "adsagent_tests_" in TEST_DB_PATH


def test_connection_module_uses_the_temp_db():
    import db.connection as conn_mod
    assert conn_mod.DB_PATH == TEST_DB_PATH


def test_temp_db_is_outside_the_project_directory():
    assert str(_ROOT) not in TEST_DB_PATH


def test_real_database_file_is_never_opened(app_module):
    """Writing through the app's own get_db() must land in the temp file."""
    from db.connection import get_db

    with get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS _probe (v TEXT)")
        conn.execute("INSERT INTO _probe (v) VALUES ('x')")

    # The probe table exists in the temp DB...
    import sqlite3
    tmp = sqlite3.connect(TEST_DB_PATH)
    assert tmp.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='_probe'"
    ).fetchone()[0] == 1
    tmp.close()

    # ...and not in the real one.
    real = _ROOT / "extractions.db"
    if real.exists():
        r = sqlite3.connect(f"file:{real}?mode=ro", uri=True)
        assert r.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='_probe'"
        ).fetchone()[0] == 0, "test data leaked into the real database"
        r.close()
