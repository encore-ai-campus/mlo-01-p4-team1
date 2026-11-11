import datetime
from django.test import TestCase
from django.utils import timezone

from backend.venues.services.open_hours_service import check_business_hours
from backend.venues.services.event_service import (
    create_owner_event,
    register_event_intent,
    verify_onsite_qr_and_deduct_stock,
    get_visible_events_for_venue,
    get_visible_events_for_venues,
)
from backend.venues.models.event import VenueEvent, VenueEventParticipation


class TestStep3DomainServices(TestCase):

    def test_midnight_business_hours(self):
        """① 심야 영업 판정 검증 (17:00 ~ 03:00 자정 넘김 포함)"""
        open_time = datetime.time(17, 0)
        close_time = datetime.time(3, 0)

        # 1. 23:00 (밤 11시) -> 영업 중
        self.assertTrue(check_business_hours(open_time, close_time, datetime.time(23, 0)))
        # 2. 01:30 (새벽 1시 30분, 자정 넘김) -> 영업 중
        self.assertTrue(check_business_hours(open_time, close_time, datetime.time(1, 30)))
        # 3. 02:59 (마감 직전) -> 영업 중
        self.assertTrue(check_business_hours(open_time, close_time, datetime.time(2, 59)))
        # 4. 03:00 (마감 정각) -> 마감
        self.assertFalse(check_business_hours(open_time, close_time, datetime.time(3, 0)))
        # 5. 12:00 (낮 12시) -> 마감
        self.assertFalse(check_business_hours(open_time, close_time, datetime.time(12, 0)))
        # 6. 16:59 (오픈 직전) -> 마감
        self.assertFalse(check_business_hours(open_time, close_time, datetime.time(16, 59)))
        # 7. 17:00 (오픈 정각) -> 영업 중
        self.assertTrue(check_business_hours(open_time, close_time, datetime.time(17, 0)))

        print("\n[Step 3 검증 1] 심야 영업(17:00 ~ 03:00) 자정 넘김 및 경계조건 판정: PASS")

    def test_event_intent_keeps_stock_and_qr_deducts_with_409(self):
        """
        ② [참여할게요] 클릭 시 재고 유지 & QR 인증 시 재고 -1 차감 및 동일 이벤트 1일 1회 중복 차단(409)
        """
        now = timezone.now()
        event = create_owner_event(
            venue_id='venue_test_01',
            owner_id='owner_test_01',
            title='비 오는 날 파전 1+1 타임세일',
            description='비 오는 날 한정 혜택',
            badge_text='1+1 타임세일',
            discount_info='파전 1+1',
            total_quantity=5,
            start_at=now - datetime.timedelta(hours=1),
            end_at=now + datetime.timedelta(hours=2),
        )

        user_id = 'user_step3_tester'

        # [A] [참여할게요] 클릭
        res_intent = register_event_intent(event.event_id, user_id)
        self.assertTrue(res_intent['success'])
        self.assertEqual(res_intent['code'], 200)

        # 재고는 그대로 5개 유지되어야 함!
        event.refresh_from_db()
        self.assertEqual(event.remaining_quantity, 5, "참여할게요 클릭 시 재고가 차감되면 안 됩니다.")
        self.assertEqual(event.intended_visitors_count, 1, "방문 예정 인원은 1명이어야 합니다.")

        # [B] 현장 방문 QR 인증 시도 -> 재고 1 차감되어 4개가 되어야 함
        res_qr = verify_onsite_qr_and_deduct_stock(event.event_id, user_id, res_intent.get('qr_auth_code'))
        self.assertTrue(res_qr['success'])
        self.assertEqual(res_qr['code'], 200)

        event.refresh_from_db()
        self.assertEqual(event.remaining_quantity, 4, "현장 QR 인증 완료 시 재고가 1 차감되어야 합니다.")

        # [C] 동일 계정으로 당일 또다시 QR 인증 시도 -> 409 중복 차단되어야 함!
        res_qr_duplicate = verify_onsite_qr_and_deduct_stock(event.event_id, user_id)
        self.assertFalse(res_qr_duplicate['success'])
        self.assertEqual(res_qr_duplicate['code'], 409, "당일 중복 인증 시 409 반환되어야 합니다.")
        self.assertEqual(res_qr_duplicate['error'], 'ALREADY_PARTICIPATED_TODAY')

        # 재고는 여전히 4개로 유지되어야 함 (추가 차감 없음)
        event.refresh_from_db()
        self.assertEqual(event.remaining_quantity, 4)

        print("[Step 3 검증 2] [참여할게요] 재고 유지(5개) & QR인증 재고 차감(4개) & 1일 1회 중복 차단(409): PASS")

    def test_grace_period_display_and_hide(self):
        """③ 종료 후 4분 시점 노출('이벤트가 종료되었어요') & 6분 시점 숨김 처리 검증"""
        now = timezone.now()
        # 10분 전에 시작해서 '지금 시점'에 종료되는 이벤트
        end_time = now
        event = VenueEvent.objects.create(
            venue_id='venue_grace_test',
            owner_id='owner_grace_01',
            title='선착순 마감 이벤트',
            badge_text='선착순 마감',
            discount_info='무료 시음',
            total_quantity=10,
            remaining_quantity=3,
            start_at=end_time - datetime.timedelta(hours=1),
            end_at=end_time,
            is_active=True,
        )

        # 시점 1: 종료 후 4분 경과 시점 -> 노출되어야 하며 '이벤트가 종료되었어요' 배지 부착
        time_at_4min = end_time + datetime.timedelta(minutes=4)
        visible_4min = get_visible_events_for_venue(event.venue_id, as_of=time_at_4min)
        self.assertEqual(len(visible_4min), 1, "종료 후 4분 시점에는 보드에 노출되어야 합니다.")
        self.assertEqual(visible_4min[0]['badge_text'], '이벤트가 종료되었어요')
        self.assertTrue(visible_4min[0]['is_ended_grace_period'])

        # 시점 2: 종료 후 6분 경과 시점 (5분 유예 만료) -> 자동 숨김(미노출) 처리되어야 함!
        time_at_6min = end_time + datetime.timedelta(minutes=6)
        visible_6min = get_visible_events_for_venue(event.venue_id, as_of=time_at_6min)
        self.assertEqual(len(visible_6min), 0, "종료 후 6분 시점에는 보드에서 자동 제외되어야 합니다.")

        print("[Step 3 검증 3] 종료 후 4분 시점 노출('이벤트가 종료되었어요') & 6분 시점 자동 숨김: PASS")


    def test_bulk_event_lookup_matches_single_lookup_with_few_queries(self):
        """목록 랭킹용 일괄 조회가 매장별 조회와 같은 결과를 내고 쿼리는 매장 수와 무관해야 함"""
        now = timezone.now()
        for venue_id, delta in (('bulk_a', datetime.timedelta(hours=2)), ('bulk_b', datetime.timedelta(minutes=-2)),
                                ('bulk_c', datetime.timedelta(minutes=-30))):
            VenueEvent.objects.create(
                venue_id=venue_id, owner_id='owner_bulk', title=f'{venue_id} 이벤트', badge_text='배지',
                discount_info='혜택', total_quantity=5, remaining_quantity=5,
                start_at=now - datetime.timedelta(hours=1), end_at=now + delta, is_active=True,
            )
        venue_ids = ['bulk_a', 'bulk_b', 'bulk_c', 'bulk_none']
        with self.assertNumQueries(1):
            bulk = get_visible_events_for_venues(venue_ids, as_of=now)
        for venue_id in venue_ids:
            single = get_visible_events_for_venue(venue_id, as_of=now)
            self.assertEqual(bulk.get(venue_id, []), single, venue_id)
        # 종료 후 5분 초과(bulk_c)와 이벤트 없음(bulk_none)은 노출되지 않아야 함
        self.assertEqual(sorted(bulk), ['bulk_a', 'bulk_b'])
        self.assertTrue(bulk['bulk_b'][0]['is_ended_grace_period'])
        self.assertEqual(get_visible_events_for_venue('bulk_a', prefetched=bulk), bulk['bulk_a'])
        self.assertEqual(get_visible_events_for_venue('bulk_none', prefetched=bulk), [])
