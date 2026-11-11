"""Interpret one request with an LLM and return cards from the venue MCP."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
import tomllib

import httpx
from django.conf import settings
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .mcp_stdio import VenueMcpRequestError, VenueMcpUnavailable


TOOL_NAME = "recommend_venues"
CLI_TIMEOUT_SECONDS = 45
CLI_SLOTS = BoundedSemaphore(1)
MAX_CLI_OUTPUT = 2_000_000
KST = timezone(timedelta(hours=9))
ERROR_MESSAGES = {
    "INVALID_LLM_REQUEST": "매장 추천 질문을 240자 이내로 입력해 주세요.",
    "LLM_NOT_CONFIGURED": "매장 추천 LLM 설정을 확인해 주세요.",
    "LLM_AUTH_FAILED": "매장 추천 LLM 인증을 확인해 주세요.",
    "LLM_TIMEOUT": "매장 추천 LLM 응답 시간이 초과됐습니다.",
    "LLM_BUSY": "매장 추천 LLM이 사용 중입니다. 잠시 후 다시 시도해 주세요.",
    "LLM_PROVIDER_ERROR": "매장 추천 LLM 호출에 실패했습니다.",
    "LLM_INVALID_RESPONSE": "LLM이 전달한 추천 조건을 확인할 수 없습니다.",
    "VENUE_MCP_UNAVAILABLE": "매장 추천 MCP에 연결할 수 없습니다.",
    "MCP_REQUEST_REJECTED": "매장 추천 MCP가 해석된 조건을 처리하지 못했습니다.",
    "MCP_INVALID_RESULT": "매장 추천 MCP 응답을 확인할 수 없습니다.",
}
LLM_ERROR_HTTP_STATUS = {
    "INVALID_LLM_REQUEST": 400,
    "LLM_NOT_CONFIGURED": 503,
    "LLM_AUTH_FAILED": 503,
    "LLM_TIMEOUT": 504,
    "LLM_BUSY": 503,
    "LLM_PROVIDER_ERROR": 502,
    "LLM_INVALID_RESPONSE": 502,
    "VENUE_MCP_UNAVAILABLE": 503,
    "MCP_REQUEST_REJECTED": 502,
    "MCP_INVALID_RESULT": 502,
}
NO_MATCH_MESSAGES = {
    "NO_REGISTERED_VENUES": "현재 매장 자료에 이 지역의 매장이 없습니다.",
    "NO_NEARBY_VENUES": "지정한 장소 주변에 등록된 매장이 없습니다.",
    "NO_CATEGORY": "이 지역에서 요청한 업종의 매장을 찾지 못했습니다.",
    "NO_MOOD": "요청한 분위기 태그를 갖춘 매장을 찾지 못했습니다.",
    "NO_MENU": "요청한 메뉴 조건을 모두 충족하는 매장을 찾지 못했습니다.",
    "NO_BUDGET": "확인된 1인 예산이 요청 범위에 드는 매장을 찾지 못했습니다.",
    "REGION_UNSUPPORTED": "현재 매장 자료에서 검색할 수 있는 지역을 입력해 주세요.",
    "MULTIPLE_LOCATIONS": "검색할 지역을 한 곳으로 알려 주세요.",
    "LOCATION_CONFLICT": "시·구·동이 서로 일치하는지 확인해 주세요.",
    "STATION_REQUIRED": "출구 번호와 함께 역 이름을 알려 주세요.",
    "STATION_UNSUPPORTED": "현재 검색할 수 있는 역 이름을 확인해 주세요.",
    "EXIT_UNSUPPORTED": "이 역에서 조회할 수 있는 출구 번호를 확인해 주세요.",
    "PLACE_UNSUPPORTED": "장소 이름이나 가까운 역을 확인해 주세요.",
}


class VenueLlmError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VenueToolCall:
    name: str
    arguments: dict
    result: dict | None = None
    model: str | None = None


def instructions(region):
    context = f"화면에서 선택한 기본 지역은 {region!r}입니다." if region else "기본 지역 지정이 없습니다."
    return (
        f"한국시간 현재 {datetime.now(KST).isoformat(timespec='minutes')}. {context} "
        "사용자 질문을 구조화해 recommend_venues를 정확히 한 번 호출하세요. "
        "질문의 지역이 화면의 기본 지역보다 우선합니다. query와 request_text는 보내지 마세요. "
        "질문에 나온 방문 시각만 UTC 오프셋이 붙은 ISO 8601로 전달하세요. "
        "서로 다른 메뉴는 orders의 별개 항목에 넣으세요. 공통 태그 이름이나 ID를 사용하세요. "
        "제외 조건과 메뉴 중 하나만 고르는 조건을 임의로 긍정 조건으로 바꾸지 마세요. "
        "예산 상한·하한이나 필수 범위는 budget_mode REQUIRE, 대략 예산은 PREFER를 사용하세요. "
        "필수 분위기와 선호 분위기를 각각 required_moods, preferred_moods로 전달하세요. "
        "잔잔한 술집, 조용한 술집은 PUB과 필수 mood_calm입니다. 아늑함을 차분함으로 대신하지 마세요. "
        "기본 limit은 5이며 요청에 명시된 수는 도구의 범위 내에서 반영하세요. "
        "요청자가 요구한 매장 조건 중 도구로 확인할 수 없는 사회적 목적, 좌석·편의·접근성, "
        "식이 제한, 영업시간 등은 unverified_conditions에 원문의 표현과 종류를 보존하세요. "
        "만남이나 회식 전 커피처럼 방문 배경만 말한 경우에는 이를 매장 조건으로 추가하지 마세요. "
        "헌팅, 소개팅, 낯선 사람과 어울리기 같은 조건을 일반 업종이나 활기찬 분위기로 바꾸지 마세요. "
        "매장·메뉴·가격·근거는 도구 응답만 사용하세요. 파일, 셸, 웹은 사용하지 마세요."
    )


class GeminiVenueProvider:
    name = "gemini"
    mode = "API"

    def __init__(self):
        self.model = os.environ.get("VENUE_LLM_MODEL", "").strip() or None
        self.api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.timeout_ms = min(settings.GEMINI_TIMEOUT_MS or 8000, 30_000)
        if not settings.GEMINI_ENABLED or not self.api_key or not self.model or self.timeout_ms <= 0:
            raise VenueLlmError("LLM_NOT_CONFIGURED")

    def call(self, message, region, schema):
        try:
            from google import genai
            from google.genai import errors, types
        except ImportError:
            raise VenueLlmError("LLM_NOT_CONFIGURED") from None

        try:
            with genai.Client(
                api_key=self.api_key, http_options={"timeout": self.timeout_ms, "retry_options": {"attempts": 1}},
            ) as client:
                response = client.models.generate_content(
                    model=self.model,
                    contents=message,
                    config=types.GenerateContentConfig(
                        system_instruction=instructions(region),
                        tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
                            name=TOOL_NAME, description="사용자가 지정한 조건을 팀 매장 카탈로그로 검증합니다.",
                            parameters_json_schema=schema,
                        )])],
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                            mode="ANY", allowed_function_names=[TOOL_NAME],
                        )),
                        max_output_tokens=2048,
                    ),
                )
            calls = response.function_calls or []
        except (TimeoutError, httpx.TimeoutException):
            raise VenueLlmError("LLM_TIMEOUT") from None
        except errors.APIError as exc:
            error = exc.details.get("error") if isinstance(exc.details, dict) else None
            details = error.get("details") if isinstance(error, dict) else None
            details = details if isinstance(details, list) else []
            invalid_key = any(isinstance(item, dict) and item.get("reason") == "API_KEY_INVALID" for item in details)
            if exc.code in (401, 403) or invalid_key:
                code = "LLM_AUTH_FAILED"
            elif exc.code in (408, 504) or exc.status == "DEADLINE_EXCEEDED":
                code = "LLM_TIMEOUT"
            elif exc.code in (429, 503):
                code = "LLM_BUSY"
            else:
                code = "LLM_PROVIDER_ERROR"
            raise VenueLlmError(code) from None
        except (ValueError, OSError, httpx.HTTPError):
            raise VenueLlmError("LLM_PROVIDER_ERROR") from None
        if len(calls) != 1:
            raise VenueLlmError("LLM_INVALID_RESPONSE")
        return VenueToolCall(calls[0].name, calls[0].args, model=response.model_version or self.model)


def codex_model_overrides():
    """Preserve model selection while keeping other user config disabled."""
    try:
        home = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else Path.home() / ".codex"
        with (home / "config.toml").open("rb") as config_file:
            config = tomllib.load(config_file)
    except (OSError, RuntimeError, ValueError):
        return []
    overrides = []
    for key in ("model", "model_reasoning_effort"):
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            overrides.extend(("-c", f"{key}={json.dumps(value)}"))
    return overrides


def cli_events(stdout):
    if len(stdout) > MAX_CLI_OUTPUT:
        raise VenueLlmError("LLM_INVALID_RESPONSE")
    try:
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        if any(not isinstance(event, dict) for event in events):
            raise ValueError("CLI event must be an object")
        items = [event["item"] for event in events if event.get("type") == "item.completed"]
        if any(item.get("type") in {"command_execution", "file_change", "web_search"} for item in items):
            raise ValueError("CLI used a different tool")
        calls = [item for item in items if item.get("type") == "mcp_tool_call"]
        call, = calls
        if call.get("server") != "venue" or call.get("tool") != TOOL_NAME:
            raise ValueError("Unexpected CLI MCP event")
        if call.get("status") == "failed":
            raise VenueLlmError("MCP_REQUEST_REJECTED")
        if call.get("status") != "completed":
            raise ValueError("Unexpected CLI MCP event")
        result = call["result"]
        if result.get("is_error") or result.get("isError"):
            raise VenueLlmError("MCP_REQUEST_REJECTED")
        structured = result.get("structured_content", result.get("structuredContent"))
        if structured is None:
            # Older CLI events may wrap the MCP result in a text content block.
            block, = result["content"]
            if block.get("type") != "text":
                raise ValueError("Unexpected MCP content")
            structured = json.loads(block["text"])
        if not isinstance(structured, dict) or not isinstance(call["arguments"], dict):
            raise ValueError("Unexpected MCP argument or result shape")
    except (ValueError, TypeError, KeyError, AttributeError):
        raise VenueLlmError("LLM_INVALID_RESPONSE") from None
    # Some CLI versions emit no model metadata; leave it unset in that case.
    model = next((event["model"] for event in reversed(events)
                  if event.get("type") in {"turn.started", "turn.completed"}
                  and isinstance(event.get("model"), str) and event["model"]), None)
    return VenueToolCall(TOOL_NAME, call["arguments"], structured, model)


class CodexCliVenueProvider:
    name = "codex_cli"
    mode = "LOCAL_TEST"
    # The CLI selects the account's available model. Its JSON events need not report a model ID.
    model = None

    def __init__(self, mcp_client):
        self.executable = shutil.which(os.environ.get("VENUE_CODEX_CLI") or ("codex.exe" if os.name == "nt" else "codex"))
        self.mcp_command = getattr(mcp_client, "command", None)
        if not settings.LOCAL_DEMO or not self.executable or not self.mcp_command:
            raise VenueLlmError("LLM_NOT_CONFIGURED")

    def call(self, message, region, schema):
        if not CLI_SLOTS.acquire(blocking=False):
            raise VenueLlmError("LLM_BUSY")
        try:
            return self._call(message, region)
        finally:
            CLI_SLOTS.release()

    def _call(self, message, region):
        # Preserve the existing local ChatGPT login without forwarding API keys to the CLI or MCP child.
        environment = {
            name: value for name, value in os.environ.items() if name.upper() in {
                "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "TEMP", "TMP",
                "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME", "CODEX_HOME", "PROGRAMFILES",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
            }
        }
        environment["VENUE_MCP_REQUEST_TEXT"] = message
        process_options = {
            "capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
            "env": environment, "shell": False, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
        executable, *mcp_args = self.mcp_command
        try:
            with TemporaryDirectory(prefix="venue-codex-cli-") as directory:
                auth = subprocess.run(
                    [self.executable, "login", "status"], cwd=directory, timeout=5, **process_options,
                )
                if auth.returncode or "Logged in using ChatGPT" not in auth.stdout + auth.stderr:
                    raise VenueLlmError("LLM_AUTH_FAILED")
                result = subprocess.run([
                    self.executable, "exec", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                    "--json", "--color", "never", "--sandbox", "read-only", "--cd", directory,
                    *codex_model_overrides(),
                    "-c", 'forced_login_method="chatgpt"', "-c", 'approval_policy="never"',
                    "-c", "features.shell_tool=false", "-c", "features.apps=false",
                    "-c", "features.view_image=false", "-c", "features.skill_mcp_dependency_install=false",
                    "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0",
                    "-c", f"mcp_servers.venue.command={json.dumps(executable)}",
                    "-c", f"mcp_servers.venue.args={json.dumps(mcp_args)}",
                    "-c", f'mcp_servers.venue.enabled_tools=["{TOOL_NAME}"]',
                    "-c", 'mcp_servers.venue.env_vars=["VENUE_MCP_REQUEST_TEXT"]',
                    "-c", 'mcp_servers.venue.env={PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NO_PROXY="127.0.0.1,localhost"}',
                    "-c", "mcp_servers.venue.required=true", "-c", "mcp_servers.venue.startup_timeout_sec=12",
                    "-c", "mcp_servers.venue.tool_timeout_sec=15", "-",
                ], input=instructions(region) + "\n\n사용자 질문:\n" + message,
                    cwd=directory, timeout=CLI_TIMEOUT_SECONDS, **process_options)
        except subprocess.TimeoutExpired:
            raise VenueLlmError("LLM_TIMEOUT") from None
        except OSError:
            raise VenueLlmError("LLM_PROVIDER_ERROR") from None
        if result.returncode:
            diagnostic = (result.stdout + result.stderr).casefold()
            if "401 unauthorized" in diagnostic or "invalid access token" in diagnostic:
                raise VenueLlmError("LLM_AUTH_FAILED")
            if "rate limit" in diagnostic or "429 too many requests" in diagnostic:
                raise VenueLlmError("LLM_BUSY")
            raise VenueLlmError("LLM_PROVIDER_ERROR")
        return cli_events(result.stdout)


def tool_schema(mcp_client):
    specs = mcp_client.list_tool_specs()
    try:
        spec, = [item for item in specs if item.get("name") == TOOL_NAME]
        schema = deepcopy(spec["inputSchema"])
        properties = schema["properties"]
        if not {"unverified_conditions", "request_text"} <= properties.keys():
            raise ValueError("MCP intent guard is missing")
        properties.pop("request_text")
        properties.pop("query", None)
        schema["additionalProperties"] = False
        Draft202012Validator.check_schema(schema)
        return schema
    except (AttributeError, KeyError, SchemaError, TypeError, ValueError):
        raise VenueLlmError("VENUE_MCP_UNAVAILABLE") from None


def validate_result(result, arguments):
    if not isinstance(result, dict) or result.get("status") not in ("OK", "NO_MATCH", "NEEDS_CHECK"):
        raise VenueLlmError("MCP_INVALID_RESULT")
    if "empty_reason" in result and not isinstance(result["empty_reason"], str):
        raise VenueLlmError("MCP_INVALID_RESULT")
    rows, counts = result.get("ranked"), result.get("counts")
    if (
        not isinstance(rows, list) or not isinstance(counts, dict) or type(counts.get("ranked")) is not int
        or not len(rows) <= counts["ranked"] or len(rows) > arguments.get("limit", 10)
        or bool(rows) != (result["status"] == "OK") or not isinstance(result.get("request"), dict)
        or bool(counts["ranked"]) != bool(rows)
        or any(not isinstance(result.get(bucket), list) for bucket in ("pending", "excluded"))
    ):
        raise VenueLlmError("MCP_INVALID_RESULT")
    if any(
        not isinstance(row, dict) or not isinstance(row.get("venue_id"), str)
        or not isinstance(row.get("name"), str) or not isinstance(row.get("address"), str)
        or not isinstance(row.get("matched_moods"), list) or not isinstance(row.get("matched_orders"), list)
        or not isinstance(row.get("forecast"), dict)
        for row in rows
    ):
        raise VenueLlmError("MCP_INVALID_RESULT")
    if arguments.get("unverified_conditions") or result.get("empty_reason") == "CONDITION_UNVERIFIED":
        conditions = result.get("unverified_conditions")
        if (
            result["status"] != "NEEDS_CHECK" or result.get("empty_reason") != "CONDITION_UNVERIFIED"
            or not isinstance(conditions, list) or not conditions or result["pending"] or result["excluded"]
            or any(not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip()
                   or item.get("evidence_status") != "UNKNOWN" for item in conditions)
        ):
            raise VenueLlmError("MCP_INVALID_RESULT")


def run_venue_llm_concierge(message, *, mcp_client, region=None, provider=None):
    """Return the recommendation tool result and a trace the UI can show."""
    provider_name = provider.name if provider else os.environ.get("VENUE_LLM_PROVIDER", "gemini").strip()
    llm = {
        "status": "ERROR", "provider": provider_name,
        "mode": "LOCAL_TEST" if provider_name == "codex_cli" else "API",
        "model": getattr(provider, "model", None), "interpreted_conditions": {}, "tool_calls": [],
    }
    try:
        if not isinstance(message, str) or not 0 < len(message.strip()) <= 240:
            raise VenueLlmError("INVALID_LLM_REQUEST")
        if mcp_client is None:
            raise VenueLlmError("VENUE_MCP_UNAVAILABLE")
        if region is not None and (not isinstance(region, str) or not 0 < len(region.strip()) <= 80):
            raise VenueLlmError("INVALID_LLM_REQUEST")
        if provider is None:
            if provider_name == "codex_cli":
                provider = CodexCliVenueProvider(mcp_client)
            elif provider_name == "gemini":
                provider = GeminiVenueProvider()
            else:
                raise VenueLlmError("LLM_NOT_CONFIGURED")
        schema = tool_schema(mcp_client)
        plan = provider.call(message.strip(), region, schema)
        if (
            not isinstance(plan, VenueToolCall) or plan.name != TOOL_NAME or not isinstance(plan.arguments, dict)
            or not Draft202012Validator(schema).is_valid(plan.arguments)
            or (provider_name == "codex_cli" and plan.result is None)
        ):
            raise VenueLlmError("LLM_INVALID_RESPONSE")

        arguments = dict(plan.arguments)
        if plan.result is None:
            if region and not arguments.get("region"):
                arguments["region"] = region
            arguments.setdefault("limit", 5)
        if not any(arguments.get(key) for key in (
            "region", "venue_category", "orders", "budget", "required_moods", "preferred_moods", "unverified_conditions",
        )):
            raise VenueLlmError("LLM_INVALID_RESPONSE")
        result = plan.result if plan.result is not None else mcp_client.recommend({
            **arguments, "request_text": message.strip(),
        })
        validate_result(result, arguments)
    except VenueMcpRequestError:
        code = "MCP_REQUEST_REJECTED"
    except VenueMcpUnavailable:
        code = "VENUE_MCP_UNAVAILABLE"
    except VenueLlmError as exc:
        code = exc.code
    else:
        llm["model"] = plan.model
        interpreted = arguments.copy()
        for key in ("region", "venue_category", "required_moods", "preferred_moods", "budget", "budget_mode", "orders"):
            if key in result["request"]:
                value = result["request"][key]
                if value:
                    interpreted[key] = value
                else:
                    interpreted.pop(key, None)
        llm["interpreted_conditions"] = interpreted
        llm["tool_calls"].append({
            "name": TOOL_NAME, "arguments": arguments,
            "source": "CODEX_CLI_MCP_EVENT" if plan.result is not None else "MCP_STDIO",
        })
        if result["status"] == "NEEDS_CHECK":
            llm["status"] = "NEEDS_CONFIRMATION"
            if result.get("empty_reason") == "CONDITION_UNVERIFIED":
                llm["interpreted_conditions"]["unverified_conditions"] = result["unverified_conditions"]
                llm["message"] = "매장 자료에서 이 조건의 근거를 확인할 수 없습니다. 조건을 확인해 주세요."
            else:
                llm["message"] = "검색할 지역을 확인해 주세요."
        elif result["status"] == "NO_MATCH":
            llm["status"] = "OK"
            reason = result.get("empty_reason")
            llm["message"] = NO_MATCH_MESSAGES.get(reason, "조건에 맞는 매장을 찾지 못했습니다.")
        else:
            llm["status"] = "OK"
            llm["message"] = "조건에 맞는 매장을 찾았습니다."
        return {"success": True, "mcp": result, "llm": llm}

    return {"success": False, "code": code, "mcp": None, "llm": {**llm, "message": ERROR_MESSAGES[code]}}
