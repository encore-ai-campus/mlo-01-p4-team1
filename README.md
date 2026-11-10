# MLO 4차 프로젝트 — MUSIC X WEATHER 앱·Docker·AWS 인프라

날씨와 사용자 피드백을 활용하는 맞춤형 음악 서비스의 **웹/API·데이터 파이프라인·Docker 로컬 환경과 AWS 인프라 코드**를 함께 관리합니다. 인프라는 Terraform·Ansible·Docker·systemd로 구성합니다. 실제 AWS 계정 접근권한, 자격증명과 실데이터는 이 저장소에 포함하지 않습니다.

## 현재 상태

**2026-11-04 로컬 통합 준비 상태:** 최신 인프라 실행 코드와 3차 기반 Docker 앱을 한 디렉터리로 모았습니다. 기존 인프라·Docker 각 단계의 로컬 검증 이력은 있지만, 이 통합된 공개용 파일 세트 자체의 전체 CI는 최종 검증이 필요합니다. 실제 AWS 배포는 진행하지 않았으며 `ready_for_aws=false`입니다.

- Foundation·Runtime·Lifecycle의 공개용 기준에 최신 공통 실행·인증 전 안전경계 코드를 반영하고 `app/`에 Django·React·Kafka·Spark·Elasticsearch Docker 개발 구성을 추가했습니다. 공개용 Git 이력은 기존 Private 저장소 이력과 분리하여 새로 시작합니다.
- 인프라의 과거 `make predeploy-check` 검증은 각 개발 단계에서 수행됐으며, 새 통합본에서는 재검증이 필요합니다. 공개 가능한 [기술 기준](docs/handoff/infra/BASELINE.md) 및 [테스트 안내](docs/handoff/infra/TESTING.md)만 저장소에 포함합니다.
- Docker 앱은 합성 데이터로 음악·YouTube·광고·피드백 Spark와 Spark Master HA를 로컬 테스트했으며 실제 외부 API·Linux AMD64·실제 AWS 계정 검증은 완료하지 않았습니다.
- 일반 Git push는 AWS 생성·배포·삭제를 실행하지 않습니다. 실제 배포는 운영 입력·권한·plan·비용 검토와 별도 승인 후 진행합니다. 실제 AWS destroy adapter는 비활성 상태입니다.

## 아키텍처

서울 리전(`ap-northeast-2`)의 **5계정·5VPC·16EC2·3물리 AZ**, 계정 간 직접 VPC Peering 10쌍을 기준으로 합니다. EC2 호스트 기준 OS는 **Ubuntu 24.04 amd64**입니다.

| 계정 | EC2 구성 | 수량 |
|---|---|---:|
| encore1 | private Control A/B, PostgreSQL | 3 |
| encore2~4 | 계정별 Kafka·Spark·Elasticsearch 각 1대 | 9 |
| encore5 | public Gateway A/B, private 광고 앱·MongoDB | 4 |

- 공개 서비스: 사용자 HTTPS → CloudFront → HTTP ALB → private Control A/B.
- 운영 접속: private EC2의 SSM Agent → 중앙 SSM Endpoint·내부 DNS. S3는 각 VPC의 Gateway Endpoint로 접근합니다.
- 외부 패키지·이미지: private 호스트 프록시 → Gateway A/B → 인터넷. NAT Gateway는 사용하지 않습니다.
- 집 모델: Control → Gateway 사설 중계 API → Tailscale HTTPS → 집 Windows PC의 모델 API. 실제 tailnet·GPU 통신은 후속 검증 대상입니다.
- 데이터: 날씨 원문 S3 → Kafka → Spark → MongoDB·Elasticsearch, 피드백 PostgreSQL Outbox → Kafka → Spark → PostgreSQL·S3 Delta.

PostgreSQL·MongoDB·집 PC의 단일 노드와 SSM Endpoint 1AZ 제약을 수용한 설계입니다. 상세 역할·사양·실행 규약은 [기술 기준](docs/handoff/infra/BASELINE.md), 그림은 [아키텍처 설계도 3장](docs/architecture/README.md)을 참고합니다.

## 저장소 구성

| 경로 | 역할 |
|---|---|
| [aws-infra/foundation/](aws-infra/foundation/README.md) | Terraform 네트워크·EC2·스토리지·IAM 정의, mock plan, inventory export, 실제 실행 wrapper |
| [aws-infra/runtime/](aws-infra/runtime/README.md) | Ansible·Compose·호스트 설정, 설치·health·배포·롤백, 서비스·중계 로컬 시험 |
| [aws-infra/lifecycle/](aws-infra/lifecycle/README.md) | 종료·보존·삭제 계획, 승인·journal·재개·관측 검증. 실제 AWS 삭제는 비활성 |
| [aws-infra/contracts/](aws-infra/contracts/README.md) | topology·inventory·서비스·artifact·공통 실행 잠금 계약 |
| [app/](app/README.md) | Django·React·광고 API·Docker Compose·Kafka·Spark·Elasticsearch 로컬 통합 소스 |
| [scripts/](scripts/) · [tests/](tests/) | 준비 검사와 파트 간 실행·종료·백업 연계 검증 |
| [.github/workflows/](.github/workflows/) | PR 검사와 별도 수동 실행 workflow |
| [docs/handoff/infra/BASELINE.md](docs/handoff/infra/BASELINE.md) | 공개 가능한 인프라 기준 계약. 개인정보 감사·작업 세션·점검 보고서는 로컬 Git 제외 |

