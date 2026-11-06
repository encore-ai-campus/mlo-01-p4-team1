# Runtime 로컬 준비

이 파트는 B 소유이며 계약 1.0.0을 소비합니다. 실제 AWS·집 PC에 연결하지 않고 16노드 계획·호스트 준비·실행 실패 처리·최소 실제 서비스 시험을 제공합니다. 실제 앱과 인증 binding을 받기 전에는 `ready_for_aws=false`입니다.

## 설치와 검사

Track2의 artifact/query·release·관측 수집·TLS gate와 미인수 입력은 [Track2 실행 경계](docs/TRACK2.md)에 있습니다.

저장소 루트에서 다음 명령을 사용합니다. 공개 의존성 다운로드와 Docker 접근이 필요하며 전역 Docker 설정은 변경하지 않습니다.

```bash
bash aws-infra/runtime/scripts/prepare-lab.sh
make PYTHON=aws-infra/runtime/.venv/bin/python prep-check prep-test
make runtime-unit
make runtime-integration
```

기본 Docker 시험 플랫폼은 `linux/arm64`입니다. amd64 Linux로 이관할 때는 `RUNTIME_LAB_PLATFORM=linux/amd64`를 설치와 검사 모두에 지정합니다. ARM64 결과는 EC2 amd64 성공으로 표시하지 않습니다. OS/Python 의존성은 정확한 버전으로 고정했으며 해당 공개 버전이 배포처에서 제거되면 설치가 실패합니다. 자동 최신화하지 않습니다.

`scripts/check-local.sh unit|integration|all`은 각 구성 요소를 끝까지 실행하고 실패·도구 부족을 비정상 종료와 JSON summary에 남깁니다. `mlo_runtime.suite`는 실제 unittest 결과의 시험 수·skip·expected failure를 검사하며 빈 suite와 미완료 필수 시험도 실패로 처리합니다. 로그와 fixture 출력은 자기 `.artifacts/`에만 보존합니다.

## 렌더링

```bash
bash aws-infra/runtime/scripts/render-fixture.sh \
  --inventory aws-infra/contracts/inventory.example.json \
  --expected-generation fixture-v1 \
  --output aws-infra/runtime/.artifacts/plan.json \
  --bundle aws-infra/runtime/.artifacts/new-bundle
aws-infra/runtime/.venv/bin/python scripts/check_preparation.py \
  --runtime-plan aws-infra/runtime/.artifacts/plan.json
```

bundle은 16개 호스트를 별도 디렉터리에 모두 검증·작성한 뒤 게시합니다. 쓰기·8번째 호스트·plan 게시 실패 시 이전 bundle과 plan을 보존하고 동시 렌더링은 거부합니다. bundle 안의 `runtime-plan.json`이 호스트 설정과 함께 게시되는 기준 snapshot입니다. 별도 output plan은 마지막에 교체됩니다. `inventory.yml`은 계정별 `p4-controller-encoreN` SSM profile/region을, `hosts/<node>/`는 Compose·TLS/인증 설정·HAProxy·Docker proxy/no_proxy·timer·영속 경로를 담습니다. v1 schema가 허용하지 않는 상세 항목은 `runtime-details.json`에 같은 generation과 입력 hash로 묶었습니다. 기존 example 계획을 복사하지 않습니다.

실제 inventory도 읽기 전용으로 렌더링할 수 있지만 출력은 `.private/` 아래만 허용합니다. 예제 inventory의 `ansible_host` 값은 합성 instance ID이므로 실제 접속에 사용하지 않습니다. `ansible/site.yml`은 example과 실행 미승인 대상을 거부합니다.

## 검증 범위

- 단위: 잘못된 inventory·공개 노출·포트 충돌·누락 secret·generation 혼합, lease/종료 증거·old owner·timer lock, 배포/백업 실패 보존, artifact checksum/경로 검사를 수행합니다.
- 실제 Linux: Ubuntu 컨테이너에서 Ansible을 두 번 적용하고 `changed=0`·권한·전용 mount·재생성 후 데이터를 확인합니다. 컨테이너의 systemd는 실행하지 않으며 실제 systemd/SSM 수용 시험과 구분합니다.
- 실제 로컬 서비스: HTTP 프로세스·HAProxy·웹 컨테이너 교체/복구, Kafka TLS 생산/소비/재생성, PG·Mongo TLS/인증·백업/복원, ES TLS/인증·문서 보존, Spark/Delta 부분 sink 실패 후 checkpoint 재개를 단계적으로 실행합니다.
- 추가 실제 설치 시험: 공유 gzip TAR 검증 → 고정 Python entrypoint → Linux 설치·재적용·health·deploy·rollback과 Ansible 설치 role의 `changed=0`, 실제 Compose readiness 실패 후 이전 revision 복구를 확인합니다. 소스가 없는 업무 앱은 명시적 fixture로 구분합니다.
- ZooKeeper는 고정 이미지로 실제 TLS 3노드 quorum·한 노드 중단 후 조회·두 노드 중단 시 쓰기 차단·전체 재시작·myid/데이터 보존을 시험합니다. 추가 Spark cluster suite는 세 master·세 worker·TLS ZooKeeper에서 두 실제 Driver/executor, master 승계, 실제 PostgreSQL lease 연결 상실 시 container 종료와 전체 TLS 관측/idle을 검사합니다. 단일 Docker 호스트 ARM64 시험이며 실 AWS 3AZ 성공을 뜻하지 않습니다.
- 로컬 Kafka/ES는 최소 단일 노드입니다. RF3/minISR2·ES 3노드·3AZ·실제 모델과 업무 앱은 별도 인수가 필요합니다.

시험 자원은 `mlo-p4-b-<역할>-<run-id>` Compose project와 라벨을 가집니다. 종료 시 해당 project의 시험용 볼륨만 정리하고 원본 로그·archive는 `.artifacts/`에 남깁니다. `docker system prune`·전역 volume 정리는 없습니다.

실행과 복구·남은 인수 조건은 [운영 인수 명세](docs/ACCEPTANCE.md), 현재 업무 소스와 필요한 배포 입력은 [앱 인수 계약](docs/APP-INPUTS.md)에 정리합니다. 최신 시험 결과는 로컬 B 인계에 기록합니다.

공유 Artifact 생산 API, 보호 host binding, 실제 설치·롤백·stateful 백업 gate와 추가 시험 근거는 [설치 보완 기록](docs/INSTALLATION-REVISION-2026-10-30.md)에 있습니다.

## CI와 실행 workflow

`runtime-check.yml`의 PR 검사는 AWS 인증 없이 공통·단위·Ansible 검사를 실행합니다. 실제 Docker 통합은 수동 입력으로 고정 도구가 설치된 `mlo-runtime-locked` amd64 runner에서 실행하도록 분리했습니다. 이 GitHub Actions 실행 자체는 이번 세션에서 하지 않았습니다.

`image-build.yml`은 수동 trigger와 기본 미설정 enable 변수를 가진 로컬 시험 이미지 빌드입니다. GHCR 게시와 업무 앱 빌드 입력은 아직 인수 전입니다. `app-deploy.yml`도 기본 비활성이며 private repository·main·별도 보호 environment와 승인 review SHA256·공개키/비공개 review 입력을 요구하는 공통 실행기를 호출합니다. 이 workflow나 실제 SSM 배포를 로컬 시험 성공으로 표시하지 않습니다.
