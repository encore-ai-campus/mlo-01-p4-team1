# 독립 광고 성과 분석 V2

- 작성일: 2026-11-06
- 수정일: 2026-11-09
- 버전: 0.1.0 / 독립 프로토타입
- 상태: 로컬 판정·저장·모의 전송·조회 검증 완료. 기존 광고 서버 미연결, 운영 미적용입니다.
- 원본 작성 당시 3차 기준: `develop` / `42e5a821c937580b56b8a5a81503912ea6156c0c`
- 4차 이관 기준: `encore-ai-campus/mlo-01-p4-team1`의 `develop` / `a4155b1bebfe19318c2f20c8419fea41d32e84b7`
- 이관 브랜치/경로: `feature/AD` / `app/prototype/ads-analytics-v2/`
- [이관·변경 기록](PORTING.md)

## 기존 버전과의 관계

사용자 요청으로 기존 `ad-server/`, `frontend/`, `pipeline/`, 인프라 코드·광고 문서를 수정하지 않고 새 경로에 구현했습니다. 기존 실행기·API·환경 파일·의존성 잠금 파일에도 연결하지 않았습니다. 기존 원본 문서와 인수인계서는 그대로 보존합니다.

기존 광고에는 조기 클릭 인정·예상 토큰·반복 제한·최초 응답 불변 정책이 있습니다. V2는 그 판정을 재작성하거나 예산을 다시 계산하지 않습니다. 승인받아 내보낸 기존 이벤트 스냅샷을 받아 별도 성과 분석 결과를 만듭니다.

`ads-performance-PROPOSAL-20261106.1`은 제안 정책 버전입니다. 조기 클릭과 성과용 클릭 분리, 선행 노출일 귀속, 매체 정의, 지연/보고 마감은 사용자·담당자 확인 전 운영 정책으로 채택하지 않습니다.

## 구현 범위

| 기능 | 이번 구현 | 실제 확인 경계 |
|---|---|---|
| 이벤트 인계 | V1 문서의 필수값만 추출, 시간/판정/스키마 검사, 토큰·쿠키·guest 원문 제외 | 고정 자료·단위시험 |
| 성과 판정 | 같은 delivery의 유효 노출/클릭 연결, 조기·미연결·대기 분류, KST 노출일 귀속, 가중 합계 CTR | 로컬 실제 실행 |
| 로컬 저장 | 독립 SQLite에 이벤트·전송 대기 원자 저장, 충돌·재전송·스냅샷 보존 | 실제 SQLite 동시 점유/복구 시험 |
| 전송 | 임대·재시도·최대 횟수·DEAD·늦은 ACK 거부, 중복 전달 허용 후 sink 멱등 저장 | 메모리 broker 시험. 실제 Kafka 아님 |
| 조회 | CLI·loopback 전용 읽기 JSON API, 토큰 검사·기간/광고/위치/매체 필터 | WSGI 직접 요청 시험 |
| MongoDB 어댑터 | 기존 DB의 새 V2 컬렉션, 이벤트/대기열 트랜잭션, 명시 인덱스 준비 | 구현·정적 검사. 실제 MongoDB 미실행 |
| V1 MongoDB 읽기 | 필수 필드 projection·상한 검사·명시 스냅샷 읽기, V2 저장으로 인계 | 인터페이스 모의시험. 실접속 미실행 |
| Kafka 어댑터 | SASL_SSL/호스트 검증·acks=all·future ACK 대기, delivery_id key | 주입한 producer로 ACK 경계 시험. 실접속 미실행 |
| Spark 어댑터 | native join·groupBy·집계 결과 수집, Kafka foreachBatch·체크포인트·상한 초과 차단 | 코드·정적 검사. PySpark/Java 및 클러스터 검증 미실행 |

기존 React 관리자 화면·공개 배너·공통 ADMIN 인증은 변경하지 않았습니다. JSON 조회 API는 운영 관리자 인증의 대체물이 아니며, 실제 리포트 탭·배너 재시도 보완·공통 인증 연결은 후속 통합 대상입니다.

## 로컬 실행

4차 저장소 루트에서 아래 폴더로 이동합니다. 핵심 모듈은 Python 3.12 이상 표준 라이브러리만 사용합니다. 이미 설치된 Python 3.12 이상의 실행 파일을 선택하며, clone에 가상환경이 포함되어 있다고 가정하지 않습니다. 패키지 설치나 기존 환경 변경은 필요하지 않습니다.

