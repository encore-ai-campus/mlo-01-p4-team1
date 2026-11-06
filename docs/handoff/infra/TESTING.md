# 배포 전 로컬 검증과 인계 기준

첫 목표는 각 파트의 구현·로컬 단위·통합 테스트입니다. 로컬 통과와 실제 AWS·집 PC 수용 시험은 구분합니다. 구현되지 않은 suite·필수 시험의 skip·의존성 부족은 통과가 아닙니다.

## 공통 검증 환경

공통 검증은 저장소 전용 Python venv와 `requirements-common.lock.txt`를 사용합니다. 현재 준비 검증 환경은 Python 3.14.8입니다. A/B는 자기 toolchain 정확한 버전과 lock을 자기 소유 영역에 추가하며 공통 lock을 변경하지 않습니다. Docker 데몬·인터넷·Linux 환경이 필요한 검증은 요구를 먼저 기록합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-common.lock.txt
make prep-check prep-test
```

공통 checker는 파일 소유권·로컬 링크·schema·합성 데이터·topology·통신 계약을 검사합니다. AWS CLI·SDK를 실행하지 않습니다. dependency 설치는 네트워크가 필요하지만 검증 자체는 AWS 인증을 요구하지 않습니다.

## 역할별 명령 인터페이스

| 명령 | 구현 담당과 의미 |
|---|---|
| `make foundation-unit` | A의 단위 suite |
| `make foundation-integration` | A의 mock Terraform·exporter·wrapper 통합 |
| `make runtime-unit` | B의 렌더링·상태 전이·실패 처리 단위 suite |
| `make runtime-integration` | B의 adapter·Ansible·로컬 서비스 통합 |
| `make integration-contract` | 통합된 A exporter → B renderer → 공통 계약 검사 |
| `make integration-execution` | 실제 SDK credential_process·모의 STS와 A의 평가된 SSM dispatcher → 실제 B tar.gz installer 연결 |
| `make predeploy-check` | 위 모든 검사와 공통 검증 |

각 역할은 `aws-infra/<foundation|runtime>/scripts/check-local.sh unit|integration|all`을 제공합니다. 필수 구현·suite·도구가 없으면 명시적으로 NOT READY로 실패해야 하며 빈 성공 스크립트로 대체하지 않습니다.

공동 통합용 실행 인터페이스는 다음과 같습니다. 두 스크립트는 실제 AWS에 연결하지 않고 입력/출력 JSON 파일만 사용합니다.

```text
A: bash aws-infra/foundation/scripts/render-fixture.sh --output <파일>
B: bash aws-infra/runtime/scripts/render-fixture.sh --inventory <A-출력> --output <파일>
```

A는 모의 Terraform 결과를 실제 exporter에 연결합니다. B는 입력을 실제 adapter/renderer에 연결합니다. 공통 example 파일을 복사한 출력만으로 구현을 대체하지 않습니다. 두 branch 통합 전에는 각자 common example로 독립 검증하고, 공동 producer-consumer 검증은 통합 담당자가 같은 checkout에서 실행합니다.

## 최소 시험 목록

| ID | 담당 | 단위 또는 통합 수용 기준 |
|---|---|---|
| A-U1 | A | 잘못된 topology·CIDR·AZ·account·누락/한쪽 경로를 거부 |
| A-U2 | A | state 소유권·잠금·S3 보존·IAM/PassRole·origin/cache 정책의 변형 실패 |
| A-U3 | A | exporter 누락·중복·타 generation·fixture 혼합을 거부 |
| A-I1 | A | 실제 provider 호출 없는 mock 구성으로 5account+connectivity 조합 |
| A-I2 | A | 모의 output → 실제 exporter → schema/의미 검사 |
| A-I3 | A | 가짜 CLI에서 wrapper의 실패·재개·실제 apply 차단 |
| B-U1 | B | inventory·서비스·secret 참조·포트 충돌·렌더링 변형 실패 |
| B-U2 | B | lease 상실·old owner·종료 실패·idle 미확인에서 승계 차단 |
| B-U3 | B | retry ID/기한·취소·부분 배포/적재·백업 실패 처리 |
| B-I1 | B | fixture → 실제 adapter → 16node 렌더링 → runtime-plan 검사 |
| B-I2 | B | disposable Linux에서 Ansible 재적용·권한·영속 경로·기동 확인 |
| B-I3 | B | 실제 로컬 proxy A 종료 후 B 새 연결·인증·readiness·웹 롤백 |
| B-I4 | B | 가능한 최소 규모 실제 Kafka/Spark/Delta/DB/ES의 단계별 생산·저장·재시작·복원 |
| X-I1 | 통합 | A의 새 출력 → B의 실제 renderer. 입력 변형·generation 혼합 시 실패 |
| X-I2 | 통합 | 공통 gzip TAR·외부 archive/manifest hash·IMDSv2 대상 결합·고정 entrypoint·설치/health/rollback·부분 실패와 이전 데이터 보존 |
| X-I3 | 통합 | A의 고정 CLI bridge를 실제 boto3 credential_process consumer로 실행·모의 STS AssumeRole·만료/실패 source 거부 |

2026-10-30 보완 계약은 `aws-infra/contracts/runtime-execution.v1.md`와 `runtime-artifact.schema.json`입니다. `make integration-execution`은 A integration의 최신 평가 결과와 준비된 전용 Linux lab image를 사용하므로 `make foundation-integration`과 `bash aws-infra/runtime/scripts/prepare-lab.sh`가 먼저 필요합니다. 가짜 AWS CLI·로컬 IMDSv2만 사용하며 AWS 네트워크는 사용하지 않습니다. 비어 있는 서비스의 공동 artifact fixture는 bootstrap/복구 경계를 검사합니다. 실제 Compose 서비스 기동·health 실패 복원·Ansible changed=0은 B 설치 suite에서 별도로 검사합니다.

B의 필수 통합에는 `test_install.py`, `test_zookeeper.py`, `test_supervisor.py`, `test_relay.py`가 추가되었습니다. 실제 Linux 설치, TLS ZooKeeper 3노드 quorum·재시작, 실제 PostgreSQL CAS·두 supervisor process·self-fence·백업/복원, Gateway A/B HTTPS → fake home HTTPS 중계를 포함합니다. 실제 원격 SSM hook·업무 Driver·집 모델을 대신 검증했다고 표시하지 않습니다.

`.github/workflows/integration-check.yml`은 승인된 prepared Linux X64 runner에서 수동으로 전체 로컬 suite를 실행할 코드입니다. 원격 실행·runner 준비는 not_run입니다. push로 Terraform apply·stateful 변경·원격 배포를 시작하지 않습니다.

B-I4는 자원을 단계적으로 사용하고 모든 서비스를 동시에 기동하지 않아도 됩니다. TLS와 데이터 보존을 생략한 단순 응답을 전체 통합 성공으로 표시하지 않습니다. Mac 자원이 부족한 시험은 측정한 제약과 재현/이관 절차를 기록합니다.

## 결과와 readiness

모든 suite는 `pass / fail / blocked / not_run`, 명령·도구 버전·입력 버전·일시·근거·제약을 남깁니다. unit pass인데 integration blocked이면 전체 검증 완료로 표시하지 않습니다. 코드 구현 완료, 로컬 검증 완료, AWS 적용 준비 검토 완료, AWS 실제 성공을 따로 기록합니다.

전체 로컬 검증이 가능한 범위에서는 실패를 수정하고 재실행합니다. 실제 SSM/S3·교차 계정 IAM·3AZ·EC2 물리 fencing·IMDS·CloudFront 인증/캐시·Windows Tailscale/GPU·10명 부하는 실제 환경 검수로 인계합니다. fixture 성공으로 이 항목을 체크하지 않습니다.

## 역할별 기록

개인용 진행 기록·프롬프트·세션·인계 양식은 각 checkout의 Git 제외 `.local/agents/`에 둡니다. 원시 로그는 해당 파트의 `.artifacts/`에 저장합니다. 공개 PR에는 비밀값 없는 검증 명령·결과만 포함합니다.

## C — Lifecycle 첫 목표와 시험 인계

C의 첫 목표는 실제 AWS 변경·삭제 없는 종료·삭제 자동화 구현·로컬 검증입니다. 공개 수용 기준은 `aws-infra/contracts/local-validation.v1.md`와 Lifecycle README/tests를 따릅니다. 잘못된 대상·보존 데이터 삭제·증거/계획 변경·동시 실행·부분 실패·미확정 실행을 거부하고, C CLI에서 B 종료/백업 adapter와 실제 로컬 DB 복원까지 연결하는 시험을 포함합니다.

2026-10-31 공통 연계 변경부터 `make predeploy-check`는 A/B 기존 suite에 `lifecycle-unit`, `lifecycle-integration`, `integration-lifecycle`를 순서대로 추가합니다. 과거 10/30 실행 기록은 C를 별도 실행한 결과이며 당시 Makefile이 C를 포함했다고 소급 해석하지 않습니다.

`make integration-lifecycle`는 실제 A mock plan 7개 전체 → C 보존/삭제 set 검토, localhost S3 SDK 조건부 쓰기·별도 프로세스·응답 유실·공통 A/B/C 실행 잠금, 실제 로컬 PostgreSQL/MongoDB의 dump → 버전 사본 → 독립 컨테이너 복원 → 원본 정지를 검사합니다. 필수 module 누락·0 tests·skip·실패는 nonzero입니다. 실계정 credentials는 제거하고 SDK는 명시적 localhost 대역만 사용합니다.

통합 CI에도 C 전용 venv·검증된 Terraform archive 준비와 전체 target을 포함합니다. 관리자 준비 Python/wheelhouse/archive는 저장소 밖의 root 소유·쓰기 제한 경로여야 합니다. 해당 runner 준비 및 원격 Linux X64 CI 실행은 여전히 not_run입니다. 이 변경은 push에 따른 AWS 배포를 추가하지 않습니다.

C가 자기 구현을 마친 뒤 사용할 인터페이스는 다음과 같습니다. 실행 전에 파일·의존성이 실제로 준비됐는지 확인하며, 없으면 not_run/blocked로 기록합니다.

```text
bash aws-infra/lifecycle/scripts/check-local.sh unit
bash aws-infra/lifecycle/scripts/check-local.sh integration
.venv/bin/python scripts/check_preparation.py --owner C --base <확정한-공통-기준-SHA>
```

C는 구현된 로컬 경로와 A/B `make predeploy-check` 회귀를 같은 checkout에서 검증합니다. 실제 계정·SSM 종료·S3 백업·교차 root 삭제·보존 자원 처리·보호 해제·집 PC는 별도 실환경 인수 항목입니다. 모의 삭제로 AWS 전체 삭제 완료를 주장하지 않습니다.

## 실제 앱 운영 이미지의 배포 전 검사

인프라 suite에 이어 실제 공통 Django·광고 Django의 운영 target을 같은 공개 소스로 검증합니다.
`make app-images`는 현재 앱 파일 전체의 SHA256을 revision으로 고정하고 ARM64·AMD64 이미지를
로컬에 빌드합니다. `make predeploy-check app-predeploy`는 인프라와 실제 앱 readiness 검사를
순서대로 실행합니다. source revision이 다른 이미지 manifest는 거부합니다.

앱 검사는 새 project와 전용 DB에서 TLS·인증·DB migration·광고 Mongo replica set/스키마,
revision·secret 충돌 거부·DB 장애/복구·정상 종료를 확인하고 시험 볼륨만 정리합니다.
세부 입력은 [앱 운영 이미지 계약](../../../app/docker/PRODUCTION.md)을 따릅니다.
기존 개발/운영 데이터의 migration·삭제는 포함하지 않습니다.

결과와 원시 로그는 Git 제외 `.artifacts/`에 저장합니다. 로컬 image ID와 레지스트리 digest를
구분하고, 실제 AWS·물리 AMD64·3AZ·제품 전체 E2E·새 회원 기능 성공으로 확대하지 않습니다.
Mac의 AMD64 컨테이너 실행은 에뮬레이션 호환 시험입니다. 실제 EC2/systemd 인수는 별도입니다.
