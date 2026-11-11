# 광고 성과 분석 V2 독립 구현 변경 라우팅

- 작성일: 2026-11-06
- 수정일: 2026-11-09
- 작성자/담당: Codex / 사용자 요청에 따른 별도 개선판 작성
- 78452f9 이관 당시 범위: 기존 광고 코드·문서를 보존한 독립 성과 분석 V2, 로컬 시연·검증 및 선택 어댑터
- 원본 작성 당시 3차 기준: `develop` / `42e5a821c937580b56b8a5a81503912ea6156c0c`
- 4차 이관 기준: `encore-ai-campus/mlo-01-p4-team1`의 `develop` / `a4155b1bebfe19318c2f20c8419fea41d32e84b7`
- 이관 브랜치/경로: `feature/AD` / `app/prototype/ads-analytics-v2/`
- 이관 당시 상태: 독립 프로토타입 로컬 검증. 기존 서버·DB·프론트·AWS 미연결/미적용
- 현재 로컬 개선: 아래 광고 시스템 개선 절과 FEEDBACK.md를 따릅니다. 실제 외부 서비스 통합은 수행 전입니다.
- [코드·실행·인계 안내](README.md)

## 기준 → 원래 코드 → 이번 독립판

| 기준/근거 | 원래 코드·계약 | 이번 구현 |
|---|---|---|
| 2026-10-27 평가서, 평가 대상 `f94f2f202182` | 원본 작성 당시 해당 커밋이 로컬 객체에 없어 평가 이후 개선 여부를 단정하지 않았음. 2026-11-09 이관 전 3차 main이 `f94f2f2021824eb7a0a686a2991759ed98d98a9e`임을 별도로 확인 | 수집/집계/조회·CTR·정합성 대응용 독립 분석 프로토타입. 평가 통과를 주장하지 않음 |
| `docs/handoff/ads/01_광고_화면_API와_측정_계약.md`, `03_광고_인프라와_검증_인계.md`, `05_광고_간소화_통합과_추가_인계.md` | 조기 클릭 인정·예상량·발생일 KST·최초 판정/응답 불변 | 원본 COUNTED/예산은 보존. 별도 제안 성과 정책으로 선행 노출일 귀속과 미연결/대기/조기를 구분 |
| 기존 제품 기준 4문서·저장 계약 | 음악/추천·광고·날씨·피드백의 역할·저장 경계 | 기존 계약이나 태스크/단계 범위 변경 없이 독립 폴더로 분리 |
| `ad-server/backend/domains/ads/public_service.py:record_event` | MongoDB 이벤트·원장·로그 원자 저장 | 허가받아 내보낸 snapshot만 읽는 Event 변환/선택 read-only source adapter. 원래 저장 경로에 hook 설치 없음 |
| `ad-server/backend/repositories/ads_policy_mongo.py:usage_rows/metrics_summary` | 기존 5분 예산·운영 요약 | 새 V2 이벤트/대기열/보고서 컬렉션 제안. 기존 집계를 덮어쓰지 않음 |
| `pipeline/kafka_config.py`, 기존 피드백 Spark 경로 | 기존 서비스의 Kafka·Spark 접속/처리 | 독립 producer·native Spark join/groupBy·foreachBatch/체크포인트 어댑터. 실제 클러스터 미실행 |
| `docs/handoff/infra/APPLICATION-CONTRACT.md`, `TEAM-HANDOFF.md`, `TEAM-ACCESS.md` | 과거 인계의 7쌍 Peering·직접 경로 부재 전제. 4차 기준은 10쌍이며 포트·권한·실제 연결은 별도 검증 | Control의 발행기·Driver 저장은 선택 실행안으로 보존. 최신 4차 인프라 계약을 우선하며 SG/IAM/토픽/인덱스 설치·앱 배포 미실행 |

