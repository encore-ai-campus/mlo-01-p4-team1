# 실행 감독·계획 종료·LLM 중계 보완 기록

검토일은 2026-10-30입니다. 원 Orca worktree를 보존하고 승인된 `.artifacts/remediation-2026-10-30/workspace` 사본에서 구현·검증했습니다. AWS API, 원격 SSM, 집 PC, Git commit/push/merge는 실행하지 않았습니다.

## 변경과 실행 계약

- `src/mlo_runtime/lease.py`: 기존 PostgreSQL에 명시적으로 설치하는 `mlo_execution_lease_v1` 테이블과 transactional read/CAS를 구현했습니다. group별 advisory transaction lock과 row lock으로 최초 동시 생성도 직렬화합니다. owner/generation/epoch/expiry를 비교하고 잘못된 갱신·낡은 token·혼합 generation을 거부합니다. 모든 SQL 값은 psycopg 매개변수이며 SQL 문자열에 사용자 값을 삽입하지 않습니다. 판단 시간은 PostgreSQL `clock_timestamp()`입니다. 운영 DSN에는 `sslmode=verify-full`이 필요합니다. `lab=True`는 loopback으로 한정됩니다. `initialize()`는 승인된 schema 설치 때 별도 호출하며 daemon이 암묵적으로 DB schema를 변경하지 않습니다.
- `src/mlo_runtime/supervisor.py`: 3초 간격으로 15초 lease를 갱신하고 하나의 local daemon lock을 유지합니다. 상태를 durable JSON으로 남긴 뒤 idempotent 시작 명령을 호출합니다. 재시작은 기존 owner/epoch를 확인하며 같은 실행을 다시 제출하지 않습니다. 갱신·DB 접근 실패 때 실제 종료와 관측을 시도하고 미확인 상태는 `fencing`으로 남깁니다. 이전 owner가 활성 상태면 standby는 작업을 시작하지 않습니다.
- `src/mlo_runtime/execution.py`: owner별 승인된 hook 경로/SHA256/target을 묶은 `FixedCommandExecution`을 추가했습니다. `--operation start|stop|observe --owner --generation --epoch`의 고정 argv만 사용합니다. 관측 영수증은 세 master와 세 worker 전체 집합, token identity, 최근 timestamp, 실제 boolean 증거를 요구합니다. 이전 host stop/관측 실패, Driver/Executor 잔존은 승계를 막습니다. 기존 `DockerSparkExecution`의 실제 Docker/TLS Spark 관측도 유지했습니다.
- `src/mlo_runtime/operations.py`: 최초 빈 lease에도 실행 adapter의 전체 idle 확인을 요구합니다. takeover는 이전 host stop 뒤 증거 검증과 CAS를 수행합니다. lease read/renew 예외도 self-fence로 연결합니다. 웹 rollout은 지속 상태 경로의 lifetime lock·단계별 receipt·실패 rollback receipt를 기록합니다. crash/미확정 실행은 자동 재개를 거부하며 관측과 운영자 reconciliation이 필요합니다. 같은 폴더를 사용하는 `DockerWebBackend`는 `rollout-state.json`과 이력 receipt를 사용합니다.
- `src/mlo_runtime/stateful.py`: 고정 hash-bound DB adapter를 통해 계획 종료를 연결했습니다. quiesce → dump/검사 → 독립 사본 → 사본 hash 검사 → 실제 restore 검사 → stop → 종료 관측 → receipt 순서입니다. 앞 단계 실패는 stop 호출을 금지합니다. stop 자체가 미확정이면 `stop_ambiguous`를 보존합니다. 강제 종료·EC2 손실·OS 강제 재부팅까지 마지막 백업을 보장한다는 주장은 하지 않습니다.
- `src/mlo_runtime/relay.py`: Control 인증과 home 인증을 분리한 bounded HTTPS 중계를 구현했습니다. request ID/body/deadline을 보존하고 POST submit, GET result/status, DELETE cancel을 실제 home HTTPS API로 전달합니다. TLS CA/hostname 검증을 유지하고 redirect 및 일반 HTTP home origin을 거부합니다. 비밀값과 요청 본문을 access log에 출력하지 않습니다.
- `src/mlo_runtime/home_api.py`: **로컬 시험 전용 fake home API**입니다. 실제 모델을 구현하지 않습니다. SQLite의 request ID/body hash로 중복·충돌을 구분하고 submit/result/cancel/status 및 fake slot을 관측합니다. 취소 접수와 fake worker의 실제 중단 후 slot 반환을 분리합니다. 완료 결과와 중복 방지 정보는 fake API 재시작 후에도 보존합니다. 실제 GPU 장비의 cancellation/release를 대신 보장하지 않습니다.
- Control의 `request_model(..., ca_file=...)`은 HTTPS gateway와 비동기 202/result lookup을 지원합니다. 응답 단절 재시도에도 기존 ID를 유지합니다. `model_status(..., cancel=True)`로 원래 ID의 취소·결과를 조회합니다. 기존 HTTP smoke 시험 호환성은 유지하며 운영 HTTPS 경로는 별도 실제 TLS 시험으로 검증합니다.

