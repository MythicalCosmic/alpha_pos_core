"""Shared Django configuration for Alpha POS editions.

``settings_base.py`` (added in Phase 0) holds everything common — apps shared by both
editions, middleware (incl. the position-asserted licensing kill-switch), DRF, sync
settings, Postgres defaults. Each edition's settings module imports * from here and
appends its edition-specific apps, URLs, channel layer and DB profile.
"""
