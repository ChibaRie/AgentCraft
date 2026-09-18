import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import { V2_DISCOVER, V2_MCP_SERVERS, V2_PROVIDERS, V2_TASKS } from "../api/v2/routes.js";
import TaskCreatePage from "./TaskCreatePage.jsx";

// 页面级网络面 mock：requestV2 换 vi.fn（V2ApiError/newIdempotencyKey/getCsrfToken 保留真实实现）
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

/**
 * P09 任务创建页 V2 重建测试（Phase 8 计划 Task 8 Step 3 用例清单 ①-⑥）。
 * mock 面：requestV2（JSON 面）+ 全局 fetch（multipart 上传走 raw fetch——
 * requestV2 是 JSON-only）；上传闸门可控，用于断言 uploading 态 quota 展示。
 */

const EXPERT_ID = "e1000000-0000-0000-0000-00000000000a";
const RID = "r2000000-0000-0000-0000-00000000000b";
const TASK_ID = "t9000000-0000-0000-0000-00000000000c";
const PROVIDER_ID = "p4000000-0000-0000-0000-00000000000d";

const EXPERT_CARD = {
  id: EXPERT_ID,
  published_revision_id: RID,
  name: "周报秘书",
  description: "整理技术周报",
  avatar_url: null,
  category: "tech",
  skill_count: 2,
};

const PROVIDER_ROW = {
  id: PROVIDER_ID,
  catalog_id: "c".repeat(32),
  catalog_display_name: "DeepSeek",
  model_id: "deepseek-chat",
  key_last4: "ab12",
  key_version: 1,
  status: "active",
  is_default: true,
  created_at: "2026-09-01T08:00:00",
};

const TASKS_CREATED = {
  status: 201,
  data: { task: { id: TASK_ID, status: "uploading" }, message: { id: "m1", event_sequence: 1 } },
  headers: new Headers(),
};

const QUOTA_VIEW = {
  status: 200,
  data: {
    usage: {
      inputs_count: 0,
      outputs_count: 0,
      inputs_bytes: 0,
      outputs_bytes: 0,
      total_bytes: 0,
    },
    limits: {
      max_files_per_task: 10,
      max_single_file_bytes: 5242880,
      max_task_bytes: 10485760,
    },
    input_frozen: false,
  },
  headers: new Headers(),
};

const COMMIT_OK = {
  status: 200,
  data: {
    task: { id: TASK_ID, status: "queued" },
    manifest_sha256: "ab".repeat(32),
    round_id: "rr-1",
    event_sequence: 9,
  },
  headers: new Headers(),
};

// requestV2 路由键（method + path）
const DISCOVER_KEY = `GET ${V2_DISCOVER}/experts?page=1&page_size=50`;
const PROVIDERS_KEY = `GET ${V2_PROVIDERS}`;
const MCP_SERVERS_KEY = `GET ${V2_MCP_SERVERS}`;
const CREATE_KEY = `POST ${V2_TASKS}`;
const QUOTA_PATH = `${V2_TASKS}/${TASK_ID}/quota`;
const QUOTA_KEY = `GET ${QUOTA_PATH}`;
const FILES_PATH = `${V2_TASKS}/${TASK_ID}/files`;
const COMMIT_PATH = `${V2_TASKS}/${TASK_ID}/input/commit`;
const COMMIT_KEY = `POST ${COMMIT_PATH}`;

let timeline;

function ok(data, status = 200) {
  return { status, data, headers: new Headers() };
}

function okList(items) {
  return ok({ items, total: items.length, page: 1, page_size: 50 });
}

/** 排空微任务队列（异步 handler 的 setState 全部落定） */
function flush() {
  return act(async () => {});
}

/** requestV2 可编程路由：按 "METHOD path" 分派；条目=响应对象/Error/返回器。
 * 多次调用合并（后写覆盖同键）——用例先路由写端点、mountWith 再补装载端点。 */
const requestRoutes = {};

