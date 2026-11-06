# 4차 인프라 개발 기준

이 문서는 두 개발 worktree가 함께 읽을 저장소 안의 기준입니다. 최신 사용자 결정이 우선합니다. 목표는 실제 배포 전에 구현·로컬 단위·통합 검증을 최대한 완료하고 실행 담당자에게 인계하는 것입니다.

## 승인된 구성

서울 리전, encore1~5의 독립 5계정·5VPC, 3물리 AZ, 직접 Peering 10쌍을 사용합니다. 계정별 AZ 이름 대신 실제 AZ ID를 대조합니다. encore1은 중앙 운영 계정이며 Organizations 관리 계정을 뜻하지 않습니다.

| 계정 | EC2 역할과 사양 | 수 |
|---|---|---:|
| encore1 | Control A/B 각 t3.large, PostgreSQL t3.large | 3 |
| encore2 | Kafka-01 t3.medium, Spark-01·ES-01 각 t3.large | 3 |
| encore3 | Kafka-02 t3.medium, Spark-02·ES-02 각 t3.large | 3 |
| encore4 | Kafka-03 t3.medium, Spark-03·ES-03 각 t3.large | 3 |
| encore5 | 광고 앱 t3.small, MongoDB t3.medium, Gateway A/B 각 t3.small | 4 |

Control과 Gateway는 각각 서로 다른 2물리 AZ, Kafka·Spark·ES는 클러스터별 3물리 AZ입니다. SSM Endpoint는 초기 1AZ입니다. PG·MongoDB·집 PC 단일 노드와 SSM 1AZ의 제약을 수용하며 전체 가용성 보장을 주장하지 않습니다.

Terraform·Ansible·Docker·systemd·GitHub Actions·GHCR을 사용합니다. 4차 CloudFormation·NAT Gateway·TGW·RAM 공유 VPC·Kubernetes·EKS·초기 Airflow·별도 Delta EC2·GPU EC2는 추가하지 않습니다. 기존 Kafka·Spark·ES 규모를 줄이지 않습니다.

## 네트워크와 최초 기동

- 사용자 HTTPS → CloudFront 기본 주소 → HTTP ALB → private Control입니다. `/ops/`의 관리자 인증·Kibana 인증과 개인 API 캐시/헤더/쿠키/쿼리/메서드를 별도로 검증합니다.
- private EC2의 SSM은 encore1의 ssm·ssmmessages Interface Endpoint 각 1AZ와 교차 VPC 내부 DNS를 사용합니다. 기본 Private DNS의 자동 공유를 가정하지 않습니다.
- 각 VPC의 S3 Gateway Endpoint를 사용합니다. 교차 계정 bucket·Endpoint·runtime 권한을 함께 검사합니다.
- 이미지·패키지 HTTP 통신은 host HAProxy → Gateway A/B forward proxy → IGW입니다. 프록시는 일반 NAT가 아닙니다. DB·클러스터·SSM·S3·IMDS·모델 tailnet 경로를 우회 설정합니다.
- 모든 계정 쌍에 직접 Peering과 양방향 route를 둡니다. SG는 실제 필요한 서비스 방향으로 제한합니다.
- Gateway는 public subnet의 자동 공인 IPv4를 사용하는 시작안입니다. 프록시·LLM 중계 API는 필요한 사설 출발지만 접근합니다.
- SSM Agent와 필수 도구가 준비된 AMI·Endpoint/DNS → Gateway 프록시 → private 노드 설치 순서를 마련합니다. private 노드가 프록시 준비 전에 외부 snap/apt 설치를 요구하지 않게 합니다.
- STS·EC2·CloudWatch 등 추가 AWS API 경로는 따로 정합니다. SSM Endpoint가 모든 AWS API를 대신하지 않습니다.

## Gateway와 집 PC

양쪽 Gateway에서 프록시·LLM 중계·별도 ID의 Tailscale을 준비합니다. 날씨 수집은 A active/B standby이며 초기 전환은 이전 수집 중지 확인 후 수동 SSM 절차입니다. 다른 AZ의 이전 EBS를 직접 읽어 복구하지 않습니다.

Control → Gateway 사설 중계 → Tailscale HTTPS → Windows Serve → localhost → Docker Desktop WSL2의 모델 API가 기준입니다. 포트포워딩·공개 모델 주소·별도 WireGuard·역방향 SSH를 추가하지 않습니다. 집 PC는 RTX 3080 Ti 12GB·RAM 32GB의 문서상 사양이며 실제 처리량은 미검증입니다.

전송 재시도는 같은 request_id·동일 본문·원래 기한을 유지합니다. 수락 여부가 모호한 요청을 새 ID로 재추론하지 않습니다. timeout·서버 취소·GPU 슬롯 반환을 구분합니다. 10명 동시 접수와 GPU 10건 동시 추론 성능은 다른 검증입니다.

