# Lifecycle 후속 인터페이스와 신뢰 경계

이 문서는 로컬 구현과 운영 인수 계약을 구분합니다. 현재 CLI는 example_only=true·snapshot-only·예약된 합성 계정만 받습니다. 실제 AWS adapter, 실제 backend init/refresh/plan/destroy, SSM hook을 추가하지 않았습니다. Foundation의 기존 차단·prevent_destroy·deletion protection도 유지합니다.

## 1. Foundation 읽기 전용 exporter 제안

추출 주체는 Foundation입니다. 실제 API를 호출하지 않는 `snapshot_contract.audit_export()`는 **이미 제공받은 mock 정규화 envelope를 검사하는 소비 인터페이스**입니다. 실제 state 파일을 자동 탐색하거나 Terraform을 실행하지 않습니다. `offline-foundation-export-v1`은 다음 내용을 포함합니다.

| 입력 | 검증·후속 생산 책임 |
|---|---|
| binding | project/environment/generation, account/region, state key, lineage/serial. 신뢰 가능한 별도 binding과 snapshot 버전을 대조 |
| state.resources | module/count/for_each가 확장된 전체 address, module_address, type, provider, ID, ownership, dependencies, 보호 여부 |
| configuration | 정확한 확장 address별 평가된 references, prevent_destroy, deletion protection, 관측된 API 보호 속성 |
| provider_schemas | provider source/version/schema digest에 결합된 지원 resource type. 지금 mock은 type 분류만 시험 |
| expected_targets | 신뢰 가능한 생산자가 별도로 결합한 전체 address→ID 집합 |

resource/module 주소·중복·버전·account/region·state key·ownership·보호 충돌·references 누락·순환을 거부합니다. 미지원 type/provider schema/configuration은 unknown 목록과 blocker로 반환합니다. **unknown이 없어도 execution_enabled=false**입니다. caller가 함께 조작한 state/config/binding은 신뢰 가능한 외부 증거가 아니므로 이 검사가 실제 provenance를 보증하지 않습니다.

Foundation의 `lifecycle_export.py`와 C의 `foundation.py`는 별도 구현된 연계 경로입니다. `integration-lifecycle`는 A의 실제 mock plan 7개 전체를 exporter → C 보존 집합 → root별 저장 계획 검토에 연결합니다. raw state의 lineage/serial/ID 불일치와 계획의 누락·중복·replacement·ID 변경도 거부합니다. 8개 type·81개 CLI 합성 모델과 이 전체 mock graph를 구분하며, 어느 쪽도 실제 AWS graph 관측은 아닙니다.

실환경 생산자는 raw state의 lineage/serial과 Terraform show/config/provider schema를 매핑하고 module의 표현식을 전체 확장 주소로 해석해야 합니다. implicit/provider/API 의존성, ENI/EBS attachment·IAM·SSM·DNS·ALB·CloudFront·Route53·endpoint·peering·S3 object versions까지 전체 graph를 대조해야 합니다.

보호 판단은 snapshot의 bool 단독 값으로 승인하지 않습니다. 현재 simulator는 코드에 고정된 정책으로 data/network/backend=true를 강제합니다. 운영은 configuration digest, API 속성, ownership, freshness, 별도 검토된 정적 보존 구성과 정확한 protection_release 승인에 결합해야 합니다. 생성 config를 전역 치환하거나 state rm/force-unlock/target으로 우회하지 않습니다.

`check_partition(before, deleted, retained, after)`는 전체 집합 분할·중복·관측 잔여 set equality를 검사합니다. 실제 provider 관측과 address/ID 조합의 후속 비교도 필요합니다. Foundation 검토 결과를 실제 CLI bundle로 변환하거나 AWS 삭제를 실행하는 경로는 제공하지 않습니다.

## 2. 공통 실행 잠금 제안

계약 필드: `{resource_scope, environment, generation, operation, owner, epoch, issued_at, expiry, fencing_token}`입니다. operation은 create/deploy/destroy입니다. 잠금 key는 동일 물리 resource_scope/environment로 고정하고 generation을 바꿔 동시 소유할 수 없게 합니다. generation은 소유권 검증 필드입니다.

- 단일 authoritative store에서 acquire/renew를 원자적 CAS로 처리하고 epoch는 영구적으로 증가해야 합니다.
- 갱신·저장소 연결 실패 또는 expiry 이후 owner는 후속 효과를 시작하지 않습니다. 서로 다른 Control도 같은 store를 사용해야 합니다.
- 새 owner는 lease 만료만으로 이전 효과가 멈췄다고 가정하지 않습니다. 진행 중 작업 관측과 재승계 fencing이 필요합니다.
- A wrapper, B deploy·supervisor/Driver/timer/collector, C adapter 모두 효과 직전에 fencing token을 검증해야 합니다. 한 소비자가 무시하면 공통 상호 배제가 성립하지 않습니다.
- fence token을 검사할 수 없는 외부 API의 진행 중 요청은 취소됐다고 간주하지 않고 ambiguous로 기록합니다. 네트워크 단절 시 자동 재실행하지 않습니다.

