from pathlib import Path

from pydantic import ValidationError, model_validator
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
    PROVIDER_PROXY_UPSTREAM: str = "https://api.openai.com"  # 系统默认模式的上游
    OPENAI_API_KEY: str = ""  # 系统默认模式的真实上游 Key（仅 proxy 读取，§7.7）
    PI_MAX_CONCURRENT_CONTAINERS: int = 4
    PI_IDLE_TIMEOUT_MINUTES: int = 10
    PI_WORKER_IMAGE: str = "agentcraft-pi-worker:0.84.3"
    PROVIDER_PROXY_IMAGE: str = "agentcraft-provider-proxy:latest"
    PI_RUNTIME: str = "auto"  # auto | docker | cli | subprocess
    PI_ROUND_TIMEOUT_SECONDS: int = 300
    PI_TASK_MAX_LIFETIME_MINUTES: int = 30  # 任务总超时（§7.8.1），超阈 abort+failed
    PI_FAUX_CHUNK_DELAY_MS: int = 15  # faux 回显分帧节奏（0=即时；供 abort 观察流式）
    PI_NETWORK_NAME: str = "agentcraft-internal"
    MCP_SANDBOX_IMAGE: str = "agentcraft-mcp-sandbox:latest"  # stdio MCP Server 沙箱镜像
    MCP_CALL_TIMEOUT_SECONDS: int = 30  # /internal/mcp/call 单请求超时（§6.8）
    MCP_RESULT_MAX_BYTES: int = 102400  # 工具结果截断上限（100KB，§6.8）
    AGENTCRAFT_BACKEND_URL: str = "http://agentcraft-control:8000"
    AGENTCRAFT_BACKEND_PORT: int = 8000  # dev 转发容器回源宿主机控制面的端口
    DOCKER_API_URL: str = "http://docker-socket-proxy:2375"
    UPLOAD_MAX_FILE_BYTES: int = 20971520
    UPLOAD_MAX_FILES_PER_REQUEST: int = 10
    UPLOAD_MAX_TASK_BYTES: int = 104857600
    SKILL_PROMPT_MAX_BYTES: int = 65536
    SECRET_KEY: str = "replace-me"
    JWT_ALGORITHM: str = "HS256"
    MCP_ENCRYPTION_ACTIVE_KID: str = "primary"
    MCP_ENCRYPTION_KEYRING: str = ""  # kid:<base64url 32B>[:,...];Provider Key/MCP env 信封加密
    JWT_EXPIRE_MINUTES: int = 120
    DATABASE_URL: str = "sqlite+aiosqlite:///./agentcraft.db"
    TASK_TOKEN_SECRET: str = ""  # 任务凭据签名密钥，必须与 SECRET_KEY 不同
    ALLOW_INSECURE_SECRETS: bool = False  # 仅 dev/test 逃生舱；生产禁止
    LOG_LEVEL: str = "INFO"

    @model_validator(mode="after")
    def validate_secrets(self) -> "Settings":
        if self.ALLOW_INSECURE_SECRETS:
            return self
        weak = {"", "replace-me", "your-secret-key-here"}
        if self.SECRET_KEY in weak:
            raise ValueError("SECRET_KEY 必须设置为强随机值（生产禁止默认值）")
        if not self.TASK_TOKEN_SECRET or self.TASK_TOKEN_SECRET in weak:
            raise ValueError("TASK_TOKEN_SECRET 必须设置为独立的强随机值")
        if self.TASK_TOKEN_SECRET == self.SECRET_KEY:
            raise ValueError("TASK_TOKEN_SECRET 不得与 SECRET_KEY 共用")
        return self


def get_settings() -> Settings:
    """构造 Settings；校验失败时只透出干净校验消息（不携带任何输入值 repr）。

    pydantic 的 ValidationError 被 str()/traceback 打印时会嵌入截断的
    input_value 片段（可能泄露 TASK_TOKEN_SECRET 尾巴），生产启动崩溃
    信息必须只含校验消息，故改抛 RuntimeError（from None 隐藏异常链）。
    """
    try:
        return Settings()
    except ValidationError as exc:
        raise RuntimeError("; ".join(err["msg"] for err in exc.errors())) from None
