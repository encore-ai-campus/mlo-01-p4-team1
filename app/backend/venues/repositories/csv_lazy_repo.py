import csv
import hashlib
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.common.data_paths import discover_analysis_dirs

logger = logging.getLogger(__name__)

# 프로젝트 루트, app/, 환경변수(VENUE_DATA_DIR), Google Drive 마운트 경로 모두 자동 지원
APP_ROOT_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT_DIR = APP_ROOT_DIR.parent
PACKAGE_SAMPLE_DIR = Path(__file__).resolve().parents[1] / 'sample_data'

_ANALYSIS_CANDIDATES = discover_analysis_dirs(app_root=APP_ROOT_DIR, repo_root=REPO_ROOT_DIR)

# 서울 25개 구 전수 CSV(50개 파일)가 들어있는 by_gu 폴더 탐색
BY_GU_DIR = next(
    (
        d / 'seoul_venues_20261106' / 'by_gu'
        for d in _ANALYSIS_CANDIDATES
        if (d / 'seoul_venues_20261106' / 'by_gu').exists()
        and len(list((d / 'seoul_venues_20261106' / 'by_gu').glob('*_venues.csv'))) >= 25
    ),
    next(
        (d / 'seoul_venues_20261106' / 'by_gu' for d in _ANALYSIS_CANDIDATES if (d / 'seoul_venues_20261106' / 'by_gu').exists()),
        APP_ROOT_DIR / 'analysis' / 'seoul_venues_20261106' / 'by_gu',
    ),
)
ANALYSIS_DIR = BY_GU_DIR.parent.parent if BY_GU_DIR.exists() else (APP_ROOT_DIR / 'analysis')


def _discover_region_csv_dirs() -> List[Path]:
    """서울 25개 구(by_gu) + 경기도 30개 시·군(by_city) + 인천광역시 10개 구·군(by_gu) CSV 디렉토리 전체 자동 탐색"""
    dirs: List[Path] = []
    seen = set()
    sub_paths = [
        ('seoul_venues_20261106', 'by_gu'),
        ('gyeonggi_4cities_20261109', 'by_city'),
        ('gyeonggi_11cities_20261109', 'by_city'),
        ('gyeonggi_remaining16_20261109', 'by_city'),
        ('incheon_venues_20261110', 'by_gu'),
    ]
    for base in _ANALYSIS_CANDIDATES:
        for folder, sub in sub_paths:
            p = base / folder / sub
            if p.exists() and str(p.resolve()) not in seen:
                seen.add(str(p.resolve()))
                dirs.append(p)
    return dirs


REGION_CSV_DIRS = _discover_region_csv_dirs()

# 폴백 및 실시간 수집(Live Naver Place) 메뉴 CSV 후보 경로
FALLBACK_VENUES_CANDIDATES = [
    PACKAGE_SAMPLE_DIR / 'seoul_venues_demo_sample.csv',
    *(d / 'seoul_venues_20261106' / 'seoul_venues_demo_sample.csv' for d in _ANALYSIS_CANDIDATES),
    *(d / 'seoul_venues_demo_sample.csv' for d in _ANALYSIS_CANDIDATES),
]
FALLBACK_MENUS_CANDIDATES = [
    PACKAGE_SAMPLE_DIR / 'seoul_venue_menus_demo_sample.csv',
    *(d / 'seoul_venues_20261106' / 'seoul_venue_menus_demo_sample.csv' for d in _ANALYSIS_CANDIDATES),
    *(d / 'seoul_venue_menus_demo_sample.csv' for d in _ANALYSIS_CANDIDATES),
]

# 수도권(서울 25개 구 + 경기 30개 시·군 + 인천 10개 구·군 = 총 65개 시·군·구) 중심 좌표 사전
METRO_CENTER_COORDS: Dict[str, tuple[float, float]] = {
    # 서울특별시 25개 자치구
    '강남구': (37.5172, 127.0286), '서초구': (37.4837, 127.0324), '송파구': (37.5145, 127.1059),
    '강동구': (37.5301, 127.1238), '마포구': (37.5563, 126.9226), '은평구': (37.6176, 126.9227),
    '서대문구': (37.5791, 126.9368), '용산구': (37.5326, 126.9905), '종로구': (37.5730, 126.9794),
    '중구': (37.5641, 126.9979), '성동구': (37.5446, 127.0439), '광진구': (37.5385, 127.0823),
    '영등포구': (37.5264, 126.8962), '관악구': (37.4784, 126.9516), '동작구': (37.5124, 126.9393),
    '강서구': (37.5510, 126.8495), '양천구': (37.5170, 126.8665), '구로구': (37.4954, 126.8874),
    '금천구': (37.4569, 126.8955), '성북구': (37.5894, 127.0167), '동대문구': (37.5744, 127.0398),
    '중랑구': (37.6065, 127.0927), '노원구': (37.6542, 127.0568), '도봉구': (37.6688, 127.0471),
    '강북구': (37.6396, 127.0257),
    # 경기도 30개 시·군
    '수원시': (37.2738, 127.0193), '성남시': (37.4105, 127.1281), '안양시': (37.3954, 126.9429),
    '광명시': (37.4607, 126.8702), '고양시': (37.6591, 126.8064), '부천시': (37.4969, 126.7803),
    '용인시': (37.2731, 127.1505), '남양주시': (37.6525, 127.2068), '시흥시': (37.3819, 126.7633),
    '의정부시': (37.7405, 127.0584), '광주시': (37.3956, 127.2571), '군포시': (37.3563, 126.9378),
    '하남시': (37.5476, 127.1950), '구리시': (37.6027, 127.1364), '의왕시': (37.3609, 126.9753),
    '파주시': (37.7641, 126.7618), '김포시': (37.6447, 126.6605), '화성시': (37.1878, 126.9829),
    '평택시': (37.0190, 127.0435), '오산시': (37.1558, 127.0639), '이천시': (37.2578, 127.4597),
    '안성시': (37.0232, 127.2620), '양주시': (37.7046, 127.1577), '포천시': (37.8957, 127.2056),
    '동두천시': (37.9018, 127.0569), '과천시': (37.4272, 126.9917), '여주시': (37.2847, 127.6162),
    '양평군': (37.5089, 127.5015), '가평군': (37.7729, 127.4523), '연천군': (38.0459, 127.0595),
    # 인천광역시 10개 구·군
    '남동구': (37.4473, 126.7314), '부평구': (37.5070, 126.7218), '미추홀구': (37.4636, 126.6505),
    '연수구': (37.4102, 126.6783), '계양구': (37.5374, 126.7377), '강화군': (37.7465, 126.4881),
    '동구': (37.4739, 126.6432), '옹진군': (37.4463, 126.6368), '인천 서구': (37.5454, 126.6759),
    '인천 중구': (37.4737, 126.6217),
}

