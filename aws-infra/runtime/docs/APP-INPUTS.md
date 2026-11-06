# 업무 앱과 Runtime 인수 입력

이 문서는 현재 소스에서 확인한 실행점과 Runtime이 요구하는 인터페이스입니다.
업무 앱의 배포·인증·운영 readiness 수용을 의미하지 않습니다. `ready_for_aws=false`를 유지합니다.

## 현재 소스

| 대상 | 확인한 실행점·입력 | 인수할 항목 |
|---|---|---|
| Control API | `app/backend/cafe/asgi.py`의 `backend.cafe.asgi:application`; `app/config/environments/recommendation/music-recommendation-api.service`는 Uvicorn 1 worker·사설 bind·8000 | Linux amd64 immutable image, 같은 revision의 API/worker, 실행 cwd·UID/GID |
| 추천 worker | `python -m backend.manage run_recommendations`; `music-recommendation@.service`는 SIGTERM·210초 stop deadline | 호스트별 worker ID·queue·동시 처리 한도·PG lease/heartbeat·provider 취소 및 중복 방지. Spark streaming 그룹과 서로 다른 업무 실행점 |
| 광고 API | `app/ad-server/backend/config/wsgi.py`의 `config.wsgi:application`; 별도 Django 5.2 계열 `uv.lock` | 고정된 운영 WSGI 서버/실행 명령. 이 소스에는 광고 ASGI 진입점이 없으므로 공통 API 진입점으로 대체하지 않음 |
| 광고 worker | `python manage.py run_ads_workers --watch`; `check_ads_runtime --database` | 동일 광고 image/lock, SIGTERM 종료·중복 실행 방지·readiness 계약 |
| Migration | 공통 `python -m backend.manage migrate`; 광고 `python manage.py migrate`, 광고 Mongo schema 준비는 광고 담당 계약 | 단일 migration 실행자·schema version·실행 전후 검증·구/신 버전 호환 증거. API/worker 모든 시작 때 자동 migrate 금지 |
| Health | Control `/health/`는 `database_verified=false`; 광고 `/api/health`는 `database_checked=false` | liveness와 별도 readiness URL, revision, DB/schema·필수 adapter 가용성. 기존 200만으로 target 복귀 금지 |
| Image | `app/docker/api.Dockerfile`은 Python 3.14·`app/backend/requirements.lock`; `app/docker/ads.Dockerfile`은 Python 3.12·광고 `uv.lock` | 두 Dockerfile의 기본 명령은 개발 `runserver`. 운영 command·base image digest·최종 image digest와 공급 근거 필요. lock을 합치거나 새 digest를 임의 생성하지 않음 |

## Runtime에 전달할 인터페이스

앱 Dockerfile의 `production` target은 업무 `web`/`advertising` 실행점
`/opt/mlo/bin/serve`, `/opt/mlo/bin/readiness`를 제공합니다. renderer는 node별
`service.json`을 private IP·운영 포트와 결합해 읽기 전용으로 연결합니다.
공통 Django ASGI와 별도 광고 Django WSGI·의존성 잠금을 유지합니다.
구체적인 파일/secret 입력과 검증 명령은 [앱 운영 이미지 계약](../../../app/docker/PRODUCTION.md)을 따릅니다.
운영 image와 보호 입력의 최종 인수에는 다음 정보가 필요합니다.

- `image`는 `registry/path@sha256:<64hex>`, `version`은 앱의 불변 revision입니다.
- 서비스별 argv·cwd·사용자 UID/GID·listen 주소/port·graceful stop timeout과 host별 리소스 한도입니다. 공통 `services.v1.json`의 운영 계약은 Control `web=8000`, 광고 `advertising_api=8001`입니다. 개발 Dockerfile은 공통 API 8001·광고 8000을 사용하므로 운영 command/bind를 명시적으로 맞춰야 합니다. 개발 port를 공통 계약에 자동 반영하지 않습니다.
- 보호 secret 파일 이름·권한·소비 방식입니다. 공통 API의 `DJANGO_SECRET_KEY`, PG 접속/`PGSSLMODE=verify-full`/CA, SSO·광고 내부 인증·provider 인증과 광고의 Mongo TLS/인증을 따로 매핑합니다. 현재 앱은 환경변수를 읽으므로 `/run/mlo/secrets/*`에서 환경으로 읽는 승인된 image entrypoint가 필요합니다. 실제 값을 artifact·Compose·보고서에 넣지 않습니다.
- `readiness_url`은 HTTP 200과 JSON `revision`을 반환하며, 필수 의존성·schema·인증 입력이 준비되지 않으면 non-200입니다. `DockerWebBackend.wait_ready`는 container health와 image/revision까지 확인합니다. PKI·DB 비밀값이나 장애 세부 내용을 공개 응답에 넣지 않습니다.
- 승인된 revision마다 `migration_compatible=true`의 근거를 제공합니다. 새 schema를 구 image도 읽을 수 있는 동안만 자동 web rollback을 허용합니다. DB downgrade·기존 데이터 삭제를 rollback에 넣지 않습니다.
- `target_hook.{path,sha256}`은 고정 argv `--node <node> --action drain|status|register`를 소비합니다. drain/register는 `accepted=true`, status는 `state=unused,inflight=0` 또는 `state=healthy`를 관측해 반환합니다. 실제 ALB/SSM 권한과 target binding은 Foundation 인수 입력입니다.

## Mongo 저장 계약 경계

광고 소스는 단일 멤버 replica set `ads-rs`에서 MongoDB transaction을 사용합니다
(`app/ad-server/config/environments/ads/README.md`). Runtime renderer는 MongoDB EC2 1개를
유지하면서 `ads-rs`와 `/run/mlo/secrets/mongodb-keyfile`을 요구합니다. TLS/auth 시험에는
새 disposable DB의 replica-set 초기화·실제 transaction·backup/restore·재기동을 포함합니다.
앱 readiness는 Mongo primary와 광고 collection validator/index도 검사합니다.
운영 keyfile·TLS SAN·기존 데이터의 최초 설정/독립 복원 순서는 보호 입력으로 별도 인수합니다.
기존 데이터에 자동 `rs.initiate`/`rs.reconfig`·강제 초기화는 수행하지 않습니다.

## 로컬 시험과 실환경 인수

Runtime suite의 web rollback revision은 명시적인 시험 앱입니다. 별도 `make app-images`와
`make app-predeploy`는 실제 두 Django 운영 이미지의 TLS DB·schema·revision readiness,
secret 입력·DB 장애/복구·종료를 새 disposable DB에서 확인합니다. 이 검사와 실제 업무 API/worker의
전체 migration 호환성·회원 기능·업무 시나리오 수용은 구분합니다.
업무 코드를 임의로 작성하거나 placeholder로 완료 표시하지 않습니다.
실제 AWS SSM/systemd/IMDS·3AZ·CloudFront/ALB 인증·S3/IAM·집 GPU/tailnet은 별도 실환경 인수입니다.
