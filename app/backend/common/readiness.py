"""Deployment readiness: immutable build revision and applied database schema."""
import re
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from rest_framework.response import Response
from rest_framework.views import APIView


def database_ready():
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        if cursor.fetchone() != (1,):
            return False
    executor = MigrationExecutor(connection)
    return not executor.migration_plan(executor.loader.graph.leaf_nodes())


class Readiness(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        try:
            revision = settings.APP_REVISION
            if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision) or not database_ready():
                raise ValueError('not ready')
            return Response({'status': 'ready', 'revision': revision})
        except Exception:
            # Keep credentials, database addresses and exception messages private.
            return Response({'status': 'unavailable'}, status=503)
