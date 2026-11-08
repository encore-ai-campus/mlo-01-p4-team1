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
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'venue_status_overlay'
        verbose_name = '매장 상태 오버레이'
        verbose_name_plural = '매장 상태 오버레이 목록'

    def __str__(self):
        return f"{self.venue_id}: {self.get_status_display()} (제보: {self.closure_report_count}건)"
