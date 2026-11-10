# 손님용 매장 추천 MCP

서울의 역·동·주요 장소에서 분위기, 메뉴와 예산 조건에 맞는 매장을 찾습니다. 방문 시각 예보와 어울리는 분위기와 메뉴에 각각 가점을 주고, 카드에 해당 근거를 표시합니다. 시연 자료는 기존 250곳과 검증 CSV의 성수 13곳·을지로 14곳을 사용합니다.

## 로컬 시연

백엔드 Python 3.14와 MCP Python 3.12를 각각 준비합니다. 프런트엔드는 저장소의 pnpm 잠금 파일을 사용합니다. 저장소 루트에서 실행하는 PowerShell 예시입니다.

```powershell
Set-Location app/frontend
pnpm install --frozen-lockfile
pnpm build
Set-Location ..
py -3.14 -m venv .local/venue-api
py -3.12 -m venv .local/venue-mcp
& ./.local/venue-api/Scripts/python.exe -m pip install -r backend/requirements.lock
& ./.local/venue-mcp/Scripts/python.exe -m pip install -r prototype/venue-recommendation-mcp/requirements.txt
& ./.local/venue-api/Scripts/python.exe -B prototype/venue-recommendation-mcp/run_local_demo.py --mcp-python .local/venue-mcp/Scripts/python.exe
```

[시연 화면](http://127.0.0.1:18748/venues)에서 ‘성수카페거리 2만원 이하 아늑한 술집’ 또는 ‘을지로 1만원 이하 술집’을 검색합니다. 성수오뎅과 토리키조쿠에서 주문 예시와 공개 메뉴 출처를 확인할 수 있습니다.

이 실행 명령은 독립 시연용입니다. 공통 계정으로 손님 탐색을 이용하는 통합 모드는 손님 이용 유형과 로그인을 확인합니다.

### 기상청 실예보로 시연

팀에서 사용하는 기상청 API허브 키를 수집기 환경에 설정합니다. `app`에서 시연 전 캐시를 갱신합니다.

```powershell
& ./.local/venue-api/Scripts/python.exe -B -m pipeline.weather.live_cache --once --path .local/venue-mcp/weather-live.json
& ./.local/venue-api/Scripts/python.exe -B prototype/venue-recommendation-mcp/run_local_demo.py --mcp-python .local/venue-mcp/Scripts/python.exe --kma-cache .local/venue-mcp/weather-live.json
```

방문 시각에 유효한 예보가 없으면 날씨 가점을 적용하지 않습니다.

## MCP 연결

- `list_venue_tags`는 사용할 태그와 검색 지역을 조회합니다.
- `recommend_venues`는 `query` 한 개로 호출하거나 `region`, `venue_category`, `required_moods`, `orders`, `budget`을 직접 전달할 수 있습니다. `visit_at`을 생략하면 서울 현재 시각을 사용합니다.
- 예산이 필수 조건이면 `budget_mode`를 `REQUIRE`로 전달합니다. `budget_example`에 공개 메뉴로 확인한 1인 주문 금액·구성·출처가 들어갑니다. 최소 주문·추가요금·당일 가격은 별도 확인이 필요합니다.
- `ranked`, `pending`, `excluded`와 `forecast_status`는 추천·확인 필요·제외 및 예보 상태를 구분합니다. 위치가 모호하면 `location_candidates`를 돌려줍니다.

## 검증

`app`에서 실행합니다.

```powershell
& ./.local/venue-mcp/Scripts/python.exe -B prototype/venue-recommendation-mcp/verify_tag_weather.py
& ./.local/venue-mcp/Scripts/python.exe -B prototype/venue-recommendation-mcp/verify_venue_recommendation_mcp.py
```

공식 SDK로 도구를 호출해 성수·을지로 맑음·비 순위, 필수 분위기, 예산 포함·제외 경계, 예보 누락을 확인합니다. 예보 비교는 통제된 로컬 시험 자료를 사용합니다.

위치 자료와 출처는 `seoul_location_index.json`, 매장·분위기·메뉴 근거는 `fixtures/csv_sample_catalog.json`에 기록했습니다.
