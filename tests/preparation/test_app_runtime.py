import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('app_runtime_contract', ROOT/'app/docker/runtime/app_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class AppRuntimeTests(unittest.TestCase):
    def test_protected_secret_file_accepts_weather_venue_mcp_and_playlist_settings(self):
        settings_by_case = {
            'weather_api_key': {'ES_API_KEY': 'Zml4dHVyZTpmaXh0dXJl'},
            'weather_password_with_venue_mcp_and_playlist': {
                'ES_URL': 'https://weather.example.test:9200',
                'WEATHER_INDEX': 'weather-current-v1',
                'ES_USERNAME': 'weather_reader',
                'ES_PASSWORD': 'disposable-unit-fixture',
                'ES_CA_CERT': '/run/mlo/secrets/weather-ca.crt',
                'VENUE_MCP_SERVICE_ENABLED': '1',
                'VENUE_MCP_PUBLIC_CATALOG': '/run/mlo/secrets/published-venues.json',
                'VENUE_WEATHER_BASE_URL': 'https://weather-api.example.test',
                'VENUE_LLM_MODEL': 'fixture-model',
                'GEMINI_ENABLED': '1',
                'GEMINI_API_KEY': 'disposable-unit-fixture',
                'OPENAI_API_KEY': 'disposable-unit-fixture',
                'OPENAI_PLAYLIST_ENABLED': '1',
                'OPENAI_PLAYLIST_TIMEOUT_MS': '20000',
                'OPENAI_PLAYLIST_MAX_CANDIDATES': '150',
                'OPENAI_PLAYLIST_MAX_OUTPUT_TOKENS': '3000',
                'OPENAI_PLAYLIST_MAX_CONCURRENT': '1',
                'OPENAI_PLAYLIST_MIN_INTERVAL_SECONDS': '2',
                'RECOMMENDATION_MAX_QUEUED_GPT': '20',
            },
        }
        for case, settings in settings_by_case.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                path = root / 'app-env.json'
                path.write_text(json.dumps(settings))
                info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=path.stat().st_size)
                with patch.object(Path, 'stat', return_value=info):
                    self.assertEqual(runtime.secret_environment(path, root), settings)

    def test_protected_secret_file_rejects_symlinks_public_permissions_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve()
            path=root/'app-env.json'
            path.write_text(json.dumps({'DJANGO_SECRET_KEY':'disposable-unit-fixture'}))
            path.chmod(0o600)
            self.assertEqual(set(runtime.secret_environment(path,root)), {'DJANGO_SECRET_KEY'})
            path.chmod(0o644)
            with self.assertRaises(ValueError): runtime.secret_environment(path,root)
            path.chmod(0o600)
            link=root/'link.json';link.symlink_to(path)
            with self.assertRaises(ValueError): runtime.secret_environment(link,root)
            path.write_text(json.dumps({'PYTHONPATH':'/untrusted'}))
            with self.assertRaises(ValueError): runtime.secret_environment(path,root)

    def test_image_revision_service_kind_and_fixed_port_are_bound_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'APP_KIND':'web','APP_REVISION':'a'*64}):
            path=Path(folder)/'service.json'
            config={'kind':'web','bind':'10.40.0.10:8000'}
            path.write_text(json.dumps(config))
            parsed,host,port=runtime.configuration(path)
            self.assertIn('backend.cafe.asgi:application',runtime.server_command(parsed,host,port))
            for broken in ({**config,'bind':'10.40.0.10:8001'}, {**config,'kind':'advertising'},
                           {**config,'command':'arbitrary shell'}):
                path.write_text(json.dumps(broken))
                with self.assertRaises(ValueError): runtime.configuration(path)
