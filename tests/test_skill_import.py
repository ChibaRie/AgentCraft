"""Skill 导入测试（markdown / zip → Provider LLM 拆解填充）。

- md 导入：LLM 返回字段 JSON → 创建 draft Skill；缺省栏目回退「（待补充）」
- LLM 坏 JSON / 上游失败 → 502；非 md/zip → 400；超大文本 → 400
- zip 导入：安全解包（zip-slip 拒绝）→ 包落盘 skill-packages/skill-{id}/，
  文件树进入 LLM 提示词，响应返回文件清单
- LLM 调用：走 BYOK 回退链（user Key 解密 / 系统默认）；注入 fake 客户端测试
"""

import base64
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from backend.config import Settings, get_settings
from backend.main import app
from backend.services import skill_import_service
from tests.test_skills import auth_header, register_expert

pytestmark = pytest.mark.usefixtures("client")

KEY = os.urandom(32)
KID = "primary"

LLM_FIELDS = {
    "name": "技术周报整理",
    "description": "把一周零散技术动态整理成结构化周报",
    "use_case": "团队每周技术动态汇总与存档",
    "role": "资深技术编辑",
    "goal": "产出结构化周报正文与风险提示",
    "steps": "收集素材；按主题归类；提炼要点；输出风险提示",
    "input_requirements": "原始素材链接或片段",
    "output_requirements": "分主题的周报正文，末尾三条风险提示",
    "constraints": "不虚构事实；引用注明来源",
}


def make_settings(tmp_path: Path, **overrides) -> Settings:
    raw = base64.urlsafe_b64encode(KEY).decode().rstrip("=")
    return Settings(
        MCP_ENCRYPTION_ACTIVE_KID=KID,
        MCP_ENCRYPTION_KEYRING=f"{KID}:{raw}",
        HOST_DATA_ROOT=str(tmp_path / "data"),
        HOST_WORKSPACE_ROOT=str(tmp_path / "workspaces"),
        **overrides,
    )


@pytest.fixture()
def import_env(test_db, tmp_path: Path, client):
    settings = make_settings(tmp_path)
    app.dependency_overrides[get_settings] = lambda: settings
    token, user_id = register_expert(client)
    yield ImportEnv(client, test_db, settings, token, user_id, tmp_path)
    app.dependency_overrides.pop(get_settings, None)


class ImportEnv:
    def __init__(self, client, test_db, settings, token, user_id, tmp_path):
        self.client = client
        self.test_db = test_db
        self.settings = settings
        self.token = token
        self.user_id = user_id
        self.tmp_path = tmp_path

    def auth(self):
        return auth_header(self.token)


def md_bytes() -> bytes:
    return (
        "# 技术周报整理\n\n把一周零散技术动态整理成结构化周报。\n\n"
        "## 步骤\n1. 收集素材 2. 按主题归类 3. 提炼要点\n"
    ).encode("utf-8")


def fake_llm(fields=None, *, raw=None):
    """构造注入用 LLM 替身：返回 fields 的 JSON 文本（可包 code fence）。"""

    async def _invoke(db, settings, *, user_id, prompt):
        if raw is not None:
            return raw
        return json.dumps(fields or LLM_FIELDS, ensure_ascii=False)

    return _invoke


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# markdown 导入
# ---------------------------------------------------------------------------


