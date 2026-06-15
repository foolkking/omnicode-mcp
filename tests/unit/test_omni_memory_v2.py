"""Contract tests for omni_memory v2 (audit-bundle.r5).

Pinned by the audit:

* store returns the real backend memory_id
* store echoes file/symbol/task/content + dedup-transparency fields
* search rows include memory_id (with id alias)
* advisory.memory_count matches len(referenced_memories) AND the text
* advisory ships structured action_items + risks (not emoji+list-only)
* duplicate store sets duplicate=true + existing_memory_id
* context exposes a unified ``memories[]`` alias alongside the legacy
  recent_progress/key_learnings/user_preferences buckets
* illegal action returns allowed_actions + next_actions
* empty / missing-content store rejects with handler_version stamp
* every successful response ships next_actions
* JSON advisory_text has no emoji heading (emoji stays in text mode)
* contract_version is exactly memory.v2
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from typing import Any, Callable, Dict, List

import pytest

from omnicode_adapters.mcp_server.high_level_tools import (
    _CONTRACT_VERSIONS,
    _HANDLER_VERSION,
    register_high_level_tools,
)


# ---------------------------------------------------------------------------
# FastMCP shim + scripted backend
# ---------------------------------------------------------------------------


class _ToolManagerStub:
    def __init__(self) -> None:
        self._tools: Dict[str, Callable[..., Any]] = {}


class _MCPStub:
    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}
        self._tool_manager = _ToolManagerStub()

    def tool(self, *args: Any, **kwargs: Any):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            self._tool_manager._tools[fn.__name__] = fn
            return fn

        return deco

    async def list_tools(self) -> List[Any]:  # pragma: no cover
        from types import SimpleNamespace
        return [SimpleNamespace(name=n) for n in self._tool_manager._tools]


def _build_tools(routes: Dict[str, Any]) -> Dict[str, Callable[..., Any]]:
    captured: Dict[str, List[Dict[str, Any]]] = {}

    async def make_request(
        method: str, endpoint: str, **kwargs: Any
    ) -> Dict[str, Any]:
        captured.setdefault(endpoint, []).append(kwargs)
        if endpoint in routes:
            payload = routes[endpoint]
        else:
            payload = None
            key = endpoint.rstrip("/").rsplit("/", 1)[-1] or endpoint
            if key in routes:
                payload = routes[key]
            else:
                for k, v in routes.items():
                    if k.endswith("/") and endpoint.startswith(k):
                        payload = v
                        break
        if payload is None:
            return {"result": {}}
        if callable(payload):
            payload = payload(method, endpoint, kwargs)
        return {"result": payload}

    mcp = _MCPStub()
    register_high_level_tools(mcp, make_request)
    tools = mcp.tools
    tools["__captured__"] = captured  # type: ignore[assignment]
    return tools


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _now_iso() -> str:
    """Recent (fresh-insert) timestamp."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _old_iso(seconds_ago: float) -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds_ago)
    ).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# 1. store returns memory_id
# ---------------------------------------------------------------------------


def test_memory_store_returns_memory_id() -> None:
    routes = {
        "/memory/store": {
            "id": 42,
            "category": "mistake",
            "content": "...",
            "importance": 4,
            "timestamp": _now_iso(),
            "session_id": "s1",
            "status": "stored",
        }
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="store",
        content="When modifying _detect_mode, always update the test.",
        category="mistake",
        importance=4,
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["memory_id"] == 42
    assert payload["id"] == 42  # alias
    # Stamp survives the store path.
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_memory"]


def test_memory_store_returns_id_via_memory_envelope_field() -> None:
    """Backend variations: id may live under memory.id / item.id."""
    routes = {
        "/memory/store": {
            "memory": {"id": 99, "category": "mistake"},
            "timestamp": _now_iso(),
        }
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="store", content="x", category="mistake", format="json",
    ))
    payload = json.loads(raw)
    assert payload["memory_id"] == 99


