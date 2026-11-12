"""Visit purposes that can be checked against sourced venue research."""

from dataclasses import dataclass
from enum import StrEnum
import re
import unicodedata

from verify_tag_weather import normalize


class VisitPurpose(StrEnum):
    SOLO_DRINK = "visit_solo_drink"
    SOLO_CAFE = "visit_solo_cafe"
    GROUP = "visit_group"
    DATE = "visit_date"
    SECOND_ROUND = "visit_second_round"


@dataclass(frozen=True)
class PurposeDefinition:
    label: str
    aliases: tuple[str, ...]
    evidence: str
    category: str | None = None
    request_pattern: str | None = None


ROUND_NUMBER = r"(?<![가-힣\d])(?:[23]|이|삼)\s*차"
LIGHT_DRINK = (
    r"(?:가볍게|간단히)\s*(?:(?:술|맥주|하이볼|칵테일|와인)(?:을|를)?\s*)?(?:한\s*잔|마시|먹을)"
)
AFTER_MEAL = (
    r"(?:식사|밥|저녁)(?:를|을)?\s*(?:후(?:에)?|먹고)\s*(?:가볍게\s*)?"
    r"(?:술|한\s*잔|맥주|하이볼|커피|차(?:를|\s*한\s*잔))"
)


VISIT_PURPOSES = {
    VisitPurpose.SOLO_DRINK: PurposeDefinition(
        "혼술", ("혼자 한 잔", "혼자 술", "혼자 마시"),
        r"혼술\s*(?:하기\s*(?:좋|편|괜찮)|도\s*좋|가능|할\s*만|추천|키워드|태그|방문)|"
        r"혼자(?:서)?\s*(?:술을?\s*)?(?:한\s*잔|마시|마실)",
        "PUB",
    ),
    VisitPurpose.SOLO_CAFE: PurposeDefinition(
        "혼카페", ("혼커", "혼자 카페", "혼자 커피"),
        r"혼\s*(?:카페|커)|혼자(?:서)?\s*(?:카페|커피)",
        "CAFE",
    ),
    VisitPurpose.GROUP: PurposeDefinition(
        "단체 모임", ("단체석", "단체 예약", "회식", "여럿이 모이"),
        r"단체\s*(?:모임|석|좌석|예약)|회식\s*(?:하기\s*좋|에\s*좋)",
    ),
    VisitPurpose.DATE: PurposeDefinition(
        "데이트", ("데이트 코스", "둘이 데이트"),
        r"데이트\s*(?:하기\s*(?:좋|편)|코스|(?:장소로|에)\s*(?:좋|추천))",
    ),
    VisitPurpose.SECOND_ROUND: PurposeDefinition(
        "2차", ("3차", "이차", "삼차", "가볍게 한 잔", "밥 먹고 한 잔", "한 잔 더"),
        rf"{ROUND_NUMBER}\s*(?:로|에)\s*(?:방문|이용|찾|들르|들렀|가기|갔|적당|좋|한\s*잔)|"
        rf"{LIGHT_DRINK}|{AFTER_MEAL}",
        request_pattern=(
            rf"(?:{ROUND_NUMBER}|(?<=에서)[23]\s*차)(?=$|[\s,.!?]|로|에|는|도|만|갈|가|할|술집|카페|추천|ᄎᄎ)|"
            rf"{LIGHT_DRINK}|{AFTER_MEAL}|한\s*잔\s*더"
        ),
    ),
}

