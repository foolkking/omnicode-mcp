"""Regression tests for the bugs reported in 2026-05-22 evening session:

1. /symbols/graph and /symbols/relations were being shadowed by the catch-all
   /symbols/{file_path:path} handler — fixed by moving the catch-all to the
   bottom of the router.
2. /read endpoint accepts the literal string "null" / "undefined" for
   symbol_name / start_line / end_line because URLSearchParams stringifies
   None as "null".
3. Provider /test endpoint must return a single-line error string, not a
   wall of LiteLLM stack frames.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Route resolution for /search/symbols/graph
# ---------------------------------------------------------------------------
def test_symbols_graph_resolves_to_dedicated_handler(client):
    """/symbols/graph must hit the call-graph builder, not the file-symbol
    catch-all (which previously interpreted ``graph`` as a filename)."""
    r = client.get("/search/symbols/graph", params={"max_files": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    result = body["result"]
    # Must have the call-graph response shape, NOT the file-symbol shape.
    assert "summary" in result
    assert "edges" in result
    assert "scope_path" in result
    # Call-graph shape carries (caller, callee, line) tuples.
    if result["edges"]:
        edge = result["edges"][0]
        assert "caller" in edge
        assert "callee" in edge


def test_symbols_relations_does_not_collide_with_catch_all(client):
    """POST /symbols/relations should be POST-only and not match the GET
    catch-all even when the symbol name happens to be ``relations``."""
    # Wrong method should yield 405 (Method Not Allowed), proving the route
    # exists and isn't being captured by the GET catch-all.
    r = client.get("/search/symbols/relations")
    assert r.status_code in (404, 405), r.status_code
    if r.status_code == 404:
        # If our reserved-path guard kicked in, it should mention the
        # reserved name.
        body = r.json()
        assert "relations" in (body.get("error") or "").lower()


def test_inheritance_endpoint_returns_graph(client):
    r = client.get("/search/inheritance", params={"max_files": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert "summary" in body["result"]


# ---------------------------------------------------------------------------
# 2. /read tolerates "null" literal strings
# ---------------------------------------------------------------------------
def test_read_with_null_string_params(client):
    """URLSearchParams stringifies None as the literal "null".  The /read
    endpoint must coerce those back to absent."""
    r = client.post(
        "/read",
        params={
            "file_path": "main.py",
            "symbol_name": "null",
            "occurrence": 1,
            "start_line": "null",
            "end_line": "null",
            "with_line_numbers": "true",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    res = body["result"]
    assert res.get("success") is True
    assert res["file_path"] == "main.py"
    assert res["total_lines"] > 0
    assert "create_app" in res["content"] or "FastAPI" in res["content"]


def test_read_with_real_line_range(client):
    r = client.post(
        "/read",
        params={
            "file_path": "main.py",
            "start_line": 1,
            "end_line": 5,
            "with_line_numbers": "true",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["success"] is True
    assert body["start_line"] == 1
    assert body["end_line"] == 5
    # Should have line numbers prepended
    assert body["content"].lstrip().startswith("1")


def test_read_invalid_int_returns_400_not_500(client):
    r = client.post(
        "/read",
        params={"file_path": "main.py", "start_line": "abc", "end_line": "5"},
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert "abc" in body["error"]


# ---------------------------------------------------------------------------
# 3. Provider /test returns concise error
# ---------------------------------------------------------------------------
def test_provider_test_unknown_returns_404_style(client):
    """An unknown provider returns 200 with success=False at the inner level
    so the UI can render a structured error (and hint, when available)
    instead of an opaque traceback."""
    r = client.post("/providers/this-provider-does-not-exist-xyz/test")
    assert r.status_code == 200, r.text
    body = r.json()
    inner = body.get("result", {})
    assert inner.get("success") is False
    err = inner.get("error", "")
    # Single line, no stack-frame markers.
    assert "\n" not in err.strip(), f"Expected single-line error, got: {err!r}"
    assert "Traceback" not in err
    assert "this-provider-does-not-exist-xyz" in err.lower() or "unknown" in err.lower()



# ---------------------------------------------------------------------------
# 4. /edit and /write surface pipeline failures as 200 + structured payload
# ---------------------------------------------------------------------------
def test_edit_pipeline_failure_returns_200_with_failure_analysis(client, tmp_path, monkeypatch):
    """When the LLM call fails (e.g. no provider configured / wrong key),
    /edit must return 200 with `result.success=false` and a structured
    `failure_analysis` block — NOT 422.  422 used to make the API client
    throw and discard the diagnostic payload.
    """
    # Force the edit pipeline's process_edit to return an unsuccessful result
    # so we exercise the failure branch without making real LLM calls.
    from core import dependencies as deps

    pipeline = deps.get_edit_pipeline()
    if pipeline is None:
        pytest.skip("Edit pipeline not initialised (lifespan-dependent)")

    class _StubResult:
        success = False
        instructions = "x"
        summary = ""
        quality_score = 0.0
        gemini_edit_success = False
        format_success = True
        error_correction_attempts = 0
        total_gemini_calls = 0
        processing_time_seconds = 0.01
        original_content = "print(1)\n"
        final_content = "print(1)\n"
        gemini_errors = ["litellm.AuthenticationError: API key not valid"]
        format_errors = []
        warnings = []
        file_path = "main.py"

    async def _fake_process_edit(*args, **kwargs):
        return _StubResult()

    monkeypatch.setattr(pipeline, "process_edit", _fake_process_edit)

    r = client.post(
        "/edit",
        json={
            "target_file": "main.py",
            "instructions": "noop",
            "code_edit": "# ... existing code ...\n",
            "save_to_file": False,
        },
    )
    assert r.status_code == 200, r.text  # NOT 422
    body = r.json()
    assert body["success"] is True   # HTTP-layer success
    inner = body["result"]
    assert inner["success"] is False  # business-logic failure
    fa = inner["failure_analysis"]
    assert fa["failure_stage"] == "llm_edit"
    assert "AuthenticationError" in fa["root_cause"] or "API key" in fa["root_cause"]
    assert fa["suggested_fixes"]
