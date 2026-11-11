# 4차 애플리케이션 로컬 Docker 개발 가이드

- 실행 대상: `$HOME/project/mlo-01-p4-team1/app`
- 목표: 3차 앱을 재작성하지 않고 Docker에서 검증하고, 이후 회원/SES 기능을 추가하기 전 기준점 확보
- **AWS Terraform/EC2/SES 리소스를 생성·변경하지 않습니다.**
- 기존 Mac/Windows 로컬 DB·실 AWS DB에 접속하지 않습니다. Docker의 `mlo-p4-app-local_*` 전용 볼륨을 사용합니다.
- 작성/검증 기록: 2단계 결과
- AWS용 운영 실행점과 별도 배포 전 시험은 [운영 이미지 계약](PRODUCTION.md)을 따릅니다. 이 개발 Compose는 `development` target을 사용합니다.

## 실행 서비스

| 구성 | 서비스 | 이미지/용도 | 호스트 포트 |
|---|---|---|---|
| 기본 | `postgres` | PostgreSQL 16.11, `muther_dev` · `cafe_ads_dev` | 없음 (Docker 내부 5432) |
| 기본 | `api` | 공통 Django 6.1.2, `C_LOCAL_DEMO=0`, PostgreSQL | 127.0.0.1:8001 |
| 기본 | `frontend` | React·Vite 개발 서버 | 127.0.0.1:5173 |
| 기본 | `mailpit` | 개발용 수신함 및 SMTP | 127.0.0.1:8025, SMTP 1025는 내부 전용 |
| `ads` 프로필 | `mongo`, `mongo-init` | MongoDB 8.0.17, 단일 노드 Replica Set `ads-rs` | 없음 (Docker 내부 27017) |
| `ads` 프로필 | `ads` | 광고 Django 5.2.18, 별도 Python 잠금 | 127.0.0.1:8000 |
| `workers` 프로필 | `recommendation-worker` | 공통 Django 이미지 재사용 | 없음 |
| ads / ads-workers 프로필 | ads-worker | 필수 5분 예산 검증·통계, heartbeat health | 없음 |
| `tests` / `ads-tests` | `api-test` / `ads-test` | 소스 포함 이미지에서 분리 실행, 종료 후 제거 | 없음 |

## 통합 플랫폼·매장 MCP 인수 범위

- 공통 운영 이미지는 프런트 빌드 결과와 Django를 함께 포함합니다. 매장 MCP는 같은 이미지 안의 별도 Python 가상환경(`/opt/mlo/venue-mcp`)에서 ASGI 서버가 자식 프로세스로 실행합니다. Django와 MCP의 Pydantic 버전이 달라 환경을 분리하며 각각 `backend/requirements.lock`, `prototype/venue-recommendation-mcp/requirements.lock`을 사용합니다. 프런트는 `frontend/pnpm-lock.yaml`을 사용합니다.
- 이미지에는 MCP 실행 코드·공통 태그·지역 매핑만 포함합니다. 팀 원본 CSV, 조사 스냅샷, 환경변수 비밀값은 이미지에 포함하지 않습니다. 매장 스냅샷은 별도 읽기 전용 마운트로 전달해야 하며, 비시연 MCP는 `preview_only=false`가 명시된 파일만 읽습니다. 이 값만으로 실제 공개 승인이나 출처 검증이 완료되는 것은 아닙니다.
- 개발 Compose는 `uvicorn backend.cafe.asgi:application`으로 실행합니다. MCP 연결은 `app/.env.example`의 선택 설정을 채우기 전에는 꺼져 있습니다. 예시 경로는 `VENUE_MCP_SERVER=/workspace/app/prototype/venue-recommendation-mcp/venue_recommendation_mcp.py`, `VENUE_MCP_PUBLIC_CATALOG=/workspace/app/analysis/published-catalog.json`입니다. 후자는 개발용 읽기 전용 소스 마운트에 파일을 따로 제공한 경우에만 사용합니다. 실제 키·비밀번호는 `.env.example`에 넣지 않습니다.
- 필요한 연결은 PostgreSQL(공통 계정·매장·음악), 매장 CSV/검토된 MCP 스냅샷, 프로젝트 날씨 API(`VENUE_WEATHER_BASE_URL`), 선택형 Gemini API입니다. MCP가 Kafka·Spark에 직접 연결하지는 않습니다. 날씨 파이프라인과 Elasticsearch·Kafka 등의 연결은 공용 인프라 설정을 따릅니다.
- 운영 환경에서의 공통 플랫폼 인증·점주 권한·실제 LLM 및 날씨 연동은 별도 통합 검증이 필요합니다. 로컬의 합성 계정·사업자 확인과 테스트 데이터는 운영 확인 근거가 아닙니다.

