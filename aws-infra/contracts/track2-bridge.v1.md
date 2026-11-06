# Track2 인프라 간 인수 계약

`foundation-track2/1`은 기존 inventory-v1을 바꾸지 않는 별도 출력입니다. B renderer의
`--foundation-track2` 및 C `accept_foundation`은 `infra_common.track2.foundation_binding`을 사용합니다.
정확한 inventory SHA256, 환경/generation/fixture, Control query owner와 dataset별 경로를 대조합니다.

| 용도 | 계약 |
|---|---|
| 신규 Spark query | `venues-catalog`, Driver/Block/Executor 19010/19011/19012 |
| 기존 query 보존 | weather-stream, feedback-postgresql, feedback-analytics-batch |
| 원천 | `raw/venues/<dataset_version>/` |
| Delta table | `lake/venues/<dataset_version>/v1/{bronze,silver,gold}/` |
| Driver checkpoint | `aws/<environment>/<generation>/<query>/checkpoint/` |
| 매장 수집 worker | `catalog-ingest`; query 이름과 구별 |

단일 active group/lease·maintenance·epoch와 전체 old owner 종료 검증을 우회하지 않습니다.
렌더·artifact hash가 일치해도 실제 app/worker artifact, 전 노드 process/container ID와 stop/drain 관측,
pending/unknown=0, live deletion authority, source archive version/hash와 별도 recovery reader 권한은 별도로 인수합니다.
이 증거가 없으면 실행을 활성화하지 않습니다. 합성 `synthetic-stop-v1`은 운영 증거가 아닙니다.

`track2-observation-archive/1`은 B sanitized envelope의 emission UUID별로 중복 수송을 제거하고
business_event_id·stage·run·release·source hash를 보존합니다. 서로 다른 emission은 별도 UUID를 사용합니다.
같은 UUID가 다른 stage/payload로 재사용되면 거부합니다. ACK는 COMMITTED/SEARCHABLE/PUBLISHED를 증명하지 않습니다.
C의 기존 flat 합성 archive와는 다른 형식입니다. 로그는 PG/Mongo 업무 원장을 대체하지 않습니다.

`make integration-track2`는 A의 현재 Terraform mock 평가 출력으로 B render와 C 보존/복구 경로를 검사하며,
변조·잘못된 query/owner·generation·dataset·경로·로그 식별자 재사용을 거부합니다. `predeploy-check`에 필수로 포함됩니다.
이 검사는 실제 S3/IAM/SSM·업무 앱·AMD64/3AZ·인증서/TLS handshake·원격 복원을 확인하지 않습니다.
항상 `ready_for_aws=false`를 유지하며 패스키 ceremony는 별도 진행합니다.
