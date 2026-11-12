from django.conf import settings
from django.db import models


class VenueStatusOverlay(models.Model):
    """정적 CSV 매장 데이터 위에 덮어씌우는 동적 영업 상태 오버레이 (3단계 상태)"""
    STATUS_CHOICES = [
        ('OPEN', '정상 영업'),
        ('CLOSURE_REPORTED', '폐업 제보 접수 (확인 중)'),
        ('CLOSED_HIDDEN', '폐업 확정 (검색 노출 제외)'),
    ]

    venue_id = models.CharField(max_length=64, primary_key=True, help_text="매장 고유 식별자")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='OPEN',
        db_index=True,
        help_text="3단계 영업 상태"
    )
    closure_report_count = models.PositiveIntegerField(
        default=0,
        help_text="누적 폐업 제보 건수"
    )
    notes = models.TextField(blank=True, help_text="관리자 확인 메모 및 제보 사유 요약")
    last_reported_at = models.DateTimeField(null=True, blank=True, help_text="최근 폐업 제보 시각")
    version = models.PositiveIntegerField(default=1, help_text="낙관적 동시성 제어용 revision")
    last_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_venue_closures',
        help_text="최종 검수 관리자"
    )
    last_reviewed_at = models.DateTimeField(null=True, blank=True, help_text="최종 관리자 검수 시각")
    decision_reason_code = models.CharField(max_length=48, blank=True, default='', help_text="최종 관리자 판정 사유 코드")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'venue_status_overlay'
        verbose_name = '매장 상태 오버레이'
        verbose_name_plural = '매장 상태 오버레이 목록'

    def __str__(self):
        return f"{self.venue_id}: {self.get_status_display()} (제보: {self.closure_report_count}건)"


class VenueClosureDecision(models.Model):
    """폐업 제보 관리자 판정 불변 감사 이력"""
    decision_id = models.BigAutoField(primary_key=True)
    overlay = models.ForeignKey(
        VenueStatusOverlay,
        on_delete=models.CASCADE,
        related_name='decisions',
        help_text="대상 매장 상태 오버레이"
    )
    overlay_version = models.PositiveIntegerField(help_text="판정 적용 시점의 오버레이 version")
    decision = models.CharField(max_length=20, help_text="적용된 상태 (OPEN / CLOSED_HIDDEN / CLOSURE_REPORTED)")
    reason_code = models.CharField(max_length=48, default='ADMIN_REVIEW', help_text="판정 사유 코드")
    notes = models.TextField(blank=True, default='', help_text="판정 메모")
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='venue_closure_decisions',
        help_text="판정 수행 관리자"
    )
    request_id = models.CharField(max_length=64, blank=True, default='', help_text="요청 추적 식별자")
    decided_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'venue_closure_decision'
        verbose_name = '폐업 검수 판정 이력'
        verbose_name_plural = '폐업 검수 판정 이력 목록'
        ordering = ['-decided_at']

    def __str__(self):
        return f"{self.overlay_id} v{self.overlay_version} -> {self.decision} ({self.decided_at})"
