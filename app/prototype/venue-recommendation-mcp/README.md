# 손님용 매장 추천 MCP v0.2

기존 `/api/weather`의 방문 시각 예보와 공통 분위기·메뉴 태그를 조합하는 읽기 전용 도구입니다. Python SDK의 `stdio` 방식으로 실행합니다.

| 도구 | 역할 |
| --- | --- |
| `list_venue_tags` | 분위기·요리 계열·메뉴 유형 목록 조회 |
| `recommend_venues` | 지역·방문 시각·태그·좌석 조건으로 매장과 메뉴 ID 조회 |

예보를 확인할 수 없으면 확인 불가 사유를 반환합니다. 우천 운영 정책이 확인되지 않은 야외 필수 후보는 확인 필요로 분리합니다. 매장·메뉴 ID는 등록 자료의 값을 그대로 사용합니다.

## 실행

Python 3.12 전용 환경에서 이 폴더의 의존성을 준비합니다.

```sh
python -m pip install -r requirements.txt
python -B venue_recommendation_mcp.py --weather-base-url http://127.0.0.1:8000
```

MCP 호스트가 위 서버 명령을 실행합니다. 날씨 기본 주소는 `VENUE_WEATHER_BASE_URL`로도 지정할 수 있습니다. 기본 실행은 공개 자료 기반 시범 등록본을 사용합니다. 팀 시연 CSV의 1매장을 사용하려면 아래 인수를 추가합니다.

```text
--catalog fixtures/csv_sample_catalog.json --locations fixtures/csv_sample_locations.json
```

이 자료는 백엔드 시연 CSV의 매장 ID 1개와 태그를 붙인 메뉴 ID 3개를 사용합니다. 운영 LLM·호스트 연결과 실제 점주 등록 데이터 연결은 별도 단계입니다. [매장 CSV](../../backend/venues/sample_data/seoul_venues_demo_sample.csv) · [메뉴 CSV](../../backend/venues/sample_data/seoul_venue_menus_demo_sample.csv)

## 검증

```sh
python -B verify_tag_weather.py
python -B verify_venue_recommendation_mcp.py
```

검증은 공식 SDK가 서버를 시작하고 로컬 HTTP 예보를 호출하는 방식으로 진행합니다. 맑음·비·예보 누락과 야외 필수·선호를 확인하고 팀 시연 CSV의 원본 매장·메뉴 ID를 대조합니다.
