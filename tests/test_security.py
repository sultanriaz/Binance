import pytest

from trader.security import check_token, issue_token, password_hash, password_verify, totp, verify_totp


KEY = "local-test-key-0123456789abcdef-0123456789"


def test_password_round_trip_and_wrong_password():
    encrypted = password_hash("very-secure-demo-password")
    assert encrypted.startswith("scrypt$")
    assert password_verify("very-secure-demo-password", encrypted)
    assert not password_verify("very-secure-demo-password-bad", encrypted)
    assert not password_verify("somepass", "broken")


def test_too_short_password():
    with pytest.raises(ValueError):
        password_hash("weak")


def test_totp_known_reference_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 SHA-1 test key
    assert totp(secret, 59) == "287082"
    assert verify_totp(secret, "287082", 59)
    assert not verify_totp(secret, "random", 59)


def test_jwt_token_valid_expired_and_tampered():
    tok = issue_token(10, "ADMIN", KEY, now=100)
    assert check_token(tok, KEY, now=150)["sub"] == "10"
    with pytest.raises(ValueError):
        check_token(tok, KEY, now=1001)
    with pytest.raises(ValueError):
        check_token(tok + "x", KEY, now=150)
    with pytest.raises(ValueError):
        check_token(tok, "different-key", now=150)
