import re
import string
import unicodedata
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from backend.recommendation.errors import DomainError, require


def username(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_]{4,20}', value),
            'INVALID_USERNAME', '아이디는 영문·숫자·밑줄 4~20자로 입력해 주세요.')
    return value.lower()


def nickname(value):
    require(isinstance(value, str), 'INVALID_NICKNAME', '별명을 확인해 주세요.')
    value = unicodedata.normalize('NFKC', value)
    require(re.fullmatch(r'[A-Za-z0-9\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7a3\ud7b0-\ud7ff]{2,10}', value),
            'INVALID_NICKNAME', '별명은 공백 없이 한글·영문·숫자 2~10자로 입력해 주세요.')
    return value, value.casefold()


def email(value):
    require(isinstance(value, str) and len(value) <= 254 and value == value.strip(),
            'INVALID_EMAIL', '이메일 주소를 확인해 주세요.')
    try:
        validate_email(value)
        local, domain = value.rsplit('@', 1)
        # Preserve the local part; never collapse provider-specific dots/aliases.
        return local + '@' + domain.encode('idna').decode().lower()
    except (ValidationError, ValueError, UnicodeError):
        raise DomainError('INVALID_EMAIL', '이메일 주소를 확인해 주세요.') from None


def password(value, confirmation, user):
    require(isinstance(value, str) and 8 <= len(value) <= 20
            and all('!' <= c <= '~' for c in value)
            and any('A' <= c <= 'Z' for c in value)
            and sum(c in string.punctuation for c in value) >= 2,
            'INVALID_PASSWORD', '비밀번호는 공백 없이 영문·숫자·특수문자 8~20자, 대문자 1개 이상·특수문자 2개 이상으로 입력해 주세요.')
    require(value == confirmation, 'PASSWORD_MISMATCH', '비밀번호 확인이 일치하지 않습니다.')
    try:
        validate_password(value, user)
    except ValidationError:
        raise DomainError('WEAK_PASSWORD', '흔하거나 계정 정보와 비슷한 비밀번호는 사용할 수 없습니다.') from None


def documents():
    return {'version': settings.MEMBER_POLICY_VERSION, 'ready': settings.MEMBER_LOCAL_PREVIEW or settings.MEMBER_POLICIES_READY,
            'local_preview': settings.MEMBER_LOCAL_PREVIEW, 'documents': settings.MEMBER_POLICY_DOCUMENTS}


def consent(body):
    require(settings.MEMBER_LOCAL_PREVIEW or settings.MEMBER_POLICIES_READY,
            'POLICY_NOT_READY', '회원 정책 게시를 준비하고 있습니다.', 503)
    require(settings.MEMBER_POLICY_VERSION and all(isinstance(settings.MEMBER_POLICY_DOCUMENTS.get(k), str)
            and settings.MEMBER_POLICY_DOCUMENTS[k].strip() for k in ('terms', 'privacy_notice', 'email')),
            'POLICY_NOT_READY', '회원 정책 게시를 준비하고 있습니다.', 503)
    require(body.get('policy_version') == settings.MEMBER_POLICY_VERSION,
            'POLICY_CHANGED', '최신 약관과 안내를 다시 확인해 주세요.', 409)
    require(all(body.get(name) is True for name in ('terms_accepted', 'privacy_acknowledged', 'age_confirmed')),
            'AGREEMENT_REQUIRED', '이용약관 동의·처리 안내 확인·만 14세 이상 확인이 필요합니다.')
    require(type(body.get('email_consent', False)) is bool)