```powershell
$AdsV2Python = (Get-Command python -ErrorAction Stop).Source
& $AdsV2Python -B --version  # Python 3.12 이상인지 확인합니다.
Set-Location app/prototype/ads-analytics-v2
& $AdsV2Python -B -m unittest discover -s tests -t . -q
& $AdsV2Python -B -m ads_analytics_v2 demo --out .local/demo
```

데모 폴더가 이미 있으면 덮어쓰지 않고 실패합니다. 새 시연은 `.local/demo-2` 같은 별도 경로를 지정합니다. CLI 저장 경로는 이 프로토타입의 `.local/` 아래로 제한하여 기존 DB 경로를 실수로 지정하지 못하게 했습니다.

데모는 명시한 새 폴더에 `source.sqlite3`, `report.sqlite3`, `demo-report.json`, `demo-evidence.json`을 생성합니다. 기존 광고 DB·API·AWS·Kafka·Spark에는 연결하지 않습니다.

실제 실행한 고정 시연 결과는 다음과 같습니다. 운영 광고 실적이나 성능 수치가 아닙니다.

| 항목 | 결과 |
|---|---:|
| 입력 이벤트 | 8 |
| 중복 전달을 포함한 모의 메시지 | 9 |
| sink 고유 이벤트 | 8 |
| 노출 / 연결 클릭 | 2 / 1 |
| 조기 / 미연결 / 대기 클릭 | 1 / 1 / 1 |
| 연결 기준 CTR | 50.0% |

데모 기준 `cutoff=2026-11-06T01:10:00Z`, 분류 경계 `settled_through=2026-11-06T01:00:00Z`를 사용합니다. 입력은 [고정 예시](fixtures/source-events.json)입니다.

### 파일 등록과 리포트

기존 V1 DB를 직접 수정하지 않습니다. 허가받아 내보낸 이벤트 배열 JSON을 읽고 새 V2 저장소에만 저장합니다. `media_id`는 등록된 매체 식별자를 명시해야 하며 예제 이름을 운영값으로 자동 사용하지 않습니다.

```powershell
& $AdsV2Python -B -m ads_analytics_v2 import-v1 fixtures/source-events.json --media-id muther-public-DEMO --db .local/import.sqlite3
& $AdsV2Python -B -m ads_analytics_v2 report --db .local/demo/report.sqlite3 --cutoff 2026-11-06T01:10:00Z --settled-through 2026-11-06T01:00:00Z --date-from 2026-11-05 --date-to 2026-11-06 --details
```

추가 필터는 `--ad-id`, `--placement top|bottom`, `--media-id`입니다. `--save`를 명시할 때만 V2 보고서 스냅샷을 저장하며 같은 ID의 다른 결과를 덮어쓰지 않습니다. 조회만 하는 API/CLI는 스냅샷을 자동 생성하지 않습니다.

### 로컬 읽기 API

```powershell
# ADS_V2_REPORT_TOKEN은 로컬에서 별도로 설정합니다. 채팅/공유 문서에 값을 기록하지 않습니다.
& $AdsV2Python -B -m ads_analytics_v2 serve --db .local/demo/report.sqlite3 --port 8765
```

16자 이상의 환경 변수 `ADS_V2_REPORT_TOKEN`이 없으면 시작을 거부합니다. 바인드는 `127.0.0.1` 고정이며 외부 공개 옵션은 없습니다.

- `GET /health`: 프로세스 생존만 응답. 외부 서비스 성공을 뜻하지 않습니다.
- `GET /reports?cutoff=2026-11-06T01:10:00Z`: `Authorization: Bearer <로컬 토큰>` 필요.
- 선택 query: `settled_through`, `date_from`, `date_to`, `ad_id`, `placement`, `media_id`.
- 날짜 필터는 양 끝을 함께 입력하며 양 끝 포함, 차이는 최대 366일입니다.
- 쓰기 메서드·중복/알 수 없는 query·잘못된 기간은 거부합니다. 원본 예외·접속 정보는 응답에 넣지 않습니다.

## 성과 규칙과 원장 보호