# These already have public mood IDs. For a CSV preview, template mood values
# alone do not establish that a cafe supports work or conversation.
RESEARCHED_MOODS = {
    "mood_work_friendly": r"카공|(?:작업|공부)(?:하기)?\s*(?:좋|편)",
    "mood_conversation": r"(?:(?:대화|이야기)(?:하기|를\s*나누기)?|담소(?:를)?\s*나누기)\s*(?:좋|편)",
}
PURPOSE_PHRASES = {
    purpose: re.compile(definition.request_pattern or "|".join(
        r"\s*".join(map(re.escape, normalize(alias))) for alias in (definition.label, *definition.aliases)
    ))
    for purpose, definition in VISIT_PURPOSES.items()
}
BACKGROUND = re.compile(r"\s*(?:하기\s*)?(?:(?:전|후)(?:\s|에|까지|$)|끝나고)")
SOLO_VISIT = re.compile(r"혼자(?:서)?\s*(?:가기\s*(?:좋|편)|가도|갈\s*만|방문(?:하기\s*(?:좋|편)|해도))")
UNCERTAIN_OR_NEGATIVE = re.compile(
    r"불가|불편|어렵|어려|비추천|금지|미확인|없(?:음|다|는|었|고|지만)|"
    r"(?:좋|편하|가능하|적합하)(?:지|진)\s*않|별로|"
    r"확인(?:이)?\s*(?:안|되지\s*않|못|필요)|확인하지\s*못"
)
CLAUSE = re.compile(r"[.!?,;|。\n]+")
SOURCE_URL = re.compile(r"https?://[^/\s]+", re.IGNORECASE)
SECOND_ROUND_MENU_TYPES = {
    "PUB": (
        {"menu_beer", "menu_cocktail", "menu_wine"},
        {"menu_fries", "menu_nachos", "menu_cheese_plate", "menu_charcuterie", "menu_skewer"},
    ),
    "CAFE": (
        {"menu_coffee", "menu_tea"},
        {"menu_dessert", "menu_bread"},
    ),
}


def requested_visit_purposes(text, venue_category):
    phrase = unicodedata.normalize("NFKC", text)
    purposes = {
        purpose for purpose, pattern in PURPOSE_PHRASES.items()
        if any(not BACKGROUND.match(phrase, match.end()) for match in pattern.finditer(phrase))
    }
    if SOLO_VISIT.search(phrase):
        if venue_category == "PUB":
            purposes.add(VisitPurpose.SOLO_DRINK)
        elif venue_category == "CAFE":
            purposes.add(VisitPurpose.SOLO_CAFE)
    return purposes


def reviewed_venue_tags(venue_category, research):
    if research is None or not any(SOURCE_URL.match(url) for url in research.sources):
        return set(), set()
    text = unicodedata.normalize("NFKC", f"{research.description}\n{research.seating}")
    supported = [clause for clause in CLAUSE.split(text) if not UNCERTAIN_OR_NEGATIVE.search(clause)]
    purposes = {
        purpose for purpose, definition in VISIT_PURPOSES.items()
        if definition.category in (None, venue_category)
        and any(re.search(definition.evidence, clause) for clause in supported)
    }
    moods = {
        tag_id for tag_id, pattern in RESEARCHED_MOODS.items()
        if (venue_category == "CAFE" or tag_id != "mood_work_friendly")
        and any(re.search(pattern, clause) for clause in supported)
    }
    return purposes, moods


def second_round_menu_names(venue, menus, expanded_types):
    """Use reviewed menu items or menu items with their own source links."""
    if venue.get("venue_category") not in SECOND_ROUND_MENU_TYPES:
        return ()
    drinks, snacks = SECOND_ROUND_MENU_TYPES[venue["venue_category"]]
    review_urls = venue.get("research", {}).get("sources", ())
    has_review = any(SOURCE_URL.match(url) for url in review_urls)
    drink_menus, snack_menus = [], []
    for menu in menus:
        menu_id = menu["menu_id"]
        has_menu_source = any(SOURCE_URL.match(url) for url in menu.get("evidence", {}).get("urls", ()))
        if not (has_menu_source or (has_review and menu_id.startswith("research-"))):
            continue
        types = expanded_types[menu_id]
        if types & drinks:
            drink_menus.append(menu)
        if types & snacks:
            snack_menus.append(menu)
    return next((
        (drink["name"], snack["name"]) for drink in drink_menus for snack in snack_menus
        if drink["menu_id"] != snack["menu_id"]
    ), ())
