import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import { createTaskStream, fetchEvents } from "../api/v2/sse.js";
import { V2_TASKS } from "../api/v2/routes.js";
import { initialTaskChatState, taskChatReducer } from "./taskChatReducer.js";
import TaskChatPage from "./TaskChatPage.jsx";

// 页面级网络面 mock：requestV2 换 vi.fn（V2ApiError/newIdempotencyKey 保留真实实现）；
// SSE 面整体 mock（createTaskStream 捕获回调供用例注入帧，fetchEvents 供对账续补）。
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});
vi.mock("../api/v2/sse.js", () => ({
  createTaskStream: vi.fn(),
  fetchEvents: vi.fn(),
}));

/**
 * P09 任务对话页 V2 重写测试（Phase 8 计划 Task 9 Step 3 用例清单 ①-⑧ +
 * 202 水位校准 / 删除新语义两附加例 + reducer 单调约束直测）。
 * mock 面：requestV2（JSON 面）+ createTaskStream/fetchEvents（T8 冻结签名）。
 */

const TASK_ID = "t9000000-0000-0000-0000-00000000000c";
const W = 5; // 初始快照水位（task.event_sequence）

const BASE = `${V2_TASKS}/${TASK_ID}`;
const LIST_KEY = `GET ${V2_TASKS}?page=1&size=50`;
const TASK_KEY = `GET ${BASE}`;
const MSGS0_KEY = `GET ${BASE}/messages?after=0&limit=200`;
const FILES_IN_KEY = `GET ${BASE}/files?direction=input`;
const FILES_OUT_KEY = `GET ${BASE}/files?direction=output`;
const ARTIFACTS_KEY = `GET ${BASE}/artifacts`;
const SEND_KEY = `POST ${BASE}/messages`;
const ABORT_KEY = `POST ${BASE}/abort`;
const COMPLETE_KEY = `POST ${BASE}/complete`;
const DELETE_KEY = `DELETE ${BASE}`;

function msgBackfillKey(after) {
  return `GET ${BASE}/messages?after=${after}&limit=200`;
}

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

function taskView(status, extra = {}) {
  return {
    status: 200,
    data: {
      task: {
        id: TASK_ID,
        status,
        abort_reason: null,
        created_at: "2026-09-15T08:00:00",
        input_committed: true,
        input_manifest_sha256: "ab".repeat(32),
        event_sequence: W,
        active_round: null,
        initial_round: { id: "r-init", state: "settled", attempt: 1 },
        counts: { inputs: 1, outputs: 1 },
        expert: { name: "周报秘书", avatar_url: null },
        provider: { display_name: "DeepSeek", model: "deepseek-chat" },
        ...extra,
      },
    },
    headers: new Headers(),
  };
}

const HISTORY_MESSAGES = [
  {
    id: "m-user-1",
    event_sequence: 1,
    author: "user",
    content: "请帮我整理本周技术周报",
    created_at: "2026-09-15T08:00:01",
  },
  {
    id: "m-ai-1",
    event_sequence: 2,
    author: "assistant",
    content: "好的，周报初稿已生成。",
    created_at: "2026-09-15T08:00:05",
  },
];

function ok(data, status = 200) {
  return { status, data, headers: new Headers() };
}

/** 排空微任务队列（异步 handler 的 setState 全部落定） */
function flush() {
  return act(async () => {});
}

// requestV2 可编程路由（TaskCreatePage.test.jsx 同型）：按 "METHOD path" 分派
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

let timeline;

