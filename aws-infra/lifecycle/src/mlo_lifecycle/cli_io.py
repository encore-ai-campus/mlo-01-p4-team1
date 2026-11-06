"""Local input snapshots and exclusive report reservation before effects start."""

import os
from pathlib import Path

from .journal import atomic
from .model import file_hash, local_path, need


class InputSnapshots:
    def __init__(self, paths):
        self.files = {Path(p): self.identity(Path(p)) for p in paths if p is not None}

    @staticmethod
    def identity(path):
        value = file_hash(path)
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_mtime_ns, value

    def check(self):
        for path, identity in self.files.items():
            need(self.identity(path) == identity, "input snapshot replaced/changed",
                 code="artifact_drift", phase="boundary")


class OutputReservation:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self.identity = None
        self.published = False

    def reserve(self):
        if self.path is None:
            return
        self.path = local_path(self.path)
        need(self.path.parent.is_dir(), "output parent must exist", code="invalid_output")
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            from .model import Refused
            raise Refused("output reservation failed", code="invalid_output") from exc
        with os.fdopen(fd, "wb") as stream:
            stat = os.fstat(stream.fileno())
            self.identity = stat.st_dev, stat.st_ino
            stream.write(b'{"status":"reserved","ready_for_aws":false}\n')
            stream.flush()
            os.fsync(stream.fileno())

    def owned(self):
        if self.path is None or any(p.is_symlink() for p in (self.path, *self.path.parents)):
            return False
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return False
        return (info.st_dev, info.st_ino) == self.identity

    def publish(self, value):
        if self.path is None:
            return
        need(self.owned(), "output reservation replaced", code="report_write_failure", phase="report")
        atomic(self.path, value)
        self.published = True

    def close(self):
        try:
            if self.identity and not self.published and self.owned():
                self.path.unlink()
        except OSError:
            # Never turn a reported execution outcome into an unhandled cleanup traceback.
            # A reservation left by a crash/permission loss must be inspected, not reused.
            return False
        return True


def safe_error(exc, operation, lab, *, reporting=False):
    from .model import Refused
    code = ("report_write_failure" if reporting else exc.code if isinstance(exc, Refused)
            else "backend_unavailable" if isinstance(exc, OSError) else "invalid_input")
    reasons = {
        "authorization_failure": ("승인 또는 증거 검증 실패", "승인 범위·발급자·유효기간을 검토하세요"),
        "plan_expired": ("계획 유효기간 만료", "영수증을 보존하고 관측·재계획 절차를 따르세요"),
        "state_drift": ("계획과 상태 또는 선행 증거 불일치", "현재 관측과 기존 영수증을 대조하세요"),
        "artifact_drift": ("실행 코드 또는 입력 snapshot 변경", "실행을 중단하고 승인된 artifact와 비교하세요"),
        "backup_failure": ("백업 또는 복원 검증 실패; 부분 효과 확인 필요", "DB·사본·영수증을 관측하고 자동 재시도하지 마세요"),
        "partial_effect_unknown": ("효과 완료 여부 미확정", "관측 후 서명된 reconciliation을 검토하세요"),
        "backend_unavailable": ("로컬 저장소 접근 실패; 부분 효과 확인 필요", "접근을 복구하고 journal과 현재 상태를 대조하세요"),
        "journal_integrity": ("실행 이력 무결성 확인 실패", "외부 보존 이력과 checkpoint를 복구·대조하세요"),
        "invalid_output": ("출력 경로 사전 검증 실패", "존재하지 않는 안전한 출력 경로를 지정하세요"),
        "protection_mismatch": ("보호 정책과 snapshot 불일치", "보호 근거를 다시 검토하세요"),
        "report_write_failure": ("작업 후 보고서 저장 실패", "재실행하지 말고 stdout 결과와 journal을 확인하세요"),
        "invalid_input": ("지원되지 않거나 잘못된 로컬 입력", "입력 계약과 로컬 전용 실행 범위를 확인하세요"),
    }
    if code not in reasons:
        code = "invalid_input"
    reason, recovery = reasons[code]
    return {"schema_version": "lifecycle-error-v1", "ready_for_aws": False, "error_code": code,
            "phase": "report" if reporting else getattr(exc, "phase", "validation"),
            "step_id": getattr(exc, "step_id", None), "safe_reason": reason,
            "journal_location": str(Path(lab) / "journal.jsonl") if lab else None,
            "recovery_action": recovery, "operation": operation}
