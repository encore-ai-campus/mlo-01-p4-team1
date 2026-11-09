import tempfile
from contextlib import contextmanager
from pathlib import Path

from ads_analytics_v2.events import Event

CUTOFF = "2026-11-06T01:10:00Z"
SETTLED = "2026-11-06T01:00:00Z"


@contextmanager
def test_directory():
    root = Path(__file__).resolve().parents[1] / ".local" / "tests"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        resolved = Path(directory).resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise RuntimeError("Test cleanup target must remain under the prototype")
        yield resolved


def event(identity="i", *, delivery="d", kind="VIEWABLE_IMPRESSION",
          occurred="2026-11-06T00:00:00Z", received=None, **overrides):
    data = dict(event_id=identity, delivery_id=delivery, ad_id="ad-one", creative_version="creative-1",
                placement="top", media_id="muther-test", event_type=kind,
                occurred_at=occurred, received_at=received or occurred, result="COUNTED",
                counted=True, is_test=False, source_policy_version="v1-test-policy")
    data.update(overrides)
    return Event(**data)
