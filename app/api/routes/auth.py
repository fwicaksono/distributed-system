from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentUser, get_auth_service, rate_limit
from app.schemas.common import ErrorResponse
from app.schemas.user import Token, UserCreate, UserRead
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(rate_limit)])

AuthDep = Annotated[AuthService, Depends(get_auth_service)]


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
)
async def register(data: UserCreate, auth: AuthDep) -> UserRead:
    user = await auth.register(data)
    return UserRead.model_validate(user)


@router.post("/token", response_model=Token, responses={401: {"model": ErrorResponse}})
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], auth: AuthDep) -> Token:
    return await auth.authenticate(form.username, form.password)


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)
