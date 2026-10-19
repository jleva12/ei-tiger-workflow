"""SQLAlchemy models.

Define each model on forge_admin.db.base.AuditBase in a module of this package and
import that module here, so Alembic autogenerate sees every table.
"""

from forge_admin.models.agent_access import (
    AgentCasbinRule,
    AgentGroupLink,
    AgentPermission,
)
from forge_admin.models.api_credentials import ApiCredential, ApiCredentialOAuthFlow
from forge_admin.models.api_keys import ApiKey
from forge_admin.models.authorization import (
    CasbinRule,
    Permission,
    PermissionGroupLink,
    Role,
)
from forge_admin.models.chat_turns import ChatTurnLease, WorkflowStartIntent
from forge_admin.models.code_repositories import CodeIngestionJob, CodeRepository
from forge_admin.models.dashboard_layouts import DashboardLayout, SharedWidget
from forge_admin.models.helpers import HelperJob
from forge_admin.models.hierarchy import Organization
from forge_admin.models.intake_requests import IntakeRequest
from forge_admin.models.knowledge import (
    KnowledgeBase,
    KnowledgeBaseCodeLink,
    KnowledgeBaseComponent,
    KnowledgeBaseConnection,
    KnowledgeBaseGroup,
    KnowledgeBaseRepository,
    KnowledgeCollection,
    KnowledgeDocument,
)
from forge_admin.models.mcp_servers import McpOAuthFlow, McpServer
from forge_admin.models.skills import Skill
from forge_admin.models.users import User

__all__ = [
    "AgentCasbinRule",
    "AgentGroupLink",
    "AgentPermission",
    "ApiCredential",
    "ApiCredentialOAuthFlow",
    "ApiKey",
    "CasbinRule",
    "ChatTurnLease",
    "CodeIngestionJob",
    "CodeRepository",
    "DashboardLayout",
    "HelperJob",
    "IntakeRequest",
    "KnowledgeBase",
    "KnowledgeBaseCodeLink",
    "KnowledgeBaseComponent",
    "KnowledgeBaseConnection",
    "KnowledgeBaseGroup",
    "KnowledgeBaseRepository",
    "KnowledgeCollection",
    "KnowledgeDocument",
    "McpOAuthFlow",
    "McpServer",
    "Organization",
    "Permission",
    "PermissionGroupLink",
    "Role",
    "SharedWidget",
    "Skill",
    "User",
    "WorkflowStartIntent",
]
