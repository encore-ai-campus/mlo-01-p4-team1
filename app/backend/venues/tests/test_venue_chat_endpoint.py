import os
from unittest.mock import patch

from django.conf import settings
from django.test import AsyncClient, SimpleTestCase, override_settings

from backend.venues.views import concierge_view


OPTIONS = "/api/venues/ai-concierge/options"
CHAT = "/api/venues/ai-concierge/chat"
ORIGIN = "http://testserver"
MESSAGE = "서교동 헌팅술집"


class OptionsMcp:
    def list_tags(self):
        return {"regions": ["인천 연수구", "서울 마포구 서교동"], "tags": []}


@override_settings(ALLOWED_HOSTS=["testserver"], MEMBER_LOCAL_PREVIEW=True, PLATFORM_ENABLED=False)
class VenueChatEndpointTests(SimpleTestCase):
    async def demo_client(self):
        client = AsyncClient(enforce_csrf_checks=True)
        mcp = OptionsMcp()
        django_handler = client.handler

        async def with_mcp(scope):
            return await django_handler({**scope, "venue_mcp_client": mcp})

        client.handler = with_mcp
        response = await client.get(OPTIONS)
        self.assertEqual(response.status_code, 200)
        headers = {"Origin": ORIGIN, "X-CSRFToken": client.cookies[settings.CSRF_COOKIE_NAME].value}
        return client, mcp, headers

    async def test_preview_accepts_only_same_origin_csrf_json_and_valid_input(self):
        client, _, headers = await self.demo_client()
        with patch.object(concierge_view, "run_venue_llm_concierge") as execute:
            for content_type, sent_headers, data, status, code in (
                ("text/plain", headers, MESSAGE, 415, "JSON_REQUIRED"),
                ("application/json", {**headers, "Origin": "https://other.invalid"}, {"message": MESSAGE}, 403, "CSRF_FAILED"),
                ("application/json", {"X-CSRFToken": headers["X-CSRFToken"]}, {"message": MESSAGE}, 403, "ORIGIN_NOT_ALLOWED"),
                ("application/json", {"Origin": ORIGIN}, {"message": MESSAGE}, 403, "CSRF_FAILED"),
                ("application/json", headers, "{", 400, "INVALID_LLM_REQUEST"),
                ("application/json", headers, {"message": " ", "region": 1}, 400, "INVALID_LLM_REQUEST"),
                ("application/json", headers, {"mcp": {"query": MESSAGE}}, 400, "INVALID_LLM_REQUEST"),
            ):
                with self.subTest(code=code, status=status, data=data):
                    response = await client.post(CHAT, data, content_type=content_type, headers=sent_headers)
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.json()["code"], code)
            execute.assert_not_called()

    async def test_platform_and_nonpreview_block_anonymous_chat_and_options(self):
        client, _, headers = await self.demo_client()
        with patch.object(concierge_view, "run_venue_llm_concierge") as execute:
            for override in ({"PLATFORM_ENABLED": True}, {"MEMBER_LOCAL_PREVIEW": False}):
                with self.subTest(override=override), override_settings(**override):
                    self.assertEqual((await client.get(OPTIONS)).status_code, 404)
                    response = await client.post(CHAT, {"message": MESSAGE}, content_type="application/json", headers=headers)
                    self.assertEqual(response.status_code, 404)
            execute.assert_not_called()

    async def test_same_origin_preserves_unverified_and_zero_result_without_legacy_fallback(self):
        client, mcp, headers = await self.demo_client()
        with (
            patch.object(concierge_view, "run_venue_llm_concierge") as execute,
            patch.object(concierge_view, "generate_concierge_briefing") as legacy,
        ):
            for llm_status, mcp_status in (("NEEDS_CONFIRMATION", "NEEDS_CHECK"), ("OK", "NO_MATCH")):
                answer = {
                    "success": True,
                    "mcp": {"status": mcp_status, "ranked": [], "counts": {"ranked": 0}},
                    "llm": {"status": llm_status, "message": "조건 확인", "interpreted_conditions": {"region": "서교동"}},
                }
                execute.return_value = answer
                response = await client.post(CHAT, {"message": MESSAGE}, content_type="application/json", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), answer)
                self.assertIn("no-store", response["Cache-Control"])
                execute.assert_called_with(MESSAGE, mcp_client=mcp, region=None)
            legacy.assert_not_called()

    async def test_options_distinguish_unconfigured_gemini_and_local_codex(self):
        client, _, _ = await self.demo_client()
        with patch.dict(os.environ, {"VENUE_LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "", "VENUE_LLM_MODEL": ""}):
            response = await client.get(OPTIONS)
            self.assertEqual(response.json()["llm"], {"configured": False, "provider": "gemini", "mode": "API"})
        with (
            patch.dict(os.environ, {"VENUE_LLM_PROVIDER": "codex_cli"}),
            patch.object(concierge_view.CodexCliVenueProvider, "__init__", return_value=None),
        ):
            response = await client.get(OPTIONS)
            self.assertEqual(response.json()["llm"], {"configured": True, "provider": "codex_cli", "mode": "LOCAL_TEST"})

    async def test_provider_errors_keep_the_status_and_no_recommendation(self):
        client, _, headers = await self.demo_client()
        with patch.object(concierge_view, "run_venue_llm_concierge") as execute:
            for code, status, message in (
                ("LLM_NOT_CONFIGURED", 503, "AI 연결 설정 필요"),
                ("LLM_TIMEOUT", 504, "시간 초과"),
            ):
                execute.return_value = {"success": False, "code": code, "mcp": None,
                                        "llm": {"status": "ERROR", "message": "시간 초과"}}
                response = await client.post(CHAT, {"message": MESSAGE}, content_type="application/json", headers=headers)
                self.assertEqual(response.status_code, status)
                body = response.json()
                self.assertFalse(body["success"])
                self.assertIsNone(body["mcp"])
                self.assertEqual((body["code"], body["message"]), (code, message))