`lease_contract.LeaseContract`는 **단일 메모리 reference model**이며 C execute에 연결하지 않습니다. 현재 공통 연계는 별도의 `infra_common.coordination.OperationLease`를 사용합니다. C execute는 lab flock과 함께 주입된 공통 guard를 각 단계에서 확인합니다. localhost S3 SDK 조건부 쓰기·영구 epoch·별도 프로세스·응답 유실·만료 후 자동 승계 차단·A/B/C 진입점의 배제를 `integration-lifecycle`에서 검사합니다. 실제 S3/IAM 권한·네트워크 단절·원격 효과 fencing은 실환경 인수 사항입니다. guard를 주입하지 않은 CLI 합성 lab은 로컬 flock만 사용합니다.

## 3. 종료·백업 증거

로컬 local-evidence-v1은 전체 record TTL 5분, scope/state/input digest, 엄격한 boolean facts와 runtime을 요구합니다. runtime은 예약된 synthetic-observer-v1 issuer, generation, 16개 node/instance 결합, Control A/B fencing·웹 admission·collector/timer/supervisor·weather-stream·feedback-postgresql·feedback-analytics-batch·Kafka producer/consumer·Spark driver/executor/master/worker·Delta checkpoint flush/object 확인을 포함합니다. 누락/다른 issuer/다른 instance/false/numeric true를 거부합니다.

이 필드는 **합성 발급자가 선언한 사실**입니다. 실제 Spark REST API·프로세스·systemd·Kafka·S3를 조회한 증거가 아닙니다. 운영은 서비스별 `{issuer, instance_id, generation, active_group, barrier_id, service, observed_at, expires_at, artifact_digest, result}`에 외부 서명을 요구하고, 필요한 모든 증거가 유효하기 전 DB stop/compute delete를 막아야 합니다. 신규 작업과 재승계를 먼저 막고 실제 Driver/executor 부재·flush를 관측해야 합니다.

운영 PostgreSQL은 쓰기 차단 장벽과 일관 snapshot/transaction, dump 시점·WAL/LSN·역할·확장·schema·복원된 카탈로그/행·업무 불변식이 필요합니다. MongoDB는 deployment 종류별 일관성/읽기 시점, oplog 사용 가능 여부, transaction·index·권한·collection 검증을 합의해야 합니다. 현재 단일 MongoDB에서 oplog/PITR가 가능하다고 가정하지 않습니다. 두 엔진 모두 dump 성공과 복원 가능 판정을 분리합니다.

운영 archive/사본 증거는 S3 bucket/key/version ID/checksum, 암호화·소유권·독립 failure domain, 원격 조회·복원 영수증과 쓰기 장벽에 결합해야 합니다. CLI의 SQLite independent 경로는 같은 lab/failure domain입니다. 별도 C `runtime.stop_engine_backends()` 연계는 B `ContainerStatefulBackend`를 소비하며 실제 disposable PostgreSQL/MongoDB에서 quiesce·dump·localhost 버전 사본·hash·독립 컨테이너 복원·원본 정지를 시험합니다. 이 local-lab 경로의 서명된 전체 서비스 종료 증거와 localhost S3 대역을 실제 서비스 관측·S3 내구성·독립 장애 영역으로 간주하지 않습니다. 운영 SSM hook·권한·원격 백업 복원은 별도 인수 사항입니다.

승인자·실행자는 별도 운영 identity와 권한을 사용하고 실행자가 승인 서명을 발급할 수 없어야 합니다. 실환경 발급자 allowlist·key rotation·외부 검토자 신원·서명 검증은 별도 인수 사항입니다. 예약 reviewer와 로컬 HMAC은 테스트 신뢰 경계이며 이 역할 분리를 보장하지 않습니다.

## 4. Artifact와 journal의 운영 보장

CLI는 입력 hash·inode·mtime을 결합하고 각 단계 전후에 input/code digest를 다시 검사합니다. 임의 symlink/ancestor 교체를 거부하며 macOS의 고정 /var·/tmp 시스템 별칭만 정규화합니다. 효과 전 검사와 실제 외부 요청 사이의 경쟁을 완전히 제거하지는 않습니다. 운영은 읽기 전용 versioned artifact·컨테이너 digest·동일 Python 실행기/라이브러리/설정 manifest를 배포하고 승인 scope에 결합해야 합니다. 현재 code_hash가 모든 OS binary·runtime library를 포함한다고 주장하지 않습니다.

journal은 sequence/hash chain/HMAC와 마지막 checkpoint를 대조합니다. 단일 journal 또는 checkpoint의 유실·truncation·rollback·중복/부분 쓰기를 거부합니다. **둘을 함께 같은 과거 시점으로 복원한 경우는 외부 witness 없이는 탐지할 수 없습니다.** 운영은 다른 장애 영역에 append-only/WORM 정책으로 plan/승인/영수증/checkpoint head를 보존하고 발급자·버전·수신 시간을 기록해야 합니다. 원격 수신 후 응답 유실은 관측·reconciliation 대상으로 처리합니다.

수동 reconciliation 증거는 원래 plan/step·scope·address/ID·instance/generation·provider 요청 ID·현재 존재 여부·DB stop/백업/복원 영수증·S3 version/checksum·외부 journal head·관측 시각/issuer·검토자·결정 만료를 포함해야 합니다. effect가 없는 complete DB receipt는 unknown입니다. 증거가 불충분하면 자동 성공·자동 retry를 하지 않습니다.
