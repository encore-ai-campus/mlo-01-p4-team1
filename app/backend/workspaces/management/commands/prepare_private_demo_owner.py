"""Read consent and a password from the person operating the synthetic account."""
from getpass import GetPassWarning, getpass
import sys
import warnings

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backend.accounts import crypto
from backend.recommendation.errors import DomainError
from backend.workspaces import private_demo


class Command(BaseCommand):
    help = "Prepare one synthetic Phase 4 owner and submit the claim for separate passkey review."

    def handle(self, *args, **options):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise CommandError("A private interactive terminal is required")
        try:
            private_demo.require_demo_runtime()
            crypto.key("MEMBER_LOOKUP_KEY")
            crypto.seal({})
            documents = settings.PHASE4_PRIVATE_DEMO_POLICY_DOCUMENTS
            self.stdout.write(f"시연 점주 계정 {private_demo.DEMO_USERNAME} / {private_demo.POLICY_VERSION}")
            self.stdout.write(documents["terms"])
            if input("이용약관에 동의하면 동의라고 입력해 주세요. ").strip() != "동의":
                raise CommandError("이용약관 동의가 필요합니다.")
            self.stdout.write(documents["privacy_notice"])
            if input("기본정보 처리 안내를 읽었다면 확인이라고 입력해 주세요. ").strip() != "확인":
                raise CommandError("기본정보 처리 안내 확인이 필요합니다.")
            if input("만 14세 이상이면 확인이라고 입력해 주세요. ").strip() != "확인":
                raise CommandError("연령 확인이 필요합니다.")
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                password = getpass("시연 점주 비밀번호: ")
                confirmation = getpass("비밀번호 확인: ")
            claim = private_demo.prepare_claim(
                password, confirmation, terms_accepted=True, privacy_acknowledged=True, age_confirmed=True,
            )
        except GetPassWarning:
            raise CommandError("A secure password prompt is required") from None
        except DomainError as error:
            raise CommandError(error.code) from None
        self.stdout.write(f"신청 {claim['id']} / {claim['state']}")
        self.stdout.write(f"심사 화면 /platform/review?claim={claim['id']}")
