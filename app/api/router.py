from fastapi import APIRouter

from app.api.routes import auth, tasks

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(tasks.router)
