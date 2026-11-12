import json
import os
import tempfile
import uuid
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from backend.common.admin_roles import ROLES
from backend.common.management.commands import check_runtime
from backend.venues.management.commands.relay_venue_outbox import relay_pending_batch
from backend.venues.models.event import OwnerEventTrustScore, VenueEvent
from backend.venues.models.interaction import VenueInteraction
from backend.venues.models.outbox import VenueOutbox
from backend.venues.models.status_overlay import VenueClosureDecision, VenueStatusOverlay
from backend.venues.streaming.async_kafka_dispatcher import AsyncKafkaDispatcher
from backend.venues.streaming.observability import build_sanitized_observation
from backend.workspaces.tests.test_platform import Helpers, PASSWORD, TEST_CONFIG


@override_settings(**{**TEST_CONFIG, "ADMIN_PASSKEY_REQUIRED": True})
class VenueAdminClosureAndMonitoringTests(Helpers, TestCase):
    venue_id = "VN_SL_000001"
    closure_list_url = "/api/admin/venues/closures"
    closure_resolve_url = f"/api/admin/venues/closures/{venue_id}/resolve"
    metrics_url = "/api/admin/venues/metrics"

    def setUp(self):
        self.consumer = self.user("closure_consumer", "CONSUMER")
        self.business = self.user("closure_owner", "BUSINESS")
        self.other_business = self.user("closure_other_owner", "BUSINESS")

    def _make_admin(self, username: str, *, permissions=(), passkey_age_seconds: int | None = 0):
        user = get_user_model().objects.create_user(username=username, password=PASSWORD, is_staff=True)
        for perm_code in permissions:
            perm = Permission.objects.get(content_type__app_label="common", codename=perm_code)
            user.user_permissions.add(perm)
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        if passkey_age_seconds is not None:
            session = client.session
            session["admin_passkey_user"] = str(user.pk)
            session["admin_passkey_verified_at"] = timezone.now().timestamp() - passkey_age_seconds
            session.save()
        return user, client

    def _post_with_csrf(self, client: Client, url: str, payload: dict, **extra_headers):
        client.get("/api/auth/me")
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        return client.post(
            url,
            payload,
            content_type="application/json",
            HTTP_ORIGIN="http://testserver",
            HTTP_X_CSRFTOKEN=token,
            **extra_headers,
        )

    def test_admin_closure_and_metrics_rbac_and_passkey_enforced(self):
        _, ads_client = self._make_admin("ads_only", permissions=("manage_ads",), passkey_age_seconds=0)
        _, no_pk_ops = self._make_admin("ops_no_pk", permissions=ROLES["OPS_ADMIN"], passkey_age_seconds=None)
        _, stale_ops = self._make_admin("ops_stale_pk", permissions=ROLES["OPS_ADMIN"], passkey_age_seconds=600)
        ops_user, fresh_ops = self._make_admin("ops_fresh", permissions=ROLES["OPS_ADMIN"], passkey_age_seconds=5)

        anon = Client(enforce_csrf_checks=True)
        self.assertEqual(anon.get(self.closure_list_url).status_code, 401)
        self.assertEqual(anon.get(self.metrics_url).status_code, 401)

        consumer_client = self.client_for(self.consumer)
        self.assertEqual(consumer_client.get(self.closure_list_url).status_code, 403)
        self.assertEqual(consumer_client.get(self.metrics_url).status_code, 403)

        # Staff without manage_venues/view_operations forbidden
        self.assertEqual(ads_client.get(self.closure_list_url).status_code, 403)
        self.assertEqual(ads_client.get(self.metrics_url).status_code, 403)
        self.assertEqual(self._post_with_csrf(ads_client, self.closure_resolve_url, {"decision": "CLOSED_HIDDEN"}).status_code, 403)

        # Staff with role but without passkey rejected
        self.assertEqual(no_pk_ops.get(self.closure_list_url).status_code, 403)
        self.assertEqual(no_pk_ops.get(self.metrics_url).status_code, 403)

        # Fresh session GET allowed; expired recent passkey rejected for mutating resolve
        self.assertEqual(stale_ops.get(self.closure_list_url).status_code, 200)
        stale_res = self._post_with_csrf(stale_ops, self.closure_resolve_url, {"decision": "CLOSED_HIDDEN"})
        self.assertEqual(stale_res.status_code, 403)
        self.assertEqual(stale_res.json()["code"], "PASSKEY_REQUIRED")

        # OPS_ADMIN /api/auth/me exposes integrated /platform/review capability flags
        me = fresh_ops.get("/api/auth/me").json()
        self.assertTrue(me["venue_closure_allowed"])
        self.assertTrue(me["venue_metrics_allowed"])
        self.assertIn("/platform/review", [m["path"] for m in me["allowed_menus"]])

        metrics_res = fresh_ops.get(self.metrics_url)
        self.assertEqual(metrics_res.status_code, 200)
        body = metrics_res.json()
        self.assertTrue(body["success"])
        self.assertIn("outbox", body)
        self.assertIn("dispatcher", body)
        self.assertIn("catalog", body)
        self.assertEqual(ops_user.is_staff, True)

    def test_closure_report_resolve_conflict_outbox_and_visibility_flow(self):
        consumer_client = self.client_for(self.consumer)
        business_client = self.client_for(self.business)
        ops_user, ops_client = self._make_admin("ops_reviewer", permissions=ROLES["OPS_ADMIN"], passkey_age_seconds=5)

        # 1. Consumer submits closure report -> status CLOSURE_REPORTED
        report_res = self._post_with_csrf(
            consumer_client,
            f"/api/venues/{self.venue_id}/closure-report",
            {"user_id": "forged_guest", "reason": "매장이 폐업하고 철거 중입니다."},
        )
        self.assertEqual(report_res.status_code, 200)
        self.assertEqual(report_res.json()["status"], "CLOSURE_REPORTED")

        # Consumer detail exposes recent reviews safely while masking platform user_id
        self._post_with_csrf(
            consumer_client,
            f"/api/venues/{self.venue_id}/reviews",
            {"content": "이전 방문 리뷰", "rating": 4},
        )
        detail = consumer_client.get(f"/api/venues/{self.venue_id}")
        self.assertEqual(detail.status_code, 200)
        reviews = detail.json()["venue"]["recent_reviews"]
        self.assertTrue(reviews)
        self.assertEqual(reviews[0]["user_id"], "방문 회원")

        # 2. Admin queue lists reported venue with version + summary counts
        queue_res = ops_client.get(self.closure_list_url, {"status": "CLOSURE_REPORTED"})
        self.assertEqual(queue_res.status_code, 200)
        items = queue_res.json()["closures"]
        target = next(row for row in items if row["venue_id"] == self.venue_id)
        self.assertEqual(target["closure_report_count"], 1)
        initial_version = target["version"]

        # Stale version returns 409 VERSION_CONFLICT
        conflict_res = self._post_with_csrf(
            ops_client,
            self.closure_resolve_url,
            {"decision": "CLOSED_HIDDEN", "version": initial_version + 99},
        )
        self.assertEqual(conflict_res.status_code, 409)
        self.assertEqual(conflict_res.json()["code"], "VERSION_CONFLICT")

        # 3. Valid resolve with If-Match hides venue & records audit + outbox
        resolve_res = self._post_with_csrf(
            ops_client,
            self.closure_resolve_url,
            {"decision": "CLOSED_HIDDEN", "notes": "현장 확인 완료", "reason_code": "CONFIRMED_CLOSED"},
            HTTP_IF_MATCH=f'"{initial_version}"',
        )
        self.assertEqual(resolve_res.status_code, 200)
        self.assertEqual(resolve_res.json()["status"], "CLOSED_HIDDEN")
        self.assertEqual(resolve_res.json()["version"], initial_version + 1)

        overlay = VenueStatusOverlay.objects.get(venue_id=self.venue_id)
        self.assertEqual(overlay.last_reviewed_by_id, ops_user.pk)
        self.assertTrue(VenueClosureDecision.objects.filter(overlay_id=self.venue_id, decision="CLOSED_HIDDEN").exists())
        self.assertTrue(VenueOutbox.objects.filter(venue_id=self.venue_id, event_type="CLOSURE_RESOLVED_CLOSED_HIDDEN", state="PENDING").exists())

        # Hidden venue excluded from browse, detail, and owner bridge claim
        browse = consumer_client.get("/api/venues", {"sigungu": "강남구", "q": "그레인바운더리"}).json()
        self.assertFalse(any(v["venue_id"] == self.venue_id for v in browse["venues"]))
        self.assertEqual(consumer_client.get(f"/api/venues/{self.venue_id}").status_code, 404)

        blocked_claim = self._post_with_csrf(
            business_client,
            "/api/owner/venues/claim",
            {"venue_id": self.venue_id, "business_number": "111-11-11111"},
        )
        self.assertEqual(blocked_claim.status_code, 409)
        self.assertEqual(blocked_claim.json()["code"], "VENUE_UNAVAILABLE")

        # 4. Admin restores venue to OPEN -> visible again
        restore_res = self._post_with_csrf(
            ops_client,
            self.closure_resolve_url,
            {"decision": "OPEN", "notes": "재오픈 확인", "version": initial_version + 1},
        )
        self.assertEqual(restore_res.status_code, 200)
        self.assertEqual(restore_res.json()["status"], "OPEN")
        self.assertEqual(consumer_client.get(f"/api/venues/{self.venue_id}").status_code, 200)

    def test_false_event_report_and_owner_membership_check(self):
        consumer_client = self.client_for(self.consumer)
        other_biz_client = self.client_for(self.other_business)

        # Business without ACTIVE OWNER/MANAGER workspace membership cannot publish event
        unauth_event = self._post_with_csrf(
            other_biz_client,
            f"/api/owner/venues/{self.venue_id}/events",
            {"title": "미승인 이벤트", "total_quantity": 5},
        )
        self.assertEqual(unauth_event.status_code, 403)

        # Approved owner workspace allows event creation; event report-false deducts trust score & writes outbox
        self.setup_store(self.business, location=f"catalog:{self.venue_id}")
        owner_client = self.client_for(self.business)
        created = self._post_with_csrf(
            owner_client,
            f"/api/owner/venues/{self.venue_id}/events",
            {"title": "오후 특가", "total_quantity": 5, "duration_hours": 1},
        )
        self.assertEqual(created.status_code, 200)
        event_id = created.json()["event_id"]

        for idx in range(3):
            report = self._post_with_csrf(
                consumer_client,
                f"/api/venues/events/{event_id}/report-false",
                {"reason": f"허위 혜택 신고 {idx + 1}"},
            )
            self.assertEqual(report.status_code, 200)

        ev = VenueEvent.objects.get(event_id=event_id)
        self.assertTrue(ev.is_active)
        trust = OwnerEventTrustScore.objects.get(owner_id=f"platform:{self.business.pk}")
        self.assertEqual(float(trust.trust_score), 85.0)
        self.assertEqual(VenueInteraction.objects.filter(venue_id=self.venue_id, interaction_type="REPORT_FALSE_EVENT").count(), 3)
        self.assertEqual(VenueOutbox.objects.filter(venue_id=self.venue_id, event_type="EVENT_FALSE_REPORTED").count(), 3)

    def test_observability_spool_dispatcher_outbox_relay_and_check_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            spool_path = os.path.join(tmpdir, "venues-observations.ndjson")
            with patch.dict(os.environ, {"VENUE_OBSERVATION_SPOOL_PATH": spool_path}, clear=False):
                req_id = str(uuid.uuid4())
                envelope = build_sanitized_observation(
                    dataset="mlo.pipeline",
                    action="unit-test",
                    outcome="success",
                    pipeline_name="venue-outbox-relay",
                    stage="relay",
                    state="PUBLISHED",
                    request_id=req_id,
                    records_in=1,
                    records_out=1,
                )
                uuid.UUID(envelope["event"]["id"])
                self.assertEqual(envelope["trace"]["id"], req_id.replace("-", ""))
                self.assertNotIn("email", json.dumps(envelope))

                VenueOutbox.objects.create(
                    event_id=str(uuid.uuid4()),
                    event_type="REVIEW_CREATED",
                    venue_id=self.venue_id,
                    actor_id=f"platform:{self.consumer.pk}",
                    payload={"rating": 5},
                )
                summary = relay_pending_batch(batch_size=10, mode="observation")
                self.assertEqual(summary["sent"], 1)
                self.assertEqual(summary["failed"], 0)
                relayed = VenueOutbox.objects.get(venue_id=self.venue_id, event_type="REVIEW_CREATED")
                self.assertEqual(relayed.state, "SENT")
                self.assertEqual(relayed.attempts, 1)

                with open(spool_path, encoding="utf-8") as f:
                    lines = [json.loads(line) for line in f.read().splitlines() if line.strip()]
                self.assertTrue(lines)
                self.assertEqual(lines[0]["pipeline"]["name"], "venue-outbox-relay")

        # Outbox relay Kafka failure increments attempts and writes sanitized failure record
        bad_producer = MagicMock()
        bad_producer.send.return_value.get.side_effect = RuntimeError("broker down")
        pending = VenueOutbox.objects.create(
            event_id=str(uuid.uuid4()),
            event_type="VENUE_CLOSURE_RESOLVED",
            venue_id=self.venue_id,
            payload={"status": "CLOSED_HIDDEN"},
        )
        with patch.dict(os.environ, {"VENUE_KAFKA_BOOTSTRAP": "broker:9092", "VENUE_KAFKA_TOPIC": "venue-events"}, clear=False):
            fail_summary = relay_pending_batch(batch_size=10, mode="kafka", producer=bad_producer)
            self.assertEqual(fail_summary["failed"], 1)
            pending.refresh_from_db()
            self.assertEqual(pending.attempts, 1)
            self.assertIn("RuntimeError", pending.last_error)

            # Dispatcher publish with mocked Kafka producer tracks stats cleanly
            disp = AsyncKafkaDispatcher(queue_size=16, max_workers=1)
            try:
                mock_producer = MagicMock()
                mock_producer.send.return_value.get.return_value = None
                disp._producer = mock_producer
                disp._dispatch_to_kafka({
                    "event_id": str(uuid.uuid4()),
                    "behavior_type": "CLICK",
                    "venue_id": self.venue_id,
                    "timestamp": 1234.5,
                })
                stats = disp.get_stats()
                self.assertEqual(stats["mode"], "kafka")
                self.assertEqual(stats["kafka_sent_count"], 1)
                self.assertEqual(stats["kafka_failed_count"], 0)
            finally:
                disp.shutdown()

        # Runtime readiness check includes venues catalog and manage_venues admin permissions
        cfg = check_runtime.configuration()
        self.assertTrue(cfg["venues_catalog_configured"])
        self._make_admin("ops_runtime", permissions=ROLES["OPS_ADMIN"], passkey_age_seconds=5)
        self._make_admin("ads_runtime", permissions=ROLES["ADS_ADMIN"], passkey_age_seconds=5)
        self._make_admin("super_runtime", permissions=ROLES["SUPER_ADMIN"], passkey_age_seconds=5)
        with (
            patch("pipeline.weather.mapping_postgres.load_regions", return_value={"entries": [{"gu": "강남구"}]}),
            patch("pipeline.weather.es_latest.configuration", side_effect=RuntimeError("no es")),
            patch("kafka.KafkaConsumer", side_effect=RuntimeError("no kafka")),
            override_settings(MUSIC_S3_ARCHIVE_DEFERRED=True),
        ):
            conn = check_runtime.connections()
        self.assertEqual(conn["venues_catalog"], {"ok": True})
        self.assertEqual(conn["administrator_permissions"], {"ok": True})

    @override_settings(LOCAL_DEMO=False, MEMBER_LOCAL_PREVIEW=False, PLATFORM_ENABLED=False)
    def test_non_demo_non_platform_allows_public_catalog_read_while_keeping_writes_closed(self):
        anon = Client(enforce_csrf_checks=True)
        listing = anon.get("/api/venues", {"sigungu": "강남구"})
        self.assertEqual(listing.status_code, 200)
        self.assertTrue(listing.json()["venues"])
        self.assertEqual(anon.get(f"/api/venues/{self.venue_id}").status_code, 200)
        self.assertEqual(anon.get("/api/venues/events/active", {"gu": "강남구"}).status_code, 200)

        # Personal notification/summary endpoints degrade cleanly to empty state in non-demo public mode
        notis = anon.get("/api/venues/notifications")
        self.assertEqual(notis.status_code, 200)
        self.assertEqual(notis.json()["notifications"], [])

        # CLOSED_HIDDEN venues are excluded in non-demo public reads too
        VenueStatusOverlay.objects.create(venue_id=self.venue_id, status="CLOSED_HIDDEN")
        self.assertEqual(anon.get(f"/api/venues/{self.venue_id}").status_code, 404)

        # Write endpoints stay fail-closed without CSRF/auth
        self.assertEqual(anon.post(f"/api/venues/{self.venue_id}/bookmark", {}, content_type="application/json").status_code, 403)
        anon.get("/api/auth/me")
        csrf = anon.cookies[settings.CSRF_COOKIE_NAME].value
        self.assertEqual(
            anon.post(
                f"/api/venues/{self.venue_id}/bookmark",
                {},
                content_type="application/json",
                HTTP_ORIGIN="http://testserver",
                HTTP_X_CSRFTOKEN=csrf,
            ).status_code,
            503,
        )