1. 기존 `COUNTED`이며 시험 데이터가 아닌 이벤트만 성과 후보입니다. V1 판정과 예상 사용량은 바꾸지 않습니다.
2. 같은 delivery의 광고·소재·위치·매체가 일치해야 합니다. 서로 다른 두 사건이 같은 delivery/type으로 모두 인정됐으면 부분 계산 대신 충돌로 차단합니다.
3. 클릭 발생 시각보다 앞서거나 같은 시각의 인정 노출이 있으면 연결합니다. 동일 시각은 원본 정밀도에서 구분되지 않으므로 동률을 허용하는 제안입니다.
4. 클릭 뒤에 실제 발생한 노출은 조기 클릭의 분모로 연결하지 않습니다. 노출을 자동 생성하지 않습니다.
5. 노출이 없으면 기본적으로 대기입니다. 운영자가 완전성을 확인하고 분류 경계를 공급한 경우에만 그 경계 이하 클릭을 미연결로 구분합니다.
6. `settled_through` 입력은 원본 수집 완료·보고 기간 확정의 증거가 아닙니다. 모든 출력 상태는 `PROVISIONAL`이며 호출자 경계 적용 여부를 따로 표시합니다.
7. 연결 클릭은 선행 노출의 KST 날짜, 조기/미연결/대기는 클릭 발생의 KST 날짜로 분류합니다. 조회 기간은 연결 후 적용해 자정 경계를 보존합니다.
8. `cutoff` 이후 수신 이벤트는 해당 관찰 결과에서 제외합니다. 나중에 자료가 도착하면 새 입력 해시·새 보고서 ID로 재계산하며 기존 스냅샷은 보존합니다.
9. CTR은 연결 클릭 합계 ÷ 유효 노출 합계 × 100이며 노출 0건은 `null`입니다. 광고별 백분율을 평균하지 않습니다.
10. 이벤트 원본의 KST 발생일 기준 진단과 노출일 귀속 성과는 의미가 다릅니다. 기존 원장과 성과 클릭 숫자가 무조건 같아야 한다고 검사하지 않습니다.

## 코드 구조

| 경로 | 역할 |
|---|---|
| `events.py` | V1 스냅샷 인계·화이트리스트·불변 V2 envelope·해시 |
| `reporting.py` | 참조 판정·노출/클릭 연결·필터·CTR·보고서 ID |
| `sqlite_store.py` | 로컬 원자 저장·임대 대기열·스냅샷 |
| `transport.py` | at-least-once 전송·ACK·실패 재시도·sink 멱등성 |
| `api.py`, `cli.py` | 독립 읽기 API·명시 로컬 실행 |
| `adapters/source_snapshot.py` | 원본 MongoDB의 제한된 읽기 스냅샷 인계 |
| `adapters/mongo.py` | 새 컬렉션만 사용하는 MongoDB 저장·대기열 |
| `adapters/kafka.py` | 명시 연결에서만 동작하는 실제 producer 어댑터 |
| `adapters/spark.py` | Spark join·분산 집계·체크포인트 기반 수신 어댑터 |

새 MongoDB 컬렉션명은 `ads_analytics_v2_events`, `ads_analytics_v2_outbox`, `ads_analytics_v2_reports`입니다. `_id` 고유키와 대기열 인덱스를 사용하며 인덱스 준비 호출도 명시적으로 분리했습니다. 기존 `ad_events`, 원장, `ad_daily_metrics`, 내장 로그에는 쓰지 않습니다.

## 인프라 적용 시 필요한 연결

원본 작성 당시 인계는 7쌍 Peering과 계정 5↔2~4의 직접 경로 부재를 전제로 했습니다. 4차 이관 기준의 [최신 토폴로지](../../../aws-infra/contracts/topology.v1.json)는 5개 계정 사이 10쌍 Peering을 정의합니다. Peering 구성은 서비스 포트·보안그룹·DB 권한·실제 연결 완료를 뜻하지 않으며, 현재 인프라 계약을 우선합니다.

아래 Control Driver 경로는 선택 어댑터의 실행 제안이며, 현재 토폴로지가 강제하는 유일한 경로가 아닙니다. 실제 배포·클러스터 연결은 미실행입니다.

```text
광고 MongoDB 원본(읽기)
  → Control의 명시 스냅샷 인계 또는 향후 합의할 지속 수집
  → V2 이벤트/전송 대기 저장
  → Control Kafka 발행기
  → Kafka / delivery_id key
  → Spark 집계 / Control Driver
  → Control Driver가 V2 MongoDB 집계 스냅샷 저장
  → 검토한 운영 ADMIN 인증을 거친 리포트 조회
```

