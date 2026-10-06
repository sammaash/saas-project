"""Development entry point: ``python -m platform_admin`` (binds to localhost only)."""

from __future__ import annotations

from .app import create_app

if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5055, debug=False)
