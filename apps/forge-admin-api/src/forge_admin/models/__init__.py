"""SQLAlchemy models.

Define each model on forge_admin.db.base.AuditBase in a module of this package and
import that module here, so Alembic autogenerate sees every table.
"""

from forge_admin.models.api_keys import ApiKey
from forge_admin.models.authorization import (
    CasbinRule,
    Permission,
    PermissionGroupLink,
    Role,
)
from forge_admin.models.code_repositories import CodeIngestionJob, CodeRepository
from forge_admin.models.hierarchy import Organization
from forge_admin.models.knowledge import (
    KnowledgeBase,
    KnowledgeBaseRepository,
    KnowledgeCollection,
    KnowledgeDocument,
)
from forge_admin.models.mcp_servers import McpOAuthFlow, McpServer
from forge_admin.models.users import User

__all__ = [
    "ApiKey",
    "CasbinRule",
    "CodeIngestionJob",
    "CodeRepository",
    "KnowledgeBase",
    "KnowledgeBaseRepository",
    "KnowledgeCollection",
    "KnowledgeDocument",
    "McpOAuthFlow",
    "McpServer",
    "Organization",
    "Permission",
    "PermissionGroupLink",
    "Role",
    "User",
]
