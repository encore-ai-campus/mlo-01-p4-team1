# 앱 운영 이미지와 배포 전 로컬 검증

공통 Django와 광고 Django는 각각의 dependency lock과 이미지를 유지합니다.
공통 API는 Uvicorn ASGI, 광고 API는 [Gunicorn 26.2.0](https://pypi.org/project/gunicorn/26.2.0/) WSGI 실행점을 사용합니다.
개발 Compose는 `development` target과 개발 포트를 명시하며, `production` target은
공통 API 8000·광고 API 8001의 운영 포트 계약을 사용합니다. 실행 시 migration을 자동 수행하지 않습니다.

## 이미지와 검증 명령

저장소 루트에서 다음 명령을 순서대로 실행합니다. Docker Engine과 고정 도구·이미지가 필요합니다.

```bash
make app-images
make predeploy-check app-predeploy
```

`app-images`는 현재 공개 앱 파일 전체의 해시로 `APP_REVISION`을 만들고 ARM64·AMD64 이미지를 각각
로컬에 빌드합니다. 명령·source manifest·image ID는 Git 제외 `.artifacts/app-predeploy/`에 저장합니다.
레지스트리 게시나 AWS 연결을 수행하지 않습니다. 코드나 앱 자산 변경 후에는 이미지를 다시 빌드합니다.
로컬 image ID와 배포 레지스트리의 manifest digest는 별개입니다. 원격 배포 전에는 검증한
동일 이미지를 승인된 레지스트리에 게시하고 실제 digest와 공급 근거를 보호 binding에 인수합니다.

`app-predeploy`는 새 Docker project·DB·볼륨만 생성하고 다음을 실제 이미지에서 확인합니다.

- PostgreSQL TLS·독립 앱 계정과 MongoDB TLS·인증·단일멤버 replica set
- migration/광고 schema 준비 전 readiness 실패와 명시적 준비 후 정상 응답
- 실제 공통 인증·CSRF 응답, 잘못된 revision·충돌하는 secret 입력 거부
- DB 중단 시 readiness 실패·재기동 후 복구와 실제 서버의 SIGTERM 정상 종료
- 두 CPU 아키텍처에서 같은 소스 revision, 이미지 내 개인용 에이전트 지침 제외

이 검사는 실제 AWS·3AZ·SSM·ALB·S3, 신규 회원 기능이나 모든 업무 시나리오의 수용을 대신하지 않습니다.
현재 환경의 기존 개발 DB/볼륨을 사용하거나 초기화하지 않습니다. 해당 시험 project가 만든 볼륨만 정리합니다.

## 고정 실행 인터페이스

이미지는 `/opt/mlo/bin/serve --config /etc/mlo/service.json`과 `/opt/mlo/bin/readiness`를 제공합니다.
Runtime은 node별 `service.json`을 읽기 전용으로 연결하고 private IP에 바인딩합니다.

```json
{"kind":"web","bind":"10.40.0.10:8000","secret_env":"/run/mlo/secrets/app-env.json"}
```

광고는 `kind=advertising`, port 8001을 사용합니다. config는 임의 command·shell을 받지 않습니다.
`APP_REVISION`은 빌드에 고정하며, 이미지 kind·포트·revision 불일치는 기동을 차단합니다.

`app-env.json`은 이미지 UID 10001(공통) 또는 10002(광고)가 읽을 수 있는 owner-only 일반 파일로
제공합니다. symlink·공개 파일 권한·허용되지 않은 환경변수·환경값 충돌을 거부합니다.
일반 환경변수로 전달하는 배포 설정과 비밀값 파일을 혼용할 때 같은 키의 값이 다르면 기동하지 않습니다.
실제 값은 이미지·Compose·Git·검증 보고서에 넣지 않습니다.

- 공통: Django secret, PG host/database/user/password, `PGSSLMODE=verify-full`과 CA 경로,
  광고 내부 인증 및 수락된 SSO 설정을 매핑합니다.
- 광고: 별도 Django secret, PG 연결/TLS, Mongo URI·database·replica set, 공통 API 주소·내부 인증과
  delivery signing key를 매핑합니다. Mongo URI는 TLS CA·클라이언트 인증서 및 `replicaSet=ads-rs`를 지정합니다.
- `/readyz`는 실제 DB 접속·적용된 Django migration과 불변 revision을 확인합니다. 광고는 MongoDB의
  `ads-rs` primary·collection validator/index도 확인합니다. 실패 응답에 주소·비밀값·예외 원문을 노출하지 않습니다.

인증 session·Guest TTL·provider 및 업무 adapter는 수락한 별도 운영 설정을 함께 제공합니다.
운영 migration은 승인된 단일 실행자가 별도 단계에서 수행하고, 구/신 이미지 호환 근거 없이 자동 rollback을 허용하지 않습니다.

## MongoDB 초기화 경계

Runtime `mongod.yaml`은 `ads-rs`와 보호된 `mongodb-keyfile`을 요구합니다. 한 EC2의 단일멤버 구성은
transaction 지원을 위한 것이며 replica failover를 제공하지 않습니다. 기존 데이터의 계정·replica set·
키와 TLS SAN을 먼저 검토하고 명시적으로 초기화합니다. 앱 시작 때 `rs.initiate`·`rs.reconfig`를 자동 실행하지 않습니다.
배포 전 로컬 시험의 초기화는 새 project의 빈 disposable DB에만 적용합니다.

## 사용자 화면

web 운영 target은 고정 Node/pnpm 도구로 React를 빌드하고 결과를 같은 Django 이미지에 포함합니다. `/signup`, `/mypage`, 로그인·복구·관리자 화면의 직접 접근과 공개 정적 자산을 제공하며 Vite 개발 서버를 운영에 사용하지 않습니다. HTML은 no-store, 해시 자산은 immutable cache이며 임의 소스 파일이나 API 경로를 SPA로 대체하지 않습니다. 운영 readiness는 프런트 빌드 누락도 거부합니다.
