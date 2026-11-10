import os
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE','backend.cafe.settings')


def build_application():
    django_application = get_asgi_application()
    # Venue services import models, so Django must finish setup first.
    from backend.venues.services.mcp_stdio import with_local_venue_mcp

    return with_local_venue_mcp(django_application)


application = build_application()
