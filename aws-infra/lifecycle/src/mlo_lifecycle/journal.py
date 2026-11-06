"""Durable append journal, authenticated records, and an OS process-lifetime lock."""

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import time

from .model import Refused, canonical, digest, keys, local_path, need, read, timestamp


def atomic(path, value):
    path = local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    need(not path.is_symlink(), "symlink output refused")
    fd, temp = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temp).unlink(missing_ok=True)


def authority_key(path):
    path = local_path(path)
    need(not path.is_symlink() and not path.stat().st_mode & 0o077, "authority key requires protected 0600 file")
    key = path.read_bytes()
    need(32 <= len(key) <= 4096, "invalid authority key length")
    return key


def sign(body, key):
    return {**body, "signature": hmac.new(key, canonical(body), hashlib.sha256).hexdigest()}


def authenticate(value, key):
    body = {k: v for k, v in value.items() if k != "signature"}
    need(hmac.compare_digest(str(value.get("signature", "")), sign(body, key)["signature"]),
         "untrusted approval/evidence/journal signature")
    return body


@contextmanager
def locked(lab):
    path = local_path(Path(lab) / "execution.lock")
    need(path.parent.is_dir() and not path.is_symlink(), "lab/lock missing or unsafe")
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            need(False, "concurrent execution refused")
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Journal:
    def __init__(self, lab, plan_hash, key, authorization=None):
        self.lab, self.plan_hash, self.key = Path(lab), plan_hash, key
        self.authorization = authorization
        self.path = self.lab / "journal.jsonl"
        self.checkpoint = self.lab / "checkpoint.json"
        self.events = []
        try:
            self._load()
        except (ValueError, KeyError, TypeError) as exc:
            raise Refused("journal/checkpoint integrity rejected", code="journal_integrity") from exc

    def _load(self):
        key, plan_hash = self.key, self.plan_hash
        if self.path.exists():
            need(not self.path.is_symlink(), "unsafe journal")
            for line in self.path.read_text().splitlines():
                event = json.loads(line)
                body = authenticate(event, key)
                keys(body, "sequence previous plan_sha256 step phase observation recorded_at authorization")
                need(timestamp(body["recorded_at"]), "invalid journal timestamp")
                need(body["plan_sha256"] == plan_hash, "journal belongs to another plan")
                need(type(body["sequence"]) is int and body["sequence"] == len(self.events) and
                     body["previous"] == (digest(self.events[-1]) if self.events else None),
                     "broken append journal chain")
                need(body["phase"] in {"started", "complete", "ambiguous", "reconciled"}, "invalid journal phase")
                self.events.append(event)
        if self.checkpoint.exists():
            saved = authenticate(read(self.checkpoint), key)
            sequence = saved["sequence"]
            need(type(sequence) is int and sequence == len(self.events) - 1
                 and 0 <= sequence and digest(self.events[sequence]) == saved["head"],
                 "journal truncated or checkpoint rolled back; reconcile externally", code="journal_integrity")
        else:
            need(not self.events, "checkpoint lost; restore/reconcile externally", code="journal_integrity")

    def latest(self):
        return {e["step"]: e for e in self.events}

    def append(self, step, phase, observation=None):
        value = sign({"sequence": len(self.events),
                      "previous": digest(self.events[-1]) if self.events else None,
                      "plan_sha256": self.plan_hash, "step": step, "phase": phase,
                      "observation": observation, "recorded_at": time.time(),
                      "authorization": self.authorization}, self.key)
        fd = os.open(local_path(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.events.append(value)
        atomic(self.checkpoint, sign({"sequence": len(self.events) - 1, "head": digest(value)}, self.key))
