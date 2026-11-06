# 3단계 Docker 분산 통합 테스트 가이드

**오직 개인 로컬 개발용. AWS·운영 DB·외부 서비스 연결 금지.** 모든 서비스는 `mlo-p4-app-local` 전용 Bridge 네트워크에 실행됩니다. [기본 Docker 가이드](../README.md) 참조.

## 구성

| 오버레이 | 구성 |
|---|---|
| `compose.integration.yaml` | Kafka 4.1.2 KRaft 3노드, ES 8.19.23 3노드+HAProxy, Spark 4.1.3 Master 1 + Worker 2, 합성 날씨/피드백 검증 |
| `compose.spark-ha.yaml` | 선택적 ZooKeeper 3노드와 Spark Master 2개(리더/대기), Worker에 Master 주소 2개 등록 |

- Kafka 토픽: `weather.v1.local`, `feedback.v1.local`. 3파티션, 복제3, 최소 ISR2
- ES 인덱스: `weather-stream-current-v1-local` (로컬 데이터만)
- 메모리: Docker Desktop 할당 약 7.748 GiB 기준. 전부 Mac 한 대에 있으므로 실제 3AZ HA에 대한 증거는 아닙니다.
- 보안: 개발용 Kafka PLAINTEXT, ES 무인증/비TLS. Kafka·ZooKeeper·PG·Mongo 포트는 호스트에 공개되지 않습니다. 테스트 완료 후 중지 권장.

## 실행 1 — 기본 앱

```bash
cd $HOME/project/mlo-01-p4-app-integration/app
python3 docker/init_dev_env.py
mkdir -p .local .local-preview-logs
docker compose up -d --wait postgres api frontend mailpit
docker compose --profile ads up -d --wait mongo mongo-init ads
```

초기 데이터베이스에서만 `python -m backend.manage migrate --noinput` 및 광고 `python manage.py migrate --noinput`을 실행합니다. 새 DB 외에는 절대 마이그레이션하지 않습니다.

## 실행 2 — 클러스터·토픽·로컬 지역 매핑

```bash
docker compose -f compose.yaml -f compose.integration.yaml config --quiet
docker compose -f compose.yaml -f compose.integration.yaml up -d --wait \
  kafka-01 kafka-02 kafka-03 es-01 es-02 es-03 es-router \
  spark-master spark-worker-01 spark-worker-02

docker compose -f compose.yaml -f compose.integration.yaml --profile tools run --rm --no-deps \
  integration-tool python docker/integration/smoke_stream.py prepare

docker compose -f compose.yaml -f compose.integration.yaml up -d --wait api
docker compose -f compose.yaml -f compose.integration.yaml exec -T -e MLO_LOCAL_INTEGRATION=1 \
  api python docker/integration/prepare_kma_regions.py --apply
```

로컬 `weather_mapping`에는 공식 KMA 행정 지역(읍·면·동 3564개 등) 3836행만 적재합니다. **리/도로명 원본 미포함**, 운영 DB 미연결.

## 실행 3 — 기존 날씨 Spark 실제 처리

```bash
docker compose -f compose.yaml -f compose.integration.yaml --profile tools run --rm --no-deps \
  integration-tool python docker/integration/smoke_stream.py publish --marker manual-complete-001

docker compose -f compose.yaml -f compose.integration.yaml --profile stream build spark-stream-weather
docker compose -f compose.yaml -f compose.integration.yaml --profile stream run --rm --no-deps \
  spark-stream-weather

docker compose -f compose.yaml -f compose.integration.yaml --profile tools run --rm --no-deps \
  integration-tool python docker/integration/smoke_stream.py verify
python3 docker/smoke_test.py --ads
```

합성 날씨 75건에는 KMA 원본이 아닌 가상 관측 8항목, 6시간 예보 각 11항목, 현재 구름 1항목만 포함됩니다. 동일 격자 `58_125`(신도림동)로 저장됩니다. 해당 API는 데이터가 유효한 시간 안에서만 `recommendation_ready=true`가 됩니다.

## 실행 4 — 실제 피드백 Outbox → Kafka → DB 소비 경로

```bash
docker compose -f compose.yaml -f compose.integration.yaml exec -T -e MLO_LOCAL_INTEGRATION=1 \
  api python docker/integration/smoke_feedback.py prepare

docker compose -f compose.yaml -f compose.integration.yaml exec -T \
  api python -m backend.manage relay_feedback

docker compose -f compose.yaml -f compose.integration.yaml exec -T -e MLO_LOCAL_INTEGRATION=1 \
  api python docker/integration/smoke_feedback.py verify
```

