# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AgentCraft: a local AI expert workbench. Domain expertise is packaged as reusable "experts"
(persona + methodology + Skills + platform tools); each task runs a Pi coding agent inside a
per-task Docker sandbox, with model requests proxied BYOK-style to the user's own provider.

**Current shape: V2 Public Beta, single-track.** The V1 application surface was physically
deleted in Phase 8; contract paths are unified under `/api/*` (admin under `/api/admin`),
sessions are a single `v2Session` cookie. API version **2.0.0**. Phase 10 (user-supplied MCP
face) is delivered. Stack: FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 (dual RLS roles) + Alembic |
React 18 + Vite 5 | Pi coding-agent 0.84.3 | Docker.

> Stale-guidance warning: `../CLAUDE.md` and `../AGENTS.md` (in the parent docs repo) still
> describe a "V1/V2 dual-track" with V1 serving the task domain, a `v1Token` localStorage JWT,
> and a removed user-MCP face. All three are obsolete. This file is authoritative for the code.

## Repository layout: two nested git repos

The parent `Desktop/AgentCraft/` is a separate **docs repo** (branch `master`, **no remote** —
sole copy of the design baselines). This directory is the **code repo** (branch `main`, remote
`origin` = github.com/ChibaRie/AgentCraft).

- Bare `git` here hits the code repo. Design contracts, phase plans, and the progress log live
  one level up, in `../docs/` (see [Docs map](#docs-map-and-workflow)).
- Contract changes go to the docs repo; implementation to the code repo. Both use
  `type: description` conventional commits with **Chinese** descriptions.

## Commands

Python runs from this directory with [uv](https://docs.astral.sh/uv/) (Python 3.12, `.venv` exists):

```bash
uv sync                                            # install deps from uv.lock
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000

uv run pytest                                      # full backend suite (needs PG, see Testing)
uv run pytest tests/test_v2_mcp.py -k discover     # single file / single test
uv run pytest -q --tb=short --cov=backend --cov-report=term-missing --cov-fail-under=80
uv run ruff check backend tests && uv run ruff format --check backend tests
```

Frontend (`frontend/`, Node 22):

```bash
npm run dev      # http://127.0.0.1:5173 (proxies /api and /internal to :8000)
npm run build    # production build
npm test         # vitest run (all)
npx vitest run src/pages/TaskChatPage.test.jsx -t "name"   # single test
```

**vitest is pinned at `^3.2.7`** — 4.x/5.x require Vite ≥6.4, which conflicts with this repo's
Vite 5.4. Do not upgrade without upgrading Vite too.

Migrations (two chains — see [Dual migration chains](#dual-migration-chains)):

```bash
uv run alembic -c alembic_v2.ini upgrade head      # V2 main chain (PostgreSQL)
AGENTCRAFT_V2_DATABASE_URL=... uv run alembic -c alembic_v2.ini upgrade head
uv run alembic -c alembic_v2.ini check             # drift gate (also runs in CI)
uv run alembic upgrade head                        # V1 historical chain (SQLite, frozen)
```

Docker sandbox images (first run; needs Docker Desktop):

```bash
docker compose -f docker/docker-compose.yml --profile pi-worker build pi-worker
docker compose -f docker/docker-compose.yml --profile mcp-sandbox build mcp-sandbox
```

Local seeds (optional, idempotent, local dev only):

```bash
V2_DATABASE_URL=postgresql+asyncpg://<superuser>:<pw>@localhost:5432/<db> \
  uv run python tools/seed_v2_admin.py admin@example.com --password '<pw>'
uv run python tools/seed_v2_demo.py --i-know-demo-bypasses-review
```

## Architecture

### Control plane assembly and startup gating

`backend/main.py` mounts three routers: `/internal` (sandbox callbacks), `/api` (V2 contract
paths), `/api/admin` (admin face). Note `get_settings()` runs at **import time** (`main.py:83`),
so invalid secrets crash on import, not on startup.

V2 mode is gated **solely** by both `V2_DATABASE_URL` and `V2_ADMIN_DATABASE_URL` being set —
setting exactly one raises `ValueError` at startup (`config.py:119-124`). With both set, the
lifespan starts four background loops: `outbox-dispatcher`, `deletion-sweeper`, `task-executor`,
`task-dispatcher` (`main.py:48-62`). With neither set, the process still boots but the entire V2
face is inert — useful only for the frozen `/internal` routes.

V2 mode additionally requires four independent b64url-32-byte keys (`MFA_ENCRYPTION_KEY`,
`EMAIL_OUTBOX_ENCRYPTION_KEY`, `RATE_LIMIT_HMAC_KEY`, `PROVIDER_KEY_ENCRYPTION_KEY`) — pairwise
distinct and distinct from `SECRET_KEY`/`TASK_TOKEN_SECRET`. `get_settings()` converts pydantic
`ValidationError` into a sanitized `RuntimeError` because pydantic echoes input fragments in
tracebacks (`config.py:152-162`).

### Sandbox topology and trust boundaries

```
browser ──/api/* (v2Session cookie)──> control plane (FastAPI)
              │  PostgreSQL 16 (agentcraft_app / agentcraft_admin, RLS)
              │  outbox / deletion sweep / dispatcher + executor loops
              ├─ per-task `docker run` ──> pi-task-<id>
              │     read-only rootfs, non-root, cap_drop ALL, internal network only,
              │     ZERO business mounts — only the generated extension script is mounted
              │     ├─ model calls ──> provider-proxy/v1 (Bearer task token)
              │     │      └─ grant redemption ──> /internal/provider-grant
              │     └─ platform tools ──> /internal/tools (X-Task-Token + fence + kill switch)
              └─ one-shot `docker run` ──> mcp-sandbox (user stdio MCP)
```

**All file and state access is callback-based.** The container has no `/workspace`,
no `/task-files`, no `/outputs` — the five platform tools (`check_code_style`, `read_task_file`,
`write_output_file`, `list_task_files`, `query_task_state`) are defined in
`backend/engine/platform_tools.py` and executed by the control plane.

Tool-callback validation order is **frozen** (`backend/api/internal.py:167-346`), and every
failure returns the same 401 envelope with no layer disclosure:

1. `X-Task-Token` decode → 401
2. `claims.task_id == payload.task_id` → 401
3. token registered in `executor.tokens[round_id]` **on this instance** → 401
4. epoch fence: `claims.lease_epoch == task_rounds.lease_epoch` and round `state='running'` → 401
5. `assert_tool_enabled` against `tool_catalog` (kill switch) → 403 `TOOL_REVOKED`
6. server-side `permissions` enforcement → 400 `TOOL_CALL_REJECTED`

Steps 5–6 deliberately run **after** the fence, so a fenced stale token gets 401 even with a
malformed body. `/internal/provider-grant` (`internal.py:460-534`) additionally requires
`PROXY_GRANT_SECRET` (constant-time compare), and audits **before** releasing the key in a
separate admin transaction — audit failure means the key never leaves.

Real provider keys exist only in `provider-proxy` process memory, minted per round and settled
immediately. The `OPENAI_API_KEY` env inside a task container *is the task token*, not a key.

### Task execution: detached rounds, leases, epoch fencing

HTTP requests only persist and return 202; a background dispatcher claims platform slots and the
executor runs the round. `backend/v2/task_state.py:25-34` is the single source of truth for
transitions (8 task states: `uploading, queued, running, ready, completed, failed, aborted,
deleted`).

The load-bearing pattern is **D16 two-phase orchestration**: Phase 1 shortlists candidates with a
plain (lock-free) SELECT on the *admin* engine; Phase 2 re-does each candidate's real work inside
an *app-role* `owner_session`, where a conditional `UPDATE ... WHERE <state/fence predicate>` is
the arbiter. `rowcount == 0` ⇒ `_CandidateDropped` ⇒ the whole candidate transaction rolls back.

- Claiming a round sets the lease triple (`lease_owner`, `lease_epoch += 1`, `lease_expires_at`)
  and bumps `attempt`. **`lease_epoch` is monotonic and never reset** — reclaim clears owner and
  expiry but keeps the epoch, which is what permanently invalidates a dead container's token.
- Every executor write is preceded by a no-op-advancing fenced `UPDATE`; rowcount 0 ⇒ write nothing.
- Lease renewal starts **before** container assembly so a slow image pull can't starve the 90s TTL.
- `attempt >= 3` on an expired lease fails the round; otherwise it goes back to `pending`.
- Quotas are enforced by the conditional UPDATE itself (`WHERE running_tasks < max`), never by
  COUNT-then-insert. Releases use `WHERE state='held' RETURNING bytes` as the refund basis.

### Database roles and RLS

- **`agentcraft_app`** (`V2_DATABASE_URL`) — business face. Per-table grant matrix: full DML on
  owner tables, SELECT-only on catalogs, **zero grants** on `content_reviews` and `audit_logs`.
- **`agentcraft_admin`** (`V2_ADMIN_DATABASE_URL`) — auth/system jobs. Full DML, but on RLS owner
  tables it has only an `admin_read` SELECT policy: **admin-role writes to those tables silently
  affect 0 rows** (no error). Test seeding of such tables must use the superuser connection.
- RLS context is the GUC `app.current_owner_id`, set by `app.set_current_owner(uuid)`. It is
  **transaction-local** — set it in the same transaction as the writes (`owner_session` in
  `backend/v2/runtime.py:98-103` does this).

### Non-negotiables

- **Never concatenate SQL.** ORM-only. `server_default` is deliberately omitted from V2 models —
  the application layer applies defaults.
- **Never use `FOR UPDATE` on an RLS table in an admin session** — admin's SELECT-only policy
  makes a locking read return 0 rows silently. That is precisely why D16 exists.
- **Never touch SSE streams inside a DB transaction.** Publish frames after commit
  (`publish_runtime_frames` / `after_commit` listeners), or a rollback leaves phantom frames.
- `container_alive` must stay pure in-memory and must never do I/O — a false negative lets
  reclaim cancel a live round.
- `assert_transition` is for **user-facing entry points only**. System write paths (executor,
  reclaim, terminator, sweeps) must use conditional UPDATE + rowcount arbitration instead.
- Shutdown cancels dispatcher **before** executor (stop new claims, then let in-flight rounds
  unwind); lifespan uses async `engine.dispose()`, not `V2Runtime.close()`.
- `MAX_LINE_BYTES = 32 MiB` must stay aligned between transport and engine layers — the
  2026-09-08 incident was the transport raising before the engine's guard.
- Secrets red lines: stdio MCP command material exists only in coroutine memory (never DB
  plaintext, logs, audit, or errors); stderr and prompt fragments are never logged verbatim,
  only counts and lengths.
- **Single-instance assumption** (`instance_id = task-dispatch-<pid>`, in-process token and
  stream registries). Horizontal scaling is a deployment-phase item; it breaks token validation
  and SSE as written.

## Testing

```bash
uv run pytest                    # full suite
uv run pytest tests/test_v2_mcp.py -k discover
```

- **Autouse fixture `neutralize_v2_env`** (`tests/conftest.py:41`) blanks the V2 DSNs and all four
  V2 keys so a developer's `.env` cannot leak into tests (a real baseline-breaking bug). It uses
  `setenv("")` rather than `delenv` — after `delenv`, pydantic-settings falls back to reading the
  `.env` file. Tests needing V2 use `make_v2_runtime` (`tests/test_v2_runtime.py:11`) plus
  dependency overrides, **not** `Settings`.
- **Autouse fixture `v2_executor_streams_hygiene`** clears module-level executor/registry
  references between tests. Keep it synchronous and I/O-free.
- Default path needs **no Docker**: each test gets an isolated SQLite tmp DB from ORM metadata and
  the engine is faked (`FakePiTransport` / `EchoEngine`).
- V2/PostgreSQL path: Docker Desktop running → testcontainers spins up `postgres:16`; or point
  `AGENTCRAFT_TEST_PG_URL` at an external PG (CI uses a service container). A template DB is
  migrated once and **cloned per test** (`pg` fixture). Override the template name with
  `AC_TEMPLATE_DB_NAME` to isolate parallel pytest sessions.
- CI's PG service container **must** be created with `POSTGRES_DB=test`: the fixtures strip the
  database name from the DSN and asyncpg then defaults to the username as the database.
- Test helper modules are shared via `tests/v2_*_helpers.py` (not fixtures); `conftest.py`
  re-exports some by name at the file's end — that position is load-bearing (import cycle).
- **Migration drift gate**: `tests/test_v2_migrations.py::test_model_metadata_matches_migrated_schema`
  (mirrored by `alembic -c alembic_v2.ini check` in CI) asserts ORM metadata matches the migrated
  schema. Model/migration drift turns CI red — fix by aligning model metadata, not by editing
  shipped migrations.
- Coverage gate is **80%** (`pyproject.toml [tool.coverage.report] fail_under`); omits are the V1
  historical chain, `tools/`, and the D15-Option1 frozen surface.

Current baselines: backend collects **1067** tests, frontend **44 files / 429 tests**, both green
with ruff check + format and the production build.

Conventions: tests are `test_<behavior>.py` with descriptive names; frontend tests are colocated
as `Foo.test.jsx` next to `Foo.jsx` and mock the network layer (`requestV2` / `sse.js`), not
components. UI copy assertions are in Chinese.

## Dual migration chains

- `alembic_v2.ini` + `backend/alembic_v2/` — **V2 main chain** (PostgreSQL), currently at `0014`.
- `alembic.ini` + `backend/alembic/` — **V1 historical chain** (SQLite), frozen. V1's application
  surface is gone; this chain survives only as a record of the old data shape. Do not extend it.

## Frozen V1 remnants (do not "clean up")

Deliberately retained after the Phase 8 cutover. Removing them is a separate, deliberate decision:

- `backend/database.py`, `backend/models/` (V1 ORM), `backend/schemas/` (empty shell).
- `backend/engine/pi_engine_manager.py` — its only live entry points are
  `ensure_background()` (idle reclaim + watchdog) and `get_task_token` for the frozen
  `/internal/harness` face; `run_round`/`ensure_container` have no production caller.
  `_V1_TRANSITION_TOOLS` lives here.
- `backend/services/harness_service.py`, `backend/services/task_token.py` (V1 `aud` tokens),
  the V1 branch of `backend/provider_proxy.py`, `SkillLoader`, and the V1 `build_container_spec`.
- `backend/api/internal.py`'s `/internal/ui/response` (501) and `/internal/harness/*`.

Both token families share `TASK_TOKEN_SECRET` but are isolated by JWT `aud`
(`agentcraft:task-token` vs `agentcraft:v2-task-token`).

## Docs map and workflow

- **Contract-first**: V2 API changes are specified in the docs repo *before* implementation.
  `../docs/AgentCraft-V2-Public-Beta/` holds PRD/Engineering/DB/Ops v2.0.0 plus
  `AgentCraft_API_Supplement_v2.0.1.md` — **on conflict the supplement wins** over the
  higher-tier docs.
- `../docs/AgentCraft_V2_Progress.md` — the living progress/decision log. Read it before planning
  work; update it when a phase or task closes.
- `../docs/superpowers/specs/` (designs), `plans/` (per-phase plans + completion records),
  `reports/` (manual-test records, SBOM).
- `./docs/` — V1-era history only (`Scaffold_Progress_v0.12.4.md` et al.). Several files here
  (`DEVELOPMENT_PLAN.md`, `harness/context/*.md`) are V1 scaffold placeholders and are stale.
- `.superpowers/sdd/<phase>/` — SDD ledgers. V2 phases were executed via subagent-driven
  development (fresh implementer per task + adversarial review + fix loops), all subagents on
  sonnet, and **tests always run in the foreground synchronously** (a background-wait mode
  deadlocked once and was abandoned).

## Frontend specifics

- Routes: public token flows (`/login`, `/invitations/accept`, `/email-verification`,
  `/password-reset*`, `/account/deletion/cancel`) are unguarded; `/discover*` is public inside the
  shell; `RequireAuth` guards the app; `RequireAdmin` guards `/admin/*` and is a **soft** gate
  only — the 12h admin TOTP window is enforced by the data plane returning `ADMIN_MFA_REQUIRED`,
  which the gate catches and replays after enrollment/verification.
- Auth: `ac_session` is HttpOnly and never touched by JS; CSRF is double-submit via the
  non-HttpOnly `ac_csrf` cookie and an `X-CSRF-Token` header, auto-retried once on `CSRF_INVALID`.
  Only `401 SESSION_EXPIRED` triggers the global logout-and-redirect; probes must pass
  `silent: true` or they will bounce the user to `/login`.
- Idempotency is a real contract: writes in `V2_IDEMPOTENCY_REQUIRED` (`src/api/v2/client.js`)
  require an `Idempotency-Key` or the server 400s. For mount-triggered submissions use a `useRef`
  initializer so StrictMode's double-invoke keeps one key. In admin flows `reason` participates in
  the idempotency hash — same key + different reason = 409.
- SSE is a `fetch` stream reader (not `EventSource`) over
  `GET /api/tasks/{id}/events/stream?after=<seq>`, with exponential backoff, `Retry-After`
  handling, a 45s half-open watchdog, and reconciliation via `GET /api/tasks/{id}/events?after=`
  before reconnecting. **Persistent frames with `id <= watermark` are dropped** — this
  monotonicity invariant is what makes replayed frames idempotent. New frame handling must go
  through `applySequencedFrame` in `taskChatReducer.js` (split out of the page so it can be
  unit-tested without rendering).
- `requestV2` is JSON-only and returns the unwrapped `data`. Multipart upload and artifact
  download use raw `fetch` with a manual CSRF header.
- Styling: two plain global CSS files with a CSS-custom-property token system ("ink and paper",
  grayscale); dark mode is a token override plus a `prefers-color-scheme` fallback, with an inline
  pre-paint script in `index.html` to avoid a flash. No CSS framework or modules.
- Backend payloads are snake_case; the auth-context user is camelCased by `mapV2User`. There is
  **no ErrorBoundary**, so render-path code must never throw.
- All UI copy and code comments are Simplified Chinese — match that in new code.

## Environment notes (Windows)

- Shell is Git Bash (Unix paths: `/c/Users/ChibaRie`). PowerShell lacks `&&`.
- A dev `.env` with `SESSION_COOKIE_SECURE=false` is intentional for local browser testing —
  leave it alone; the autouse test fixture pins the value back.