def test_import_markdown_creates_draft_skill(client, import_env, monkeypatch):
    env = import_env
    monkeypatch.setattr(skill_import_service, "invoke_llm_fields", fake_llm())
    response = client.post(
        "/api/skills/import",
        files={"file": ("skill.md", md_bytes(), "text/markdown")},
        headers=env.auth(),
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["skill"]["status"] == "draft"
    assert data["skill"]["name"] == LLM_FIELDS["name"]
    assert data["skill"]["goal"].startswith("产出结构化周报")
    # 落库核验
    detail = client.get(f"/api/skills/{data['skill']['id']}", headers=env.auth())
    assert detail.json()["data"]["status"] == "draft"


def test_import_markdown_fills_missing_fields(client, import_env, monkeypatch):
    env = import_env
    partial = {k: v for k, v in LLM_FIELDS.items() if k not in ("constraints", "role")}
    monkeypatch.setattr(skill_import_service, "invoke_llm_fields", fake_llm(partial))
    response = client.post(
        "/api/skills/import",
        files={"file": ("skill.md", md_bytes(), "text/markdown")},
        headers=env.auth(),
    )
    assert response.status_code == 201
    skill = response.json()["data"]["skill"]
    assert skill["constraints"] == "（待补充）"
    assert skill["role"] == "（待补充）"


def test_import_markdown_tolerates_code_fence(client, import_env, monkeypatch):
    env = import_env
    fenced = "```json\n" + json.dumps(LLM_FIELDS, ensure_ascii=False) + "\n```"
    monkeypatch.setattr(skill_import_service, "invoke_llm_fields", fake_llm(raw=fenced))
    response = client.post(
        "/api/skills/import",
        files={"file": ("skill.md", md_bytes(), "text/markdown")},
        headers=env.auth(),
    )
    assert response.status_code == 201
    assert response.json()["data"]["skill"]["name"] == LLM_FIELDS["name"]


def test_import_llm_bad_json_502(client, import_env, monkeypatch):
    env = import_env
    monkeypatch.setattr(skill_import_service, "invoke_llm_fields", fake_llm(raw="不是 JSON 的输出"))
    response = client.post(
        "/api/skills/import",
        files={"file": ("skill.md", md_bytes(), "text/markdown")},
        headers=env.auth(),
    )
    assert response.status_code == 502


def test_import_llm_upstream_error_502(client, import_env, monkeypatch):
    async def failing(db, settings, *, user_id, prompt):
        raise skill_import_service.LLMUpstreamError("upstream down")

    env = import_env
    monkeypatch.setattr(skill_import_service, "invoke_llm_fields", failing)
    response = client.post(
        "/api/skills/import",
        files={"file": ("skill.md", md_bytes(), "text/markdown")},
        headers=env.auth(),
    )
    assert response.status_code == 502


def test_import_rejects_unknown_extension(client, import_env):
    env = import_env
    response = client.post(
        "/api/skills/import",
        files={"file": ("skill.exe", b"MZ", "application/octet-stream")},
        headers=env.auth(),
    )
    assert response.status_code == 400


def test_import_rejects_oversize_text(client, import_env):
    env = import_env
    big = b"# x\n" + b"x" * (skill_import_service.MAX_IMPORT_TEXT_BYTES + 10)
    response = client.post(
        "/api/skills/import",
        files={"file": ("skill.md", big, "text/markdown")},
        headers=env.auth(),
    )
    assert response.status_code == 400


def test_import_requires_auth(client, import_env):
    response = client.post(
        "/api/skills/import",
        files={"file": ("skill.md", md_bytes(), "text/markdown")},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# zip 导入
# ---------------------------------------------------------------------------


def test_import_zip_extracts_package_and_lists_files(client, import_env, monkeypatch):
    env = import_env
    zip_bytes = make_zip(
        {
            "scripts/run.py": b"print('weekly')\n",
            "references/notes.md": "素材来源说明".encode("utf-8"),
            "assets/cover.txt": "cover",
        }
    )
    monkeypatch.setattr(skill_import_service, "invoke_llm_fields", fake_llm())
    response = client.post(
        "/api/skills/import",
        files={"file": ("weekly-skill.zip", zip_bytes, "application/zip")},
        headers=env.auth(),
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    skill_id = data["skill"]["id"]
    names = {item["path"] for item in data["files"]}
    assert {"scripts/run.py", "references/notes.md", "assets/cover.txt"} <= names
    # 包已落盘
    package_dir = Path(env.settings.HOST_DATA_ROOT) / "skill-packages" / f"skill-{skill_id}"
    assert (package_dir / "scripts" / "run.py").read_text(encoding="utf-8") == "print('weekly')\n"
    # LLM 提示词包含文件树
    prompt = data["prompt_preview"]
    assert "scripts/run.py" in prompt


def test_import_zip_rejects_zip_slip(client, import_env, monkeypatch):
    env = import_env
    zip_bytes = make_zip({"../evil.txt": b"nope"})
    monkeypatch.setattr(skill_import_service, "invoke_llm_fields", fake_llm())
    response = client.post(
        "/api/skills/import",
        files={"file": ("evil.zip", zip_bytes, "application/zip")},
        headers=env.auth(),
    )
    assert response.status_code == 400
    assert "路径" in response.json()["error"]["message"]


def test_import_zip_rejects_oversize(client, import_env, monkeypatch):
    env = import_env
    zip_bytes = make_zip({"scripts/big.bin": b"0" * (skill_import_service.MAX_ZIP_BYTES + 10)})
    monkeypatch.setattr(skill_import_service, "invoke_llm_fields", fake_llm())
    response = client.post(
        "/api/skills/import",
        files={"file": ("big.zip", zip_bytes, "application/zip")},
        headers=env.auth(),
    )
    assert response.status_code == 400