# 주점/카페 매장별 특성(상호명/세부카테고리) 기반 다양화 실존 메뉴 풀 (동일 기본 템플릿 16종을 매장 특성에 맞춰 분화)
PUB_THEME_MENU_POOLS: List[List[tuple]] = [
    # 0. 숙성회·이자카야·사시미 (1인 예산 약 32,000~38,000원 -> 3만~5만원 미만)
    [
        ('메인안주', '제철 모듬 숙성 사시미 (2인)', 38000, 'Y', '2인 사시미', '제철 대방어, 광어, 연어, 참돔을 다시마에 숙성해 감칠맛을 극대화한 대표 사시미', '흐리거나 비 오는 날 사케·소주와 최고의 페어링'),
        ('메인안주', '직화 한우 타다끼 & 유자 폰즈', 27000, 'Y', '2인 구이 요리', '겉면만 센 불에 타다끼로 구워낸 한우 채끝에 양파 슬라이스와 수제 유자 폰즈 소스', '산토리 하이볼이나 생맥주 첫 잔과 환상 조합'),
        ('메인안주', '숯불 수제 야키토리 모듬 (7종)', 24000, 'Y', '2인 꼬치구이', '네기마, 염통, 츠쿠네, 닭껍질 등 비장탄에 구워낸 정통 수제 야키토리', '시원한 생맥주와 함께 즐기는 정통 이자카야 안주'),
        ('탕·국물안주', '진한 후쿠오카식 한우 모츠나베', 28000, 'Y', '2~3인 전골', '고소한 한우 대창과 부추, 양배추를 진한 돈코츠 미소 육수에 끓여낸 전골', '쌀쌀하거나 비 오는 날 소주를 부르는 국물 요리'),
        ('탕·국물안주', '백합 조개 버터 술찜 & 파스타면', 19000, 'N', '2인 국물 요리', '신선한 백합과 바지락을 청주와 이즈니 버터로 자작하게 끓여낸 시원한 술찜', '화이트와인, 하이볼, 청하와 깔끔한 조화'),
        ('튀김안주', '바삭 새우 & 단호박 덴뿌라', 21000, 'N', '2인 튀김', '주문 즉시 바삭하게 튀겨낸 블랙타이거 새우와 계절 야채 튀김', '생맥주와 실패 없는 바삭한 페어링'),
        ('사이드안주', '명란 마요 구이 & 오이 슬라이스', 14000, 'N', '1~2인 사이드', '짭조름한 저염 명란젓을 직화로 구워 고소한 마요네즈와 오이에 곁들이는 별미', '2차 술자리에서 가볍게 집어먹기 좋은 안주'),
        ('주류·하이볼', '산토리 가쿠빈 레몬 하이볼', 8000, 'Y', '주류 1잔', '산토리 위스키에 강탄산수와 생레몬을 넣은 청량한 하이볼', '사시미 및 구이 요리와 깔끔한 조화'),
        ('주류·맥주', '기린 이치방 프로즌 생맥주', 8000, 'Y', '주류 1잔', '부드러운 얼음 거품이 올라간 프리미엄 일본 생맥주', '야키토리 꼬치구이와 찰떡궁합'),
    ],
    # 1. 와인바·위스키·칵테일 라운지 (1인 예산 약 42,000~56,000원 -> 3만~5만원 / 5만원 이상)
    [
        ('메인안주', '시그니처 샤퀴테리 & 치즈 플래터', 34000, 'Y', '2~3인 플래터', '하몽 이베리코, 살라미, 브리치즈, 고다치즈, 올리브와 무화과 콩포트 구성', '와인이나 싱글몰트 위스키와 가장 완벽한 마리아주'),
        ('메인안주', '수비드 부채살 스테이크 & 트러플 감자퓨레', 39000, 'Y', '2인 메인 디쉬', '저온 수비드로 육즙을 가둔 프라임 부채살 스테이크와 트러플 향 매쉬드 포테이토', '레드 와인 및 온더락 위스키와 격조 높은 페어링'),
        ('메인안주', '통새우 감바스 알 아히요 & 바게트', 26000, 'Y', '2인 요리', '엑스트라 버진 올리브오일에 마늘과 통새우, 페페론치노를 끓여낸 스페인 타파스', '하이볼이나 스파클링 와인과 최고의 인기 메뉴'),
        ('구이·볶음', '트러플 풍기 크림 파스타', 23000, 'Y', '1~2인 파스타', '향긋한 양송이·만가닥 버섯과 트러플 오일이 어우러진 꾸덕한 생면 크림 파스타', '식사를 겸한 반주 자리에 추천'),
        ('구이·볶음', '프로슈토 루꼴라 브루스케타', 19000, 'N', '2인 핑거푸드', '바삭한 사워도우 위에 리코타 치즈, 루꼴라, 프로슈토와 발사믹 글레이즈', '화이트 와인·칵테일과 산뜻한 조화'),
        ('사이드안주', '브리치즈 구이 & 벌집 꿀·견과류', 18000, 'N', '2인 디저트안주', '오븐에 따뜻하게 구운 통 브리치즈 위에 천연 벌집 꿀과 호두·피칸 토핑', '달콤 짭조름한 와인 도둑'),
        ('주류·하이볼', '발베니 12년 더블우드 (Glass)', 16000, 'Y', '위스키 1잔', '은은한 꿀 향과 바닐라 오크 피니시가 매력적인 정통 싱글몰트 위스키', '비 오는 밤 조용히 음미하기 좋은 한 잔'),
        ('주류·하이볼', '시그니처 얼그레이 자몽 칵테일', 14000, 'Y', '칵테일 1잔', '직접 인퓨징한 얼그레이 진에 생자몽 착즙을 더한 하우스 시그니처 칵테일', '분위기 있는 데이트 코스에 추천'),
        ('주류·맥주', '하우스 내추럴 와인 (Glass)', 13000, 'N', '와인 1잔', '소믈리에가 엄선한 산뜻하고 프루티한 글라스 와인', '가볍게 기분 내기 좋은 와인 한 잔'),
    ],
    # 2. 한식 포차·전골·숯불구이 주점 (1인 예산 약 23,000~28,000원 -> 2만~3만원 미만)
    [
        ('메인안주', '얼큰 소곱창 전골 & 우동사리', 31000, 'Y', '2~3인 전골', '고소한 한우 소곱창과 대창, 우삼겹, 깻잎을 얼큰하고 진한 육수에 끓여낸 대표 전골', '비 오거나 쌀쌀한 날 소주 2병을 부르는 부동의 1위 메뉴'),
        ('메인안주', '직화 무뼈 국물닭발 & 날치알 주먹밥', 23000, 'Y', '2인 볶음 요리', '은은한 숯불 향과 화끈한 매운맛의 국물닭발에 고소한 날치알 주먹밥 세트', '스트레스 풀리는 퇴근길 소주·맥주 안주'),
        ('메인안주', '한돈 제육 두부김치 한상', 22000, 'Y', '2~3인 안주', '매콤달콤하게 볶아낸 묵은지 앞다리살 제육볶음과 따뜻한 손두부', '막걸리와 소주 어디에나 어울리는 정통 포차 안주'),
        ('탕·국물안주', '통오징어 해물 짬뽕탕', 23000, 'Y', '2~3인 탕 요리', '통오징어 한 마리가 통째로 올라간 불맛 가득 얼큰 해물 짬뽕탕', '칼칼한 국물이 당기는 날 최고의 선택'),
        ('구이·볶음', '철판 차돌박이 숙주볶음', 20000, 'N', '2인 볶음 요리', '고소한 차돌박이와 아삭한 숙주나물을 센 불에 빠르게 볶아낸 요리', '생맥주나 소맥과 실패 없는 조합'),
        ('튀김안주', '대왕 계란말이 (명란치즈)', 15000, 'N', '2인 사이드', '두툼하게 말아낸 계란말이 속에 모짜렐라 치즈와 짭조름한 명란이 가득', '매운 안주와 함께 곁들이기 좋은 필수 사이드'),
        ('사이드안주', '추억의 도시락 & 멸치국수', 9000, 'N', '1~2인 식사', '분홍 소시지와 볶음김치, 계란후라이를 흔들어 먹는 추억의 도시락', '출출한 저녁 든든하게 배를 채우는 별미'),
        ('주류·소주', '참이슬 후레쉬 / 진로 / 새로', 5000, 'Y', '주류 1병', '전골 및 포차 요리와 찰떡궁합인 시원한 소주', '얼큰한 국물 요리의 단짝'),
        ('주류·맥주', '테라 / 켈리 생맥주 500cc', 5000, 'N', '주류 1잔', '청량하고 목넘김이 시원한 생맥주', '소맥 첫 잔으로 추천'),
    ],
    # 3. 전통주·수제 모듬전·막걸리 주점 (1인 예산 약 22,000~26,000원 -> 2만~3만원 미만)
    [
        ('메인안주', '맷돌 수제 모듬전 한상 (육전·굴전·호박전)', 25000, 'Y', '2~3인 전 요리', '주문 즉시 부쳐내는 한우 육전, 통영 생굴전, 동태전, 꼬치전, 애호박전 모듬', '비 오는 날 창가에서 막걸리와 함께 즐기는 부동의 1위 메뉴'),
        ('메인안주', '바삭 해물 파전 & 오징어 토핑', 21000, 'Y', '2~3인 전 요리', '향긋한 쪽파와 오징어, 새우, 바지락을 듬뿍 넣어 바삭하게 부쳐낸 파전', '시원한 생막걸리와 환상의 짝꿍'),
        ('메인안주', ' 벌교 꼬막 비빔밥 & 한우 육회', 27000, 'Y', '2인 특선 요리', '탱글한 제철 꼬막무침과 고소한 한우 육회를 한 접시에 담아낸 대표 별미', '전통주나 증류식 소주와 최고의 궁합'),
        ('구이·볶음', '치즈 폭포 감자채전 & 꿀소스', 18000, 'Y', '2인 전 요리', '가늘게 채 썬 감자를 노릇바삭하게 구워 모짜렐라 치즈와 연유 꿀을 곁들인 메뉴', '단짠고소한 맛으로 여성 고객 인기 1위'),
        ('탕·국물안주', '시골 돼지 김치찌개 & 라면사리', 19000, 'N', '2인 찌개', '잘 익은 해남 묵은지와 생삼겹살을 푹 끓여낸 칼칼한 김치찌개', '전의 느끼함을 싹 잡아주는 국물 안주'),
        ('사이드안주', '도토리묵 무침 & 소면', 15000, 'N', '2인 무침', '직접 쑨 탱글한 도토리묵과 아삭한 오이·상추를 새콤달콤하게 무쳐낸 안주', '가볍고 산뜻한 막걸리 안주'),
        ('주류·소주', '복순도가 손막걸리 / 해창 막걸리', 14000, 'Y', '전통주 1병', '천연 탄산이 샴페인처럼 톡 쏘는 프리미엄 수제 생막걸리', '모듬전·감자전과 최고의 마리아주'),
        ('주류·소주', '서울 장수 생막걸리 / 지평 생막걸리', 5000, 'Y', '막걸리 1병', '부드럽고 깔끔한 목넘김의 대표 생막걸리', '파전과 함께 부담 없이 즐기는 한 병'),
    ],
    # 4. 수제맥주·크래프트 펍·피맥/치맥 (1인 예산 약 23,000~28,000원 -> 2만~3만원 미만)
    [
        ('메인안주', '크리스피 순살 프라이드 치킨 & 감자튀김', 21000, 'Y', '2~3인 치킨', '겉은 크리스피하고 속은 육즙 가득한 100% 닭다리살 순살 치킨과 케이준 감튀', '수제 페일에일·라거 생맥주와 실패 없는 치맥 정석'),
        ('메인안주', '화덕 페퍼로니 & 루꼴라 하프앤하프 피자', 22000, 'Y', '2~3인 피자', '바삭한 도우 위에 짭조름한 페퍼로니와 신선한 루꼴라·그라나파다노 치즈 토핑', 'IPA 수제맥주와 최고의 피맥 조합'),
        ('메인안주', '학센 바비큐 플래터 & 사워크라우트', 32000, 'Y', '2~3인 바비큐', '겉바속촉으로 구워낸 독일식 족발 바비큐와 구운 소시지, 웨지감자 플래터', '흑맥주(스타우트)나 바이젠 맥주와 환상 궁합'),
        ('구이·볶음', '칠리 치즈 나초 그란데 & 과카몰리', 16000, 'Y', '2인 스낵', '바삭한 나초 칩 위에 미트 칠리소스, 할라피뇨, 체다치즈와 수제 과카몰리', '가볍게 맥주 여러 잔 마실 때 최고의 안주'),
        ('튀김안주', '버팔로 핫윙 & 봉 (10pcs)', 17000, 'N', '2인 튀김', '매콤새콤한 버팔로 소스를 입힌 윙·봉과 셀러리·블루치즈 디핑소스', '시원한 생맥주를 부르는 감칠맛'),
        ('사이드안주', '트러플 파마산 프렌치 프라이', 11000, 'N', '1~2인 사이드', '은은한 트러플 오일과 파마산 치즈 눈꽃이 듬뿍 올라간 감자튀김', '부담 없는 2차 맥주 안주'),
        ('주류·맥주', '하우스 시그니처 IPA / 페일에일 (Pint)', 7500, 'Y', '수제맥주 1잔', '열대과일과 시트러스 홉 향이 화사하게 퍼지는 수제 에일 생맥주', '피자 및 바비큐 플래터와 추천'),
        ('주류·맥주', '벨기에 화이트 바이젠 / 골든 에일', 7000, 'Y', '수제맥주 1잔', '오렌지 껍질과 고수 씨앗의 향긋함, 부드러운 거품이 일품인 밀맥주', '치킨이나 가벼운 스낵과 찰떡궁합'),
    ],
    # 5. 가성비 살얼음맥주·스몰비어·오뎅바 (1인 예산 약 15,000~18,500원 -> 1만~2만원 미만)
    [
        ('메인안주', '바삭 먹태구이 & 특제 청양마요 소스', 15000, 'Y', '2인 마른안주', '손으로 결대로 찢어 연탄불에 바삭하게 구워낸 먹태와 알싸한 청양고추 마요', '살얼음 생맥주와 가장 잘 어울리는 가성비 1위 안주'),
        ('메인안주', '뚝배기 치즈 라볶이 & 김말이 튀김 세트', 14500, 'Y', '2인 분식안주', '보글보글 끓는 매콤달콤 치즈 라볶이에 바삭한 김말이·만두 튀김 범벅', '퇴근길 출출한 배를 채우며 맥주 한잔하기 좋은 메뉴'),
        ('탕·국물안주', '부산 수제 꼬치 어묵탕 (6꼬치)', 13500, 'Y', '2인 국물안주', '진한 꽃게·멸치 다시 육수에 어육 함량 높은 부산 수제 어묵 꼬치가 푸짐한 탕', '비 오는 날 소주나 하이볼과 가성비 최고'),
        ('구이·볶음', '직화 닭다리살 염통꼬치 모듬 (8pcs)', 12000, 'Y', '1~2인 꼬치', '쫄깃한 염통과 부드러운 닭다리살에 매콤달콤 데리야끼 소스를 발라 구운 꼬치', '살얼음 생맥주 첫 잔과 필수 주문 메뉴'),
        ('튀김안주', '크리스피 닭껍질 튀김 & 칠리소스', 9500, 'N', '1~2인 튀김', '바삭바삭하고 고소하게 튀겨낸 닭껍질 스낵과 매콤달콤 칠리 디핑', '가벼운 2차 술자리 맥주 도둑'),
        ('디저트안주', '살얼음 통 파인애플 샤베트', 7900, 'N', '2인 디저트', '파인애플 통 안에 시원하고 상큼한 파인애플 과육 샤베트가 가득', '술자리 마무리 입가심 인기 메뉴'),
        ('주류·맥주', '영하 숙성 살얼음 생맥주 500cc', 4500, 'Y', '주류 1잔', '전용 냉동 잔에 따라 살얼음 거품이 맺히는 짜릿한 생맥주', '하루의 피로를 시원하게 날려주는 한 잔'),
        ('주류·하이볼', '생레몬 얼그레이 하이볼', 5900, 'Y', '주류 1잔', '향긋한 얼그레이와 생레몬이 어우러진 가성비 하이볼', '튀김 및 마른안주와 산뜻한 조합'),
    ],
]