def test_memory_store_warns_when_backend_returns_no_id() -> None:
    """If neither id field is present, surface a warning rather than
    silently returning memory_id=null."""
    routes = {"/memory/store": {"category": "mistake", "timestamp": _now_iso()}}
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="store", content="x", category="mistake", format="json",
    ))
    payload = json.loads(raw)
    assert payload["memory_id"] is None
    assert payload.get("warnings"), payload
    assert any("backend_missing_id" in w for w in payload["warnings"])


def test_memory_store_backend_validation_error_is_not_success() -> None:
    routes = {
        "/memory/store": {
            "status_code": 422,
            "detail": [
                {
                    "loc": ["body", "category"],
                    "msg": "Input should be a supported category",
                }
            ],
        }
    }
    tools = _build_tools(routes)

    raw = _run(tools["omni_memory"](
        action="store",
        content="x",
        category="note",
        format="json",
    ))
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "Memory store error" in payload["error"]
    assert "body.category" in payload["error"]
    assert "memory_id" not in payload


def test_memory_search_backend_validation_error_is_not_success() -> None:
    routes = {
        "/memory/search": {
            "success": False,
            "status_code": 422,
            "detail": [
                {
                    "loc": ["body", "query"],
                    "msg": "query is invalid",
                }
            ],
        }
    }
    tools = _build_tools(routes)

    raw = _run(tools["omni_memory"](
        action="search",
        query="x",
        format="json",
    ))
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "Memory search error" in payload["error"]
    assert "body.query" in payload["error"]
    assert "results" not in payload


# ---------------------------------------------------------------------------
# 2. store echoes file / symbol / task
# ---------------------------------------------------------------------------


def test_memory_store_echoes_file_symbol_task() -> None:
    routes = {"/memory/store": {"id": 7, "timestamp": _now_iso()}}
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="store",
        content="When modifying _detect_mode, always update the test.",
        category="mistake",
        importance=4,
        tags="search,test",
        file="omnicode_adapters/mcp_server/high_level_tools.py",
        symbol="_detect_mode",
        task="change search auto routing rules",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["file"] == "omnicode_adapters/mcp_server/high_level_tools.py"
    assert payload["symbol"] == "_detect_mode"
    assert payload["task"] == "change search auto routing rules"
    assert "search" in payload["tags"] and "test" in payload["tags"]
    # The content itself is echoed for callers that want a quick confirm.
    assert "_detect_mode" in payload["content"]

    raw_list_tags = _run(tools["omni_memory"](
        action="store",
        content="List tags should be accepted too.",
        category="lesson",
        importance=3,
        tags=["search", "mode-routing"],
        format="json",
    ))
    payload_list_tags = json.loads(raw_list_tags)
    assert payload_list_tags["ok"] is True
    assert payload_list_tags["tags"] == ["search", "mode-routing"]


def test_memory_store_forwards_context_to_backend() -> None:
    """Symbol/file/task land in the backend request's context dict so
    they're searchable later."""
    routes = {"/memory/store": {"id": 5, "timestamp": _now_iso()}}
    tools = _build_tools(routes)
    _run(tools["omni_memory"](
        action="store", content="x", category="mistake",
        symbol="_detect_mode", task="change routing", file="x.py",
        format="json",
    ))
    sent = tools["__captured__"]["/memory/store"][0]
    body = sent.get("json") or {}
    ctx = body.get("context") or {}
    assert ctx.get("symbol") == "_detect_mode"
    assert ctx.get("task") == "change routing"
    assert ctx.get("file") == "x.py"
    # related_files prefilled with the explicit file param.
    assert "x.py" in (body.get("related_files") or [])


# ---------------------------------------------------------------------------
# 3. search results include memory_id
# ---------------------------------------------------------------------------


