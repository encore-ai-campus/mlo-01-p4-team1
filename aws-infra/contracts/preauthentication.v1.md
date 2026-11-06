# 계정 인증 전 준비와 이후 실행 계약

이 문서는 2026-11-01 공통 연계 보완의 최신 실행 경계입니다. 과거 보고서의 미구현 관측은 그 시점의 기록으로 보존합니다. 로컬 코드 검증과 실제 AWS 운영 인수는 구분하며 `ready_for_aws=false`를 유지합니다.

## 지금 실행할 수 있는 명령

저장소 루트에서 다음 명령은 AWS profile·로그인·API를 사용하지 않습니다. `preauth-check`는 먼저 Foundation mock 산출물이 필요합니다. 처음에는 전체 검사를 사용합니다.

```bash
make predeploy-check
# Foundation mock 산출물이 이미 준비된 경우 신규 경계만 재검증
make preauth-check

aws-infra/runtime/.venv/bin/python scripts/infra_control.py prepare \
  --binding aws-infra/foundation/binding.example.json \
  --inventory aws-infra/foundation/.artifacts/inventory.mock.json \
  --output .private/prepared-example
```

위 명령은 전체 검사가 생성한 같은 generation의 Foundation mock inventory를 사용합니다. 서로 다른 generation의 binding/inventory는 거부합니다. 운영 시에는 같은 generation의 실제 Foundation export inventory와 승인 binding을 인수합니다. prepare는 재사용한 출력 폴더를 덮어쓰지 않습니다. 16개 host coordination 계약, scope, 단일 bootstrap controller 양식 등을 0600 파일로 생성하되 모두 `accepted=false`입니다. 주소·role·경로가 생겼다는 이유로 운영 승인이 된 것은 아닙니다.

`infra_control.py validate --review .private/review.json`은 review envelope·소스 해시 검사입니다. 승인을 발행하지 않으며 payload의 실행 시 검증을 대신하지 않습니다. 승인 발행·AWS 로그인·권한 보완을 자동 수행하는 명령은 없습니다.

## 공통 잠금과 STS

물리 scope는 project, environment, 다섯 account ID, bootstrap/connectivity/encore1~5의 state key로 해시합니다. generation을 바꿔도 같은 자원에 별도 잠금이 생기지 않습니다.

- state bucket의 `operations/<scope-sha256>.json` 한 객체만 사용합니다. Terraform state key 접근 권한과 분리합니다.
- 16개 runtime 역할과 `p4-operation-controller`에 GetObject·PutObject를 부여합니다. 중앙 bucket, 각 VPC S3 Endpoint, identity policy의 교집합을 모의 평가합니다. 목록·삭제·버전 읽기·다른 key 권한은 부여하지 않습니다.
- bucket은 이 key의 삭제와 조건 없는 Put을 거부합니다. 최초 initialize는 별도 승인 뒤 IfNoneMatch 한 번으로 수행하며 acquire가 자동 초기화하지 않습니다. ListBucket 없이 존재하지 않는 key를 GET하여 403이 발생하는 문제를 피합니다.
- 로컬 운영자는 CLI bridge → 전용 operation controller 역할을 사용합니다. role의 trust와 source의 AssumeRole 허용은 인증 후 실제 IAM 검수 항목입니다.
- EC2는 IMDSv2 provider만 사용하고 node/account/role을 대조합니다. S3·SSM·IMDS는 프록시를 우회합니다.
- Gateway A/B의 STS는 서울 regional endpoint로 직접 접속합니다. 나머지 private host의 STS는 승인한 Gateway private IP의 3128 CONNECT를 사용합니다. TLS는 AWS까지 검증하며 복호화하지 않습니다. host HAProxy 3129 설치 전에도 사용할 수 있는 경로입니다.
- 네트워크 연결 오류일 때만 두 번째 승인 Gateway로 전환합니다. 자격증명·계정·role 불일치는 다른 경로로 우회하지 않습니다.

NAT Gateway·추가 STS Interface Endpoint·EC2는 추가하지 않았습니다. **Gateway를 먼저 설치하고 proxy health를 검수한 뒤 private host를 설치합니다.** 실제 SG·Peering·DNS·proxy ACL·STS 도달성은 AWS 생성 후 확인합니다. proxy 경로의 로컬 검증은 실제 AWS 도달성 성공을 의미하지 않습니다.

