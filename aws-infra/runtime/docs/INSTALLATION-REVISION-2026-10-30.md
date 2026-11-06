# 2026-10-30 Runtime 설치·identity·원자 렌더링 보완 기록

통합 담당자가 준비한 `.artifacts/remediation-2026-10-30/workspace` 사본에서 수정·시험했습니다. 원 Orca worktree, Git commit/push/merge, AWS API, 집 PC에는 변경하지 않았습니다. 전체 HANDOFF/STATUS는 통합 담당자가 공동 검증 결과와 함께 갱신합니다.

## 변경 내용

- `src/mlo_runtime/render.py`: Kafka/ZooKeeper ID를 승인 노드명 `kafka-01..03`, `spark-01..03`에서 고정하며 voters/ensemble도 같은 순서로 만듭니다. ZooKeeper 이미지의 myid 자동 생성 entrypoint를 우회하고 설치기가 영속 myid를 검사합니다. 16노드를 staging에서 모두 생성하고 bundle 게시 후 별도 plan을 교체하며 쓰기·부분 호스트·plan 게시 실패 시 이전 값을 복구합니다. 파일 lock으로 동일 bundle/plan 동시 렌더링을 거부합니다. 원자 snapshot은 bundle 내부 `runtime-plan.json`과 host 디렉터리입니다. 외부 plan과 다른 파일시스템까지 하나의 원자 트랜잭션으로 보장하지 않습니다.
- `artifact.py`: 공통 `aws-infra/contracts/artifact_verifier.py`만 호출합니다. archive와 manifest SHA256을 모두 외부 입력으로 받습니다.
- `packager.py`: 공통 manifest-v1과 gzip TAR를 생산합니다. 고정 entrypoint, 선택 호스트, runtime Python 소스와 공통 계약을 포함합니다. 운영 이미지의 immutable digest와 실제 secret 파일 경로를 검사하고 YAML Compose를 표준 JSON으로 변환해 호스트 bootstrap의 PyYAML 의존성을 없앱니다. 명시적 `fixture:true`만 no-service 시험을 허용합니다.
- `install.py`: 표준 라이브러리만으로 시작하며 보호 host binding과 artifact hash·target·역할·image·secret·mount를 검사합니다. 새 immutable release에 staging하고 Compose readiness 후 `/opt/mlo/current`, `/etc/mlo/current`를 전환합니다. 실패하면 이전 release/Compose/units를 복구하며 첫 설치 실패는 부분 Compose 자원을 중지합니다. 전체 lifetime deploy lock과 성공/실패 receipt를 남깁니다. stateful deploy와 rollback 모두 현재 generation·만료 전 검증 백업/독립 사본 수락이 필요합니다.
- `ansible/`: 실제 site는 install 실행 입력을 필수로 요구합니다. 설치 role은 manifest hash별 incoming 디렉터리에 복사한 뒤 고정 entrypoint만 호출합니다. 기존 호스트 준비 lab은 별도이며 `install-lab.yml`이 실제 설치 task 재적용을 시험합니다.
- Control supervisor systemd, Gateway relay의 실제 Python module·보호 binding·runtime code mount와 `--check`, 명시적으로 호출하는 stateful-stop unit을 연결했습니다. systemd activation 시 stale 관리 service/timer는 중지·제외합니다. 실제 systemd/SSM은 이번 Docker 시험 범위 밖입니다.

## 생산과 실행 API

controller Python 환경에서 호출합니다. 이 명령은 로컬 artifact만 작성하며 게시·원격 실행하지 않습니다.

```text
mlo_runtime.packager.package(bundle, output,
    artifact_version=..., environment=..., generation=...,
    node_roles={node: role}, image_digests={name: immutable_ref},
    required_secrets={node: ['/run/mlo/secrets/name']}, fixture=False)
```

반환값은 `archive`, `sha256`, `manifest_sha256`, `manifest`입니다. CLI는 `python -m mlo_runtime.packager --bundle ... --output ... --artifact-version ... --environment ... --generation ... --binding ...`이며 binding에는 `node_roles`, `image_digests`, `required_secrets`, 선택 `fixture`를 넣습니다. 기존 output artifact를 덮어쓰지 않습니다.

A dispatcher가 두 hash와 metadata를 공통 verifier로 확인한 뒤 고정 `bin/runtime-entrypoint.py`를 `python3`로 실행합니다.

```text
python3 <verified-payload>/bin/runtime-entrypoint.py \
  --operation install|deploy|health|rollback \
  --environment <environment> --generation <generation> --node <node> \
  --binding /etc/mlo/host-binding.json
```

생산 host binding은 root 소유·0600의 고정 경로만 허용합니다. 필수 target은 `schema_version=1.0.0`, `instance_id`, `node`, `role`, `environment`, `generation`입니다. 추가로 다음 입력을 인수해야 합니다.

