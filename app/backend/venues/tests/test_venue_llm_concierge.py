from contextlib import nullcontext
import json
from subprocess import TimeoutExpired
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from google.genai.errors import ClientError
from google.genai.types import GenerateContentResponse
import httpx

from backend.venues.services.venue_llm_concierge import TOOL_NAME, run_venue_llm_concierge


def catalog_result(*, ranked=(), unverified=()):
    return {
        "status": "NEEDS_CHECK" if unverified else "OK",
        **({"empty_reason": "CONDITION_UNVERIFIED", "unverified_conditions": list(unverified)} if unverified else {}),
        "request": {"region": "인천 연수구" if not unverified else "서교동"},
        "counts": {"ranked": len(ranked), "pending": 0, "excluded": 0},
        "ranked": list(ranked), "pending": [], "excluded": [],
    }


@override_settings(GEMINI_ENABLED=True, GEMINI_TIMEOUT_MS=2000)
class VenueLlmConciergeTests(SimpleTestCase):
    def setUp(self):
        self.mcp = Mock()
        self.mcp.list_tool_specs.return_value = [{
            "name": TOOL_NAME,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "region": {"type": "string"}, "query": {"type": "string"},
                    "request_text": {"type": "string"}, "venue_category": {"enum": ["CAFE", "PUB"]},
                    "orders": {"type": "array"}, "unverified_conditions": {"type": "array"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
            },
        }]
        self.rows = [{
            "venue_id": venue_id, "name": venue_id, "address": "인천 연수구", "matched_moods": [],
            "matched_orders": [[{"menu_id": "cookie", "name": "초코 쿠키 (매장 메뉴판 미확인)"}]],
            "forecast": {"state": "NO_FORECAST"},
        } for venue_id in ("venue-B", "venue-A")]

    def gemini_response(self, arguments):
        return GenerateContentResponse.model_validate({
            "modelVersion": "verified-model",
            "candidates": [{"content": {"role": "model", "parts": [
                {"text": "확인하지 않은 임의 영업시간이나 가게 추천"},
                {"functionCall": {"name": TOOL_NAME, "args": arguments}},
            ]}}],
        })

    @patch.dict("os.environ", {"GEMINI_API_KEY": "local-test-value", "VENUE_LLM_MODEL": "configured-model",
                              "VENUE_LLM_PROVIDER": "gemini"})
    @patch("google.genai.Client")
    def test_model_uses_actual_tool_contract_and_returns_ranked_cards_unchanged(self, sdk):
        generate = sdk.return_value.__enter__.return_value.models.generate_content
        orders = [{"types": ["menu_coffee"]}, {"types": ["menu_cookie"]}]
        generate.return_value = self.gemini_response({"venue_category": "CAFE", "orders": orders})
        result = catalog_result(ranked=self.rows)
        self.mcp.recommend.return_value = result
        message = "커피랑 쿠키 있는 카페"

        response = run_venue_llm_concierge(message, region="인천 연수구", mcp_client=self.mcp)

        self.assertTrue(response["success"], response)
        self.assertEqual(response["mcp"], result)
        self.assertEqual(response["llm"]["model"], "verified-model")
        self.assertNotIn("임의 영업시간", response["llm"]["message"])
        self.mcp.recommend.assert_called_once_with({
            "region": "인천 연수구", "venue_category": "CAFE", "orders": orders,
            "limit": 5, "request_text": message,
        })
        config = generate.call_args.kwargs["config"]
        schema = config.tools[0].function_declarations[0].parameters_json_schema
        self.assertNotIn("request_text", schema["properties"])
        self.assertNotIn("query", schema["properties"])
        self.assertTrue(config.automatic_function_calling.disable)

    @patch.dict("os.environ", {"GEMINI_API_KEY": "local-test-value", "VENUE_LLM_MODEL": "configured-model",
                              "VENUE_LLM_PROVIDER": "gemini"})
    @patch("google.genai.Client")
    def test_model_dropping_social_purpose_cannot_replace_mcp_hold_with_cards(self, sdk):
        sdk.return_value.__enter__.return_value.models.generate_content.return_value = self.gemini_response({
            "region": "서교동", "venue_category": "PUB",
        })
        unverified = [{"kind": "SOCIAL_PURPOSE", "text": "헌팅술집", "evidence_status": "UNKNOWN"}]
        self.mcp.recommend.return_value = catalog_result(unverified=unverified)

        response = run_venue_llm_concierge("서교동 헌팅술집", mcp_client=self.mcp)

        self.assertTrue(response["success"], response)
        self.assertEqual(response["llm"]["status"], "NEEDS_CONFIRMATION")
        self.assertEqual(response["llm"]["interpreted_conditions"]["unverified_conditions"], unverified)
        self.assertEqual(response["mcp"]["ranked"], [])
        self.assertEqual(self.mcp.recommend.call_args.args[0]["request_text"], "서교동 헌팅술집")

    @patch.dict("os.environ", {"GEMINI_API_KEY": "local-test-value", "VENUE_LLM_MODEL": "configured-model",
                              "VENUE_LLM_PROVIDER": "gemini"})
    @patch("google.genai.Client")
    def test_auth_timeout_and_invalid_model_output_do_not_trigger_fallback(self, sdk):
        generate = sdk.return_value.__enter__.return_value.models.generate_content
        invalid_key = ClientError(400, {"error": {"details": [{"reason": "API_KEY_INVALID"}]}})
        for error, expected in ((invalid_key, "LLM_AUTH_FAILED"), (httpx.ReadTimeout("timeout"), "LLM_TIMEOUT")):
            with self.subTest(expected=expected):
                generate.side_effect = error
                response = run_venue_llm_concierge("커피", mcp_client=self.mcp)
                self.assertEqual(response["code"], expected)
                self.assertIsNone(response["mcp"])
        generate.side_effect = None
        for invalid in ({"query": "모델이 임의로 추가한 원문"}, {"region": "인천 연수구", "limit": 0}):
            generate.return_value = self.gemini_response(invalid)
            self.assertEqual(run_venue_llm_concierge("커피", mcp_client=self.mcp)["code"], "LLM_INVALID_RESPONSE")
        self.mcp.recommend.assert_not_called()

    @override_settings(LOCAL_DEMO=True)
    @patch.dict("os.environ", {"VENUE_LLM_PROVIDER": "codex_cli"})
    @patch("backend.venues.services.venue_llm_concierge.shutil.which", return_value="codex.exe")
    @patch("backend.venues.services.venue_llm_concierge.codex_model_overrides",
           return_value=["-c", 'model="existing-model"', "-c", 'model_reasoning_effort="high"'])
    @patch("backend.venues.services.venue_llm_concierge.TemporaryDirectory", return_value=nullcontext("isolated-cli-cwd"))
    @patch("backend.venues.services.venue_llm_concierge.Path.write_text")
    @patch("backend.venues.services.venue_llm_concierge.subprocess.run")
    def test_cli_interprets_the_question_and_uses_the_existing_mcp_client(self, run, write, _directory, _overrides, _which):
        self.mcp.command = ["python.exe", "venue_recommendation_mcp.py"]
        self.mcp.recommend.return_value = catalog_result(ranked=self.rows)
        auth = Mock(returncode=0, stdout="Logged in using ChatGPT", stderr="")
        plan = {"region": "인천 연수구", "venue_category": "CAFE", "limit": 2, "orders": None}
        final = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(plan)}}
        result = Mock(returncode=0, stdout=json.dumps(final))
        run.side_effect = [auth, result]

        response = run_venue_llm_concierge("인천 연수구 카페", mcp_client=self.mcp)

        self.assertTrue(response["success"], response)
        self.assertEqual(response["mcp"]["ranked"], self.rows)
        self.assertEqual(response["llm"]["mode"], "LOCAL_TEST")
        self.assertIsNone(response["llm"]["model"])
        self.assertEqual(response["llm"]["tool_calls"][0]["source"], "MCP_STDIO")
        self.assertIn('model="existing-model"', run.call_args.args[0])
        self.assertIn("--output-schema", run.call_args.args[0])
        schema = json.loads(write.call_args.args[0])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertNotIn("request_text", schema["properties"])
        self.mcp.recommend.assert_called_once_with({
            "region": "인천 연수구", "venue_category": "CAFE", "limit": 2, "request_text": "인천 연수구 카페",
        })

        self.mcp.recommend.reset_mock()
        for text in ("임의 매장과 메뉴", json.dumps({**plan, "query": "임의 조건"})):
            invalid = {**final, "item": {**final["item"], "text": text}}
            run.side_effect = [auth, Mock(returncode=0, stdout=json.dumps(invalid))]
            response = run_venue_llm_concierge("인천 연수구 카페", mcp_client=self.mcp)
            self.assertEqual(response["code"], "LLM_INVALID_RESPONSE")
            self.assertIsNone(response["mcp"])
        unexpected = {"type": "item.completed", "item": {"type": "mcp_tool_call"}}
        run.side_effect = [auth, Mock(returncode=0, stdout=json.dumps(final) + "\n" + json.dumps(unexpected))]
        self.assertEqual(run_venue_llm_concierge("인천 연수구 카페", mcp_client=self.mcp)["code"], "LLM_INVALID_RESPONSE")
        self.mcp.recommend.assert_not_called()
        run.side_effect = [auth, TimeoutExpired("codex", 45)]
        response = run_venue_llm_concierge("인천 연수구 카페", mcp_client=self.mcp)
        self.assertEqual(response["code"], "LLM_TIMEOUT")
        self.assertIsNone(response["mcp"])
        run.side_effect = [auth, result]
        self.assertTrue(run_venue_llm_concierge("인천 연수구 카페", mcp_client=self.mcp)["success"])