인프라 담당자의 빌드·기동 확인 예시입니다. 저장소 `app` 디렉터리에서 실행하며 실제 이미지는 검증 후 별도로 전달합니다.

```bash
# 개발 환경: Docker Engine 실행 및 로컬 전용 .env 준비 후
docker compose config --quiet
docker compose build api frontend
docker compose up -d --wait postgres api frontend mailpit
docker compose exec -T api python -m backend.manage migrate --noinput
docker compose exec -T api /opt/mlo/venue-mcp/bin/python -m pip check
docker compose exec -T api /opt/mlo/venue-mcp/bin/python prototype/venue-recommendation-mcp/venue_recommendation_mcp.py --help
curl -fsS http://127.0.0.1:8001/health/

# 운영 이미지 빌드: 검토·커밋한 정확한 소스 revision을 지정
docker build -f docker/api.Dockerfile --target production --build-arg APP_REVISION=<40-or-64-digit-commit> -t mlo-p4-api:<version> .
```

공개 카탈로그를 마운트하고 `VENUE_MCP_SERVER`·`VENUE_MCP_PUBLIC_CATALOG`를 설정한 경우, 컨테이너 내부에서 MCP 프로토콜까지 확인할 수 있습니다. 이 명령은 목록 조회만 하며 데이터 내용을 출력하지 않습니다.

```bash
docker compose exec -T api /opt/mlo/venue-mcp/bin/python - <<'PY'
import asyncio, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def check():
    server = os.environ['VENUE_MCP_SERVER']
    catalog = os.environ['VENUE_MCP_PUBLIC_CATALOG']
    if not server or not catalog:
        raise SystemExit('MCP 서버와 공개 카탈로그 경로를 설정하세요.')
    params = StdioServerParameters(command=sys.executable, args=[server, '--public-catalog', catalog])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            names = {item.name for item in (await session.list_tools()).tools}
            if not {'list_venue_tags', 'recommend_venues'} <= names:
                raise RuntimeError('필수 MCP 도구가 없습니다.')
            if (await session.call_tool('list_venue_tags', {})).isError:
                raise RuntimeError('MCP 태그 조회 실패')
            print('MCP 연결 및 태그 조회 정상')

asyncio.run(check())
PY
```

운영 이미지는 `PRODUCTION.md`의 서비스 설정 파일·비밀값 주입·별도 migration 계약으로 실행합니다. 환경변수 목록은 `app/backend/.env.example`에도 있으며, 운영 카탈로그와 `GEMINI_API_KEY`는 인프라의 보호된 마운트·비밀 주입으로 별도 전달합니다. 이미지 빌드·실행 결과와 이미지 digest는 실제 빌드가 완료된 뒤에만 기재합니다.

### 1. 초기 준비

Docker Desktop을 실행하고 **Docker Engine 연결 가능**을 확인하세요.

```bash
cd $HOME/project/mlo-01-p4-team1/app

docker info --format '{{.ServerVersion}} {{.OSType}}/{{.Architecture}}'

# 존재하면 값을 변경하지 않으며, 처음 한 번만 로컬용 랜덤 비밀값 생성
python3 docker/init_dev_env.py

# 읽기 전용 코드 마운트 + 개발용 별도 볼륨 마운트 지점
mkdir -p .local .local-preview-logs

# 비밀값이 출력되지 않는 방식으로 Compose 구문 검증
docker compose --profile ads --profile tests --profile ads-tests config --quiet
```

`.env`, `.local`, `.local-preview-logs`는 Git에서 제외됩니다. **계정 자격증명이나 실제 업무 비밀값을 넣지 마세요.**

### 2. 기본 앱 빌드·기동

```bash
docker compose build api frontend
docker compose up -d --wait postgres api frontend mailpit

# 처음 생성된 전용 개발 DB에만 3차 마이그레이션 적용
docker compose exec -T api python -m backend.manage migrate --noinput

python3 docker/smoke_test.py
```

