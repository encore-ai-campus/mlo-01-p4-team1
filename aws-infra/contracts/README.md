# 공통 개발 계약 v1

공통 기준 담당자가 관리하는 읽기 전용 계약입니다. A와 B는 자기 작업에서 그대로 소비하며 변경 요청은 자기 `docs/handoff/infra/agent-*/requests/`에 남깁니다. 현재 계약 버전은 1.0.0입니다.

## 파일과 데이터 흐름

| 파일 | 의미 |
|---|---|
| `topology.v1.json` | 16노드 역할·계정·사양·논리 AZ·직접 Peering |
| `services.v1.json` | 개발 포트·출발 역할·S3 경로. 실제 연결 성공이나 보안 완성의 증거는 아닙니다 |
| `inventory.schema.json` | A exporter가 생산하고 B adapter가 소비하는 JSON Schema 2020-12 |
| `inventory.example.json` | 16노드 합성 fixture. 실제 AWS ID·CIDR·AZ 선택값이 아닙니다 |
| `runtime-plan.schema.json` | B renderer의 비파괴적 로컬 계획 출력 |
| `runtime-plan.example.json` | 합성 inventory에 대응하는 계획 예제 |
| `ownership.v1.json` | 정확한 파일 예외와 역할별 경로 소유권 |

example fixture는 `example_only=true`이며 가짜 instance/VPC/Endpoint ID·10.200~204 대역·AZ ID·bucket ref를 사용합니다. 실제 대상에 실행하지 않습니다. 실제 inventory는 자기 `.private/`에 두고 `example_only=false`로 명시합니다. 로컬 검사 명령은 example만 사용합니다.

## inventory 규칙

- region은 ap-northeast-2, account key는 encore1~5, node name/role/spec/논리 AZ는 topology와 일치합니다.
- logical AZ a/b/c와 physical AZ ID는 일대일이어야 합니다. 세 Kafka·Spark·ES는 3AZ, 두 Control·Gateway는 2AZ입니다.
- VPC CIDR은 중복·중첩하지 않고 각 노드 IP는 해당 VPC 안에서 고유해야 합니다.
- Peering은 자기 연결·중복·역방향 중복 없는 account 쌍 10개입니다.
- secret은 `runtime-secret:<논리이름>`, IAM은 `role-ref:<논리이름>` 참조입니다. 실제 비밀번호·토큰·키를 이 계약에 넣지 않습니다. 실제 IAM binding·secret 전달 방식은 파트별 private binding 입력에서 인수합니다.
- storage ref는 원문·lake·backup·state·전송 임시 영역을 구분합니다. 데이터 버킷과 state 버킷을 혼용하지 않습니다.

## runtime-plan 규칙

environment·generation·example_only는 입력 inventory와 같습니다. node당 계획은 한 개이며 role과 서비스 목록이 공통 계약과 일치해야 합니다. v1 계획은 `ready_for_aws=false`, `destructive=false`입니다. 렌더링 성공을 AWS 배포 가능 판정으로 오인하지 않게 합니다.

v1은 core inventory와 비파괴적 서비스 계획의 경계입니다. 최종 배포에 필요한 이미지 digest·실제 인증 binding·SSM Document version/hash·migration·모델 API는 별도 입력과 검수 대상입니다. 값이 없으면 미정으로 기록하고 가짜 digest/hash를 실제 검증값으로 만들지 않습니다.

## 서비스 기본값

기존 web 8000·PG 5432·MongoDB 27017을 출발점으로 정했습니다. 그 밖의 포트는 이번 병렬 개발의 명시적인 기본값입니다. 외부에 공개하는 허용 목록이 아닙니다. A는 방향·대상 role/node로 SG를 한정하고 B는 listener·인증·TLS·host 방화벽을 맞춥니다. ALB·SSM·S3·Tailscale 같은 관리형/외부 경로는 별도 요구도 확인합니다.

Spark의 19000~19099 pool은 여러 Driver·block manager의 고정 할당 범위입니다. B는 query별 포트를 할당하고 같은 호스트 충돌을 검사합니다. A는 필요한 Control/Spark 방향에만 이 pool을 허용합니다. 대역 전체를 인터넷이나 모든 역할에 여는 뜻이 아닙니다.

## 검증과 버전 변경

```bash
make prep-check prep-test
.venv/bin/python scripts/check_preparation.py --inventory <합성-inventory.json>
.venv/bin/python scripts/check_preparation.py --inventory <합성-inventory.json> --runtime-plan <계획.json>
```

schema 검사와 별도로 배치·CIDR·주소·Peering·서비스·generation 의미 검사를 수행합니다. 예제 검증은 AWS·DNS·SG 실통신을 확인하지 않습니다.

필드 추가·삭제·의미 변경은 공통 기준 담당자가 양쪽 영향과 테스트·예제를 함께 검토합니다. 두 작업자가 같은 버전을 서로 다른 의미로 사용하지 않습니다.
# 2026-10-30 실행 계약 보완

`runtime-execution.v1.md`는 credential_process/controller 전송 권한과 A/B 공통 gzip TAR 실행 규약을 정의합니다. `runtime-artifact.schema.json`과 표준 라이브러리 전용 `artifact_verifier.py`가 manifest·외부 해시·안전한 압축 해제·대상 결합을 검사합니다. 실제 AWS 입력·비밀값을 예제 계약에 넣지 않습니다.

## 계정 인증 전 공통 실행 경계

[preauthentication.v1.md](preauthentication.v1.md)는 운영 잠금 권한·private STS 경로·서명된 별도 승인·고정 SSM 배포·retain-data 저장 plan 적용·비활성 수동 workflow의 계약과 입력을 정의합니다. AWS 인증 없이 `make preauth-check`로 검사하며 전체 `make predeploy-check`의 마지막 단계에도 포함됩니다.
