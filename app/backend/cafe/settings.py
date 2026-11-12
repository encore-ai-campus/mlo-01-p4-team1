"""C integration host. SQLite and synthetic inputs require explicit local demo mode."""
import os
from ipaddress import IPv4Network
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parents[2]
from pipeline.environment import load as load_service_env
load_service_env('common')
LOCAL_DEMO = os.environ.get('C_LOCAL_DEMO') == '1'
INTEGRATION_ENABLED = os.environ.get('INTEGRATION_ENABLED', '0' if LOCAL_DEMO else '1') == '1'
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'local-demo-only' if LOCAL_DEMO else '')
if not SECRET_KEY:
    raise ImproperlyConfigured('DJANGO_SECRET_KEY is required')
DEBUG = LOCAL_DEMO
ALLOWED_HOSTS = [value.strip() for value in os.environ.get('DJANGO_ALLOWED_HOSTS','127.0.0.1,localhost,testserver').split(',') if value.strip()]
CSRF_TRUSTED_ORIGINS = [value.strip() for value in os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS','').split(',') if value.strip()]
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'rest_framework',
                  'backend.recommendation', 'backend.music', 'backend.common', 'backend.accounts', 'backend.workspaces', 'backend.venues',
                  'backend.phase4_ops.apps.Phase4OperationsConfig']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware',
              'django.contrib.sessions.middleware.SessionMiddleware',
              'backend.common.identity.GuestCookieMiddleware',
              'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware']
PHASE4_TRUSTED_PROXY_CIDRS = ()
if os.environ.get('PHASE4_TRUSTED_PROXY_CIDRS'):
    try:
        private_networks = tuple(map(IPv4Network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
        PHASE4_TRUSTED_PROXY_CIDRS = tuple(
            IPv4Network(value.strip()) for value in os.environ['PHASE4_TRUSTED_PROXY_CIDRS'].split(',')
        )
        if LOCAL_DEMO or not all(
            any(network.subnet_of(private) and network != private for private in private_networks)
            for network in PHASE4_TRUSTED_PROXY_CIDRS
        ):
            raise ValueError
    except ValueError as exc:
        raise ImproperlyConfigured('PHASE4_TRUSTED_PROXY_CIDRS requires specific private IPv4 ingress subnets') from exc
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_MLO_TRUSTED_SCHEME', 'https')
    MIDDLEWARE.insert(0, 'backend.cafe.ingress.TrustedCloudFrontScheme')
ROOT_URLCONF = 'backend.cafe.urls'
APP_REVISION = os.environ.get('APP_REVISION', '')
FRONTEND_DIST = BASE_DIR/'frontend/dist'
FRONTEND_REQUIRED = os.environ.get('FRONTEND_REQUIRED') == '1'
CSRF_FAILURE_VIEW = 'backend.recommendation.api.csrf_failure'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': os.environ.get('C_LOCAL_DATABASE') or BASE_DIR / 'local-demo.sqlite3'}} if LOCAL_DEMO else {
    'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': os.environ.get('PGDATABASE', ''),
                'USER': os.environ.get('PGUSER', ''), 'PASSWORD': os.environ.get('PGPASSWORD', ''),
                'HOST': os.environ.get('PGHOST', ''), 'PORT': os.environ.get('PGPORT', '5432')}}
USE_TZ = True
if not LOCAL_DEMO:
    DATABASES['default']['OPTIONS']={'connect_timeout':int(os.environ.get('PGCONNECT_TIMEOUT','5')),**{key:os.environ[name] for key,name in
        [('sslmode','PGSSLMODE'),('sslrootcert','PGSSLROOTCERT')] if os.environ.get(name)}}
TIME_ZONE = 'UTC'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not LOCAL_DEMO
CSRF_COOKIE_SECURE = not LOCAL_DEMO
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
REST_FRAMEWORK = {'UNAUTHENTICATED_USER': None, 'DEFAULT_AUTHENTICATION_CLASSES': [],
                  'EXCEPTION_HANDLER': 'backend.recommendation.api.exception_handler'}
