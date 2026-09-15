import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { V2ApiError } from "./client.js";
import {
  SSE_DEFAULT_RETRY_AFTER_SECONDS,
  SSE_MAX_BUFFER_BYTES,
  createTaskStream,
  fetchEvents,
} from "./sse.js";

/**
 * T8 SSE 客户端测试（D10 语义，用例清单对应 Phase 8 计划 Task 8 Step 1 ①-⑦）。
 * 流 mock：真 Response + ReadableStream 分段喂帧（Node 24 undici 原生支持）。
 * 定时器：假时钟 + Math.random 打桩（jitter 归零/放大）断言精确退避序列。
 */

const EVENTS_OK = {
  data: { events: [], snapshot: { status: "uploading", event_sequence: 0 } },
};

const ENCODER = new TextEncoder();

function jsonResponse(body, { status = 200, headers } = {}) {
  const init = { status, headers: headers ?? { "Content-Type": "application/json" } };
  return new Response(JSON.stringify(body), init);
}

/** SSE 流响应：chunks 分段喂帧；close=false 时流保持打开（模拟活连接） */
function sseResponse(chunks, { close = true } = {}) {
  const stream = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(ENCODER.encode(chunk));
      }
      if (close) {
        controller.close();
      }
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function isStreamUrl(url) {
  return url.includes("/events/stream");
}

function isEventsUrl(url) {
  return url.includes("/events?") && !url.includes("/events/stream");
}

