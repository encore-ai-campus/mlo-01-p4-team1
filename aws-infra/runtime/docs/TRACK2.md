# Track2 Runtime 실행 경계

제공된 pipeline/앱 artifact를 검증·실행·운송하는 인프라입니다. 매장 변환, PG overlay/권한 API, Outbox 생성,
광고 예산·Mongo projection 업무 로직은 앱 팀이 담당합니다. 공통 v1 schema를 변경하지 않고
기본 상태를 `accepted=false`, `ready_for_aws=false`로 유지합니다.

## Query와 artifact

| Query | Driver | Block | Executor | 상태 |
|---|---:|---:|---:|---|
| weather-stream | 19000 | 19001 | 19006 | 기존 계약 |
| feedback-postgresql | 19002 | 19003 | 19007 | 기존 계약 |
| feedback-analytics-batch | 19004 | 19005 | 19008 | 기존 계약 |
| venues-catalog | 19010 | 19011 | 19012 | registry 일치; 실제 app/stop hook 인수 대기 |

Executor 포트를 registry 순서에서 계산하지 않습니다. 모든 Driver/block/executor의 충돌과 pool을 검사합니다.
venues는 Driver 1GiB, executor당 1GiB·1core, total 2cores로 제한합니다. 자동 retry 포트는 없습니다.
수용 binding v0.3은 entrypoint/모든 파일 SHA256, schema SHA256, 고정 writer table 세 개,
`enqueue-active-group` 모드를 요구합니다. 렌더링한 `track2-inputs.json`의 query/worker 주기는
인수 대기 제안이며 systemd에 자동 등록하지 않습니다.

`Supervisor`/`ActiveGroup`의 lease와 maintenance guard를 재사용합니다. 새 query/worker도 old owner 전체
컨테이너 목록에 포함하고 모든 Master/Worker에서 Driver/executor 종료·idle을 확인해야 합니다.
`CatalogRelease` guard는 각 effect 전 live lease/epoch·maintenance를 확인하는 고정 adapter로 주입합니다.
로컬 lock이나 lease 만료만으로 승계하지 않습니다. inflight 요청의 종료는 기존 전체 process fencing에 의존합니다.

## Catalog release

원천은 immutable `raw/venues/<dataset-version>/`입니다. 파일 manifest·checksum·schema·완전성이 고정되어야 하며
동일 key 덮어쓰기를 change feed로 쓰지 않습니다. 업무 PG+Outbox→Kafka 분석과 별개입니다.
checkpoint는 `aws/<environment>/<generation>/venues-catalog/checkpoint/`, writer table은
`lake/venues/<dataset-version>/v1/{bronze,silver,gold}/`입니다. accepted artifact의 `dataset_version`과 세 writer table prefix는 서로 일치해야 하며, Foundation의 `lake/venues/<dataset-version>/v1/` 아래에 있어야 합니다. 실제 S3 bucket·IAM·Endpoint 인수 전 연결하지 않습니다.

`accept_export`는 accepted artifact의 complete snapshot 파일을 검사합니다. 파일/전체 크기를 제한하고
동일 ID의 최고 revision을 선택하며 같은 revision의 상충 payload를 거부합니다. tombstone은 빈 payload로
유지합니다. 전체 count/hash/schema가 맞아야 release 실행에 전달됩니다. producer의 domain schema 검증·
전체 source reconciliation을 대신하지 않습니다.

`CatalogRelease`는 `catalog-search/1`의 flat 필드 mapping과 manifest의 schema SHA256 일치를 요구합니다.
keyword/text/long/double/geo_point/boolean/date만 수용하고 dynamic=strict로 색인합니다. 앱 팀이 mapping을 제공합니다.
REST Bulk의 각 item 확인 → refresh → count → 모든 문서 mget 비교 → field caps 및 실제 대표 문자열/범위/위치
질의까지 성공해야 SEARCHABLE입니다. `_source`만 읽히는 상태를 검색 성공으로 취급하지 않습니다.
부분 실패는 private index만 남기고 같은 ID·revision으로 재시도하며, 검증 index는 write-block으로 봉인합니다.