원문은 기준 커밋의 2026-10-14~16 인계 자료이며 현재 운영 완료의 증거로 사용하지 않습니다. 원문·기존 광고 문서에 링크나 변경 이력을 덧붙이지 않았습니다. 실제 입력 모델/매체/보고 기준은 제안 상태입니다.

## 신규 파일과 핵심 함수

| 새 경로 (`prototype/ads-analytics-v2/` 아래) | 함수/클래스와 변경 이유 |
|---|---|
| `ads_analytics_v2/events.py` | `Event.from_v1_record/from_envelope`: 인계 필드 whitelist·스키마/시각/판정 검사·원본 해시 |
| `ads_analytics_v2/reporting.py` | `prepare/report/assemble`: 불변 사건 충돌·노출/클릭 연결·기간/광고/위치/매체·가중 CTR·보고 ID |
| `ads_analytics_v2/sqlite_store.py` | `SQLiteStore`: 로컬 이벤트/대기열 원자 저장·임대 fencing·불변 스냅샷 |
| `ads_analytics_v2/transport.py` | `publish_once/consume_messages`: broker ACK 확인 이후 상태 저장·at-least-once와 sink 멱등성 |
| `ads_analytics_v2/api.py`, `cli.py`, `__main__.py` | local-only JSON GET·명시 CLI·보호된 `.local/` 저장·고정 데모 |
| `adapters/mongo.py`, `source_snapshot.py` | 새 컬렉션 저장·명시 인덱스 준비, V1 source 필수 필드의 제한된 read-only 인계 |
| `adapters/kafka.py`, `spark.py` | 실제 네트워크는 명시 호출에만 연결. ACK/TLS·native join/분산 집계·Driver sink·상한/체크포인트 |
| `tests/`, `fixtures/source-events.json` | 경계·충돌·동시성·복구·권한·고정 예시. 실제 Spark 검사는 의존성 없으면 skip |
| `README.md`, `pyproject.toml`, `.gitignore`, `config/host-settings.env.example` | 별도 실행·의존성/보관·실제 미검증·제안 규약·인계 설명 |

프로토타입의 세부 파일은 README 구조표와 코드에 있습니다. 기존 함수·환경·라우팅·의존성은 수정하지 않았습니다. 새 API `/reports`는 loopback 전용 샘플이며 공통 ADMIN 인증을 대신하지 않습니다.

## API·저장·송수신 흐름

- 입력: Event V2 envelope에 event/delivery/ad/creative/placement/media ID, 종류·발생/수신 시각·원본 판정·정책/스키마 버전을 고정합니다. 원본 토큰·쿠키·guest 정보·예산 금액은 발행하지 않습니다.
- 로컬: 명시 JSON import → 새 SQLite 이벤트/대기열 → 메모리 broker/중복 전달 → 별도 sink → 성과 스냅샷·JSON 조회.
- 배포 제안: 승인된 원본 snapshot/향후 합의할 지속 인계 → Control의 V2 MongoDB 대기열 → Kafka → Spark/Control Driver → 새 MongoDB 성과 스냅샷. 실제 source capture 자동화는 연결 대기입니다.
- 새 저장: `ads_analytics_v2_events`, `ads_analytics_v2_outbox`, `ads_analytics_v2_reports`. 기존 광고/원장/내장 로그·운영 집계에는 쓰지 않습니다.
- 오류: 사건/불변 snapshot 충돌, 상한 초과, 잘못된 입력, 권한·임대 만료를 구분하고 접속 URI/비밀값을 응답에 노출하지 않습니다.
- 송수신 조율: B 광고 저장/검수·트랜잭션/공통 인증, C 공개 배너·관리 리포트 화면 연결, INF Control/클러스터·DB 권한·checkpoint/단일 writer, PL 조기 클릭/매체/마감 정책. 실제 일정·배정은 미정입니다.

## 원본 검증 결과·보존·미실행 (2026-11-06)

