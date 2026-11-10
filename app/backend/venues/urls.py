from django.urls import path
from backend.venues import views
from backend.venues.views.concierge_view import ai_concierge_options_api

urlpatterns = [
    # 1. 고정 하위 경로 우선 매칭 (동적 <venue_id>보다 반드시 앞에 위치해야 함)
    path('api/venues/events/active', views.active_events_banner_api, name='venue-events-active'),
    path('api/venues/events', views.active_events_banner_api, name='venue-events'),
    path('api/venues/events/<int:event_id>/intent', views.event_intent_api, name='venue-event-intent'),
    path('api/venues/events/<int:event_id>/verify-qr', views.event_verify_qr_api, name='venue-event-verify-qr'),
    path('api/venues/events/<int:event_id>/report-false', views.event_report_false_api, name='venue-event-report-false'),
    path('api/venues/my/summary', views.venue_my_summary_api, name='venue-my-summary'),
    path('api/venues/notifications', views.venue_notifications_api, name='venue-notifications'),
    path('api/venues/interactions', views.venue_interaction_generic_api, name='venue-interactions'),
    path('api/venues/ai-concierge', views.ai_concierge_api, name='venue-ai-concierge'),
    path('api/venues/ai-concierge/options', ai_concierge_options_api, name='venue-ai-concierge-options'),
    path('api/venues/concierge/recommend', views.ai_concierge_api, name='venue-concierge-recommend'),

    # 2. 매장 탐색 목록
    path('api/venues', views.venue_list_api, name='venue-list'),
    path('api/venues/search', views.venue_list_api, name='venue-search'),

    # 3. 매장 개별 동적 상세 및 피드백 (단일 <venue_id>)
    path('api/venues/<str:venue_id>/menus', views.venue_detail_api, name='venue-menus'),
    path('api/venues/<str:venue_id>', views.venue_detail_api, name='venue-detail'),
    path('api/venues/<str:venue_id>/bookmark', views.venue_bookmark_api, name='venue-bookmark'),
    path('api/venues/<str:venue_id>/reviews', views.venue_review_api, name='venue-review'),
    path('api/venues/<str:venue_id>/closure-report', views.venue_closure_report_api, name='venue-closure-report'),

    # 4. 점주 관리 API
    path('api/owner/venues/claim', views.owner_claim_venue_api, name='venue-owner-claim'),
    path('api/owner/venues/register', views.owner_register_venue_api, name='venue-owner-register'),
    path('api/owner/venues/<str:venue_id>/events', views.owner_create_event_api, name='venue-owner-create-event'),
    path('api/owner/venues/events/<int:event_id>/stop', views.owner_stop_event_api, name='venue-owner-stop-event'),

    # 5. 관리자 폐업 검수 API
    path('api/admin/venues/closures', views.admin_closure_list_api, name='venue-admin-closures'),
    path('api/admin/venues/closures/<str:venue_id>/resolve', views.admin_resolve_closure_api, name='venue-admin-resolve-closure'),
]