## 실행 담당자가 제공할 보호 입력

다음 실행 binding은 0600과 `ready=true`, `example_only=false`를 요구합니다. 실제 앱·원격 hook·SSM/Tailscale·인증서가 인수되기 전에는 실행 입력을 승인 상태로 만들지 않습니다.

```text
python3 -m mlo_runtime.supervisor --binding /etc/mlo/supervisor.json
python3 -m mlo_runtime.relay --binding /etc/mlo/relay.json
python3 -m mlo_runtime.stateful --binding /etc/mlo/stateful-stop.json --confirm-planned-stop <node>
python3 -m mlo_runtime.supervisor --binding /etc/mlo/supervisor.json --check
python3 -m mlo_runtime.relay --binding /etc/mlo/relay.json --check
python3 -m mlo_runtime.stateful --binding /etc/mlo/stateful-stop.json --validate-only
```

supervisor binding에는 `owner`, `generation`, `group`, `state`, `lease_dsn_file`, `execution`이 필요합니다. DSN 파일도 0600입니다. `execution`은 `masters` 3개, `workers` 3개, `owners.<owner>.{target,path,sha256}`를 포함합니다. hook은 해당 owner의 실제 host에서 전체 실행 그룹을 idempotent 시작/종료하고 전체 Spark 상태를 관측해야 합니다. arbitrary shell을 전달하는 adapter는 허용하지 않습니다. 실제 SSM transport, instance identity 및 신뢰 가능한 원격 receipt 발급은 AWS 인수 시험 대상입니다.

relay binding에는 `home_url`(HTTPS), `control_token_file`, `home_token_file`, `home_ca`, `certificate`, `key`, 선택적 `listen_address`/`port`가 필요합니다. 기본 listen 주소는 loopback이고 port는 8088입니다. home의 실제 Serve 주소·tailnet ACL·인증서·모델 API는 인수 대상입니다.

stateful-stop binding에는 `node`, `generation`, `hook`, `sha256`, `receipt_directory`가 필요합니다. hook의 고정 operation은 `quiesce`, `backup`, `copy`, `verify-copy`, `verify-restore`, `stop`, `observe-stopped`이며 모든 JSON 응답은 node/generation/operation identity와 관측값을 반환해야 합니다. `backup`은 동일한 quiesce barrier ID와 SHA256/location/verified를, copy/검증은 같은 digest와 독립 location을 증명해야 합니다. 실제 S3 object version/독립 failure domain 및 운영 DB 쓰기 중단을 구현한 승인 hook이 필요합니다. 로컬 파일 복사는 실제 S3 내구성 검증으로 세지 않습니다.

supervisor `--check`는 실제 DB의 살아 있는 lease, active의 durable token과 실제 host 작업, 또는 standby의 자기 host 작업 부재와 전체 관측을 확인합니다. relay `--check`는 검증된 TLS와 home 인증으로 `/status`를 호출해 `ready=true`를 확인합니다. GPU 처리량을 확인하는 검사는 아닙니다. stateful `--validate-only`는 hook checksum과 binding만 검사하며 백업·복원을 실행하지 않습니다. systemd active 상태와 업무 readiness는 함께 확인해야 합니다.

## 검증 결과와 실패·수정 기록

