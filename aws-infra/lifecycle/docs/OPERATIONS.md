# Lifecycle 종료·삭제와 복구 Runbook

현재는 로컬 simulator 운영 절차입니다. 실제 AWS 실행·Terraform destroy·원격 hook은 차단돼 있습니다. 이 문서의 검수 목록은 실행 승인이 아닙니다.

## 모드와 소유 책임

| 모드 | 합성 계획 결과 | 복구·비용 범위 |
|---|---|---|
| retain-data | 81개 중 55개 logical removal, 26개 보존: data20·VPC5·backend1 | data EBS16·S3 data4·의존 VPC·state backend 보존. compute/access/connectivity는 제거 대상으로 접근 복구가 추가로 필요 |
| purge-data | 20개 data backup 후 80개 logical removal, backend1 보존 | 합성 copy/restore 증거만 생성. data 원본 JSON/SQLite를 물리 삭제하지 않으며 운영 자원 복구 가능성을 보증하지 않음 |

두 모드 모두 all_resources_deleted=false이며 보존 비용이 남을 수 있습니다. 운영 retention 기간, 비용 owner, 복구 담당자, 승인자, 야간 연락·중단 담당자는 아직 미정입니다. 불명확한 상태에서 purge를 승인하지 않습니다. raw/Delta/checkpoint/backup·구형 EBS·S3 version/delete marker·미완료 multipart를 보존 정책에서 각각 정의해야 합니다. retain-data의 데이터만 남기고 이를 읽을 네트워크/키/역할을 잃지 않도록 복구 metadata를 함께 보관해야 합니다.

## 실행 전·후 기록

1. 대상 scope·generation·5계정 identity·서울 region·state key/lineage/serial·전체 address/ID·구성/artifact digest·변경 창·담당자를 검토합니다. 기존 encore3 VPC 소유권 미확인은 blocker입니다.
2. immutable plan·유효기간·정확한 삭제/보존/보호 해제·별도 purge 승인, 공통 잠금과 fencing, 신규 작업/승계 차단·실제 idle·백업/독립 복원 증거를 확인합니다.
3. 종료 시작/단계별 시작·완료/삭제 시각/최종 관측 시각과 시간대를 영수증에 기록하고 plan·state·백업/원문·외부 journal을 보관합니다. 증거 발급자와 사람이 승인한 시각을 구분합니다.
4. SSM·DNS/Route53·Endpoint·IAM·S3/state 접근은 모든 소비 작업·백업 검증·원격 관측이 끝나기 전에 제거하지 않습니다. 실제 graph별 보존 연결을 검수해야 합니다.
5. 전체 삭제/보존 집합과 provider 현재 상태를 대조합니다. API의 성공 응답·논리 state 제거만으로 물리 삭제를 판정하지 않습니다.

## 중단·야간 장애·만료

- 중단 시 새 효과를 시작하지 않고 현재 작업을 관측합니다. 프로세스 종료나 lease 만료만으로 원격 요청이 취소됐다고 간주하지 않습니다. 야간 담당자에게 scope·마지막 journal head·미확정 step·보존 위치·다음 관측 항목을 인계합니다. 이 도구가 외부 메시지를 자동 전송하지 않습니다.
- plan 만료는 다음 단계 시작을 막습니다. 이미 완료한 효과는 유지됩니다. `verify`/`observe`는 보고 목적에서 만료 후에도 가능하지만 코드·입력 변경 검사는 유지합니다. 만료를 연장하려고 기존 plan JSON·서명을 수정하지 않습니다.
- 현재 로컬 resume은 **동일하고 아직 유효한 plan**만 허용합니다. 만료 plan을 새 계획으로 lab에 교체하거나 이전 journal을 삭제하는 경로는 제공하지 않습니다. 새 계획 연결은 아래 운영 재검토 절차와 별도 구현이 필요합니다.
- 효과 전 started만 있거나 stop receipt가 complete인데 C effect가 없으면 unknown입니다. 자동 retry/성공 처리를 하지 않습니다. 효과가 없다는 증거가 있어야 retry를 검토하고, 완료 증거가 명확해야 completed reconciliation을 검토합니다.

## 새 계획 연결·partial Terraform apply·state drift

원래 plan/승인·journal/checkpoint/외부 head·state 사본·백업 버전을 수정 없이 보존합니다. 실제 provider 현재 상태와 최신 state를 읽기 전용으로 대조하고 address/ID/ownership·이전 효과를 분류합니다. partial apply라면 생성/삭제된 실제 대상, state 기록 여부, 보호/보존 자원의 남은 의존성을 별도로 확인합니다. state rm·force-unlock·target을 복구의 기본 절차로 사용하지 않습니다.

