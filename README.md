# AgentCraft

AgentCraft scaffold-only workspace based on PRD, Engineering Spec, Database Design, and Scaffold Plan v0.4.0.

## Current boundary

This repository intentionally contains structure, contracts, ORM models, migrations, frontend placeholders, and integration boundaries only. Services and Pi/MCP integration modules raise `NotImplementedError`, and unimplemented API routes return HTTP `501`.

## Backend

```powershell
uv sync
uv run alembic upgrade head
uv run uvicorn backend.main:app --reload
```

Health endpoint: `GET /api/health`.

## Frontend

```powershell
cd frontend
npm install
npm run dev
```

Build check:

```powershell
npm run build
```

## Verification

```powershell
uv run ruff check .
uv run pytest
docker compose -f docker/docker-compose.yml config
```

## Docker

`docker/docker-compose.yml` defines the control plane, Provider Proxy, Docker socket proxy, and Pi worker image boundary. Pi containers are not started by default.
