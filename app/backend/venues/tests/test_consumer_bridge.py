from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from unittest.mock import patch

from backend.accounts.models import MemberProfile
from backend.workspaces.models import ServiceMembership, Workspace
from backend.workspaces.tests.test_platform import Helpers, TEST_CONFIG


@override_settings(PLATFORM_ENABLED=True, MEMBER_LOCAL_PREVIEW=True)
class ConsumerVenueTests(TestCase):
    venue_id = "VN_SL_000001"

    @classmethod
    def setUpTestData(cls):
        cls.consumer = cls.make_member("testconsumer", "CONSUMER")
        cls.business = cls.make_member("testbusiness", "BUSINESS")

    @staticmethod
    def make_member(name, kind):
        user = get_user_model().objects.create_user(username=name)
        MemberProfile.objects.create(user=user, nickname=name, nickname_key=name)
        ServiceMembership.objects.create(user=user, kind=kind, status="ACTIVE")
        return user

    def test_list_and_detail_require_active_consumer_session(self):
        paths = (
            "/api/venues", "/api/venues/search",
            f"/api/venues/{self.venue_id}", f"/api/venues/{self.venue_id}/menus",
        )
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.client.force_login(self.business)
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 403)
        self.client.force_login(self.consumer)
        for path in paths:
            self.assertEqual(self.client.post(path).status_code, 405)

    def test_list_coordinates_and_detail_menus_use_the_same_id(self):
        self.client.force_login(self.consumer)
        listing = self.client.get("/api/venues", {"sigungu": "강남구", "q": "그레인바운더리"})
        self.assertEqual(listing.status_code, 200)
        venue = next(row for row in listing.json()["venues"] if row["venue_id"] == self.venue_id)
        self.assertEqual((venue["latitude"], venue["longitude"]), (37.520945, 127.023642))

        detail = self.client.get(f"/api/venues/{venue['venue_id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("no-store", detail.headers["Cache-Control"])
        result = detail.json()["venue"]
        self.assertEqual((result["venue_id"], result["venue_name"]), (venue["venue_id"], venue["venue_name"]))
        menus = [menu for group in result["menu_tabs"] for menu in group["items"]]
        self.assertEqual(result["all_menus_count"], 16)
        self.assertEqual(len({menu["menu_id"] for menu in menus}), 16)
        self.assertEqual({menu["venue_id"] for menu in menus}, {self.venue_id})
        self.assertIn("에스프레소", {menu["menu_name"] for menu in menus})

    def test_integrated_browse_never_exposes_demo_writes(self):
        self.client.force_login(self.consumer)
        for method, path in (
            ("post", "/api/owner/venues/claim"),
            ("post", f"/api/venues/{self.venue_id}/reviews"),
            ("post", f"/api/venues/{self.venue_id}/bookmark"),
            ("get", "/api/venues/notifications"),
        ):
            self.assertEqual(getattr(self.client, method)(path).status_code, 404)

    def test_mcp_chat_requires_consumer_session_and_csrf(self):
        client = Client(enforce_csrf_checks=True)
        options = '/api/venues/ai-concierge/options'
        chat = '/api/venues/ai-concierge/chat'
        self.assertEqual(client.get(options).status_code, 401)
        client.force_login(self.business)
        self.assertEqual(client.get(options).status_code, 403)
        client.force_login(self.consumer)
        self.assertEqual(client.get(options).status_code, 200)
        with patch('backend.venues.views.concierge_view.run_venue_llm_concierge') as run:
            self.assertEqual(client.post(chat, {'message': '카페'}, content_type='application/json', HTTP_ORIGIN='http://testserver').status_code, 403)
            run.return_value = {'success': True, 'mcp': {'status': 'NO_MATCH'}, 'llm': {'status': 'OK'}}
            token = client.cookies['csrftoken'].value
            response = client.post(chat, {'message': '카페'}, content_type='application/json', HTTP_ORIGIN='http://testserver', HTTP_X_CSRFTOKEN=token)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(run.call_count, 1)

    def test_empty_list_and_missing_detail_have_distinct_results(self):
        self.client.force_login(self.consumer)
        empty = self.client.get("/api/venues", {"sigungu": "없는구"})
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json()["venues"], [])
        self.assertEqual(self.client.get("/api/venues/VN_SL_999999").status_code, 404)