새 계획에는 `supersedes_plan_sha256`, 원래 journal 최종 head·sequence, 최신 관측/state digest, 확정/미확정 effect 매핑, 유지할 backup version, 새 scope/expiry, 별도 검토자 승인을 결합하는 후속 계약이 필요합니다. 이전 plan의 완료를 새 plan의 완료로 복사하지 않습니다. **현재 CLI에는 cross-plan resume/rebind 구현이 없으므로 수동 JSON 수정으로 우회하지 않습니다.** 동일 lab을 새 lab로 복사해 기존 효과를 숨기지도 않습니다.

journal/checkpoint 중 하나를 잃거나 checkpoint가 뒤처져 있으면 외부 append-only 사본과 원격 효과를 대조합니다. append 뒤 checkpoint 전 crash도 자동 복구하지 않습니다. 전체 head·sequence·plan·서명·대상 관측이 합의되기 전 실행을 중지합니다. authority key 유실·외부 사본 부재는 별도 운영 사고입니다.

## 백업 복구와 삭제의 한계

삭제는 코드 rollback으로 되돌릴 수 없습니다. 보존된 데이터·backup version/checksum·암호화 키/권한·schema·설정·이미지 digest로 새 인프라를 구성하고 격리된 위치에서 복원 검증 후 별도 승격해야 합니다. RPO/RTO 수치와 허용 데이터 손실은 담당자가 확정해야 하며 현재 보장값은 없습니다.

PostgreSQL/Mongo의 엔진별 일관성·복원 기준은 [증거 계약](CONTRACTS.md)에 따릅니다. 복원된 행/문서·index·권한·업무 불변식·revision·Outbox/manifest·Delta/checkpoint 연결을 검증해야 합니다. dump 성공 또는 SQLite lab 성공만으로 운영 복구를 승인하지 않습니다.

## 결과·오류 판독

`lifecycle-result-v1.deleted`는 `removal_semantics=synthetic-logical-removal`이며 물리 AWS/실데이터 삭제가 아닙니다. retain-data/purge-data 모두 physical_resources_deleted=false, physical_data_deleted=false입니다. restore DB와 data JSON은 lab에 남습니다.

stderr의 lifecycle-error-v1은 안전한 error_code·phase·step_id·journal_location·recovery_action을 제공합니다. authorization_failure는 재승인 검토, plan_expired는 관측/재계획, state_drift/artifact_drift는 입력/효과 대조, backup_failure는 DB/사본/영수증 확인, partial_effect_unknown은 reconciliation, backend_unavailable은 접근 복구와 관측, journal_integrity는 외부 이력 대조가 필요합니다. invalid_output은 실행 전 차단됩니다. report_write_failure는 **작업이 이미 완료됐을 수 있으므로** stdout과 journal을 확인하고 execute를 맹목적으로 재실행하지 않습니다. 강제 종료 시 남은 reserved 출력 파일도 기존 결과로 재사용하지 않습니다.

## 실환경 삭제 후 검수와 backend 은퇴

- 전체 Terraform root·실제 resource ID의 삭제/보존/실패/unknown 집합을 대조합니다.
- EC2 외에 미연결 EBS/snapshot, S3 raw·Delta·checkpoint·backup·구형 version, Elastic IP, Endpoint/ENI, ALB/target group, CloudFront, Route53, route/peering·VPC, 로그/보존 저장소를 확인합니다. ownership 미확인 자원은 삭제하지 않습니다.
- Cost Explorer/청구 자료는 반영 지연을 고려해 잔여 비용과 보존 owner를 확인합니다. 삭제 완료 즉시 무과금이라고 보고하지 않습니다.
- backend/state lock·실행 기록은 마지막까지 보존합니다. 모든 소비 종료, state/전체 실행 증거·복구 metadata의 독립 보관/복원 확인 후 **별도 backend 은퇴 승인**이 필요합니다. purge 승인에 포함하지 않습니다.

실제 AWS 조회·삭제와 이 사후 검수는 이번 로컬 작업에서 not_run입니다. 처음 인프라를 생성하기 전에도 실제 identity/권한·backend bootstrap·CIDR/AZ·기존 자원 소유권·실제 plan/cost·배포 이미지/설정·승인이 별도로 필요합니다. C 로컬 완료/PR 병합을 이 배포 전 검수의 완료로 사용하지 않습니다.
