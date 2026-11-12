from django.db import models


class VenueCatalogRecord(models.Model):
    """DB-backed snapshot of normalized cafe/pub venue and menu catalog rows."""

    venue_id = models.CharField(max_length=64, primary_key=True, help_text="매장 고유 식별자")
    dataset_version = models.CharField(max_length=64, default='v1', db_index=True, help_text="동기화된 카탈로그 버전")
    venue_name = models.CharField(max_length=200, help_text="매장명")
    gu_name = models.CharField(max_length=50, db_index=True, help_text="시·군·구 명칭")
    dong_name = models.CharField(max_length=80, blank=True, default='', help_text="읍·면·동 명칭")
    road_address = models.CharField(max_length=255, blank=True, default='', help_text="도로명 주소")
    category = models.CharField(max_length=20, default='CAFE', db_index=True, help_text="업종 카테고리")
    sub_category = models.CharField(max_length=100, blank=True, default='', help_text="세부 업종")
    store_type = models.CharField(max_length=20, default='LOCAL', db_index=True, help_text="매장 형태 (LOCAL/CHAIN)")
    brand_name = models.CharField(max_length=100, blank=True, default='', help_text="브랜드명")
    price_tier = models.PositiveSmallIntegerField(default=2, db_index=True, help_text="PA 5단계 가격대")
    avg_price = models.PositiveIntegerField(default=0, help_text="1인 평균 예상 금액(원)")
    latitude = models.FloatField(default=37.5, help_text="위도")
    longitude = models.FloatField(default=127.0, help_text="경도")
    is_partner = models.BooleanField(default=False, db_index=True, help_text="제휴 매장 여부")
    is_open_now = models.BooleanField(default=True, help_text="기본 영업 여부")
    primary_weather_affinity = models.CharField(max_length=32, default='ALL', help_text="추천 날씨 적합도")
    venue_payload = models.JSONField(default=dict, help_text="정규화된 전체 매장 딕셔너리")
    menus_payload = models.JSONField(default=list, help_text="연결된 메뉴 리스트")
    source_sha256 = models.CharField(max_length=64, blank=True, default='', help_text="원천 레코드 해시")
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        db_table = 'venue_catalog_record'
        verbose_name = '매장 카탈로그 DB 미러'
        verbose_name_plural = '매장 카탈로그 DB 미러 목록'
        indexes = [
            models.Index(fields=['gu_name', 'category'], name='venue_cat_gu_cat_idx'),
        ]

    def __str__(self):
        return f"{self.venue_id} ({self.gu_name} · {self.venue_name})"
