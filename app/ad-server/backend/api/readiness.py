"""Read-only PostgreSQL schema and Mongo replica-set/validator readiness."""
import re
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from pymongo import MongoClient
from rest_framework.response import Response
from rest_framework.views import APIView
from workers.ads_runtime_check import schema_issues


def databases_ready():
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        if cursor.fetchone() != (1,):
            return False
    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        return False
    if not settings.ADS_MONGODB_URI or not settings.ADS_MONGODB_DATABASE:
        return False
    with MongoClient(settings.ADS_MONGODB_URI, serverSelectionTimeoutMS=2000,
                     connectTimeoutMS=2000, socketTimeoutMS=2000, tz_aware=True) as client:
        hello = client.admin.command('hello')
        return (hello.get('setName') == settings.ADS_MONGODB_REPLICA_SET
                and hello.get('isWritablePrimary') is True
                and not schema_issues(client[settings.ADS_MONGODB_DATABASE]))


class ReadinessView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        try:
            revision = settings.APP_REVISION
            if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision) or not databases_ready():
                raise ValueError('not ready')
            return Response({'status':'ready', 'revision':revision})
        except Exception:
            return Response({'status':'unavailable'}, status=503)
