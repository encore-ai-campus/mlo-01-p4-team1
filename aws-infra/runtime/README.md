# Runtime 로컬 준비

이 파트는 B 소유이며 계약 1.0.0을 소비합니다. 실제 AWS·집 PC에 연결하지 않고 16노드 계획·호스트 준비·실행 실패 처리·최소 실제 서비스 시험을 제공합니다. 실제 앱과 인증 binding을 받기 전에는 `ready_for_aws=false`입니다.

## 설치와 검사

Track2의 artifact/query·release·관측 수집·TLS gate와 미인수 입력은 [Track2 실행 경계](docs/TRACK2.md)에 있습니다.

## 현재 설계 대상: EC2 직접 설치와 systemd

Kafka·Spark·Elasticsearch와 Spark HA용 ZooKeeper는 Ubuntu 24.04/x86_64 호스트에 직접 설치하는 구조로 변경합니다. 공통 계약은 [native host 계약](../contracts/native-host.v1.json)이며 현재 상태는 **실행기 구현·로컬 검증, 실제 호스트 인수 대기**입니다. 나머지 웹·DB·관측 보조 서비스는 기존 Compose 범위를 유지합니다. Control의 Spark driver는 lease에 묶인 systemd 실행기와 연결하며 실제 원격 failover 인수는 별도입니다. [Native 배포 인계 문서](docs/NATIVE-PREDEPLOY.md)에 설치물·단계별 명령·증빙 경계가 있습니다.

- Foundation: EC2/EBS/네트워크를 유지하고 `DataExecutionTarget` 태그와 `foundation.native_host_design` output으로 목표 실행 방식·OS·차단 항목을 전달합니다. Terraform user_data/remote-exec로 설치·포맷·secret 전달을 수행하지 않습니다.
- Runtime: 검증된 AMI 또는 오프라인 artifact, `/opt/mlo/native/releases/<제품>-<digest>`, `/etc/mlo-native/<generation>/<node>/<revision>`, EBS의 기존 데이터 경로를 사용합니다. 고정 archive 체크섬 검증과 서명·OS/Python·커넥터 인수를 구분하며 배포 직전 최신 패키지를 자동 설치하지 않습니다.
- systemd: 전용 비로그인 사용자, foreground 실행, `KillMode=control-group`, CPUQuota/MemoryMax, EBS mount·UUID 선검증, 제한된 journald, `LoadCredential`을 사용합니다. 원본 secret 디렉터리를 서비스 계정에 공개하지 않습니다. JVM 비힙/페이지 캐시/Python과 호스트 여유는 별도로 측정합니다.
- Lifecycle: driver unit의 owner/generation/epoch 및 cgroup 전체 종료와 모든 master/worker의 executor 부재를 확인합니다. `docker stop`이나 PID 하나의 종료는 native 서비스 종료 증거가 아닙니다. KRaft UUID/ZooKeeper myid/ES UUID와 S3 checkpoint를 보존합니다.
- `native_host` 설치·기동·protocol-health·동일 binary 구성 rollback, `native_execution`/`native_fence` driver 입장·종료 관찰, Lifecycle native 단계가 구현돼 있습니다. 실제 호스트·원격 SSM·복원 인수는 아직 남아 있습니다. 기존 Compose installer의 실제 Kafka/Spark/ES 배포는 거부하며, Control은 체크섬으로 고정한 보호 native 계약과 일치하는 supervisor backend를 요구합니다. 로컬 Docker 회귀 시험을 native/systemd 기동 성공으로 표시하지 않습니다.

다음 명령은 9개 데이터 호스트의 systemd unit·호스트 경로 설정을 JSON으로 **검토용 렌더링**합니다. 설치·활성화·서비스 시작을 실행하지 않으며 기존 출력도 덮어쓰지 않습니다. 실제 inventory 출력은 `.private/` 안에만 허용합니다.

```bash
PYTHONPATH=aws-infra/runtime/src aws-infra/runtime/.venv/bin/python -m mlo_runtime.native \
  --inventory aws-infra/contracts/inventory.example.json \
  --output .local/native-host-review.json
```

