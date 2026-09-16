"""
Session token minting, expiry, and backward compatibility.

Covers config._utc_now_ts / _make_token / _decode_token. These encode the
login system's lifetime rules, so a regression here either logs everyone out
or lets a stale session live forever.
"""
from datetime import datetime, timezone

import pytest


def _user(role="user", uid=1):
    return {"id": uid, "username": f"tester{uid}", "role": role}


# --------------------------------------------------------------------------
# _utc_now_ts
# --------------------------------------------------------------------------

def test_utc_now_ts_is_a_true_utc_epoch(app_module):
    """Must match a timezone-aware UTC epoch, NOT the old naive utcnow()
    value, which .timestamp() interpreted as local time."""
    import config
    expected = int(datetime.now(timezone.utc).timestamp())
    assert abs(config._utc_now_ts() - expected) <= 2


def test_utc_now_ts_is_not_offset_by_local_timezone(app_module):
    """On a non-UTC machine the old naive form differed from the correct value
    by the machine's UTC offset. Guards against that being reintroduced.

    The old buggy value is reconstructed arithmetically rather than by calling
    datetime.utcnow(): that call is itself deprecated (it would make this test
    emit the very warning the fix removed) and will eventually be deleted from
    Python, which would break this test. utcnow() returned a naive datetime
    whose wall-clock fields were UTC, and .timestamp() then treated those
    fields as LOCAL time — so the result was the true epoch shifted by the
    local UTC offset, which is exactly what is computed here.
    """
    import config

    correct = int(datetime.now(timezone.utc).timestamp())

    # Local UTC offset in seconds, for the current moment (handles DST).
    local_offset = datetime.now(timezone.utc).astimezone().utcoffset()
    offset_seconds = int(local_offset.total_seconds()) if local_offset else 0

    # What the old naive utcnow().timestamp() would have produced.
    old_buggy_value = correct - offset_seconds

    assert abs(config._utc_now_ts() - correct) <= 2, "must be a true UTC epoch"

    # On a machine that is not UTC, the two differ — and _utc_now_ts() must
    # agree with the correct value, never with the old buggy one.
    if abs(offset_seconds) > 60:
        assert abs(config._utc_now_ts() - old_buggy_value) > 60, (
            "still producing the old timezone-offset timestamp"
        )


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------

def test_token_round_trips(app_module):
    import config
    payload = config._decode_token(config._make_token(_user()))
    assert payload is not None
    assert payload["id"] == 1
    assert payload["role"] == "user"
    assert "issued_at" in payload and "activity" in payload


def test_fresh_token_is_accepted(app_module):
    import config
    assert config._decode_token(config._make_token(_user())) is not None


def test_tampered_token_is_rejected(app_module):
    import config
    token = config._make_token(_user())
    assert config._decode_token(token[:-6] + "AAAAAA") is None


def test_garbage_token_is_rejected(app_module):
    import config
    assert config._decode_token("not-a-token") is None
    assert config._decode_token("") is None


# --------------------------------------------------------------------------
# Expiry rules
# --------------------------------------------------------------------------

def test_idle_session_expires(app_module):
    """activity older than INACTIVITY_TIMEOUT must end the session."""
    import config
    stale = config._utc_now_ts() - (config.INACTIVITY_TIMEOUT + 60)
    token = config._signer.dumps({
        "id": 1, "username": "t", "role": "user",
        "issued_at": stale, "activity": stale,
    })
    assert config._decode_token(token) is None


def test_session_just_inside_the_idle_limit_survives(app_module):
    import config
    recent = config._utc_now_ts() - (config.INACTIVITY_TIMEOUT - 120)
    token = config._signer.dumps({
        "id": 1, "username": "t", "role": "user",
        "issued_at": recent, "activity": recent,
    })
    assert config._decode_token(token) is not None


def test_absolute_session_cap_is_enforced(app_module):
    """Even with fresh activity, a session older than SESSION_MAX_AGE ends —
    this is what stops the refresh middleware extending it forever."""
    import config
    now = config._utc_now_ts()
    token = config._signer.dumps({
        "id": 1, "username": "t", "role": "user",
        "issued_at": now - (config.SESSION_MAX_AGE + 60),
        "activity": now,
    })
    assert config._decode_token(token) is None


def test_session_just_inside_the_absolute_cap_survives(app_module):
    import config
    now = config._utc_now_ts()
    token = config._signer.dumps({
        "id": 1, "username": "t", "role": "user",
        "issued_at": now - (config.SESSION_MAX_AGE - 3600),
        "activity": now,
    })
    assert config._decode_token(token) is not None


# --------------------------------------------------------------------------
# Backward compatibility with pre-change cookies
# --------------------------------------------------------------------------

@pytest.mark.parametrize("offset_hours", [1, 5.5, 14])
def test_future_dated_cookies_are_treated_as_recent(app_module, offset_hours):
    """A timestamp ahead of now cannot come from a legitimately-aged cookie:
    it means a legacy naive-utcnow() cookie from a server east of UTC, or a
    backwards clock change. The session is genuinely recent, so it must stay
    valid rather than a negative age flowing into the comparisons."""
    import config
    now = config._utc_now_ts()
    future = now + int(offset_hours * 3600)
    token = config._signer.dumps({
        "id": 1, "username": "t", "role": "user",
        "issued_at": future, "activity": future,
    })
    assert config._decode_token(token) is not None, (
        f"a future-dated cookie ({offset_hours}h ahead) was wrongly rejected"
    )


def test_the_future_clamp_cannot_extend_an_idle_session(app_module):
    """The clamp must not become a way to outlive INACTIVITY_TIMEOUT. An
    earlier version of this fix allowed a 14h skew in both directions, which
    silently tripled the 4h idle limit — this locks that out."""
    import config
    stale = config._utc_now_ts() - (config.INACTIVITY_TIMEOUT + 60)
    token = config._signer.dumps({
        "id": 1, "username": "t", "role": "user",
        "issued_at": stale, "activity": stale,
    })
    assert config._decode_token(token) is None


def test_a_genuinely_old_session_still_expires(app_module):
    import config
    ancient = config._utc_now_ts() - (config.SESSION_MAX_AGE + 86400)
    token = config._signer.dumps({
        "id": 1, "username": "t", "role": "user",
        "issued_at": ancient, "activity": ancient,
    })
    assert config._decode_token(token) is None


# --------------------------------------------------------------------------
# Missing fields (cookies minted before these fields existed)
# --------------------------------------------------------------------------

def test_token_without_timestamps_is_accepted(app_module):
    """issued_at/activity are checked only when present, so a very old cookie
    predating them is not rejected outright."""
    import config
    token = config._signer.dumps({"id": 1, "username": "t", "role": "user"})
    assert config._decode_token(token) is not None
