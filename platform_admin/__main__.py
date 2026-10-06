"""Entry point: ``python -m platform_admin`` (binds to localhost only).

Startup validates the environment and proves both database logins are the expected
unprivileged roles; any failure stops the process. Production should run the app factory
behind a real WSGI server and reverse proxy rather than this development runner.
"""

from __future__ import annotations

from .app import create_app
from .config import Settings, verify_database_roles

if __name__ == "__main__":
    settings = Settings.from_env()
    verify_database_roles(settings)
    create_app(settings).run(host="127.0.0.1", port=5055, debug=False)
