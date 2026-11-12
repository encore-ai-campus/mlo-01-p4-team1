from django.db import models


class VenueInteraction(models.Model):
    """유저 상호작용 이력 (찜하기, 날씨 스냅샷 한줄평, 폐업 제보, 상세 조회)"""
    TYPE_CHOICES = [
        ('LIKE', '찜하기'),
        ('REVIEW', '날씨 스냅샷 한줄평'),
        ('REPORT_CLOSURE', '폐업 제보'),
        ('REPORT_FALSE_EVENT', '허위 이벤트 신고'),
        ('VIEW', '상세 조회'),
    ]

    interaction_id = models.BigAutoField(primary_key=True)
    user_id = models.CharField(max_length=64, db_index=True, help_text="게스트 또는 로그인 유저 식별자")
    venue_id = models.CharField(max_length=64, db_index=True, help_text="대상 매장 고유 식별자")
    interaction_type = models.CharField(
        max_length=20,
        choices=TYPE_CHOICES,
        db_index=True,
        help_text="상호작용 유형"
    )
    weather_condition = models.CharField(
        max_length=20,
        blank=True,
        help_text="상호작용 당시 실시간 날씨 (CLEAR, RAIN, SNOW, CLOUDY 등)"
    )
    weather_temp = models.FloatField(null=True, blank=True, help_text="상호작용 당시 기온(℃)")
    content = models.TextField(blank=True, help_text="리뷰 본문 또는 제보 사유")
    rating = models.PositiveSmallIntegerField(null=True, blank=True, help_text="별점 (1~5점)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'venue_interaction'
        verbose_name = '매장 상호작용'
        verbose_name_plural = '매장 상호작용 목록'
        indexes = [
            models.Index(fields=['venue_id', 'interaction_type']),
            models.Index(fields=['user_id', 'interaction_type']),
        ]

    def __str__(self):
        return f"[{self.get_interaction_type_display()}] {self.user_id} -> {self.venue_id} ({self.created_at})"