임의 UUID의 `is_test=true` 이벤트만 새 로컬 PostgreSQL에 기록합니다. Kafka 메시지를 실제 소비한 뒤 기존 `pipeline.feedback.process.apply_record`를 사용하여 offset 저장·투영·중복 방지를 검증합니다. **상시 실행형 피드백 Spark Job을 완료했다고 주장하지 않습니다.**

## 실행 5 — 선택적 Spark Master HA (ZooKeeper)

```bash
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  --profile spark-ha up -d --wait \
  zookeeper-01 zookeeper-02 zookeeper-03 spark-master spark-master-standby \
  spark-worker-01 spark-worker-02

curl -fsS http://127.0.0.1:18080/json/
curl -fsS http://127.0.0.1:18081/json/
```

정상일 때 Master 하나는 `ALIVE`, 다른 하나는 `STANDBY`입니다. KRaft Kafka는 ZooKeeper를 사용하지 않으며, ZK는 Spark Master 선출에만 사용합니다. HA Overlay 적용 후 다른 명령에서 기본 Compose로 Master를 재생성하면 선출 설정이 바뀌므로 Master 작업에는 **항상 세 Compose 파일을 함께** 전달하세요.

## 실행 6 — Kafka 지속 발행 중 Spark Master HA (검증 완료)

추가 검증 보고서: Spark HA 지속 스트리밍 실측.

이 실험은 기존 업무 Kafka/ES 데이터와 분리하기 위해 **새 전용 토픽** `weather.ha.soak.v1.local`, **새 전용 ES 인덱스** `weather-ha-soak-v1-local`, 전용 체크포인트 `weather-ha-soak`를 사용합니다.

동일한 테스트 리소스가 이미 생성된 상태라면 `prepare`가 안전하게 중단됩니다. 기존 데이터/볼륨을 삭제하여 재실행하지 마세요. *현재 테스트를 다시 확인하려면 아래 verify 명령만 사용*합니다. 완전히 새 테스트가 필요한 경우 토픽·인덱스·체크포인트 명칭을 함께 새 값으로 변경한 후 별도의 프로젝트에서 실행하세요.

먼저 5절의 Kafka·ES·Spark HA가 실행 중인지 확인합니다.

```bash
# 새 로컬 데이터가 존재하지 않을 때에만 1회 실행
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  --profile tools run --rm --no-deps \
  -e KAFKA_TOPIC=weather.ha.soak.v1.local \
  -e WEATHER_INDEX=weather-ha-soak-v1-local \
  integration-tool python docker/integration/soak_ha.py prepare

# 이 서비스는 기존 Spark 날씨 Job을 microbatch 모드로 상시 실행합니다.
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  --profile soak up -d --no-deps spark-ha-soak
```

**터미널 1 — 90초 동안 0.5초마다 합성 이벤트 한 건씩 발행:**

```bash
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  --profile tools run --rm --no-deps \
  -e KAFKA_TOPIC=weather.ha.soak.v1.local \
  -e WEATHER_INDEX=weather-ha-soak-v1-local \
  integration-tool python docker/integration/soak_ha.py publish \
  --run-id test20261104 --count 180 --interval 0.5
```

**터미널 2 — 발행 중 활성 Spark Master를 중지하고 승격 확인:**

```bash
# 각각 상태를 읽어 ALIVE인 서비스만 대상으로 선정하세요.
curl -fsS http://127.0.0.1:18080/json/
curl -fsS http://127.0.0.1:18081/json/

# 예시: 실측 당시 spark-master-standby(18081)가 ALIVE였음
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  --profile spark-ha stop spark-master-standby

# 18080이 ALIVE로 승격됐는지 확인한 후 중지했던 서비스만 복구
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  --profile spark-ha start spark-master-standby
```

**발행 종료 후 — Kafka 끝 오프셋·Spark 커밋 오프셋·ES 전체 고유 ID 대조:**

```bash
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  --profile tools run --rm --no-deps \
  -e KAFKA_TOPIC=weather.ha.soak.v1.local \
  -e WEATHER_INDEX=weather-ha-soak-v1-local \
  integration-tool python docker/integration/soak_ha.py verify \
  --run-id test20261104 --count 180
```

**2026-11-04 실측:** Kafka 승인 180/180, Spark 정상화 180/180(32개 양수 마이크로배치, 최대 성공 배치 간격 3.39초), Kafka 오프셋 180 = Spark 커밋 오프셋 180, ES 180개의 서로 다른 ID 누락·중복 0, Driver 재시작 0, Master 승격 38초. 이는 **로컬 단일 호스트에서 Compose stop(SIGTERM)을 사용한 한 가지 장애 실험**이며, SIGKILL·호스트 다운·네트워크 단절·장기 부하 또는 AWS 멀티 AZ 환경까지 무중단을 보증하지 않습니다.

## 실행 7 — 음악·광고·피드백 Spark 잔여 회귀검증

