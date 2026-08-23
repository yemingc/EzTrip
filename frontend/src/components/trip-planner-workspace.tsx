"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import { PlanningResults } from "@/components/planning-results";
import {
  apiBaseUrl,
  createPlanningTask,
  getPlanningTask,
  planningEventsUrl,
  type PlannerFormValues,
  type PlanningTaskEvent,
  type PlanningTaskEventKind,
  type PlanningTaskSnapshot,
} from "@/lib/planning-task";

type WorkspacePhase = "idle" | "submitting" | "streaming" | "loading_result" | "complete" | "error";
type ConnectionState = "idle" | "connecting" | "open" | "reconnecting" | "closed";

const eventKinds: PlanningTaskEventKind[] = [
  "task_created",
  "task_started",
  "graph_node_completed",
  "task_awaiting_input",
  "task_succeeded",
  "task_failed",
];

const eventLabels: Record<PlanningTaskEventKind, string> = {
  task_created: "任务已入队",
  task_started: "工作流启动",
  graph_node_completed: "图节点已提交",
  task_awaiting_input: "等待人工审核",
  task_succeeded: "规划已完成",
  task_failed: "任务失败",
};

const nodeLabels: Record<string, string> = {
  run_vertical_slice: "搜索、规划与校验",
  prepare_human_review: "生成审核请求",
  human_review: "人工审核",
  apply_review_decision: "应用审核决定",
};

function connectionLabel(state: ConnectionState) {
  return {
    idle: "尚未连接",
    connecting: "连接进度流",
    open: "SSE 已连接",
    reconnecting: "正在重连",
    closed: "进度流已结束",
  }[state];
}

function formatEventTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function PlanningTrace({
  events,
  phase,
  connection,
  taskId,
}: {
  events: PlanningTaskEvent[];
  phase: WorkspacePhase;
  connection: ConnectionState;
  taskId: string | null;
}) {
  const active = phase === "submitting" || phase === "streaming" || phase === "loading_result";

  return (
    <aside className="relative min-h-[420px] overflow-hidden rounded-[1.75rem] bg-slate-950 p-6 text-white shadow-[0_24px_70px_rgba(15,23,42,.2)] sm:p-7">
      <div className="absolute -right-24 -top-24 size-64 rounded-full bg-emerald-400/10 blur-2xl" />
      <div className="relative">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-[11px] font-bold uppercase tracking-[0.2em] text-emerald-300">Live workflow</p>
            <h2 className="mt-2 text-xl font-semibold tracking-tight">Agent 执行轨迹</h2>
          </div>
          <span className="flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-[11px] text-slate-300">
            <span className={`size-2 rounded-full ${active ? "animate-pulse bg-emerald-400" : "bg-slate-500"}`} />
            {connectionLabel(connection)}
          </span>
        </div>

        <div className="mt-4 min-h-5 font-mono text-[10px] text-slate-500">
          {taskId ? `task / ${taskId}` : "等待提交任务"}
        </div>

        <ol className="mt-6 space-y-1" aria-live="polite" data-testid="event-trace">
          {events.length ? (
            events.map((event, index) => (
              <li className="grid grid-cols-[28px_1fr] gap-3" key={event.event_id}>
                <div className="flex flex-col items-center">
                  <span className="mt-1 grid size-7 place-items-center rounded-full border border-emerald-300/30 bg-emerald-400/10 text-[10px] font-bold text-emerald-300">
                    {event.sequence}
                  </span>
                  {index < events.length - 1 ? <span className="min-h-8 w-px flex-1 bg-white/10" /> : null}
                </div>
                <div className="pb-5">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-sm font-semibold text-slate-100">{eventLabels[event.kind]}</p>
                    <time className="font-mono text-[10px] text-slate-500">{formatEventTime(event.occurred_at)}</time>
                  </div>
                  {event.node ? (
                    <p className="mt-1 text-xs font-medium text-emerald-300/80">
                      {nodeLabels[event.node] ?? event.node} · {event.state_status}
                    </p>
                  ) : null}
                  <p className="mt-1 text-xs leading-5 text-slate-500">{event.message}</p>
                </div>
              </li>
            ))
          ) : (
            <li className="rounded-2xl border border-dashed border-white/10 bg-white/[.025] p-5">
              <p className="text-sm font-medium text-slate-300">提交后，这里会显示真实 SSE 事件</p>
              <p className="mt-2 text-xs leading-5 text-slate-500">
                事件来自 LangGraph 节点提交，不是前端定时器模拟。
              </p>
            </li>
          )}
        </ol>
      </div>
    </aside>
  );
}

