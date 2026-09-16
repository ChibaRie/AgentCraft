import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { requestV2 } from "../api/v2/client.js";
import { V2_TASKS } from "../api/v2/routes.js";
import TaskContextPanel from "./TaskContextPanel.jsx";

// 面板级网络面 mock：quota 视图由面板自取（requestV2），其余数据经 props 注入。
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

/**
 * 右侧上下文面板 V2 化测试（Phase 8 计划 Task 9 Step 2）：
 * expert 卡 / 输入 manifest / 产物下载直链（⑦）/ quota 条 / rounds 时间线。
 */

const TASK_ID = "t9000000-0000-0000-0000-00000000000c";
const QUOTA_PATH = `${V2_TASKS}/${TASK_ID}/quota`;
const QUOTA_KEY = `GET ${QUOTA_PATH}`;

const TASK = {
  id: TASK_ID,
  status: "ready",
  abort_reason: null,
  created_at: "2026-09-15T08:00:00",
  event_sequence: 5,
  counts: { inputs: 1, outputs: 1 },
  expert: { name: "周报秘书", avatar_url: "https://cdn.example.com/a.png" },
  provider: { display_name: "DeepSeek", model: "deepseek-chat" },
  initial_round: { id: "r-init", state: "settled", attempt: 1 },
  active_round: { id: "r-active", state: "running", attempt: 2 },
};

const INPUT_FILE = {
  id: "f-in-1",
  file_name: "周报素材.md",
  sha256: "a".repeat(64),
  size_bytes: 2048,
  state: "committed",
};

const ARTIFACT = {
  id: "f-out-1",
  file_name: "周报成品.docx",
  sha256: "b".repeat(64),
  size_bytes: 4096,
  state: "registered",
  produced_in_round_id: "r-init",
};

const QUOTA_VIEW = {
  status: 200,
  data: {
    usage: {
      inputs_count: 1,
      outputs_count: 1,
      inputs_bytes: 2048,
      outputs_bytes: 4096,
      total_bytes: 6144,
    },
    limits: { max_files_per_task: 10, max_single_file_bytes: 5242880, max_task_bytes: 10485760 },
    input_frozen: true,
  },
  headers: new Headers(),
};

function ok(data, status = 200) {
  return { status, data, headers: new Headers() };
}

function flush() {
  return act(async () => {});
}

function renderPanel(props = {}) {
  return render(
    <TaskContextPanel
      taskId={TASK_ID}
      task={TASK}
      inputFiles={[INPUT_FILE]}
      artifacts={[ARTIFACT]}
      {...props}
    />
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  requestV2.mockImplementation(async (path) => {
    if (path === QUOTA_PATH) {
      return QUOTA_VIEW;
    }
    throw new Error(`requestV2：本用例未编排 ${path}`);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("expert 卡与 provider", () => {
  it("渲染 expert.name 与 avatar_url 图片", () => {
    renderPanel();
    expect(screen.getByText("周报秘书")).toBeTruthy();
    const img = screen.getByRole("img", { name: "周报秘书" });
    expect(img.getAttribute("src")).toBe("https://cdn.example.com/a.png");
  });

  it("expert 不可见（null，takedown/指针前移）时回退未知专家首字块", () => {
    renderPanel({ task: { ...TASK, expert: null } });
    expect(screen.getByText("未知专家")).toBeTruthy();
    expect(screen.getByText("未")).toBeTruthy();
  });

  it("渲染 provider 显示名与模型", () => {
    renderPanel();
    expect(screen.getByText(/DeepSeek/)).toBeTruthy();
    expect(screen.getByText(/deepseek-chat/)).toBeTruthy();
  });
});

describe("文件面", () => {
  it("输入 manifest 列表渲染文件名与大小", () => {
    renderPanel();
    expect(screen.getByText("周报素材.md")).toBeTruthy();
    expect(screen.getByText("2.0 KB")).toBeTruthy();
  });

  it("产物列表下载按钮直链 /artifacts/{file_id}/download（⑦）", () => {
    renderPanel();
    const link = screen.getByRole("link", { name: /周报成品/ });
    expect(link.getAttribute("href")).toBe(
      `${V2_TASKS}/${TASK_ID}/artifacts/${ARTIFACT.id}/download`
    );
    expect(link.getAttribute("download")).toBe("周报成品.docx");
  });

  it("无产物时展示空态而非空列表", () => {
    renderPanel({ artifacts: [] });
    expect(screen.getByText("暂无产物")).toBeTruthy();
  });
});

describe("quota 条", () => {
  it("拉取任务用量视图并渲染用量/上限与冻结态", async () => {
    renderPanel();
    await flush();

    expect(requestV2.mock.calls.find(([path]) => path === QUOTA_PATH)).not.toBeNull();
    expect(screen.getByText(/1\/10/)).toBeTruthy();
    expect(screen.getByText(/6144\/10485760/)).toBeTruthy();
    expect(screen.getByText(/输入已冻结/)).toBeTruthy();
  });

  it("quota 拉取失败静默（不阻塞面板其余区块）", async () => {
    requestV2.mockRejectedValue(new Error("quota down"));
    renderPanel();
    await flush();

    expect(screen.getByText("周报秘书")).toBeTruthy();
    expect(screen.queryByText(/输入已冻结/)).toBeNull();
  });
});

describe("rounds 时间线", () => {
  it("渲染 initial/active 轮的 id/state/attempt", () => {
    renderPanel();
    expect(screen.getByText(/r-init/)).toBeTruthy();
    expect(screen.getByText(/已完成/)).toBeTruthy();
    expect(screen.getByText(/r-active/)).toBeTruthy();
    expect(screen.getByText(/执行中/)).toBeTruthy();
    expect(screen.getByText(/attempt 2/)).toBeTruthy();
  });

  it("无轮次时展示空态", () => {
    renderPanel({ task: { ...TASK, initial_round: null, active_round: null } });
    expect(screen.getByText("暂无轮次记录")).toBeTruthy();
  });
});
