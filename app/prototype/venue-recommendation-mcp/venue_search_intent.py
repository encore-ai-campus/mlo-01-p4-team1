"""공통 태그로 해석할 수 있는 손님 검색 조건."""

import re
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

from verify_tag_weather import normalize


VenueCategory = Literal["CAFE", "PUB"]
Price = Annotated[StrictInt, Field(ge=1, le=1_000_000)]
CALM_WORDS = ("잔잔한", "잔잔함", "조용한", "조용함", "차분한", "차분함")
PUB_WORDS = ("술집", "주점", "이자카야", "포차", "와인바", "칵테일바", "맥주집", "펍")
CRAVING_WORDS = ("땡기", "땡긴", "땡겨", "당기", "당긴", "당겨", "마시고싶", "먹고싶", "한잔")
SHORT_MENU_WORDS = {"전": "menu_jeon", "빵": "menu_bread", "떡": "menu_rice_cake"}
SHORT_MENU = re.compile(r"(?<!\w)(전|빵|떡)(?=이?랑|하고|[이가을를만도은는]?\s*(?:땡|당|먹고|맛집|있는|파는))")
KOREAN_DIGITS = {char: str(number) for number, char in enumerate("일이삼사오육칠팔구", start=1)}
WRITTEN_WON = re.compile(
    r"(?<!\w)(?=[일이삼사오육칠팔구만천])"
    r"(?:(?P<man>[일이삼사오육칠팔구]?)만\s*)?(?:(?P<cheon>[일이삼사오육칠팔구]?)천\s*)?원"
)
BUDGET_TEXT = re.compile(
    r"(?<![\d,.])(?P<number>\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:(?:\.(?P<fraction>\d{1,4}))?\s*(?P<unit>만|천)"
    r"(?:\s*(?P<thousands>[1-9일이삼사오육칠팔구])\s*천)?\s*원?|\s*원)"
    r"\s*(?P<qualifier>대|미만|이하|이내|이상|부터|까지|아래|안쪽)?"
)
MAX_BUDGET_SUFFIX = re.compile(
    r"\s*(?:을|를)?\s*(?:(?:넘지|초과하지)\s*않(?:는|게|도록)|(?:넘으면|초과하면)\s*안\s*(?:돼|되))"
)
APPROXIMATE_BUDGET = re.compile(r"(?:원|만|천)\s*(?:정도|전후|쯤)")
SOCIAL_PURPOSE = re.compile(
    r"헌팅(?:\s*(?:술집|포차|가능|되는|하기|할\s*수))?|"
    r"(?:소개\s*팅|합석)\s*(?:가능|되는|할\s*수|하기\s*좋|장소|술집|포차)|"
    r"(?:처음\s*(?:보는|만나는)|낯선|새로운)\s*사람(?:들)?(?:이랑|과|하고|을)?"
    r"\s*(?:어울리|만나|만날|대화하|친해지|술\s*마시)(?:기|고)?"
)
SOCIAL_CONTEXT = re.compile(r"\s*(?:은|는|이|가|을|를)?\s*(?:말고|빼고|아닌|제외|전에)")
OPTIONAL_MOOD = re.compile(r"가능하면|되도록|가급적|이면(?:더)?좋|선호|아니어도")
PER_PERSON = re.compile(r"(?<!\d)(?:인\s*당|1\s*인(?![\d분석용])|한\s*사람\s*당|일인당)")
EXCLUSION = re.compile(r"말고|빼고|제외|아닌|(?:조용|차분|잔잔|아늑)하지\s*않(?:은|는)")
MENU_ABSENT = re.compile(r"(?:이|가|은|는)?(?:없(?:는|이|고|으면)|안파는|안먹)")
MENU_ALTERNATIVES = {"또는", "혹은", "이나", "나", "아니면", "/"}
MENU_JOINERS = {"랑", "이랑", "하고", "와", "과", "및", "그리고", ",", "+", "·", "&"}
ONLY_MENU = re.compile(r"(?:만|한잔만)(?=$|먹|마시|주문|파는|있는|땡|당|하고|할|추천|원해|가능|하려)")
REQUESTED_MENU = re.compile(r"(?:을|를|이|가)?(?:먹|마시|파는|있는|땡|당|카페|술집|한잔|맛집|추천|$)")
VENUE_TARGET = re.compile(
    r"(카페|" + "|".join(PUB_WORDS) + r")(?=$|[ .,!?]|1인|인당|한사람당|"
    r"(?:을|를|로|으로)?(?:좀)?(?:추천|찾|검색|알려|골라|원해)|에(?:가고|갈))"
)


