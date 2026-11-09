# 광고 시스템 개선 — 실행·검증 가이드

- 원본 작성: 2026-11-06 / 현재 개선: 2026-11-09
- 작업 브랜치: feature/AD, 이관 기준 커밋: 78452f9162e239b16e7926d2a394026fe57f9591
- 범위: 광고의 수집·전송·집계·관리자 보고 연결과 재시도·정합성·복구 검증입니다.
- 상태: 로컬 코드 연결·단위/컴포넌트 검증 완료. 실제 MongoDB/Kafka/Spark/AWS 통합·부하 시험은 수행 전입니다.
- [이관·변경 기록](PORTING.md) / [피드백별 대응](FEEDBACK.md)

## 1. 현재 구조

공통 Python 구현은 [app/ads_analytics_v2](../../ads_analytics_v2/)로 이동했습니다. 이 폴더에는 fixture·테스트·설정 예시·문서를 보존합니다. Docker에는 공통 core를 별도로 포함합니다.

~~~text
공개 배너 → 기존 광고 API → 기존 MongoDB ad_events (읽기 전용)
  → 지속 수집 → V2 발행 대기 DB(events/outbox/captures/requeues)
  → delivery_id 키·broker ACK → Kafka
  → Spark foreachBatch·중복 제거·join/groupBy
  → 별도 V2 수신 집계 DB(events/reports)
  → 기존 ADMIN 인증·성과 조회 API → 관리자 성과 리포트
~~~

원본·발행 대기·수신 집계의 DB 이름을 분리합니다. producer에 데이터가 있다는 이유로 소비 완료를 표시하지 않습니다. 기존 COUNTED·예산·원장·최초 응답을 보존합니다. 원본 계정은 읽기 전용으로 준비합니다. V2의 신규 컬렉션은 ads_analytics_v2_events/outbox/reports/captures/requeues이며 실제 최소 권한·replica set 트랜잭션 지원은 환경에서 확인해야 합니다.

## 2. 개선 내용

| 항목 | 현재 동작 |
|---|---|
| 지속 수집 | cutoff와 event_id 상한을 고정한 제한 페이지 순회, 완료 후 재순회로 늦은 commit 회수 |
| 재시작 | 진행상태·사건·outbox의 원자 저장, CAS 충돌 검사 |
| 전송·소비 | source/sink 분리, 같은 사건 ID 재전송, ACK·임대·중복 저장 검사 |
| DEAD 운영 | 목록 조회, 명시 ID 재처리와 이력 저장. 임대 중·비DEAD 재처리 거부 |
| 성과 판정 | 선행/동시 노출 연결, 조기·미연결·대기 구분, KST 노출일 귀속, 합계 기준 CTR |
| 관리자 화면 | 기존 광고 권한 안의 성과 탭, 기간·광고·위치·매체·관찰시각, 일별 추이·상세·지연 |
| 배너 | 408/425/429·네트워크·5xx 및 Retry-After 재시도, 동일 ID/본문/토큰, 동시 전송·언마운트 차단 |
| 원본 대조 | 원본·수집·메시지·소비·보고서, 사건 지문·입력 해시·정책·스냅샷 해시 비교 |

순회 수집은 CDC나 원본 보관 완전성 증명이 아닙니다. 원본 보관기간이 전체 순회·재시도보다 길어야 합니다. source_completeness_verified는 false를 유지합니다.

보고 정책 ads-performance-PROPOSAL-20261106.1은 제안 상태이며 결과는 PROVISIONAL입니다. 기존 원장이 인정한 조기 클릭과 성과용 연결 클릭을 구분합니다. 미집계(PENDING), 실제 0건, 조회 오류를 구분하며 노출 0건의 CTR은 null입니다.

## 3. 외부 서비스 없는 로컬 검증

4차 저장소 루트에서 이미 설치된 Python 3.12 이상을 사용합니다.

~~~powershell
$AdsPython = (Get-Command python -ErrorAction Stop).Source
& $AdsPython -B --version
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = (Resolve-Path app).Path
& $AdsPython -B -m unittest discover -s app/prototype/ads-analytics-v2/tests -t app/prototype/ads-analytics-v2 -q
& $AdsPython -B -m ads_analytics_v2 demo --out .local/demo-new
& $AdsPython -B -m ads_analytics_v2 benchmark --out .local/benchmark-new --pairs 1000 --batch-size 100
~~~

