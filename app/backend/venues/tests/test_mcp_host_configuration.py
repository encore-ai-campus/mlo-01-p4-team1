"""Check that an operational MCP host cannot silently use preview data."""

import os
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from backend.venues.services.mcp_stdio import VenueMcpApplication, with_local_venue_mcp


class McpHostConfigurationTests(SimpleTestCase):
    def setUp(self):
        self.application = object()

    @override_settings(LOCAL_DEMO=False)
    def test_operational_host_is_disabled_without_explicit_opt_in(self):
        with patch.dict(os.environ, {"VENUE_MCP_SERVER": "server.py", "VENUE_MCP_PUBLIC_CATALOG": "catalog.json"}, clear=True):
            self.assertIs(with_local_venue_mcp(self.application), self.application)

    @override_settings(LOCAL_DEMO=False)
    def test_operational_host_requires_a_public_catalog(self):
        with patch.dict(os.environ, {"VENUE_MCP_SERVER": "server.py", "VENUE_MCP_SERVICE_ENABLED": "1", "VENUE_MCP_CATALOG": "fixture.json"}, clear=True):
            self.assertIs(with_local_venue_mcp(self.application), self.application)

    @override_settings(LOCAL_DEMO=False)
    def test_operational_host_does_not_enable_preview_data(self):
        env = {"VENUE_MCP_SERVER": "server.py", "VENUE_MCP_SERVICE_ENABLED": "1", "VENUE_MCP_PUBLIC_CATALOG": "published.json", "VENUE_MCP_CATALOG": "fixture.json"}
        with patch.dict(os.environ, env, clear=True):
            host = with_local_venue_mcp(self.application)
        self.assertIsInstance(host, VenueMcpApplication)
        self.assertIn("published.json", host.command)
        self.assertNotIn("--allow-preview-catalog", host.command)
        self.assertNotIn("fixture.json", host.command)

    @override_settings(LOCAL_DEMO=True)
    def test_local_preview_keeps_its_existing_behavior(self):
        env = {"VENUE_MCP_SERVER": "server.py", "VENUE_MCP_PUBLIC_CATALOG": "preview.json"}
        with patch.dict(os.environ, env, clear=True):
            host = with_local_venue_mcp(self.application)
        self.assertIsInstance(host, VenueMcpApplication)
        self.assertIn("--allow-preview-catalog", host.command)