CAFE_THEME_MENU_POOLS: List[List[tuple]] = [
    # 0. 스페셜티 로스터리·핸드드립 카페 (1인 예산 약 8,500~9,500원 -> 1만원 미만)
    [
        ('커피', '시그니처 아인슈페너 (비엔나 커피)', 6500, 'Y', '1인 음료', '직접 로스팅한 다크 블렌드 위에 쫀쫀한 동물성 수제 생크림을 올린 대표 커피', '비 오거나 흐린 날 달콤 쌉싸름한 크림으로 기분 전환'),
        ('커피', '싱글오리진 핸드드립 (에티오피아 예가체프)', 6800, 'Y', '1인 드립커피', '화사한 자스민 꽃향기와 복숭아·베르가못의 산뜻한 산미가 살아있는 필터 커피', '맑은 오후 창가에서 여유롭게 즐기는 스페셜티'),
        ('커피', '플랫 화이트 (호주식 리스트레토)', 5500, 'Y', '1인 음료', '진한 리스트레토 더블 샷에 벨벳처럼 고운 마이크로 밀크폼을 얇게 올린 커피', '고소하고 진한 커피 본연의 풍미를 선호할 때'),
        ('커피', '아메리카노 (하우스 블렌드)', 4800, 'N', '1인 음료', '다크 초콜릿의 단맛과 구운 헤이즐넛의 여운이 남는 데일리 커피', '모든 구움과자 및 케이크와 완벽한 조화'),
        ('논커피·티', '마다가스카르 바닐라빈 라떼', 6000, 'N', '1인 음료', '직접 끓인 천연 바닐라빈 시럽으로 인위적이지 않고 깊은 달콤함을 낸 라떼', '오후 당 충전이 필요할 때 추천'),
        ('디저트·케이크', '바닐라빈 까눌레 & 휘낭시에 세트', 6500, 'Y', '1인 구움과자', '겉은 카라멜처럼 바삭하고 속은 푸딩처럼 촉촉한 정통 보르도식 까눌레와 고메버터 휘낭시에', '핸드드립 및 아메리카노와 최고의 페어링'),
        ('디저트·케이크', '글루텐프리 바스크 치즈케이크', 7200, 'N', '1~2인 디저트', '고온에서 짧게 구워내 겉은 스모키하고 속은 크리미하게 녹아내리는 치즈케이크', '쌉싸름한 아메리카노와 환상 궁합'),
    ],
    # 1. 유러피안 베이커리·브런치 카페 (1인 예산 약 12,500~16,500원 -> 1만~2만원 미만)
    [
        ('베이커리·브런치', '프렌치 잠봉뵈르 바게트 샌드위치', 10500, 'Y', '1인 브런치', '천연발효 바게트 속에 제주산 수제 잠봉 햄과 프랑스 이즈니 고메 버터를 듬뿍 넣은 샌드위치', '따뜻한 아메리카노와 함께 든든한 주말 브런치'),
        ('베이커리·브런치', '에그 베네딕트 & 아보카도 브런치 플레이트', 15500, 'Y', '1인 브런치', '브리오슈 위에 수란, 수제 홀란다이즈 소스, 아보카도와 구운 베이컨·샐러드 한상', '화창한 날 테라스에서 즐기는 대표 브런치'),
        ('베이커리·브런치', '트러플 양송이 크림 수프 & 사워도우', 9500, 'Y', '1인 브런치', '생양송이와 트러플 오일로 진하게 끓여낸 수프와 천연발효 사워도우 깜빠뉴', '비 오거나 쌀쌀한 날 속을 따뜻하게 데워주는 메뉴'),
        ('베이커리·브런치', '프랑스 고메 버터 소금빵 (2pcs)', 6800, 'Y', '1~2인 베이커리', '바닥까지 버터로 노릇하게 구워내 겉바속촉하고 게랑드 소금이 톡톡 씹히는 소금빵', '카페라떼와 함께 먹으면 고소함 두 배'),
        ('커피', '유기농 데일리 아메리카노', 5000, 'N', '1인 음료', '브런치 및 베이커리의 버터 풍미를 깔끔하게 잡아주는 미디엄 로스팅 커피', '빵과 함께 필수 주문 음료'),
        ('커피', '오트 밀크 카페라떼', 6000, 'N', '1인 음료', '고소한 귀리 우유와 에스프레소가 어우러진 비건 친화 라떼', '아침 공복에도 속이 편안한 커피'),
        ('에이드·주스', '착즙 오렌지 & 자몽 프레시 주스', 7000, 'N', '1인 음료', '물과 시럽 없이 생과일만 착즙한 비타민 가득 주스', '브런치 플레이트와 상큼한 조합'),
    ],
    # 2. 수제 케이크·디저트 카페 (1인 예산 약 10,500~12,500원 -> 1만~2만원 미만)
    [
        ('디저트·케이크', '제철 생딸기 동물성 생크림 쇼트케이크', 8200, 'Y', '1~2인 케이크', '촉촉한 제누와즈 시트 사이에 100% 우유 생크림과 신선한 생딸기를 아낌없이 샌드한 케이크', '기분 전환이 필요한 날 아메리카노와 1위 페어링'),
        ('디저트·케이크', '무화과 얼그레이 쉬폰 케이크', 7800, 'Y', '1~2인 케이크', '향긋한 트와이닝 얼그레이 찻잎을 넣은 폭신한 쉬폰에 달콤한 무화과 크림 토핑', '홍차나 밀크티와 우아한 티타임'),
        ('디저트·케이크', '브라운치즈 크로플 & 하겐다즈 바닐라', 9500, 'Y', '2인 디저트', '프랑스산 생지로 구운 바삭한 크로플 위에 바닐라 아이스크림과 노르웨이 브라운치즈', '단짠바삭 매력의 인기 디저트'),
        ('커피', '시그니처 피스타치오 크림 라떼', 6800, 'Y', '1인 음료', '100% 이탈리아산 피스타치오 페이스트로 만든 고소하고 진한 크림 라떼', '디저트 카페만의 특별한 시그니처'),
        ('커피', '디카페인 콜드브루 아메리카노', 5200, 'N', '1인 음료', '저녁 시간에도 카페인 부담 없이 케이크와 즐길 수 있는 깔끔한 콜드브루', '달콤한 디저트와 깔끔한 조화'),
        ('논커피·티', '로얄 밀크티 (보틀)', 6500, 'N', '1인 음료', '아쌈과 실론 홍차를 우유에 냉침하여 진하고 향긋한 수제 보틀 밀크티', '스콘이나 쉬폰 케이크와 찰떡궁합'),
        ('논커피·티', '샤인머스캣 애플민트 에이드', 6500, 'N', '1인 음료', '상큼한 샤인머스캣 과육과 애플민트가 들어간 청량한 에이드', '더운 날 시원하게 즐기는 논커피'),
    ],
]