/** 挂载并等待五端点装配 + 流建立落定 */
function mountAt({ status = "ready", taskExtra, messages = HISTORY_MESSAGES, routes = {} } = {}) {
  routeRequestV2({
    [LIST_KEY]: ok({ items: [], total: 0, page: 1, size: 50 }),
    [TASK_KEY]: taskView(status, taskExtra),
    [MSGS0_KEY]: ok(messages),
    [FILES_IN_KEY]: ok([INPUT_FILE]),
    [FILES_OUT_KEY]: ok([]),
    [ARTIFACTS_KEY]: ok([ARTIFACT]),
    ...routes,
  });
  const view = render(
    <MemoryRouter initialEntries={[`/tasks/${TASK_ID}`]}>
      <Routes>
        <Route path="/tasks" element={<div>任务列表标记</div>} />
        <Route path="/tasks/:id" element={<TaskChatPage />} />
      </Routes>
    </MemoryRouter>
  );
  return view;
}

// -- SSE 流 mock 治具 -------------------------------------------------------

let streamHarness;

function pushFrame(id, event, data) {
  act(() => {
    streamHarness.onFrame({ id, event, data });
  });
}

// -- 五端点装配断言（①共用） ------------------------------------------------

const FIVE_LOAD_KEYS = [TASK_KEY, MSGS0_KEY, FILES_IN_KEY, FILES_OUT_KEY, ARTIFACTS_KEY];

function expectFiveEndpointsAssembled() {
  for (const key of FIVE_LOAD_KEYS) {
    expect(timeline.filter((entry) => entry === key)).toHaveLength(1);
  }
  expect(createTaskStream).toHaveBeenCalledTimes(1);
  const [options] = createTaskStream.mock.calls[0];
  expect(options.taskId).toBe(TASK_ID);
  expect(options.after).toBe(W);
  expect(typeof options.onFrame).toBe("function");
  expect(typeof options.onEvents).toBe("function");
  expect(typeof options.onError).toBe("function");
}

