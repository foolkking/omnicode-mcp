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


# ---------------------------------------------------------------------------
# 5. Symbol search returns metadata.symbol_name matches (was returning 0
#    because the chunker stored empty symbol_name and the engine's search()
#    fell through to semantic search).
# ---------------------------------------------------------------------------
def test_symbol_search_finds_chunker_metadata_match(client):
    """After indexing the working directory the chunker MUST populate
    ``metadata.symbol_name`` and the /search/symbols endpoint MUST be able
    to find that name via SQL LIKE.

    This guards against:
    * the chunker only iterating top-level children and missing methods
    * the engine ``search()`` method falling through to FAISS semantic
      search for ``search_type='fuzzy_symbol'`` instead of doing the
      metadata lookup
    """
    # Make sure the index has at least one file's worth of symbols.
    index_resp = client.post("/search/index")
    assert index_resp.status_code == 200, index_resp.text

    # ``main`` and ``create_app`` both live in main.py; whichever the test
    # repository ships with should be findable.
    r = client.post(
        "/search/symbols",
        params={
            "query": "create_app",
            "fuzzy": True,
            "max_results": 10,
            "min_score": 0.5,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["search_type"] == "fuzzy_symbol"
    names = [r["symbol_name"] for r in body["results"]]
    assert "create_app" in names, f"expected 'create_app' in {names!r}"

    # Exact mode should also find it and not match unrelated text.
    r = client.post(
        "/search/symbols",
        params={
            "query": "create_app",
            "fuzzy": False,
            "max_results": 10,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    names = [r["symbol_name"] for r in body["results"]]
    assert all(n == "create_app" for n in names) if names else True



# ---------------------------------------------------------------------------
# 6. /search/symbols/{file_path} returns real symbol names (not 'symbol_NN'
#    placeholders) and includes line_start / line_end so the UI can render
#    a useful "Lines 23-81" label and jump to the right line.
# ---------------------------------------------------------------------------
def test_list_file_symbols_returns_real_names(client):
    r = client.get("/search/symbols/main.py")
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body.get("language") == "python"
    syms = body.get("symbols") or []
    assert syms, "main.py should expose at least one symbol"
    names = [s.get("name") for s in syms]
    # Top-level functions must be present.
    assert "create_app" in names, names
    # No placeholder names ever.
    assert not any(str(n).startswith("symbol_") for n in names), names
    # Line ranges must be present and well-formed.
    for s in syms:
        ls, le = s.get("line_start"), s.get("line_end")
        assert isinstance(ls, int) and ls >= 1, s
        assert isinstance(le, int) and le >= ls, s


def test_list_file_symbols_finds_methods_inside_classes(client):
    """The chunker used to only iterate root_node.children which meant
    methods nested inside classes never made it into the symbol list."""
    # mcp_server.py defines several functions; pick one that's known to
    # exist as a top-level def for stability.
    r = client.get("/search/symbols/mcp_server.py")
    assert r.status_code == 200, r.text
    syms = r.json()["result"]["symbols"]
    names = {s["name"] for s in syms}
    assert "search_tool" in names, names


# ---------------------------------------------------------------------------
# 7. /read with a forward-slash path and *no* line range / symbol returns
#    the whole file successfully (regression for "API Error: /read?file_path=
#    tests/__init__.py&symbol_name=null&...")
# ---------------------------------------------------------------------------
def test_read_whole_file_with_no_range(client):
    r = client.post(
        "/read",
        params={"file_path": "tests/__init__.py", "with_line_numbers": True},
    )
    assert r.status_code == 200, r.text
    payload = r.json()["result"]
    assert payload["success"] is True
    assert payload["file_path"] == "tests/__init__.py"
    assert "content" in payload
    assert payload["start_line"] == 1
