# Track2 종료·보존·격리 복구

C 소유의 `lifecycle-track2-input-v2` / `lifecycle-track2-plan-v1` 로컬 확장입니다. 기존 `lifecycle-input-v1`의 허용 필드는 유지합니다. 공통 inventory·shutdown evidence v1을 변경하지 않습니다. 실제 앱 제공 artifact, A의 보호 대상, B 원격 hook 및 공통 실행 계약의 인수는 별도이며 `ready_for_aws=false`입니다.

## 등록과 종료 순서

입력은 기존 합성 7-root snapshot bundle에 `track2`를 추가합니다. 신규 형식 이름을 명시해야 하며, 기존 v1에 필드를 몰래 추가하면 거부합니다. `track2`의 `writers`는 다음 고정 목록과 `app_workers`의 정확한 합집합이어야 합니다.

| 단계 | 필수 writer |
|---|---|
| 업무 drain | catalog-ingest, outbox-exporter, mail-worker, business-check-worker, ads-writer, weather-collector, 앱 artifact의 명시적 worker |
| Driver | weather-driver, feedback-driver, analytics-driver, catalog-driver |
| 발행 | search-publisher |
| 관측 | observation-writer, observation-collector |

각 등록은 artifact SHA256와 `synthetic-stop-v1` hook을 결합합니다. 앱 artifact 목록은 신뢰할 수 있는 외부 인수 자료로 확정해야 합니다. 호출자가 임의로 작성한 목록만으로 실제 worker 전체를 발견했다고 주장하지 않습니다. 현재 artifact hash와 hook은 합성 fixture이며 실행할 shell/SSM 명령을 받지 않습니다.

계획은 ingress·재시작 차단 → idle → 업무 worker → Driver → publisher → 관측 writer/collector → 보존 seal → PG/Mongo backup·복원·정지 → compute/연결 제거 순서를 강제합니다. 모든 실제 효과 경계에서 이전 완료 관측과 승인·입력·실행 코드 hash를 다시 검사합니다. seal은 모든 backup/삭제의 직접 선행조건입니다. stop hook의 실패, 미처리/불명확 작업, 등록 누락·추가, 이전 writer 부활, fencing epoch 혼합은 진행을 차단합니다.

합성 hook의 상태는 lab의 `track2-state.json`에 있습니다. `pending_outbox`는 확인된 정수 0이어야 합니다. `queue_observations_known`는 정확한 boolean true여야 합니다. 일반 jobs idle과 별도로 검사하므로 jobs=0이 Outbox/queue 확인을 대체하지 않습니다.

## 보존 자료와 일관성

`accept_foundation`은 공통 `track2-bridge/1`을 통해 A의 정확한 inventory hash·환경·generation·dataset과
위 네 query를 검증합니다. raw는 `raw/venues/<dataset>/`, lake는 `lake/venues/<dataset>/v1/`입니다.
`catalog-ingest`는 worker 이름이고 checkpoint query 이름이 아닙니다. 자동 checkpoint 이동은 하지 않습니다.

`accept_observation_archive`는 B의 sanitized nested envelope를 별도 `track2-observation-archive/1`로 정규화합니다.
버전 ID와 원본 checksum을 요구하고 emission UUID의 상충 재사용을 거부합니다. emission마다 다른 ID,
동일 업무의 단계 연결은 `business_event_id`로 보존합니다. 이 결과는 아래 합성 flat fixture를 운영 원장이나
실제 stop receipt로 승격하지 않습니다. source archive 인증·복구 reader IAM·실제 원격 복원은 별도 인수입니다.

`assets`의 정확한 필수 목록은 `track2.ASSETS`입니다. 각 자료는 immutable version ID·canonical payload SHA256를 가지며 plan/input signature에 결합됩니다. 새 lab에 복사한 뒤 seal·복구 시 원본 사양 및 현재 파일을 다시 비교합니다.

