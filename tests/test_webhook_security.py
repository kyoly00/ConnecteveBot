"""webhook_security · health endpoint 단위 테스트."""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.core.settings import Settings
from app.services.webhook_security import (
    verify_confluence_webhook_request,
    verify_graph_notification_payload,
)


def _make_request(
    *,
    method: str = "POST",
    path: str = "/webhook-endpoint",
    headers: dict | None = None,
    query: str = "",
    body: bytes = b"{}",
) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query.encode("latin-1"),
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
        "client": ("203.0.113.10", 12345),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class TestConfluenceWebhookSecurity(unittest.TestCase):
    def test_prod_rejects_without_secret(self):
        settings = Settings(
            ENVIRONMENT="production",
            CONFLUENCE_WEBHOOK_SECRET="",
        )
        request = _make_request()
        with self.assertRaises(HTTPException) as ctx:
            verify_confluence_webhook_request(request, b"{}", settings)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_plain_secret_header_ok(self):
        settings = Settings(
            ENVIRONMENT="production",
            CONFLUENCE_WEBHOOK_SECRET="topsecret",
        )
        request = _make_request(
            headers={"X-Confluence-Webhook-Secret": "topsecret"},
        )
        verify_confluence_webhook_request(request, b"{}", settings)

    def test_hmac_signature_ok(self):
        secret = "hmac-secret"
        body = b'{"page":{}}'
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        settings = Settings(
            ENVIRONMENT="production",
            CONFLUENCE_WEBHOOK_SECRET=secret,
        )
        request = _make_request(
            headers={"X-Hub-Signature-256": f"sha256={digest}"},
            body=body,
        )
        verify_confluence_webhook_request(request, body, settings)

    def test_ip_allowlist_blocks(self):
        settings = Settings(
            ENVIRONMENT="production",
            CONFLUENCE_WEBHOOK_SECRET="s",
            CONFLUENCE_WEBHOOK_ALLOWED_IPS="198.51.100.0/24",
        )
        request = _make_request(headers={"X-Confluence-Webhook-Secret": "s"})
        with self.assertRaises(HTTPException) as ctx:
            verify_confluence_webhook_request(request, b"{}", settings)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_dev_skips_when_auth_disabled(self):
        settings = Settings(
            ENVIRONMENT="development",
            CONFLUENCE_WEBHOOK_REQUIRE_AUTH=False,
            CONFLUENCE_WEBHOOK_SECRET="",
        )
        verify_confluence_webhook_request(_make_request(), b"{}", settings)


class TestGraphWebhookSecurity(unittest.TestCase):
    def test_requires_client_state_config(self):
        settings = Settings(OUTLOOK_ROOM_WEBHOOK_CLIENT_STATE="")
        with self.assertRaises(HTTPException) as ctx:
            verify_graph_notification_payload({"value": [{}]}, settings)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_rejects_bad_client_state(self):
        settings = Settings(OUTLOOK_ROOM_WEBHOOK_CLIENT_STATE="expected")
        payload = {"value": [{"clientState": "wrong", "id": "1"}]}
        with self.assertRaises(HTTPException) as ctx:
            verify_graph_notification_payload(payload, settings)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_accepts_matching_notifications(self):
        settings = Settings(OUTLOOK_ROOM_WEBHOOK_CLIENT_STATE="expected")
        payload = {
            "value": [
                {"clientState": "expected", "id": "a"},
                {"clientState": "expected", "id": "b"},
            ],
        }
        out = verify_graph_notification_payload(payload, settings)
        self.assertEqual(len(out), 2)


class TestHealthRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_healthz(self):
        from app.api.routes.health import healthz

        self.assertEqual(await healthz(), {"status": "ok"})

    async def test_readyz_not_ready_without_config(self):
        from app.api.routes.health import readyz

        settings = Settings(
            DATABASE_URL=None,
            SLACK_SIGNING_SECRET="",
            OUTLOOK_ROOM_WEBHOOK_CLIENT_STATE="",
            CONFLUENCE_WEBHOOK_SECRET="",
        )
        with patch("app.api.routes.health._database_live", new_callable=AsyncMock, return_value=False):
            result = await readyz(settings)
        self.assertEqual(result["status"], "not_ready")
        self.assertIn("database_url_configured", result["failed"])


if __name__ == "__main__":
    unittest.main()