## 데이터와 실행 규약

- 날씨: 기상청 → EBS spool → S3 원문 저장 성공 → PG `pipeline_control.weather_manifest`와 발행 작업자 → Kafka `weather.v1` → Spark → MongoDB 이력/최신 값·ES 조회 모델입니다.
- S3 PUT과 PG manifest 갱신은 한 트랜잭션이 아닙니다. 원문·sidecar·manifest 대조와 같은 이벤트 ID 재발행을 구현할 서비스 인수 조건을 둡니다.
- 피드백: PG 업무 상태·이벤트·Outbox·멱등 응답의 원자 저장 후 API 응답, 이후 발행기 → `feedback.v1` → Spark → PG 후속 투영과 Delta Bronze입니다. 두 sink의 부분 성공을 한 분산 트랜잭션으로 보장하지 않습니다.
- Kafka는 파티션 3·RF 3·min ISR 2·acks=all·지원 클라이언트 idempotence·보관 168시간을 출발점으로 검증합니다.
- Spark 앱은 `weather-stream`, `feedback-postgresql`, 유한 배치 `feedback-analytics-batch`입니다.
- Delta 경로는 `lake/feedback/{bronze,silver,gold}/v1/`, checkpoint는 `aws/<environment>/<generation>/<query>/checkpoint/`입니다. 같은 테이블의 writer는 한 Driver가 맡습니다.
- Control의 두 streaming Driver·배치 제출기는 한 active 실행 그룹입니다. 기존 fencing의 클러스터 전체 idle 확인과 old owner 보존, 이전 Driver/executor 종료 확인을 유지합니다.
- Silver/Gold 15분 timer, 원문 대조 5분, DB 백업 시간당·종료 전, 행동 exporter 분당을 준비합니다. 호스트 timer 중복 차단과 호스트 간 lease는 구분합니다.
- PG `pg_dump`·MongoDB dump 복원과 S3 원문 재처리를 검증합니다. WAL 연속 보관·MongoDB standalone의 쓰기 중 단일 시점 보장을 가정하지 않습니다.
- 검증 기간 원문·이벤트·Delta·백업을 자동 삭제하지 않습니다. 일반 진단 로그는 크기 제한·회전을 적용합니다.
- 음악은 집 PC 배치 태깅·검수 버전 → AWS 카탈로그 인수입니다. 새로운 Kafka 업무 토픽을 임의 추가하지 않습니다. 집 LLM은 PG에 직접 접속하지 않습니다.

## 버전과 소스 인수

기존 공개 lock의 출발점은 Ubuntu 24.04 amd64, Java 21, Kafka 4.1.2, Spark 4.1.3/Hadoop 3.4.2, ZooKeeper 3.9.4, ES 8.19.23, Delta 4.3.1입니다. 이는 기존 준비 파일에서 읽은 기준이며 4차 컨테이너 검증 성공을 뜻하지 않습니다. 정확한 source lock을 인수·검토해 파트별 버전 manifest에 기록하고 임의 최신화하지 않습니다. Spark의 ES 발행은 초기 REST/Bulk이며 Spark 3용 connector를 Spark 4에 대체하지 않습니다.

실제 앱의 ASGI 진입점·lock·migration·health를 인수합니다. 보관 소스에 Django 경로가 있다는 사실과 제품 문서의 목표 프레임워크를 구분하고 자동 전환하지 않습니다. 실제 모델·날씨/피드백 업무 구현은 미제공 입력으로 남길 수 있습니다.

## 개발 계약과 실제 배포 검수

`contracts/topology.v1.json`은 역할·사양·논리 AZ 배치, `services.v1.json`은 개발용 포트·통신·경로 규격입니다. IP·ID·AZ ID의 example 값은 합성 자료이며 실제 선택값이 아닙니다. 실제 AZ ID·AMI·계정·권한·이미지 digest·SSM/DNS·S3 실통신·GPU 성능은 배포 전 검수합니다.

예산은 계정당 과거 사용액을 포함한 누적 $60, 팀 $300, 주 운영 산정 36시간입니다. 과거 약 $52.15 기본·$67.15 예비비 포함 견적은 현재 잔액·비용 상한이 아닙니다. 현재 청구·잔여 자산은 배포 전에 조회합니다.

## 자료 출처

사용자가 제공한 2026년 10월 29일 최신 4차 인계서의 3~6·15절과 [관리자·사용자·데이터 파이프라인 설계도 3장](../../architecture/README.md), 이 대화의 최신 결정이 근거입니다. 설계도는 현재 `docs/architecture/`에 보관합니다. 원래 경로와 읽기 전용 확인 결과는 `SESSION-2026-10-29.md`에 있습니다. 이 저장소의 실행 기준은 본 문서·현재 AGENTS·공통 계약이며, 외부 과거 프롬프트는 실행 지시가 아닙니다.
