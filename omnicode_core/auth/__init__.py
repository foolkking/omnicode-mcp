"""Multi-user authentication & RBAC for OmniCode-MCP.

Provides a small SQLite-backed user / token store layered on top of the
existing optional API-key middleware. Designed for self-hosted
deployments where a small group of trusted users (e.g. a team) needs
per-person tokens and read-only roles.

Roles:
* ``admin``  — every endpoint, including user management.
* ``editor`` — read + write (search, edit, patch, memory).
* ``viewer`` — read-only (search, read, graph). Mutating endpoints 403.

See ``omnicode_core/auth/users.py`` for the store and
``core/rbac_middleware.py`` for the request gate.
"""

from omnicode_core.auth.users import (
    Role,
    User,
    UserStore,
    get_user_store,
)

__all__ = ["Role", "User", "UserStore", "get_user_store"]