class UnverifiedCondition(BaseModel):
    """원본 조건 중 매장 카탈로그에 확인할 근거가 없는 요구입니다."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["SOCIAL_PURPOSE", "AMENITY", "ACCESSIBILITY", "DIETARY", "OPENING_HOURS", "OTHER"]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


def unverified_venue_conditions(text, declared, menu_words, menu_pattern):
    conditions = {(item.kind, normalize(item.text)): item.text for item in declared or []}
    phrase = unicodedata.normalize("NFKC", text)
    for match in SOCIAL_PURPOSE.finditer(phrase):
        if SOCIAL_CONTEXT.match(phrase, match.end()):
            continue
        key = normalize(match[0])
        known = [item for item in conditions if item[0] == "SOCIAL_PURPOSE"]
        if any(key in item[1] for item in known):
            continue
        for item in known:
            if item[1] in key:
                conditions.pop(item)
        conditions["SOCIAL_PURPOSE", key] = match[0]

    if match := EXCLUSION.search(phrase):
        excerpt = phrase[max(0, match.start() - 24):match.end()].strip()
        conditions["OTHER", normalize(excerpt)] = excerpt
    normalized = normalize(phrase)
    matches = menu_matches(normalized, menu_words, menu_pattern)
    for match in matches:
        if absent := MENU_ABSENT.match(normalized, match.end()):
            excerpt = f"{match[0]} {absent[0]}"
            conditions["OTHER", normalize(excerpt)] = excerpt
    for left, right in zip(matches, matches[1:]):
        if normalized[left.end():right.start()] in MENU_ALTERNATIVES:
            excerpt = normalized[left.start():right.end()]
            conditions["OTHER", excerpt] = excerpt
    return [{"kind": kind, "text": value, "evidence_status": "UNKNOWN"}
            for (kind, _), value in conditions.items()]


def per_person_budget(text):
    """원문에 1인 기준이 분명한 경우에만 해석 과정에서 빠진 예산을 복구합니다."""
    if not PER_PERSON.search(unicodedata.normalize("NFKC", text)):
        return None, False
    return budget_from_text(text)


class BudgetBand(BaseModel):
    """1인 예산 범위입니다. 한쪽 경계만 지정할 수 있습니다."""

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
        amount += int(KOREAN_DIGITS.get(thousands, thousands)) * 1_000
    if fraction:
        fractional_won, remainder = divmod(int(fraction) * factor, 10 ** len(fraction))
        if remainder:
            raise ValueError("1인 예산을 원 단위 금액으로 입력해 주세요.")
        amount += fractional_won
    return amount, factor


def written_won_to_digits(match):
    man, cheon = match["man"], match["cheon"]
    amount = f"{KOREAN_DIGITS.get(man, '1')}만" if man is not None else ""
    if cheon is not None:
        amount += f"{KOREAN_DIGITS.get(cheon, '1')}천"
    return f"{amount}원"


def budget_from_text(query):
    """1인 예산 범위와 필수 조건 여부를 반환합니다."""
    phrase = WRITTEN_WON.sub(written_won_to_digits, unicodedata.normalize("NFKC", query))
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
    if qualifier is None and MAX_BUDGET_SUFFIX.match(phrase, first.end()):
        qualifier = "이하"
    if qualifier in ("미만", "이하", "이내", "까지", "아래", "안쪽"):
        return BudgetBand(max_krw=amount - (qualifier in ("미만", "아래"))), True
    if qualifier in ("이상", "부터"):
        return BudgetBand(min_krw=amount), True
    if qualifier == "대":
        return BudgetBand(min_krw=amount, max_krw=amount + max(1000, factor) - 1), True
    return BudgetBand(min_krw=amount, max_krw=amount), False


def compile_menu_words(labels):
    words = {word: (tag,) for word, tag in labels.items() if len(word) >= 2 and word != tag}
    words.update({
        "치맥": ("menu_beer", "menu_chicken"), "꼬치": ("menu_skewer",),
        "라떼": ("menu_cafe_latte",), "달달한거": ("menu_dessert",), "달달한것": ("menu_dessert",),
    })
    # 업종 이름 안의 와인·맥주·칵테일을 별도 주문으로 해석하지 않습니다.
    words.update({word: () for word in PUB_WORDS})
    pattern = re.compile("|".join(re.escape(word) for word in sorted(words, key=len, reverse=True)))
    return words, pattern


def menu_matches(phrase, menu_words, menu_pattern):
    return [match for match in menu_pattern.finditer(phrase) if menu_words[match[0]]]


def venue_categories(phrase, menu_words, menu_pattern, *, explicit_target=False):
    category_text = menu_pattern.sub(lambda match: " " if menu_words[match[0]] else match[0], phrase)
    if explicit_target:
        return {"CAFE" if word == "카페" else "PUB" for word in VENUE_TARGET.findall(category_text)}
    return {
        category for category, mentioned in (
            ("CAFE", "카페" in category_text or "카공" in category_text),
            ("PUB", any(word in category_text for word in PUB_WORDS)),
        ) if mentioned
    }


def required_mood_tags(phrase, mood_labels, *, honor_preference=False):
    if honor_preference and OPTIONAL_MOOD.search(phrase):
        return set()
    return {tag for label, tag in mood_labels.items() if label in phrase}


def explicit_menu_tags(phrase, menu_words, menu_pattern):
    """구조화 인수에서 빠지면 안 되는 명확한 메뉴와 단품 요청을 찾습니다."""
    matches = menu_matches(phrase, menu_words, menu_pattern)
    tags = {}
    only = False
    for match in matches:
        values = menu_words[match[0]]
        if ONLY_MENU.match(phrase, match.end()):
            only = True
            tags.update(dict.fromkeys(values))
        elif len(values) > 1 or (len(matches) == 1 and REQUESTED_MENU.match(phrase, match.end())):
            tags.update(dict.fromkeys(values))
    for left, right in zip(matches, matches[1:]):
        if phrase[left.end():right.start()] in MENU_JOINERS:
            tags.update(dict.fromkeys(menu_words[left[0]] + menu_words[right[0]]))
    return list(tags), only


def query_tags(query, mood_labels, menu_words, menu_pattern, ancestors):
    phrase = normalize(query)
    matched_words = [match[0] for match in menu_matches(phrase, menu_words, menu_pattern)]
    menu_tags = {tag: None for word in matched_words for tag in menu_words[word]}
    for word, tag in SHORT_MENU_WORDS.items():
        # 한 글자인 전·빵·떡은 메뉴 문맥이 있는 경우에만 읽습니다.
        if phrase == word or re.search(rf"(?:랑|하고)\s+{word}\s*$", query):
            menu_tags.setdefault(tag)
    for word in SHORT_MENU.findall(query):
        menu_tags.setdefault(SHORT_MENU_WORDS[word])

    categories = venue_categories(phrase, menu_words, menu_pattern)
    if len(categories) > 1:
        raise ValueError("카페와 술집 중 원하는 업종 하나를 입력해 주세요.")

    has_alcohol = any("menu_alcohol" in ancestors[tag] for tag in menu_tags)
    category = next(iter(categories), "PUB" if has_alcohol else None)
    moods = required_mood_tags(phrase, mood_labels)
    allow_pairing = (
        any(word in phrase for word in CRAVING_WORDS)
        and not any(word + "만" in phrase or word + "한잔만" in phrase for word in matched_words)
    )
    return category, moods, list(menu_tags), allow_pairing