| 자료 | 검사와 의미 |
|---|---|
| PG ledger·Mongo log_record | 원장 스냅샷 별도 보존. 합성 projection이 실제 업무 schema나 DB dump를 대체하지 않음 |
| raw/lake manifest·catalog | 세대 및 catalog hash 대조. catalog 입력은 immutable 버전 자료 |
| query별 checkpoint | weather-stream, feedback-postgresql, feedback-analytics-batch, venues-catalog의 `aws/dev/<generation>/<query>/checkpoint/`; 경로·query·세대·batch ID 대조 |
| releases | active/rollback 등록, manifest hash, 중복 없는 기대 ID와 indexed ID의 완전 일치. 부분 색인 공개 차단 |
| exporter/publisher cursor | 원장 event_id 범위와 세대 확인. cursor만으로 검색 성공을 주장하지 않음 |
| observation archive·collector cursor | archive hash 및 포함 event_id 확인. 로그 재처리는 archive에서 수행 |
| Filebeat·Logstash queue | bounded bytes/max_bytes, archive hash와 event ID 보존 대조. 로컬 queue를 복제 원장으로 사용하지 않음 |
| deletion revisions | PG 삭제 원장과 일치하고 복구/rollback 결과에 재적용 |

queue fixture의 `bytes`와 상한은 이 시험의 입력값입니다. 운영 디스크 크기·보유 기간·TTL로 자동 채택하지 않습니다. 관측 stage는 SENT·ACKNOWLEDGED·COMMITTED·SEARCHABLE·PUBLISHED를 보존하며 HTTP 응답이나 cursor로 다음 stage를 만들어내지 않습니다. 실제 queue 압력 센서와 collector 중단 이후 lifecycle 진단 운송은 B/공통 인수 항목입니다.

`retention={status: unconfirmed, automatic_delete: false}`만 허용합니다. Track2의 `purge-data` 계획은 blocker를 가지며 서명된 삭제 목록이 있어도 실행할 수 없습니다. 자동 TTL·VACUUM·원천 삭제·버킷 삭제를 구현하지 않습니다. retained resource 비용은 남을 수 있습니다.

## 복구 CLI와 승인

새 합성 입력은 `aws-infra/lifecycle/.venv/bin/python aws-infra/lifecycle/scripts/write-example.py --track2 --output <새 파일>`로 생성할 수 있습니다. 이 명령은 승인이나 authority key를 만들지 않습니다.

기존 `inspect/plan/lab-init/execute/resume/verify/observe`에 다음 로컬 명령을 추가합니다.

```text
restore-track2 --bundle <track2.json> --plan <plan.json> --lab <lab>
  --approval <shutdown-approval.json> --evidence <shutdown-evidence.json>
  --authority-key <0600-key> --recovery-approval <recovery-approval.json>
  [--rollback-release]
resume-track2 <동일 입력> [--reconciliation <signed-records.json>]
observe-track2 --bundle <track2.json> --plan <plan.json> --lab <lab>
  --step <ledgers|business-replay|observation-replay|checkpoints|alias>
  [--rollback-release]
verify-track2 --bundle <track2.json> --plan <plan.json> --lab <lab>
  --authority-key <0600-key> [--rollback-release]
```

`restore-track2`는 기존 shutdown 결과가 완전하고 seal이 현재도 유효한 경우에만 lab 내부의 새 `track2-recovery/`에 씁니다. 운영 DB·S3·ES에 쓰지 않습니다. 원본 DB/보존 자료를 덮어쓰지 않으며 writer를 재기동하지 않습니다.

복구에는 별도 `local-track2-recovery-approval-v1`이 필요합니다. 필드는 kind, reviewer=`synthetic-test-authority`, recovery_sha256, issued_at, expires_at 및 signature입니다. recovery hash는 원래 plan SHA256·Track2 registry SHA256·rollback 선택을 결합합니다. HMAC-SHA256 서명과 최대 5분 유효기간은 기존 로컬 authority 규칙을 따릅니다. CLI는 승인·키를 발급하지 않습니다.

복구 단계는 다음과 같습니다.