@override_settings(**TEST_CONFIG)
class ApprovedVenueLinkTests(Helpers, TestCase):
    fixture_id = "demo-linked-venue-000001"
    detail_path = "/api/venues/VN_SL_000001"

    def setUp(self):
        self.owner = self.client_for(self.user("venueowner"))
        self.other_owner = self.client_for(self.user("otherowner"))
        self.consumer = self.client_for(self.user("venueguest", "CONSUMER"))
        _, self.reviewer_client = self.reviewer()

    def detail(self):
        response = self.consumer.get(self.detail_path)
        self.assertEqual(response.status_code, 200)
        return response.json()["venue"]

    def approved_workspace(self, client, fixture_id):
        claim = self.submit(client, self.claim(client, fixture_id))
        approved = self.approve(self.reviewer_client, claim)
        return approved["approved_workspace_id"]

    def apply_playlist(self, client, workspace_id, playlist_id, revision=1):
        return self.call(
            client, f"/workspaces/{workspace_id}/music",
            {"action": "APPLY", "playlist_id": playlist_id}, method="patch", version=revision,
        )

    def test_public_playlist_requires_the_linked_approved_and_active_workspace(self):
        other = self.approved_workspace(self.other_owner, "demo-active-001")
        self.data(self.apply_playlist(self.other_owner, other, "demo-bright"))
        self.assertIsNone(self.detail()["applied_playlist"])

        submitted = self.submit(self.owner, self.claim(self.owner, self.fixture_id))
        self.assertEqual(submitted["display_name"], self.detail()["venue_name"])
        self.assertEqual(submitted["address"], self.detail()["road_address"])
        self.assertIsNone(self.detail()["applied_playlist"])

        approved = self.approve(self.reviewer_client, submitted)
        workspace_id = approved["approved_workspace_id"]
        self.assertIsNone(self.detail()["applied_playlist"])
        self.data(self.apply_playlist(self.owner, workspace_id, "demo-calm"))

        venue = self.detail()
        alias = self.consumer.get("/api/venues/venue_seoul_0001/menus")
        self.assertEqual(alias.status_code, 200)
        self.assertEqual(alias.json()["venue"]["canonical_venue_id"], "VN_SL_000001")
        self.assertEqual(alias.json()["venue"]["applied_playlist"], venue["applied_playlist"])
        self.assertEqual(venue["applied_playlist"], {"id": "demo-calm", "name": "합성 차분한 선곡표"})
        self.assertEqual(venue["all_menus_count"], 16)
        self.assertNotIn("workspace_id", venue)

        Workspace.objects.filter(pk=workspace_id).update(status="SUSPENDED")
        self.assertIsNone(self.detail()["applied_playlist"])

    def test_another_owner_cannot_change_the_linked_venue_or_approve_a_duplicate(self):
        workspace_id = self.approved_workspace(self.owner, self.fixture_id)
        applied = self.data(self.apply_playlist(self.owner, workspace_id, "demo-calm"))
        other = self.approved_workspace(self.other_owner, "demo-branch-002")
        self.data(self.apply_playlist(self.other_owner, other, "demo-bright"))

        blocked = self.apply_playlist(self.other_owner, workspace_id, "demo-bright", applied["revision"])
        self.assertEqual(blocked.status_code, 404)
        self.assertEqual(blocked.json()["error"]["code"], "NOT_FOUND")
        self.assertEqual(self.detail()["applied_playlist"]["id"], "demo-calm")

        duplicate = self.submit(self.other_owner, self.claim(self.other_owner, self.fixture_id))
        decision = self.call(
            self.reviewer_client, f"/admin/workspace-claims/{duplicate['id']}/decisions",
            {"decision": "APPROVE", "reason_code": "DEMO_SCENARIO_CONFIRMED"}, version=duplicate["revision"],
        )
        self.assertEqual(decision.status_code, 409)
        self.assertEqual(decision.json()["error"]["code"], "WORKSPACE_ALREADY_LINKED")
        self.assertEqual(self.detail()["applied_playlist"]["id"], "demo-calm")
