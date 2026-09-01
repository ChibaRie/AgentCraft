from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import api_router, internal_router

app = FastAPI(title="AgentCraft API", version="0.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix="/api")
app.include_router(internal_router, prefix="/internal")


@app.get("/api/health")
async def health() -> dict[str, object]:
    return {"data": {"status": "ok", "version": "0.4.0"}}
