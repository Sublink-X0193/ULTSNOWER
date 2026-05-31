from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx


class BridgeClientError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


class BridgeClient:
    """HMAC-signed client for the central Device Control Bridge Merchant API."""

    def __init__(self, base_url: str, merchant_key: str, merchant_secret: str, *, timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.merchant_key = merchant_key
        self.merchant_secret = merchant_secret
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def _headers(self, method: str, path: str, raw: bytes, query: str, idem: str | None) -> dict[str, str]:
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        body_sha = hashlib.sha256(raw).hexdigest()
        canonical = "\n".join([method.upper(), path, query, ts, nonce, body_sha])
        sig = hmac.new(self.merchant_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {
            "X-Merchant-Key": self.merchant_key,
            "X-Merchant-Timestamp": ts,
            "X-Merchant-Nonce": nonce,
            "X-Merchant-Body-SHA256": body_sha,
            "X-Merchant-Signature": sig,
            "Content-Type": "application/json",
        }
        if idem:
            headers["X-Idempotency-Key"] = idem
        return headers

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None, params: dict[str, Any] | None = None, idem: str | None = None) -> dict[str, Any]:
        query = urlencode([(k, v) for k, v in (params or {}).items() if v is not None], doseq=True)
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._headers(method, path, raw, query, idem)
        url = self.base_url + path + (("?" + query) if query else "")
        resp = self._client.request(method.upper(), url, content=raw, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {"ok": False, "error": "bad_response", "message": resp.text[:500]}
        if resp.status_code >= 400 or data.get("ok") is False:
            raise BridgeClientError(str(data.get("error") or "bridge_error"), str(data.get("message") or data.get("detail") or resp.text), resp.status_code, data)
        return data

    def get_capacity(self) -> dict[str, Any]:
        return self.request("GET", "/api/merchant/v1/capacity")

    def create_control_session(self, *, merchant_context_ref: str, idem: str, device_id: int | None = None, auto_assign: bool = True, technical_lease_ttl_seconds: int = 180, selection_policy: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "auto_assign": auto_assign,
            "merchant_context_ref": merchant_context_ref,
            "purpose": "customer_control",
            "technical_lease_ttl_seconds": technical_lease_ttl_seconds,
            "expected_device_state": "idle",
            "takeover_policy": "reject",
        }
        if selection_policy:
            body["selection_policy"] = selection_policy
        if device_id is not None:
            body["device_id"] = device_id
            body["auto_assign"] = False
        data = self.request("POST", "/api/merchant/v1/control-sessions", body=body, idem=idem)
        sess = data.get("control_session") or data
        return {
            "control_session_id": sess.get("control_session_id") or sess.get("id"),
            "device_id": int(sess.get("device_id")),
            "fencing_token": sess.get("fencing_token"),
            "status": sess.get("status"),
            "technical_lease_expires_at": sess.get("technical_lease_expires_at"),
            "device_epoch": int(sess.get("device_epoch") or 0),
        }

    def queue_command_bundle(self, session_id: str, *, fencing_token: str, expected_device_epoch: int | None, team_code: str, quality: str, idem: str, ace_enabled: bool = False) -> dict[str, Any]:
        commands = [
            {"action": "set_loadout", "params": {"quality": quality, "loadout_id": f"default_{quality or 'standard'}"}},
            {"action": "enter_team", "params": {"team_code": team_code, "clear_existing": True}},
            {"action": "ready", "params": {}},
            {"action": "watch", "params": {"ace_enabled": bool(ace_enabled), "ace_window_seconds": 120}},
        ]
        body = {
            "fencing_token": fencing_token,
            "mode": "sequential_stop_on_error",
            "expected_device_epoch": expected_device_epoch,
            "commands": commands,
        }
        return self.request("POST", f"/api/merchant/v1/control-sessions/{session_id}/command-bundles", body=body, idem=idem)

    def queue_stop(self, session_id: str, *, fencing_token: str, idem: str, reason: str = "merchant_order_finished") -> dict[str, Any]:
        body = {
            "fencing_token": fencing_token,
            "action": "stop_current",
            "params": {"reason": reason, "cleanup": True},
            "command_ttl_seconds": 30,
        }
        data = self.request("POST", f"/api/merchant/v1/control-sessions/{session_id}/commands", body=body, idem=idem)
        return data.get("command") or data

    def renew_session(self, session_id: str, *, fencing_token: str, idem: str, ttl_seconds: int = 180) -> dict[str, Any]:
        body = {"fencing_token": fencing_token, "technical_lease_ttl_seconds": ttl_seconds}
        return self.request("POST", f"/api/merchant/v1/control-sessions/{session_id}/renew", body=body, idem=idem)

    def events(self, *, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        return self.request("GET", "/api/merchant/v1/events", params={"cursor": cursor, "limit": limit})

    def session_state(self, session_id: str) -> dict[str, Any]:
        data = self.request("GET", f"/api/merchant/v1/control-sessions/{session_id}/state")
        return data.get("control_session") or data

    def session_by_ref(self, merchant_context_ref: str) -> dict[str, Any]:
        data = self.request("GET", f"/api/merchant/v1/control-sessions/by-ref/{merchant_context_ref}")
        return data.get("control_session") or data

    def active_sessions(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/api/merchant/v1/control-sessions", params={"status": "active"})
        return data.get("control_sessions") or data.get("sessions") or []
