from __future__ import annotations

import hashlib
import hmac
import json
import os
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

    WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(
        self,
        base_url: str,
        merchant_key: str,
        merchant_secret: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        api_prefix: str | None = None,
        auth_header_prefix: str | None = None,
        idempotency_retry_attempts: int = 3,
        idempotency_retry_delay: float = 0.15,
    ):
        self.base_url = base_url.rstrip("/")
        self.merchant_key = merchant_key
        self.merchant_secret = merchant_secret
        self.timeout = timeout
        self.api_prefix = (api_prefix or os.getenv("BRIDGE_API_PREFIX") or "/api/external/v1").rstrip("/")
        if not self.api_prefix.startswith("/"):
            self.api_prefix = "/" + self.api_prefix
        # SNOWSERVER slim baseline uses X-External-* on /api/external/v1.
        # Older standalone bridge builds used X-Merchant-* on /api/merchant/v1;
        # keep this configurable so the same merchant server can talk to both.
        self.auth_header_prefix = (auth_header_prefix or os.getenv("BRIDGE_AUTH_HEADER_PREFIX") or "External").strip() or "External"
        self.idempotency_retry_attempts = max(1, int(idempotency_retry_attempts or 1))
        self.idempotency_retry_delay = max(0.0, float(idempotency_retry_delay or 0.0))
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def _path(self, suffix: str) -> str:
        suffix = "/" + suffix.lstrip("/")
        return self.api_prefix + suffix

    def _headers(self, method: str, path: str, raw: bytes, query: str, idem: str | None) -> dict[str, str]:
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        body_sha = hashlib.sha256(raw).hexdigest()
        canonical = "\n".join([method.upper(), path, query, ts, nonce, body_sha])
        sig = hmac.new(self.merchant_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        header_prefix = self.auth_header_prefix
        headers = {
            f"X-{header_prefix}-Key": self.merchant_key,
            f"X-{header_prefix}-Timestamp": ts,
            f"X-{header_prefix}-Nonce": nonce,
            f"X-{header_prefix}-Body-SHA256": body_sha,
            f"X-{header_prefix}-Signature": sig,
            "Content-Type": "application/json",
        }
        if idem:
            headers["X-Idempotency-Key"] = idem
        return headers

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None, params: dict[str, Any] | None = None, idem: str | None = None) -> dict[str, Any]:
        method_u = method.upper()
        if method_u in self.WRITE_METHODS and not idem:
            raise BridgeClientError("idempotency_required", "写接口必须提供 X-Idempotency-Key", 400)
        query = urlencode([(k, v) for k, v in (params or {}).items() if v is not None], doseq=True)
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        url = self.base_url + path + (("?" + query) if query else "")
        attempts = self.idempotency_retry_attempts if idem else 1
        last_error: BridgeClientError | None = None
        for attempt in range(attempts):
            headers = self._headers(method_u, path, raw, query, idem)
            resp = self._client.request(method_u, url, content=raw, headers=headers)
            try:
                data = resp.json()
            except Exception:
                data = {"ok": False, "error": "bad_response", "message": resp.text[:500]}
            if resp.status_code < 400 and data.get("ok") is not False:
                return data
            error_code = str(data.get("error") or "bridge_error")
            message = str(data.get("message") or data.get("msg") or data.get("detail") or resp.text)
            if method_u in self.WRITE_METHODS and path.startswith(self._path("/devices")) and resp.status_code in {404, 405}:
                error_code = "bridge_unsupported"
                message = "当前中央 External API 未开放设备写接口，请在中央控制台维护设备"
            err = BridgeClientError(error_code, message, resp.status_code, data)
            if err.code == "idempotency_in_progress" and attempt + 1 < attempts:
                last_error = err
                if self.idempotency_retry_delay:
                    time.sleep(self.idempotency_retry_delay * (attempt + 1))
                continue
            raise err
        assert last_error is not None
        raise last_error

    def get_capacity(self) -> dict[str, Any]:
        return self.request("GET", self._path("/capacity"))

    def list_devices(self) -> list[dict[str, Any]]:
        data = self.request("GET", self._path("/devices"))
        return data.get("devices") or []

    def create_device(self, *, machine_id: str, display_name: str, mode: str = "machine", radar_url: str = "", watchdog_card: str = "", accept_orders: bool = True, idem: str) -> dict[str, Any]:
        body = {
            "machine_id": machine_id,
            "display_name": display_name,
            "mode": mode,
            "radar_url": radar_url,
            "watchdog_card": watchdog_card,
            "accept_orders": bool(accept_orders),
        }
        data = self.request("POST", self._path("/devices"), body=body, idem=idem)
        return data.get("device") or data

    def update_device(self, device_id: int, *, machine_id: str | None = None, display_name: str | None = None, mode: str | None = None, radar_url: str | None = None, watchdog_card: str | None = None, enabled: bool | None = None, accept_orders: bool | None = None, idem: str) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if machine_id is not None:
            body["machine_id"] = machine_id
        if display_name is not None:
            body["display_name"] = display_name
        if mode is not None:
            body["mode"] = mode
        if radar_url is not None:
            body["radar_url"] = radar_url
        if watchdog_card is not None:
            body["watchdog_card"] = watchdog_card
        if enabled is not None:
            body["enabled"] = bool(enabled)
        if accept_orders is not None:
            body["accept_orders"] = bool(accept_orders)
        data = self.request("PUT", self._path(f"/devices/{int(device_id)}"), body=body, idem=idem)
        return data.get("device") or data

    def set_device_mode(self, device_id: int, mode: str, *, idem: str) -> dict[str, Any]:
        data = self.request("PUT", self._path(f"/devices/{int(device_id)}"), body={"mode": mode}, idem=idem)
        return data.get("device") or data

    def set_device_enabled(self, device_id: int, enabled: bool, *, idem: str) -> dict[str, Any]:
        action = "enable" if enabled else "disable"
        data = self.request("POST", self._path(f"/devices/{int(device_id)}/{action}"), body={}, idem=idem)
        return data.get("device") or data

    def set_device_accept_orders(self, device_id: int, accept_orders: bool, *, idem: str) -> dict[str, Any]:
        data = self.request("PUT", self._path(f"/devices/{int(device_id)}"), body={"accept_orders": bool(accept_orders)}, idem=idem)
        return data.get("device") or data

    def delete_device(self, device_id: int, *, idem: str) -> dict[str, Any]:
        data = self.request("DELETE", self._path(f"/devices/{int(device_id)}"), idem=idem)
        return {k: v for k, v in data.items() if k not in {"ok", "msg"}}

    def create_control_session(
        self,
        *,
        merchant_context_ref: str,
        idem: str,
        device_id: int | None = None,
        auto_assign: bool = True,
        technical_lease_ttl_seconds: int = 180,
        selection_policy: dict[str, Any] | None = None,
        purpose: str = "customer_control",
        expected_device_state: str = "idle",
        takeover_policy: str = "reject",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "auto_assign": auto_assign,
            "merchant_context_ref": merchant_context_ref,
            "purpose": purpose,
            "technical_lease_ttl_seconds": technical_lease_ttl_seconds,
            "expected_device_state": expected_device_state,
            "takeover_policy": takeover_policy,
        }
        if selection_policy:
            body["selection_policy"] = selection_policy
        if device_id is not None:
            body["device_id"] = device_id
            body["auto_assign"] = False
        data = self.request("POST", self._path("/control-sessions"), body=body, idem=idem)
        sess = data.get("control_session") or data
        return {
            "control_session_id": sess.get("control_session_id") or sess.get("id"),
            "device_id": int(sess.get("device_id")),
            "fencing_token": sess.get("fencing_token"),
            "status": sess.get("status"),
            "technical_lease_expires_at": sess.get("technical_lease_expires_at"),
            "device_epoch": int(sess.get("device_epoch") or 0),
        }

    def queue_command_bundle(
        self,
        session_id: str,
        *,
        fencing_token: str,
        expected_device_epoch: int | None,
        team_code: str,
        quality: str,
        idem: str,
        ace_enabled: bool = False,
        max_rounds: int = 0,
        max_coin_loss: int = 0,
        loadout: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        loadout_params = {"quality": quality, "loadout_id": f"default_{quality or 'standard'}"}
        if loadout:
            loadout_params.update(loadout)
        watch_params = {"ace_enabled": bool(ace_enabled), "ace_window_seconds": 120}
        if max_rounds:
            watch_params["max_rounds"] = int(max_rounds)
        if max_coin_loss:
            watch_params["max_coin_loss_w"] = int(max_coin_loss)
        commands = [
            {"action": "set_loadout", "params": loadout_params},
            {"action": "enter_team", "params": {"team_code": team_code, "clear_existing": True}},
            {"action": "ready", "params": {}},
            {"action": "watch", "params": watch_params},
        ]
        body = {
            "fencing_token": fencing_token,
            "mode": "sequential_stop_on_error",
            "expected_device_epoch": expected_device_epoch,
            "commands": commands,
        }
        return self.request("POST", self._path(f"/control-sessions/{session_id}/command-bundles"), body=body, idem=idem)

    def queue_stop(self, session_id: str, *, fencing_token: str, idem: str, reason: str = "merchant_order_finished") -> dict[str, Any]:
        body = {
            "fencing_token": fencing_token,
            "action": "stop_current",
            "params": {"reason": reason, "cleanup": True},
            "command_ttl_seconds": 30,
        }
        data = self.request("POST", self._path(f"/control-sessions/{session_id}/commands"), body=body, idem=idem)
        commands = data.get("commands")
        return data.get("command") or (commands[0] if isinstance(commands, list) and commands else data)

    def queue_command(self, session_id: str, *, fencing_token: str, action: str, params: dict[str, Any] | None = None, expected_device_epoch: int | None = None, idem: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "fencing_token": fencing_token,
            "action": action,
            "params": params or {},
            "command_ttl_seconds": 30,
        }
        if expected_device_epoch is not None:
            body["expected_device_epoch"] = expected_device_epoch
        data = self.request("POST", self._path(f"/control-sessions/{session_id}/commands"), body=body, idem=idem)
        commands = data.get("commands")
        return data.get("command") or (commands[0] if isinstance(commands, list) and commands else data)

    def renew_session(self, session_id: str, *, fencing_token: str, idem: str, ttl_seconds: int = 180) -> dict[str, Any]:
        body = {"fencing_token": fencing_token, "technical_lease_ttl_seconds": ttl_seconds}
        return self.request("POST", self._path(f"/control-sessions/{session_id}/renew"), body=body, idem=idem)

    def events(self, *, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        return self.request("GET", self._path("/events"), params={"cursor": cursor, "limit": limit})

    def session_state(self, session_id: str) -> dict[str, Any]:
        data = self.request("GET", self._path(f"/control-sessions/{session_id}/state"))
        return data.get("control_session") or data

    def session_by_ref(self, merchant_context_ref: str) -> dict[str, Any]:
        data = self.request("GET", self._path(f"/control-sessions/by-ref/{merchant_context_ref}"))
        return data.get("control_session") or data

    def active_sessions(self) -> list[dict[str, Any]]:
        data = self.request("GET", self._path("/control-sessions"), params={"status": "active"})
        return data.get("control_sessions") or data.get("sessions") or []