/** 可编程路由 fetch mock：记录全部调用（url/init），按 handler 分派响应 */
function installFetchRouter(handler) {
  const calls = [];
  const fetchMock = vi.fn(async (input, init) => {
    const url = typeof input === "string" ? input : String(input?.url ?? "");
    calls.push({ url, init });
    return handler(url, init, calls.length);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

/** 兼容普通对象与 Headers 实例两种 init.headers 形态 */
function headersOf(init) {
  const headers = init?.headers;
  if (headers instanceof Headers) {
    return Object.fromEntries(headers.entries());
  }
  return { ...headers };
}

/** 排空微任务与已到点定时器（+1ms 推进不会触发秒级退避定时器） */
async function flush() {
  await vi.advanceTimersByTimeAsync(1);
}

function streamCallCount(calls) {
  return calls.filter((entry) => isStreamUrl(entry.url)).length;
}

function eventsCallUrls(calls) {
  return calls.filter((entry) => isEventsUrl(entry.url)).map((entry) => entry.url);
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("fetchEvents", () => {
  it("GET /events?after=&limit=1000（补拉上限页），返回 {events, snapshot}", async () => {
    const { calls } = installFetchRouter((url) => {
      if (!isEventsUrl(url)) {
        throw new Error(`fetchEvents 不应触发 ${url}`);
      }
      return jsonResponse({
        data: {
          events: [{ sequence: 1, type: "status_changed", status: "queued" }],
          snapshot: { status: "queued", event_sequence: 1 },
        },
      });
    });

    const result = await fetchEvents("task-1", 0);

    expect(result.events).toHaveLength(1);
    expect(result.events[0]).toMatchObject({ sequence: 1, type: "status_changed" });
    expect(result.snapshot).toEqual({ status: "queued", event_sequence: 1 });
    expect(calls[0].url).toBe("/api/v2/tasks/task-1/events?after=0&limit=1000");
    expect(calls[0].init.credentials).toBe("same-origin");
  });
});

describe("createTaskStream", () => {
  // 用例 ①
  it("帧解析：id/event/data 三行+空行分帧（跨 chunk 拼接、心跳注释行跳过），逐帧回调", async () => {
    const frames = [];
    installFetchRouter((url) =>
      isStreamUrl(url)
        ? sseResponse([
            "id: 12\nevent: mess",
            `age_saved\ndata: {"message_id":"m1","event_sequence":12}\n\nevent: text_delta\ndata: {"text":"你"}\n\n: ping\n\nid: 14\nevent: done\ndata: {"finish_reason":"stop"}\n\n`,
          ])
        : jsonResponse(EVENTS_OK)
    );

    const stream = createTaskStream({
      taskId: "task-1",
      after: 11,
      onFrame: (frame) => frames.push(frame),
    });
    await flush();

    expect(frames).toEqual([
      { id: 12, event: "message_saved", data: { message_id: "m1", event_sequence: 12 } },
      { id: null, event: "text_delta", data: { text: "你" } },
      { id: 14, event: "done", data: { finish_reason: "stop" } },
    ]);
    stream.close();
  });

  it("D10 请求形态：fetch 流（非 EventSource）、credentials same-origin、无 Authorization、after 透传", async () => {
    const { calls } = installFetchRouter((url) =>
      isStreamUrl(url) ? sseResponse([]) : jsonResponse(EVENTS_OK)
    );

    const stream = createTaskStream({ taskId: "task-1", after: 7, onFrame: () => {} });
    await flush();

    expect(calls[0].url).toBe("/api/v2/tasks/task-1/events/stream?after=7");
    expect(calls[0].init.method).toBe("GET");
    expect(calls[0].init.credentials).toBe("same-origin");
    const headers = headersOf(calls[0].init);
    expect(headers.Accept).toBe("text/event-stream");
    expect(headers).not.toHaveProperty("Authorization");
    stream.close();
  });

  // 用例 ②
  it("水位只被持久帧推进：瞬态帧（无 id 行）不动 lastSeq", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0); // 抖动归零：重连恰好 1s
    let phase = 0;
    const { calls } = installFetchRouter((url) => {
      if (isStreamUrl(url)) {
        phase += 1;
        if (phase === 1) {
          return sseResponse([
            'event: meta\ndata: {"status":"uploading"}\n\n',
            "event: queued\ndata: {}\n\n",
          ]);
        }
        return sseResponse([
          "event: meta\ndata: {}\n\n",
          'id: 7\nevent: status_changed\ndata: {"status":"ready"}\n\n',
          "event: text_delta\ndata: {}\n\n",
        ]);
      }
      return jsonResponse(EVENTS_OK);
    });

    const stream = createTaskStream({
      taskId: "task-1",
      after: 0,
      onFrame: () => {},
      onEvents: () => {},
    });
    await flush();
    await vi.advanceTimersByTimeAsync(1000);
    await flush();
    await vi.advanceTimersByTimeAsync(1000);
    await flush();

    const eventsUrls = eventsCallUrls(calls);
    expect(eventsUrls).toHaveLength(2);
    expect(eventsUrls[0]).toContain("/events?after=0&"); // 首段全瞬态 → 水位仍 0
    expect(eventsUrls[1]).toContain("/events?after=7&"); // 仅持久帧 id:7 推进
    stream.close();
  });

  // 用例 ③
  it("断流重连：先 /events 对账（先于 stream 续连），watermark 大于本地时经 onEvents 补拉", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0); // 抖动归零：重连恰好 1s
    const onEvents = vi.fn();
    let phase = 0;
    const { calls } = installFetchRouter((url) => {
      if (isStreamUrl(url)) {
        phase += 1;
        return phase === 1
          ? sseResponse(['id: 2\nevent: done\ndata: {}\n\n'])
          : sseResponse(["event: meta\ndata: {}\n\n"], { close: false });
      }
      return jsonResponse({
        data: {
          events: [
            { sequence: 3, type: "status_changed", status: "queued" },
            {
              sequence: 5,
              type: "message_saved",
              message_id: "m9",
              event_sequence: 5,
              author: "user",
            },
          ],
          snapshot: { status: "running", event_sequence: 5 },
        },
      });
    });

    const stream = createTaskStream({
      taskId: "task-1",
      after: 0,
      onFrame: () => {},
      onEvents,
    });
    await flush();
    await vi.advanceTimersByTimeAsync(1000);
    await flush();

    const secondStreamIdx = calls.findIndex(
      (entry, index) => index > 0 && isStreamUrl(entry.url)
    );
    const eventsIdx = calls.findIndex((entry) => isEventsUrl(entry.url));
    expect(eventsIdx).toBeGreaterThan(-1);
    expect(secondStreamIdx).toBeGreaterThan(eventsIdx); // 对账调用先于 stream 续连

    expect(onEvents).toHaveBeenCalledTimes(1);
    const [eventsArg, snapshotArg] = onEvents.mock.calls[0];
    expect(eventsArg).toHaveLength(2);
    expect(eventsArg[0]).toMatchObject({ sequence: 3, type: "status_changed" });
    expect(eventsArg[1]).toMatchObject({ sequence: 5, type: "message_saved" });
    expect(snapshotArg).toEqual({ status: "running", event_sequence: 5 });
    // 对账后水位对齐快照 watermark → 续连 after=5
    expect(calls[secondStreamIdx].url).toContain("/events/stream?after=5");
    stream.close();
  });

  // 用例 ④
  describe("退避与 429 Retry-After", () => {
    it("退避序列 1/2/4/8…（random=0 抖动归零，安全带检查点断言倍增间隔）", async () => {
      vi.spyOn(Math, "random").mockReturnValue(0);
      const { calls } = installFetchRouter((url) =>
        isStreamUrl(url)
          ? jsonResponse(
              { error: { code: "SERVICE_UNAVAILABLE", message: "流不可用" } },
              { status: 503 }
            )
          : jsonResponse(EVENTS_OK)
      );

      const stream = createTaskStream({
        taskId: "task-1",
        after: 0,
        onFrame: () => {},
        onEvents: () => {},
      });
      await flush();

      // 定时器在 mount flush（clock≈1）排定：各次重连到期点 ≈1001/3001/7001/15001。
      // 检查点带 ≥499ms 安全边距，断言相邻重连间隔 1s→2s→4s→8s 倍增。
      expect(streamCallCount(calls)).toBe(1);
      await vi.advanceTimersByTimeAsync(500);
      expect(streamCallCount(calls)).toBe(1); // 1s 未到不重连
      await vi.advanceTimersByTimeAsync(1000);
      expect(streamCallCount(calls)).toBe(2); // 1.5s → 第 1 次重连（间隔 1s）
      await vi.advanceTimersByTimeAsync(1000);
      expect(streamCallCount(calls)).toBe(2); // 2.5s → 间隔 2s 未到不重连
      await vi.advanceTimersByTimeAsync(1000);
      expect(streamCallCount(calls)).toBe(3); // 3.5s → 第 2 次重连（间隔 2s）
      await vi.advanceTimersByTimeAsync(3000);
      expect(streamCallCount(calls)).toBe(3); // 6.5s → 间隔 4s 未到不重连
      await vi.advanceTimersByTimeAsync(1000);
      expect(streamCallCount(calls)).toBe(4); // 7.5s → 第 3 次重连（间隔 4s）
      await vi.advanceTimersByTimeAsync(7000);
      expect(streamCallCount(calls)).toBe(4); // 14.5s → 间隔 8s 未到不重连
      await vi.advanceTimersByTimeAsync(1000);
      expect(streamCallCount(calls)).toBe(5); // 15.5s → 第 4 次重连（间隔 8s）
      stream.close();
    });

    it("429 遵守 Retry-After（有效秒数作退避下限）", async () => {
      vi.spyOn(Math, "random").mockReturnValue(0);
      let phase = 0;
      const onError = vi.fn();
      const { calls } = installFetchRouter((url) => {
        if (isStreamUrl(url)) {
          phase += 1;
          return phase === 1
            ? jsonResponse(
                { error: { code: "RATE_LIMITED", message: "重连过于频繁" } },
                {
                  status: 429,
                  headers: { "Content-Type": "application/json", "Retry-After": "90" },
                }
              )
            : sseResponse(["event: meta\ndata: {}\n\n"], { close: false });
        }
        return jsonResponse(EVENTS_OK);
      });

      const stream = createTaskStream({
        taskId: "task-1",
        after: 0,
        onFrame: () => {},
        onEvents: () => {},
        onError,
      });
      await flush();

      expect(onError).toHaveBeenCalledTimes(1);
      expect(onError.mock.calls[0][0]).toBeInstanceOf(V2ApiError);
      expect(onError.mock.calls[0][0].retryAfter).toBe(90);

      // 定时器在 mount flush（clock≈1）排定，90s 下限到期点 ≈90001：60s 处不重连
      await vi.advanceTimersByTimeAsync(60_000);
      expect(streamCallCount(calls)).toBe(1); // 90s 下限内不重连
      await vi.advanceTimersByTimeAsync(60_000);
      await flush();
      expect(streamCallCount(calls)).toBe(2); // 越 90s 到点续连
      stream.close();
    });

    it("429 Retry-After 缺失/非法 → 默认下限 60s（安全 Minor 4）", async () => {
      expect(SSE_DEFAULT_RETRY_AFTER_SECONDS).toBe(60);
      vi.spyOn(Math, "random").mockReturnValue(0);
      let phase = 0;
      const { calls } = installFetchRouter((url) => {
        if (isStreamUrl(url)) {
          phase += 1;
          // 缺失 Retry-After 头（非法值 "soon" 同走默认——Number() 不可解析）
          return phase === 1
            ? jsonResponse(
                { error: { code: "RATE_LIMITED", message: "重连过于频繁" } },
                { status: 429 }
              )
            : sseResponse(["event: meta\ndata: {}\n\n"], { close: false });
        }
        return jsonResponse(EVENTS_OK);
      });

      const stream = createTaskStream({
        taskId: "task-1",
        after: 0,
        onFrame: () => {},
        onEvents: () => {},
      });
      await flush();

      // 60s 默认下限到期点 ≈60001：45s 处不重连，75s 处已续连
      await vi.advanceTimersByTimeAsync(45_000);
      expect(streamCallCount(calls)).toBe(1); // 60s 下限内不重连
      await vi.advanceTimersByTimeAsync(30_000);
      await flush();
      expect(streamCallCount(calls)).toBe(2);
      stream.close();
    });

    it("抖动只加不减：random=0.5 时 60s 下限放大为 69s，绝不早于下限", async () => {
      vi.spyOn(Math, "random").mockReturnValue(0.5);
      let phase = 0;
      const { calls } = installFetchRouter((url) => {
        if (isStreamUrl(url)) {
          phase += 1;
          return phase === 1
            ? jsonResponse({ error: { code: "RATE_LIMITED", message: "x" } }, { status: 429 })
            : sseResponse(["event: meta\ndata: {}\n\n"], { close: false });
        }
        return jsonResponse(EVENTS_OK);
      });

      const stream = createTaskStream({
        taskId: "task-1",
        after: 0,
        onFrame: () => {},
        onEvents: () => {},
      });
      await flush();

      await vi.advanceTimersByTimeAsync(60_000);
      expect(streamCallCount(calls)).toBe(1); // 60s 下限整点不触发（jitter 只加，到期 ≈69s）
      await vi.advanceTimersByTimeAsync(15_000); // 60s * 1.15 = 69s
      expect(streamCallCount(calls)).toBe(2);
      stream.close();
    });
  });

  // 用例 ⑤
  it("close() 清理全部定时器与 AbortController：之后零请求零回调，幂等", async () => {
    // 场景 a：流打开中（看门狗定时器在场）→ close
    let phase = 0;
    const onError = vi.fn();
    const { calls } = installFetchRouter((url) => {
      if (isStreamUrl(url)) {
        phase += 1;
        return phase === 1
          ? sseResponse(["event: meta\ndata: {}\n\n"], { close: false })
          : sseResponse(["event: meta\ndata: {}\n\n"]);
      }
      return jsonResponse(EVENTS_OK);
    });
    const stream = createTaskStream({
      taskId: "task-1",
      after: 0,
      onFrame: () => {},
      onError,
    });
    await flush();
    expect(vi.getTimerCount()).toBeGreaterThan(0); // 看门狗定时器在场
    const callsBeforeClose = calls.length;
    stream.close();
    expect(vi.getTimerCount()).toBe(0);
    stream.close(); // 幂等
    await vi.advanceTimersByTimeAsync(120_000);
    await flush();
    expect(calls.length).toBe(callsBeforeClose);
    expect(onError).not.toHaveBeenCalled(); // close 触发的 abort 不算错误

    // 场景 b：断流后重连定时器挂起 → close
    const { calls: calls2 } = installFetchRouter((url) =>
      isStreamUrl(url) ? sseResponse(["event: meta\ndata: {}\n\n"]) : jsonResponse(EVENTS_OK)
    );
    const stream2 = createTaskStream({ taskId: "task-1", after: 0, onFrame: () => {} });
    await flush();
    expect(vi.getTimerCount()).toBe(1); // 仅剩重连定时器
    stream2.close();
    expect(vi.getTimerCount()).toBe(0);
    const count2 = calls2.length;
    await vi.advanceTimersByTimeAsync(120_000);
    await flush();
    expect(calls2.length).toBe(count2);
  });

  // 用例 ⑥a
  it("恶意帧：data 非 JSON → onError（不崩溃、帧跳过、该帧 id 不推进水位）", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0); // 抖动归零：重连恰好 1s
    const frames = [];
    const onError = vi.fn();
    let phase = 0;
    const { calls } = installFetchRouter((url) => {
      if (isStreamUrl(url)) {
        phase += 1;
        return phase === 1
          ? sseResponse([
              'id: 2\nevent: done\ndata: {"ok":true}\n\n',
              "id: 9\nevent: done\ndata: {broken\n\n",
            ])
          : sseResponse(["event: meta\ndata: {}\n\n"], { close: false });
      }
      return jsonResponse(EVENTS_OK);
    });

    const stream = createTaskStream({
      taskId: "task-1",
      after: 0,
      onFrame: (frame) => frames.push(frame),
      onError,
    });
    await flush();

    expect(frames).toEqual([{ id: 2, event: "done", data: { ok: true } }]); // 坏帧不交付
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toBeInstanceOf(V2ApiError);
    expect(onError.mock.calls[0][0].code).toBe("SSE_PARSE_ERROR");
    // 水位钉在最后完好持久帧：坏帧 id:9 未污染 → 对账 after=2（若被推进将是 9）
    await vi.advanceTimersByTimeAsync(1000);
    await flush();
    expect(eventsCallUrls(calls)[0]).toContain("/events?after=2&");
    stream.close();
  });

  // 用例 ⑥b（安全审查 I-6.1：防 NaN 水位污染后 ?after=NaN 400 死循环烧穿限流）
  it("恶意帧：id 行非正整数 → 忽略该 id（水位不动），帧内容照常交付", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0); // 抖动归零：重连恰好 1s
    const frames = [];
    let phase = 0;
    const { calls } = installFetchRouter((url) => {
      if (isStreamUrl(url)) {
        phase += 1;
        return phase === 1
          ? sseResponse([
              'id: 5\nevent: done\ndata: {"n":5}\n\n',
              "id: abc\nevent: done\ndata: {\"n\":6}\n\n",
              "id: -3\nevent: done\ndata: {\"n\":7}\n\n",
              "id: 0\nevent: done\ndata: {\"n\":8}\n\n",
              "id: 99999999999999999999\nevent: done\ndata: {\"n\":9}\n\n",
            ])
          : sseResponse(["event: meta\ndata: {}\n\n"], { close: false });
      }
      return jsonResponse(EVENTS_OK);
    });

    const stream = createTaskStream({
      taskId: "task-1",
      after: 0,
      onFrame: (frame) => frames.push(frame),
    });
    await flush();

    expect(frames.map((frame) => frame.id)).toEqual([5, null, null, null, null]);
    await vi.advanceTimersByTimeAsync(1000);
    await flush();
    expect(eventsCallUrls(calls)[0]).toContain("/events?after=5&"); // 水位钉在合法持久帧
    stream.close();
  });

  // 用例 ⑦
  it("解析 buffer 设上限：超限无边界帧被丢弃+报错，边界后恢复解析", async () => {
    const frames = [];
    const onError = vi.fn();
    const oversized = `data: ${"x".repeat(SSE_MAX_BUFFER_BYTES + 1024)}`; // 无空行边界
    const half = Math.floor(oversized.length / 2);
    installFetchRouter((url) =>
      isStreamUrl(url)
        ? sseResponse([
            oversized.slice(0, half),
            oversized.slice(half),
            '\n\nid: 3\nevent: done\ndata: {"ok":1}\n\n',
          ])
        : jsonResponse(EVENTS_OK)
    );

    const stream = createTaskStream({
      taskId: "task-1",
      after: 0,
      onFrame: (frame) => frames.push(frame),
      onError,
    });
    await flush();

    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0].code).toBe("SSE_FRAME_TOO_LARGE");
    expect(frames).toEqual([{ id: 3, event: "done", data: { ok: 1 } }]); // 丢弃后恢复
    stream.close();
  });
});