출력은 이 검증 폴더의 .local 아래로 제한하며 기존 demo/benchmark 폴더를 덮어쓰지 않습니다. SQLite·실행 결과·가상환경은 Git 제외입니다.

benchmark는 합성 입력·실제 SQLite·메모리 broker로 저장·발행·소비·집계·대조를 측정합니다. ACK 직후 outbox 완료 전 중단을 주입하고 파일을 다시 열어 복구합니다. 환경·입력/배치 크기·시간·처리량·배치 p50/p95와 누락/중복을 기록합니다. 실제 Kafka/MongoDB/Spark 성능이나 사건별 종단 지연이 아니며 입력 속도는 제한하지 않은 조건입니다.

### 같은 관찰범위 대조

허가받아 내보낸 V1 배열과 V2 envelope 배열을 사용합니다. 메시지 자료는 같은 범위에서 확보한 Kafka 내보내기이며 토큰·쿠키·guest 원문을 포함하지 않습니다.

~~~powershell
& $AdsPython -B -m ads_analytics_v2 reconcile --source source-v1.json --staged staged-v2.json --messages messages-v2.json --delivered delivered-v2.json --snapshot report.json --media-id reviewed-media --cutoff 2026-11-09T00:00:00Z
~~~

MISMATCH는 종료 코드 1, 입력/설정 오류는 2입니다. 메시지 자료를 발행 대기 테이블로 대체하지 않습니다. CLI 파일당 상한은 10MB·배열 10,000개이며 임의로 잘라 통과시키지 않습니다. benchmark의 최대 10,000쌍(20,000개 사건)은 별도 한도입니다.

## 4. 실제 실행 준비

[빈 설정 예시](config/host-settings.env.example)와 광고 backend의 .env.example을 참고합니다. 실제 값을 Git·채팅·명령 인자·로그에 기록하지 않습니다. ADS_V2_ENABLED는 기본 0입니다.

1. 원본 읽기·V2 producer/sink 권한, 원본 보관과 replica set을 확인합니다.
2. 매체·source_id·보고 기준을 합의합니다. 입력원이 달라지면 식별자·저장 버전을 구분합니다.
3. 기존 Kafka 토픽·partition/복제 기대값·ACL·CA·SASL_SSL을 준비합니다. 자동 토픽 생성 없이 메타데이터를 대조합니다.
4. Spark/Java, 검증된 Kafka·S3A JAR, 별도 s3a:// checkpoint·단일 writer·복구 규약을 준비합니다.
5. 같은 cutoff의 원본·메시지·집계를 대조하고 실제 관리자 화면까지 확인합니다.

광고 backend 환경에서 수집/발행은 분리 프로세스로 실행합니다.

~~~text
python manage.py run_ads_analytics --stage collect --watch
python manage.py run_ads_analytics --stage publish --watch
python manage.py run_ads_analytics --stage dead
python manage.py run_ads_analytics --stage requeue --event-id reviewed-dead-id
~~~

Spark는 Django에 의존하지 않는 진입점을 제공합니다.

~~~text
python -m ads_analytics_v2.worker_cli --check-config
spark-submit app/ads_analytics_v2/worker_cli.py
~~~

환경 구조 확인은 외부 연결 성공이 아닙니다. 실행 중 JAR를 자동 다운로드하지 않습니다. 원본 삭제·checkpoint 초기화를 복구 방법으로 사용하지 않습니다.

[Docker](../../docker/ads.Dockerfile)는 core를 포함한 앱 이미지와 선택 analytics-worker/analytics-spark target을 제공합니다. [Compose](../../compose.yaml)의 ads-analytics profile도 명시적으로 켜며 기본 V2는 꺼져 있습니다. 설정·토픽·CA·권한이 준비되지 않은 구성을 완성된 파이프라인으로 표시하지 않습니다.

## 5. 관리자 API와 남은 확인

