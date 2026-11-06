# Runtime 실행·복구 인수 명세

## 현재 실행 경계

로컬 renderer·호스트 Ansible·Compose 설정과 실패 처리 코드는 제공하며 실제 앱 소스는 작성하지 않았습니다. 제품 ASGI·날씨 manifest/outbox·피드백 투영·추천·모델 서버 구현은 별도 인수 대상입니다. 렌더링 성공이나 단일 노드 시험은 실제 배포 승인이 아닙니다.

## 최초 구성 순서

1. foundation inventory의 계정·generation·physical AZ ID·mount UUID·instance profile·SSM Document version/hash를 인수합니다. 실제 inventory와 binding은 `.private/`에만 둡니다.
2. AMI에 SSM Agent·curl·Python·Docker Engine/Compose·SSM 필수 OS 도구·Session Manager 전송 경로를 미리 준비합니다. 버전·패키지 hash와 공식 출처가 인수되지 않은 AMI는 거부합니다. runtime host role은 인터넷 apt/snap 설치를 하지 않습니다.
3. SSM Endpoint/DNS와 각 VPC의 S3 Gateway Endpoint를 먼저 검수합니다. Controller에는 Session Manager plugin과 호환되는 AWS SDK가 필요합니다. SSM 전송 임시 bucket은 raw/lake/backup/state와 분리하고 민감 payload의 version 잔존·삭제·암호화 정책을 검토합니다. 요구 근거는 [Ansible SSM connection 공식 문서](https://docs.ansible.com/projects/ansible/latest/collections/amazon/aws/aws_ssm_connection.html)입니다.
4. Gateway A/B를 먼저 준비하고 전용 identity·영속 Tailscale 상태 경로를 각각 인수합니다. `tailscale up`·Serve 변경은 이 코드가 실행하지 않습니다. 상태 파일을 반대 Gateway로 복사하지 않습니다. Squid는 사설 CIDR ACL과 목적 port를 제한합니다. 실제 Squid 이미지 hash·Tailscale package lock은 아직 미정입니다.
5. private 호스트의 HAProxy 이미지는 AMI에 미리 preload합니다. Docker가 처음 뜰 때 registry 접속 없이 host-network HAProxy를 시작할 수 있어야 합니다. 그 뒤 host/daemon/container 프록시를 활성화합니다. Docker daemon 설정은 기존 JSON과 검토 후 병합하며, 이 Ansible은 전역 설정을 자동 덮어쓰지 않습니다. 기준은 [Docker daemon proxy 문서](https://docs.docker.com/engine/daemon/proxy/)입니다.
6. `/srv/mlo`의 사전 마운트와 UUID·소유권을 확인합니다. role은 루트 filesystem·다른 UUID를 거부하고 format·자동 mount·volume prune을 하지 않습니다. EC2 서비스별 UID/GID와 실제 secret 파일 접근 권한을 별도로 인수해야 합니다.
7. secret은 `/run/mlo/secrets`에 private binding으로 주입합니다. PKI SAN에는 실제 접속 IP/DNS가 필요합니다. keystore password·Spark/ZK client TLS 설정·Kafka ACL bootstrap을 완성하기 전에는 Compose를 기동하지 않습니다.
8. PG/Mongo → Kafka → ZooKeeper/Spark → ES → 업무 worker → Control web/active group 순으로 원격 readiness를 확인합니다. KRaft 최초 format/cluster identity·ES 최초 cluster bootstrap·Mongo 최초 사용자 생성은 기존 데이터 유무를 검사하는 별도 수동 인수 절차입니다. 일반 재실행 경로에 format·initial master 재주입을 넣지 않습니다.

## 통신·identity·인증

- proxy/no_proxy에는 모든 노드 이름/IP·VPC CIDR·SSM·S3·IMDSv2·tailnet 우회를 출력합니다. `NO_PROXY`의 CIDR 지원은 client마다 다르므로 IP/host도 포함합니다. 추가 AWS API(STS/EC2/CloudWatch) egress는 foundation 합의 전 미정입니다.
- EC2 Compose는 Linux host networking을 사용합니다. 컨테이너의 IMDSv2 token 요청·instance profile·S3A 경로와 metadata proxy 우회를 실제 EC2에서 확인합니다. 정적 AWS key를 컨테이너/모델에 전달하지 않습니다.
- `aws login` 원본 named profile은 격리 credential_process bridge의 source credential로 사용하고, Ansible SSM은 계정별 `p4-controller-encoreN` 역할 profile을 사용합니다. OIDC는 CI의 별도 short-lived role 경로이며 개발자 login/profile을 복사하지 않습니다. 공동 로컬 시험은 고정 SDK와 가짜 credential/STSes 대역을 실행하며 실제 AWS 네트워크를 호출하지 않습니다. 실제 login 세션·STS 역할 체인·SSM SDK/plugin·계정 권한은 별도 인수해야 합니다.
- PG는 TLS와 SCRAM, Mongo는 TLS와 사용자 인증, Kafka broker/controller는 mTLS/ACL, ES는 HTTP·transport TLS/인증을 요구합니다. Spark는 공유 secret/RPC TLS와 private admin UI, ZK는 secure client·quorum TLS를 렌더링합니다. 인증서·ACL·keystore 비밀값과 다중 노드 기동은 아직 실제 인수 전입니다. [Spark 보안 설정](https://spark.apache.org/docs/4.1.3/security.html)을 기준으로 TLS와 shared-secret 전달을 검수합니다.
- 사용자 HTTPS → CloudFront → HTTP ALB → Control 경로입니다. 원본 구간 TLS 완료로 표시하지 않습니다. `/ops/`·Kibana의 별도 관리자 인증과 CloudFront 개인 API의 cache/header/cookie/query/method 처리는 실제 검수 항목입니다.

## Control 실행 그룹과 timer

`operations.ActiveGroup`은 선형화 가능한 `read/cas` lease backend와 `stop_group/observe` 실행 backend를 요구합니다. 갱신 실패 시 old owner를 기록한 채 실제 종료 검증을 수행합니다. lease 만료만으로 인수하지 않으며 컨테이너·Driver·executor 종료, master/worker 전체 관측·cluster idle이 빠지면 승계를 거부합니다. `execution.DockerSparkExecution`은 실제 Docker inspect와 인증된 Spark TLS JSON 응답을 검사합니다. unreachable·스키마 누락은 성공이 아닙니다.

`lease.PostgresLease`가 기존 PostgreSQL의 행 잠금·transaction CAS를 구현하며 `supervisor.Supervisor`가 갱신·재기동·실행 그룹 시작·lease 상실 fencing을 연결합니다. 실제 로컬 PostgreSQL과 프로세스로 경합·장애·재기동을 시험하고 renderer가 Control systemd unit과 보호 `/etc/mlo/supervisor.json`을 연결합니다. old host의 실제 SSM 원격 관측/fencing hook, master/worker 인증 JSON endpoint, 실제 업무 streaming entrypoint 인수는 남아 있습니다. local file lock은 호스트 간 lease를 대신하지 않습니다. 이 실제 입력이 없으면 두 Control의 streaming을 자동 시작하지 않습니다.

`jobs.spark_submit`은 `weather-stream=19000/19001`, `feedback-postgresql=19002/19003`, `feedback-analytics-batch=19004/19005`, executor RPC는 순서대로 `19006/19007/19008`, `spark.port.maxRetries=0`을 사용합니다. Driver UI는 비활성화하여 기본 4040 추가 listener를 만들지 않습니다. 같은 query의 executor는 host당 한 개가 되도록 실제 앱의 cores/placement binding을 인수해야 하며, 동일 host 충돌을 다른 임의 port로 우회하지 않습니다. 모든 executor·Driver는 인수한 image·mount·S3 credential 경로를 공유해야 합니다. Delta checkpoint는 environment/generation/query를 분리하며 reset하지 않습니다. [Delta 공식 호환 표](https://docs.delta.io/releases/)에서 4.3.x와 Spark 4.1.x 지원을 확인했으며 로컬 checkpoint 시험도 별도로 실행했습니다.

timer는 배치 15분·원문 대조 5분·exporter 분당·DB backup 시간당을 렌더링합니다. Control timer는 실행 그룹의 검증된 enqueue entrypoint만 호출할 수 있으며 직접 Spark를 제출하는 binding은 거부합니다. 14분 제한과 host lock으로 중복 실행을 차단합니다. 실제 binding은 `/etc/mlo/jobs.json` 0600이며 executable의 SHA256·version 경로·명시적 readiness를 검사합니다. collector는 A active/B standby 수동 전환이며 이전 수집 중지 확인 binding이 없으면 거부합니다.

## 모델 중계 인수

Gateway는 양쪽에서 별도 identity로 동작하고 home의 Tailscale HTTPS/Serve로 접근합니다. `relay` module은 인증된 TLS submit/result/cancel/status 중계를 구현하며 실제 TLS 로컬 home 대역으로 accepted-but-disconnected·같은 ID 결과 조회·기한·취소와 slot 반환을 구분해 검증합니다. renderer의 relay Compose는 이 module과 보호 `/etc/mlo/relay.json`, runtime code를 연결합니다. 실제 relay 이미지 digest·배포 대상 token/CA/인증서·Tailscale identity 및 실제 집 모델 API 인수는 남아 있습니다. `operations.request_model`은 transport 재시도 시 ID·본문·원래 기한을 보존하고 429/503/접속 실패를 처리합니다. 응답의 ID·catalog revision·승인 후보를 AWS 쪽에서 검증합니다. timeout은 GPU slot 반환 증거가 아니며 취소 acknowledgement와 resource release를 분리합니다.

home은 같은 ID/같은 본문을 deduplicate하고 수락 모호 상태·기존 결과 조회·취소·늦은 응답을 인수해야 합니다. Gateway 전환을 새 ID 재추론으로 연결하지 않습니다. DB credential을 모델 payload로 전달하지 않습니다. 실제 3080 Ti/Windows Serve·10명 접수와 GPU 처리량은 미실행입니다.

## 웹 배포와 artifact

`artifact.inspect_artifact`는 공통 verifier를 통해 artifact_version/environment/generation/node/role/platform·외부 archive/manifest SHA256·내부 파일 hash를 검증하고 새 디렉터리만 만듭니다. 절대/상위 경로·link/device·중복 member를 거부합니다. `packager`가 공유 gzip TAR를 만들고 foundation dispatcher가 공통 검증 후 고정 `bin/runtime-entrypoint.py`로 `install`의 Compose/systemd backend를 호출합니다. 압축 안의 임의 배포 shell hook을 실행하지 않습니다. Linux entrypoint·Ansible 재적용·실제 Compose 실패/복구와 공동 dispatcher 대역 시험을 확인했으며 실제 SSM/systemd/EC2는 별도 인수입니다. 상세 입력·복구 계약은 [설치 보완 기록](INSTALLATION-REVISION-2026-10-30.md)에 있습니다.

`web.DockerWebBackend`는 hash로 고정된 target-control hook을 통해 한 host씩 target 제외 → inflight 0 → web만 교체 → container health·remote readiness/revision → target 복귀/healthy를 수행합니다. `operations.rollout`은 부분 교체 실패 시 이전 image와 Compose 설정을 역순 복구합니다. rollback readiness 실패는 target을 제외한 채 실패로 남깁니다. stateful 서비스는 web Compose에 포함할 수 없습니다. 실제 target hook·ALB 권한/Document 계약은 공통 요청 001 인수 전입니다.

## 백업·복구와 계획된 종료

- PG는 custom-format `pg_dump`, Mongo는 archive/gzip dump를 사용합니다. 도구의 TLS 옵션이 다릅니다(`mongosh --tls`, `mongodump/mongorestore --ssl`). 비밀값은 실제 명령 인자 대신 보호된 client config/secret 전달로 인수합니다.
- 로컬 `backup`은 새 `.partial-*` 파일에 쓰고 exit code·검증 뒤 atomic rename합니다. 기존 archive를 덮어쓰지 않습니다. DB 복원은 별도 빈 staging DB/volume에서 검증하고 실제 데이터 승격을 승인된 절차로 수행합니다. 운영 DB에 자동 drop/restore를 실행하지 않습니다.
- dump 성공만으로 원격 독립 복사/S3 검증 완료가 아닙니다. `stop_stateful`은 검증된 원격 backup receipt가 없으면 계획된 stop을 호출하지 않습니다. shutdown systemd hook은 최후의 백업 시도이며 강제 종료·전원 상실이나 OS 종료 자체를 막는 보장이 아닙니다.
- PG WAL 연속 보관·Mongo standalone 쓰기 중 단일 시점 보장을 주장하지 않습니다. raw·event·Delta·checkpoint·backup에는 자동 삭제 경로가 없습니다. 다른 host의 EBS를 직접 읽어 AZ 복구하지 않습니다.

## 남은 실제 검수

실제 앱 ASGI/lock/migration/health·worker interfaces·이미지 digest·실서비스 file UID/GID·secret/PKI·AMI·SSM SDK/plugin package hashes·실제 PG lease 자격증명과 remote 실행 hook·target hook·Kafka/S3A/JDBC connector closure가 필요합니다. ZooKeeper TLS 3노드 quorum은 로컬 단일 Docker 호스트에서 확인했습니다. 실제 3AZ ZooKeeper/Spark HA·Kafka RF3/minISR2·ES 3노드·SSM/S3/IAM·IMDS·물리 fencing·독립 원격 복원·CloudFront 관리 인증·tailnet/GPU/부하는 실제 환경에서 별도 검수합니다.
