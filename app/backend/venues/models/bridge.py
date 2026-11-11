import uuid
from django.db import models


class VenueOwnerBridge(models.Model):
    """점주 Track 1 계정과 매장 venue_id 연결 및 점주 직접 등록 매장 모델"""
    bridge_id = models.BigAutoField(primary_key=True)
    owner_id = models.CharField(max_length=64, db_index=True, help_text="Track 1 점주 계정 ID")
    venue_id = models.CharField(max_length=64, unique=True, db_index=True, help_text="매장 고유 식별자")
    venue_name = models.CharField(max_length=200, help_text="매장명")
    gu_name = models.CharField(max_length=50, db_index=True, help_text="서울시 자치구 명칭 (예: 강남구)")
    road_address = models.CharField(max_length=255, blank=True, help_text="도로명 주소")
    category = models.CharField(
        max_length=20,
        choices=[('CAFE', '카페'), ('PUB', '주점/펍')],
        default='CAFE',
        help_text="업종 카테고리"
    )
    is_verified = models.BooleanField(default=False, help_text="점주 소유권 인증 여부")
    track1_partner_music_id = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        help_text="Track 1 파트너 음악 플레이리스트/트랙 ID"
    )
    business_number = models.CharField(max_length=30, blank=True, help_text="사업자 등록 번호")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'venue_owner_bridge'
        verbose_name = '점주 매장 브릿지'
        verbose_name_plural = '점주 매장 브릿지 목록'

    def __str__(self):
        return f"[{self.gu_name}] {self.venue_name} ({self.venue_id}) - {self.owner_id}"
