"""Explicit import/send/status commands; no inferred host or automatic network activity."""

import argparse
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

from .errors import SenderError, ValidationError
from .runner import AudioPreparer, MertCommand, WorkerHTTP, run_once
from .service import send_once
from .store import Outbox
from .transport import HTTPTransport, validate_endpoint
from .validation import load_results


def parser():
    root = argparse.ArgumentParser(description="PC 추론 후보 결과의 로컬 등록과 서버 수신 확인")
    root.add_argument("--outbox", required=True, help="독립 송신 outbox SQLite 파일")
    commands = root.add_subparsers(dest="command", required=True)
    add = commands.add_parser("import", help="결과 JSON을 전체 검증한 뒤 원자적으로 등록합니다")
    add.add_argument("file")
    status = commands.add_parser("status", help="본문·토큰을 제외한 송신 상태를 조회합니다")
    status.add_argument("--limit", type=int, default=100)
    history = commands.add_parser("history", help="본문·토큰 없는 곡별 송신/재시도 이력을 조회합니다")
    history.add_argument("result_id")
    history.add_argument("--limit", type=int, default=100)
    retry = commands.add_parser("requeue", help="거절/재시도 소진 항목을 같은 결과로 명시 재등록합니다")
    retry.add_argument("result_id")
    send = commands.add_parser("send", help="대기 결과를 제한된 횟수만 송신합니다")
    send.add_argument("--base-url", required=True, help="예: https://host.example/api/phase4")
    send.add_argument("--token-env", default="PHASE4_SENDER_TOKEN")
    send.add_argument("--allow-loopback-http", action="store_true", help="숫자 loopback HTTP 데모만 허용합니다")
    send.add_argument("--timeout", type=float, default=10)
    send.add_argument("--limit", type=int, default=100)
    send.add_argument("--max-attempts", type=int, default=8)
    run = commands.add_parser("run", help="준비된 WAV가 있는 곡 한 건을 추론하고 후보를 송신합니다")
    run.add_argument("--base-url", required=True, help="예: https://host.example/api/phase4")
    run.add_argument("--token-env", default="PHASE4_SENDER_TOKEN")
    run.add_argument("--allow-loopback-http", action="store_true")
    run.add_argument("--timeout", type=float, default=10, help="HTTP 제한 시간(초)")
    run.add_argument("--wav-root", required=True, type=Path, help="<YouTube 영상 ID>.wav를 보관한 폴더")
    run.add_argument("--baseline-root", required=True, type=Path, help="고정 2차 모델 기준 실험 폴더")
    run.add_argument("--refinement-root", required=True, type=Path, help="고정 3차 앙상블 실험 폴더")
    run.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    run.add_argument("--model-timeout", type=float, default=840, help="추론 제한 시간(초)")
    run.add_argument("--audio-preparer", type=Path, help="WAV가 없을 때만 사용할 준비 프로그램의 절대 경로")
    run.add_argument("--audio-preparer-arg", action="append", default=[], help="운영자가 지정하는 고정 인자(반복 가능)")
    run.add_argument("--audio-preparer-timeout", type=float, default=120, help="곡당 오디오 준비 제한 시간(초)")
    run.add_argument("--poll-seconds", type=float, help="지정하면 한 건씩 처리하며 다음 조회까지 이 시간만큼 대기합니다")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            return run_worker(args)
        values, transport = None, None
        if args.command == "import":
            values = load_results(args.file)
        elif args.command == "send":
            validate_endpoint(args.base_url, allow_loopback_http=args.allow_loopback_http)
            transport = HTTPTransport(args.base_url.rstrip("/") + "/worker/results",
                                      token_env=args.token_env, timeout=args.timeout,
                                      allow_loopback_http=args.allow_loopback_http)
            transport.check_ready()
        if args.command != "import" and not Path(args.outbox).is_file():
            raise ValidationError("OUTBOX_NOT_FOUND")
        with Outbox(args.outbox) as outbox:
            if args.command == "import":
                output = outbox.enqueue(values, now=time.time())
            elif args.command == "status":
                output = {"results": outbox.status(limit=args.limit)}
            elif args.command == "history":
                output = {"history": outbox.history(args.result_id, limit=args.limit)}
            elif args.command == "requeue":
                outbox.requeue(args.result_id, now=time.time())
                output = {"requeued": True}
            else:
                output = send_once(outbox, transport, limit=args.limit, max_attempts=args.max_attempts)
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        if args.command == "send" and any(output[key] for key in ("retry", "rejected", "dead", "lease_lost")):
            return 1
        return 0
    except SenderError as exc:
        print(json.dumps({"error": exc.code}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print('{"error":"LOCAL_STORAGE_UNAVAILABLE"}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def run_worker(args):
    root = args.wav_root.resolve()
    if not root.is_dir():
        raise ValidationError("WAV_ROOT_UNAVAILABLE")
    if args.poll_seconds is not None and (not math.isfinite(args.poll_seconds) or not 1 <= args.poll_seconds <= 3600):
        raise ValidationError("INVALID_POLL_INTERVAL")
    if args.audio_preparer is None and args.audio_preparer_arg:
        raise ValidationError("INVALID_AUDIO_PREPARER")
    preparer = (AudioPreparer(args.audio_preparer, args.audio_preparer_arg, args.audio_preparer_timeout,
                              args.token_env) if args.audio_preparer is not None else None)
    transport = WorkerHTTP(args.base_url, token_env=args.token_env, timeout=args.timeout,
                           allow_loopback_http=args.allow_loopback_http)
    transport.check_ready()
    predictor = MertCommand(args.baseline_root, args.refinement_root, args.device, args.token_env)
    predictor.check_ready()
    with Outbox(args.outbox) as outbox:
        while True:
            try:
                outcome = run_once(outbox, transport, predictor, root, preparer=preparer, model_timeout=args.model_timeout)
            except SenderError as exc:
                if args.poll_seconds is None or exc.code in {"WORKER_AUTH_REJECTED", "INVALID_MODEL_TIMEOUT"}:
                    raise
                outcome = {"status": "ERROR", "error": exc.code}
            print(json.dumps(outcome, ensure_ascii=False, sort_keys=True), flush=True)
            if args.poll_seconds is None:
                return 0 if outcome["status"] in {"NO_READY_WAV", "RECEIVED"} else 1
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