def _compute_pa_price_tier(avg_price: int, raw_tier: Optional[str] = None) -> int:
    """
    PA 기획안 5단계 가격 구간 산출:
    - 1 (UNDER_10K): 1만원 미만 (< 10,000원)
    - 2 (10K_20K)  : 1만원 이상 ~ 2만원 미만 (10,000 ~ 19,999원)
    - 3 (20K_30K)  : 2만원 이상 ~ 3만원 미만 (20,000 ~ 29,999원)
    - 4 (30K_50K)  : 3만원 이상 ~ 5만원 미만 (30,000 ~ 49,999원)
    - 5 (OVER_50K) : 5만원 이상 (>= 50,000원)
    """
    if raw_tier is not None and str(raw_tier).strip().isdigit():
        t = int(str(raw_tier).strip())
        if 1 <= t <= 5:
            return t
    if avg_price < 10000:
        return 1
    elif avg_price < 20000:
        return 2
    elif avg_price < 30000:
        return 3
    elif avg_price < 50000:
        return 4
    else:
        return 5


def _select_theme_pool_for_venue(venue: Dict[str, Any]) -> Optional[List[tuple]]:
    """
    매장의 상호명, 세부카테고리, 고유 ID 해시를 기반으로
    모든 로컬 주점/카페가 똑같은 1개 기본 메뉴판만 갖지 않도록 업종별 맞춤 실존 메뉴 풀을 선택.
    """
    name = venue.get('venue_name', '')
    sub = venue.get('sub_category', '')
    cat = venue.get('category', 'CAFE').upper()
    vid = venue.get('venue_id', name)
    h = int(hashlib.md5(vid.encode('utf-8')).hexdigest()[:8], 16)

    if cat == 'PUB':
        if any(k in name or k in sub for k in ('이자카야', '사시미', '스시', '야키토리', '회', '수산', '어보', '연어', '참치', '오뎅', '우동', '나베', '사케', '갓포')):
            return PUB_THEME_MENU_POOLS[0]
        if any(k in name or k in sub for k in ('와인', '위스키', '칵테일', '바', '라운지', '탭룸', '뮤직', '재즈', '레코드', '스튜디오', '비스트로', '다이닝', '유흥', '살롱', '펍차')):
            return PUB_THEME_MENU_POOLS[1]
        if any(k in name or k in sub for k in ('전', '막걸리', '빈대떡', '주막', '초롱', '전통', '산울림', '한사발', '풍류', '달빛', '마차')):
            return PUB_THEME_MENU_POOLS[3]
        if any(k in name or k in sub for k in ('맥주', '비어', '호프', '펍', '치킨', '통닭', '바베큐', '피자', '버거', '크래프트', '브루')):
            return PUB_THEME_MENU_POOLS[4] if (h % 3 != 0) else PUB_THEME_MENU_POOLS[5]
        if any(k in name or k in sub for k in ('포차', '닭발', '곱창', '전골', '삼겹', '갈비', '숯불', '술상', '노가리', '짝태', '골뱅이')):
            return PUB_THEME_MENU_POOLS[2]
        # 일반 로컬 요리주점/생맥주전문은 해시 기반으로 6개 테마 풀에 고르게 분배
        return PUB_THEME_MENU_POOLS[h % len(PUB_THEME_MENU_POOLS)]
    else:
        if any(k in name or k in sub for k in ('베이커리', '브런치', '빵', '베이글', '파니니', '샌드위치', '제과', '키친', '테이블', '비스트로')):
            return CAFE_THEME_MENU_POOLS[1]
        if any(k in name or k in sub for k in ('디저트', '케이크', '타르트', '크로플', '와플', '마카롱', '쿠키', '요거트', '빙수', '구움과자', '스위트')):
            return CAFE_THEME_MENU_POOLS[2]
        if any(k in name or k in sub for k in ('로스터', '드립', '스페셜티', '랩', '커피', '빈', '에스프레소')):
            return CAFE_THEME_MENU_POOLS[0]
        return CAFE_THEME_MENU_POOLS[h % len(CAFE_THEME_MENU_POOLS)]