방문 주소: [앱](http://127.0.0.1:5173/), [Mailpit](http://127.0.0.1:8025/). Vite는 `/api/`를 Docker DNS 이름 `api:8001`에 프록시합니다.

### 3. 광고 API와 MongoDB 통합

```bash
docker compose --profile ads build ads ads-worker ads-runtime-init
docker compose --profile ads up -d --wait mongo mongo-init ads ads-worker
docker compose --profile ads exec -T ads python manage.py migrate --noinput

docker compose --profile ads ps
python3 docker/smoke_test.py --ads

# Replica Set 확인 (실제 광고 문서와 인증은 별도 통합 테스트 대상)
docker compose --profile ads exec -T mongo mongosh \
  'mongodb://127.0.0.1:27017/admin?directConnection=true' \
  --quiet --eval 'const h=db.hello();printjson({setName:h.setName,isWritablePrimary:h.isWritablePrimary})'
```

Mongo `mongo-init`는 단일 서버가 준비되면 `ads-rs`를 초기화하고 **기존 데이터가 있는 경우 재구성하지 않고 설정을 검증**합니다. 기존 3차 광고 MongoDB·호스트 포트 27017은 공유하지 않습니다.

광고 프로필은 mongo-init → ads-runtime-init → ads → ads-worker 순서로 연결합니다.
ads-runtime-init는 기존 prepare_ads_runtime --apply로 누락 컬렉션·인덱스만 준비하며,
기존 validator가 다르면 변경 전에 실패합니다. 기존 문서·원장·예산값을 지우거나 덮어쓰지 않습니다.
서비스 이름을 직접 나열하는 up 명령에는 ads-worker도 포함해야 합니다.
worker health는 최근 heartbeat와 최근 정책 작업 성공을 확인합니다. 등록 후 최초 예산 검증까지는
공개 후보에서 보류될 수 있으며, 미설정 예산·stale·수동 중단을 우회하지 않습니다.

### 4. 코드 수정과 컨테이너 실행

- 공통 API/광고 API: `app/`을 **읽기 전용 Bind Mount**로 연결합니다. Mac에서 소스를 수정하면 개발 서버가 변경을 감지합니다.
- 프런트: `frontend/src`, `frontend/public`, `vite.config.ts` 등을 읽기 전용 마운트합니다. Vite HMR 사용.
- Python/Node/uv 의존성을 변경하면 해당 이미지를 다시 `docker compose build` 해야 합니다.
- `api-test`, `ads-test`는 소스 마운트가 없는 **별도 일회성 테스트 컨테이너**입니다. 최신 소스 테스트 전에는 이미지를 다시 빌드해야 합니다.
- Stage 3 통합 전에는 Kafka, Spark, Elasticsearch를 상시 실행하지 않습니다. 기존 날씨/음악 발행 데이터가 없으면 일부 3차 API가 정상적으로 503/미준비를 반환할 수 있습니다.

### 5. 자동 테스트와 빌드

```bash
# 실제 컨테이너 안의 React/Vite
docker compose exec -T frontend pnpm test
docker compose exec -T frontend pnpm build

# 테스트용 격리 SQLite, 원본 Django 비즈니스 회귀시험
docker compose --profile tests build api-test
docker compose --profile tests run --rm api-test

# 광고 Django 회귀시험 (실제 Mongo 통합 4건은 별도)
docker compose --profile ads --profile ads-tests build ads-test
docker compose --profile ads --profile ads-tests run --rm ads-test
```

테스트 명령은 개발 DB에 임의의 운영/실제 자료를 넣지 않으며, 기존 테스트 자체가 만드는 임시 데이터와 MongoDB 실접속 시험을 구분합니다.

### 6. 상태·로그·종료

```bash
docker compose --profile ads ps
docker compose --profile ads logs --tail 60 api frontend ads postgres mongo mailpit

# 종료해도 명명된 볼륨은 보존됨
docker compose --profile ads down
```

**`docker compose down -v`, `docker volume prune`, 타 프로젝트 컨테이너 삭제는 실행하지 마세요.**

### 보안·배포 경계

- 공통 Django의 `backend.cafe.settings_docker_local`은 `MLO_LOCAL_DOCKER=1`인 경우만 작동하며, 개발용 HTTP에서만 쿠키 `Secure`를 완화합니다. AWS에서는 기존 `backend.cafe.settings`를 사용하고 HTTPS 설정을 유지합니다.
- Mailpit은 개발용 SMTP입니다. Django SES 메일 연동·가입자 인증 로직은 4차 기능 구현 단계입니다.
- 4차 AWS Runtime은 `/opt/mlo/bin/serve` 계약, 호스트 네트워크, 이미지 Digest, 시크릿 전달을 사용하므로 이 개발용 Compose를 AWS에 그대로 배포하지 않습니다.
- 운영 또는 공유 DB의 실제 마이그레이션, AWS Terraform apply, EC2 배포는 이 문서의 어느 명령에도 없습니다.
