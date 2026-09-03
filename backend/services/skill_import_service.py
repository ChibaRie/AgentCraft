"""Skill 导入业务逻辑：markdown / zip 文件 → Provider LLM 拆解 → 草稿 Skill。

- md：正文截断后进提示词；zip：安全解包（拒绝 zip-slip/超限）→ 文件树 +
  小文本文件摘录进提示词，包整体落盘 `HOST_DATA_ROOT/skill-packages/skill-{id}/`
- LLM：走 Provider 回退链（用户 BYOK Key 解密 → 系统默认 .env），调
  chat/completions（非流式，60s 超时）；输出剥 code fence 后 JSON 解析
- 产物为 status=draft 的 Skill：LLM 字段仅供预填，发布前仍走 validate；
  缺失栏目回退「（待补充）」
- 密钥纪律：Key 只在本次请求内存中出现，不落日志
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path, PurePosixPath

import httpx

from backend.models.skill import Skill
from backend.services import provider_service
from backend.services.user_service import UserSystemError
from backend.utils.crypto import EncryptionError, decrypt_text, make_keyring, provider_key_aad

logger = logging.getLogger("agentcraft")

MAX_IMPORT_TEXT_BYTES = 512 * 1024  # md 正文上限
MAX_ZIP_BYTES = 20 * 1024 * 1024  # zip 解压总字节上限
MAX_ZIP_FILES = 200
MAX_LLM_CHARS = 24_000  # 提示词正文截断
MAX_EXCERPT_FILES = 8
_EXCERPT_EXTENSIONS = {
    ".md", ".txt", ".py", ".js", ".ts", ".sh", ".json", ".yaml", ".yml", ".toml",
}
_PLACEHOLDER = "（待补充）"

_FIELDS = (
    "name", "description", "use_case", "role", "goal", "steps",
    "input_requirements", "output_requirements", "constraints",
)

_SYSTEM_PROMPT = (
    "你是 Skill 结构化助手。把用户提供的材料拆解为 AgentCraft Skill 的字段，"
    "只输出一个 JSON 对象（不要 markdown 代码块、不要解释文字），字段如下：\n"
    'name：能力名（≤30 字符）；description：功能描述（10-200 字）；'
    "use_case：适用场景；role：专家角色；goal：目标；steps：分步骤工作方法；"
    "input_requirements：用户需要提供什么；output_requirements：产出要求；"
    "constraints：约束。\n所有字段都用中文，值为字符串。"
)


class SkillImportInvalidError(UserSystemError):
    status_code = 400
    code = "INVALID_IMPORT"


class LLMUpstreamError(UserSystemError):
    status_code = 502
    code = "LLM_UPSTREAM_FAILED"


async def invoke_llm_fields(db, settings, *, user_id: int, prompt: str) -> str:
    """默认 LLM 调用：Provider 回退链解析路由 → chat/completions（注入点）。"""
    snapshot, _config_id = await provider_service.resolve_task_provider(
        db, user_id, None, settings
    )
    if snapshot.get("source") == "user":
        base_url = (snapshot.get("base_url") or "").rstrip("/")
        api_key = None
        encrypted = snapshot.get("api_key_encrypted")
        if encrypted:
            try:
                _, keyring = make_keyring(
                    settings.MCP_ENCRYPTION_KEYRING, active_kid=settings.MCP_ENCRYPTION_ACTIVE_KID
                )
                api_key = decrypt_text(
                    encrypted, aad=provider_key_aad(user_id), keyring=keyring
                )
            except EncryptionError as exc:
                raise LLMUpstreamError("Provider Key 解密失败") from exc
    else:
        base_url = (settings.PROVIDER_PROXY_UPSTREAM or "").rstrip("/")
        api_key = settings.OPENAI_API_KEY or None
    if not base_url:
        raise LLMUpstreamError("未配置可用的 Provider")

    body = {
        "model": snapshot.get("model_id") or settings.PI_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(f"{base_url}/chat/completions", json=body,
                                         headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("Skill 导入 LLM 连接失败: %s", type(exc).__name__)
        raise LLMUpstreamError("连接 Provider 失败，请稍后重试") from exc
    if response.status_code >= 400:
        logger.warning("Skill 导入 LLM 返回 %s", response.status_code)
        raise LLMUpstreamError(f"Provider 返回 {response.status_code}，请检查模型配置")
    try:
        return response.json()["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMUpstreamError("Provider 应答格式异常") from exc


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def _coerce_fields(raw: str, fallback_name: str) -> dict:
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise LLMUpstreamError("LLM 输出不是合法 JSON，请重试") from exc
    if not isinstance(data, dict):
        raise LLMUpstreamError("LLM 输出不是 JSON 对象")
    fields = {}
    for key in _FIELDS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            fields[key] = _PLACEHOLDER
        else:
            fields[key] = value.strip()
    name = fields["name"]
    if name == _PLACEHOLDER or len(name) > 30:
        fields["name"] = fallback_name[:30]
    if fields["description"] == _PLACEHOLDER or len(fields["description"]) > 200:
        fields["description"] = f"从 {fallback_name} 导入的能力包"
    return fields


def _truncate_text(data: bytes) -> str:
    if len(data) > MAX_IMPORT_TEXT_BYTES:
        raise SkillImportInvalidError("markdown 文件超过 512KB 上限")
    text = data.decode("utf-8", "replace")
    if len(text) > MAX_LLM_CHARS:
        text = text[:MAX_LLM_CHARS] + "\n…（内容过长已截断）"
    return text


def _inspect_zip(data: bytes) -> list[tuple[str, bytes]]:
    """安全读取 zip 条目：拒绝 zip-slip、超量、超限；返回 (路径, 字节)。"""
    if len(data) > MAX_ZIP_BYTES:
        raise SkillImportInvalidError("zip 包超过 20MB 上限")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) > MAX_ZIP_FILES:
                raise SkillImportInvalidError("zip 包文件数超过 200 上限")
            entries: list[tuple[str, bytes]] = []
            total = 0
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    raise SkillImportInvalidError(
                        f"zip 内存在不安全的路径条目: {name[:60]}"
                    )
                if not name or name.endswith("/"):
                    continue
                raw = archive.read(name)
                total += len(raw)
                if total > MAX_ZIP_BYTES:
                    raise SkillImportInvalidError("zip 解压后超过 20MB 上限")
                entries.append((name, raw))
            return entries
    except zipfile.BadZipFile as exc:
        raise SkillImportInvalidError("不是合法的 zip 文件") from exc


def _zip_prompt_material(entries: list[tuple[str, bytes]]) -> str:
    """文件树 + 小文本文件摘录（进提示词）。"""
    lines = ["包内文件树："]
    for name, raw in entries:
        lines.append(f"- {name} ({len(raw)} bytes)")
    excerpts = 0
    used = 0
    for name, raw in entries:
        suffix = PurePosixPath(name).suffix.lower()
        if suffix not in _EXCERPT_EXTENSIONS or len(raw) > 4096:
            continue
        if excerpts >= MAX_EXCERPT_FILES or used >= 12_000:
            break
        text = raw.decode("utf-8", "replace")
        lines.append(f"--- {name} ---\n{text}")
        excerpts += 1
        used += len(text)
    return "\n".join(lines)


async def import_skill_file(
    db,
    user_id: int,
    *,
    filename: str,
    content: bytes,
    settings,
    packages_root: Path,
    llm=None,
) -> dict:
    """导入入口：按扩展名分发 md / zip；创建 draft Skill 并返回导入报告。"""
    invoke = llm or invoke_llm_fields
    lower = filename.lower()
    stem = Path(filename).stem or "导入 Skill"

    if lower.endswith((".md", ".markdown")):
        material = _truncate_text(content)
        prompt = f"请把以下 markdown 材料拆解为 Skill 字段：\n\n{material}"
        entries: list[tuple[str, bytes]] | None = None
    elif lower.endswith(".zip"):
        entries = _inspect_zip(content)
        if not entries:
            raise SkillImportInvalidError("zip 包为空或无可导入内容")
        material = _zip_prompt_material(entries)
        prompt = f"请把以下 Skill 包（脚本/资料）拆解为 Skill 字段：\n\n{material}"
    else:
        raise SkillImportInvalidError("仅支持 .md / .markdown / .zip 文件")

    raw_fields = await invoke(db, settings, user_id=user_id, prompt=prompt)
    fields = _coerce_fields(raw_fields, fallback_name=stem)

    skill = Skill(
        owner_id=user_id,
        name=fields["name"],
        description=fields["description"],
        use_case=fields["use_case"],
        role=fields["role"],
        goal=fields["goal"],
        steps=fields["steps"],
        input_requirements=fields["input_requirements"],
        output_requirements=fields["output_requirements"],
        constraints=fields["constraints"],
        status="draft",
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)

    files: list[dict] = []
    if entries is not None:
        package_dir = packages_root / f"skill-{skill.id}"
        package_dir.mkdir(parents=True, exist_ok=True)
        for name, raw in entries:
            target = (package_dir / name).resolve()
            if package_dir.resolve() not in target.parents and target != package_dir.resolve():
                continue  # 双重防线：解析后仍越界的条目丢弃
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            files.append({"path": name, "size": len(raw)})

    return {
        "skill": skill,
        "files": files,
        "prompt_preview": prompt[:600],
    }
