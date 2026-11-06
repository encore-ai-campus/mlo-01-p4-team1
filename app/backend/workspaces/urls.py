import re
from django.urls import path
from .api import PlatformEndpoint, openapi
from .schema import CONTRACT

urlpatterns=[path('api/platform/v1/openapi.json',openapi)]
for route,operations in CONTRACT['paths'].items():
    url='api/platform/v1'+re.sub(r'\{([^}]+)\}',r'<uuid:\1>',route)
    urlpatterns.append(path(url,PlatformEndpoint.as_view(operations=operations)))