발행과 rollback 모두 보호된 앱 adapter의 live `catalog-revision-authority/1` callback이 필수입니다.
generation·epoch·단조 증가 revision·삭제 revision 집합·최대 60초 freshness를 확인합니다. 로컬 high-water 파일은
감지 보조 자료이며 원장 조회를 대체하지 않습니다. 최신 삭제를 적용한 private variant index를 재생성·검색 검증한 뒤
atomic alias를 이동하므로 과거 index의 삭제 전 문서를 그대로 되살리지 않습니다. 원래 봉인 index도 보존합니다.
authority 또는 lease가 없거나 퇴행·변경하면 거부합니다. 응답 유실은 실제 alias와 재대조합니다.
PG 원장 변경과 ES alias는 분산 트랜잭션이 아니므로 앱의 현재 PG overlay/권한 검사는 계속 필요합니다.
SENT/ACKNOWLEDGED/COMMITTED/SEARCHABLE/PUBLISHED는 별도 상태입니다.
ES/checkpoint를 현재 권한·현재 선곡의 원장으로 쓰지 않습니다.

## 관측과 배포 입력

Filebeat/Logstash/Kibana는 ES와 같은 8.19.23·registry에서 확인한 digest를 사용합니다.
sanitized NDJSON spool→Filebeat diskqueue/registry→mTLS Beats5044→Control-b Logstash PQ→
`logs-mlo.{pipeline,ads,security,service}-*` index로 운송합니다. 업무 Kafka에 의존하지 않습니다.
일반 요청/환경/DB/Docker socket은 agent에 mount하지 않습니다. source exporter는 `sanitize`와 bounded spool을
사용하고 Filebeat와 sink에서도 envelope를 제한합니다. 허용 필드에 개인정보를 넣으면 source 계약 위반입니다.
Logstash PQ는 filter보다 앞에 있으므로 인수한 mTLS agent의 source-sanitized envelope만 연결해야 합니다.

| 항목 | 제한·영속경로 |
|---|---|
| source spool | 64MiB / 16files; 상한 도달 시 cursor 유지·backpressure |
| Filebeat | 256MiB / 0.25CPU, diskqueue128MB; observations/filebeat에 registry 포함 |
| Logstash | 1536MiB / 1CPU, heap512MiB/direct256MiB, PQ128MB / DLQ32MB |
| Kibana | 1536MiB / 1CPU, Node heap768MiB; observations/kibana 영속 |
| pressure/no-progress | queue80% 경고, 진행 없이 backlog120초면 STALE |

이미지 상한은 host 전체 부하 보장이 아닙니다. OS·Spark·앱 예산과 디스크 여유는 실환경에서 측정합니다.
ES 로그 보존은 미확정이며 TTL/ILM delete/VACUUM을 켜지 않습니다. ES disk watermark와 queue/spool 상한에서
압력 상태를 보고하고 보존 정책 인수 전 무한 적재를 허용하지 않습니다. Control-b collector 단일 장애점은 남습니다.

설치 binding은 `observations_enabled=true`와 persistent directory별 명시 UID/GID를 요구합니다.
`observations/spool`, `observations/filebeat`, Control-b의 `observations/logstash`, `observations/kibana`를
이미지의 검증된 UID/GID로 만들며 기존 소유권을 재귀 chown하지 않습니다.
Logstash/Kibana는 secret 파일을 개별 read-only bind합니다. component UID가 읽을 수 있는 보호된 0600
파일/keystore 소유권을 인수해야 합니다. CA/hostname/rotation/client cert/PKCS8 key/operator health credential은
별도 입력입니다. Logstash keystore key는 `MLO_LOG_WRITER`, `MLO_LOG_PASSWORD`, Kibana keystore key는 정식 setting
`elasticsearch.password`, `xpack.security.encryptionKey`, `xpack.encryptedSavedObjects.encryptionKey`,
`xpack.reporting.encryptionKey`입니다. renderer가 secret 값을 만들지 않습니다.

아카이브/추출 파일의 0600은 유지합니다. installer는 복사한 Logstash/Kibana 설정에만 승인된
`persistent_owners[observations/{logstash,kibana}]` UID/GID와 0600을 적용하고 Compose user도 같은 비루트 UID로 고정합니다.
secret/keystore는 기존 파일의 UID와 정확한 0600을 검사하며 자동 chown·권한 완화하지 않습니다.
잘못 준비된 secret은 서비스 기동 전에 거부합니다.

Kibana5601은 private HTTPS/별도 로그인입니다. [관측 artifact](../observability/)의 ES writer는 로그 index의
index/create_index·monitor만, reader/operator는 로그 조회와 observation space read만 사용합니다.
venue/원장 index 권한은 없습니다. source backlog/registry/diskqueue/PQ/DLQ 잔량을 종료 담당에 인계합니다.

