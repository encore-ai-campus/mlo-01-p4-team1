# 공통 생성·배포·종료 연계 v1

2026-10-31 추가된 로컬 검증 계약입니다. 실제 AWS 배포·종료·삭제 승인을 뜻하지 않으며 `ready_for_aws=false`입니다.

## 공통 작업 잠금

`infra_common.coordination`은 주입된 S3 client의 GET/조건부 PUT을 사용합니다. create(A)·deploy(B)·destroy(C)가 같은 계정/7개 state key 집합의 key를 공유합니다. generation을 바꿔 다른 잠금을 취득할 수 없습니다. scope의 project=mlo-p4, environment=dev, encore1~5 계정 ID와 `dev/<root>/terraform.tfstate` 일치를 요구합니다.

`OperationLease.initialize()`는 bootstrap 이후 빈 operation record에 대한 **명시적 최초 1회 쓰기**입니다. acquire가 자동 호출하지 않습니다. bucket versioning·조건부 쓰기·해당 key 삭제 금지와 접근권한은 실제 인수 항목입니다. 운영 storage/IAM 설정을 이 로컬 라이브러리가 구성했다고 해석하지 않습니다. 외부 S3 사용은 쓰기 승인을 받은 이후에만 가능합니다.

CAS와 영구 epoch를 확인하고, 갱신/네트워크 실패·불확실한 응답·프로세스 종료 때 held/ambiguous를 유지합니다. TTL이 지나도 자동 승계하지 않습니다. 운영자가 실제 요청 결과·호스트 상태·state를 별도로 조정해야 합니다. 외부 API에 이미 들어간 요청을 취소하거나 S3 전체 과거 버전 rollback을 자동 탐지한다는 보장은 없습니다. 운영 시계 오차·IAM·권한과 외부 증거도 별도 검수합니다.

state bucket 생성 전에는 `BootstrapGuard`가 단일 승인 controller의 OS flock과 fsync한 pending receipt를 사용합니다. 분산 잠금이 아니므로 다른 기기에서 병행 bootstrap을 금지합니다. 첫 시도 기록이 있으면 성공/실패와 관계없이 재검토 없이 재실행하지 않습니다. bootstrap guard로 account apply를 보호할 수 없습니다.

## 보호된 입력

실제 진입은 기존 enable-real·host binding 승인에 더해 `.private/`의 0600 `infra-coordination-binding-v1` 입력을 요구합니다. 예시 필드입니다. 아래는 자동 생성/실행 가능한 운영 설정이 아닙니다.

```json
{
  "schema_version": "infra-coordination-binding-v1",
  "accepted": false,
  "backend": "s3-cas",
  "bucket": "operator-supplied-state-bucket",
  "key": "operations/<scope_key>.json",
  "scope_key": "<계정 및 state key 집합의 SHA256>",
  "generation": "<승인한 generation>",
  "credential_source": "ec2-instance-profile",
  "instance_role_name": "<승인한 EC2 IAM role>"
}
```

로컬 controller는 `credential_source=aws-cli-profile`로 A의 CLI login bridge를 명시 선택합니다. EC2 host는 `ec2-instance-profile`을 선택하며 ambient 환경의 다른 credential provider로 대체하지 않습니다. IMDSv2 및 STS 계정/role 확인을 포함한 **실제 호스트 경로는 미실행**입니다. 공통 operation prefix에 접근할 호스트/운영 role·bucket policy의 실환경 검토가 남아 있습니다. Runtime artifact에는 공통 Python package가 해시와 함께 포함됩니다.

A wrapper는 각 외부 명령 경계에 create guard, B installer는 host 명령 경계에 deploy guard, C library는 단계 경계에 destroy guard를 확인합니다. supervisor와 유한 job admission은 MaintenanceBarrier가 create/destroy/불명확한 상태에서 새 실행을 막습니다. 이미 실행 중인 장기 작업은 barrier만으로 종료됐다고 간주하지 않고 별도 drain/stop/새 관측을 요구합니다.

