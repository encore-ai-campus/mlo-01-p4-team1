import hashlib
import json
import os
import sys
import tempfile
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pc_sender.errors import ValidationError
from pc_sender.runner import AudioPreparer, WorkerHTTP, run_once
from pc_sender.store import Outbox
from pc_sender.transport import HTTPResponse
from pc_sender.validation import canonical


NOW = 1_800_000_000
VIDEO_A = "AAAAAAAAAAA"
VIDEO_B = "BBBBBBBBBBB"


def source(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


def task(video_id):
    return {"id": str(uuid4()), "source_ref": source(video_id)}


class Api:
    def __init__(self, tasks):
        self.ready = tasks
        self.claimed = []
        self.sent = []
        self.http_status = 201

    def tasks(self, offset):
        return self.ready[offset:offset + 20], offset + 20 < len(self.ready)

    def claim_task(self, identifier):
        self.claimed.append(identifier)
        row = next(row for row in self.ready if row["id"] == identifier)
        return {"task_id": identifier, "request_item_id": str(uuid4()), "lease_token": "original-lease",
                "lease_expires_at": datetime.fromtimestamp(NOW + 900, timezone.utc).isoformat(),
                "source_ref": row["source_ref"], "track_id": None,
                "input_payload": {"youtube_url": row["source_ref"], "track_name": "곡", "artist_name": "가수"}}

    def send(self, body, *, idempotency_key):
        self.sent.append(body)
        if self.http_status != 201:
            return HTTPResponse(self.http_status, b"{}")
        payload = json.loads(body)
        digest = payload.pop("payload_sha256")
        assert digest == hashlib.sha256(canonical(payload)).hexdigest()
        assert idempotency_key == payload["result_id"]
        ack = {"result_id": idempotency_key, "payload_sha256": digest, "receipt_id": str(uuid4()),
               "status": "RECEIVED_PENDING_REVIEW"}
        return HTTPResponse(201, canonical(ack))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.outbox = Outbox(self.root / "outbox.sqlite3")
        self.addCleanup(self.outbox.close)

    def predict(self, path, *, timeout):
        self.assertTrue(path.is_file())
        self.assertEqual(timeout, 840)
        return {"genre": ["K-POP"], "mood": ["CALM", "GROOVY"]}

    def test_unprepared_track_does_not_block_ready_track(self):
        absent, present = task(VIDEO_A), task(VIDEO_B)
        api = Api([absent, present])
        with wave.open(str(self.root / (VIDEO_B + ".wav")), "wb") as audio:
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            audio.writeframes(bytes(36000))
        result = run_once(self.outbox, api, self.predict, self.root, clock=lambda: NOW)
        self.assertEqual(api.claimed, [present["id"]])
        self.assertEqual(result["status"], "RECEIVED")
        self.assertEqual(result["inference"], "CANDIDATES_GENERATED")
        payload = json.loads(api.sent[0])
        self.assertEqual(payload["tags"], {"genre": ["K-POP"], "mood": ["CALM", "GROOVY"]})
        self.assertEqual(payload["metadata"], {"duration_ms": 750})
        self.assertIsNone(payload["track_id"])

    def test_retry_replays_byte_identical_result_without_reprocessing(self):
        api = Api([task(VIDEO_A)])
        (self.root / (VIDEO_A + ".wav")).touch()
        api.http_status = 503
        first = run_once(self.outbox, api, self.predict, self.root, clock=lambda: NOW)
        self.assertEqual(first["status"], "RETRY")
        api.ready, api.http_status = [], 201
        run_once(self.outbox, api, self.predict, self.root, clock=lambda: NOW + 60)
        self.assertEqual(len(api.claimed), 1)
        self.assertEqual(api.sent, [api.sent[0], api.sent[0]])
        self.assertEqual(self.outbox.result_status(first["result_id"]), "RECEIVED")

    def test_model_failure_never_sends_a_result(self):
        api = Api([task(VIDEO_A)])
        (self.root / (VIDEO_A + ".wav")).touch()

        def fail(path, *, timeout):
            raise ValidationError("MODEL_EXECUTION_FAILED")

        with self.assertRaisesRegex(ValidationError, "MODEL_EXECUTION_FAILED"):
            run_once(self.outbox, api, fail, self.root, clock=lambda: NOW)
        self.assertFalse(api.sent)
        self.assertFalse(self.outbox.status())

    def test_preparer_failure_skips_to_next_candidate(self):
        absent, present = task(VIDEO_A), task(VIDEO_B)
        api = Api([absent, present])

        def prepare(root, reference):
            if reference == source(VIDEO_A):
                return None
            path = root / (VIDEO_B + ".wav")
            path.touch()
            return path

        result = run_once(self.outbox, api, self.predict, self.root, preparer=prepare, clock=lambda: NOW)
        self.assertEqual(result["prepare_failed"], 1)
        self.assertEqual(api.claimed, [present["id"]])

    def test_preparer_uses_canonical_argument_and_removes_worker_token(self):
        code = ('import os,sys,wave; assert "TOKEN_FOR_TEST" not in os.environ; '
                'assert sys.argv[1:4] == ["--youtube-url", "' + source(VIDEO_A) + '", "--wav-output"]; '
                'w=wave.open(sys.argv[4],"wb"); w.setparams((1,2,24000,0,"NONE","not compressed")); '
                'w.writeframes(bytes(48000)); w.close()')
        with patch.dict(os.environ, {"TOKEN_FOR_TEST": "private"}):
            preparer = AudioPreparer(Path(sys.executable), ["-B", "-c", code], 5, "TOKEN_FOR_TEST")
            path = preparer(self.root, source(VIDEO_A))
        self.assertEqual(path, self.root / (VIDEO_A + ".wav"))
        with wave.open(str(path), "rb") as audio:
            self.assertEqual(audio.getnframes(), 24000)

    def test_worker_http_matches_server_paths_and_json_body(self):
        row = task(VIDEO_A)
        transport = WorkerHTTP("https://example.invalid/api/phase4")
        with patch.object(transport, "_request", side_effect=(
                HTTPResponse(200, canonical({"items": [row], "has_more": False})),
                HTTPResponse(409, b"{}"))) as request:
            self.assertEqual(transport.tasks(0), ([row], False))
            self.assertIsNone(transport.claim_task(row["id"]))
        calls = [call.args for call in request.call_args_list]
        self.assertEqual(calls, [
            ("https://example.invalid/api/phase4/worker/tasks?limit=20&offset=0", "GET"),
            (f"https://example.invalid/api/phase4/worker/tasks/{row['id']}/claim", "POST", b"{}"),
        ])


if __name__ == "__main__":
    unittest.main()
