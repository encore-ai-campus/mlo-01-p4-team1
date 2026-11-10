"""공통 태그로 해석할 수 있는 손님 검색 조건."""

import re
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from verify_tag_weather import normalize


VenueCategory = Literal["CAFE", "PUB"]
Price = Annotated[StrictInt, Field(ge=1, le=1_000_000)]
CALM_WORDS = ("잔잔한", "잔잔함", "조용한", "조용함", "차분한", "차분함")
PUB_WORDS = ("술집", "주점", "이자카야", "포차", "와인바", "칵테일바", "맥주집", "펍")
PUB_MENU_WORDS = {
    "막걸리": "menu_makgeolli", "파전": "menu_pajeon", "하이볼": "menu_highball", "생맥주": "menu_draft_beer",
}
MENU_WORDS = {
    **PUB_MENU_WORDS,
    "커피": "menu_coffee", "케이크": "menu_cake", "케잌": "menu_cake", "쿠키": "menu_cookie",
    "도넛": "menu_donut", "도너츠": "menu_donut", "빙수": "menu_bingsu", "국물": "menu_soup",
}
BUDGET_TEXT = re.compile(
    r"(?<![\d,.])(?P<number>\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:(?:\.(?P<fraction>\d{1,4}))?\s*(?P<unit>만|천)(?:\s*(?P<thousands>[1-9])\s*천)?\s*원?|\s*원)"
    r"\s*(?P<qualifier>대|미만|이하|이내|이상|부터|까지|아래|안쪽)?"
)


class BudgetBand(BaseModel):
    """순위에 참고하는 1인 예산 범위. 한쪽 경계만 지정할 수 있습니다."""

    model_config = ConfigDict(extra="forbid")
    min_krw: Price | None = None
    max_krw: Price | None = None

    @model_validator(mode="after")
    def check_bounds(self):
        if self.min_krw is None and self.max_krw is None:
            raise ValueError("1인 예산의 경계를 하나 이상 지정해 주세요.")
        if self.min_krw is not None and self.max_krw is not None and self.min_krw > self.max_krw:
            raise ValueError("1인 최소 예산은 최대 예산 이하여야 합니다.")
        return self

    def distance(self, price):
        return max(0, (self.min_krw or price) - price, price - (self.max_krw or price))


def budget_amount(match):
    digits, fraction, unit, thousands, _ = match.groups()
    factor = {"만": 10_000, "천": 1_000, None: 1}[unit]
    amount = int(digits.replace(",", "")) * factor
    if thousands:
        if unit != "만" or fraction:
            raise ValueError("1인 예산을 원 단위 금액으로 입력해 주세요.")
        amount += int(thousands) * 1_000
    if fraction:
        fractional_won, remainder = divmod(int(fraction) * factor, 10 ** len(fraction))
        if remainder:
            raise ValueError("1인 예산을 원 단위 금액으로 입력해 주세요.")
        amount += fractional_won
    return amount, factor


def budget_from_text(query):
    """1인 예산 범위와 필수 조건 여부를 반환합니다."""
    phrase = unicodedata.normalize("NFKC", query)
    matches = list(BUDGET_TEXT.finditer(phrase))
    if not matches:
        return None, False
    first = matches[0]
    amount, factor = budget_amount(first)
    qualifier = first["qualifier"]
    if len(matches) > 1:
        last = matches[-1]
        connector = phrase[first.end():last.start()].strip()
        if len(matches) == 2 and (
            (qualifier is None and connector in {"~", "-", "–", "에서"})
            or (qualifier == "부터" and not connector and last["qualifier"] == "까지")
        ):
            if last["qualifier"] in (None, "까지"):
                return BudgetBand(min_krw=amount, max_krw=budget_amount(last)[0]), True
        raise ValueError("1인 예산 조건을 하나의 범위로 입력해 주세요.")
    if qualifier in ("미만", "이하", "이내", "까지", "아래", "안쪽"):
        return BudgetBand(max_krw=amount - (qualifier in ("미만", "아래"))), True
    if qualifier in ("이상", "부터"):
        return BudgetBand(min_krw=amount), True
    if qualifier == "대":
        return BudgetBand(min_krw=amount, max_krw=amount + max(1000, factor) - 1), True
    return BudgetBand(min_krw=amount, max_krw=amount), False


def query_tags(query, mood_labels):
    phrase = normalize(query)
    if any(word in phrase for word in ("말고", "빼고", "제외")):
        raise ValueError("원하는 업종과 분위기를 직접 입력해 주세요.")
    wants_cafe = "카페" in phrase or "카공" in phrase
    wants_pub = any(word in phrase for word in PUB_WORDS)
    if wants_cafe and wants_pub:
        raise ValueError("카페와 술집 중 원하는 업종 하나를 입력해 주세요.")

    menu_tags = list(dict.fromkeys(tag for word, tag in MENU_WORDS.items() if word in phrase))
    category = "CAFE" if wants_cafe else "PUB" if wants_pub or any(word in phrase for word in PUB_MENU_WORDS) else None
    moods = {tag for label, tag in mood_labels.items() if label in phrase}
    if any(word in phrase for word in CALM_WORDS):
        moods.add("mood_calm")
    return category, moods, menu_tags
