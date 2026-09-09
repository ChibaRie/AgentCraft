import base64
import binascii
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
    V2_DATABASE_URL: str = ""  # agentcraft_app role DSN（V2 业务面；空 = V1-only 模式）
    V2_ADMIN_DATABASE_URL: str = ""  # agentcraft_admin role DSN（认证面/系统作业）
    MFA_ENCRYPTION_KEY: str = ""  # b64url 32B；TOTP secret 信封加密（Ops §4.2 独立密钥材料）
    EMAIL_OUTBOX_ENCRYPTION_KEY: str = ""  # b64url 32B；outbox payload 信封加密
    RATE_LIMIT_HMAC_KEY: str = ""  # b64url 32B；限流 HMAC（独立于加密密钥）
    SESSION_COOKIE_SECURE: bool = True  # dev 经 http://localhost 浏览器豁免；LAN 调试可关
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
        # `.env.example` 类 change-me- 占位符与过短密钥同样拒绝：
        # 模板未经编辑不可启动，密钥长度下限防暴力可猜值（32 字符含端点）
        for name, value in (
            ("SECRET_KEY", self.SECRET_KEY),
            ("TASK_TOKEN_SECRET", self.TASK_TOKEN_SECRET),
        ):
            if value.startswith("change-me-"):
                raise ValueError(f"{name} 不得使用 change-me- 占位符（须为强随机值）")
            if len(value) < 32:
                raise ValueError(f"{name} 长度不足32字符（须为强随机值）")
        self._validate_v2_secrets()
        return self

    def _validate_v2_secrets(self) -> None:
        v2_mode = bool(self.V2_DATABASE_URL) and bool(self.V2_ADMIN_DATABASE_URL)
        if not v2_mode:
            if self.V2_DATABASE_URL or self.V2_ADMIN_DATABASE_URL:
                raise ValueError("V2_DATABASE_URL 与 V2_ADMIN_DATABASE_URL 必须同时配置")
            return
        key_specs = (
            ("MFA_ENCRYPTION_KEY", self.MFA_ENCRYPTION_KEY),
            ("EMAIL_OUTBOX_ENCRYPTION_KEY", self.EMAIL_OUTBOX_ENCRYPTION_KEY),
            ("RATE_LIMIT_HMAC_KEY", self.RATE_LIMIT_HMAC_KEY),
        )
        decoded: dict[str, bytes] = {}
        for name, raw in key_specs:
            if not raw:
                raise ValueError(f"{name} 在 V2 模式下必须设置（b64url 32 字节）")
            try:
                material = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            except (ValueError, binascii.Error) as exc:
                raise ValueError(f"{name} 不是合法 base64url") from exc
            if len(material) != 32:
                raise ValueError(f"{name} 解码后必须为 32 字节")
            if raw in {self.SECRET_KEY, self.TASK_TOKEN_SECRET}:
                raise ValueError(f"{name} 不得与既有密钥共用")
            decoded[name] = material
        if len(set(decoded.values())) != len(decoded):
            raise ValueError("三把 V2 密钥材料必须互不相同")
        if not self.SESSION_COOKIE_SECURE and not self.ALLOW_INSECURE_SECRETS:
            raise ValueError(
                "SESSION_COOKIE_SECURE=false 仅限 ALLOW_INSECURE_SECRETS=true 的开发环境"
            )


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