# A supplies an importable callable: (region_id, as_of) -> normalized bundle.
WEATHER_PROVIDER = os.environ.get('WEATHER_PROVIDER', 'backend.weather.adapter.obtain' if INTEGRATION_ENABLED else 'backend.weather.live.obtain')
KMA_LOCAL_CACHE_PATH = os.environ.get('KMA_LOCAL_CACHE_PATH', str(BASE_DIR / '.local/weather-live/latest.json'))
# B supplies ownership resolver (request -> opaque owner), event sink and shared guest bootstrap.
OWNER_RESOLVER = os.environ.get('OWNER_RESOLVER', 'backend.common.identity.owner' if INTEGRATION_ENABLED else '')
EVENT_SINK = os.environ.get('EVENT_SINK', 'backend.common.events.record' if INTEGRATION_ENABLED else '')
PASSWORD_HASHERS = ['backend.common.hashers.ServiceArgon2Hasher', 'django.contrib.auth.hashers.PBKDF2PasswordHasher']
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
]
# Member/SMTP configuration. Production has no fabricated policy, key or operator defaults.
import json
MEMBER_POLICY_VERSION = os.environ.get('MEMBER_POLICY_VERSION', 'local-preview-v1' if LOCAL_DEMO else '')
MEMBER_LOCAL_PREVIEW = LOCAL_DEMO
MEMBER_POLICIES_READY = os.environ.get('MEMBER_POLICIES_READY') == '1'
MEMBER_PUBLIC_SIGNUP_ENABLED = os.environ.get('MEMBER_PUBLIC_SIGNUP_ENABLED') == '1'
MEMBER_POLICY_DOCUMENTS = json.loads(os.environ.get('MEMBER_POLICY_DOCUMENTS', '{}'))
if LOCAL_DEMO and not MEMBER_POLICY_DOCUMENTS:
    MEMBER_POLICY_DOCUMENTS = {
        'terms': '로컬 검증용 이용약관입니다. 실제 서비스 약관은 운영 전에 게시합니다.',
        'privacy_notice': '로컬 검증용 안내: 아이디·비밀번호 해시·별명·카페 유형을 계정 운영에 사용하며 탈퇴 시 정리합니다. 안내 확인은 개인정보 수집 동의와 구분합니다.',
        'email': '선택한 이메일은 암호화 저장하며 복구 주소 확인·아이디 안내·비밀번호 재설정에만 사용합니다. 등록 철회 시 정리하며, 미동의 시 이메일 복구만 제한됩니다. 로컬 검증용 문구입니다.',
    }
