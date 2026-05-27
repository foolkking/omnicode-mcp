"""Local-agent file-sync client (Wave 2, W2-2).

The agent watches a local working tree and pushes file bodies to a
remote OmniCode server's ``/index/upsert-file`` and ``/index/delete-file``
endpoints. Designed for the hybrid-mode story: cloud handles
embedding / search / memory, local stays the source of truth for the
actual code.

Public API:

* :class:`AgentClient` — synchronous HTTP wrapper, stateless, easy to
  unit-test against a fake server.
* :class:`Watcher`     — event-loop wrapper that uses ``watchfiles``
  to detect changes and feeds them into ``AgentClient``.

Usage from the CLI:

    omnicode agent --remote https://omnicode.example.com \\
                   --token sk-... \\
                   --workspace .
"""

from omnicode_adapters.agent.client import AgentClient, AgentResult
from omnicode_adapters.agent.watcher import Watcher, run_agent

__all__ = ["AgentClient", "AgentResult", "Watcher", "run_agent"]
