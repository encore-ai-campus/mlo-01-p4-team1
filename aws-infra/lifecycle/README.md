# Lifecycle 로컬 구현 v1

Issue #10의 종료·백업·삭제 **로컬 simulator**입니다. `inspect`, `plan`, `execute`, `resume`, `verify`를 제공하며 모든 결과의 `ready_for_aws`는 false입니다. 실제 AWS adapter와 backend 접근 경로는 제공하지 않습니다.

## 준비와 검사

Python 3.14.8을 사용합니다. 저장소 루트에서 다음 순서로 준비합니다.

```bash
python3 -m venv aws-infra/lifecycle/.venv
aws-infra/lifecycle/.venv/bin/python -m pip install -r aws-infra/lifecycle/requirements.lock.txt
aws-infra/lifecycle/.venv/bin/python aws-infra/lifecycle/scripts/setup-toolchain.py
bash aws-infra/lifecycle/scripts/check-local.sh unit
bash aws-infra/lifecycle/scripts/check-local.sh integration
```

준비 스크립트만 고정 공개 dependency·Terraform archive를 내려받습니다. 출처·버전·SHA256는 [toolchain.lock.json](toolchain.lock.json)과 [requirements.lock.txt](requirements.lock.txt)에 고정했습니다. 이미 받은 archive는 setup-toolchain.py의 `--archive <파일>`로 검사·설치할 수 있습니다. 검사는 AWS credentials를 사용하지 않습니다. integration은 Terraform 1.11.4의 builtin terraform_data만 사용하며 AWS provider가 없습니다.

필수 venv·버전·suite·Terraform이 없으면 NOT READY와 0이 아닌 코드로 종료합니다. suite의 skip·0 tests도 실패입니다. 결과는 `.artifacts/<run-id>/summary.json`, unit.log, integration.log에 보존합니다. Mac arm64·SQLite·provider 없는 시험 결과이며 EC2 amd64·systemd·SSM·운영 PG/Mongo 성공은 별도 검수입니다.

## CLI

실행 파일은 [scripts/lifecycle.py](scripts/lifecycle.py)입니다. 다음은 합성 입력의 검사·계획 생성입니다.

```bash
aws-infra/lifecycle/.venv/bin/python aws-infra/lifecycle/scripts/lifecycle.py inspect --bundle aws-infra/lifecycle/fixtures/input.example.json
aws-infra/lifecycle/.venv/bin/python aws-infra/lifecycle/scripts/lifecycle.py plan --bundle aws-infra/lifecycle/fixtures/input.example.json --output aws-infra/lifecycle/.private/review-plan.json
aws-infra/lifecycle/.venv/bin/python aws-infra/lifecycle/scripts/lifecycle.py lab-init --bundle aws-infra/lifecycle/fixtures/input.example.json --lab aws-infra/lifecycle/.private/lab-run1
```

`plan`의 기본 모드는 retain-data이며 `--mode purge-data`를 명시할 수 있습니다. 출력 파일은 기존 파일을 덮어쓰지 않습니다. 출력 부모 폴더를 먼저 준비해야 합니다. 실행 전에 새 출력 경로를 독점 예약하며 기존 출력 경로 오류가 작업 후에야 발견되지 않도록 했습니다. lab-init은 새 폴더만 만들고 합성 자원 DB·시험용 SQLite DB 2개를 준비합니다. 운영 DB에 연결하지 않습니다.

execute/resume/verify에는 다음 경로 인수를 사용합니다. 아래 파일은 로컬 검토자가 정확한 계획을 검토하고 제공하는 입력입니다. CLI가 승인을 스스로 발급하지 않습니다.

```text
execute --bundle <input.json> --plan <review-plan.json> --lab <lab-dir>
        --approval <signed-approval.json> --evidence <signed-evidence.json>
        --authority-key <0600-local-authority.key>
resume  <같은 인수> [--reconciliation <signed-reconciliations.json>]
verify  --bundle <input.json> --plan <review-plan.json> --lab <lab-dir>
        --authority-key <0600-local-authority.key>
observe --bundle <input.json> --plan <review-plan.json> --lab <lab-dir> --step <step-id>
```

자동 시험은 [tests/support.py](tests/support.py)의 합성 authority로 서명을 만들어 전체 CLI를 실행합니다. 이 서명·키는 실제 실행 승인이 아닙니다. `--yes`, real adapter, 임의 shell/CLI 입력을 지원하지 않습니다. CLI에서 socket 생성·네트워크 호출과 허용된 C 고정 stateful hook 외 subprocess를 거부합니다. 가짜 CLI에서 실제 PATH로 넘어가는 fallback도 없습니다. 별도 Terraform 시험 프로세스는 provider 없는 임시 구성과 로컬 state만 사용합니다.

