"""``forge-admin``: the API process (also ``python -m forge_admin``).

Takes no arguments; everything is configured through the environment (see
forge_admin.config).
"""

from pathlib import Path

import uvicorn
from forge_common.logging import configure_logging, get_logger

from forge_admin.config import get_settings
from forge_admin.db.migrate import upgrade

log = get_logger(__name__)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.logging)
    if settings.migrate_on_start:
        log.info("applying database migrations")
        upgrade(settings)
    uvicorn.run(
        "forge_admin.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        # Watch the forge_admin package only, not .venv or the tests, and its
        # workflow schema and step catalog (*.json) as well as its code.
        reload_dirs=[str(Path(__file__).resolve().parents[1])],
        reload_includes=["*.json"] if settings.reload else None,
        # Logging is owned by configure_logging() (create_app runs it again in
        # the reloader's server process); access lines come from
        # RequestContextMiddleware.
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