MEMBER_EMAIL_KEYS = json.loads(os.environ.get('MEMBER_EMAIL_KEYS', '{}'))
MEMBER_ACTIVE_EMAIL_KEY = os.environ.get('MEMBER_ACTIVE_EMAIL_KEY', '')
MEMBER_LOOKUP_KEY = os.environ.get('MEMBER_LOOKUP_KEY', '')
MEMBER_MAIL_LIMIT_HOUR = int(os.environ.get('MEMBER_MAIL_LIMIT_HOUR', '10' if LOCAL_DEMO else '0'))
MEMBER_RECOVERY_IP_LIMIT_HOUR = int(os.environ.get('MEMBER_RECOVERY_IP_LIMIT_HOUR', '30' if LOCAL_DEMO else '0'))
MEMBER_LOGIN_LIMIT_HOUR = int(os.environ.get('MEMBER_LOGIN_LIMIT_HOUR', '30' if LOCAL_DEMO else '0'))
MEMBER_PUBLIC_LIMIT_HOUR = int(os.environ.get('MEMBER_PUBLIC_LIMIT_HOUR', '100' if LOCAL_DEMO else '0'))
MEMBER_SECURITY_MAIL_SECONDS = int(os.environ.get('MEMBER_SECURITY_MAIL_SECONDS', '3600' if LOCAL_DEMO else '0'))
MEMBER_PUBLIC_ORIGIN = os.environ.get('MEMBER_PUBLIC_ORIGIN', 'http://localhost:5173' if LOCAL_DEMO else '')
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', '1') == '1'
EMAIL_USE_SSL = os.environ.get('EMAIL_USE_SSL') == '1'
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', '')
ADMIN_PASSKEY_REQUIRED = not LOCAL_DEMO or os.environ.get('ADMIN_PASSKEY_REQUIRED') == '1'
ADMIN_PASSKEY_RP_ID = os.environ.get('ADMIN_PASSKEY_RP_ID', 'localhost' if LOCAL_DEMO else '')
ADMIN_PASSKEY_ORIGIN = os.environ.get('ADMIN_PASSKEY_ORIGIN', 'http://localhost:5173' if LOCAL_DEMO else '')
PLATFORM_ENABLED = os.environ.get('PLATFORM_LOCAL_MVP') == '1'
VENUE_MCP_SERVICE_ENABLED = os.environ.get('VENUE_MCP_SERVICE_ENABLED') == '1'
PHASE4_OWNER_PORTAL_ENABLED = os.environ.get('PHASE4_OWNER_PORTAL_ENABLED') == '1'
PHASE4_PRIVATE_DEMO_ENABLED = os.environ.get('PHASE4_PRIVATE_DEMO_ENABLED') == '1'
PHASE4_PRIVATE_DEMO_POLICY_VERSION = os.environ.get('PHASE4_PRIVATE_DEMO_POLICY_VERSION', '')
PHASE4_PRIVATE_DEMO_POLICY_DOCUMENTS = json.loads(os.environ.get('PHASE4_PRIVATE_DEMO_POLICY_DOCUMENTS', '{}'))
PHASE4_SONG_POLICY = {
    'version': 'owner-song-requests-v1', 'allow_general_users': False, 'require_approved_venue': True,
    'quota_scope': 'account', 'window_days': None, 'period_limit': None, 'count_all_submitted': True,
}
PHASE4_WORKER_TOKEN = os.environ.get('PHASE4_WORKER_TOKEN', '')
PHASE4_ALLOW_INSECURE_WORKER_LOCAL = LOCAL_DEMO
PHASE4_LEASE_SECONDS = int(os.environ.get('PHASE4_LEASE_SECONDS', '900'))
PHASE4_TAG_CODES = {
    'genre': frozenset({'POP', 'K-POP', 'J-POP', 'ROCK_PUNK_METAL', 'ELECTRONIC_DANCE', 'HIPHOP',
                        'RNB_SOUL_FUNK', 'JAZZ', 'FOLK_COUNTRY_BLUES', 'OPERA', 'CLASSICAL', 'NEWAGE',
                        'REGGAE_SKA', 'LATIN'}),
    'mood': frozenset({'CALM', 'EMOTIONAL', 'PEACEFUL', 'DREAMY', 'ENERGETIC', 'BRIGHT', 'PARTY', 'GROOVY', 'INTENSE'}),
}
GUEST_COOKIE_NAME = 'cafe_guest'
GUEST_TTL_SECONDS = int(os.environ.get('GUEST_TTL_SECONDS', '0'))
ADMIN_SESSION_SECONDS = int(os.environ.get('ADMIN_SESSION_SECONDS', '0'))
ADMIN_SSO_ENABLED = os.environ.get('ADMIN_SSO_ENABLED') == '1'
ADMIN_LOCAL_LOGIN_ENABLED = os.environ.get('ADMIN_LOCAL_LOGIN_ENABLED', '0' if ADMIN_SSO_ENABLED else '1') == '1'
ADMIN_SSO_ISSUER = os.environ.get('ADMIN_SSO_ISSUER', '').rstrip('/')
ADMIN_SSO_CLIENT_ID = os.environ.get('ADMIN_SSO_CLIENT_ID', '')
ADMIN_SSO_CLIENT_SECRET = os.environ.get('ADMIN_SSO_CLIENT_SECRET', '')
ADMIN_SSO_REDIRECT_URI = os.environ.get('ADMIN_SSO_REDIRECT_URI', '')
ADMIN_SSO_POST_LOGOUT_URI = os.environ.get('ADMIN_SSO_POST_LOGOUT_URI', '')
ADMIN_SSO_GROUPS_CLAIM = os.environ.get('ADMIN_SSO_GROUPS_CLAIM', 'groups')
ADMIN_SSO_KEYCLOAK_ADMIN_URL = os.environ.get('ADMIN_SSO_KEYCLOAK_ADMIN_URL', '').rstrip('/')
ADMIN_KIBANA_URL = os.environ.get('ADMIN_KIBANA_URL', '')
ADMIN_OPERATIONS_DASHBOARD_URL = os.environ.get('ADMIN_OPERATIONS_DASHBOARD_URL', '')
ADMIN_LOG_DASHBOARD_URL = os.environ.get('ADMIN_LOG_DASHBOARD_URL', '')
# In native-login mode, dashboard redirects must stay on explicitly approved Kibana hosts.
ADMIN_KIBANA_ALLOWED_HOSTS = tuple(part.strip().lower() for part in
    os.environ.get('ADMIN_KIBANA_ALLOWED_HOSTS', '').split(',') if part.strip())