export function TripPlannerWorkspace({ defaultStartDate }: { defaultStartDate: string }) {
  const [values, setValues] = useState<PlannerFormValues>({
    rawText: "帮我规划一次北京两日历史文化之旅，节奏轻松一些。",
    originCity: "上海",
    startDate: defaultStartDate,
    adults: 2,
    budgetLimit: "",
  });
  const [phase, setPhase] = useState<WorkspacePhase>("idle");
  const [connection, setConnection] = useState<ConnectionState>("idle");
  const [events, setEvents] = useState<PlanningTaskEvent[]>([]);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<PlanningTaskSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sourceRef = useRef<EventSource | null>(null);
  const terminalRef = useRef(false);
  const recoveryRef = useRef(false);

  useEffect(() => {
    return () => sourceRef.current?.close();
  }, []);

  const updateValue = <Key extends keyof PlannerFormValues>(
    key: Key,
    value: PlannerFormValues[Key],
  ) => {
    setValues((current) => ({ ...current, [key]: value }));
  };

  async function loadFinalSnapshot(currentTaskId: string) {
    setPhase("loading_result");
    try {
      const result = await getPlanningTask(currentTaskId);
      setSnapshot(result);
      if (result.status === "failed") {
        setError(result.failure?.user_message ?? "规划任务失败，请稍后重试。");
        setPhase("error");
      } else {
        setPhase("complete");
      }
    } catch (snapshotError) {
      setError(snapshotError instanceof Error ? snapshotError.message : "无法读取规划结果");
      setPhase("error");
    }
  }

  function connectToEvents(currentTaskId: string) {
    setConnection("connecting");
    const source = new EventSource(planningEventsUrl(currentTaskId));
    sourceRef.current = source;

    source.onopen = () => setConnection("open");
    source.onerror = () => {
      if (!terminalRef.current) {
        setConnection("reconnecting");
        if (!recoveryRef.current) {
          recoveryRef.current = true;
          void getPlanningTask(currentTaskId)
            .then((currentSnapshot) => {
              if (["awaiting_input", "succeeded", "failed"].includes(currentSnapshot.status)) {
                terminalRef.current = true;
                source.close();
                setConnection("closed");
                setSnapshot(currentSnapshot);
                if (currentSnapshot.status === "failed") {
                  setError(currentSnapshot.failure?.user_message ?? "规划任务失败，请稍后重试。");
                  setPhase("error");
                } else {
                  setPhase("complete");
                }
              }
            })
            .catch(() => undefined)
            .finally(() => {
              recoveryRef.current = false;
            });
        }
      }
    };

    const receiveEvent = (message: MessageEvent<string>) => {
      let event: PlanningTaskEvent;
      try {
        event = JSON.parse(message.data) as PlanningTaskEvent;
      } catch {
        terminalRef.current = true;
        source.close();
        setConnection("closed");
        setError("进度事件不符合 JSON 协议，任务展示已停止。");
        setPhase("error");
        return;
      }
      setEvents((current) =>
        current.some((item) => item.event_id === event.event_id)
          ? current
          : [...current, event].sort((left, right) => left.sequence - right.sequence),
      );

      if (["task_awaiting_input", "task_succeeded", "task_failed"].includes(event.kind)) {
        terminalRef.current = true;
        source.close();
        setConnection("closed");
        void loadFinalSnapshot(currentTaskId);
      }
    };

    for (const kind of eventKinds) {
      source.addEventListener(kind, receiveEvent as EventListener);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    sourceRef.current?.close();
    terminalRef.current = false;
    recoveryRef.current = false;
    setEvents([]);
    setSnapshot(null);
    setTaskId(null);
    setError(null);
    setPhase("submitting");
    setConnection("idle");

    try {
      const accepted = await createPlanningTask(values);
      setTaskId(accepted.task_id);
      setPhase("streaming");
      connectToEvents(accepted.task_id);
    } catch (submitError) {
      setError(
        submitError instanceof Error
          ? submitError.message
          : `无法连接规划 API（${apiBaseUrl}）`,
      );
      setPhase("error");
      setConnection("closed");
    }
  }

  function reset() {
    sourceRef.current?.close();
    terminalRef.current = false;
    recoveryRef.current = false;
    setPhase("idle");
    setConnection("idle");
    setEvents([]);
    setTaskId(null);
    setSnapshot(null);
    setError(null);
  }

  const isBusy = ["submitting", "streaming", "loading_result"].includes(phase);

  return (
    <>
      <section className="mx-auto grid max-w-[1480px] gap-5 px-4 pb-8 sm:px-6 lg:grid-cols-[minmax(0,1.2fr)_minmax(380px,.8fr)] lg:px-8">
        <form
          className="rounded-[1.75rem] border border-white/80 bg-white/88 p-6 shadow-[0_24px_70px_rgba(15,23,42,.08)] backdrop-blur sm:p-8"
          onSubmit={submit}
        >
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="eyebrow">Create a planning task</p>
              <h2 className="mt-2 text-2xl font-semibold tracking-[-0.03em] text-slate-950">
                描述这次北京旅行
              </h2>
            </div>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-[11px] font-semibold text-slate-500">
              2 日可验证演示
            </span>
          </div>

          <label className="mt-6 block">
            <span className="field-label">旅行需求</span>
            <textarea
              className="field-control min-h-28 resize-y"
              disabled={isBusy}
              maxLength={500}
              onChange={(event) => updateValue("rawText", event.target.value)}
              required
              value={values.rawText}
            />
          </label>
          <p className="mt-2 text-[11px] leading-5 text-slate-500">
            当前版本会保留这段原始需求，但不会假装已经完成中文语义抽取；下方已确认约束才会进入确定性工作流。
          </p>

          <div className="mt-5 rounded-2xl border border-emerald-900/10 bg-emerald-50/70 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs font-bold uppercase tracking-[0.14em] text-emerald-900">已确认硬约束</p>
              <span className="text-[10px] text-emerald-800/60">Fixture provider 支持</span>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <span className="constraint-chip">必须游览 · 故宫博物院</span>
              <span className="constraint-chip">必须游览 · 天坛公园</span>
              <span className="constraint-chip constraint-chip-soft">偏好 · 历史文化 / 轻步行</span>
            </div>
          </div>

          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            <label>
              <span className="field-label">出发城市</span>
              <input
                className="field-control"
                disabled={isBusy}
                onChange={(event) => updateValue("originCity", event.target.value)}
                placeholder="例如：上海"
                value={values.originCity}
              />
            </label>
            <label>
              <span className="field-label">出发日期</span>
              <input
                className="field-control"
                disabled={isBusy}
                min={defaultStartDate}
                onChange={(event) => updateValue("startDate", event.target.value)}
                required
                type="date"
                value={values.startDate}
              />
            </label>
            <label>
              <span className="field-label">成人数量</span>
              <select
                className="field-control"
                disabled={isBusy}
                onChange={(event) => updateValue("adults", Number(event.target.value))}
                value={values.adults}
              >
                {[1, 2, 3, 4, 5, 6].map((count) => (
                  <option key={count} value={count}>{count} 人</option>
                ))}
              </select>
            </label>
            <label>
              <span className="field-label">整趟预算（可选）</span>
              <div className="relative">
                <span className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-sm text-slate-400">¥</span>
                <input
                  className="field-control pl-8"
                  disabled={isBusy}
                  min="1"
                  onChange={(event) => updateValue("budgetLimit", event.target.value)}
                  placeholder="留空则不校验预算"
                  step="1"
                  type="number"
                  value={values.budgetLimit}
                />
              </div>
            </label>
          </div>

          {error ? (
            <div className="mt-5 rounded-2xl border border-rose-200 bg-rose-50 p-4" role="alert">
              <p className="text-sm font-semibold text-rose-900">任务没有完成</p>
              <p className="mt-1 text-xs leading-5 text-rose-700">{error}</p>
              <p className="mt-2 break-all font-mono text-[10px] text-rose-500">API: {apiBaseUrl}</p>
            </div>
          ) : null}

          <div className="mt-6 flex flex-wrap items-center gap-3">
            <button
              className="primary-button"
              data-testid="submit-planning-task"
              disabled={isBusy}
              type="submit"
            >
              {phase === "submitting"
                ? "正在创建任务…"
                : phase === "streaming" || phase === "loading_result"
                  ? "Agent 正在规划…"
                  : "开始多 Agent 规划"}
              <span aria-hidden="true">→</span>
            </button>
            {phase === "complete" || phase === "error" ? (
              <button className="secondary-button" onClick={reset} type="button">重新开始</button>
            ) : null}
            <p className="text-[11px] text-slate-400">目标城市固定为北京 · 行程固定为 2 天</p>
          </div>
        </form>

        <PlanningTrace
          connection={connection}
          events={events}
          phase={phase}
          taskId={taskId}
        />
      </section>

      {snapshot ? <PlanningResults snapshot={snapshot} /> : null}
    </>
  );
}
