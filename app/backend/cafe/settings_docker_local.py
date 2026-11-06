"""HTTP-only Docker development settings. Never use for staging or AWS."""
import os

from django.core.exceptions import ImproperlyConfigured
from .settings import *  # noqa: F403,F401

if os.environ.get("MLO_LOCAL_DOCKER") != "1":
    raise ImproperlyConfigured(
        "settings_docker_local is restricted to explicitly enabled local Docker."
    )
if LOCAL_DEMO:  # noqa: F405
    raise ImproperlyConfigured(
        "Docker development must use its isolated PostgreSQL, not the SQLite demo DB."
    )

DEBUG = True
# The Compose services are bound to 127.0.0.1 on the host, never an external interface.
# This override is not imported by the AWS/production DJANGO_SETTINGS_MODULE.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