- `accepted_manifests`: 외부에서 수락한 manifest SHA256 목록입니다.
- `image_digests`, `required_secrets`: manifest와 맞는 immutable 이미지 및 보호 파일의 절대 경로입니다.
- `mount_uuid`: 기존 `/srv/mlo` 파일시스템 UUID입니다. format/mount/자동 초기화는 하지 않습니다.
- `persistent_owners`: 서비스별 승인 이미지 uid/gid입니다. 기존 소유권이 다르면 실패하며 자동 recursive chown하지 않습니다.
- Kafka/Spark의 `cluster_identity.node_id`, Kafka의 `cluster_identity.cluster_id`: Kafka는 별도 검수된 기존 `meta.properties` 없으면 시작하지 않습니다. 자동 storage format은 없습니다. ZooKeeper는 data 디렉터리가 정말 빈 경우에만 myid를 최초 기록하며 기존 값이 다르거나 data만 있고 myid가 없으면 거부합니다.
- stateful 교체/rollback의 `stateful_change`: `backup_verified=true`, `remote_copy_verified=true`, 현재 `generation`, 미래 `expires_at`이 필요합니다. 이 보호 수락값은 실제 백업/독립 사본 검수의 대체가 아닙니다.
- 실제 운영용 AMI의 Docker/Compose 5.5.1, 오프라인 host Python runtime 의존성, 보호 supervisor/relay/stateful/job binding을 준비해야 합니다. bootstrap installer가 통과해도 실제 timer/업무 앱이 준비됐다는 뜻은 아닙니다. supervisor의 psycopg 3.3.6 및 jsonschema/PyYAML 버전은 실행 시 확인합니다.

`lab:true`는 별도 `mlo-*` disposable root 안의 보호 binding으로만 허용합니다. fixture artifact는 생산 binding에서 거부됩니다. 설치 receipt는 `<root>/opt/mlo/receipts/`이며 secret/body는 기록하지 않습니다. 모든 로컬 결과의 `ready_for_aws`는 false입니다.

## 시험과 결과

현재 보완 시점의 unit 전체 57개를 포함하여 최종 공통 suite의 숫자는 통합 HANDOFF를 기준으로 합니다. 소유 시험은 artifact 3개, atomic render 5개, identity/실행 배선 4개, installer 9개입니다.

```bash
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=aws-infra/runtime/src \
  aws-infra/runtime/.venv/bin/python -m unittest discover \
  -s aws-infra/runtime/tests/unit -v

env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=aws-infra/runtime/src \
  aws-infra/runtime/.venv/bin/python -m unittest discover \
  -s aws-infra/runtime/tests/integration -p test_install.py -v

env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=aws-infra/runtime/src \
  aws-infra/runtime/.venv/bin/python -m unittest discover \
  -s aws-infra/runtime/tests/integration -p test_zookeeper.py -v
```

- pass: 잘못된 hash/manifest/target/role·실제 secret 누락·동시 deploy 거부, 8번째 host·ENOSPC·plan 게시 실패 시 이전 bundle 보존, inverse/random inventory의 node ID 고정, 기존 Kafka metadata/ZK myid 보존, stateful rollback 백업 gate입니다.
- pass: Linux stdlib entrypoint install/reapply/health/v2 deploy/v1 rollback 및 Ansible 실제 설치 task 재적용 `changed=0`, 실제 Linux Compose readiness 실패 후 이전 revision 재기동의 2개 통합 시험입니다.
- pass: ZooKeeper 3.9.4 고정 이미지의 실제 TLS 3노드 quorum에서 데이터 생성·한 노드 중단 후 조회·두 노드 중단 시 쓰기 차단·quorum 복구·전체 재시작 후 데이터/myid 보존 1개 시험입니다.
- fail 후 수정: 초기 ZooKeeper CLI harness가 stdout만 검사했으나 응답은 stderr였고, 재시작마다 기존 truststore alias를 다시 만들었습니다. 두 시험 harness를 수정해 최종 pass를 확인했습니다. 실패 로그도 보존합니다.
- not_run: 실제 systemd host activation, EC2 AMD64, IMDS, 실제 SSM/S3/IAM, AWS 3AZ, Kafka RF3/minISR2·ES 3노드, 실제 업무 앱·TLS·독립 백업, 집 Windows/Tailscale/GPU입니다. Compose 시험의 controller mount probe는 명시적 lab 대역이며 실제 mount와 권한은 별도 Linux/Ansible 시험으로 확인합니다.

unit/install 최종 로그는 `runtime/.artifacts/install-revision-20261030/`, Linux·ZK 명령 로그는 `runtime/.artifacts/mlo-p4-b-installer-*/commands.log`, `runtime/.artifacts/mlo-p4-b-zookeeper-quorum-*/commands.log`입니다. 매 run project는 고유하며 해당 컨테이너/네트워크/시험 볼륨만 정리하고 로그와 fixture는 보존했습니다. 전역 Docker/외부 볼륨 변경은 없습니다.
