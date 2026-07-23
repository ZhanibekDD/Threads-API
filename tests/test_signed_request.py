import base64
import hashlib
import hmac
import json

from app.oauth import parse_signed_request

APP_SECRET = "test_app_secret_12345"


def _make_signed_request(payload: dict, secret: str = APP_SECRET) -> str:
    payload_json = json.dumps(payload).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload_json).rstrip(b"=").decode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), encoded_payload.encode("utf-8"), hashlib.sha256).digest()
    encoded_sig = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("utf-8")
    return f"{encoded_sig}.{encoded_payload}"


def test_valid_signed_request_is_parsed():
    sr = _make_signed_request({"algorithm": "HMAC-SHA256", "user_id": "12345", "issued_at": 1700000000})
    data = parse_signed_request(sr, APP_SECRET)
    assert data is not None
    assert data["user_id"] == "12345"


def test_wrong_secret_is_rejected():
    sr = _make_signed_request({"algorithm": "HMAC-SHA256", "user_id": "12345"})
    assert parse_signed_request(sr, "a-completely-different-secret") is None


def test_tampered_payload_is_rejected():
    sr = _make_signed_request({"algorithm": "HMAC-SHA256", "user_id": "12345"})
    sig_part, payload_part = sr.split(".", 1)
    tampered_payload = base64.urlsafe_b64encode(b'{"user_id": "99999"}').rstrip(b"=").decode()
    tampered = f"{sig_part}.{tampered_payload}"
    assert parse_signed_request(tampered, APP_SECRET) is None


def test_missing_dot_is_rejected():
    assert parse_signed_request("not-a-signed-request", APP_SECRET) is None


def test_empty_input_is_rejected():
    assert parse_signed_request("", APP_SECRET) is None
    assert parse_signed_request(None, APP_SECRET) is None


def test_garbage_base64_is_rejected():
    assert parse_signed_request("!!!not-base64!!!.also-not-base64", APP_SECRET) is None
