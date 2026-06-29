# Documentation Index

Start with the shortest document that answers the current question. `PROJECT_STATE.md`
is the fastest way for a future AI agent to recover the current repository state.

## Start Here

| Document | Purpose |
|---|---|
| [Project State](../PROJECT_STATE.md) | Current AI-readable repo snapshot, implementation status, known risks |
| [README](../README.md) | Human overview and quick start |
| [Usage Guide](usage.md) | Install, CLI, MCP, indexing, embedding model cache, optional LLM features |
| [Architecture](architecture.md) | Capability-aware architecture and module map |
| [Deployment & Security](deployment.md) | Local, cloud, hybrid deployment and safety model |
| [API Reference](api.md) | HTTP endpoint reference |
| [Roadmap](roadmap.md) | Historical roadmap and post-r60 future work |

## Current r60 Priorities

| Area | Read |
|---|---|
| Safe editing and rollback | [Architecture](architecture.md#capability-aware-r60-contract), [Usage Guide](usage.md#deterministic-index-and-search) |
| Local/cloud hybrid deployment | [Deployment & Security](deployment.md#hybrid-mcp--cloud-sync) |
| Exact index and search fallback | [Usage Guide](usage.md#deterministic-index-and-search), [Architecture](architecture.md#deterministic-index-and-search) |
| Embedding model cache | [Usage Guide](usage.md#embedding-model-cache), [Deployment & Security](deployment.md#embedding-models-in-deployment) |
| Remaining verification work | [Project State](../PROJECT_STATE.md#8-known-issues--risks) |

## Documentation Policy

- Keep root docs short and durable.
- Put current implementation status in [Project State](../PROJECT_STATE.md).
- Put usage commands in [Usage Guide](usage.md).
- Put architecture decisions and capability boundaries in [Architecture](architecture.md).
- Do not persist secrets, local personal paths, raw logs, or one-off conversation transcripts.