## 승인 입력과 실행 영수증

실행 입력은 `.private` 아래 소유자 전용 파일입니다. 마지막 경로의 symlink, 중복 JSON key, 만료·변조·미승인·example 입력을 거부합니다. `source_sha256`은 Git에서 추적하거나 제외하지 않은 비 Markdown 소스의 상대 경로와 내용 해시를 canonical JSON으로 묶은 SHA256입니다. 문서·실행 산출물·venv·캐시는 포함하지 않습니다.

review의 필드는 다음과 같습니다.

| 필드 | 조건 |
|---|---|
| schema_version | `infra-execution-review-v1` |
| accepted / example_only | 실제 실행은 각각 true / false |
| operation | foundation, runtime, coordination-init, destroy |
| environment / generation | dev / 검토한 generation |
| account_ids | 서로 다른 실제 다섯 계정, encore1~5 |
| source_sha256 | 실행 소스 전체 해시 |
| payload | 아래 작업별 고정 입력 |

approval은 `infra-execution-approval-v1`, review의 canonical SHA256, operation, reviewer, issued_at, expires_at, signature로 구성합니다. 유효 기간은 최대 1시간입니다. **Ed25519 서명 개인키는 별도 승인자에게 보관하고 runner에는 raw 32-byte 공개키만 제공합니다.** 공개키는 별도로 신뢰한 운영 입력이어야 하며 review가 임의 선택하게 하지 않습니다. signature는 signature 필드를 제외한 canonical JSON의 Ed25519 서명을 128자리 hex로 인코딩합니다. 이 코드는 사용자를 대신해 승인·서명을 발행하지 않습니다.

향후 별도 승인을 받은 실행 명령의 형태는 다음과 같습니다. **이번 준비 단계에서는 실행하지 않습니다.**

```bash
aws-infra/runtime/.venv/bin/python scripts/infra_control.py execute \
  --review .private/review.json --approval .private/approval.json \
  --authority-key .private/authority-public.key \
  --receipt .private/execution-receipt.json --enable-live
```

기본 상태에서는 execute가 거부됩니다. gate는 각 효과 경계에서 소스·입력·서명·시각을 다시 검사합니다. 기존 실행 영수증이 있으면 새 시도로 덮어쓰지 않습니다. 원격 결과 소실·실패·timeout은 `ambiguous`로 보존하고 자동 재전송·잠금 강제 해제·성공 처리를 하지 않습니다. 운영자가 실제 상태를 대조한 뒤 새 검토를 진행합니다.

## 작업별 payload

### Foundation

`stage`, `binding`, `binding_sha256`, `private_dir`, `coordination_contract`, `coordination_contract_sha256`, `reviewed_plans`를 정확히 제공합니다.

순서는 **preflight → bootstrap-plan → bootstrap → plan → apply → connectivity-plan → connectivity-apply → export → verify**입니다. bootstrap-plan은 local state backend의 저장 plan만 생성하고 apply하지 않습니다. bootstrap은 그 plan만 적용하며 재계획하지 않습니다. account apply는 encore1~5, connectivity-apply는 connectivity의 plan을 각각 검토합니다.

reviewed_plans는 bootstrap 때 bootstrap 한 key, apply 때 encore1~5 다섯 key, connectivity-apply 때 connectivity 한 key의 저장 plan SHA256입니다. 나머지 단계는 빈 객체입니다. 내부 receipt의 해시와 별개로 외부 승인 해시까지 대조합니다.

state bucket이 아직 없으므로 최초 bootstrap만 승인한 한 기기의 `single-bootstrap-controller` 계약·flock·pending receipt를 사용합니다. bootstrap 후 operation 객체를 별도 승인으로 initialize하고 나머지는 S3 CAS를 사용합니다. bootstrap guard는 account apply에 사용할 수 없습니다. 처음 OIDC/IAM도 아직 없으므로 **최초 bootstrap은 로컬의 승인된 CLI profiles로 진행**합니다.

### Coordination initialization

