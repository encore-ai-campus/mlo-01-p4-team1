# Foundation 로컬 준비

이 구현은 공통 계약 1.0.0을 소비합니다. 5계정·5VPC·16EC2·10쌍 직접 Peering을 Terraform으로 정의하고 실제 AWS를 호출하지 않는 mock plan으로 검증합니다. 실제 계정·AMI·SSM·DNS·GPU 검증은 완료하지 않았습니다.

## 로컬 실행

저장소 루트에서 실행합니다. Python 3.14.8과 공통 dependency lock을 사용하며, 도구 준비 단계만 공식 배포처에 연결합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-common.lock.txt
.venv/bin/python aws-infra/foundation/scripts/setup-local.py
make prep-check prep-test
make foundation-unit foundation-integration
bash aws-infra/foundation/scripts/render-fixture.sh --output aws-infra/foundation/.artifacts/inventory.mock.json
.venv/bin/python scripts/check_preparation.py --inventory aws-infra/foundation/.artifacts/inventory.mock.json
```

Terraform 1.11.4와 hashicorp/aws 5.94.1을 고정합니다. provider lock에는 공식 서명을 대조한 darwin_arm64·linux_amd64 h1 해시가 모두 있습니다. `toolchain.lock.json`의 공식 Terraform SHA256 및 각 root의 `.terraform.lock.hcl`을 사용합니다. 설치는 `.cache/` 안에만 수행합니다. 체크는 provider mirror만 사용하고 network download를 시도하지 않습니다. 의존성이 없으면 실패하며, mock 실패를 성공으로 대체하지 않습니다. macOS sandbox가 provider 로컬 socket을 막는 경우 같은 로컬 검사를 승인된 실행 환경에서 실행해야 합니다.

`integration.py`는 전용 임시 복사본에서 각 root를 `init -backend=false -lockfile=readonly`, `validate`, `test -json -verbose`로 검사합니다. 모든 AWS provider alias와 data source를 mock/override합니다. AWS 환경변수와 CLI 설정을 제거하고 profile/credentials를 `/dev/null`로 대체하며 HTTP(S) 경로는 폐쇄된 localhost:9로 고정합니다. test는 `command = plan`, `override_during = plan`을 사용합니다. 리소스 보호용 prevent_destroy를 없애거나 mock apply teardown 실패를 숨기지 않습니다.

Terraform verbose plan의 `output_changes.after`를 실제 exporter 입력으로 변환합니다. 그 값으로 계정 5개의 output → connectivity 입력 → 공통 inventory를 조합합니다. 예제 inventory를 복사하지 않습니다. `runtime-plan.example.json`은 generation 등 메타데이터만 조정한 공통 consumer 대역이며 B 실제 renderer와의 공동 통합 성공을 뜻하지 않습니다.

## Terraform 경계와 데이터

| state/root | 소유 리소스 | backend |
|---|---|---|
| bootstrap | state 전용 S3와 구성별 state 접근 역할 | foundation/.private/bootstrap.tfstate, 최초 전용 local state 보존 |
| encore1~5 | 자기 VPC·6 subnet/route table·SG·EC2·runtime IAM·ENI·data EBS·S3 Endpoint·identity | 중앙 state bucket의 dev/encoreN/terraform.tfstate |
| encore1 추가 | raw/lake/backup/transfer S3, ssm/ssmmessages 각 private-a 한 AZ, private zone 2개, ALB·CloudFront | encore1 state |
| connectivity | requester·acceptor·양방향 route 전체, 중앙 zone 연결 authorization와 교차 계정 association | dev/connectivity/terraform.tfstate |

S3 backend는 암호화와 `use_lockfile=true`, 별도 key와 구성별 state 역할을 사용합니다. 업무 버킷과 state 버킷은 분리하며 버전 관리·공개 차단·TLS deny·SSE-S3를 적용합니다. 업무 데이터를 자동 만료시키지 않고 bucket·data EBS·private ENI·VPC에는 prevent_destroy를 둡니다. EBS 크기는 보수적인 개발 초깃값이며 실제 잔액·데이터량 검토 후 조정합니다. EBS 용량·분리 보존도 비용이 발생하므로 실제 예산 검토 대상입니다.

EC2 root는 암호화·T3 Standard·IMDSv2/hop 2를 사용합니다. 별도 데이터 EBS를 연결하고 포맷·mount·migration은 수행하지 않습니다. private primary ENI는 별도 보호 리소스이며 Gateway primary ENI는 EC2 생성 방식으로 자동 public IPv4를 받습니다. private route에는 인터넷 default route가 없습니다. Gateway는 HTTP 80/HTTPS 443 outbound를 열어 forward proxy 및 Tailscale DERP/HTTPS fallback을 준비합니다. UDP direct Tailscale 경로는 추가하지 않았습니다.

SG는 공통 flows를 node /32 방향으로 구현합니다. ALB→Control만 SG reference를 사용합니다. host_proxy 3129는 SG ingress를 열지 않습니다. SSM Endpoint는 16개 runtime role ARN을 한정합니다. S3 Endpoint는 자기 계정 runtime role의 업무 버킷 권한과 별도 controller role의 exact instance-ID 전송 prefix를 나누고, bucket·identity 정책과 교차 계정 grant를 함께 둡니다. 교차 계정 S3는 account root Principal + 정확한 aws:PrincipalArn 조건을 사용해 상대 IAM role 생성 순서 의존을 줄입니다. Endpoint 정책은 IAM 자체 권한을 추가하지 않습니다.

SSM private DNS는 Endpoint 기본 Private DNS 자동 공유 대신 서비스 이름의 private zone 2개와 alias, 명시적 계정 간 zone association으로 구현합니다. 일반 DB·클러스터·S3·SSM·IMDS·모델 경로는 proxy를 우회해야 합니다. SSM 외 EC2·STS·CloudWatch API 경로를 SSM Endpoint가 대신한다고 가정하지 않습니다. AMI에는 SSM Agent와 AWS CLI 2.37.10·Python3가 준비되어야 합니다.

기본 구성의 CloudFront viewer는 기본 인증서 HTTPS이고 ALB origin은 HTTP입니다. 전 구간 TLS라고 주장하지 않습니다. ALB ingress는 CloudFront origin prefix list, listener default 403과 비공개 origin header 조건으로 제한합니다. 기본 및 명시적인 `/api/*` behavior는 `Cache-Control: no-store, private`를 강제하고 cache TTL은 0이며 header/cookie/query 및 7개 HTTP method를 전달합니다. CloudFront가 생성하는 `CloudFront-Forwarded-Proto`도 전달합니다. B는 제한된 origin 경계에서 이 헤더의 신뢰·덮어쓰기와 Secure cookie/CSRF/redirect 처리를 인수해야 합니다. `/ops/`·Kibana 인증, CSRF·Host·Authorization·광고 proof 전달과 실제 cache 동작은 runtime 및 실환경 검수 대상입니다. Origin header는 state에 남는 민감값으로 취급하며 HTTP origin 구간의 한계가 남습니다. 아래 선택 media 경로만 별도 캐시 정책을 사용합니다.

## Exporter와 runtime 연결

```bash
.venv/bin/python aws-infra/foundation/scripts/export_inventory.py \
  --input <encore1-output.json> --input <encore2-output.json> \
  --input <encore3-output.json> --input <encore4-output.json> \
  --input <encore5-output.json> --input <connectivity-output.json> \
  --output <.private/inventory.json>
```

각 입력은 Terraform `output -json`의 `foundation` output envelope입니다. 선택적 `runtime_artifact_boundary`와 `foundation_track2` output은 inventory에 섞지 않습니다. B는 private raw output에서 Document name/version/hash/hash_type을 별도로 인수할 수 있습니다. 여섯 component의 schema/environment/generation/example_only/region/AZ를 대조하고 노드 역할·ID·IP·Peering·storage를 공통 검사기에 전달합니다. output 순서에 독립적이며 중복·누락·다른 generation·fixture 혼합·잘못된 JSON을 거부합니다. 실제 inventory는 `.private/` 안에 원자적으로 0600 권한으로 저장합니다. 실패 시 기존 inventory를 덮어쓰지 않습니다. fixture ID는 실제 리소스 증거가 아닙니다.

## Track2 조건부 확장

[Track2 입력 예제](track2.binding.example.json)는 합성 fixture입니다. `binding.track2`가 없으면 새 venues 권한·media/archive bucket·OAC·사진 캐시·HTTPS listener를 만들지 않습니다. 공통 topology/services/inventory-v1을 변경하지 않습니다. 입력 검증과 생성은 [track2.py](scripts/track2.py), 생성된 Terraform JSON은 기존 account/edge module에 있습니다. 생성 파일을 직접 수정하지 말고 `build_config.py`로 재생성합니다.

| 역할 | 새 접근 범위 |
|---|---|
| Control A/B | `raw/venues/<dataset>/` 읽기, 명시적 ingest allowlist만 쓰기; `lake/venues/<dataset>/v1/{bronze,silver,gold}/`의 개별 Delta 테이블 I/O |
| 지정 Control Driver | `aws/<environment>/<generation>/<venues-query>/checkpoint/` I/O와 cleanup |
| Spark executor | 위 raw 읽기·lake 테이블 I/O; checkpoint/media/archive 없음 |
| Control A/B 사진 workflow | 선택 media bucket의 `media/venues/approved/`만 I/O |
| Control-b collector | 선택 archive bucket의 `observability/<environment>/<generation>/` 쓰기·multipart 처리; 읽기/삭제 없음 |
| Kafka·ES·광고·Mongo·Gateway | 새 venues/media/archive 권한 없음; 기존 backup/weather/artifact/operation 권한 보존 |

identity·중앙 bucket·각 VPC Endpoint는 같은 per-node 권한을 사용하고, 새 권한에는 각 노드의 **자기 계정 Endpoint ID**를 조건으로 넣습니다. 데이터 object의 SourceVpce Deny는 venues와 지정 checkpoint로 한정합니다. 기존 포괄 Endpoint data grant는 역할별 prefix grant로 좁혔고, 기존 Control checkpoint wildcard도 기존 세 job 이름으로 좁혔습니다. Track2를 켜기 전 첫 단계의 다섯 실제 Endpoint 출력을 별도로 검토해 입력해야 합니다. Terraform은 자기 Endpoint ID와 불일치하는 binding을 거부하며, 새로 생성되는 ID의 검증은 apply 시점까지 지연될 수 있습니다. Endpoint를 다른 VPC에서 Peering으로 빌리는 경로는 제공하지 않습니다. 한 instance profile을 공유하는 Control 프로세스 사이의 권한 분리와 active Driver fencing은 IAM만으로 보장하지 않습니다.

선택 bucket은 서로 및 기존 data/state와 다른 이름을 요구하며 SSE-S3·versioning·BucketOwnerEnforced·전체 공개 차단·TLS deny·prevent_destroy를 사용합니다. 자동 TTL/delete/VACUUM 또는 KMS key를 추가하지 않습니다. KMS를 채택하면 identity/key/bucket/endpoint의 별도 검토가 필요합니다. 긴 query registry가 정책 한도를 넘으면 Endpoint/bucket 정책 또는 runtime inline 정책 합계의 생성 검증을 실패시킵니다.

media는 S3 REST origin과 SigV4 `always` OAC를 사용하고, exact distribution `AWS:SourceArn`의 승인 사진 prefix 읽기만 허용합니다. 데이터용 SourceVpce Deny를 복사하지 않습니다. URL은 `/media/venues/approved/<venue-id>/<sha256>/<file>.(jpg|jpeg|png|webp|avif)`이며 CloudFront Function이 버전 없는 경로·경로 탈출·다른 확장자를 거부합니다. 이 prefix에 승인된 공개 사진만 넣는 업무 검증은 앱 팀이 인수합니다. `/api/ad-deliveries/*/image` 등 광고·개인·인증·검색 응답은 기본/API 0TTL을 사용합니다. browser/edge TTL은 정수 입력과 명시적인 `ttl_approved=true`를 요구합니다. fixture의 60/120초는 로컬 시험값이며, 3600/86400초나 정적자산 31536000초를 운영값으로 확정하지 않았습니다. 삭제 시 listing 제거·CDN invalidation 및 이미 내려받은 사본의 한계는 운영 인수 사항입니다.

TLS는 기본적으로 `UNRESOLVED_ORIGIN_NAME_CERTIFICATE`입니다. 기존 HTTP 구성을 새로 승인했다는 뜻이 아닙니다. `tls.enabled=true`는 제어 도메인·encore1/서울 ACM ARN·Control HTTPS:8443 readiness를 모두 요구하며, ALB 기본 `*.amazonaws.com`/CloudFront 기본 이름·누락 인증서·부분 TLS를 거부합니다. 설정 후보는 CloudFront `https-only` → ALB HTTPS:443 → private Control HTTPS:8443과 HTTPS health check/SG를 함께 바꿉니다. 결과는 `CONFIGURED_NOT_VERIFIED`이며 인증서 소유/만료/체인·DNS 매핑·실제 TLS·서버 기동은 검증하지 않았습니다. ALB target HTTPS는 암호화이며 target 인증서 검증을 보장하지 않습니다. 현재 사용자가 선택한 ALB 기본 DNS만으로 full TLS를 활성화할 수 없습니다. B의 HTTPS listener/신뢰 protocol 계약은 공통 인수 대기입니다.

## Foundation 전용 Track2 출력

[export_track2.py](scripts/export_track2.py)는 같은 여섯 Terraform output 파일에서 별도 [Foundation schema](schemas/foundation-track2.schema.json)의 sidecar를 만듭니다. CLI 형식은 `export_inventory.py`와 같은 반복 `--input` 및 `--output`입니다. 실제 출력은 `.private/` 안에만 0600 atomic replace로 기록하며 실패 시 기존 파일을 보존합니다.

sidecar는 `foundation-track2/1`, metadata, 정규화된 inventory JSON의 SHA256, dataset/query/ingest allowlist, 정확한 path, 선택 bucket ref, distribution ARN, TLS 상태와 `ready_for_aws=false`를 제공합니다. `inventory_sha256`는 `json.dumps(inventory, sort_keys=True, separators=(',', ':'))`의 UTF-8 SHA256입니다. 소비자는 같은 inventory와 결합해야 합니다. 누락/중복·component/account/Endpoint 불일치·다른 generation·fixture 혼합·unknown field를 거부합니다. 공통 inventory-v1의 다섯 storage ref는 그대로이며 **B/C가 이 sidecar를 소비하는 공통 계약은 아직 승인/구현되지 않았습니다.** Runtime renderer나 Lifecycle의 새 bucket 인수를 완료했다고 주장하지 않습니다.

## Track2 검증과 자원 영향

기존 `bash .local/agents/run-local.sh unit|integration`에 추가 suite를 필수 연결했습니다. unit은 입력/TLS/TTL 거부와 정책 평가기 오류 처리를, integration은 기존 7개 mock root 및 artifact/controller 권한과 결합한 Track2 5계정 plan을 검사합니다. 실제 평가된 identity/bucket/Endpoint의 prefix·role·account·query·generation 거부, weather 보존, OAC 다른 distribution 거부, API cache/header/cookie/query/method 변형, exporter 실패 원자성, full TLS 설정 후보·미준비 거부와 optional resource 부재를 확인합니다. 로컬 정책 평가기는 사용한 IAM subset만 모델링하며 AWS IAM simulator·실제 CloudFront HTTP/cache나 앱/패스키 수용 시험을 대신하지 않습니다. `.artifacts/runs/<id>/track2-*.jsonl`과 `track2-{inventory,sidecar}.mock.json`이 증거입니다.

확장 시 최대 두 private bucket·하나 OAC·하나 Function·사진 cache/response-header policy와 기존 역할 policy가 추가됩니다. EC2/VPC/Peering/Endpoint 수는 늘리지 않습니다. S3 현재/noncurrent version·요청·전송, CloudFront 사진 전송/요청/Function/invalidation, 관측 archive 증가량이 비용 입력입니다. 자동 삭제가 없어 보존량과 디스크 queue/ES 압력은 별도 용량·보존 결정이 필요합니다. S3 access logging·CloudTrail data events·CloudWatch metrics와 암호화된 로그 목적지는 배포 검토에서 확정할 권고이며 이 구현이 유료 관측 자원을 활성화하지는 않습니다. 실계정 잔액/가격·권한·Endpoint·TLS·앱 인수는 not_run이고 `ready_for_aws=false`입니다.

공식 근거: [OAC와 exact SourceArn](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html), [origin TLS 인증서 요구](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-https-cloudfront-to-custom-origin.html), [CloudFront 생성 header](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/adding-cloudfront-headers.html), [S3 Endpoint bucket 제한과 잠금 주의](https://docs.aws.amazon.com/AmazonS3/latest/userguide/example-bucket-policies-vpc-endpoint.html), [Endpoint 정책 한도/Principal](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-access.html), [bucket policy 한도](https://docs.aws.amazon.com/AmazonS3/latest/userguide/add-bucket-policy.html).

B는 inventory의 node name/role/account/instance_id/private_ip/az_id/runtime_role_ref와 storage ref를 소비합니다. 실제 IAM ARN/secret은 별도 private binding으로 전달하며 모델에 DB 자격증명을 전달하지 않습니다. `binding.example.json`은 fixture 전용입니다. 실제 값을 공용 파일에 쓰지 않습니다.

SSM payload는 공통 `runtime-execution.v1.md`의 gzip TAR를 참조합니다. `artifact`는 `{key: "artifacts/<version>.tar.gz", version, sha256, manifest_sha256}`입니다. `artifact`가 비어 있으면 Document·artifact 읽기 권한을 만들지 않습니다. Document에는 신뢰하는 공통 `artifact_verifier.py`와 A dispatcher를 embed합니다. exact S3 version 다운로드 후 외부 archive/manifest hash·manifest 내용·모든 파일 hash·크기·경로·중복·파일타입을 먼저 검증합니다. 실제 EC2 identity는 IMDSv2로 구하며 compiled instance-ID→node/role 맵과 protected `/etc/mlo/host-binding.json`의 instance_id/node/role/environment/generation을 대조합니다. 고정 `python3 -I bin/runtime-entrypoint.py --operation ... --environment ... --generation ... --node ... --binding /etc/mlo/host-binding.json`만 실행합니다. Operation은 install/deploy/health/rollback이며 임의 shell 입력은 없습니다. 실패 receipt는 `/var/lib/mlo/dispatch/receipt.json`에 0600으로 남기며 child 출력·자격증명을 로그에 쓰지 않습니다. Python 최적화 모드에서도 검사는 유지됩니다.

Ansible `amazon.aws.aws_ssm` 11.4.0은 `<instance-id>/...` object key를 사용하며 임의 prefix 옵션이 없습니다. 계정마다 `p4-controller-encoreN` role/profile을 사용합니다. `controller_instance_ids`가 비면 role trust만 생성하고 전송 policy는 비활성입니다. 최초 16 EC2 생성 후 실제 output을 검토하여 다섯 계정의 exact ID set을 private binding에 넣고 새 검토 run의 두 번째 plan/apply에서 controller policy를 활성화합니다. 계정별 설정은 실제 aws_instance ID 집합과 같아야 하며 교체 뒤 stale ID 입력을 거부합니다. identity·중앙 transfer bucket·대상 VPC endpoint는 동일 controller ARN과 동일 prefix에 Get/Put/Delete, prefix List, bucket location만 허용합니다. Runtime role에는 임시 전송 쓰기 권한이 없습니다.

SSM 임시 전송과 immutable artifact는 같은 transfer bucket의 별도 key 공간입니다. 이 버킷의 versioning 때문에 plugin의 정상 DeleteObject는 과거 version까지 지우지 않습니다. secret 본문을 Ansible 파일/모듈 전송에 넣지 않고 호스트의 보호 secret 참조로 인수해야 합니다. 비정상 종료 잔존 object와 noncurrent version 회수·retention은 실제 검수 때 검토된 prefix/version을 대조하여 처리하며, 여기서 bucket 전체 삭제나 업무 데이터 만료를 추가하지 않습니다.

## 실제 실행 입구와 재개

`run_foundation.py`는 로컬 검사에서 호출하지 않습니다. 실제 실행은 현재 목표 범위에 포함되지 않습니다. 이 세션에서는 가짜 CLI만 사용했습니다.

실행 wrapper는 전용 Terraform 1.11.4와 AWS CLI 2.37.10을 확인합니다. 실제 AWS CLI는 이 세션에서 설치하거나 실행하지 않았습니다. 별도 배포 승인 이후 운영자가 실제 계정별 profile encore1~5, 기존 리소스 소유권과 잔액·AMI·AZ ID·CIDR을 검수하고 `.private/binding.json`을 준비해야 합니다. foundation 생성 역할은 기본 trust만 정의하고 권한을 추정하지 않습니다. CI plan/apply/runtime 역할은 정확한 GitHub environment subject와 검토된 정책이 없으면 생성하지 않습니다. 계정 profile의 실행 권한과 state 접근 role trust가 실제 principal과 맞는지 인수해야 합니다.

명령 형태는 다음과 같으며 여기서 실행 허가를 제공하지 않습니다.

```text
.venv/bin/python aws-infra/foundation/scripts/run_foundation.py <stage> --binding aws-infra/foundation/.private/binding.json --private-dir aws-infra/foundation/.private/run-<id> --enable-real
```

stage 순서는 preflight → bootstrap-plan → bootstrap → plan → apply → connectivity-plan → connectivity-apply → export → verify입니다. 최초 account 생성 후 실제 account output이 필요하므로 connectivity는 별도 plan/apply로 검토합니다. 실행 전 각 단계에서 계정 identity를 재확인합니다. `credential_bridge.py`는 원래 `AWS_CONFIG_FILE`/`AWS_SHARED_CREDENTIALS_FILE` 경로를 명시적으로 복원해 `aws configure export-credentials --profile encoreN --format process`를 호출합니다. SDK에는 격리 config의 `credential_process`로 인수하며 토큰은 메모리/SDK pipe에만 있습니다. 원본 config나 login session은 변경하지 않습니다. source `encore1~5`와 `source_profile=encoreN`인 `p4-controller-encoreN` 10개 profile을 `.private/<run>/sdk-config`에 0600으로 생성합니다. Ansible 실행 담당자는 이 config 경로를 `AWS_CONFIG_FILE`, `/dev/null`을 `AWS_SHARED_CREDENTIALS_FILE`로 지정하고 B inventory의 controller profile과 맞춥니다. 실제 Provider/SDK/Ansible 인증은 mock credential-process 인수 검사와 구분하여 AWS에서 검수해야 합니다. plan hash·binding hash·generation과 전용 lock/receipt로 순서를 제한합니다. verify는 inventory 구조 검사이며 실제 통신 검증은 별도 검수입니다.

실패 시 completed/pending receipt와 이전 파일을 보존하고 자동 재시도를 막습니다. 변경 발생 여부를 알 수 없는 partial apply는 state·실제 리소스를 운영자가 대조한 후에만 receipt를 조정합니다. lock은 정상 실패 때 해제하지만 프로세스 강제 종료로 남은 lock은 소유 프로세스 확인 후 수동 회수합니다. 새로운 binding으로 기존 receipt를 재사용하지 않습니다. 동시 backend 잠금을 강제 해제하지 않습니다. wrapper에는 import/destroy 진입점이 없으며 기존 encore3 자산 인수는 새 생성과 별도의 검토·승인 절차가 필요합니다.

이전 커밋/이미지와 보존 state·S3 version·EBS snapshot을 사용해 복구안을 검토합니다. 되돌리는 Terraform apply가 데이터 복원을 뜻하지 않습니다. DB migration·AZ 이동·볼륨 교체·Driver fencing은 별도 runtime 절차와 backup 복원이 필요합니다.

## 검증 근거와 남은 검수

raw 로그는 `.artifacts/runs/<UTC-run-id>/`에 매 실행 새로 보존하고 `.artifacts/latest-run.json`에 최근 경로를 남깁니다. 내부 세션·상태·최종 인계는 Git 제외 `.local/agents/`에 기록합니다. 로컬 검증은 실제 IAM 평가, 3AZ 물리 배치, SSM Agent 등록·명령, DNS·Peering·S3 실통신, CloudFront 인증·cache·origin 우회, Linux EC2 부팅, 집 PC·GPU·부하 검증을 대체하지 않습니다. S3 access logging/CloudTrail data event/CloudWatch 지표와 암호화된 관측 저장소의 구성·비용은 배포 검토에서 확정합니다. 새로운 유료 서비스를 이 기본 구성에 임의 추가하지 않습니다.

공식 참고: [Terraform mocks](https://developer.hashicorp.com/terraform/language/tests/mocking), [S3 backend lockfile](https://developer.hashicorp.com/terraform/language/backend/s3), [SSM VPC Endpoint](https://docs.aws.amazon.com/systems-manager/latest/userguide/setup-create-vpc.html), [ALB origin 제한](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/restrict-access-to-load-balancer.html), [S3 보안](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html).

인증·전송 공식 근거: [CLI credential export](https://docs.aws.amazon.com/cli/latest/reference/configure/export-credentials.html), [Ansible SSM 11.4 문서](https://docs.ansible.com/projects/ansible/latest/collections/amazon/aws/aws_ssm_connection.html), [고정 plugin 소스](https://github.com/ansible-collections/amazon.aws/blob/11.4.0/plugins/connection/aws_ssm.py), [Session Manager IAM](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-quickstart.html), [교차 계정 IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies-cross-account-resource-access.html), [S3 Endpoint 정책](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-s3.html).

도구 설치기 역시 assert에 의존하지 않습니다. 고정 Terraform SHA256·서명 대조 provider lock의 정확한 zh 해시와 ZIP regular flat member·중복·파일 크기를 명시 예외로 확인합니다. 임시 파일 작성 후 atomic replace하며 기존 캐시 executable을 다운로드 중 부분 내용으로 덮지 않습니다. `make foundation-unit`의 setup 회귀는 fake archive를 `python -O`로 검사하므로 최적화 환경의 검사 우회도 확인합니다.

## 2026-11-01 인증 전 실행 경계 보완

[최신 공통 실행 계약](../contracts/preauthentication.v1.md)을 따릅니다. 기존 wrapper는 저수준 실행 포트이며 운영 진입점은 서명·소스/입력 해시를 검사하는 `scripts/infra_control.py execute`입니다. bootstrap-plan은 plan을 저장하고 bootstrap은 외부에서 승인한 같은 plan만 적용합니다. 최초 bootstrap에는 승인한 단일 controller 계약을, 이후에는 전용 operation controller 역할의 S3 CAS를 사용합니다. 16개 runtime 역할·5개 Endpoint·중앙 bucket에 exact operation 객체 권한을 정의했고, 조건 없는 덮어쓰기·삭제·state key 접근은 모의 검사에서 차단합니다. 실제 IAM·STS 경로는 아직 검수하지 않았습니다.

PassRole CI 입력은 계정별 topology에서 도출한 16개 runtime role ARN의 부분집합만 허용합니다. 접두사가 같은 미등록 역할·다른 계정에 놓인 node 역할·wildcard는 거부합니다. IAM action 대소문자와 관계없이 같은 제한을 적용하며 action의 `*`·`?` wildcard를 차단합니다. 활성화된 mock OIDC plan은 provider URL/audience·Federated principal·역할별 정확한 environment subject·단일 trust statement를 대조하며, provider/principal/action/audience/subject/statement 변형과 plan/apply subject 교환 9건을 거부합니다. 이는 로컬 입력·평가 결과 검증이며 AWS IAM의 실제 허용 판정은 실환경 인수에 남습니다.