GET /api/admin/ads/analytics는 기존 활성 ADMIN·광고 권한을 검사합니다. 허용 query는 date_from/date_to/ad_id/placement/media_id/cutoff입니다. 날짜 양 끝을 함께 지정하며 차이는 최대 366일입니다. 중복·알 수 없는 query는 거부합니다.

원본을 요청 시 재집계하지 않고 cutoff 이하 최신 전체 materialized snapshot을 읽습니다. report_available=false는 미집계, true와 0 카운터는 빈 실적입니다. DB/설정 오류는 별도 오류이며 응답은 no-store입니다.

실제 브라우저→공통 인증→광고 DB→Kafka→Spark→리포트 통합은 수행 전입니다. 기존 AWS 광고→PostgreSQL 접속 허용 누락도 이 앱 변경으로 해결됐다고 간주하지 않습니다. 최신 4차 인프라 계약을 우선하고 실환경 통신·권한을 별도로 검증합니다. 과거 이관 기록과 현재 개선 결과는 PORTING에서 날짜별로 구분합니다.

## 6. 2026-11-09 후속 수정과 재검증

67f230f 이후 사용자 요청으로 등록·수정·공개 노출·성과 조회의 오류를 수정했습니다.
앞 절의 최초 관측과 현재 결과는 [후속 검증 기록](FEEDBACK.md#4-2026-11-09-후속-오류-수정과-기능별-재검증)을 구분합니다.

- CSRF는 설정된 플랫폼/기존 쿠키만 요청별로 검사하며 Origin/Referer·토큰 검사를 유지합니다.
- 등록 실패 재시도는 같은 멱등 키, 성공 후 새 등록은 새 요청으로 구분합니다. 수정하지 않은 게시 시각의 초·밀리초는 보존합니다.
- 브라우저가 정확하게 표현하지 못하는 정수는 저장 전에 거부합니다. 백엔드 int64·운영 예산 상한을 새 정책으로 바꾸지 않습니다.
- 일반 요청은 본문 포함 15초, 배너 한 시도는 5초입니다. 배너 3회·전체 5분 수명·Retry-After는 유지합니다.
- Mongo 스냅샷은 마이크로초 정밀도 UTC 키를 사용하며 기존 BSON 자료는 본문·해시를 덮어쓰지 않고 호환 조회합니다.
- 생산 materialization은 iter_events와 임시 SQLite spool을 사용합니다. 누적 100,001건 이후에도 전체 해시와 같은 delivery의 노출·클릭 연결을 보존합니다.
- 생산 경로의 암묵적인 전체 사건 100,000건 상한을 제거했습니다. 참조용 report/read_events 및 CLI/audit의 명시 입력 방어는 별도입니다.
- 최종 집계 5,000행 제한은 유지합니다. 전체 사건에 비례하는 처리 시간·임시 디스크가 필요하므로 OS 임시 경로의 여유·권한을 확인합니다. 무제한 확장·운영 처리량 보장이 아닙니다.
- 수집 다음 페이지·가득 찬 발행 batch는 짧은 대기 후 계속합니다. 순회 완료·완전 idle만 설정 주기 대기, 지연 발행은 1초 이내 재확인합니다.
- Kafka 공개 CA는 검증한 PEM 본문으로 driver/executor에 전달합니다. 인증서 개인키·실제 CA 파일·자격증명은 Git에 넣지 않습니다.
- Docker 광고 프로필은 기존 스키마 준비와 필수 예산 worker를 연결합니다. [실행 안내](../../docker/README.md)를 따릅니다.

### 재실행 명령

~~~text
# 광고 backend의 기존 Python 환경. 설치된 mongod, 새 loopback 포트·UUID DB 사용
python -B tests/run_ads_mongo_regression.py

# app/frontend. 설치된 Chromium 브라우저, 합성 API만 사용
pnpm test:ads-browser
pnpm test
pnpm build
~~~

브라우저 경로는 ADS_TEST_BROWSER_PATH로 지정할 수 있으며 자동 다운로드하지 않습니다.
Mongo 검증은 임시 DB·프로세스를 정리하고 app/.local에 비공개 로컬 결과를 남깁니다.
이번 DB 관측은 설치된 MongoDB 8.3.7이며 Docker 후보 8.0.17의 이미지 검증을 대신하지 않습니다.