`source_snapshot.py`는 명시한 시각까지의 제한된 재조회이며 CDC·실시간 설치 hook이 아닙니다. 동시 commit·보관 기간·누락 대조를 검증하지 않고 수집 완전성을 주장하지 않습니다. 범위가 상한을 넘으면 조용히 자르지 않고 실패합니다.

`stream_from_kafka()`는 Control Driver에서 실행하도록 작성했습니다. executor는 MongoDB에 연결하지 않습니다. 입력 micro-batch 1,000건(설정 상한 10,000), 저장 이벤트 재조회 100,000건, 집계 결과 5,000행 제한이 있으며 상한 초과 시 실패해 checkpoint 성공을 막습니다. 이는 제한된 프로토타입의 전체 재계산 방식입니다. 장기·대규모 운영에는 보관/partition·지속 수집·확정 마감·부분 재집계 설계가 필요합니다.

대기열 기본 임대는 60초, 최대 8회 재시도입니다. DEAD 항목의 운영 재처리 UI/명령과 장기 보관/정리는 아직 없습니다. 여러 호스트의 시각 차이·DB 서버 시각 기반 임대와 실제 장애 승계도 운영 적용 전에 검증해야 합니다.

- 기존 광고 DB 사용자나 Control 날씨 계정을 재사용하지 않고 V2 전용 최소 권한을 검토합니다.
- 실제 토픽·partition/복제 수·ACL·Kafka/Spark/S3A 의존성 lock·앱 start/stop·단일 writer를 합의합니다. 코드가 토픽이나 IAM·네트워크를 자동 생성하지 않습니다.
- Kafka 발행은 실제 broker ACK를 기다립니다. ACK 직후 로컬 상태 저장 전 장애는 재전송을 만들 수 있으므로 sink에서 사건 ID/해시로 중복을 제거합니다.
- 기존 로그 relay는 재활성화하지 않습니다. 분석용 V2 대기열은 새 구조이며 원본 로그 정책과 분리합니다.
- `config/host-settings.env.example`는 설정 이름의 인계안입니다. 자동 `.env` 로더/설치기가 아니며 값만 채웠다고 서비스가 시작되지 않습니다.

선택 어댑터의 `pymongo`, `kafka-python`, `pyspark`는 기본 핵심 의존성에 포함하지 않았습니다. 현재 사용한 광고 환경에는 PyMongo 4.18.3이 있고 Kafka/PySpark는 없습니다. 운영 의존성은 인프라 lock과 호환성을 확인한 별도 환경에서 고정해야 하며 이 작업은 기존 가상환경에 패키지를 설치하지 않았습니다.

## 원본 검증 결과와 미완료 범위 (2026-11-06)

- `unittest`: 56개 실행, **55개 통과·실제 Spark 1개 skip**.
- Ruff: 전체 통과. Python 소스 정적 검사와 로컬 CLI 시연을 수행했습니다.
- 주요 실제 로컬 시험: 원본 불변·비밀 필드 제외, 늦은 노출 도착·실제 조기 클릭·자정 귀속, 사건 충돌, 원자 rollback, 4개 동시 점유, ACK 후 장애 재전송, 늦은 ACK 거부, 재시도/DEAD, 조회 권한·무쓰기·오류 비노출.
- 초기 2개 시험은 Windows sandbox의 OS 임시 경로 접근 거부로 실패했습니다. 새 프로토타입의 `.local/tests/` 아래 검증된 경로만 사용하도록 수정 후 통과했습니다. 최초 Ruff의 미사용 import 1개도 제거했습니다.
- 실제 MongoDB 트랜잭션·Kafka 발행/소비·Spark 실행·S3 checkpoint·AWS 접속·SSM·운영 부하/장애·기존 관리자/공개 화면 통합은 **미실행**입니다.

이 결과는 독립판의 로컬 기능 확인이며 평가 피드백 전체 완료나 운영 배포 성공을 의미하지 않습니다. 기존 문서·코드 변경이나 실제 연결이 필요하면 해당 변경 범위를 별도로 요청받아 진행합니다.

## 4차 이관 후 확인 (2026-11-09)

광고 전용 신규 파일 26개를 feature/AD에 선별했으며 Python 소스 20개는 원본과 같습니다. 단위 테스트 55개 통과·Spark 1개 skip, Ruff 통과와 문서 링크 확인 결과를 [이관 기록](PORTING.md)에 별도로 남겼습니다. 실제 서비스 통합·DB/Kafka/Spark/AWS 실행은 수행하지 않았습니다.