1. **pass**: 단위 전체 53개를 재현했습니다(다른 보완 작업자의 신규 시험 포함). fake home의 sqlite connection close 누락과 cleanup 순서로 초기 thread 경고가 발생했으며 connection context 종료와 worker join을 수정한 후 경고 없이 통과했습니다.
2. **pass**: 실제 TLS 중계 통합 4개를 통과했습니다. A가 home 수락 후 응답을 끊어 B로 재시도해도 동일 raw body/ID/deadline이며 fake worker는 한 번만 실행됩니다. lookup/cancel/slot release, 늦은 응답의 expiry, 잘못된 인증·미신뢰 CA 차단도 확인했습니다. 최초 실행은 sandbox socket 제한으로 실패했고 플랫폼 승인 후 실행했습니다. 이어 Python 3.14의 strict TLS 검증이 fixture 인증서의 AKI 부재를 검출했습니다. SKI/AKI/KeyUsage를 갖춘 시험 인증서로 수정했으며 TLS 검증을 끄지 않았습니다.
3. **pass**: 실제 PostgreSQL 통합 4개를 초회 통과했습니다. 두 connection의 CAS 경쟁 한 승자, 오래된 갱신 거부, 두 supervisor process와 세 실제 자식 작업을 이용한 A 강제 종료→만료→잔존 작업 fencing→B 승계, 미확인 old host/stop 실패/Executor 잔존/master·worker 누락 차단, 실제 pg_dump→독립 복사→pg_restore→행 조회→계획 stop→재시작 데이터 유지입니다. 이후 초기 빈 lease idle 검사와 실제 DB 연결 실패 self-fence 시험을 추가해 최종 회귀 결과를 후속 기록합니다.
4. **not_run**: 실제 PostgreSQL TLS/권한/장애 가용성, 분산 3AZ Spark 전체 master/worker와 Docker 종료, SSM 원격 fencing, 실제 업무 Driver·enqueue 인터페이스, S3 backup version·독립 저장소 복원, Tailscale/집 GPU/실제 취소 자원 반환. 로컬 process adapter의 Spark endpoint 목록은 의도적인 관측 대역입니다.

재현 명령(저장소 루트, 전용 runtime venv):

```bash
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=aws-infra/runtime/src aws-infra/runtime/.venv/bin/python -m unittest discover -s aws-infra/runtime/tests/unit -v
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=aws-infra/runtime/src aws-infra/runtime/.venv/bin/python -m unittest discover -s aws-infra/runtime/tests/integration -p test_supervisor.py -v
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=aws-infra/runtime/src aws-infra/runtime/.venv/bin/python -m unittest discover -s aws-infra/runtime/tests/integration -p test_relay.py -v
```

의존성은 `psycopg==3.3.6`, `psycopg-binary==3.3.6`이며 공식 PyPI와 Psycopg 설치 문서를 확인했습니다. 공통 담당자가 requirements/toolchain source lock과 suite discovery에 반영합니다. 시험은 `mlo-p4-b-ops-pg-<runid>` 전용 project만 생성·정리합니다. `Lab`의 `.artifacts/<project>/commands.log`에 Docker 명령/결과가 남습니다. 다른 컨테이너/볼륨을 prune하지 않았습니다. 최종 ready_for_aws는 계속 false입니다.

## 최종 후속 회귀

- **pass**: 단위 전체 53개, 0.835초, SQLite thread/connection 경고 없이 통과했습니다.
- **pass**: PostgreSQL/process 운영 통합을 **5개**, 20.661초에 통과했습니다. 초기 빈 lease의 전체 idle 확인, 실제 closed-loopback DB 연결 오류에 의한 self-fence, 같은 실행 token의 재시작 후 pid 유지, active/standby 관측 readiness를 추가했습니다. 원래 4개 시험도 재현했습니다.
- **pass**: TLS 두 구간 중계 통합 **4개**, 3.245초에 최종 통과했습니다. 취소 API가 slot을 즉시 반환하는 대신 fake worker 중단 뒤 반환하도록 고쳤으며 실제 result/status에서 반환을 관측했습니다. authenticated home readiness도 확인했습니다.
- **보존**: 전용 PostgreSQL Compose project/volume을 종료·정리했습니다. A/B supervisor와 해당 run의 자식 process는 cleanup 또는 검증된 fencing으로 종료했습니다. AWS/집 PC/원 Orca checkout/공통 Git 상태는 변경하지 않았습니다. 이 문서·runtime source/test만 승인된 수정 사본에 남습니다.