`coordination_contract`, 그 파일의 `coordination_contract_sha256`, `coordination_scope`가 필요합니다. bootstrap 이후 최초 한 번의 조건부 Put이며, 이 쓰기도 별도 승인 대상입니다.

### Runtime

`account`, `operation`, `targets`, `document`, `artifact`, `timeout_seconds`, `inventory`, `inventory_sha256`를 제공합니다.

- operation은 install/deploy/health/rollback 중 하나, targets는 승인 inventory에서 선택한 해당 계정의 exact instance IDs입니다.
- document는 `{name: p4-runtime-artifact, version: 숫자 문자열, sha256}`입니다. `$LATEST`·임의 shell Document를 사용하지 않습니다.
- artifact는 `{bucket, key, version, sha256, manifest_sha256}`입니다. S3 exact version을 읽고 TAR·manifest·파일 해시·node·role·operation·non-fixture·secret 인수를 검사합니다.
- 실제 호출 role은 `p4-controller-encoreN`이어야 합니다. Describe/GetDocument·GetObjectVersion을 읽기 검증한 뒤 고정 Document/version/hash와 Operation 하나만 SendCommand에 전달합니다.
- host installer가 실제 변경 구간의 공통 deploy lease를 소유합니다. controller가 별도 lease를 잡아 remote installer와 교착하지 않게 합니다.
- 앱 소스·이미지·host binding·certificates·secret은 별도 인수 항목입니다. secret 본문을 TAR·Git·workflow input·로그에 넣지 않습니다. installer가 읽을 수 있도록 승인된 별도 경로로 호스트에 공급합니다.

### Destroy

새 운영 adapter는 **retain-data 저장 plan 적용**만 허용합니다. 기존 C local simulator의 retain/purge 검사와 구분합니다. 실제 데이터 purge·backend retirement는 이 승인 경로로 실행할 수 없습니다.

payload의 정확한 필드는 `infra_common/preauth_execution.py`의 `apply_saved_plan`에 고정돼 있습니다. root, 저장 plan/JSON과 각각의 해시, delete/retained 집합, actual state SHA256, Terraform 경로, inventory·snapshots·trusted_issuers와 해시, runtime/backup evidence, coordination 계약·해시·scope, mode가 필요합니다.

- bootstrap/backend·알 수 없는 root·mock-plan snapshot·교체/create/update·누락/중복/예상 외 삭제를 거부합니다.
- 7개 state snapshot의 분류·의존성 보존 집합과 root별 before ID, inventory 16개 EC2 ID, actual state lineage/serial/전체 해시를 대조합니다.
- 별도로 신뢰한 issuer keyring으로 서명된 실제 종료 관측과 PostgreSQL/MongoDB 독립 백업·복원·정지 증거가 필요합니다. 30초 freshness·operation/epoch·generation을 적용 직전에 다시 검사합니다.
- 검토한 저장 plan을 `terraform show -json`으로 다시 대조하고 파일 해시를 재확인한 뒤 그 파일만 apply합니다. 이 adapter는 새 plan을 만들거나 state를 삭제하지 않습니다.
- 적용 뒤 삭제 주소가 없어졌고 보존 주소의 물리 ID가 유지됐는지 검사합니다. 실패는 ambiguous로 남깁니다.

**운영 관측 발급기는 아직 실제 앱·Control·Spark·Kafka·systemd·Delta·S3와 연결해 인수하지 않았습니다.** 현재 테스트의 서명된 관측은 로컬 포트 검증용 합성 자료입니다. 이 자료를 운영 proof로 복사하거나 모든 사실을 true로 수동 채워 삭제하지 않습니다. 실제 proof가 없으면 운영 destroy는 계속 차단됩니다. 최초 생성 전에 삭제 시 보존할 데이터와 잔존 비용도 합의합니다.

## 수동 GitHub Actions

infra-plan / infra-apply / app-deploy / infra-destroy는 모두 workflow_dispatch 전용입니다. repo가 private이고 main이며 각각의 `*_ENABLED` variable이 명시적으로 true일 때만 준비된 self-hosted Linux X64 runner에서 실행됩니다. 변수는 이번에 설정하지 않았습니다. 공개용 별도 저장소에서는 이 운영 경로를 켜지 않습니다.

