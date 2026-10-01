"""Applies the Alembic migrations shipped inside this package."""

from pathlib import Path

from alembic import command
from alembic.config import Config

from forge_admin.config import Settings

MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config(settings: Settings) -> Config:
    """Config for running Alembic in-process, e.g. at startup.

    The alembic CLI uses apps/forge-admin-api/alembic.ini instead; both read the
    connection from the forge-admin settings, never from an ini file.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("path_separator", "os")
    config.attributes["settings"] = settings
    return config


def upgrade(settings: Settings, revision: str = "head") -> None:
    command.upgrade(alembic_config(settings), revision)
