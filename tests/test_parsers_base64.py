from __future__ import annotations

import base64

from nodebench.parsers.base64_text import decode_subscription

SRC = "src-b64"

URI_ONE = (
    "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.5:443"
    "?type=ws#sample-b64-a\n"
)
URI_TWO = "trojan://sample-password@198.51.100.5:443?sni=fast.example.test#b\n"

URLSAFE_PAYLOAD = (
    "dHJvamFuOi8vc2FtcGxlLXBhc3N3b3JkQDE5Mi4wLjIuOTo0NDMj6K-V0Yfnq6_oqp45MTQzCg=="
)


def encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_standard_base64_payload():
    text, issue = decode_subscription(encode(URI_ONE + URI_TWO), SRC)
    assert issue is None
    assert text is not None
    assert text.count("://") == 2
    assert text.startswith("vless://")


def test_missing_padding_accepted():
    payload = encode(URI_ONE).rstrip("=")
    text, issue = decode_subscription(payload, SRC)
    assert issue is None
    assert text is not None
    assert text.startswith("vless://")


def test_urlsafe_alphabet_accepted():
    assert "-" in URLSAFE_PAYLOAD
    assert "_" in URLSAFE_PAYLOAD
    text, issue = decode_subscription(URLSAFE_PAYLOAD, SRC)
    assert issue is None
    assert text is not None
    assert text.startswith("trojan://")


def test_non_base64_rejected():
    text, issue = decode_subscription("this is not base64!", SRC)
    assert text is None
    assert issue is not None
    assert issue.code == "invalid_base64"
    assert issue.source_id == SRC


def test_payload_without_uri_lines_rejected():
    text, issue = decode_subscription(
        encode("remember to retest this folder later"), SRC
    )
    assert text is None
    assert issue is not None
    assert issue.code == "invalid_base64"


def test_empty_input_rejected():
    for payload in ("", "   \n\t"):
        text, issue = decode_subscription(payload, SRC)
        assert text is None
        assert issue is not None
        assert issue.code == "invalid_base64"


def test_byte_order_mark_stripped():
    text, issue = decode_subscription("\ufeff" + encode(URI_ONE), SRC)
    assert issue is None
    assert text is not None
    assert text.startswith("vless://")


def test_invalid_utf8_after_decode_rejected():
    payload = base64.b64encode(b"\xff\xfe\xfc\xfd").decode("ascii")
    text, issue = decode_subscription(payload, SRC)
    assert text is None
    assert issue is not None
    assert issue.code == "invalid_base64"


def test_single_character_payload_rejected():
    text, issue = decode_subscription("A", SRC)
    assert text is None
    assert issue is not None
    assert issue.code == "invalid_base64"
