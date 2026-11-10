"""Fail-closed multi-driver admission: no accepted partial writer group."""
from pathlib import Path
import tempfile
import unittest

from mlo_runtime.native_fence import dispatch
from mlo_runtime.operations import UnsafeTransition


class NativeGroupFence(unittest.TestCase):
    @staticmethod
    def token():
        return {"owner": "control-a", "generation": "native-g1",
                "epoch": 3, "expires_at": 1000.0}

    def exercise(self, failure_on=None, stop_fails=False):
        token = self.token()
        events = []
        class Lease:
            def read(self):
                return token.copy()
        class Admission:
            def admit(self, accepted, query, spec, digest):
                events.append(("admit", query))
                if query == failure_on:
                    raise UnsafeTransition("synthetic admission error")
                return {"unit": query, "started": True}
        class Execution:
            host = object()
            def stop_group(self, observed):
                events.append(("stop_group", observed["epoch"]))
                if stop_fails:
                    raise UnsafeTransition("local cgroup stop uncertain")
        with tempfile.TemporaryDirectory(prefix="mlo-native-fence-") as folder:
            binding = {"registry": str(Path(folder) / "registry.json"),
                       "continuous_drivers": [
                           {"query": "weather-stream", "specification": "/accepted/weather.json",
                            "sha256": "a" * 64},
                           {"query": "feedback-postgresql", "specification": "/accepted/feedback.json",
                            "sha256": "b" * 64},
                       ]}
            if failure_on is None:
                receipt = dispatch(binding, Lease(), None, token, "start",
                                   execution=Execution(), admission=Admission())
                self.assertTrue(receipt["accepted"])
                self.assertTrue(receipt["running"])
                self.assertNotIn(("stop_group", token["epoch"]), events)
            else:
                with self.assertRaises(UnsafeTransition):
                    dispatch(binding, Lease(), None, token, "start",
                             execution=Execution(), admission=Admission())
                self.assertEqual(events[-1], ("stop_group", token["epoch"]))
            return events

    def test_second_driver_failure_self_fences_first(self):
        events = self.exercise("feedback-postgresql")
        self.assertEqual(events[:2], [("admit", "weather-stream"),
                                      ("admit", "feedback-postgresql")])

    def test_first_driver_failure_never_starts_next(self):
        events = self.exercise("weather-stream")
        self.assertEqual(events, [("admit", "weather-stream"), ("stop_group", 3)])

    def test_uncertain_group_stop_must_not_report_accepted(self):
        self.exercise("feedback-postgresql", stop_fails=True)

    def test_complete_group_is_accepted_once(self):
        events = self.exercise()
        self.assertEqual(events, [("admit", "weather-stream"),
                                  ("admit", "feedback-postgresql")])


if __name__ == "__main__":
    unittest.main()