function routeRequestV2(map) {
  Object.assign(requestRoutes, map);
  requestV2.mockImplementation(async (path, options = {}) => {
    const method = (options.method ?? "GET").toUpperCase();
    const key = `${method} ${path}`;
    timeline.push(key);
    const entry = requestRoutes[key];
    if (!entry) {
      throw new Error(`requestV2：本用例未编排 ${key}`);
    }
    if (entry instanceof Error) {
      throw entry;
    }
    if (typeof entry === "function") {
      return entry(options);
    }
    return entry;
  });
}

/** multipart 上传走 raw fetch：可编程 stub（时间线与 requestV2 共享） */
function stubUploadFetch(handler) {
  const fetchMock = vi.fn(async (input, init) => {
    const url = typeof input === "string" ? input : String(input?.url ?? "");
    timeline.push(`fetch ${url}`);
    return handler(url, init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function uploadResponse(fileName) {
  return new Response(
    JSON.stringify({
      data: {
        files: [
          {
            id: `f-${fileName}`,
            file_name: fileName,
            sha256: "c".repeat(64),
            size_bytes: 5,
            state: "staged",
          },
        ],
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } }
  );
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/tasks/new"]}>
      <Routes>
        <Route path="/tasks/new" element={<TaskCreatePage />} />
        <Route path="/tasks/:id" element={<div>任务页标记</div>} />
      </Routes>
    </MemoryRouter>
  );
}

/** 挂载并等待专家/Provider 两个装载 GET 落定 */
async function mountWith({ experts = [EXPERT_CARD], providers = [], mcpServers = [] } = {}) {
  routeRequestV2({
    [DISCOVER_KEY]: okList(experts),
    [PROVIDERS_KEY]: ok(providers),
    [MCP_SERVERS_KEY]: ok(mcpServers),
  });
  renderPage();
  await flush();
}

async function selectExpert(name = "周报秘书") {
  fireEvent.click(screen.getByRole("button", { name: new RegExp(name) }));
  await flush();
}

function typeMessage(text = "帮我整理本周的技术周报") {
  fireEvent.change(screen.getByLabelText("首条消息"), { target: { value: text } });
}

function attachFiles(files) {
  fireEvent.change(screen.getByLabelText("附加输入文件（可选）"), { target: { files } });
}

function submitButton() {
  return screen.getByRole("button", { name: "创建任务" });
}

function createCallOptions() {
  const call = requestV2.mock.calls.find(
    ([path, options]) => path === V2_TASKS && options?.method === "POST"
  );
  return call?.[1] ?? null;
}

beforeEach(() => {
  vi.resetAllMocks();
  timeline = [];
  for (const key of Object.keys(requestRoutes)) {
    delete requestRoutes[key];
  }
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("① 专家选择与 revision id 透传", () => {
  it("专家卡渲染卡片字段，提交 payload 透传 published_revision_id（非实体 id）", async () => {
    routeRequestV2({
      [CREATE_KEY]: TASKS_CREATED,
      [QUOTA_KEY]: QUOTA_VIEW,
      [COMMIT_KEY]: COMMIT_OK,
    });
    await mountWith();

    expect(screen.getByText("周报秘书")).toBeTruthy();
    expect(screen.getByText(/整理技术周报/)).toBeTruthy();
    await selectExpert();
    typeMessage();
    fireEvent.click(submitButton());
    await flush();

    const options = createCallOptions();
    expect(options).not.toBeNull();
    expect(options.body).toEqual({
      expert_revision_id: RID,
      initial_message: "帮我整理本周的技术周报",
    });
    expect(screen.getByText("任务页标记")).toBeTruthy(); // navigate /tasks/{id}
  });

  it("选中项无 published_revision_id（异常态）→ 提交禁用且不发创建请求", async () => {
    await mountWith({
      experts: [{ ...EXPERT_CARD, id: "e2000000-0000-0000-0000-00000000000e", published_revision_id: null, name: "异常卡" }],
    });
    await selectExpert("异常卡");
    typeMessage();

    const button = submitButton();
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    await flush();
    expect(createCallOptions()).toBeNull();
  });
});

describe("MCP 挂载选择", () => {
  it("勾选已启用 MCP server → 创建 payload 含 mcp_refs", async () => {
    const server = {
      id: "m1000000-0000-0000-0000-00000000000f",
      name: "Filesystem",
      transport_kind: "stdio",
      enabled: true,
      has_command: true,
    };
    routeRequestV2({
      [CREATE_KEY]: TASKS_CREATED,
      [QUOTA_KEY]: QUOTA_VIEW,
      [COMMIT_KEY]: COMMIT_OK,
    });
    await mountWith({ mcpServers: [server] });
    await selectExpert();
    typeMessage();
    fireEvent.click(screen.getByRole("checkbox", { name: /Filesystem/ }));
    fireEvent.click(submitButton());
    await flush();
    const options = createCallOptions();
    expect(options.body.mcp_refs).toEqual([{ server_id: server.id }]);
  });
});

describe("② Provider 选择与缺省 payload", () => {
  it("无 Provider 时缺省提交 payload 不含 provider_id 键", async () => {
    routeRequestV2({
      [CREATE_KEY]: TASKS_CREATED,
      [QUOTA_KEY]: QUOTA_VIEW,
      [COMMIT_KEY]: COMMIT_OK,
    });
    await mountWith({ providers: [] });
    await selectExpert();
    typeMessage();
    fireEvent.click(submitButton());
    await flush();

    const body = createCallOptions().body;
    expect(body).not.toHaveProperty("provider_id");
    expect(screen.getByText("任务页标记")).toBeTruthy();
  });

  it("选择显式 Provider 时 payload 携带 provider_id", async () => {
    routeRequestV2({
      [CREATE_KEY]: TASKS_CREATED,
      [QUOTA_KEY]: QUOTA_VIEW,
      [COMMIT_KEY]: COMMIT_OK,
    });
    await mountWith({ providers: [PROVIDER_ROW] });
    await selectExpert();
    typeMessage();
    fireEvent.change(screen.getByLabelText("模型服务（Provider）"), {
      target: { value: PROVIDER_ID },
    });
    fireEvent.click(submitButton());
    await flush();

    expect(createCallOptions().body).toEqual({
      expert_revision_id: RID,
      initial_message: "帮我整理本周的技术周报",
      provider_id: PROVIDER_ID,
    });
  });
});

describe("③ 文件预检（客户端拦截，不发请求）", () => {
  it("超过 10 个文件 → 错误面且无任何创建请求", async () => {
    await mountWith();
    await selectExpert();
    typeMessage();
    attachFiles(Array.from({ length: 11 }, (_, index) => new File(["x"], `f${index}.txt`)));
    fireEvent.click(submitButton());
    await flush();

    expect(createCallOptions()).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain("最多附加 10 个文件");
  });

  it("单个文件超 5MiB → 错误面且无任何创建请求", async () => {
    await mountWith();
    await selectExpert();
    typeMessage();
    attachFiles([new File([new Uint8Array(5 * 1024 * 1024 + 1)], "big.bin")]);
    fireEvent.click(submitButton());
    await flush();

    expect(createCallOptions()).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain("单个文件不能超过 5MiB");
  });
});

describe("④ 提交编排（create→files→commit）", () => {
  it("三段调用序 + 幂等键逐请求生成 + manifest 取自上传响应 + uploading 态展示 quota", async () => {
    routeRequestV2({
      [CREATE_KEY]: TASKS_CREATED,
      [QUOTA_KEY]: QUOTA_VIEW,
      [COMMIT_KEY]: COMMIT_OK,
    });
    let releaseUpload;
    const uploadGate = new Promise((resolve) => {
      releaseUpload = resolve;
    });
    const fetchMock = stubUploadFetch(async (url, init) => {
      expect(url).toBe(FILES_PATH);
      expect(init.credentials).toBe("same-origin");
      expect(init.body).toBeInstanceOf(FormData);
      await uploadGate;
      return uploadResponse("a.txt");
    });

    await mountWith();
    await selectExpert();
    typeMessage();
    attachFiles([new File(["hello"], "a.txt")]);
    fireEvent.click(submitButton());
    await flush();

    // uploading 态：quota 已拉取并页内展示（首个上传仍被闸门挂起）
    expect(await screen.findByText(/输入 0\/10/)).toBeTruthy();

    releaseUpload();
    await flush();

    expect(timeline).toEqual([
      DISCOVER_KEY,
      PROVIDERS_KEY,
      MCP_SERVERS_KEY,
      CREATE_KEY,
      QUOTA_KEY,
      `fetch ${FILES_PATH}`,
      COMMIT_KEY,
    ]);

    // 幂等键逐请求现场生成（create 与 commit 键不同；upload 键独立）
    const writeCalls = requestV2.mock.calls.filter(([, options]) => options?.method === "POST");
    expect(writeCalls).toHaveLength(2);
    const [createOptions, commitOptions] = writeCalls.map(([, options]) => options);
    expect(createOptions.idempotencyKey).toBeTruthy();
    expect(commitOptions.idempotencyKey).toBeTruthy();
    expect(createOptions.idempotencyKey).not.toBe(commitOptions.idempotencyKey);
    const uploadInit = fetchMock.mock.calls[0][1];
    expect(uploadInit.headers["Idempotency-Key"]).toBeTruthy();

    // commit manifest 取自上传响应（file_name/sha256/size）
    expect(commitOptions.body).toEqual({
      manifest: [{ file_name: "a.txt", sha256: "c".repeat(64), size: 5 }],
    });
    expect(screen.getByText("任务页标记")).toBeTruthy();
  });

  it("零文件也必须 commit（空 manifest 合法）", async () => {
    routeRequestV2({
      [CREATE_KEY]: TASKS_CREATED,
      [QUOTA_KEY]: QUOTA_VIEW,
      [COMMIT_KEY]: COMMIT_OK,
    });
    await mountWith();
    await selectExpert();
    typeMessage();
    fireEvent.click(submitButton());
    await flush();

    expect(timeline).toEqual([DISCOVER_KEY, PROVIDERS_KEY, MCP_SERVERS_KEY, CREATE_KEY, QUOTA_KEY, COMMIT_KEY]);
    const commitOptions = requestV2.mock.calls.find(([path]) => path === COMMIT_PATH)[1];
    expect(commitOptions.body).toEqual({ manifest: [] });
    expect(screen.getByText("任务页标记")).toBeTruthy();
  });
});

describe("⑤ 服务端错误面", () => {
  it("create 400 PROVIDER_NOT_CONFIGURED → 引导文案、不跳转、可重试", async () => {
    routeRequestV2({
      [CREATE_KEY]: new V2ApiError("PROVIDER_NOT_CONFIGURED", "未配置有效 Provider", 400),
    });
    await mountWith();
    await selectExpert();
    typeMessage();
    fireEvent.click(submitButton());
    await flush();

    expect(screen.getByRole("alert").textContent).toContain("Provider");
    expect(screen.queryByText("任务页标记")).toBeNull();
    expect(submitButton().disabled).toBe(false);
    // 未续跑后续段：无 commit 调用
    expect(requestV2.mock.calls.filter(([path]) => path === COMMIT_PATH)).toHaveLength(0);
  });

  it("create 400 CATALOG_ITEM_DISABLED → 目录停用文案", async () => {
    routeRequestV2({
      [CREATE_KEY]: new V2ApiError("CATALOG_ITEM_DISABLED", "目录条目已停用", 400),
    });
    await mountWith();
    await selectExpert();
    typeMessage();
    fireEvent.click(submitButton());
    await flush();

    expect(screen.getByRole("alert").textContent).toContain("目录条目已被停用");
    expect(screen.queryByText("任务页标记")).toBeNull();
  });
});

describe("⑥ commit 409 INPUT_COMMITTED 重试防护", () => {
  it("不自动重试 commit，恢复导航到任务页", async () => {
    routeRequestV2({
      [CREATE_KEY]: TASKS_CREATED,
      [QUOTA_KEY]: QUOTA_VIEW,
      [COMMIT_KEY]: new V2ApiError("INPUT_COMMITTED", "任务输入已冻结", 409),
    });
    await mountWith();
    await selectExpert();
    typeMessage();
    fireEvent.click(submitButton());
    await flush();

    // 仅一次 commit 请求（无重试循环）
    expect(requestV2.mock.calls.filter(([path]) => path === COMMIT_PATH)).toHaveLength(1);
    // 输入已冻结=提交已生效 → 恢复导航
    expect(screen.getByText("任务页标记")).toBeTruthy();
  });
});