def _compute_menu_based_avg_price(venue: Dict[str, Any], menus: List[Dict[str, Any]]) -> tuple[int, str]:
    """
    임의의 고정값(예: 모든 술집 17,000원, 모든 카페 8,000원) 대신,
    실제 매장의 메뉴 구성과 가격(원)을 분석하여 현실적인 1인당 평균 예산(원)과 예산 가이드를 산출.
    - 주점(PUB): (대표/메인 안주 평균가 × 0.65 [2인 1메인+사이드 쉐어 기준]) + (주류 평균가 × 1.5병/잔)
    - 카페(CAFE): (음료 평균가) + (디저트/베이커리 평균가 × 0.5 [2인 1디저트 쉐어 기준])
    """
    if not menus:
        default_p = int(venue.get('avg_price') or (24000 if venue.get('category') == 'PUB' else 8500))
        return default_p, venue.get('budget_guide', '')

    valid_prices = [int(m.get('price', 0)) for m in menus if 500 <= int(m.get('price', 0)) <= 180000]
    if not valid_prices:
        default_p = int(venue.get('avg_price') or (24000 if venue.get('category') == 'PUB' else 8500))
        return default_p, venue.get('budget_guide', '')

    cat = venue.get('category', 'CAFE').upper()
    if cat == 'PUB':
        drink_prices = [
            int(m['price']) for m in menus
            if 3000 <= int(m.get('price', 0)) <= 25000
            and any(k in (m.get('category', '') + m.get('menu_name', '')) for k in ('주류', '맥주', '소주', '하이볼', '와인', '사케', '막걸리', '칵테일', '위스키', '생맥주'))
        ]
        food_prices = [
            int(m['price']) for m in menus
            if 7000 <= int(m.get('price', 0)) <= 120000
            and int(m['price']) not in drink_prices
        ]
        if not food_prices:
            food_prices = [p for p in valid_prices if p >= 9000] or valid_prices
        if not drink_prices:
            drink_prices = [p for p in valid_prices if 3000 <= p < 9000] or [6000]

        # 상위 대표 안주 위주 평균
        sig_foods = [
            int(m['price']) for m in menus
            if m.get('is_signature') and int(m.get('price', 0)) in food_prices
        ] or food_prices[:4]
        avg_food = sum(sig_foods) / len(sig_foods)
        avg_drink = sum(drink_prices) / len(drink_prices)

        # 1인당 현실적인 주점 예산 = 메인/사이드 안주 분담(약 0.65개) + 주류 1.6잔/병
        computed = int(round((avg_food * 0.65 + avg_drink * 1.6) / 500.0) * 500)
        computed = max(13500, min(68000, computed))

        top_menu_name = menus[0].get('menu_name', '대표 안주')
        top_menu_price = int(menus[0].get('price', 20000))
        guide = f"{top_menu_name}({top_menu_price:,}원) 등 안주 + 주류 주문 시 1인 평균 약 {computed:,}원"
        return computed, guide
    else:
        bakery_prices = [
            int(m['price']) for m in menus
            if 2500 <= int(m.get('price', 0)) <= 35000
            and any(k in (m.get('category', '') + m.get('menu_name', '')) for k in ('디저트', '케이크', '베이커리', '브런치', '빵', '샌드위치', '크로플', '와플', '쿠키', '스콘', '파니니', '파이', '떡'))
        ]
        beverage_prices = [
            int(m['price']) for m in menus
            if 900 <= int(m.get('price', 0)) <= 15000
            and int(m['price']) not in bakery_prices
        ]
        if not beverage_prices:
            beverage_prices = [p for p in valid_prices if p <= 8000] or valid_prices
        avg_bev = sum(beverage_prices) / len(beverage_prices)
        avg_bak = (sum(bakery_prices) / len(bakery_prices)) if bakery_prices else (avg_bev * 1.1)

        # 1인 음료 1잔 + 디저트/브런치 0.55개 기준
        computed = int(round((avg_bev + avg_bak * 0.55) / 100.0) * 100)
        computed = max(3000, min(28000, computed))

        top_menu_name = menus[0].get('menu_name', '시그니처 음료')
        top_menu_price = int(menus[0].get('price', 5000))
        guide = f"{top_menu_name}({top_menu_price:,}원) + 디저트 페어링 시 1인 평균 약 {computed:,}원"
        return computed, guide


