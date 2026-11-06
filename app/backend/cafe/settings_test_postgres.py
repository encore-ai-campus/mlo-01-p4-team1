"""Existing local-demo regression semantics on an explicitly disposable PostgreSQL DB."""
from .settings import *  # noqa: F403,F401

if (not LOCAL_DEMO or os.environ.get('MLO_DISPOSABLE_MEMBER_TEST') != '1'
        or os.environ.get('PGDATABASE') != 'member_validation'):
    raise ImproperlyConfigured('disposable member_validation PostgreSQL test settings only')

DATABASES = {'default': {'ENGINE':'django.db.backends.postgresql','NAME':'member_validation',
    'USER':os.environ['PGUSER'],'PASSWORD':os.environ['PGPASSWORD'],
    'HOST':os.environ['PGHOST'],'PORT':os.environ.get('PGPORT','5432')}}
INTEGRATION_ENABLED = False
OWNER_RESOLVER = ''
EVENT_SINK = ''
WEATHER_PROVIDER = 'backend.weather.live.obtain'
