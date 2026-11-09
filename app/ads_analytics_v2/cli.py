import argparse
import json
import os
import sys
from pathlib import Path

from .api import application
from .audit import reconcile
from .benchmark import run_benchmark
from .errors import AnalyticsError
from .events import Event, timestamp
from .reporting import report
from .sqlite_store import SQLiteStore
from .transport import MemoryBroker, consume_messages, publish_once

ROOT = Path(__file__).resolve().parents[1] / "prototype" / "ads-analytics-v2"
DEMO_CUTOFF = "2026-11-06T01:10:00Z"
DEMO_SETTLED = "2026-11-06T01:00:00Z"


def local_path(value):
    candidate = Path(value)
    candidate = (candidate if candidate.is_absolute() else ROOT / candidate).resolve()
    if not candidate.is_relative_to(ROOT / ".local"):
        raise AnalyticsError("CLI_STORAGE_MUST_BE_UNDER_PROTOTYPE_LOCAL")
    return candidate


def load_source(path, media_id):
    path = Path(path)
    if path.stat().st_size > 10_000_000:
        raise AnalyticsError("SOURCE_FILE_TOO_LARGE")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list) or len(data) > 10_000:
        raise AnalyticsError("INVALID_SOURCE_BATCH")
    return [Event.from_v1_record(value, media_id=media_id) for value in data]


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def demo(output):
    output = local_path(output)
    if output.exists():
        raise AnalyticsError("DEMO_OUTPUT_ALREADY_EXISTS")
    output.mkdir(parents=True)
    source = SQLiteStore(output / "source.sqlite3")
    projection = SQLiteStore(output / "report.sqlite3")
    try:
        events = load_source(ROOT / "fixtures/source-events.json", "muther-public-DEMO")
        source.ingest(events, enqueue=True, now=0)
        broker = MemoryBroker()
        counts = publish_once(source, broker, clock=lambda: timestamp(DEMO_CUTOFF).timestamp())
        messages = (
            broker.messages + broker.messages[:1]
        )  # deliberately replay one transport message
        inserted = consume_messages(projection, messages)
        result = report(
            projection.read_events(),
            cutoff=DEMO_CUTOFF,
            settled_through=DEMO_SETTLED,
            include_details=True,
        )
        projection.save_report(result)
        evidence = {
            "mode": "OFFLINE_SIMULATION_NOT_KAFKA_OR_MONGODB_OR_SPARK",
            "source_events": len(events),
            "transport_messages_including_replay": len(messages),
            "unique_sink_events": inserted,
            "queue": source.queue_counts(),
            "publish": counts,
            "result_totals": result["totals"],
            "report_id": result["report_id"],
        }
        write_json(output / "demo-report.json", result)
        write_json(output / "demo-evidence.json", evidence)
        return {"output": str(output), **evidence}
    finally:
        source.close()
        projection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="광고 개선 로컬 검증 도구. 운영 연결은 별도 관리 명령으로 실행합니다."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("demo", help="로컬 모의 전송·중복·성과 집계 시연")
    command.add_argument("--out", default=".local/demo")
    command = commands.add_parser("import-v1", help="승인받아 내보낸 V1 JSON 스냅샷만 읽습니다")
    command.add_argument("source")
    command.add_argument("--media-id", required=True)
    command.add_argument("--db", default=".local/import.sqlite3")
    command = commands.add_parser("report", help="명시한 관찰 종료시각 기준 조회")
    command.add_argument("--db", required=True)
    command.add_argument("--cutoff", required=True)
    command.add_argument("--settled-through")
    for name in ("date-from", "date-to", "ad-id", "placement", "media-id"):
        command.add_argument("--" + name)
    command.add_argument("--details", action="store_true")
    command.add_argument("--save", action="store_true", help="별도 V2 보고서 스냅샷 보존")
    command = commands.add_parser(
        "serve", help="loopback 전용 읽기 JSON API, 기존 ADMIN 인증과 별개"
    )
    command.add_argument("--db", required=True)
    command.add_argument("--port", type=int, default=8765)
    command.add_argument("--token-env", default="ADS_V2_REPORT_TOKEN")
    command = commands.add_parser(
        "benchmark", help="합성 입력·SQLite·모의 broker의 처리량과 중단 복구 측정"
    )
    command.add_argument("--out", required=True)
    command.add_argument("--pairs", type=int, default=1000)
    command.add_argument("--batch-size", type=int, default=100)
    command = commands.add_parser("reconcile", help="동일 관찰시각의 원본·수집·소비·집계 대조")
    command.add_argument("--source", required=True, help="허가받아 내보낸 V1 JSON")
    command.add_argument("--staged", required=True, help="수집 저장소 V2 envelope JSON")
    command.add_argument("--delivered", required=True, help="소비 저장소 V2 envelope JSON")
    command.add_argument(
        "--messages", required=True, help="동일 관찰범위의 Kafka 메시지 V2 envelope JSON"
    )
    command.add_argument("--snapshot", required=True)
    command.add_argument("--media-id", required=True)
    command.add_argument("--cutoff", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            result = demo(args.out)
        elif args.command == "benchmark":
            result = run_benchmark(
                local_path(args.out), pairs=args.pairs, batch_size=args.batch_size
            )
            write_json(local_path(args.out) / "evidence.json", result)
        elif args.command == "reconcile":

            def envelopes(path):
                path = Path(path)
                if path.stat().st_size > 10_000_000:
                    raise AnalyticsError("SOURCE_FILE_TOO_LARGE")
                values = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(values, list) or len(values) > 10_000:
                    raise AnalyticsError("INVALID_SOURCE_BATCH")
                return [Event.from_envelope(value) for value in values]

            snapshot_path = Path(args.snapshot)
            if snapshot_path.stat().st_size > 10_000_000:
                raise AnalyticsError("SOURCE_FILE_TOO_LARGE")
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8-sig"))
            if not isinstance(snapshot, dict):
                raise AnalyticsError("INVALID_REPORT_SNAPSHOT")
            result = reconcile(
                load_source(args.source, args.media_id),
                envelopes(args.staged),
                envelopes(args.delivered),
                snapshot,
                cutoff=args.cutoff,
                transport_events=envelopes(args.messages),
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["status"] == "CONSISTENT_OBSERVATION" else 1
        else:
            path = local_path(args.db)
            if args.command != "import-v1" and not path.is_file():
                raise AnalyticsError("LOCAL_DATASET_NOT_FOUND")
            path.parent.mkdir(parents=True, exist_ok=True)
            store = SQLiteStore(path)
            try:
                if args.command == "import-v1":
                    events = load_source(args.source, args.media_id)
                    result = {
                        "inserted": store.ingest(events, enqueue=True),
                        "input_events": len(events),
                        "queue": store.queue_counts(),
                    }
                elif args.command == "report":
                    scope = {
                        name: getattr(args, name)
                        for name in ("date_from", "date_to", "ad_id", "placement", "media_id")
                    }
                    result = report(
                        store.read_events(),
                        cutoff=args.cutoff,
                        settled_through=args.settled_through,
                        include_details=args.details,
                        **scope,
                    )
                    if args.save:
                        store.save_report(result)
                else:
                    if not 1024 <= args.port <= 65535:
                        raise AnalyticsError("INVALID_LOCAL_PORT")
                    from wsgiref.simple_server import make_server

                    app = application(store, token=os.environ.get(args.token_env))
                    # Never expose the prototype's token-based API as a production ADMIN endpoint.
                    with make_server("127.0.0.1", args.port, app) as server:
                        print(
                            json.dumps(
                                {
                                    "listen": f"http://127.0.0.1:{args.port}",
                                    "scope": "LOCAL_READ_ONLY_DEMO",
                                }
                            ),
                            flush=True,
                        )
                        server.serve_forever()
                    return 0
            finally:
                store.close()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except AnalyticsError as error:
        print(json.dumps({"code": str(error)}, ensure_ascii=True), file=sys.stderr)
        return 2
    except (ValueError, OSError, TypeError, KeyError):
        print(json.dumps({"code": "INVALID_LOCAL_INPUT"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