`accepted-host.json` 파일 존재 조건은 후보 unit의 실수 활성화를 막는 보조 장치입니다. 파일을 수동 생성해도 installer의 native 인수 차단을 해제할 수 없습니다. 인증된 binary·실제 mount UUID·권한·TLS·readiness·systemd 재시작/데이터 복구 검증을 완료한 뒤 별도 구현에서 활성화 경로를 연결해야 합니다.

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

### 기존 Docker 회귀 시험의 저부하 자원 프로파일

- `src/mlo_runtime/resources.py`의 `low-load-48h-v1`을 사용합니다. 서비스 48시간에 준비·종료 여유를 더해 과금 시간은 52시간으로 계획하며, 이 값이 자동 종료 타이머를 구성하지는 않습니다.
- Spark worker의 Docker CPU 상한과 광고 코어를 각각 1로 맞춥니다. 기존 query와 `venues-catalog` 모두 executor 1core/1GiB, application 전체 최대 1core, driver heap 1GiB입니다. 동적 할당은 사용하지 않습니다.
- 총 3개 worker 슬롯 중 상시 `weather-stream`, `feedback-postgresql`에 각각 1개를 먼저 확보합니다. `feedback-analytics-batch`와 `venues-catalog`는 남은 슬롯을 공유합니다. 배치 앱은 종료 시 SparkContext를 닫아 슬롯을 반환해야 합니다. 별도 영속 큐·우선순위 스케줄러를 추가한 것은 아닙니다.
- 앱 artifact가 SparkConf로 위 상한을 덮어쓰지 않는지 인수하고, master/worker 관측으로 실제 할당을 확인합니다. 로컬 클러스터 시험은 두 상시 앱 + 실행 중 배치 + 대기 배치, 슬롯 반환 후 진행, lease 상실 시 전체 fencing을 검사합니다.
- 컨테이너 메모리 제한과 EC2 크기는 유지합니다. 특히 Control의 driver/Python·OS 사용량과 standby 승격, Spark JVM/비힙 메모리는 실제 측정해야 합니다. 이 프로파일의 렌더링만으로 AWS readiness를 승인하지 않습니다.
- 위 Docker 프로파일은 전환 이전 동작을 검증하는 자료입니다. native 설계의 CPU 1core 상당 제한은 `CPUQuota=100%`로 옮기며 실제 기동·쿼리 스케줄링은 systemd/앱 인수 후 다시 시험합니다.
- 로컬 Kafka/ES는 최소 단일 노드입니다. RF3/minISR2·ES 3노드·3AZ·실제 모델과 업무 앱은 별도 인수가 필요합니다.

시험 자원은 `mlo-p4-b-<역할>-<run-id>` Compose project와 라벨을 가집니다. 종료 시 해당 project의 시험용 볼륨만 정리하고 원본 로그·archive는 `.artifacts/`에 남깁니다. `docker system prune`·전역 volume 정리는 없습니다.

실행과 복구·남은 인수 조건은 [운영 인수 명세](docs/ACCEPTANCE.md), 현재 업무 소스와 필요한 배포 입력은 [앱 인수 계약](docs/APP-INPUTS.md)에 정리합니다. 최신 시험 결과는 로컬 B 인계에 기록합니다.

공유 Artifact 생산 API, 보호 host binding, 실제 설치·롤백·stateful 백업 gate와 추가 시험 근거는 [설치 보완 기록](docs/INSTALLATION-REVISION-2026-10-30.md)에 있습니다.

## CI와 실행 workflow

`runtime-check.yml`의 PR 검사는 AWS 인증 없이 공통·단위·Ansible 검사를 실행합니다. 실제 Docker 통합은 수동 입력으로 고정 도구가 설치된 `mlo-runtime-locked` amd64 runner에서 실행하도록 분리했습니다. 이 GitHub Actions 실행 자체는 이번 세션에서 하지 않았습니다.

`image-build.yml`은 수동 trigger와 기본 미설정 enable 변수를 가진 로컬 시험 이미지 빌드입니다. GHCR 게시와 업무 앱 빌드 입력은 아직 인수 전입니다. `app-deploy.yml`도 기본 비활성이며 private repository·main·별도 보호 environment와 승인 review SHA256·공개키/비공개 review 입력을 요구하는 공통 실행기를 호출합니다. 이 workflow나 실제 SSM 배포를 로컬 시험 성공으로 표시하지 않습니다.
