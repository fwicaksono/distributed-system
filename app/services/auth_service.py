from __future__ import annotations

import asyncio
import uuid
from functools import lru_cache

import bcrypt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, ConflictError
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import Token, UserCreate


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A real bcrypt hash checked when the email doesn't exist, so "unknown email" and
    "wrong password" take the same time (prevents user enumeration via timing)."""
    return bcrypt.hashpw(b"timing-equalizer", bcrypt.gensalt(rounds=12)).decode()


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.settings = settings

    async def register(self, data: UserCreate) -> User:
        # bcrypt is CPU-bound (~250 ms). Running it directly would block the event loop and
        # stall every other request on this API instance, so push it to a thread.
        hashed = await asyncio.to_thread(hash_password, data.password)
        try:
            # Savepoint: a unique violation rolls back only this INSERT.
            async with self.session.begin_nested():
                return await self.users.add(data.email, hashed)
        except IntegrityError as exc:
            raise ConflictError("Email is already registered") from exc

    async def authenticate(self, email: str, password: str) -> Token:
        user = await self.users.get_by_email(email)
        hashed = user.hashed_password if user else await asyncio.to_thread(_dummy_hash)
        valid = await asyncio.to_thread(verify_password, password, hashed)
        if user is None or not valid or not user.is_active:
            raise AuthenticationError("Incorrect email or password")
        token, expires_in = create_access_token(user.id, self.settings)
        return Token(access_token=token, expires_in=expires_in)

    async def get_active_user(self, user_id: uuid.UUID) -> User:
        user = await self.users.get(user_id)
        if user is None or not user.is_active:
            raise AuthenticationError()
        return user