## 로컬 준비와 검증

명령은 저장소 루트에서 실행합니다. 고정 버전 Python **3.14.8**을 사용하며, 전체 통합 검사에는 실행 중인 Docker와 각 파트의 고정 도구·이미지가 필요합니다. 지원 검증 호스트는 macOS ARM64와 Linux X64입니다.

공통 준비 검사:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-common.lock.txt
make prep-check prep-test
```

전체 검사를 처음 실행할 때 파트별 로컬 환경도 준비합니다. 아래 준비 단계는 공개 의존성을 다운로드하며 AWS 자원을 생성하지 않습니다.

```bash
.venv/bin/python aws-infra/foundation/scripts/setup-local.py
bash aws-infra/runtime/scripts/prepare-lab.sh
python3 -m venv aws-infra/lifecycle/.venv
aws-infra/lifecycle/.venv/bin/python -m pip install -r aws-infra/lifecycle/requirements.lock.txt
aws-infra/lifecycle/.venv/bin/python aws-infra/lifecycle/scripts/setup-toolchain.py
make predeploy-check
```

| 명령 | 검사 범위 |
|---|---|
| `make prep-check prep-test` | 문서 링크·소유권·공통 계약과 준비 검사 자체의 회귀검증 |
| `make foundation-unit foundation-integration` | Foundation 단위 검사·7개 Terraform root의 모의 plan·inventory·실행 wrapper |
| `make runtime-unit runtime-integration` | Runtime 단위·Ansible 구문/lint·실제 로컬 컨테이너 서비스 시험 |
| `make lifecycle-unit lifecycle-integration` | 종료·삭제 계획·보존·실패·재개·관측의 로컬 시험 |
| `make integration-lifecycle` | Foundation 산출물·공통 잠금·Runtime 종료와 실제 로컬 DB 백업/복원 연계 |
| `make predeploy-check` | 공통·Foundation·Runtime·Lifecycle·파트 간 검사를 필요한 순서대로 실행 |

`integration-lifecycle`은 Foundation 통합 검사의 모의 산출물과 Runtime·Lifecycle 환경을 사용합니다. 최초 검증에는 환경 준비 후 `make predeploy-check`를 권장합니다. 세부 도구 버전·환경 조건은 각 파트 README와 [TESTING](docs/handoff/infra/TESTING.md)을 따릅니다.

현재 전체 로컬 결과는 공통 45, Foundation unit 29·wrapper 17·7 root 모의 plan, Runtime unit 57·integration 25, 기존 공동 실행 3, Lifecycle unit 33·integration 19, 신규 공통 연계 18 tests 통과입니다. 로컬 컨테이너 시험은 AWS 3AZ·EC2 amd64·SSM·집 PC GPU의 실제 검증을 대신하지 않습니다.

## 실제 AWS 배포 전 남은 작업

실제 AWS 적용 전에는 비공개 운영 인수 체크리스트와 권한 정책을 별도 승인 경로에서 검토하고 다음 항목을 확정해야 합니다.

1. 계정별 생성·배포·상태 접근 권한, GitHub OIDC·environment, 실제 AMI·AZ ID·CIDR·기존 자원 소유권·예산을 인수합니다.
2. 모든 실행 주체가 같은 operation storage를 사용하도록 identity·bucket·S3 Endpoint 정책을 연결하고, NAT가 없는 private 노드의 STS 접근 경로를 마련합니다.
3. 실제 업무 앱·이미지 digest·artifact·secret·DB 자격증명·운영 종료 관측·독립 백업/복원·보존 정책을 인수합니다.
4. 준비된 Linux X64 runner 검사와 실제 Terraform plan·비용 검토 후 승인을 받아 생성·배포를 진행합니다. 이후 SSM·DNS·Peering·S3·서비스·집 Tailscale/GPU·장애 복구를 실환경에서 검증합니다.

로컬 검증 과정에서 이 인프라를 AWS에 배포하지 않았습니다. 계정 자원 상태·권한·과금 내역은 공개 저장소가 아닌 별도 승인된 운영 기록으로 확인해야 하며, 계정 전체가 비어 있거나 비용이 0이라는 판정은 아닙니다.

## 협업과 작업 지침

팀은 [CONTRIBUTING](CONTRIBUTING.md)의 Issue → 개발·검증 → Commit·Push → PR·리뷰 → Merge 절차를 따릅니다. 인프라 작업의 통합 브랜치는 `feature/infra`이며 `develop`·`main` 반영은 릴리스 절차로 관리합니다. 신규 커밋은 한국어 변경 내용과 실제 Issue 번호를 기록합니다.

- 작업 기준: [파일 소유권](docs/handoff/infra/OWNERSHIP.md), [공통 연계 계약](aws-infra/contracts/infra-coordination.v1.md), [인증 전 로컬 검증](aws-infra/contracts/local-validation.v1.md).
- 개인용 지침과 프롬프트는 각 checkout의 Git 제외 `AGENTS.md`와 `.local/agents/`에 둡니다. 새 clone에는 자동 포함되지 않습니다.
- 개별 개발자의 Orca 설정·세션 로그·보안 점검 자료는 공개 저장소에서 제외하고 로컬 전용으로 관리합니다.