ADMIN_SSO_ELASTICSEARCH_URL = os.environ.get('ADMIN_SSO_ELASTICSEARCH_URL', '')
ADS_INTERNAL_SECRET = os.environ.get('ADS_INTERNAL_SECRET', '')
FEEDBACK_ENVELOPE_VERSION = 'phase3-20261014.1'
API_CONTRACT_VERSION = 'phase3-20261016.3'
CSV_UPLOAD_MAX_BYTES = int(os.environ.get('CSV_UPLOAD_MAX_BYTES','0'))
MUSIC_RAW_DIRECTORY = os.environ.get('MUSIC_RAW_DIRECTORY','')
MUSIC_S3_BUCKET = os.environ.get('MUSIC_S3_BUCKET','')
MUSIC_S3_REGION = os.environ.get('MUSIC_S3_REGION','')
MUSIC_S3_PREFIX = os.environ.get('MUSIC_S3_PREFIX','')
MUSIC_S3_ARCHIVE_DEFERRED = os.environ.get('MUSIC_S3_ARCHIVE_DEFERRED') == '1'
SEARCH_MAX_CANDIDATES = int(os.environ.get('SEARCH_MAX_CANDIDATES', '200' if LOCAL_DEMO else '0'))
SEARCH_SECONDS = float(os.environ.get('SEARCH_SECONDS', '10' if LOCAL_DEMO else '0'))
RECOMMENDATION_WORKER_IDLE_SECONDS = float(os.environ.get('RECOMMENDATION_WORKER_IDLE_SECONDS', '0.25' if LOCAL_DEMO else '1'))
RECOMMENDATION_QUEUE = os.environ.get('RECOMMENDATION_QUEUE', 'all')
RECOMMENDATION_WORKER_ID = os.environ.get('RECOMMENDATION_WORKER_ID', '')
RECOMMENDATION_LEASE_SECONDS = int(os.environ.get('RECOMMENDATION_LEASE_SECONDS', '30'))
RECOMMENDATION_HEARTBEAT_SECONDS = float(os.environ.get('RECOMMENDATION_HEARTBEAT_SECONDS', '10'))
RECOMMENDATION_JOB_TIMEOUT_SECONDS = int(os.environ.get('RECOMMENDATION_JOB_TIMEOUT_SECONDS', '180'))
RECOMMENDATION_MAX_RECOVERIES = int(os.environ.get('RECOMMENDATION_MAX_RECOVERIES', '2'))
RECOMMENDATION_MAX_QUEUED_RULE = int(os.environ.get('RECOMMENDATION_MAX_QUEUED_RULE', '100'))
RECOMMENDATION_MAX_QUEUED_GEMINI = int(os.environ.get('RECOMMENDATION_MAX_QUEUED_GEMINI', '20'))
RECOMMENDATION_MAX_QUEUED_GPT = int(os.environ.get('RECOMMENDATION_MAX_QUEUED_GPT', '20'))
OPENAI_PLAYLIST_ENABLED = os.environ.get('OPENAI_PLAYLIST_ENABLED', '0' if LOCAL_DEMO else '1') == '1'
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '').strip()
OPENAI_PLAYLIST_TIMEOUT_MS = int(os.environ.get('OPENAI_PLAYLIST_TIMEOUT_MS', '45000'))
OPENAI_PLAYLIST_MAX_CANDIDATES = int(os.environ.get('OPENAI_PLAYLIST_MAX_CANDIDATES', '150'))
OPENAI_PLAYLIST_MAX_OUTPUT_TOKENS = int(os.environ.get('OPENAI_PLAYLIST_MAX_OUTPUT_TOKENS', '8192'))
OPENAI_PLAYLIST_MAX_CONCURRENT = int(os.environ.get('OPENAI_PLAYLIST_MAX_CONCURRENT', '1'))
OPENAI_PLAYLIST_MIN_INTERVAL_SECONDS = float(os.environ.get('OPENAI_PLAYLIST_MIN_INTERVAL_SECONDS', '2'))
GEMINI_MAX_CONCURRENT = int(os.environ.get('GEMINI_MAX_CONCURRENT', '1'))
GEMINI_MIN_INTERVAL_SECONDS = float(os.environ.get('GEMINI_MIN_INTERVAL_SECONDS', '2'))
GEMINI_ENABLED = os.environ.get('GEMINI_ENABLED') == '1'
HOURLY_RECOMMENDATION_ENABLED = LOCAL_DEMO and os.environ.get('HOURLY_RECOMMENDATION_ENABLED') == '1'
HOURLY_RECOMMENDATION_BUDGET_SECONDS = 10
HOURLY_RECOMMENDATION_CANDIDATE_LIMIT = 240
HOURLY_GPT_TIMEOUT_SECONDS = 9
HOURLY_GPT_MAX_CONCURRENT = 2
GEMINI_TIMEOUT_MS = int(os.environ.get('GEMINI_TIMEOUT_MS', '0'))
GEMINI_MAX_CANDIDATES = int(os.environ.get('GEMINI_MAX_CANDIDATES', '0'))
# Known-video playback is keyless by default. Official search is an explicit option.
YOUTUBE_REGISTRY_BACKEND = os.environ.get('YOUTUBE_REGISTRY_BACKEND', 'local' if LOCAL_DEMO else 'disabled')
if YOUTUBE_REGISTRY_BACKEND not in {'local', 'database', 'disabled'}:
    raise ImproperlyConfigured('YOUTUBE_REGISTRY_BACKEND must be local, database or disabled')