- 변경 전후 기존 추적 파일 1,063개의 경로/내용 합산 SHA-256이 `c62476c5932b81fe37b6135a04ecb940bc99f2c451d94ef5f8cba354fe0a328d`로 일치했습니다. 사용자 기존 광고 예시 수정도 기준에 포함해 그대로 보존했습니다.
- `python -B -m unittest discover -s tests -t . -q`: 56개 실행, 55개 통과·실제 Spark 1개 skip. 실제 PostgreSQL/MongoDB/Kafka에 접속하지 않았습니다.
- `python -B -m ruff check --no-cache .`: 통과.
- 고정 demo: 입력8·메시지9(재전송1)·고유 sink8, 노출2·연결 클릭1·조기1·미연결1·대기1·CTR50%. `.local/demo/`의 SQLite/JSON은 이 시연 전용이며 운영 실적이 아닙니다.
- 최초 실패: OS sandbox 임시 폴더 권한 때문에 파일 시험2개 실패, 프로토타입 `.local/tests/`의 검증된 경로로 옮겨 통과. 최초 Ruff 미사용 import1개도 제거했습니다.
- 최종 확인: Python 소스20개 AST 파싱 통과, README/라우팅 상대 링크 누락0·행 끝 공백/충돌 표식0, 기존 추적 파일 보존 일치입니다. `git diff --check`는 통과했고 기존 광고 예시 문서의 LF/CRLF 안내만 있었습니다.
- CLI 추가 확인: 2026-11-05 KST만 조회했을 때 자정을 넘긴 클릭이 선행 노출일에 연결되어 노출1·연결 클릭1·CTR100%로 조회됐습니다. 단위시험·데모와 별개로 실제 CLI 경로를 확인했습니다.
- 미실행: 실제 MongoDB transaction·Kafka ACK/소비·Spark/Java·S3 checkpoint·SSM/AWS·운영 부하/복구·브라우저→실제 DB 전체 흐름. PyMongo만 기존 실행 환경에 있고 Kafka/PySpark는 없어 별도 운영 의존성 lock/검증이 필요합니다.
- 기존 광고 문서·수령 원문·제품 문서·코드·실제 `.env`·Git 인덱스는 변경하지 않았습니다. 커밋·푸시·배포도 하지 않았습니다.

## 남은 연결과 수용

기존 ADMIN 리포트 화면/인증과 배너 재시도 보완, 지속 source capture/누락 대조, 매체 식별·조기 클릭·확정 마감, 장기 데이터 보관/부분 재집계와 DEAD 운영 재처리, DB 실제 권한/스키마, Control 프로세스 설치·시각 차이·HA·실제 클러스터·부하 시험이 남았습니다. 입력 상한/제안 정책을 완성된 운영 규약으로 해석하지 않습니다.

## 4차 광고 전용 이관 범위 (2026-11-09)

