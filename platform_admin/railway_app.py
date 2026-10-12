"""Production WSGI entry point for Railway."""

from platform_admin.app import create_app
from platform_admin.config import Settings, verify_database_roles, verify_database_schema

settings = Settings.from_env()
verify_database_roles(settings)
verify_database_schema(settings)

app = create_app(settings)
