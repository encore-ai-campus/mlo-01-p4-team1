import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pc_sender.errors import RetryableTaskError, ValidationError
from pc_sender.runner import AudioPreparer, WorkerHTTP, run_once
from pc_sender.store import Outbox
from pc_sender.transport import HTTPResponse
from pc_sender.validation import canonical


NOW = 1_800_000_000
VIDEO_A = "AAAAAAAAAAA"
VIDEO_B = "BBBBBBBBBBB"
VIDEO_C = "CCCCCCCCCCC"


def source(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


def task(video_id):
    return {"id": str(uuid4()), "source_ref": source(video_id)}


def write_wave(root, video_id):
    path = root / (video_id + ".wav")
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(bytes(60000))
    return path


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
        write_wave(self.root, VIDEO_B)
        result = run_once(self.outbox, api, self.predict, self.root, clock=lambda: NOW)
        self.assertEqual(api.claimed, [present["id"]])
        self.assertEqual(result["status"], "RECEIVED")
        self.assertEqual(result["inference"], "CANDIDATES_GENERATED")
        payload = json.loads(api.sent[0])
        self.assertEqual(payload["tags"], {"genre": ["K-POP"], "mood": ["CALM", "GROOVY"]})
        self.assertEqual(payload["metadata"], {"duration_ms": 1250})
        self.assertIsNone(payload["track_id"])

    def test_retry_replays_byte_identical_result_without_reprocessing(self):
        api = Api([task(VIDEO_A)])
        write_wave(self.root, VIDEO_A)
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
        write_wave(self.root, VIDEO_A)

        def fail(path, *, timeout):
            raise ValidationError("MODEL_EXECUTION_FAILED")

        result = run_once(self.outbox, api, fail, self.root, clock=lambda: NOW)
        self.assertEqual(result["issues"], [{
            "task_id": api.ready[0]["id"], "code": "MODEL_EXECUTION_FAILED",
            "status": "BLOCKED", "attempts": 1, "retry_after": None,
        }])
        run_once(self.outbox, api, self.predict, self.root, clock=lambda: NOW + 1000)
        self.assertEqual(len(api.claimed), 1)
        self.assertFalse(api.sent)
        self.assertFalse(self.outbox.status())

    def test_new_task_for_same_wav_is_independent_of_old_permanent_failure(self):
        failed, reapproved = task(VIDEO_A), task(VIDEO_A)
        api = Api([failed])
        write_wave(self.root, VIDEO_A)

        def fail(path, *, timeout):
            raise ValidationError("MODEL_EXECUTION_FAILED")

        first = run_once(self.outbox, api, fail, self.root, clock=lambda: NOW)
        self.assertEqual(first["issues"][0]["status"], "BLOCKED")
        self.assertFalse(api.sent)
        api.ready = [failed, reapproved]

        with Outbox(self.root / "outbox.sqlite3") as reopened:
            result = run_once(reopened, api, self.predict, self.root, clock=lambda: NOW)
            self.assertEqual(result["status"], "RECEIVED")
            self.assertEqual(result["issues"], first["issues"])
            self.assertEqual(api.claimed, [failed["id"], reapproved["id"]])
            self.assertEqual(len(api.sent), 1)
            payload = json.loads(api.sent[0])
            self.assertEqual((payload["task_id"], payload["source_ref"]), (reapproved["id"], source(VIDEO_A)))
            self.assertEqual([row["task_id"] for row in reopened.task_failures()], [failed["id"]])

    def test_preparer_failure_skips_to_next_candidate(self):
        absent, present = task(VIDEO_A), task(VIDEO_B)
        api = Api([absent, present])

        def prepare(root, reference):
            if reference == source(VIDEO_A):
                return None
            return write_wave(root, VIDEO_B)

        result = run_once(self.outbox, api, self.predict, self.root, preparer=prepare, clock=lambda: NOW)
        self.assertEqual(result["prepare_failed"], 1)
        self.assertEqual(api.claimed, [present["id"]])
        self.assertEqual(result["issues"][0]["code"], "AUDIO_PREPARER_FAILED")

    def test_corrupt_wav_does_not_block_other_jobs_and_replacement_is_seen(self):
        invalid, valid = task(VIDEO_A), task(VIDEO_B)
        api = Api([invalid, valid])
        (self.root / (VIDEO_A + ".wav")).write_bytes(b"not a WAV")
        write_wave(self.root, VIDEO_B)
        first = run_once(self.outbox, api, self.predict, self.root, clock=lambda: NOW)
        self.assertEqual(first["issues"][0]["code"], "INVALID_PREPARED_WAV")
        self.assertEqual(api.claimed, [valid["id"]])
        api.ready = [invalid]
        blocked = run_once(self.outbox, api, self.predict, self.root, clock=lambda: NOW)
        self.assertEqual(blocked["issues"], first["issues"])
        self.assertEqual(api.claimed, [valid["id"]])
        write_wave(self.root, VIDEO_A)
        repaired = run_once(self.outbox, api, self.predict, self.root, clock=lambda: NOW)
        self.assertEqual(repaired["status"], "RECEIVED")
        self.assertEqual(api.claimed, [valid["id"], invalid["id"]])

    def test_symlink_is_not_opened_and_the_next_job_runs(self):
        invalid, valid = task(VIDEO_A), task(VIDEO_B)
        api = Api([invalid, valid])
        target = write_wave(self.root, VIDEO_B)
        link = self.root / (VIDEO_A + ".wav")
        try:
            link.symlink_to(target)
        except OSError:
            # Windows may disallow creating links. Supply the same lstat result
            # without weakening the production check or opening the bad path.
            original_lstat = Path.lstat
            target_info = original_lstat(target)
            link_info = os.stat_result((stat.S_IFLNK | 0o777, *target_info[1:]))

            def lstat(path):
                return link_info if path == link else original_lstat(path)

            with patch.object(Path, "lstat", lstat):
                result = run_once(self.outbox, api, self.predict, self.root, clock=lambda: NOW)
        else:
            result = run_once(self.outbox, api, self.predict, self.root, clock=lambda: NOW)
        self.assertEqual(result["issues"][0]["code"], "INVALID_PREPARED_WAV")
        self.assertEqual(api.claimed, [valid["id"]])

    def test_model_timeout_waits_for_lease_and_keeps_queue_moving(self):
        slow, valid = task(VIDEO_A), task(VIDEO_B)
        api = Api([slow, valid])
        for video in (VIDEO_A, VIDEO_B):
            write_wave(self.root, video)

        def predict(path, *, timeout):
            if path.stem == VIDEO_A:
                raise RetryableTaskError("MODEL_EXECUTION_TIMEOUT")
            return self.predict(path, timeout=timeout)

        result = run_once(self.outbox, api, predict, self.root, clock=lambda: NOW)
        self.assertEqual(result["status"], "RECEIVED")
        self.assertEqual(api.claimed, [slow["id"], valid["id"]])
        self.assertEqual(result["issues"][0]["retry_after"], NOW + 900)
        api.ready = [slow]
        result = run_once(self.outbox, api, predict, self.root, clock=lambda: NOW + 899)
        self.assertEqual(result["issues"][0]["status"], "RETRY_WAIT")
        self.assertEqual(api.claimed, [slow["id"], valid["id"]])

    def test_preparation_retry_is_bounded_and_persists_between_polls(self):
        unavailable, valid = task(VIDEO_A), task(VIDEO_B)
        api = Api([unavailable, valid])
        calls = []

        def prepare(root, reference):
            calls.append(reference)
            if reference == source(VIDEO_A):
                raise RetryableTaskError("AUDIO_PREPARATION_TIMEOUT")
            return write_wave(root, VIDEO_B)

        result = run_once(self.outbox, api, self.predict, self.root, preparer=prepare, clock=lambda: NOW)
        self.assertEqual(result["status"], "RECEIVED")
        self.assertEqual(result["issues"][0]["retry_after"], NOW + 15)
        self.assertEqual(api.claimed, [valid["id"]])
        api.ready = [unavailable]
        with Outbox(self.root / "outbox.sqlite3") as reopened:
            for offset in (14, 15, 44, 45, 900):
                result = run_once(reopened, api, self.predict, self.root, preparer=prepare,
                                  clock=lambda: NOW + offset)
            self.assertEqual(result["issues"][0]["status"], "RETRY_EXHAUSTED")
            self.assertEqual(result["issues"][0]["attempts"], 3)
            self.assertIsNone(result["issues"][0]["retry_after"])
            self.assertEqual(reopened.task_failures()[0]["task_id"], unavailable["id"])
            reopened.reset_task_failure(unavailable["id"])
            self.assertEqual(reopened.task_failures(), [])
        self.assertEqual(calls, [source(VIDEO_A), source(VIDEO_B), source(VIDEO_A), source(VIDEO_A)])

    def test_one_delivery_retry_does_not_starve_other_jobs(self):
        first, second, third = task(VIDEO_A), task(VIDEO_B), task(VIDEO_C)
        api = Api([first, second, third])
        for video in (VIDEO_A, VIDEO_B, VIDEO_C):
            write_wave(self.root, video)
        api.http_status = 503
        run_once(self.outbox, api, self.predict, self.root, clock=lambda: NOW)
        run_once(self.outbox, api, self.predict, self.root, clock=lambda: NOW + 2)
        self.assertEqual(api.claimed, [first["id"], second["id"]])
        self.assertEqual(api.sent[0], api.sent[1])
        self.assertNotEqual(api.sent[1], api.sent[2])

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
