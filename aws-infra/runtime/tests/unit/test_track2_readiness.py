"""Keep protocol failures visible instead of masking them as slow startup."""
import ast
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from mlo_runtime.operations import UnsafeTransition

INTEGRATION = Path(__file__).resolve().parents[1] / "integration"
spec = importlib.util.spec_from_file_location("track2_readiness_under_test", INTEGRATION / "track2_lab.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class Track2Readiness(unittest.TestCase):
    def test_invalid_request_and_auth_fail_immediately(self):
        for code in (400, 401, 403, 405):
            with self.subTest(code=code), patch.object(lab.time, "sleep") as sleep:
                cause = UnsafeTransition("ES request rejected: HTTP " + str(code))
                predicate = Mock(side_effect=cause)
                with self.assertRaisesRegex(AssertionError, "not a transient") as raised:
                    lab.wait_for(predicate)
                self.assertIs(raised.exception.__cause__, cause)
                self.assertEqual(predicate.call_count, 1)
                sleep.assert_not_called()

    def test_transient_startup_error_can_recover(self):
        predicate = Mock(side_effect=[UnsafeTransition("ES request rejected: HTTP 503"), {"ready": True}])
        with patch.object(lab.time, "sleep"):
            self.assertEqual(lab.wait_for(predicate), {"ready": True})
        self.assertEqual(predicate.call_count, 2)

    def test_deadline_preserves_original_failure(self):
        cause = ConnectionError("fixture connection unavailable")
        predicate = Mock(side_effect=cause)
        with patch.object(lab.time, "monotonic", side_effect=[0, 0, 151]), patch.object(lab.time, "sleep"):
            with self.assertRaisesRegex(AssertionError, "deadline exceeded") as raised:
                lab.wait_for(predicate)
        self.assertIs(raised.exception.__cause__, cause)

    def test_restart_resolves_the_current_ephemeral_port(self):
        fake = Mock()
        fake.folder = Path("/synthetic-local-lab")
        ports = iter([b"127.0.0.1:49001\n", b"127.0.0.1:49002\n"])
        def compose(*args, **kwargs):
            return Mock(stdout=next(ports)) if args[0] == "port" else Mock()
        fake.compose.side_effect = compose
        with patch.object(lab, "Elastic") as client:
            lab.start_es(fake)
            lab.start_es(fake)
        self.assertEqual([call.args[0] for call in client.call_args_list],
                         ["https://127.0.0.1:49001", "https://127.0.0.1:49002"])
        self.assertEqual(fake.ready.call_count, 2)
        source = (INTEGRATION / "test_observability.py").read_text()
        self.assertNotIn('lab.compose("start", "elasticsearch")', source)
        self.assertEqual(source.count("es = start_es(lab)"), 2)

    def test_observation_refresh_has_no_request_body(self):
        tree = ast.parse((INTEGRATION / "test_observability.py").read_text())
        refreshes = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and n.func.attr == "request"
                     and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
                     and isinstance(n.args[1].value, str) and n.args[1].value.endswith("/_refresh")]
        self.assertEqual(len(refreshes), 1)
        for call in refreshes:
            self.assertEqual(len(call.args), 2)
            self.assertFalse(any(k.arg == "body" for k in call.keywords))


class ScopedServiceReadiness(unittest.TestCase):
    def make_lab(self, target_state):
        import json
        from mlo_runtime.lab import Lab
        instance = Lab.__new__(Lab)
        instance.folder = Path("/synthetic-local-lab")
        instance.service = "elasticsearch"
        states = [
            {"Service": "elasticsearch", "State": target_state},
            {"Service": "logstash", "State": "exited"},
        ]
        def compose(*args, **kwargs):
            rows = states if args[-1] != instance.service else states[:1]
            payload = "\n".join(json.dumps(r, separators=(",", ":")) for r in rows)
            return Mock(stdout=payload.encode())
        instance.compose = Mock(side_effect=compose)
        return instance

    def test_stopped_peer_is_not_a_failed_elasticsearch_start(self):
        instance = self.make_lab("running")
        instance.exec = Mock(side_effect=[Mock(returncode=1), Mock(returncode=0)])
        with patch("mlo_runtime.lab.time.sleep"):
            instance.ready(["curl", "https://localhost:9200"], attempts=2)
        instance.compose.assert_called_once_with(
            "ps", "--all", "--format", "json", "elasticsearch", check=False
        )
        self.assertEqual(instance.exec.call_count, 2)

    def test_the_selected_service_exit_still_fails(self):
        instance = self.make_lab("exited")
        instance.exec = Mock(return_value=Mock(returncode=1))
        with patch("mlo_runtime.lab.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "service exited"):
                instance.ready(["curl", "https://localhost:9200"], attempts=2)
        self.assertEqual(instance.exec.call_count, 1)
        sleep.assert_not_called()
