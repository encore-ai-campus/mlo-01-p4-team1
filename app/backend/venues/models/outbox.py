from django.db import models


class VenueOutbox(models.Model):
    """트랜잭션 보장을 위한 매장 도메인 이벤트 Outbox (Phase 2 모니터링/Kafka 릴레이 대상)."""

    STATE_CHOICES = [
        ('PENDING', '발행 대기'),
        ('SENT', '발행 완료'),
        ('FAILED', '발행 실패'),
    ]

    id = models.BigAutoField(primary_key=True)
    event_id = models.CharField(max_length=36, unique=True, db_index=True)
    event_type = models.CharField(max_length=64, db_index=True)
    venue_id = models.CharField(max_length=64, db_index=True)
    actor_id = models.CharField(max_length=64, blank=True, default='')
    request_id = models.CharField(max_length=64, blank=True, default='')
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=12, choices=STATE_CHOICES, default='PENDING', db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=120, blank=True, default='')
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'venue_outbox'
        verbose_name = '매장 이벤트 Outbox'
        verbose_name_plural = '매장 이벤트 Outbox 목록'
        ordering = ['occurred_at']

    def __str__(self):
        return f"[{self.state}] {self.event_type} ({self.venue_id}) {self.event_id}"