종료 코드는 0=완료/검사 성공, 2=거부·실패, 3=verify의 미완료/미확정 결과입니다. `--fault-step <id> --fault before|timeout|crash`는 합성 실행의 고장 주입이며 crash는 영수증을 남긴 채 code 77로 프로세스를 종료합니다.

## 버전별 JSON 계약

직렬화는 정렬된 key·공백 없는 JSON의 UTF-8 SHA256를 사용합니다. 중복 key·NaN·미등록 필드를 거부합니다. 입력의 authoritative 검사는 [model.py](src/mlo_lifecycle/model.py)에 있습니다. 실제 Terraform state JSON을 이 합성 projection으로 간주해서는 안 됩니다.

| 형식 | 필수 내용과 결합 |
|---|---|
| lifecycle-input-v1 | example_only=true, 공통 inventory-v1, binding, 7 roots의 state 사본 metadata와 전체 managed targets, dependencies |
| binding | project=mlo-p4, dev/environment, generation, 5개 합성 account ID, root별 backend key·lineage·serial, address→ID 전체 집합, untracked_data |
| state projection | backend.kind=snapshot-only, dev/root/terraform.tfstate key, sim-lineage-root, integer serial, 자원 address·ID·account·project·generation·ownership·protected·node |
| lifecycle-plan-v1 | 입력·코드/설정 SHA256, scope/state versions, mode, created_at/expires_at(최대 1시간), target/delete/retain/protection_release, 순서·선행조건, blocker, SHA256 |
| local-approval-v1 | reviewer=synthetic-test-authority, plan_sha256, scope, mode, 정확한 delete/protection_release/purge_data 목록, issued_at/expires_at, signature |
| local-evidence-v1 | plan_sha256, scope, input_sha256, identity/state/ownership의 정확한 boolean facts, issuer·generation·16 instance 및 서비스별 정지/flush 사실을 담은 runtime, issued_at/expires_at(최대 5분), signature |
| local-reconciliation-v1 | reviewer, plan_sha256, step, decision=completed 또는 retry, 현재 observation_sha256, issued_at/expires_at(최대 5분), signature |
| lifecycle-result-v1 | planned_delete/deleted/retained/remaining/failed/unknown, retained_missing, target_status, completed/pending steps, complete·cost_may_remain |

서명은 signature 필드를 제외한 canonical JSON의 HMAC-SHA256입니다. 키는 외부 입력인 0600 파일이며 코드가 기본 키를 갖지 않습니다. 이 로컬 신뢰 경계는 운영 서명·원격 identity·append 전용 외부 보관을 대체하지 않습니다. 계획은 C 실행 코드·lock, 소비한 공통/B 코드와 A 구성도 hash에 결합합니다. state serial·lineage·계정·generation·정책·코드가 바뀌면 재계획합니다.

## 순서와 보존

planner는 먼저 신규 요청·수집·timer·producer·supervisor 재시작을 막는 freeze, 전체 작업 idle 관측, 두 stateful 서비스의 계획 종료를 배치합니다. B의 실제 `CommandStatefulBackend`와 `stop_stateful`을 읽기 전용으로 소비하여 quiesce → backup → 독립 사본 → hash → 실제 SQLite restore → stop → 종료 관측을 수행합니다. SQLite의 정상 행/무결성을 비교하며 사본 hash·복원/종료 영수증과 백업 이후 원문 변경도 재검사합니다.

compute·연결 자원 삭제는 선행 종료와 역 resource dependency DAG를 따릅니다. 연결/관측 자원은 모든 compute 소비가 끝나기 전에 제거할 수 없습니다. 순환·누락·중복 dependency는 거부합니다. service restart, ID drift, 증거 누락/변조/만료, 생성/배포 activity, 다른 plan 교체, 동시 flock도 거부합니다.

retain-data는 data EBS·S3 data·backend와 그 의존 자원을 보존합니다. 합성 예제의 81개 자원 중 26개를 보존합니다. untracked_data가 있으면 유실 blocker로 실행을 막습니다. purge-data도 backend를 보존하며 별도 정확한 purge 승인, 각 합성 data asset의 사본/hash/복원 단계를 요구합니다. 합성 asset의 JSON 백업은 실제 EBS snapshot·S3 object versions의 검증이 아닙니다. 비용이 남을 수 있으며 전체 자원 삭제 또는 무과금으로 보고하지 않습니다.

## 중단·재개

