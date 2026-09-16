"""
AI reply parsing and verdict validation.

Covers the guards added for the audit's "no schema validation post-parse" and
the PDF-upload crash. The central property: a malformed or hostile AI reply
must degrade to a safe N/A, and must NEVER become a false "Yes".
"""
import pytest


# --------------------------------------------------------------------------
# routers.qc_checks — checklist verdict validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["Yes", "No", "N/A"])
def test_valid_statuses_pass_through_unchanged(app_module, value):
    """The no-functionality-change guarantee: a real verdict is untouched."""
    from routers.qc_checks import _clean_status
    assert _clean_status(value) == value


@pytest.mark.parametrize("given,expected", [
    ("yes", "Yes"), ("YES", "Yes"), ("  Yes  ", "Yes"),
    ("no", "No"), ("NO", "No"), (" No ", "No"),
    ("n/a", "N/A"), ("N/a", "N/A"), ("na", "N/A"), ("NA", "N/A"),
])
def test_near_miss_statuses_are_repaired(app_module, given, expected):
    from routers.qc_checks import _clean_status
    assert _clean_status(given) == expected


@pytest.mark.parametrize("value", [
    "Maybe", "PASS", "FAIL", "true", "1", "", "   ",
    None, 123, 0, [], {}, ["Yes"], {"status": "Yes"},
])
def test_unknown_statuses_never_become_a_false_pass(app_module, value):
    """The key safety property. Anything unrecognised must be N/A — a QC
    item must never be reported as passing because of a malformed reply."""
    from routers.qc_checks import _clean_status, _VALID_STATUSES
    out = _clean_status(value)
    assert out in _VALID_STATUSES
    assert out == "N/A"


def test_remark_accepts_a_normal_string(app_module):
    from routers.qc_checks import _clean_remark
    assert _clean_remark("all documents present") == "all documents present"


@pytest.mark.parametrize("given,expected", [
    (None, ""),
    ("  padded  ", "padded"),
    (["one", "two"], "one two"),
    (("x", "y"), "x y"),
    (42, "42"),
])
def test_remark_coerces_non_strings(app_module, given, expected):
    """The model occasionally returns a list of sentences; storing it raw
    would render and export as a Python repr."""
    from routers.qc_checks import _clean_remark
    assert _clean_remark(given) == expected


def test_remark_is_length_capped(app_module):
    from routers.qc_checks import _clean_remark
    assert len(_clean_remark("x" * 10_000)) == 2000


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('```\n{"a": 1}\n```', {"a": 1}),
    ('  {"a": 1}  ', {"a": 1}),
])
def test_parse_claude_json_reads_valid_replies(app_module, raw, expected):
    from routers.qc_checks import _parse_claude_json
    assert _parse_claude_json(raw) == expected


@pytest.mark.parametrize("raw", [
    "",                  # empty reply
    None,                # no reply at all
    "I cannot answer.",  # prose instead of JSON
    "```json",           # opened a fence, never closed it -> was IndexError
    '{"a": ',            # truncated mid-object
])
def test_parse_claude_json_returns_none_instead_of_raising(app_module, raw):
    from routers.qc_checks import _parse_claude_json
    assert _parse_claude_json(raw) is None


# --------------------------------------------------------------------------
# End-to-end verdict building with a stubbed Claude client
# --------------------------------------------------------------------------

class _Block:
    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)] if text is not None else []
        self.usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()


def _stub_client(*replies):
    """A client whose messages.create() returns the given replies in order,
    repeating the last one if called again (covers the retry path)."""
    state = {"i": 0}

    class _Messages:
        @staticmethod
        def create(**_kwargs):
            i = min(state["i"], len(replies) - 1)
            state["i"] += 1
            return _Resp(replies[i])

    class _Client:
        messages = _Messages

    return _Client()


def test_single_item_check_returns_a_clean_verdict(app_module):
    from routers.qc_checks import _claude_check
    out = _claude_check(_stub_client('{"status":"Yes","remark":"ok"}'),
                        [{"type": "text", "text": "x"}])
    assert out["status"] == "Yes"
    assert out["remark"] == "ok"


