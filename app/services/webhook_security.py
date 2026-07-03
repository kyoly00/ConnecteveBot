"""
Webhook 인증 — Confluence(shared secret / IP / HMAC), Graph(clientState).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
from typing import Any

from fastapi import HTTPException, Request

from app.core.settings import Settings

logger = logging.getLogger(__name__)

_CONFLUENCE_SECRET_HEADERS = (
    "x-confluence-webhook-secret",
    "x-webhook-secret",
    "x-atlassian-webhook-secret",
)
_SIGNATURE_HEADERS = (
    "x-hub-signature-256",
    "x-hub-signature",
    "x-atlassian-webhook-signature",
)


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return ""


def _ip_allowed(client_ip: str, allowed: list[str]) -> bool:
    if not allowed or not client_ip:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowed:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def _verify_hmac_signature(body: bytes, secret: str, signature_header: str) -> bool:
    header = (signature_header or "").strip()
    if not header or not secret:
        return False
    algo, _, digest = header.partition("=")
    algo = algo.lower().strip()
    digest = digest.strip()
    if algo not in ("sha256", "sha1"):
        return False
    hashmod = hashlib.sha256 if algo == "sha256" else hashlib.sha1
    expected = hmac.new(
        secret.encode("utf-8"),
        body,
        hashmod,
    ).hexdigest()
    return hmac.compare_digest(expected, digest)


def _plain_secret_match(request: Request, secret: str) -> bool:
    if not secret:
        return False
    query_secret = (request.query_params.get("secret") or "").strip()
    if query_secret and hmac.compare_digest(query_secret, secret):
        return True
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token and hmac.compare_digest(token, secret):
            return True
    for header_name in _CONFLUENCE_SECRET_HEADERS:
        value = (request.headers.get(header_name) or "").strip()
        if value and hmac.compare_digest(value, secret):
            return True
    return False


def verify_confluence_webhook_request(
    request: Request,
    body: bytes,
    settings: Settings,
) -> None:
    """
    Confluence POST /webhook-endpoint 인증.

    production 또는 CONFLUENCE_WEBHOOK_REQUIRE_AUTH=true:
      - CONFLUENCE_WEBHOOK_SECRET (plain header/query/bearer 또는 HMAC) 필수
      - CONFLUENCE_WEBHOOK_ALLOWED_IPS 설정 시 IP도 통과해야 함
    """
    require_auth = settings.CONFLUENCE_WEBHOOK_REQUIRE_AUTH or settings.is_production
    if not require_auth:
        return

    secret = settings.CONFLUENCE_WEBHOOK_SECRET
    allowed_ips = settings.confluence_allowed_ip_list

    if not secret:
        logger.warning("[ConfluenceWebhook] CONFLUENCE_WEBHOOK_SECRET 미설정")
        raise HTTPException(status_code=503, detail="webhook auth not configured")

    secret_ok = _plain_secret_match(request, secret)
    if not secret_ok:
        for header_name in _SIGNATURE_HEADERS:
            sig = request.headers.get(header_name) or ""
            if sig and _verify_hmac_signature(body, secret, sig):
                secret_ok = True
                break

    if not secret_ok:
        logger.warning(
            "[ConfluenceWebhook] 인증 실패 ip=%s",
            _client_ip(request),
        )
        raise HTTPException(status_code=401, detail="invalid confluence webhook credentials")

    if allowed_ips and not _ip_allowed(_client_ip(request), allowed_ips):
        logger.warning(
            "[ConfluenceWebhook] IP 거부 ip=%s allowed=%s",
            _client_ip(request),
            allowed_ips,
        )
        raise HTTPException(status_code=403, detail="confluence webhook ip not allowed")


def verify_graph_notification_payload(
    payload: dict[str, Any],
    settings: Settings,
) -> list[dict[str, Any]]:
    """
    Graph change notification POST 본문 검증.

  - OUTLOOK_ROOM_WEBHOOK_CLIENT_STATE 필수
  - validationToken 핸들러를 제외한 POST는 notifications 전부 clientState 일치 필요
    """
    expected_state = settings.OUTLOOK_ROOM_WEBHOOK_CLIENT_STATE
    if not expected_state:
        logger.error("[OutlookRoomWebhook] OUTLOOK_ROOM_WEBHOOK_CLIENT_STATE 미설정")
        raise HTTPException(status_code=503, detail="graph webhook client state not configured")

    notifications = payload.get("value") or []
    if not isinstance(notifications, list) or not notifications:
        raise HTTPException(status_code=400, detail="empty graph notification payload")

    accepted: list[dict[str, Any]] = []
    for notification in notifications:
        if not isinstance(notification, dict):
            raise HTTPException(status_code=400, detail="invalid graph notification item")
        if notification.get("clientState") != expected_state:
            logger.warning("[OutlookRoomWebhook] clientState mismatch")
            raise HTTPException(status_code=401, detail="invalid graph client state")
        accepted.append(notification)
    return accepted