## Foundation → Lifecycle

A `scripts/lifecycle_export.py`는 공급된 Terraform JSON/config 파일만 읽습니다. 실제 state 경로는 show JSON과 raw state의 lineage·serial·전체 주소/ID를 대조합니다. mock-plan 출처는 실자원 증거와 구별됩니다. 7개 root·16 compute와 모든 관리 자원의 classification·구성 참조·보호 상태를 C가 소비합니다. 알 수 없는 자원·주소/ID 불일치·replacement/create/update·backend 삭제는 거부합니다.

반환값은 **검토용 set**입니다. 정적 retention 구성 작성, 실계정 소유권/API 대조, provider의 암묵적·교차 root 의존성, 보호 해제 구성과 실제 저장 plan 인수는 별도입니다. 전체 graph를 읽었다는 이유로 real destroy 경로를 열지 않습니다. C CLI는 기존 합성 로컬 adapter만 제공하고 실제 AWS 삭제 adapter는 disabled입니다.

## Runtime → Lifecycle

B `database_stop.ContainerStatefulBackend`는 정확히 승인한 source/restore Docker ID와 별도 writer/admin을 사용합니다. PostgreSQL login 차단·세션 종료·기본 read-only 또는 MongoDB fsync lock을 확인하고 dump → immutable version 사본 → 독립 빈 DB 복원 → 내용/구조 검증 → stop → stopped 관측을 수행합니다. 자동 source 복원·볼륨 삭제는 없습니다. 손상·대상 교체·미확정 receipt는 정지를 차단하고 수동 조정을 요구합니다.

PostgreSQL 비교 범위는 table rows/columns/sequences/indexes/constraints이며 권한·전체 cluster role·extension·기타 운영 객체의 복구 수용을 대신하지 않습니다. MongoDB는 documents/indexes/collection options를 비교합니다. 운영 credential 파일·완전한 writer 목록·암호화/다른 failure domain·RPO/RTO는 실제 인수 조건입니다.

C는 trusted issuer keyring을 증거와 별도로 받아 16개 instance/generation, operation/epoch, 서비스별 strict boolean·서명·30초 유효기간을 검증합니다. DB별·stop 직전에 증거를 다시 요구합니다. 현재 통합 시험은 **명시적 local-lab 서비스 증거**입니다. 실제 Control/Spark/Kafka/systemd/Delta/S3 관측 발급기가 구현·검수됐다는 뜻이 아닙니다.

## 재현

A/B/C 환경 준비 후 저장소 루트에서 `make predeploy-check`를 실행합니다. 새 연계만 재현하려면 A mock 산출물이 있는 동일 checkout에서 `make integration-lifecycle`입니다. localhost HTTP S3 대역과 가짜 credential만 사용하며 실제 SDK 직렬화·조건부 요청·연결 유실·다중 프로세스를 검사합니다. 실제 S3 failure domain/권한/일관성 수용으로 표시하지 않습니다.

## 2026-11-01 구현 후속

최초 initialize는 존재하지 않는 객체를 먼저 GET하지 않고 IfNoneMatch Put으로만 시도합니다. 로컬 controller 계약에는 exact `controller_role_arn`이 필요합니다. EC2 계약에는 topology와 일치하는 `node`, `account_alias`, `instance_role_name`, `sts_route`가 필요합니다. Gateway는 regional-direct, private host는 승인한 두 Gateway private IPv4:3128의 gateway-connect를 선택합니다. 기존 예시는 최신 필드 없이 운영 인수에 사용할 수 없습니다. 자동 양식 준비·권한 교집합·인증 후 체크리스트는 [인증 전 실행 계약](preauthentication.v1.md)에 기록합니다. 정책 **코드 정의와 로컬 모의 검증**이 추가됐으며 실제 AWS 설정·STS 도달성이 확인된 것은 아닙니다.