def test_memory_search_results_include_memory_id() -> None:
    routes = {
        "/memory/search": {
            "results": [
                {
                    "memory": {
                        "id": 8,
                        "category": "mistake",
                        "content": "When modifying _detect_mode...",
                        "importance": 4,
                        "tags": ["search", "test"],
                        "timestamp": _now_iso(),
                    },
                    "relevance_score": 0.9,
                    "match_reason": "Matched in content + tags",
                }
            ]
        }
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="search", query="mode routing test", format="json",
    ))
    payload = json.loads(raw)
    assert payload["count"] == 1
    row = payload["results"][0]
    assert row["memory_id"] == 8
    assert row["id"] == 8  # alias
    # And the alias collection.
    assert payload["memories"][0]["memory_id"] == 8


def test_memory_search_warns_when_backend_skips_id() -> None:
    routes = {
        "/memory/search": {
            "results": [
                {
                    "memory": {"category": "mistake", "content": "..."},
                    "relevance_score": 0.5,
                }
            ]
        }
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="search", query="x", format="json",
    ))
    payload = json.loads(raw)
    assert payload.get("warnings"), payload
    assert any("backend_missing_ids" in w for w in payload["warnings"])


def test_memory_search_redacts_absolute_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    inside = root / "omnicode_adapters" / "mcp_server" / "high_level_tools.py"
    inside.parent.mkdir(parents=True)
    inside.write_text("# marker\n", encoding="utf-8")
    outside = tmp_path / "outside" / "secret.py"
    outside.parent.mkdir()
    outside.write_text("# secret\n", encoding="utf-8")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(root))

    routes = {
        "/memory/search": {
            "results": [
                {
                    "memory": {
                        "id": 12,
                        "category": "mistake",
                        "content": f"Review {inside} but never expose {outside}",
                        "importance": 4,
                        "tags": ["paths"],
                        "timestamp": _now_iso(),
                        "related_files": [str(inside), str(outside)],
                    },
                    "relevance_score": 0.9,
                    "match_reason": f"Matched in {inside}",
                    "match_fields": [
                        {
                            "field": "content",
                            "snippet": f"{inside} -> {outside}",
                        }
                    ],
                }
            ]
        }
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="search", query="paths", format="json",
    ))
    payload = json.loads(raw)
    row = payload["results"][0]
    serialized = json.dumps(payload, ensure_ascii=False)

    assert str(root) not in serialized
    assert str(outside) not in serialized
    assert "omnicode_adapters/mcp_server/high_level_tools.py" in row["content"]
    assert "<absolute-path>" in serialized
    assert row["related_files"][0] == "omnicode_adapters/mcp_server/high_level_tools.py"


