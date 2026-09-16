from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.exceptions import AuthenticationError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from tests.conftest import TEST_SECRET


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("correct horse battery")
    assert hashed != "correct horse battery"
    assert verify_password("correct horse battery", hashed)
    assert not verify_password("wrong password!", hashed)


def test_verify_password_rejects_malformed_hash() -> None:
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_token_roundtrip(settings) -> None:
    user_id = uuid.uuid4()
    token, expires_in = create_access_token(user_id, settings)
    assert expires_in == settings.jwt_access_token_expire_minutes * 60
    assert decode_access_token(token, settings) == user_id


def test_expired_token_rejected(settings) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": past, "iss": settings.app_name}, TEST_SECRET, "HS256"
    )
    with pytest.raises(AuthenticationError):
        decode_access_token(token, settings)


def test_token_signed_with_other_key_rejected(settings) -> None:
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": datetime.now(UTC) + timedelta(minutes=5),
         "iss": settings.app_name},
        "another-secret-that-is-also-long-enough-000",
        "HS256",
    )
    with pytest.raises(AuthenticationError):
        decode_access_token(token, settings)


def test_unsigned_alg_none_token_rejected(settings) -> None:
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": datetime.now(UTC) + timedelta(minutes=5),
         "iss": settings.app_name},
        key=None,
        algorithm="none",
    )
    with pytest.raises(AuthenticationError):
        decode_access_token(token, settings)
