from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    HOST_WORKSPACE_ROOT: Path = Path("./workspaces")
    HOST_DATA_ROOT: Path = Path("./data")
    AGENTCRAFT_WORKSPACE_ROOT: str = "/workspaces/authorized"
    AGENTCRAFT_DATA_ROOT: str = "/data"
    TASK_FILE_ROOT: str = "/data/task-files"
    MAX_HISTORY_MESSAGES: int = 40
    PI_PROVIDER: str = "openai"
    PI_MODEL: str = "gpt-4o-mini"
    PI_PROXY_BASE_URL: str = "http://provider-proxy:8080/v1"
    PI_MAX_CONCURRENT_CONTAINERS: int = 4
    PI_IDLE_TIMEOUT_MINUTES: int = 10
    PI_WORKER_IMAGE: str = "agentcraft-pi-worker:0.84.3"
    PI_RUNTIME: str = "auto"  # auto | docker | cli | subprocess
    PI_ROUND_TIMEOUT_SECONDS: int = 300
    PI_FAUX_CHUNK_DELAY_MS: int = 15  # faux 回显分帧节奏（0=即时；供 abort 观察流式）
    PI_NETWORK_NAME: str = "agentcraft-internal"
    AGENTCRAFT_BACKEND_URL: str = "http://agentcraft-control:8000"
    DOCKER_API_URL: str = "http://docker-socket-proxy:2375"
    UPLOAD_MAX_FILE_BYTES: int = 20971520
    UPLOAD_MAX_FILES_PER_REQUEST: int = 10
    UPLOAD_MAX_TASK_BYTES: int = 104857600
    SKILL_PROMPT_MAX_BYTES: int = 65536
    SECRET_KEY: str = "replace-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 120
    DATABASE_URL: str = "sqlite+aiosqlite:///./agentcraft.db"


def get_settings() -> Settings:
    return Settings()