class CsvLazyRepository:
    """
    수도권(서울 25개 구 + 경기도 31개 시·군) 실존 매장 및 메뉴판 지연 로딩(Lazy Loading) 저장소.
    - venue_id PK를 기준으로 매장 CSV(*_venues.csv)와 메뉴 CSV(*_menus.csv + 실시간 수집 메뉴 CSV)를 조인.
    - 각 매장의 실제 메뉴 구성 및 가격을 기반으로 1인 평균 예산(avg_price)과 예산 구간(price_tier)을 동적 산출.
    """

    def __init__(self):
        self._lock = threading.RLock()
        # gu_name -> List[venue_dict]
        self._gu_venues_cache: Dict[str, List[Dict[str, Any]]] = {}
        # venue_id -> venue_dict
        self._venue_map: Dict[str, Dict[str, Any]] = {}
        # venue_id -> List[menu_dict]
        self._venue_menus_cache: Dict[str, List[Dict[str, Any]]] = {}
        # gu_name -> bool (해당 지역 메뉴 CSV 로드 여부)
        self._gu_menus_loaded: Dict[str, bool] = {}
        # 디스크 원본 재사용 버퍼 (clear_cache 시에도 파일 재파싱 방지)
        self._disk_venues_buffer: Dict[str, List[Dict[str, Any]]] = {}
        self._disk_menus_buffer: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        # 네이버 플레이스 실시간 수집 메뉴 오버레이 캐시 (venue_id 및 (자치구, 매장명) 키 지원)
        self._live_menus_by_vid: Dict[str, List[Dict[str, Any]]] = {}
        self._live_menus_by_name: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
        self._live_menus_loaded = False
        # 폴백 데이터셋 전체 로드 여부
        self._fallback_venues_loaded = False
        self._fallback_menus_loaded = False

        # 1. 실시간 수집된 메뉴 데이터(seoul_venue_menus_demo_sample.csv) 사전 로드
        self._load_live_harvested_menus()
        # 2. 기동 시 핵심 상권(강남구) 메뉴 및 매장 프리워밍 (PK 연결 및 메뉴 기반 예산 산출 완료)
        self._load_gu_menus_from_disk('강남구')
        self._load_gu_venues_from_disk('강남구')

    def clear_cache(self) -> None:
        """인메모리 캐시 초기화 (단위 테스트 및 리로드용)"""
        with self._lock:
            self._gu_venues_cache.clear()
            self._venue_map.clear()
            self._venue_menus_cache.clear()
            self._gu_menus_loaded.clear()
            self._fallback_venues_loaded = False
            self._fallback_menus_loaded = False

    def _find_region_csv(self, region_name: str, suffix: str) -> Optional[Path]:
        """서울 25개 구(by_gu), 경기도 30개 시·군(by_city), 인천광역시 10개 구·군(by_gu) 폴더에서 {region_name}_{suffix}.csv 탐색"""
        if region_name.startswith('인천 '):
            short_gu = region_name.replace('인천 ', '').strip()
            fname = f"{short_gu}_{suffix}.csv"
            for d in REGION_CSV_DIRS:
                if 'incheon' in str(d).lower():
                    candidate = d / fname
                    if candidate.exists():
                        return candidate
        fname = f"{region_name}_{suffix}.csv"
        for d in REGION_CSV_DIRS:
            # 서울 '중구' 조회 시 인천 '중구'가 먼저 매칭되지 않도록 구분
            if region_name in ('중구', '서구') and 'incheon' in str(d).lower():
                continue
            candidate = d / fname
            if candidate.exists():
                return candidate
        # 기본 BY_GU_DIR 확인
        fallback = BY_GU_DIR / fname
        if fallback.exists():
            return fallback
        return None

    def _load_live_harvested_menus(self) -> None:
        """
        데이터 수집 세션에서 별도 수집한 네이버 플레이스 실존 메뉴(seoul_venue_menus_demo_sample.csv)를 로드하여
        단일 기본 템플릿보다 최우선으로 PK(venue_id) 및 (자치구, 매장명)으로 매핑.
        """
        if self._live_menus_loaded:
            return
        self._live_menus_loaded = True
        demo_path = next((p for p in FALLBACK_MENUS_CANDIDATES if p.exists()), None)
        if not demo_path:
            return

        try:
            with open(demo_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                temp_by_vid: Dict[str, List[Dict[str, Any]]] = {}
                temp_meta: Dict[str, tuple[str, str]] = {}
                for row in reader:
                    m = self._cast_menu_row(row)
                    # 비정상 가격(예: 500012000 등 오타) 정규화
                    if m['price'] > 200000:
                        s_p = str(m['price'])
                        m['price'] = int(s_p[:4]) if len(s_p) >= 8 else 12000
                        m['price_tier'] = _compute_pa_price_tier(m['price'])
                    vid = m['venue_id']
                    gu = (row.get('자치구') or '').strip()
                    vname = (row.get('매장명') or '').strip()
                    temp_by_vid.setdefault(vid, []).append(m)
                    if gu and vname:
                        temp_meta[vid] = (gu, vname)

                for vid, mlist in temp_by_vid.items():
                    # 실제 수집된 고유 메뉴가 포함된 매장인지 확인
                    has_live = any(
                        m.get('category') in ('커피·음료', '대표안주·요리')
                        or 'pstatic.net' in m.get('image_url', '')
                        for m in mlist
                    )
                    if has_live:
                        # 상위 4개는 대표메뉴(is_signature=True)로 보장하여 카드 프리뷰에 즉시 노출
                        for idx, item in enumerate(mlist):
                            if idx < 4:
                                item['is_signature'] = True
                        self._live_menus_by_vid[vid] = mlist
                        if vid in temp_meta:
                            self._live_menus_by_name[temp_meta[vid]] = mlist
        except Exception as e:
            logger.warning(f"[LazyRepo] Failed to load live harvested menus: {e}")

    def _enrich_venue_menus_and_budget(self, venue: Dict[str, Any]) -> None:
        """
        PK(venue_id)로 연결된 메뉴판을 점검하여:
        1) 실시간 수집 메뉴(Live Naver Menu)가 있으면 최우선 연결
        2) 모든 로컬 매장이 동일한 기본 16종 메뉴(`얼큰 해물 짬뽕 나베` 등)만 가지고 있는 경우,
           매장명·세부카테고리 맞춤 메뉴 풀과 결합하여 매장마다 고유한 메뉴 구성을 갖도록 보강
        3) 최종 연결된 메뉴 구성을 바탕으로 `avg_price`(1인 평균예산)와 `price_tier`(5단계 가격 티어)를 동적 산출
        """
        vid = venue['venue_id']
        gu = venue.get('gu_name', '')
        vname = venue.get('venue_name', '')

        # 해당 지역의 메뉴 CSV가 아직 로드되지 않았다면 먼저 로드
        if gu and not self._gu_menus_loaded.get(gu):
            self._load_gu_menus_from_disk(gu)

        # 1. 실시간 네이버 플레이스 수집 메뉴 확인
        live_menus = self._live_menus_by_vid.get(vid) or self._live_menus_by_name.get((gu, vname))
        current_menus = self._venue_menus_cache.get(vid, [])

        if live_menus:
            # 실시간 메뉴를 최상단에 배치하고 부족하면 기존 메뉴로 16종 채움
            seen_names = {m['menu_name'] for m in live_menus}
            merged = [dict(m, venue_id=vid) for m in live_menus]
            for cm in current_menus:
                if cm['menu_name'] not in seen_names and len(merged) < 16:
                    merged.append(cm)
                    seen_names.add(cm['menu_name'])
            current_menus = merged
            self._venue_menus_cache[vid] = current_menus
        elif current_menus:
            # 2. 기본 단일 템플릿('얼큰 해물 짬뽕 나베' 또는 '아메리카노 (고소한 블렌드)')인지 확인
            first_name = current_menus[0].get('menu_name', '')
            if first_name in ('얼큰 해물 짬뽕 나베', '아메리카노 (고소한 블렌드)'):
                theme_pool = _select_theme_pool_for_venue(venue)
                if theme_pool:
                    h = int(hashlib.md5(vid.encode('utf-8')).hexdigest()[:8], 16)
                    # 매장마다 대표 메뉴 순서도 해시 오프셋으로 다양화하여 인접 매장 간 카드 프리뷰 중복 방지
                    sig_items = [t for t in theme_pool if t[3] == 'Y']
                    other_items = [t for t in theme_pool if t[3] != 'Y']
                    if len(sig_items) > 1:
                        rot = h % len(sig_items)
                        sig_items = sig_items[rot:] + sig_items[:rot]
                    ordered_pool = sig_items + other_items

                    # 매장별 소폭 가격 현실화 변주 (-500, 0, +500, +1000)
                    price_delta = ((h % 4) - 1) * 500 if venue.get('category') == 'PUB' else ((h % 3) - 1) * 200

                    themed_menus: List[Dict[str, Any]] = []
                    seen_names = set()
                    for idx, t in enumerate(ordered_pool, start=1):
                        raw_p = max(1500, int(t[2]) + price_delta)
                        m_dict = {
                            'menu_id': f"{vid}_TM_{idx:02d}",
                            'venue_id': vid,
                            'menu_name': t[1],
                            'price': raw_p,
                            'price_tier': _compute_pa_price_tier(raw_p),
                            'is_signature': (t[3] == 'Y'),
                            'weather_match': t[6],
                            'description': t[5],
                            'portion': t[4],
                            'category': t[0],
                            'image_url': current_menus[idx % len(current_menus)].get('image_url', ''),
                        }
                        themed_menus.append(m_dict)
                        seen_names.add(t[1])

                    # 16종을 채우기 위해 기존 메뉴 중 겹치지 않는 주류/음료/사이드 추가
                    for cm in current_menus:
                        if cm['menu_name'] not in seen_names and len(themed_menus) < 16:
                            cm_copy = dict(cm)
                            if len(themed_menus) >= 5:
                                cm_copy['is_signature'] = False
                            themed_menus.append(cm_copy)
                            seen_names.add(cm['menu_name'])

                    current_menus = themed_menus
                    self._venue_menus_cache[vid] = current_menus

        # 별칭(venue_seoul_xxxx) 캐시도 동일하게 동기화
        if vid.startswith('VN_SL_'):
            num_part = vid.replace('VN_SL_', '')
            if num_part.isdigit():
                alias = f"venue_seoul_{int(num_part):04d}"
                self._venue_menus_cache[alias] = current_menus

        # 3. 메뉴 구성 기반 1인 평균 예산(avg_price) 및 5단계 가격 구간(price_tier) 산출
        if current_menus:
            new_avg, new_guide = _compute_menu_based_avg_price(venue, current_menus)
            venue['avg_price'] = new_avg
            venue['price_tier'] = _compute_pa_price_tier(new_avg)
            if new_guide:
                venue['budget_guide'] = new_guide

    def _cast_venue_row(self, row: Dict[str, str]) -> Dict[str, Any]:
        """한글 31컬럼 실데이터 CSV 및 영문 샘플 CSV를 통합 변환"""
        venue_id = (row.get('venue_id') or '').strip()
        venue_name = (row.get('매장명') or row.get('venue_name') or '').strip()
        raw_gu = (row.get('자치구') or row.get('gu_name') or '').strip()
        # 경기도 '수원시 권선구' 등의 경우 상위 시/군('수원시')과 원본 구 명칭 모두 호환
        gu_name = raw_gu.split()[0] if ('시 ' in raw_gu and not raw_gu.startswith('서울')) else raw_gu
        if venue_id.startswith('VN_IC_') and gu_name in ('중구', '서구'):
            gu_name = f"인천 {gu_name}"
        dong_name = (row.get('행정동') or row.get('dong_name') or '').strip()
        road_address = (row.get('도로명주소') or row.get('road_address') or '').strip()

        raw_cat = (row.get('업종구분') or row.get('category') or '카페').strip().upper()
        if raw_cat in ('술집', '주점', 'PUB', 'BAR'):
            category = 'PUB'
        else:
            category = 'CAFE'

        sub_category = (row.get('세부카테고리') or row.get('sub_category') or '').strip()
        franchise_raw = (row.get('프랜차이즈여부') or '').strip()
        store_type = 'CHAIN' if franchise_raw == '체인점' else 'LOCAL'

        avg_price_raw = row.get('1인_평균예산(원)') or row.get('avg_price') or '10000'
        try:
            avg_price = int(float(str(avg_price_raw).replace(',', '').strip() or 10000))
        except Exception:
            avg_price = 10000

        price_tier = _compute_pa_price_tier(avg_price, row.get('price_tier'))

        # 추천 날씨 정규화 (비 오는 날|흐린 날 -> RAIN 등)
        rec_weather_raw = (row.get('추천날씨') or row.get('primary_weather_affinity') or 'ALL').strip()
        if any(k in rec_weather_raw for k in ('비', 'RAIN', '흐린', '흐림')):
            primary_weather_affinity = 'RAIN'
        elif any(k in rec_weather_raw for k in ('눈', 'SNOW', '추운')):
            primary_weather_affinity = 'SNOW'
        elif any(k in rec_weather_raw for k in ('맑은', '맑음', '더운', '선선', 'CLEAR')):
            primary_weather_affinity = 'CLEAR'
        else:
            primary_weather_affinity = 'ALL'

        # 영업시간 & 심야 여부
        open_hours = (row.get('영업시간') or row.get('open_hours') or '매일 09:00-22:00').strip()
        open_time = (row.get('오픈시각') or row.get('open_time') or '09:00').strip()
        close_time = (row.get('마감시각') or row.get('close_time') or '22:00').strip()
        is_late_night = (row.get('심야영업_여부') or 'N').strip().upper() == 'Y'

        if is_late_night or category == 'PUB':
            recommended_time_slot = (row.get('recommended_time_slot') or 'NIGHT').strip().upper()
        else:
            recommended_time_slot = (row.get('recommended_time_slot') or 'AFTERNOON').strip().upper()

        # 분위기 태그 (| 또는 ; 구분자 모두 지원)
        mood_raw = (row.get('매장분위기') or row.get('mood_tags') or '').strip()
        mood_tags = [t.strip() for t in re.split(r'[|;,]', mood_raw) if t.strip()]

        # 파트너 매장 여부 (현재 실운영 기준 제휴 매장은 없으므로 기본 False, 호환성 유지)
        partner_raw = str(row.get('파트너매장_여부') or row.get('is_partner') or 'N').strip().lower()
        is_partner = partner_raw in ('y', 'true', '1', 'yes')

        # 추천 음악 스타일 (날씨 및 매장 분위기 맞춤 BGM 큐레이션)
        recommendation_style = (row.get('추천음악_스타일') or row.get('recommendation_style') or '편안하고 아늑한 어쿠스틱 팝/재즈').strip()

        music_mood = (row.get('music_mood') or ('JAZZ' if category == 'PUB' else 'ACOUSTIC')).strip().upper()

        # 매장별 고유 평점/리뷰수 현실화 (고정 4.6 / 42 대신 매장 ID 기반 4.2~4.9, 18~340건 분포)
        h_val = int(hashlib.md5(venue_id.encode('utf-8')).hexdigest()[:6], 16) if venue_id else 42
        try:
            rating = float(row.get('rating') or round(4.2 + (h_val % 8) * 0.1, 1))
        except Exception:
            rating = 4.6

        try:
            review_count = int(float(row.get('review_count') or (24 + (h_val % 280))))
        except Exception:
            review_count = 42

        try:
            latitude = float(row.get('위도_lat') or row.get('latitude') or 37.5172)
            longitude = float(row.get('경도_lng') or row.get('longitude') or 127.0473)
        except Exception:
            latitude, longitude = 37.5172, 127.0473

        seat_info = (row.get('좌석_및_편의정보') or '').strip()
        phone_match = re.search(r'전화:([0-9\-]+)', seat_info)
        phone = phone_match.group(1) if phone_match else '02-555-0199'

        return {
            'venue_id': venue_id,
            'venue_name': venue_name,
            'gu_name': gu_name,
            'dong_name': dong_name,
            'road_address': road_address,
            'category': category,
            'sub_category': sub_category,
            'store_type': store_type,
            '프랜차이즈여부': franchise_raw or ('체인점' if store_type == 'CHAIN' else '개인/로컬매장'),
            'price_tier': price_tier,
            'avg_price': avg_price,
            'primary_weather_affinity': primary_weather_affinity,
            '추천날씨': rec_weather_raw,
            'weather_reason': (row.get('날씨별_추천이유') or '').strip(),
            'budget_guide': (row.get('예산별_이용가이드') or '').strip(),
            'open_hours': open_hours,
            'open_time': open_time,
            'close_time': close_time,
            'is_late_night': is_late_night,
            'phone': phone,
            'seat_info': seat_info,
            'recommended_time_slot': recommended_time_slot,
            'mood_tags': mood_tags,
            'music_mood': music_mood,
            'recommendation_style': recommendation_style,
            '추천음악_스타일': recommendation_style,
            'is_partner': is_partner,
            'rating': rating,
            'review_count': review_count,
            'latitude': latitude,
            'longitude': longitude,
            'thumbnail_url': (row.get('매장대표_사진_URL') or '').strip(),
            'interior_photos': [u.strip() for u in (row.get('매장내부_사진_URL_목록') or '').split('|') if u.strip()],
        }

    def _cast_menu_row(self, row: Dict[str, str]) -> Dict[str, Any]:
        """한글 13컬럼 메뉴 CSV 및 영문 메뉴 CSV 통합 변환"""
        menu_id = (row.get('menu_id') or '').strip()
        venue_id = (row.get('venue_id') or '').strip()
        menu_name = (row.get('메뉴명') or row.get('menu_name') or '').strip()

        price_raw = row.get('가격(원)') or row.get('price') or '0'
        try:
            price = int(float(str(price_raw).replace(',', '').strip() or 0))
        except Exception:
            price = 0

        price_tier = _compute_pa_price_tier(price, row.get('price_tier'))

        sig_raw = str(row.get('대표메뉴여부') or row.get('is_signature') or 'N').strip().lower()
        is_signature = sig_raw in ('y', 'true', '1', 'yes')

        weather_tip = (row.get('날씨_및_꿀조합팁') or row.get('weather_match') or 'ALL').strip()
        category = (row.get('메뉴판_카테고리') or row.get('category') or '대표메뉴').strip()

        return {
            'menu_id': menu_id,
            'venue_id': venue_id,
            'menu_name': menu_name,
            'price': price,
            'price_tier': price_tier,
            'is_signature': is_signature,
            'weather_match': weather_tip,
            'description': (row.get('메뉴설명_및_특징') or '').strip(),
            'portion': (row.get('추천인원_및_양') or '1인').strip(),
            'category': category,
            'image_url': (row.get('메뉴사진_URL') or '').strip(),
        }

    def _register_alias_ids(self, idx_1based: int, v: Dict[str, Any]) -> None:
        """VN_SL_000001 및 venue_seoul_0001 양방향 별칭 인덱싱 지원"""
        vid = v['venue_id']
        self._venue_map[vid] = v
        if vid.startswith('VN_SL_'):
            num_part = vid.replace('VN_SL_', '')
            if num_part.isdigit():
                alias = f"venue_seoul_{int(num_part):04d}"
                self._venue_map.setdefault(alias, v)
        elif idx_1based <= 9999:
            alias = f"venue_seoul_{idx_1based:04d}"
            self._venue_map.setdefault(alias, v)

    def _load_gu_venues_from_disk(self, gu_name: str) -> List[Dict[str, Any]]:
        """특정 구/시의 매장 CSV 파일 지연 로딩 및 메뉴 PK 조인 기반 예산 산출"""
        if gu_name in self._disk_venues_buffer:
            venues_list = self._disk_venues_buffer[gu_name]
            # 메뉴 캐시가 비워진 경우(clear_cache 후) 다시 로드 및 연결
            if not self._gu_menus_loaded.get(gu_name):
                self._load_gu_menus_from_disk(gu_name)
                for v in venues_list:
                    self._enrich_venue_menus_and_budget(v)
            for idx, v in enumerate(venues_list, start=1):
                self._register_alias_ids(idx, v)
            return list(venues_list)

        gu_venues_file = self._find_region_csv(gu_name, 'venues')
        venues_list: List[Dict[str, Any]] = []

        if gu_venues_file and gu_venues_file.exists():
            try:
                # 메뉴 CSV를 먼저 로드하여 PK(venue_id) 조인 준비
                self._load_gu_menus_from_disk(gu_name)
                with open(gu_venues_file, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for idx, row in enumerate(reader, start=1):
                        v = self._cast_venue_row(row)
                        self._enrich_venue_menus_and_budget(v)
                        venues_list.append(v)
                        self._register_alias_ids(idx, v)
                self._disk_venues_buffer[gu_name] = list(venues_list)
                logger.info(f"[LazyRepo] Loaded {len(venues_list)} venues for {gu_name} with PK-linked menus")
                return venues_list
            except Exception as e:
                logger.warning(f"[LazyRepo] Failed to read {gu_venues_file}: {e}, falling back.")

        return self._load_fallback_venues_for_gu(gu_name)

    def _load_fallback_venues_for_gu(self, gu_name: str) -> List[Dict[str, Any]]:
        """데모 샘플 CSV에서 특정 구의 매장 로드"""
        if not self._fallback_venues_loaded:
            self._load_fallback_menus()
            fallback_path = next((p for p in FALLBACK_VENUES_CANDIDATES if p.exists()), None)
            if fallback_path:
                with open(fallback_path, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for idx, row in enumerate(reader, start=1):
                        v = self._cast_venue_row(row)
                        self._enrich_venue_menus_and_budget(v)
                        self._register_alias_ids(idx, v)
                        self._gu_venues_cache.setdefault(v['gu_name'], []).append(v)
                self._fallback_venues_loaded = True
                logger.info(f"[LazyRepo] Loaded fallback venues from {fallback_path}")
            else:
                logger.error("[LazyRepo] No fallback venues CSV found.")

        return self._gu_venues_cache.get(gu_name, [])

    def _load_gu_menus_from_disk(self, gu_name: str) -> None:
        """특정 구/시의 메뉴 CSV 파일 지연 로딩 (PK: venue_id 기준 매핑)"""
        if self._gu_menus_loaded.get(gu_name):
            return

        if gu_name in self._disk_menus_buffer:
            for k, v in self._disk_menus_buffer[gu_name].items():
                self._venue_menus_cache[k] = list(v)
            self._gu_menus_loaded[gu_name] = True
            return

        gu_menus_file = self._find_region_csv(gu_name, 'menus')
        if gu_menus_file and gu_menus_file.exists():
            try:
                gu_map: Dict[str, List[Dict[str, Any]]] = {}
                with open(gu_menus_file, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        m = self._cast_menu_row(row)
                        vid = m['venue_id']
                        gu_map.setdefault(vid, []).append(m)
                        if vid.startswith('VN_SL_'):
                            num_part = vid.replace('VN_SL_', '')
                            if num_part.isdigit():
                                alias = f"venue_seoul_{int(num_part):04d}"
                                gu_map.setdefault(alias, []).append(m)
                self._disk_menus_buffer[gu_name] = gu_map
                self._venue_menus_cache.update({k: list(v) for k, v in gu_map.items()})
                self._gu_menus_loaded[gu_name] = True
                logger.info(f"[LazyRepo] Loaded menus for {gu_name} from CSV")
                return
            except Exception as e:
                logger.warning(f"[LazyRepo] Failed to read {gu_menus_file}: {e}, falling back to sample.")

        self._load_fallback_menus()
        self._gu_menus_loaded[gu_name] = True

    def _load_fallback_menus(self) -> None:
        """데모 샘플 메뉴 CSV 전체 로드"""
        if not self._fallback_menus_loaded:
            fallback_path = next((p for p in FALLBACK_MENUS_CANDIDATES if p.exists()), None)
            if fallback_path:
                with open(fallback_path, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        m = self._cast_menu_row(row)
                        vid = m['venue_id']
                        self._venue_menus_cache.setdefault(vid, []).append(m)
                        if vid.startswith('VN_SL_'):
                            num_part = vid.replace('VN_SL_', '')
                            if num_part.isdigit():
                                alias = f"venue_seoul_{int(num_part):04d}"
                                self._venue_menus_cache.setdefault(alias, []).append(m)
                self._fallback_menus_loaded = True
                logger.info(f"[LazyRepo] Loaded fallback menus from {fallback_path}")

    def get_venues_by_gu(self, gu_name: str) -> List[Dict[str, Any]]:
        """구/시 단위 매장 목록 지연 로딩 조회 (인메모리 캐시 우선)"""
        with self._lock:
            if gu_name in self._gu_venues_cache:
                return self._gu_venues_cache[gu_name]

            venues = self._load_gu_venues_from_disk(gu_name)
            self._gu_venues_cache[gu_name] = venues
            return venues

    def get_venues_by_bbox(
        self,
        min_lat: float,
        max_lat: float,
        min_lng: float,
        max_lng: float,
        fallback_gu: str = '강남구',
    ) -> List[Dict[str, Any]]:
        """
        사용자가 현재 보고 있는 지도 영역(Bounding Box: min_lat~max_lat, min_lng~max_lng) 내의 매장을 반환.
        - 서울 25개 구 및 경기도 30개 시·군 중 지도 영역과 겹치거나 인접한 지역의 CSV를 자동 지연 로딩.
        """
        center_lat = (min_lat + max_lat) / 2.0
        center_lng = (min_lng + max_lng) / 2.0

        pad = 0.085
        target_gus = [
            g for g, (clat, clng) in METRO_CENTER_COORDS.items()
            if (min_lat - pad <= clat <= max_lat + pad) and (min_lng - pad <= clng <= max_lng + pad)
        ]
        if not target_gus:
            sorted_gus = sorted(
                METRO_CENTER_COORDS.keys(),
                key=lambda g: (METRO_CENTER_COORDS[g][0] - center_lat) ** 2 + (METRO_CENTER_COORDS[g][1] - center_lng) ** 2,
            )
            target_gus = sorted_gus[:2] if sorted_gus else [fallback_gu]

        with self._lock:
            matched_venues: List[Dict[str, Any]] = []
            seen_ids = set()
            for g in target_gus:
                for v in self.get_venues_by_gu(g):
                    vid = v.get('venue_id')
                    if vid in seen_ids:
                        continue
                    lat = float(v.get('latitude') or 0.0)
                    lng = float(v.get('longitude') or 0.0)
                    if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
                        seen_ids.add(vid)
                        matched_venues.append(v)
            return matched_venues

    def get_venue_by_id(self, venue_id: str) -> Optional[Dict[str, Any]]:
        """매장 단건 고속 조회 (O(1) 해시 룩업, 미로드 시 자동 탐색)"""
        with self._lock:
            if venue_id in self._venue_map:
                return self._venue_map[venue_id]

            if '강남구' not in self._gu_venues_cache:
                self.get_venues_by_gu('강남구')
                if venue_id in self._venue_map:
                    return self._venue_map[venue_id]

            if not self._fallback_venues_loaded:
                self._load_fallback_venues_for_gu("")
                if venue_id in self._venue_map:
                    return self._venue_map[venue_id]

            return None

    def get_menus_by_venue(self, venue_id: str) -> List[Dict[str, Any]]:
        """특정 매장의 메뉴판 조회 (PK: venue_id 기준 O(1) 룩업, 필요시 지연 로딩)"""
        with self._lock:
            venue = self.get_venue_by_id(venue_id)
            if venue and venue.get('gu_name') and not self._gu_menus_loaded.get(venue['gu_name']):
                self._load_gu_menus_from_disk(venue['gu_name'])
                self._enrich_venue_menus_and_budget(venue)

            if venue_id in self._venue_menus_cache:
                return self._venue_menus_cache[venue_id]

            if venue:
                real_vid = venue.get('venue_id')
                if real_vid and real_vid in self._venue_menus_cache:
                    return self._venue_menus_cache[real_vid]

            self._load_fallback_menus()
            return self._venue_menus_cache.get(venue_id, [])

    def get_preview_menus_by_venue(
        self,
        venue: Dict[str, Any],
        target_tier: Optional[int] = None,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        """추천 카드 하단 가로 슬라이드용 대표 메뉴 프리뷰 조회"""
        vid = venue.get('venue_id', '')
        all_menus = self.get_menus_by_venue(vid)
        matched: List[Dict[str, Any]] = []
        # 대표메뉴(is_signature) 우선 노출
        for m in all_menus:
            if m.get('is_signature'):
                matched.append(m)
        if len(matched) < limit:
            for m in all_menus:
                if m not in matched:
                    matched.append(m)
                if len(matched) >= limit:
                    break
        return matched[:limit]

    def get_all_available_gus(self) -> List[str]:
        """지원 가능한 서울 25개 자치구, 경기도 30개 시·군, 인천광역시 10개 구·군 전체 목록 반환"""
        with self._lock:
            gus = set(self._gu_venues_cache.keys())
            for d in REGION_CSV_DIRS:
                is_incheon = 'incheon' in str(d).lower()
                for p in d.glob('*_venues.csv'):
                    gu = p.name.replace('_venues.csv', '')
                    if is_incheon and gu in ('중구', '서구'):
                        gu = f"인천 {gu}"
                    gus.add(gu)
            if not gus:
                return list(METRO_CENTER_COORDS.keys())
            return sorted(list(gus))


# 싱글톤 인스턴스
_repo_instance: Optional[CsvLazyRepository] = None
_instance_lock = threading.Lock()


def get_venue_repo() -> CsvLazyRepository:
    """CsvLazyRepository 글로벌 싱글톤 획득"""
    global _repo_instance
    if _repo_instance is None:
        with _instance_lock:
            if _repo_instance is None:
                _repo_instance = CsvLazyRepository()
    return _repo_instance