lab 폴더에 OS process-lifetime flock, signed append journal.jsonl과 atomic checkpoint.json을 남깁니다. step은 외부 효과 전에 started를 기록하고 현재 관측을 확인한 뒤 complete가 됩니다. 실패는 ambiguous로 남기며 SIGKILL은 started를 보존합니다. lock 파일과 영수증을 삭제해서 재실행하지 않습니다.

resume은 같은 계획/입력과 새로 유효한 승인·증거를 요구합니다. 확정된 step도 재관측한 뒤에만 건너뜁니다. 미확정 step은 observe 결과와 일치하는 서명된 운영자 reconciliation이 필요합니다. completed 결정은 실제 완료 관측에서만, retry는 효과가 없다는 not_started 관측에서만 허용합니다. 일부 hook 효과가 생겼는데 effect 기록이 없거나 데이터가 달라졌다면 unknown으로 차단합니다. 그 상태의 자동 복구는 제공하지 않습니다. 이전 백업/영수증과 현재 DB를 대조하는 후속 검토가 필요합니다.

## 아직 인수할 사항

실제 Foundation destroy 차단·VPC/ENI/EBS/S3 보호·ALB deletion protection은 유지했습니다. 실제 A state/module graph export·보존 구성·보호 해제, 운영 PG/Mongo/SSM/S3 hook·독립 복원, A/B/C 공통 잠금은 C requests에서 검토할 제안입니다.

현재 Makefile의 predeploy-check에는 C suite가 포함되지 않습니다. C 스크립트로 따로 검사합니다. C workflow는 수동·명시적 변수·prepared Linux X64 runner를 요구하며 원격 실행은 not_run입니다. 실제 AWS/3AZ/amd64/SSM·집 PC·운영 보존 기간과 backend 최종 제거는 별도 검수입니다.

Terraform의 [plan 명령](https://developer.hashicorp.com/terraform/cli/commands/plan)과 [prevent_destroy](https://developer.hashicorp.com/terraform/language/meta-arguments/lifecycle)를 참고해 provider 없는 1.11.4에서 실제 동작을 시험했습니다. 저장 plan은 `apply <saved-plan>`로 실행하며 `destroy <saved-plan>` 명령은 만들지 않았습니다.


## 2026-10-30 독립 검증 후 보완

- 계획 만료·승인/증거·코드/입력 hash를 각 효과 경계에서 재검사합니다. 입력 파일의 경로 교체와 내용 변경도 차단하고 효과 도중 변경은 ambiguous로 기록합니다. 운영의 immutable artifact 보장은 별도 조건입니다.
- 보호 여부는 합성 코드 정책과 대조합니다. EBS/S3 data·VPC·backend의 protected=false/missing/unknown을 거부합니다. 실제 Terraform configuration/API의 보호 증거를 대신하지 않습니다.
- purge의 모든 삭제는 모든 backup-data 증거에 직접 의존합니다. 선행 증거를 효과 전 다시 관측하므로 병렬 가능한 DAG에서도 백업 전에 통신 경로를 제거할 수 없습니다. 실제 graph 연계는 미구현입니다.
- DB 복원 전 사본/원본 archive hash를 강제하고 종료 직전에 archive·독립 사본·restore 파일·원본/복원 논리 데이터·무결성을 재검사합니다. B의 stop 호출 중 실패는 stop_ambiguous로 보존합니다.
- journal/checkpoint의 마지막 head 불일치, checkpoint 유실·이전 버전, 중복/부분 JSON을 거부합니다. append와 checkpoint 사이 crash도 외부 대조가 필요합니다. 둘 모두 같은 과거 버전으로 바뀌는 rollback은 로컬만으로 탐지할 수 없습니다.
- result.deleted는 synthetic ownership state의 logical removal입니다. `removal_semantics=synthetic-logical-removal`, `physical_resources_deleted=false`, `physical_data_deleted=false`를 구조적으로 출력합니다. 합성 원본 DB·JSON 자체의 물리 삭제를 뜻하지 않습니다.
- 실패 stderr는 lifecycle-error-v1 JSON이며 error_code/phase/step_id/safe_reason/journal_location/recovery_action을 제공합니다. 예외 원문·credentials·입력 JSON은 출력하지 않습니다. 보고서 저장 실패는 결과 stdout과 journal을 보존하고 report_write_failure로 구분합니다.

[운영·복구 절차](docs/OPERATIONS.md)와 [후속 인터페이스·신뢰 경계](docs/CONTRACTS.md)를 함께 읽습니다. offline state auditor와 lease reference model은 계약 시험 전용이며 실제 state 연결·분산 잠금 구현이 아닙니다. 강화된 runtime 증거와 result 필드 때문에 과거 plan/approval/evidence는 재사용하지 말고 새 합성 lab에서 다시 만듭니다. 기존 lab·영수증은 삭제하지 않습니다.
