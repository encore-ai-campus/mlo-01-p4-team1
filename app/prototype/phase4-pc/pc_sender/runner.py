"""Find prepared WAVs, lease one task, and deliver a review candidate."""

import hashlib
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .errors import RetryableTaskError, TransportError, ValidationError
from .service import send_once
from .transport import HTTPTransport
from .validation import MAX_DURATION_MS, SCHEMA_VERSION, _text, _uuid, canonical, decode_json, validate_tags

MODEL_VERSION = "mert-v2-l24-ensemble-mlp50-20261104"
YOUTUBE_SOURCE = re.compile(r"https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})\Z")
PAGE_SIZE = 20
DELIVERY_RESERVE_SECONDS = 15
MAX_TASK_ATTEMPTS = 3


def video_id(source):
    match = YOUTUBE_SOURCE.fullmatch(source) if isinstance(source, str) else None
    if not match:
        raise ValidationError("INVALID_TASK_SOURCE")
    return match.group(1)


class WorkerHTTP(HTTPTransport):
    def __init__(self, base_url, **options):
        super().__init__(base_url, **options)
        self.base_url = self.endpoint.rstrip("/")
        self.endpoint = self.base_url + "/worker/results"

    def _json(self, response):
        if response.status in (401, 403):
            raise TransportError("WORKER_AUTH_REJECTED")
        if response.status != 200:
            raise TransportError("WORKER_API_UNAVAILABLE")
        if response.content_type != "application/json":
            raise TransportError("INVALID_WORKER_RESPONSE")
        return decode_json(response.body)

    def tasks(self, offset):
        result = self._json(self._request(
            f"{self.base_url}/worker/tasks?limit={PAGE_SIZE}&offset={offset}", "GET"))
        if (not isinstance(result, dict) or not isinstance(result.get("items"), list)
                or type(result.get("has_more")) is not bool or len(result["items"]) > PAGE_SIZE
                or (result["has_more"] and len(result["items"]) != PAGE_SIZE)):
            raise TransportError("INVALID_WORKER_RESPONSE")
        return result["items"], result["has_more"]

    def claim_task(self, task_id):
        _uuid(task_id, "task_id")
        response = self._request(f"{self.base_url}/worker/tasks/{task_id}/claim", "POST", b"{}")
        if response.status == 409:
            return None
        return self._json(response)


