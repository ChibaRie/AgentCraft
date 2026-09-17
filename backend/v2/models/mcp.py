"""用户 MCP 服务器与工具发现缓存模型（Phase 10 M1，迁移 0013）。

user_mcp_servers：用户自注册的 MCP server 定义（stdio 命令面 / http 地址面）。
Phase 5 安全裁决的受控逆转（2026-09-17 用户决策）——V1 的「用户自由注册任意
stdio 命令并在宿主机执行」不恢复，本表只存**加密后的**启动定义：

- command_encrypted / command_dek_wrapped：stdio 形态下 {command, args, env} 整体
  JSON 经 KeySealer 双层信封加密（KEK→DEK→明文），AAD 绑定 server id（沿用
  provider_key_aad 形态，provider_id 位改传 server id）；明文命令只在 M5
  mcp-sandbox 一次性容器内解封执行，宿主机零接触；
- url：http 形态的 MCP 上游地址（仅 https 且无 userinfo，服务层校验）；
- transport_payload CHECK 保证 stdio↔密文、http↔url 一一对应（模型/迁移双保险）。

user_mcp_tools：discover 的工具描述符缓存（server_id → tool），先删后插幂等；
无 owner_id 冗余列——owner-RLS 经 EXISTS(user_mcp_servers) 子查询判定（与
expert_revisions 的 published_read EXISTS 同族），admin 仅 admin_read。
"""

import uuid as _uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.v2.models.base import Base, TimestampMixin, check_enum, pk_uuid

MCP_TRANSPORT_KINDS = ("stdio", "http")


class UserMcpServer(TimestampMixin, Base):
    __tablename__ = "user_mcp_servers"
    __table_args__ = (
        check_enum("user_mcp_servers", "transport_kind", MCP_TRANSPORT_KINDS),
        CheckConstraint(
            "(transport_kind = 'stdio' AND command_encrypted IS NOT NULL AND url IS NULL) "
            "OR (transport_kind = 'http' AND command_encrypted IS NULL AND url IS NOT NULL)",
            name="transport_payload",
        ),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    owner_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    transport_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    # stdio 形态：{command, args, env} 整体 JSON 的信封密文（KeySealer seal/open）
    command_encrypted: Mapped[str | None] = mapped_column(Text)
    command_dek_wrapped: Mapped[str | None] = mapped_column(Text)
    # http 形态：MCP 上游地址（https、无 userinfo、≤512）
    url: Mapped[str | None] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class UserMcpTool(Base):
    __tablename__ = "user_mcp_tools"
    __table_args__ = (
        UniqueConstraint("server_id", "tool_name", name="uq_user_mcp_tools_server_tool"),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    server_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("user_mcp_servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 人类可读展示名（MCP title；缺省回退 tool_name）
    name: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # MCP inputSchema 原样
    schema_json: Mapped[dict | None] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
