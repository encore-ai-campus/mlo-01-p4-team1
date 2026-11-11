import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('app_runtime_contract', ROOT/'app/docker/runtime/app_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class AppRuntimeTests(unittest.TestCase):
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