class MertCommand:
    def __init__(self, baseline_root, refinement_root, device, token_env):
        self.command = [sys.executable, "-B", "-m", "pc_sender.mert", "--baseline-root",
                        str(Path(baseline_root).resolve()), "--refinement-root",
                        str(Path(refinement_root).resolve()), "--device", device, "--worker-json-errors"]
        self.env = subprocess_environment(token_env)

    def check_ready(self):
        self._invoke(["--check"], timeout=15)

    def __call__(self, audio_path, *, timeout):
        return validate_tags(self._invoke(["--wav", str(audio_path)], timeout=timeout))

    def _invoke(self, args, *, timeout):
        try:
            result = subprocess.run([*self.command, *args], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    env=self.env, check=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RetryableTaskError("MODEL_EXECUTION_TIMEOUT") from None
        except OSError:
            raise ValidationError("MODEL_EXECUTION_UNAVAILABLE") from None
        if len(result.stdout) > 65536:
            raise ValidationError("MODEL_EXECUTION_FAILED")
        if result.returncode != 0:
            try:
                failure = decode_json(result.stdout)
            except ValidationError:
                failure = None
            if isinstance(failure, dict) and failure.get("error") == "INVALID_PREPARED_WAV":
                raise ValidationError("INVALID_PREPARED_WAV")
            raise ValidationError("MODEL_EXECUTION_FAILED")
        return decode_json(result.stdout)


def subprocess_environment(token_env):
    return {key: value for key, value in os.environ.items() if key.casefold() != token_env.casefold()}


class AudioPreparer:
    def __init__(self, program, arguments, timeout, token_env):
        if not program.is_absolute() or not program.is_file() or program.suffix.lower() in {".bat", ".cmd"}:
            raise ValidationError("INVALID_AUDIO_PREPARER")
        if not math.isfinite(timeout) or not 1 <= timeout <= 600:
            raise ValidationError("INVALID_AUDIO_PREPARER_TIMEOUT")
        self.command = [str(program.resolve()), *arguments]
        self.timeout = timeout
        self.env = subprocess_environment(token_env)

    def __call__(self, wav_root, source):
        destination = wav_root / (video_id(source) + ".wav")
        if ready_audio(wav_root, source) is not None:
            return destination
        try:
            with tempfile.TemporaryDirectory(prefix=".phase4-wav-", dir=wav_root) as directory:
                output = Path(directory) / "prepared.wav"
                result = subprocess.run([*self.command, "--youtube-url", source, "--wav-output", str(output)],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        env=self.env, check=False, timeout=self.timeout)
                if result.returncode != 0:
                    raise ValidationError("AUDIO_PREPARER_FAILED")
                if output.is_symlink() or not output.is_file() or not is_wave(output):
                    raise ValidationError("INVALID_PREPARER_OUTPUT")
                try:
                    # A Windows rename refuses an existing destination. On POSIX,
                    # a hard link supplies the same atomic no-replacement behavior.
                    if os.name == "nt":
                        output.rename(destination)
                    else:
                        os.link(output, destination)
                except FileExistsError:
                    pass
                return ready_audio(wav_root, source)
        except subprocess.TimeoutExpired:
            raise RetryableTaskError("AUDIO_PREPARATION_TIMEOUT") from None
        except OSError:
            raise RetryableTaskError("AUDIO_PREPARATION_IO") from None


def is_wave(path):
    with path.open("rb") as handle:
        header = handle.read(12)
    return len(header) == 12 and header[:4] in {b"RIFF", b"RIFX", b"RF64"} and header[8:] == b"WAVE"


def ready_audio(wav_root, source):
    path = wav_root / (video_id(source) + ".wav")
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or path.resolve().parent != wav_root or not is_wave(path):
            raise ValidationError("INVALID_PREPARED_WAV")
        return path
    except FileNotFoundError:
        return None
    except OSError:
        raise RetryableTaskError("PREPARED_WAV_IO") from None


def input_key(wav_root, source, *, has_preparer):
    stamp = None
    match = YOUTUBE_SOURCE.fullmatch(source) if isinstance(source, str) else None
    if match:
        path = wav_root / (match[1] + ".wav")
        try:
            info = path.lstat()
            stamp = [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns]
        except FileNotFoundError:
            pass
        except OSError:
            stamp = "unavailable"
    return hashlib.sha256(canonical([source, stamp, has_preparer if stamp is None else None])).hexdigest()


def wav_duration_ms(path):
    try:
        with wave.open(str(path), "rb") as audio:
            frames, rate = audio.getnframes(), audio.getframerate()
    except (OSError, EOFError, wave.Error):
        # The MERT runtime can also read floating-point WAV and RF64.
        try:
            from soundfile import info

            audio = info(str(path))
            frames, rate = audio.frames, audio.samplerate
        except (ImportError, OSError, RuntimeError):
            return None
    if rate <= 0:
        return None
    duration_ms = (frames * 1000 + rate // 2) // rate
    return duration_ms if 0 < duration_ms <= MAX_DURATION_MS else None


def _validate_claim(claim, task_id, source):
    fields = {"task_id", "request_item_id", "lease_token", "lease_expires_at", "source_ref",
              "input_payload", "track_id"}
    if (not isinstance(claim, dict) or not fields <= claim.keys()
            or claim["task_id"] != task_id or claim["source_ref"] != source):
        raise ValidationError("INVALID_TASK_CLAIM")
    _uuid(claim["request_item_id"], "request_item_id")
    _text(claim["lease_token"], "lease_token")
    if claim["track_id"] is not None:
        _text(claim["track_id"], "track_id")
    payload = claim["input_payload"]
    if (not isinstance(payload, dict) or not {"youtube_url", "track_name", "artist_name"} <= payload.keys()
            or payload["youtube_url"] != source):
        raise ValidationError("INVALID_TASK_CLAIM")
    for field in ("track_name", "artist_name"):
        _text(payload[field], field, 500)
    try:
        expires = datetime.fromisoformat(claim["lease_expires_at"])
        if expires.utcoffset() is None:
            raise ValueError
        return expires.timestamp()
    except (OverflowError, OSError, ValueError, TypeError):
        raise ValidationError("INVALID_TASK_LEASE") from None


def note_issue(result, task_id, code, *, attempts=0, retry_after=None, status="BLOCKED"):
    result["skipped"] += 1
    if len(result["issues"]) < PAGE_SIZE:
        result["issues"].append({"task_id": task_id, "code": code, "status": status,
                                 "attempts": attempts, "retry_after": retry_after})


def task_failure(outbox, result, task_id, key, error, clock, *, earliest_retry=0):
    retry = isinstance(error, RetryableTaskError)
    state = outbox.fail_task(task_id, key, error.code, now=clock(), retry=retry,
                             max_attempts=MAX_TASK_ATTEMPTS, earliest_retry=earliest_retry)
    status = "RETRY_WAIT" if state["retry_after"] is not None else "RETRY_EXHAUSTED" if retry else "BLOCKED"
    note_issue(result, task_id, **state, status=status)


def skip_task(outbox, result, task_id, key, clock):
    state = outbox.task_state(task_id, key)
    if state is None:
        return False
    delivery_status = state["delivery_status"]
    if delivery_status in {"PENDING", "SENDING", "RETRY", "REJECTED", "DEAD"}:
        note_issue(result, task_id, state["delivery_error"] or "RESULT_AWAITING_DELIVERY",
                   attempts=state["attempts"], retry_after=state["retry_after"], status=delivery_status)
        return True
    if state["code"] is None:
        return False
    retry_after = state["retry_after"]
    if retry_after is not None and retry_after <= clock():
        return False
    status = "RETRY_WAIT" if retry_after is not None else "RETRY_EXHAUSTED" if state["retryable"] else "BLOCKED"
    note_issue(result, task_id, state["code"], attempts=state["attempts"], retry_after=retry_after, status=status)
    return True


def _available_tasks(outbox, api, wav_root, preparer, result, clock):
    missing = []
    offset = 0
    while True:
        tasks, has_more = api.tasks(offset)
        for task in tasks:
            result["checked"] += 1
            try:
                task_id = task.get("id") if isinstance(task, dict) else None
                _uuid(task_id, "task_id")
            except ValidationError:
                note_issue(result, None, "INVALID_WORKER_TASK")
                continue
            source = task.get("source_ref")
            key = input_key(wav_root, source, has_preparer=preparer is not None)
            if skip_task(outbox, result, task_id, key, clock):
                continue
            try:
                audio_path = ready_audio(wav_root, source)
            except (ValidationError, RetryableTaskError) as exc:
                task_failure(outbox, result, task_id, key, exc, clock)
                continue
            if audio_path is not None:
                yield task_id, source, audio_path, key
            elif preparer is not None:
                missing.append((task_id, source))
            else:
                task_failure(outbox, result, task_id, key, ValidationError("WAV_NOT_AVAILABLE"), clock)
        if not has_more:
            break
        offset += len(tasks)
    for task_id, source in missing:
        try:
            preparer(wav_root, source)
            audio_path = ready_audio(wav_root, source)
            if audio_path is None:
                raise ValidationError("AUDIO_PREPARER_FAILED")
        except (ValidationError, RetryableTaskError) as exc:
            result["prepare_failed"] += 1
            task_failure(outbox, result, task_id, input_key(wav_root, source, has_preparer=True), exc, clock)
        else:
            yield task_id, source, audio_path, input_key(wav_root, source, has_preparer=True)


def run_once(outbox, api, predictor, wav_root, *, preparer=None, model_timeout=840, clock=time.time):
    if not math.isfinite(model_timeout) or not 0 < model_timeout <= 3600:
        raise ValidationError("INVALID_MODEL_TIMEOUT")
    # An ACK may have been lost just before an earlier process stopped. Replay the
    # same persisted body before asking the server to lease another task.
    previous = send_once(outbox, api, clock=clock)
    result = {"status": "NO_READY_WAV", "checked": 0, "prepare_failed": 0, "skipped": 0, "issues": [],
              "delivery": previous}
    for task_id, source, audio_path, key in _available_tasks(outbox, api, wav_root, preparer, result, clock):
        claim = api.claim_task(task_id)
        if claim is None:
            continue
        expires = 0
        try:
            expires = _validate_claim(claim, task_id, source)
            timeout = min(model_timeout, expires - clock() - DELIVERY_RESERVE_SECONDS)
            if timeout <= 0:
                raise RetryableTaskError("TASK_LEASE_TOO_SHORT")
            tags = validate_tags(predictor(audio_path, timeout=timeout))
            duration_ms = wav_duration_ms(audio_path)
            now = clock()
            if now >= expires:
                raise RetryableTaskError("TASK_LEASE_EXPIRED")
        except (ValidationError, RetryableTaskError) as exc:
            task_failure(outbox, result, task_id, key, exc, clock, earliest_retry=expires)
            continue
        # A newly issued lease must use a new result ID. An outbox retry keeps
        # both the original ID and the entire envelope byte-for-byte.
        result_id = str(uuid5(NAMESPACE_URL, f"phase4-inference:{task_id}:{claim['lease_token']}"))
        value = {"schema_version": SCHEMA_VERSION, "result_id": result_id, "batch_id": result_id,
                 "task_id": task_id, "request_item_id": claim["request_item_id"], "lease_token": claim["lease_token"],
                 "track_id": claim["track_id"], "source_ref": source, "model_version": MODEL_VERSION,
                 "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(), "tags": tags}
        if duration_ms is not None:
            value["metadata"] = {"duration_ms": duration_ms}
        outbox.enqueue([value], now=now, input_key=key)
        delivery = send_once(outbox, api, clock=clock)
        state = outbox.result_status(result_id)
        return {**result, "status": state, "inference": "CANDIDATES_GENERATED", "result_id": result_id,
                "delivery": delivery}
    if previous["retry"]:
        result["status"] = "RETRY_PENDING"
    elif result["skipped"]:
        result["status"] = "TASKS_BLOCKED"
    return result
