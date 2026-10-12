"""Platform admin dashboard: a private, server-rendered administration service.

Layers: routes (HTTP only) -> services (validation, rules, audit) -> ``saas`` data access.
No SQL lives here. Nothing in this package refers to any particular business.
"""