- 관련 Issue: [#13 광고 시스템 개선 및 평가 피드백 대응](https://github.com/encore-ai-campus/mlo-01-p4-team1/issues/13)

- 최신 4차 develop에서 분기한 `feature/AD`에 독립 광고 프로토타입 25개 파일과 이 이관 문서만 선별합니다. 저장소의 공개 소스 경계에 따라 과거 app/docs/development 경로 대신 프로토타입 안에 이관 기록을 둡니다.
- Python 소스·합성 fixture는 원본 내용을 유지합니다. 문서의 실행 경로·기준 커밋·인프라 전제를 이관 기준에 맞춥니다.
- 설정 예시는 `config/host-settings.env.example`로 이름을 맞춰 비밀값 없는 예시만 추적합니다. 실제 `.env`, `.local/`, SQLite·시연 결과·캐시·가상환경·운영 이벤트는 제외합니다.
- 기존 광고 런타임·프론트·Docker·공통 인증·AWS 인프라 및 별도 신청/운영 모듈은 이번 이관에서 변경하지 않습니다. 이전 검토의 광고 통신/DB 접속·계정 오류는 후속 작업입니다.
- 인프라 정본은 [4차 토폴로지](../../../aws-infra/contracts/topology.v1.json)와 해당 develop의 서비스 계약입니다. 파일 이관은 배포·통합·정책 승인 완료가 아닙니다.
- 이관 후 검증 결과는 아래에 원본 검증 이력과 구분해 기록합니다.

### 이관 후 검증 (2026-11-09)

- Python 3.12.10에서 단위 테스트 56개 중 55개 통과·실제 Spark 1개 skip을 확인했습니다. PySpark/Java·클러스터 시험은 수행하지 않았습니다.
- Ruff 정적 검사 통과. Python 소스 20개 AST 파싱 및 원본 바이트 일치를 확인했습니다. 합성 fixture·설정 예시의 내용도 원본과 같습니다.
- 이관 문서 상대 링크 모두 유효합니다. 기존 4차 추적 파일 수정 없이 광고 전용 신규 파일 26개만 선별합니다.
- 테스트가 만든 .local 임시 자료는 Git 제외 대상입니다. 기존 3차 원본과 4차 런타임·인프라는 변경하지 않았습니다.

## 광고 시스템 개선 — 미반영 피드백 보완 (2026-11-09)

### 판단과 변경

78452f9는 독립 분석 모듈의 이관이었으며 전체 광고 개선 완료가 아니었습니다. 이번에는 사용자 요청에 따라 지속 수집·원본 대조·관리자 보고·배너 전송·실행 경로와 검증 도구를 연결했습니다. 전체 작업명은 광고 시스템 개선, 분석 core는 그 하위 모듈입니다.

- [공통 core](../../ads_analytics_v2/)를 app 루트로 이동해 실제 앱/Docker에서 import합니다.
- [pipeline](../../ads_analytics_v2/pipeline.py): 원본 읽기→진행상태/발행 대기→broker→별도 sink→materialized snapshot입니다. consumer 없이 보고서를 생성하지 않습니다.
- [수집](../../ads_analytics_v2/adapters/source_snapshot.py): 고정 cutoff/ID 상한·CAS 진행상태·완료 후 재순회로 중단과 늦은 commit을 처리합니다.
- [관리자 API](../../ad-server/backend/api/ads_analytics.py): 기존 ADMIN 권한으로 저장 스냅샷만 읽고 필터·합계/CTR을 계산합니다.
- [화면](../../frontend/src/features/admin-ads/AdAnalytics.tsx): 기간·광고·위치·매체·KST 관찰시각, 추이·상세, 미활성/미집계/0건/오류·지연·잠정 정책을 구분합니다.
- [배너 전송](../../frontend/src/features/ad-banners/adEventTransport.ts): 동일 사건/본문/토큰, 408/425/429·네트워크·5xx와 Retry-After, 중복/언마운트 처리를 보완했습니다.
- [운영 명령](../../ad-server/backend/domains/ads/management/commands/run_ads_analytics.py): collect/publish/spark/dead/requeue입니다. [Spark driver](../../ads_analytics_v2/worker_cli.py)는 Django 없이 실행할 수 있습니다.
- [대조](../../ads_analytics_v2/audit.py)·[측정](../../ads_analytics_v2/benchmark.py)은 입력·메시지·sink·스냅샷 및 실제 로컬 복구/처리량을 확인합니다.
- 원본·발행 DB·수신 DB 분리, timezone-aware Mongo, 개발 origin allowlist, Kafka 계약·JAR·CA 검증을 추가했습니다. 기존 AWS 자원·원장·계정/매장 코드는 변경하지 않았습니다.

### 검증 결과

- 분석 전체 80개: 79개 통과, 실제 Spark 1개 skip.
- 광고 API·명령·인증 bridge·오프라인 수집→관리자 HTTP 연결: 56개 통과.
- 광고/공유 API/화면 관련 38개, TypeScript·Vite build 통과.
- Python 정적 검사와 변경 형식 검사 통과.
- 합성 2,000건, ACK 직후 중단·저장소 재개: 전송 2,001회/고유 저장 2,000건/전체 재전송 후 추가 0건. 공급된 원본·메시지·집계와 입력/정책/스냅샷 해시가 일치했습니다.
- 측정은 Windows AMD64·Python3.12.10·SQLite·메모리 broker입니다. 약20.953초/95.45고유사건·초, publish 100건 배치 p50약1024.10ms/p95약1203.56ms입니다. Kafka/Spark 성능이나 사건별 종단 지연이 아닙니다.
- 전체 프론트의 앞선 검사에서는 기존 시네마틱 checkout CRLF 해시 2개가 실패했습니다(167/169 통과). 원본 Git blob·LF 정규화와 일치하고 광고 변경 diff가 없어 해당 자산은 수정하지 않았습니다. 이후 광고 추가 테스트까지 포함한 관련 38개는 통과했습니다.

### 미실행·남은 완료 조건

실제 MongoDB transaction·Kafka 발행/소비·Java/Spark·S3A checkpoint·Docker 빌드·공통 인증 포함 실제 브라우저 종단 흐름·클러스터 부하/장애·AWS 배포는 미실행입니다. 보고 정책·원본 보관·매체를 합의하고 실제 입력과 같은 cutoff로 대조해야 합니다. 기존 AWS 광고→PostgreSQL 허용 누락은 앱 개선으로 해결됐다고 표시하지 않습니다.

앞선 로컬 개선 단계에서는 Git 커밋·푸시·Issue 수정·배포를 하지 않았습니다. [피드백 대응표](FEEDBACK.md)와 [실행 가이드](README.md)에 현재 범위와 다음 검증을 연결했습니다.

### 후속 Git 반영 (2026-11-09)

- 변경 묶음: 광고 시스템 개선 및 평가 피드백 대응 (#13).
- 대상 브랜치: feature/AD. 최신 개선 코드·테스트·환경 예시·실행/검증 문서를 함께 반영합니다.
- 이전 78452f9의 독립 모듈 이관 이력은 보존합니다. 이번 범위는 지속 수집·관리자 보고·전송 안정성·정합성/복구 검증을 포함합니다.
- main/develop 직접 반영·실제 배포와 실환경 검증은 이 작업에 포함하지 않습니다.

### 병합본 재검증 이후 로컬 오류 수정 (2026-11-09)

PR #14 병합본과 67f230f의 광고 파일 동일성을 확인한 뒤 사용자가 등록부터 기능별 재검증과
확인된 오류 전체 수정을 요청했습니다. 인증·새 등록 intent·시각·정수·timeout·리포트 응답,
Mongo 정밀도·기간·누적 집계·반복 작업·예산 worker·초기화·Spark CA 전달을 수정했습니다.
기존 업무 판정·원장·schema manifest·AWS 인프라는 보존합니다.

최종 회귀는 backend 464개(실제 격리 Mongo 전용 47개 포함) 전부 통과,
core 92개 통과/실제 Spark 1개 skip, frontend 210개 전부 통과,
실제 Edge+합성 API 11개·TypeScript·Vite build 통과입니다.
시험 격리·Windows generated asset 줄바꿈도 정책·원본 hash를 보존하며 정리했습니다.
자세한 시나리오·제한은 [후속 기록](FEEDBACK.md#4-2026-11-09-후속-오류-수정과-기능별-재검증)을 따릅니다.

위 결과는 Git 반영 전의 로컬 수정·검증 이력입니다.

### 후속 커밋·푸시 인계 (2026-11-09)

사용자가 검증된 수정 내용의 커밋과 feature/AD 푸시를 요청했습니다.
기존 Issue #13에 연결해 광고 시스템 오류 수정 및 기능별 검증 보완이라는 이름으로
코드·회귀 시험·실행 문서·이 기록을 함께 반영합니다.
실제 Kafka/Spark·S3A·Docker/AWS 배포, 새 PR 생성·기존 PR 변경·병합은 이 요청 범위에 포함하지 않습니다.
