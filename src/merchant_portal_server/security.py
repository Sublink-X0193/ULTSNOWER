from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return "pbkdf2_sha256$200000$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(dk).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, rounds_s, salt_b64, dk_b64 = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        rounds = int(rounds_s)
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(dk_b64.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def normalize_card_code(code: str) -> str:
    return "".join(str(code or "").upper().split())


def hash_card_code(code: str) -> str:
    return hashlib.sha256(normalize_card_code(code).encode("utf-8")).hexdigest()


def opaque_merchant_ref(local_order_no: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), local_order_no.encode("utf-8"), hashlib.sha256).hexdigest()
    return "mref_" + digest[:32]


def request_hash(payload: object) -> str:
    import json

    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