beforeEach(() => {
  vi.resetAllMocks();
  timeline = [];
  for (const key of Object.keys(requestRoutes)) {
    delete requestRoutes[key];
  }
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
  streamHarness = null;
  createTaskStream.mockImplementation(({ onFrame, onEvents, onError }) => {
    streamHarness = { onFrame, onEvents, onError, close: vi.fn() };
    return streamHarness;
  });
  fetchEvents.mockResolvedValue({ events: [], snapshot: { status: "ready", event_sequence: W } });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("① 初始五端点并行装配", () => {
  it("快照/messages/files×2/artifacts 并行拉取，流以快照水位建立", async () => {
    mountAt();
    await flush();

    expectFiveEndpointsAssembled();
    expect(screen.getByText("请帮我整理本周技术周报")).toBeTruthy();
    expect(screen.getByText("好的，周报初稿已生成。")).toBeTruthy();
    expect(screen.getAllByText("周报秘书").length).toBeGreaterThan(0);
  });

  it("快照 404 → 任务不存在引导面，不建立流", async () => {
    routeRequestV2({
      [LIST_KEY]: ok({ items: [], total: 0, page: 1, size: 50 }),
      [TASK_KEY]: new V2ApiError("TASK_NOT_FOUND", "任务不存在", 404),
    });
    render(
      <MemoryRouter initialEntries={[`/tasks/${TASK_ID}`]}>
        <Routes>
          <Route path="/tasks/:id" element={<TaskChatPage />} />
        </Routes>
      </MemoryRouter>
    );
    await flush();

    expect(screen.getByText("任务不存在")).toBeTruthy();
    expect(createTaskStream).not.toHaveBeenCalled();
  });
});

describe("② text_delta 流式聚合 → message_saved 回补替换", () => {
  it("增量先入流式缓冲，message_saved 经 /messages 拉回权威正文替换（正文一致）", async () => {
    mountAt();
    await flush();

    pushFrame(null, "text_delta", { delta: "半截增量甲" });
    pushFrame(null, "text_delta", { delta: "半截增量乙" });
    expect(document.body.textContent).toContain("半截增量甲半截增量乙");

    // 回补路由先于 message_saved 帧注入（effect 在帧应用后的同一轮渲染即发请求）
    routeRequestV2({
      [msgBackfillKey(5)]: ok([
        {
          id: "m1",
          event_sequence: 6,
          author: "assistant",
          content: "权威正文全文",
          created_at: "2026-09-15T08:01:00",
        },
      ]),
    });
    pushFrame(6, "message_saved", { message_id: "m1", event_sequence: 6, author: "assistant" });
    await flush();

    expect(document.body.textContent).toContain("权威正文全文");
    expect(document.body.textContent).not.toContain("半截增量甲");
    // 回补请求以「该帧 event_sequence 前水位」为游标
    expect(timeline).toContain(msgBackfillKey(5));
  });
});

describe("③ status_changed(aborted) 丢弃未落库半截渲染", () => {
  it("状态迁移 + 清空 delta 缓冲 + 中止原因面向用户", async () => {
    mountAt({ status: "running" });
    await flush();

    pushFrame(null, "text_delta", { delta: "未落库的半截回复" });
    pushFrame(6, "status_changed", { status: "aborted", abort_reason: "user_cancel" });
    await flush();

    expect(document.body.textContent).not.toContain("未落库的半截回复");
    expect(screen.getByText("已中止")).toBeTruthy();
    expect(screen.getByText(/用户取消/)).toBeTruthy();
  });
});

describe("④ 断线重连后 /events 补帧应用（sse mock 触发）", () => {
  it("onEvents 逐帧幂等应用，快照水位领先时经 fetchEvents 分页续补至追平", async () => {
    mountAt();
    await flush();

    fetchEvents
      .mockResolvedValueOnce({
        events: [
          { sequence: 8, type: "message_saved", message_id: "m-tool", event_sequence: 8, author: "tool" },
        ],
        snapshot: { status: "running", event_sequence: 9 },
      })
      .mockResolvedValueOnce({
        events: [{ sequence: 9, type: "done", finish_reason: "stop", usage: {} }],
        snapshot: { status: "running", event_sequence: 9 },
      });
    routeRequestV2({
      [msgBackfillKey(5)]: ok([
        {
          id: "m-recon",
          event_sequence: 6,
          author: "assistant",
          content: "重连补发的权威正文",
          created_at: "2026-09-15T08:02:00",
        },
        {
          id: "m-tool",
          event_sequence: 8,
          author: "tool",
          content: "工具执行结果",
          created_at: "2026-09-15T08:02:10",
        },
      ]),
      [msgBackfillKey(8)]: ok([]),
      // done → 轮终局刷新（快照 + messages 续拉 + files/artifacts）
      [TASK_KEY]: taskView("completed"),
      [msgBackfillKey(9)]: ok([]),
      [FILES_IN_KEY]: ok([INPUT_FILE]),
      [FILES_OUT_KEY]: ok([]),
      [ARTIFACTS_KEY]: ok([ARTIFACT]),
    });

    act(() => {
      streamHarness.onEvents(
        [
          // sequence ≤ 水位的重复帧（重放双见窗口）必须整体丢弃
          { sequence: 5, type: "queued" },
          { sequence: 6, type: "message_saved", message_id: "m-recon", event_sequence: 6, author: "assistant" },
          { sequence: 7, type: "status_changed", status: "running" },
        ],
        { status: "running", event_sequence: 9 }
      );
    });
    await flush();
    await flush();
    await flush();
    await flush();

    // 分页续补：先 after=7（已应用水位）拉缺页，再 after=8 追平至快照水位 9
    expect(fetchEvents.mock.calls).toEqual([
      [TASK_ID, 7],
      [TASK_ID, 8],
    ]);
    expect(document.body.textContent).toContain("重连补发的权威正文");
    // tool 消息经 MessageList 渲染为工具卡片（正文详情不在消息流展示，V1 同型）
    expect(screen.getAllByText("工具调用").length).toBeGreaterThan(0);
    // 重复 queued 帧（sequence 5 ≤ 水位 5）被单调约束丢弃 → 不出现排队提示
    expect(screen.queryByText(/排队，等待运行槽位/)).toBeNull();
    // done（sequence 9）→ 轮终局刷新收敛至 completed 快照（状态 chip + runtime 双处）
    expect(screen.getAllByText("已结束").length).toBeGreaterThan(0);
  });
});

describe("⑤ ready 前发送禁用 + 429 TASK_ROUND_BUSY 面向用户", () => {
  it("非 ready（queued）状态发送按钮禁用", async () => {
    mountAt({ status: "queued" });
    await flush();

    const send = screen.getByRole("button", { name: "发送" });
    expect(send.disabled).toBe(true);
  });

  it("ready 下发送 429 TASK_ROUND_BUSY → 提示含 Retry-After，幂等键随请求携带", async () => {
    mountAt({ status: "ready" });
    await flush();
    routeRequestV2({
      [SEND_KEY]: new V2ApiError("TASK_ROUND_BUSY", "当前轮次仍在执行", 429, 30),
    });

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "再来一轮" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await flush();

    const sendPath = `${BASE}/messages`;
    const call = requestV2.mock.calls.find(([path, options]) => path === sendPath && options?.method === "POST");
    expect(call).not.toBeNull();
    expect(call[1].body).toEqual({ content: "再来一轮" });
    expect(call[1].idempotencyKey).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("30");
  });
});

describe("⑥ abort running → 202「终止中」→ 终态收敛", () => {
  it("202 cancelling 显示终止中，status_changed(aborted) 后收敛并触发终局刷新", async () => {
    mountAt({ status: "running" });
    await flush();
    routeRequestV2({
      [ABORT_KEY]: ok({
        task: taskView("running").data.task,
        round: { id: "r-active", state: "cancelling", attempt: 1 },
      }, 202),
      // 轮终局刷新路由
      [TASK_KEY]: taskView("aborted", { abort_reason: "user_cancel" }),
      [msgBackfillKey(6)]: ok([]),
      [FILES_IN_KEY]: ok([INPUT_FILE]),
      [FILES_OUT_KEY]: ok([]),
      [ARTIFACTS_KEY]: ok([ARTIFACT]),
    });

    fireEvent.click(screen.getByRole("button", { name: "中止" }));
    await flush();

    expect(screen.getByText(/终止中/)).toBeTruthy();

    pushFrame(6, "status_changed", { status: "aborted", abort_reason: "user_cancel" });
    pushFrame(null, "done", { finish_reason: "aborted", usage: {} });
    await flush();
    await flush();

    expect(screen.getByText("已中止")).toBeTruthy();
    expect(screen.queryByText(/终止中/)).toBeNull();
    // 终局收敛：快照重新拉取（初始装配 + 终局刷新共 2 次）
    expect(timeline.filter((entry) => entry === TASK_KEY)).toHaveLength(2);
  });
});

describe("⑦ 产物下载链接构造", () => {
  it("产物行直链指向 /artifacts/{file_id}/download", async () => {
    mountAt();
    await flush();

    const link = screen.getByRole("link", { name: /周报成品/ });
    expect(link.getAttribute("href")).toBe(`${BASE}/artifacts/${ARTIFACT.id}/download`);
  });
});

describe("⑧ 8 态词表全渲染映射", () => {
  const EIGHT_STATES = [
    ["uploading", "上传中"],
    ["queued", "排队中"],
    ["running", "进行中"],
    ["ready", "待继续"],
    ["completed", "已结束"],
    ["failed", "异常"],
    ["aborted", "已中止"],
    ["deleted", "已删除"],
  ];

  for (const [status, label] of EIGHT_STATES) {
    it(`状态 ${status} 渲染为「${label}」`, async () => {
      const view = mountAt({ status });
      await flush();
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
      view.unmount();
    });
  }
});

describe("⑨（附加）发送 202 event_sequence 直接校准水位", () => {
  it("响应水位先行：其下的持久帧被单调约束丢弃，乐观消息经回补收敛", async () => {
    mountAt({ status: "ready" });
    await flush();
    routeRequestV2({
      [SEND_KEY]: ok(
        { message: { id: "m-user-2", event_sequence: 6 }, event_sequence: 8, round_id: "r-2" },
        202
      ),
      [msgBackfillKey(5)]: ok([
        {
          id: "m-user-2",
          event_sequence: 6,
          author: "user",
          content: "再来一轮",
          created_at: "2026-09-15T08:03:00",
        },
      ]),
    });

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "再来一轮" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await flush();
    await flush();

    // 202 即校准水位 8 → 乐观用户消息先渲染，随后被回补的落库行收敛（正文同在）
    expect(timeline).toContain(msgBackfillKey(5));
    expect(screen.getByText("再来一轮")).toBeTruthy();

    // sequence 7 ≤ 水位 8：实时帧丢弃，状态不迁移
    pushFrame(7, "status_changed", { status: "running" });
    await flush();
    expect(screen.getByText("排队中")).toBeTruthy();

    // sequence 9 > 水位 8：正常应用
    pushFrame(9, "status_changed", { status: "running" });
    await flush();
    expect(screen.getByText("进行中")).toBeTruthy();
  });
});

describe("⑩（附加）删除新语义", () => {
  it("确认后 DELETE（幂等键）并退出任务页", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mountAt({ status: "ready" });
    await flush();
    routeRequestV2({ [DELETE_KEY]: ok({ task: taskView("deleted").data.task }) });

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await flush();

    const call = requestV2.mock.calls.find(
      ([path, options]) => path === BASE && options?.method === "DELETE"
    );
    expect(call).not.toBeNull();
    expect(call[1].idempotencyKey).toBeTruthy();
    expect(screen.getByText("任务列表标记")).toBeTruthy();
  });
});

describe("⑪（附加）messages 页满分页续拉", () => {
  it("初始页达 200 上限时以页尾 event_sequence 为游标续拉至尾页", async () => {
    const fullPage = Array.from({ length: 200 }, (_, index) => ({
      id: `m-${index + 1}`,
      event_sequence: index + 1,
      author: index % 2 === 0 ? "user" : "assistant",
      content: `消息 ${index + 1}`,
      created_at: "2026-09-15T08:00:00",
    }));
    mountAt({
      messages: fullPage,
      routes: {
        [msgBackfillKey(200)]: ok([
          {
            id: "m-201",
            event_sequence: 201,
            author: "assistant",
            content: "消息 201（尾页）",
            created_at: "2026-09-15T08:10:00",
          },
        ]),
        [msgBackfillKey(201)]: ok([]),
      },
    });
    await flush();
    await flush();
    await flush();

    expect(timeline).toContain(msgBackfillKey(200));
    expect(screen.getByText("消息 201（尾页）")).toBeTruthy();
    // 尾页（未满页）后游标清除：不再发起新页请求
    expect(timeline.filter((entry) => entry === msgBackfillKey(201))).toHaveLength(0);
  });
});

describe("reducer 纯函数·应用序单调约束（安全审查 I-6.2）", () => {
  function loadedState() {
    return taskChatReducer(initialTaskChatState, {
      type: "LOAD_SUCCEEDED",
      task: taskView("ready").data.task,
      messages: HISTORY_MESSAGES,
      inputFiles: [INPUT_FILE],
      artifacts: [ARTIFACT],
    });
  }

  it("sequence ≤ 水位的持久帧一律丢弃，水位递增后才应用", () => {
    let state = loadedState();
    expect(state.watermark).toBe(W);

    state = taskChatReducer(state, { type: "FRAME", id: W, event: "status_changed", data: { status: "aborted" } });
    expect(state.task.status).toBe("ready");

    state = taskChatReducer(state, { type: "FRAME", id: W - 1, event: "queued", data: {} });
    expect(state.notice).toBeNull();

    state = taskChatReducer(state, {
      type: "FRAME",
      id: W + 1,
      event: "status_changed",
      data: { status: "aborted", abort_reason: "user_cancel" },
    });
    expect(state.task.status).toBe("aborted");
    expect(state.watermark).toBe(W + 1);
  });
});
