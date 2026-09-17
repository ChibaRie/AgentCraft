"""出网 SSRF 防护（2026-09-17 用户自带 base_url 裁决的伴随面）。

用户可自带 OpenAI 兼容上游地址——公网可达性由 URL 形态与 DNS 解析双重把关：

- 仅 https（拒绝 http/file/ftp 等任意 scheme）；
- 拒绝 userinfo（user:pass@host 形态——防凭据注入与解析歧义）；
- DNS 解析后逐 IP 校验：拒绝 loopback / 私网 / link-local / 保留 / 组播 /
  未指定（含 169.254.169.254 元数据、127.0.0.1、::1、fc00::/7、10/8、
  172.16/12、192.168/16 等）——防内网与云元数据探打；
- 消息面不回传解析细节（红线：错误不泄内部信息）。

消费方：控制面连通性测试（provider_service）与 provider-proxy 转发前
（provider_proxy——独立容器进程，经 backend 包共享本工具）。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class EgressBlockedError(Exception):
    """目标地址被出网防护拒绝（对外一律转 502 上游错误面，不透传细节）。"""


def assert_public_https(url: str) -> None:
    """校验 url 为公网 https 目标；违规/解析失败一律 EgressBlockedError。"""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise EgressBlockedError("bad url") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise EgressBlockedError("scheme/host")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise EgressBlockedError("userinfo")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise EgressBlockedError("resolve") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise EgressBlockedError("private-range")
