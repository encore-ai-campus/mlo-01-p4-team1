"""Django ASGI host for the existing read-only venue MCP server."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import json
import logging
import os
import subprocess
import sys
from threading import Lock

from django.conf import settings


logger = logging.getLogger(__name__)
PROTOCOL_VERSION = "2025-11-25"
CALL_TIMEOUT = 12


class VenueMcpUnavailable(RuntimeError):
    pass


class VenueMcpRequestError(ValueError):
    pass


def exchange(process, message):
    """Send one MCP message and read its reply on the owning I/O thread."""
    process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    process.stdin.flush()
    if "id" not in message:
        return None

    for line in process.stdout:
        reply = json.loads(line)
        if not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0":
            break
        if "id" not in reply and "method" in reply:
            continue
        if reply.get("id") == message["id"] and isinstance(reply.get("result"), dict):
            return reply["result"]
        break
    raise VenueMcpUnavailable("MCP reply missing or invalid")


class VenueMcpClient:
    def __init__(self, command):
        self.command = command
        self.process = None
        self.request_id = 0
        self.lock = Lock()
        self.io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="venue-mcp")

    def send(self, method, params=None, *, notification=False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notification:
            self.request_id += 1
            message["id"] = self.request_id
        return self.io.submit(exchange, self.process, message).result(timeout=CALL_TIMEOUT)

    def start(self):
        environment = {
            name: os.environ[name]
            for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if name in os.environ
        }
        environment.update(NO_PROXY="localhost,127.0.0.1", PYTHONUTF8="1")
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            encoding="utf-8",
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        initialized = self.send("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "django-venue-recommendation", "version": "0.2"},
        })
        server_info = initialized.get("serverInfo")
        if (
            initialized.get("protocolVersion") != PROTOCOL_VERSION
            or not isinstance(server_info, dict)
            or server_info.get("name") != "venue-recommendation"
        ):
            raise VenueMcpUnavailable("Unexpected MCP server or protocol version")
        self.send("notifications/initialized", notification=True)
        logger.info("Venue MCP stdio ready (pid=%s)", self.process.pid)

    def stop(self, *, abort=False):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if not abort:
                with suppress(OSError):
                    process.stdin.close()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    process.kill()
                process.wait(timeout=3)
        finally:
            with suppress(OSError):
                process.stdin.close()
            with suppress(OSError):
                process.stdout.close()

    def list_tags(self):
        return self._call_tool("list_venue_tags", {})

    def list_tool_specs(self):
        return self._request("tools/list", {}, "tools", list)

    def recommend(self, arguments):
        return self._call_tool("recommend_venues", arguments)

    def _call_tool(self, name, arguments):
        return self._request("tools/call", {"name": name, "arguments": arguments}, "structuredContent", dict)

    def _request(self, method, params, result_key, result_type):
        if not self.lock.acquire(timeout=CALL_TIMEOUT):
            raise VenueMcpUnavailable("Venue MCP is busy")
        try:
            if self.process is None or self.process.poll() is not None:
                self.stop(abort=True)
                self.start()
            result = self.send(method, params)
            if result.get("isError"):
                raise VenueMcpRequestError("MCP could not process the request")
            content = result.get(result_key)
            if not isinstance(content, result_type):
                raise VenueMcpUnavailable("MCP tool response is missing")
            return content
        except VenueMcpRequestError:
            raise
        except (OSError, ValueError, TimeoutError, VenueMcpUnavailable) as error:
            logger.warning("Venue MCP stdio failed (%s)", type(error).__name__)
            self.stop(abort=True)
            raise VenueMcpUnavailable("Venue MCP connection failed") from None
        finally:
            self.lock.release()

    def close(self):
        with self.lock:
            try:
                self.stop()
            finally:
                self.io.shutdown()


class VenueMcpApplication:
    """Own one client for the ASGI worker and make it available to Django views."""

    def __init__(self, application, command):
        self.application = application
        self.command = command
        self.client = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await receive()
            client = VenueMcpClient(self.command)
            self.client = client
            try:
                await send({"type": "lifespan.startup.complete"})
                await receive()
            finally:
                self.client = None
                await asyncio.to_thread(client.close)
            await send({"type": "lifespan.shutdown.complete"})
            return

        if scope["type"] == "http" and self.client is not None:
            scope = {**scope, "venue_mcp_client": self.client}
        await self.application(scope, receive, send)


def with_local_venue_mcp(application):
    server = os.environ.get("VENUE_MCP_SERVER")
    if not server:
        return application

    # Production execution must be explicitly configured and must not use the
    # local fixtures or permit preview-only catalog snapshots. This only enables
    # the host; request authentication and platform readiness remain separate.
    local_demo = settings.LOCAL_DEMO
    if not local_demo and os.environ.get("VENUE_MCP_SERVICE_ENABLED") != "1":
        return application
    public_catalog = os.environ.get("VENUE_MCP_PUBLIC_CATALOG")
    if not local_demo and not public_catalog:
        logger.warning("Venue MCP disabled: published catalog is not configured")
        return application

    command = [os.environ.get("VENUE_MCP_PYTHON") or sys.executable, "-B", "-X", "utf8", server]
    if public_catalog:
        command.extend(("--public-catalog", public_catalog))
        if local_demo:
            command.append("--allow-preview-catalog")
    else:
        for variable, option in (
            ("VENUE_MCP_CATALOG", "--catalog"),
            ("VENUE_MCP_LOCATIONS", "--locations"),
            ("VENUE_MCP_VENUES_CSV", "--venues-csv"),
        ):
            value = os.environ.get(variable)
            if value:
                command.extend((option, value))
    if weather_url := os.environ.get("VENUE_WEATHER_BASE_URL"):
        command.extend(("--weather-base-url", weather_url))
    return VenueMcpApplication(application, command)
