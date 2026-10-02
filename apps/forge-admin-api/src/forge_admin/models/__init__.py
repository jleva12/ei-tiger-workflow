"""SQLAlchemy models.

Define each model on forge_admin.db.base.AuditBase in a module of this package and
import that module here, so Alembic autogenerate sees every table.
"""

from forge_admin.models.authorization import CasbinRule, Permission, Role
from forge_admin.models.hierarchy import Organization
from forge_admin.models.users import User

__all__ = [
    "CasbinRule",
    "Organization",
    "Permission",
    "Role",
    "User",
]