1. PG/Mongo 원장과 버전 자료를 격리 보존합니다.
2. business archive의 event_id를 중복 제거하고 PG 원장과 대조합니다. archive에 빠진 미발행 이벤트는 PG에서 보충합니다. 동일 event_id의 충돌 및 동일 entity/revision의 충돌은 거부합니다.
3. 관측 archive를 event_id+stage로 중복 제거합니다. queue로 원장/로그 archive의 누락을 메우지 않습니다.
4. 더 높은 epoch·별도 recovery generation의 query별 checkpoint **인수 계획**을 기록합니다. 운영 checkpoint byte migration·offset 호환·실행 승인은 수행하지 않습니다. `writers_started=false`를 유지합니다.
5. 검증된 active 또는 승인된 rollback release로 격리 alias 결과를 만들고 삭제 revision을 다시 적용합니다. 과거 색인에서 삭제된 entity가 부활하는 것을 막습니다.

복구 journal은 source 실행과 같은 OS lock을 사용합니다. 시작/완료·HMAC append chain·checkpoint와 각 출력 hash를 기록합니다. 응답 유실·crash 이후 `resume-track2`는 현재 관측에 결합된 서명 reconciliation 없이는 진행하지 않습니다. reconciliation의 plan_sha256는 **recovery hash**이고 step은 위 다섯 단계명입니다. 효과가 없다는 관측에서만 retry, 정확한 완료 관측에서만 completed를 허용합니다. `verify-track2`는 만료된 승인으로도 상태를 읽을 수 있지만 작업을 실행하지 않습니다.

같은 로컬 저장소의 journal과 checkpoint를 모두 과거로 되돌리는 공격은 외부 append-only anchor 없이 검출할 수 없습니다. 실제 발행/alias 전환·old owner fence·단일 Delta writer·호스트 간 동시 재기동 차단은 운영 adapter 인수가 필요합니다.

## 실행과 증거

```bash
bash .local/agents/run-local.sh unit
bash .local/agents/run-local.sh integration
```

공개 checkout에서는 `.local` wrapper 대신 README의 `scripts/check-local.sh`를 사용합니다. unit은 registry/버전/보존/복구 계산·거부 시험입니다. integration은 기존 SQLite/CLI/Terraform 회귀와 Track2 CLI 실패·실제 crash·서명 재개·격리 복구 및 **실제 PostgreSQL/MongoDB Docker 시험**을 필수로 포함합니다. 누락·skip·0 tests는 실패입니다.

Docker 데몬과 `../runtime/images.lock.json`의 고정 PostgreSQL/MongoDB digest가 로컬에 준비되어야 합니다. 이미지가 없으면 잠금에 적힌 정확한 ref@digest를 별도 준비하며 시험이 임의 최신 이미지를 선택하지 않습니다. C 시험은 host architecture에 대응하는 Linux ARM64/AMD64를 명시합니다. native ARM64 통과를 AMD64 성공으로 확대하지 않습니다.

실제 DB 시험은 C `.artifacts/mlo-c-track2-<run-id>/`에만 기록합니다. network=none의 새 컨테이너 4개 및 image-declared 경로별 명명 volume을 사용합니다. B의 `ContainerStatefulBackend`를 읽기 전용으로 소비해 write barrier → 실제 dump → 로컬 immutable version 사본 → 독립 컨테이너 restore → 원본·복원 schema/row/index/sequence fingerprint 대조 → 명시적 source stop → 종료 관측을 실행합니다. C `stop_engine_backends(..., track2_check=...)`가 각 증거 경계와 정지 직전에 Track2 seal/epoch/generation을 다시 검사합니다.

이 DB 시험의 버전 사본은 로컬 file fixture입니다. 실제 S3 versioning·독립 장애 영역·원격 hook·업무 단일시점·무손실·PITR 보장이 아닙니다. 정리 시 정확한 run label과 기록한 ID/name을 확인해 이번 시험의 컨테이너/volume만 제거합니다. `resources.json`은 남은 자원·정리 오류를 기록하고 `acceptance.json`은 실제 DB 결과와 합성 부분을 구분합니다. dump·원시 로그·receipt는 Git 제외 `.artifacts`에 보존합니다.

공동 A/B adapter와 실제 localhost S3 SDK·공통 lease 연계, 전체 `predeploy-check`는 coordinator가 동일 최종 소스에서 별도로 검증해야 합니다. 실제 앱 hook/worker 목록, 운영 archive·retention, 원천삭제, 실제 AWS 복구와 패스키 ceremony는 미실행 상태입니다.
