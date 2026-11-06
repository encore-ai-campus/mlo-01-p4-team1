import json
from pathlib import Path
import tempfile
import time
import unittest

from mlo_runtime.home_api import FakeHome
from mlo_runtime.operations import Request, UnsafeTransition
from mlo_runtime.relay import envelope, relay_handler


class RelayUnitTests(unittest.TestCase):
    def test_home_persistent_dedup_and_body_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "home.sqlite"
            raw = Request("one", time.time() + 10, "r", ("x",), {}).body()
            home = FakeHome(path)
            home.submit(raw)
            home.close()
            restarted = FakeHome(path)
            self.assertEqual(restarted.submit(raw)[1]["submissions"], 1)
            altered = json.loads(raw)
            altered["payload"] = {"prompt": "different"}
            self.assertEqual(restarted.submit(json.dumps(altered).encode())[0], 409)

    def test_home_no_slot_and_cancel_release(self):
        with tempfile.TemporaryDirectory() as folder:
            home = FakeHome(Path(folder) / "home.sqlite", delay=0.05)
            home.submit(Request("first", time.time() + 10, "r", ("x",), {}).body())
            self.assertEqual(
                home.submit(
                    Request("second", time.time() + 10, "r", ("x",), {}).body()
                )[0],
                429,
            )
            home.cancel("first")
            home.close()
            self.assertTrue(home.lookup("first")[1]["gpu_slot_released"])
            self.assertEqual(home.status()["active_slots"], 0)

    def test_relay_rejects_non_tls_and_credentials_in_model_envelope(self):
        with self.assertRaises(UnsafeTransition):
            relay_handler("http://127.0.0.1", "c", "h", None)
        value = json.loads(Request("id", time.time() + 10, "r", ("x",), {}).body())
        value["payload"] = {"password": "not-forwarded"}
        with self.assertRaises(UnsafeTransition):
            envelope(json.dumps(value).encode())


if __name__ == "__main__":
    unittest.main()
