"""Fixed app entrypoints and protected secret-file ingestion; no shell commands."""
import ipaddress
import json
import os
from pathlib import Path
import re
import stat

PORTS = {'web': 8000, 'advertising': 8001}
SECRET_KEYS = {
    'DJANGO_SECRET_KEY', 'DJANGO_ALLOWED_HOSTS', 'DJANGO_CSRF_TRUSTED_ORIGINS',
    'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSSLMODE', 'PGSSLROOTCERT',
    'POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD',
    'POSTGRES_SSLMODE', 'POSTGRES_SSLROOTCERT',
    'ES_URL', 'WEATHER_INDEX', 'ES_USERNAME', 'ES_PASSWORD', 'ES_API_KEY', 'ES_CA_CERT',
    'VENUE_MCP_SERVICE_ENABLED', 'VENUE_MCP_PUBLIC_CATALOG', 'VENUE_WEATHER_BASE_URL',
    'VENUE_LLM_MODEL', 'GEMINI_ENABLED', 'GEMINI_API_KEY',
    'ADS_INTERNAL_SECRET', 'ADS_MONGODB_URI',
    'ADS_MONGODB_DATABASE', 'ADS_MONGODB_REPLICA_SET', 'ADS_COMMON_API_URL',
    'ADS_DELIVERY_SIGNING_KEY', 'ADMIN_SSO_ISSUER', 'ADMIN_SSO_CLIENT_ID',
    'ADMIN_SSO_CLIENT_SECRET', 'ADMIN_SSO_REDIRECT_URI', 'ADMIN_SSO_POST_LOGOUT_URI',
    'MEMBER_POLICY_VERSION', 'MEMBER_POLICIES_READY', 'MEMBER_POLICY_DOCUMENTS',
    'MEMBER_EMAIL_KEYS', 'MEMBER_ACTIVE_EMAIL_KEY', 'MEMBER_LOOKUP_KEY', 'MEMBER_MAIL_LIMIT_HOUR',
    'MEMBER_RECOVERY_IP_LIMIT_HOUR', 'MEMBER_LOGIN_LIMIT_HOUR', 'MEMBER_PUBLIC_LIMIT_HOUR', 'MEMBER_SECURITY_MAIL_SECONDS', 'MEMBER_PUBLIC_ORIGIN',
    'EMAIL_HOST', 'EMAIL_PORT', 'EMAIL_HOST_USER', 'EMAIL_HOST_PASSWORD', 'EMAIL_USE_TLS', 'EMAIL_USE_SSL',
    'DEFAULT_FROM_EMAIL', 'ADMIN_PASSKEY_RP_ID', 'ADMIN_PASSKEY_ORIGIN',
    'ADMIN_SSO_ENABLED', 'ADMIN_LOCAL_LOGIN_ENABLED', 'ADMIN_SSO_GROUPS_CLAIM',
    'ADMIN_SESSION_SECONDS', 'GUEST_TTL_SECONDS',
    'GEMINI_MODEL',
    'PLATFORM_LOCAL_MVP', 'MLO_CLOUD_ACCOUNT', 'WEATHER_PROVIDER',
    'ADMIN_KIBANA_URL', 'ADMIN_OPERATIONS_DASHBOARD_URL', 'ADMIN_LOG_DASHBOARD_URL', 'ADMIN_KIBANA_ALLOWED_HOSTS',
    'VENUE_DATA_DIR', 'VENUE_RAW_S3_BUCKET', 'VENUE_LAKE_S3_BUCKET', 'VENUE_DATASET_VERSION',
    'VENUE_KAFKA_BOOTSTRAP', 'VENUE_KAFKA_TOPIC',
    'VENUE_OUTBOX_KAFKA_BOOTSTRAP', 'VENUE_OUTBOX_KAFKA_TOPIC', 'VENUE_OBSERVATION_SPOOL_PATH',
    'PHASE4_WORKER_TOKEN', 'PHASE4_TRUSTED_PROXY_CIDRS', 'PHASE4_OWNER_PORTAL_ENABLED',
    'PHASE4_PRIVATE_DEMO_ENABLED', 'PHASE4_PRIVATE_DEMO_POLICY_VERSION', 'PHASE4_PRIVATE_DEMO_POLICY_DOCUMENTS',
}


def secret_environment(path, root=Path('/run/mlo/secrets')):
    path = Path(path)
    if not path.is_absolute() or not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError('protected regular secret file required')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('secret file must be owner-only')
    if info.st_size > 65536:
        raise ValueError('secret file too large')
    values = json.loads(path.read_text())
    if not isinstance(values, dict) or not set(values) <= SECRET_KEYS:
        raise ValueError('unsupported secret environment keys')
    if not all(isinstance(v, str) and '\0' not in v for v in values.values()):
        raise ValueError('invalid secret value')
    return values


def configuration(path='/etc/mlo/service.json'):
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict) or set(config) - {'kind', 'bind', 'secret_env'}:
        raise ValueError('unknown app configuration')
    kind = config['kind']
    if kind not in PORTS or kind != os.environ.get('APP_KIND'):
        raise ValueError('image and service kind mismatch')
    host, port = config['bind'].rsplit(':', 1)
    ipaddress.IPv4Address(host)
    if port != str(PORTS[kind]):
        raise ValueError('service port differs from shared contract')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', os.environ.get('APP_REVISION', '')):
        raise ValueError('immutable image revision required')
    return config, host, port


def server_command(config, host, port):
    if config['kind'] == 'web':
        return ['-m', 'uvicorn', 'backend.cafe.asgi:application', '--host', host,
                '--port', port, '--workers', '1', '--timeout-graceful-shutdown', '210',
                *(['--no-proxy-headers'] if os.environ.get('PHASE4_TRUSTED_PROXY_CIDRS') else []),
                '--no-access-log']
    return ['-m', 'gunicorn', 'config.wsgi:application', '--bind', host+':'+port,
            '--workers', '2', '--timeout', '60', '--graceful-timeout', '210']
