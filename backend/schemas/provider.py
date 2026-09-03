"""Provider 配置请求 Schema（P10 BYOK，手册 §7.7）。

api_key 仅写入（write-only）；显式 null = 清除 Key（免 Key 端点）；
缺席 = 不变（更新时）。响应侧由 API 层构造，永不含 Key 明文/密文。
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROTOCOLS = ("openai",)  # v1 仅 OpenAI 兼容；anthropic 等预留


def _strip(value):
    return value.strip() if isinstance(value, str) else value


class ProviderCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=30)
    protocol: str = Field(default="openai")
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str | None = Field(default=None, max_length=4096)
    model_id: str = Field(min_length=1, max_length=100)
    is_default: bool = False

    @field_validator("name", "base_url", "model_id", "protocol", mode="before")
    @classmethod
    def _strip_strings(cls, value):
        return _strip(value)

    @field_validator("protocol")
    @classmethod
    def _protocol_allowed(cls, value: str) -> str:
        if value not in PROTOCOLS:
            raise ValueError(f"protocol 暂仅支持: {'/'.join(PROTOCOLS)}")
        return value

    @field_validator("base_url")
    @classmethod
    def _base_url_scheme(cls, value: str) -> str:
        """URL 解析校验（scheme + 主机名必填 + 禁凭据内嵌）。

        SSRF 立场（§7.7）：本产品为本地单操作者 BYOK 部署，Ollama 等本机
        端点是合法目标，故不封禁私网/回环地址；出口防线在 provider-proxy
        （任务令牌 scope、限速、无 CONNECT、日志脱敏，阶段 6 落地）。
        """
        from urllib.parse import urlparse

        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        if not parsed.hostname:
            raise ValueError("base_url 缺少主机名")
        if parsed.username or parsed.password or "@" in normalized.split("//", 1)[1]:
            raise ValueError("base_url 不允许内嵌凭据")
        return normalized

    @field_validator("api_key")
    @classmethod
    def _api_key_nonblank(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("api_key 不能为空白（清除请传 null）")
        return value


class ProviderUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=30)
    protocol: str | None = Field(default=None)
    base_url: str | None = Field(default=None, min_length=8, max_length=500)
    # 三态：缺席=不变；null=清除；字符串=替换
    api_key: str | None = Field(default=None, max_length=4096)
    model_id: str | None = Field(default=None, min_length=1, max_length=100)
    is_default: bool | None = None

    @field_validator("name", "base_url", "model_id", "protocol", mode="before")
    @classmethod
    def _strip_strings(cls, value):
        return _strip(value)

    @field_validator("protocol")
    @classmethod
    def _protocol_allowed(cls, value):
        if value is None:
            return value
        if value not in PROTOCOLS:
            raise ValueError(f"protocol 暂仅支持: {'/'.join(PROTOCOLS)}")
        return value

    @field_validator("base_url")
    @classmethod
    def _base_url_scheme(cls, value):
        if value is None:
            return value
        normalized = value.rstrip("/")
        if not (normalized.startswith("http://") or normalized.startswith("https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return normalized

    @field_validator("api_key")
    @classmethod
    def _api_key_nonblank(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("api_key 不能为空白（清除请传 null）")
        return value