- plan → infra-plan environment / p4-ci-plan
- apply → infra-apply environment / p4-ci-apply
- runtime → infra-runtime environment / p4-ci-runtime
- destroy → infra-apply environment / p4-ci-apply, 별도 destroy review·서명·변수

destroy를 별도 environment/role로 더 격리하는 변경은 향후 IAM 인수 때 선택할 수 있습니다. 현재는 같은 apply 환경이더라도 signed operation을 바꿔 우회할 수 없습니다.

GitHub의 2026-07-15 이후 신규 저장소는 `repo:OWNER@OWNER-ID/REPO@REPO-ID:environment:infra-<purpose>` 형식의 immutable subject를 사용합니다. 이 프로젝트의 새 저장소도 실제 발급 형식을 대조하여 binding에 입력합니다. credential_process는 runner의 GITHUB_REPOSITORY_ID / GITHUB_REPOSITORY_OWNER_ID와 JWT의 ID·sub를 함께 대조하며 이름만 같은 다른 저장소로 대체하지 않습니다. 구형/사용자 지정 subject를 추측하여 trust를 완화하지 않습니다. [GitHub 공식 OIDC 기준](https://docs.github.com/en/actions/reference/security/oidc)을 따릅니다.

runner 관리자 준비 사항은 Python 3.14.8, 고정 AWS CLI·Terraform·provider, `.venv`/Runtime 환경, Docker, private backend 초기화와 접근성입니다. GitHub의 `INFRA_APPROVAL_PUBLIC_KEY_SHA256` variable에는 별도 승인자가 전달한 공개키의 SHA256을 고정합니다. review 폴더 속 임의 공개키로 바꿔 서명을 위조할 수 없도록 runner가 이 값과 대조합니다. GitHub environment의 required reviewer·main 제한, OIDC exact repo/environment/audience와 최소 권한을 실제 계정 인증 후 검토합니다.

`INFRA_PRIVATE_REVIEW_ROOT`는 checkout 밖 `.private` 경로입니다. SHA256 이름의 0700 폴더에 review.json·approval.json·authority-public.key를 0600으로 배치합니다. workflow 입력은 review SHA256 하나이며 계정 번호·토큰·plan·evidence는 workflow log나 Actions artifact로 게시하지 않습니다. `run_reviewed_workflow.py`는 서명과 목적을 확인한 후에만 OIDC credential_process config를 구성합니다. OIDC JWT와 STS 세션은 메모리/SDK pipe에만 유지합니다.

모든 운영 workflow는 같은 concurrency group을 사용하고 진행 중 작업을 취소하지 않습니다. PR 검사와 수동 local acceptance에는 id-token 권한이 없습니다. 실제 Linux X64 전체 CI·OIDC 발급·SSM 실행은 이번 로컬 검증 성공과 별도로 기록합니다.

## 인증 후 필요한 사실

1. encore1~5의 실제 인증과 계정/role identity, 생성·state·배포·삭제 권한, 명시적 비용 승인.
2. 실제 AMI(Ubuntu 24.04 amd64/SSM/AWS CLI 포함), 3물리 AZ ID, CIDR, 기존 자원 소유권과 예산. 특히 기존 encore3 VPC를 추정하여 관리하거나 삭제하지 않습니다.
3. 실제 업무 소스·불변 image digest·artifact와 S3 version, DB migration·credential·TLS·Tailscale/집 PC model 계약.
4. actual Terraform plan·보존 정책·비용 검토와 사용자 승인. 승인 뒤에만 bootstrap 및 인프라 생성.
5. 생성 뒤 SSM/DNS/Peering/S3/STS/proxy, 서비스, 집 PC/GPU, 복구와 실제 운영 관측·독립 백업/복원 시험.

참조: [SSM SendCommand의 DocumentHash/version](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_SendCommand.html), [GitHub AWS OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws), [Boto3 proxy 설정](https://docs.aws.amazon.com/boto3/latest/guide/configuration.html), [STS regional endpoint](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sts_vpc_endpoint_create.html), [S3 조건부 쓰기 정책](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html).