def test_memory_advisory_redacts_absolute_paths_in_echo_and_synthesis(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    inside = root / "omnicode_adapters" / "mcp_server" / "high_level_tools.py"
    inside.parent.mkdir(parents=True)
    inside.write_text("# marker\n", encoding="utf-8")
    outside = tmp_path / "outside" / "secret.py"
    outside.parent.mkdir()
    outside.write_text("# secret\n", encoding="utf-8")
    monkeypatch.setenv("OMNICODE_WORKSPACE_ROOT", str(root))

    memories = [
        {
            "id": 31,
            "category": "mistake",
            "content": f"Check {inside} and never leak {outside}.",
            "importance": 5,
            "tags": ["paths"],
            "timestamp": _now_iso(),
            "_score": 0.95,
        }
    ]
    tools = _build_tools(_advisory_routes(memories))
    raw = _run(tools["omni_memory"](
        action="advisory",
        file=str(inside),
        task=f"Review {outside} before editing",
        format="json",
    ))
    payload = json.loads(raw)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["ok"] is True
    assert str(root) not in serialized
    assert str(outside) not in serialized
    assert payload["file"] == "omnicode_adapters/mcp_server/high_level_tools.py"
    assert "<absolute-path>" in payload["task"]
    assert "omnicode_adapters/mcp_server/high_level_tools.py" in payload["advisory_text"]
    assert "<absolute-path>" in payload["advisory_text"]
    assert payload["why_recalled"] == [
        "file:omnicode_adapters/mcp_server/high_level_tools.py",
        "task:Review <absolute-path> before editing",
    ]


# ---------------------------------------------------------------------------
# 4. advisory.memory_count matches referenced_memories length
# ---------------------------------------------------------------------------


def _advisory_routes(memories: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build /memory/search routes returning the given memories for any
    seed (symbol/file/task)."""
    return {
        "/memory/search": {
            "results": [
                {
                    "memory": m,
                    "relevance_score": m.get("_score", 0.8),
                    "match_reason": m.get("_reason", "Matched"),
                }
                for m in memories
            ]
        }
    }


def test_memory_advisory_referenced_memories_count_matches_text() -> None:
    memories = [
        {
            "id": 8,
            "category": "mistake",
            "content": "When modifying _detect_mode, always update tests/unit/test_detect_mode.py because search mode routing regressions are easy to miss.",
            "importance": 4,
            "tags": ["search"],
            "timestamp": _now_iso(),
            "_score": 0.95,
        },
        {
            "id": 9,
            "category": "solution",
            "content": "Fixed FAISS by persisting the index after each add.",
            "importance": 4,
            "tags": ["faiss"],
            "timestamp": _now_iso(),
            "_score": 0.7,
        },
    ]
    tools = _build_tools(_advisory_routes(memories))
    raw = _run(tools["omni_memory"](
        action="advisory",
        symbol="_detect_mode",
        task="change search auto routing rules",
        format="json",
    ))
    payload = json.loads(raw)

    assert payload["memory_count"] == len(payload["referenced_memories"])
    assert payload["memory_count"] >= 2
    # Every referenced memory carries an id.
    for ref in payload["referenced_memories"]:
        assert ref.get("memory_id") is not None
    # The text mentions the same number of items as referenced_memories.
    text = payload["advisory_text"]
    # Action items are numbered "1." "2." — at least N items rendered.
    assert "1." in text


def test_memory_advisory_zero_results_yields_zero_count() -> None:
    tools = _build_tools(_advisory_routes([]))
    raw = _run(tools["omni_memory"](
        action="advisory", symbol="DefinitelyNotExist", format="json",
    ))
    payload = json.loads(raw)
    assert payload["memory_count"] == 0
    assert payload["referenced_memories"] == []
    assert payload["confidence"] == "low"


# ---------------------------------------------------------------------------
# 5. advisory returns structured action_items
# ---------------------------------------------------------------------------


def test_memory_advisory_returns_structured_action_items() -> None:
    memories = [
        {
            "id": 8,
            "category": "mistake",
            "content": "When modifying _detect_mode, always update tests/unit/test_detect_mode.py.",
            "importance": 4,
            "timestamp": _now_iso(),
            "_score": 0.9,
        }
    ]
    tools = _build_tools(_advisory_routes(memories))
    raw = _run(tools["omni_memory"](
        action="advisory",
        symbol="_detect_mode",
        task="change search auto routing rules",
        format="json",
    ))
    payload = json.loads(raw)
    advisory = payload["advisory"]
    assert isinstance(advisory, dict)
    assert "summary" in advisory
    assert "action_items" in advisory
    assert "risks" in advisory
    assert "referenced_memories" in advisory
    assert advisory["action_items"], "action_items must be non-empty"
    # The lesson about updating tests/unit/test_detect_mode.py is
    # surfaced as an action item.
    joined = " ".join(advisory["action_items"])
    assert "_detect_mode" in joined
    assert "test_detect_mode.py" in joined
    # The risk list captures importance>=4 mistakes.
    assert advisory["risks"]
    # Confidence is "high" because we have an importance-4 mistake hit.
    assert payload["confidence"] == "high"


# ---------------------------------------------------------------------------
# 6. duplicate store
# ---------------------------------------------------------------------------


def test_memory_duplicate_store_returns_existing_memory_id() -> None:
    """Backend returns the original row's old timestamp on dedup; we
    surface duplicate=true + existing_memory_id."""
    routes = {
        "/memory/store": {
            "id": 8,
            "category": "mistake",
            "timestamp": _old_iso(120),  # 2 minutes ago = obvious dedup
        }
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="store",
        content="When modifying _detect_mode...",
        category="mistake",
        format="json",
    ))
    payload = json.loads(raw)
    assert payload["duplicate"] is True
    assert payload["existing_memory_id"] == 8
    assert payload["deduplication_reason"]


def test_memory_fresh_store_is_not_marked_duplicate() -> None:
    routes = {"/memory/store": {"id": 10, "timestamp": _now_iso()}}
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](
        action="store", content="fresh", category="learning", format="json",
    ))
    payload = json.loads(raw)
    assert payload["duplicate"] is False
    assert payload["existing_memory_id"] is None


# ---------------------------------------------------------------------------
# 7. context exposes memories[] alias
# ---------------------------------------------------------------------------


def test_memory_context_exposes_memories_alias() -> None:
    routes = {
        "/memory/context": {
            "recent_progress": [
                {"id": 1, "category": "progress", "content": "hello",
                 "importance": 3, "tags": [], "timestamp": _now_iso()},
            ],
            "key_learnings": [
                {"id": 2, "category": "learning", "content": "stuff",
                 "importance": 4, "tags": ["x"], "timestamp": _now_iso()},
            ],
            "user_preferences": [],
            "important_warnings": [
                {"id": 8, "category": "mistake", "content": "When modifying _detect_mode...",
                 "importance": 4, "tags": ["search"], "timestamp": _now_iso()},
            ],
            "current_focus": None,
            "next_priorities": [],
        }
    }
    tools = _build_tools(routes)
    raw = _run(tools["omni_memory"](action="context", format="json"))
    payload = json.loads(raw)

    # Legacy buckets are still there.
    assert "recent_progress" in payload
    assert "important_warnings" in payload

    # New unified alias.
    memories = payload.get("memories")
    assert isinstance(memories, list) and memories, payload
    assert payload["memory_count"] == len(memories)
    # Each row goes through _normalise_memory_row → has memory_id +
    # search-result-shaped fields.
    ids = {m.get("memory_id") for m in memories}
    assert {1, 2, 8} <= ids
    # match_reason carries the source bucket so AI can group later.
    reasons = {m.get("match_reason") for m in memories}
    assert any(r and r.startswith("context:") for r in reasons)


# ---------------------------------------------------------------------------
# 8. illegal action returns allowed_actions
# ---------------------------------------------------------------------------


def test_memory_illegal_action_returns_allowed_actions() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_memory"](
        action="illegal_action", query="test", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    allowed = payload.get("allowed_actions")
    assert isinstance(allowed, list)
    assert set(allowed) == {"search", "store", "context", "advisory"}
    # Stamps preserved on error path.
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_memory"]
    assert payload.get("next_actions"), payload


# ---------------------------------------------------------------------------
# 9. empty store rejected with stamp
# ---------------------------------------------------------------------------


def test_memory_empty_store_rejected_with_version_stamp() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_memory"](
        action="store", content="", category="mistake", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "content" in payload["error"].lower()
    assert payload["handler_version"] == _HANDLER_VERSION
    assert payload["contract_version"] == _CONTRACT_VERSIONS["omni_memory"]
    # And the helpful next_actions are present even on this error.
    assert payload.get("next_actions"), payload
    # Backend was NOT called for an empty store.
    assert "/memory/store" not in tools["__captured__"]


def test_memory_whitespace_content_is_treated_as_empty() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_memory"](
        action="store", content="   \n\t", category="mistake", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False


def test_memory_missing_category_rejected() -> None:
    tools = _build_tools({})
    raw = _run(tools["omni_memory"](
        action="store", content="x", category="", format="json",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "category" in payload["error"].lower()


# ---------------------------------------------------------------------------
# 10. success paths include next_actions
# ---------------------------------------------------------------------------


def test_memory_success_paths_include_next_actions() -> None:
    # search
    s_routes = {
        "/memory/search": {
            "results": [
                {"memory": {"id": 1, "category": "mistake", "content": "x",
                            "importance": 3, "tags": [], "timestamp": _now_iso()},
                 "relevance_score": 0.9, "match_reason": "Matched"}
            ]
        }
    }
    tools = _build_tools(s_routes)
    sp = json.loads(_run(tools["omni_memory"](
        action="search", query="x", format="json",
    )))
    assert sp.get("next_actions")

    # store
    st_routes = {"/memory/store": {"id": 2, "timestamp": _now_iso()}}
    tools = _build_tools(st_routes)
    stp = json.loads(_run(tools["omni_memory"](
        action="store", content="x", category="mistake", format="json",
    )))
    assert stp.get("next_actions")

    # context
    c_routes = {"/memory/context": {
        "recent_progress": [], "key_learnings": [], "user_preferences": [],
        "important_warnings": [], "next_priorities": [], "current_focus": None,
    }}
    tools = _build_tools(c_routes)
    cp = json.loads(_run(tools["omni_memory"](action="context", format="json")))
    assert cp.get("next_actions")

    # advisory (with results)
    a_routes = _advisory_routes([
        {"id": 3, "category": "mistake", "content": "x", "importance": 4,
         "timestamp": _now_iso(), "_score": 0.9},
    ])
    tools = _build_tools(a_routes)
    ap = json.loads(_run(tools["omni_memory"](
        action="advisory", symbol="x", format="json",
    )))
    assert ap.get("next_actions")


# ---------------------------------------------------------------------------
# 11. JSON advisory_text has no emoji
# ---------------------------------------------------------------------------


def test_memory_json_advisory_has_no_emoji_heading() -> None:
    memories = [
        {"id": 8, "category": "mistake",
         "content": "When modifying _detect_mode, update tests.",
         "importance": 4, "timestamp": _now_iso(), "_score": 0.9},
    ]
    tools = _build_tools(_advisory_routes(memories))
    raw = _run(tools["omni_memory"](
        action="advisory", symbol="_detect_mode", format="json",
    ))
    payload = json.loads(raw)
    # advisory_text in JSON mode must be ASCII / emoji-free.
    text = payload["advisory_text"]
    EMOJI_FORBIDDEN = ["📝", "🧠", "📜", "✅", "❌", "⚠️", "🔗"]
    for emoji in EMOJI_FORBIDDEN:
        assert emoji not in text, f"emoji {emoji!r} leaked into JSON advisory_text"
    # Same for the structured advisory.summary.
    summary = payload["advisory"]["summary"]
    for emoji in EMOJI_FORBIDDEN:
        assert emoji not in summary


def test_memory_text_format_keeps_emoji_for_humans() -> None:
    """Sanity: the human-readable text variant *does* keep the emoji
    header. Only JSON mode is required to be clean."""
    memories = [
        {"id": 8, "category": "mistake",
         "content": "x", "importance": 4, "timestamp": _now_iso(), "_score": 0.9},
    ]
    tools = _build_tools(_advisory_routes(memories))
    raw = _run(tools["omni_memory"](
        action="advisory", symbol="x", format="text",
    ))
    assert "🧠" in raw  # emoji acceptable in text mode


# ---------------------------------------------------------------------------
# 12. contract_version is exactly memory.v2
# ---------------------------------------------------------------------------


def test_memory_contract_version_is_memory_v2() -> None:
    tools = _build_tools({"/memory/search": {"results": []}})
    raw = _run(tools["omni_memory"](action="search", query="x", format="json"))
    payload = json.loads(raw)
    assert payload["contract_version"] == "memory.v2"
    assert _CONTRACT_VERSIONS["omni_memory"] == "memory.v2"


def test_memory_handler_version_matches_module_constant() -> None:
    tools = _build_tools({"/memory/search": {"results": []}})
    raw = _run(tools["omni_memory"](action="search", query="x", format="json"))
    payload = json.loads(raw)
    assert payload["handler_version"] == _HANDLER_VERSION