`archive_spool`은 다시 sanitize한 checksum gzip을 제한된 로컬 공간에 만들고 source를 보존합니다.
S3 archive bucket/prefix/암호화·권한/보존/삭제·복원 정책 인수 전 업로드나 source 삭제를 실행하지 않습니다.
transport ACK를 archive 검증이나 검색 가능 증거로 취급하지 않습니다.

`ProjectionRunner`는 앱 팀의 bounded read-only exporter에 cursor→durable spool→cursor commit을 제공합니다.
부분 실패 시 cursor와 stable event ID를 유지하며 heartbeat/retry/STALE을 기록합니다. `ads-observation` job은
checksum 수용 entrypoint와 읽기 전용·원장 보존 계약을 요구합니다. Mongo log_record/SENT/예산원장이나
삭제된 relay는 변경하지 않습니다. 실제 앱 projection artifact와 worker 주기 binding은 인수 대기입니다.

관측 `event.id`는 emission마다 다른 UUID이고 재전송에서는 동일하게 유지합니다. 같은 업무의 단계 연결은
`pipeline.business_event_id`를 사용합니다. 공통 `track2-observation-archive/1` adapter는 transport 중복을 제거하지만
서로 다른 단계에 같은 emission ID가 재사용되면 거부합니다. C의 합성 flat fixture와 운영 archive 형식은 구별합니다.

## Foundation sidecar 인수

render의 `--foundation-track2 <foundation-track2/1 JSON>`은 정확한 inventory hash·환경·generation·fixture 구분과
Control query owner, dataset별 lake 세 계층 및 checkpoint 경로를 검증합니다. 결과는 각 host의
`foundation-track2.json`과 `track2-bridge.json`으로 artifact hash에 포함됩니다. 공통 v1 inventory를 변경하지 않습니다.
`venues-catalog` job은 승인 entrypoint/hash·artifact v0.3·같은 dataset의 Foundation binding과
`enqueue-active-group`을 요구합니다. 주기와 실제 app/whole-group stop hook은 아직 자동 활성화하지 않습니다.
현재 `run_job("venues-catalog", ...)`는 입력의 ready flag와 관계없이 실행을 거부합니다. 입력 검증 성공을
기존 supervisor의 신규 query stop/drain 구현 완료로 취급하지 않습니다. 실제 운영 adapter 인수 후 별도 버전에서 연결합니다.
`make integration-track2`는 A의 평가된 mock 출력을 B renderer와 C 복구 검사로 전달합니다.

## Viewer/TLS readiness

ALB 기본 DNS만으로 full origin TLS가 준비되었다고 표시하지 않습니다. `validate_edge`는 controlled origin
hostname·certificate verification 입력·Control TLS·fixed viewer/RP ID/CSRF·Secure cookie·제한된 proxy CIDR·
edge가 덮어쓰는 CloudFront-Forwarded-Proto를 요구합니다. ALB X-Forwarded-Proto만으로 viewer protocol을 추측하지
않습니다. `track2_execution_enabled=true` 설치는 이 gate를 통과해야 합니다. 기본은 미준비입니다.
설정 검증은 실제 인증서 발급/TLS handshake/SG 인수를 대신하지 않습니다. 패스키 ceremony는 보류합니다.

## 로컬 수용

Runtime wrapper의 기존 suite에 catalog/관측 suite를 추가합니다. Catalog는 합성 Spark artifact에서 Delta commit 후
실패→checkpoint 재시작→지연/중복/tombstone을 실행하고 실제 ES Bulk 부분 실패→재시도→완전성→lease 거부→
alias/rollback을 확인합니다. 관측은 실제 고정 Filebeat/Logstash/ES/Kibana·일회성 CA/credential로 mTLS/중복/마스킹,
collector·ES 중단/agent·collector 재생성/PQ·registry 재개/dashboard import/RBAC를 확인합니다.
전용 project만 정리하며 무거운 suite는 순차 실행합니다.

Mac ARM64 단일 Docker 호스트 시험입니다. 실제 앱·Mongo projection/S3 checkpoint/Endpoint/IAM/AMD64/3AZ/systemd/SSM/
실인증서·실TLS·최종 통합 predeploy는 별도 인수이며 `ready_for_aws=false`입니다.

설정 근거: [Filebeat diskqueue](https://www.elastic.co/guide/en/beats/filebeat/8.19/configuring-internal-queue.html),
[Beats/mTLS](https://www.elastic.co/guide/en/logstash/8.19/plugins-inputs-beats.html),
[Kibana 설정](https://www.elastic.co/guide/en/kibana/8.19/settings.html),
[ES atomic alias API](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/indices-aliases.html).