YOUTUBE_PLAYBACK_MODE = os.environ.get('YOUTUBE_PLAYBACK_MODE', 'KEYLESS')
if YOUTUBE_PLAYBACK_MODE not in {'KEYLESS', 'OPTIONAL_SEARCH'}:
    raise ImproperlyConfigured('YOUTUBE_PLAYBACK_MODE must be KEYLESS or OPTIONAL_SEARCH')
YOUTUBE_DATA_API_KEY = (os.environ.get('YOUTUBE_DATA_API_KEY') or os.environ.get('YOUTUBE_API_KEY') or '').strip()
YOUTUBE_SEARCH_TIMEOUT_SECONDS = float(os.environ.get('YOUTUBE_SEARCH_TIMEOUT_SECONDS') or '8')
YOUTUBE_SEARCH_PER_OWNER_MINUTE = int(os.environ.get('YOUTUBE_SEARCH_PER_OWNER_MINUTE') or '6')
YOUTUBE_SEARCH_DAILY_LIMIT = int(os.environ.get('YOUTUBE_SEARCH_DAILY_LIMIT') or '80')
YOUTUBE_SEARCH_CACHE_SECONDS = int(os.environ.get('YOUTUBE_SEARCH_CACHE_SECONDS') or '21600')
LOGGING = {
    'version': 1, 'disable_existing_loggers': False,
    'handlers': {'recommendation_console': {'class': 'logging.StreamHandler'}},
    'loggers': {
        'recommendation.worker': {
            'handlers': ['recommendation_console'], 'level': 'INFO', 'propagate': False},
        'backend.venues': {
            'handlers': ['recommendation_console'], 'level': 'INFO', 'propagate': False},
        'backend.workspaces': {
            'handlers': ['recommendation_console'], 'level': 'INFO', 'propagate': False},
    },
}
