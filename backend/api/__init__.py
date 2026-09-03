from fastapi import APIRouter

from backend.api import auth, experts, files, internal, mcp, providers, skills, tasks, users

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(experts.router)
api_router.include_router(experts.discover_router)
api_router.include_router(skills.router)
api_router.include_router(tasks.workspaces_router)
api_router.include_router(tasks.router)
api_router.include_router(providers.router)
api_router.include_router(files.router)
api_router.include_router(mcp.router)

internal_router = APIRouter()
internal_router.include_router(internal.router)
