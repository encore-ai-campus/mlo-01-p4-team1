import datetime
from django.db import models
from django.utils import timezone


class VenueEvent(models.Model):
    """점주 실시간 타임 이벤트 모델 (PA 기획안 반영)"""
    event_id = models.BigAutoField(primary_key=True)
    venue_id = models.CharField(max_length=64, db_index=True, help_text="매장 고유 식별자")
    owner_id = models.CharField(max_length=64, db_index=True, help_text="이벤트를 등록한 점주 식별자")
    title = models.CharField(max_length=200, help_text="이벤트 제목 (예: 비 오는 날 한정 파전 1+1)")
    description = models.TextField(blank=True, help_text="이벤트 상세 안내")
    badge_text = models.CharField(max_length=100, default='오늘의 이벤트', help_text="배지 문구")
    discount_info = models.CharField(max_length=100, blank=True, help_text="할인 또는 혜택 요약")
    total_quantity = models.PositiveIntegerField(default=10, help_text="총 이벤트 혜택 제공 수량")
    remaining_quantity = models.PositiveIntegerField(default=10, help_text="남은 실시간 재고 (QR 인증 시 -1 차감)")
    start_at = models.DateTimeField(help_text="이벤트 시작 일시")
    end_at = models.DateTimeField(help_text="이벤트 종료 일시")
    is_active = models.BooleanField(default=True, help_text="점주 활성화 상태")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'venue_event'
        verbose_name = '점주 이벤트'
        verbose_name_plural = '점주 이벤트 목록'
        indexes = [
            models.Index(fields=['venue_id', 'is_active', 'end_at']),
        ]

    def __str__(self):
        return f"[{self.venue_id}] {self.title} (남은수량: {self.remaining_quantity}/{self.total_quantity})"

    @property
    def is_ended(self) -> bool:
        """종료 시각 경과 여부"""
        return timezone.now() > self.end_at

    @property
    def is_visible_on_board(self) -> bool:
        """
        탐색 보드 노출 여부 (PA 기획안):
        종료 후 5분 동안은 '이벤트가 종료되었어요' 배지를 달고 노출을 유지하며, 5분 경과 후 자동 숨김.
        """
        if not self.is_active:
            return False
        grace_period = datetime.timedelta(minutes=5)
        return timezone.now() <= (self.end_at + grace_period)

    @property
    def intended_visitors_count(self) -> int:
        """[참여할게요] 클릭으로 현재 방문 예정인 유저 수"""
        return self.participations.filter(status='INTENDED').count()


class VenueEventParticipation(models.Model):
    """유저의 이벤트 참여(방문 예정 의사표시) 및 현장 QR 인증 이력"""
    STATUS_CHOICES = [
        ('INTENDED', '방문 예정 ([참여할게요])'),
        ('VERIFIED', '현장 QR 인증 완료'),
        ('CANCELLED', '참여 취소'),
    ]

    participation_id = models.BigAutoField(primary_key=True)
    event = models.ForeignKey(
        VenueEvent,
        on_delete=models.CASCADE,
        related_name='participations',
        help_text="대상 이벤트"
    )
    user_id = models.CharField(max_length=64, db_index=True, help_text="참여 유저 또는 게스트 식별자")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='INTENDED',
        db_index=True,
        help_text="참여 상태"
    )
    qr_auth_code = models.CharField(max_length=64, blank=True, help_text="생성된 QR 인증 코드")
    participation_date = models.DateField(auto_now_add=True, help_text="참여 일자 (1일 1회 제약용)")
    verified_at = models.DateTimeField(null=True, blank=True, help_text="현장 QR 인증 완료 시각")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'venue_event_participation'
        verbose_name = '이벤트 참여 및 인증'
        verbose_name_plural = '이벤트 참여 및 인증 목록'
        constraints = [
            # PA 기획안: 계정당 동일 이벤트 1일 1회 제한
            models.UniqueConstraint(
                fields=['event', 'user_id', 'participation_date'],
                name='unique_event_participation_per_user_per_day'
            )
        ]

    def __str__(self):
        return f"{self.user_id} - Event #{self.event_id} ({self.get_status_display()})"


class OwnerEventTrustScore(models.Model):
    """점주 이벤트 신뢰도 점수 (허위 이벤트 방지 및 가중치 반영)"""
    owner_id = models.CharField(max_length=64, primary_key=True, help_text="점주 식별자")
    venue_id = models.CharField(max_length=64, db_index=True, help_text="대표 매장 식별자")
    trust_score = models.FloatField(default=100.0, help_text="신뢰도 점수 (기본 100점)")
    total_events_hosted = models.PositiveIntegerField(default=0, help_text="누적 주최 이벤트 수")
    successful_verifications = models.PositiveIntegerField(default=0, help_text="정상 QR 인증 완료 건수")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'owner_event_trust_score'
        verbose_name = '점주 이벤트 신뢰도 점수'
        verbose_name_plural = '점주 이벤트 신뢰도 점수 목록'

    def __str__(self):
        return f"{self.owner_id} ({self.venue_id}) - 신뢰도: {self.trust_score:.1f}점"


class UserNotification(models.Model):
    """유저 실시간 알림 (QR 인증 완료, 관심 매장 이벤트 발생 등)"""
    notification_id = models.BigAutoField(primary_key=True)
    user_id = models.CharField(max_length=64, db_index=True, help_text="수신 대상 유저 또는 게스트 식별자")
    title = models.CharField(max_length=200, help_text="알림 제목")
    message = models.TextField(help_text="알림 내용")
    link_url = models.CharField(max_length=255, blank=True, help_text="이동 링크 URL")
    is_read = models.BooleanField(default=False, help_text="읽음 확인 여부")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'venue_user_notification'
        verbose_name = '유저 알림'
        verbose_name_plural = '유저 알림 목록'
        ordering = ['-created_at']

    def __str__(self):
        return f"[{'읽음' if self.is_read else '안읽음'}] {self.user_id}: {self.title}"