세부 결과: 3-C단계 보고서. 아래 모든 명령은 **로컬 프로젝트의 테스트 컨테이너만** 대상으로 실행하고, 실 외부 API·운영 DB에는 접근하지 않습니다.

**음악/YouTube: 데모와 PostgreSQL 통합 테스트를 분리**

```bash
# 데모 기대 정책(PendingEvent·DemoFeedback) 시험: SQLite 임시 테스트 DB
docker compose --profile tests run --rm --no-deps api-test \
  python -m backend.manage test backend.music.tests \
  backend.music.test_occasion_publication backend.recommendation.tests.test_api \
  backend.recommendation.tests.test_youtube_playback_api \
  backend.recommendation.tests.test_youtube_queue_api \
  backend.recommendation.tests.test_catalog_publication \
  backend.recommendation.tests.test_youtube_collected_links --noinput --verbosity 0

# 통합 정책(FeedbackEvent/Outbox/PG): Django가 별도 test_muther_dev 생성 후 제거
docker compose --profile tests run --rm api-pg-test \
  python -m backend.manage test backend.common.tests \
  backend.recommendation.tests.test_shared_youtube_database \
  backend.recommendation.tests.test_youtube_playback_api \
  backend.recommendation.tests.test_youtube_queue_api --noinput --verbosity 0
```

**MongoDB 실접속 테스트**

```bash
# 위 1절의 MongoDB ads-rs를 먼저 실행한 상태에서만 수행
docker run --rm --network container:mlo-p4-app-local-mongo-1 \
  -e ADS_RUN_MONGO_TESTS=1 \
  -e 'ADS_TEST_MONGODB_URI=mongodb://127.0.0.1:27017/?replicaSet=ads-rs&directConnection=true' \
  -e DJANGO_DEBUG=1 \
  -e DJANGO_SECRET_KEY=local-only-synthetic-django-secret-not-for-use \
  mlo-p4-ads:dev python manage.py test \
  tests.test_ads_mongo_integration tests.test_ads_public_mongo_integration \
  tests.test_ads_policy_mongo_integration tests.test_ads_workers_mongo_integration \
  --noinput --verbosity 1
```

**원본 피드백 Spark Structured Streaming 실기동**

```bash
# 5절 Spark/ZooKeeper HA + 2절 Kafka 및 기본 postgres가 실행된 상태
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  --profile feedback-stream build spark-feedback-stream

docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  --profile feedback-stream up -d --no-deps spark-feedback-stream

# 신규 합성 이벤트 생성: 출력되는 event_id(UUID)는 아래 verify-spark에 입력
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  run --rm --no-deps -e MLO_LOCAL_INTEGRATION=1 \
  api python docker/integration/smoke_feedback.py prepare

# 기존 Django Outbox 발행기를 사용하여 Kafka RF3 토픽으로 ACK 발행
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  run --rm --no-deps api python -m backend.manage relay_feedback

# 실행 중인 피드백 Spark Driver가 Kafka를 소비하고 PostgreSQL에 투영했는지 확인
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  run --rm --no-deps -e MLO_LOCAL_INTEGRATION=1 \
  api python docker/integration/smoke_feedback.py verify-spark --event-id "생성된-UUID"
```

드라이버 재시작 검증은 `spark-feedback-stream` 서비스만 `stop` 후 `start`하고 새 합성 이벤트를 발행한 뒤, 기존/신규 UUID에 `verify-spark`를 각각 실행합니다. 기존 checkpoint/로컬 DB 볼륨은 절대 삭제하지 마세요.

## 상태, 종료 및 안전 경계

```bash
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  --profile spark-ha ps
docker compose -f compose.yaml -f compose.integration.yaml -f compose.spark-ha.yaml \
  --profile "*" down
```

`down`은 명명된 볼륨을 보존합니다. `down -v`, `volume prune`, 다른 프로젝트의 정리나 AWS 호출은 금지합니다.

## 미검증

- KMA API 실제 수집, 외부 날씨 원본 품질·장애/쿼터 테스트
- 음원 CSV 승인/카탈로그 발행, 실제 선곡·YouTube 재생, 광고 이벤트 전체 E2E
- Spark 피드백 스트리밍은 로컬 Docker 실기동·체크포인트 재시작 PASS. **장기 고부하·다중 파티션·실 사용자 트래픽 검증은 미실행**
- 실 멀티호스트/AZ 장애, TLS/SASL/IAM, amd64·배포용 이미지, Terraform/Runtime/Lifecycle
- Spark Connector JAR 다운로드는 이번 로컬에서 Maven 온라인 해석; 운영용 lock/digest 검증은 별도

테스트 결과·실행 상태는 3단계, 3-B Master HA, 3-C 기능 회귀·Git 점검 보고서를 참고하세요.
