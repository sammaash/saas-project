"""Production WSGI entry point for Railway."""

from platform_admin.app import create_app
from platform_admin.config import Settings, verify_database_roles

settings = Settings.from_env()
verify_database_roles(settings)

app = create_app(settings)