def test_single_item_check_survives_a_json_list_reply(app_module):
    """Valid JSON of the wrong shape used to raise AttributeError on .get(),
    which no caller catches — an unhandled 500."""
    from routers.qc_checks import _claude_check
    out = _claude_check(_stub_client('[{"status":"Yes"}]'),
                        [{"type": "text", "text": "x"}])
    assert out["status"] == "N/A"


def test_single_item_check_survives_an_empty_reply(app_module):
    from routers.qc_checks import _claude_check
    out = _claude_check(_stub_client(None), [{"type": "text", "text": "x"}])
    assert out["status"] == "N/A"


def test_single_item_check_retries_then_succeeds(app_module):
    """Prose first, valid JSON on the corrective retry."""
    from routers.qc_checks import _claude_check
    out = _claude_check(_stub_client("I think so", '{"status":"No","remark":"missing"}'),
                        [{"type": "text", "text": "x"}])
    assert out["status"] == "No"
    assert out["remark"] == "missing"


def test_batch_check_validates_every_status(app_module):
    from routers.qc_checks import _claude_check_batch, _VALID_STATUSES
    reply = ('[{"item_index":0,"status":"Maybe","remark":"a"},'
             ' {"item_index":1,"status":"no","remark":"b"}]')
    results, _, _ = _claude_check_batch(_stub_client(reply),
                                        [{"type": "text", "text": "x"}], [0, 1])
    assert set(results) == {0, 1}
    for row in results.values():
        assert row["status"] in _VALID_STATUSES
    assert results[0]["status"] == "N/A"   # "Maybe" is not a verdict
    assert results[1]["status"] == "No"    # "no" is repaired


def test_batch_check_never_invents_or_drops_items(app_module):
    """A reply naming an index that was not requested must not be written
    onto another item's row."""
    from routers.qc_checks import _claude_check_batch
    reply = '[{"item_index":99,"status":"Yes","remark":"bogus"}]'
    results, _, _ = _claude_check_batch(_stub_client(reply),
                                        [{"type": "text", "text": "x"}], [0, 1])
    assert set(results) == {0, 1}
    assert 99 not in results
    for row in results.values():
        assert row["status"] == "N/A"


# --------------------------------------------------------------------------
# services.ai_service — PDF extraction reply handling
# --------------------------------------------------------------------------

@pytest.mark.parametrize("resp,expected", [
    (_Resp('{"a":1}'), '{"a":1}'),
    (_Resp(None), ""),          # empty content list -> was IndexError
])
def test_response_text_never_raises(app_module, resp, expected):
    from services.ai_service import _response_text
    assert _response_text(resp) == expected


def test_response_text_handles_a_missing_content_attribute(app_module):
    from services.ai_service import _response_text
    assert _response_text(object()) == ""


@pytest.mark.parametrize("raw", [
    "", None, "not json at all", "```json", '[1, 2, 3]', '"a string"',
])
def test_extraction_json_rejects_unusable_replies(app_module, raw):
    """Must be a JSON object: the callers immediately call data.get(...)."""
    from services.ai_service import _parse_extraction_json
    assert _parse_extraction_json(raw) is None


def test_extraction_json_accepts_a_fenced_object(app_module):
    from services.ai_service import _parse_extraction_json
    assert _parse_extraction_json('```json\n{"customer_name":"Dave"}\n```') == {
        "customer_name": "Dave"
    }


def test_extraction_returns_empty_dict_instead_of_raising(app_module, monkeypatch):
    """After two unusable replies it must return {} — the callers treat a
    result with no key fields as "couldn't read this PDF" and show a proper
    message, whereas raising would escape their anthropic-only handlers."""
    import services.ai_service as svc
    monkeypatch.setattr(svc, "_get_claude", lambda: _stub_client("nope", "still nope"))
    out = svc.extract_with_claude("some text")
    assert out == {}


def test_extraction_retry_recovers_a_late_valid_reply(app_module, monkeypatch):
    import services.ai_service as svc
    monkeypatch.setattr(
        svc, "_get_claude",
        lambda: _stub_client("prose", '{"customer_name":"Dave"}'),
    )
    assert svc.extract_with_claude("some text")["customer_name"] == "Dave"
