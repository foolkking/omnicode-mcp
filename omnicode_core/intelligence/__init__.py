"""Intelligence Composer — the unified entry point.

Implements the eight-capability final goal from architecture-v2.md
section 17 (`十七、最终目标`):

    1. 代码理解            (code understanding)
    2. 结构化上下文压缩    (structured context compression)
    3. 搜索与引用解析      (search & reference resolution)
    4. 调用图影响分析      (call-graph impact analysis)
    5. 安全 patch 操作     (safe patch operations)
    6. 记忆主动召回        (proactive memory recall)
    7. 可视化调试控制台    (debug console — capability surface)
    8. 可选 LLM 增强       (optional LLM enhancement)

`build_intelligence_context()` orchestrates as many of the eight as the
caller asks for in a single call, deduplicates, and returns a structured
payload that fits inside a configurable token budget.

The composer is a thin coordinator: it never reaches into HTTP. All
heavy lifting goes through the existing service singletons in
`core.dependencies` so the deps graph stays one-way (composer →
services, never the reverse).
"""

from omnicode_core.intelligence.composer import (
    Capability,
    CapabilityStatus,
    IntelligenceComposer,
    IntelligenceContext,
    list_capabilities,
)

__all__ = [
    "Capability",
    "CapabilityStatus",
    "IntelligenceContext",
    "IntelligenceComposer",
    "list_capabilities",
]
