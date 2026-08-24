"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import { PlanningResults } from "@/components/planning-results";
import {
  apiBaseUrl,
  buildPlanRevisionRequest,
  confirmRequestIntake,
  createPlanningTask,
  getPlanningTask,
  planningEventsUrl,
  previewRequestIntakeValues,
  proposeRequestIntake,
  resolveDestination,
  submitPlanningTaskReview,
  type DestinationResolution,
  type HumanReviewAction,
  type PlanRevisionRequest,
  type PlanRevisionSelection,
  type PlannerFormValues,
  type PlanningTaskEvent,
  type PlanningTaskEventKind,
  type PlanningTaskSnapshot,
  type RequestConfirmationDraft,
  type RequestFieldDecisionStatus,
  type RequestIntakeSelection,
} from "@/lib/planning-task";

type WorkspacePhase = "idle" | "submitting" | "streaming" | "loading_result" | "complete" | "error";
type ConnectionState = "idle" | "connecting" | "open" | "reconnecting" | "closed";

const eventKinds: PlanningTaskEventKind[] = [
  "task_created",
  "task_started",
  "graph_node_completed",
  "task_awaiting_input",
  "task_review_submitted",
  "task_succeeded",
  "task_failed",
];

const eventLabels: Record<PlanningTaskEventKind, string> = {
  task_created: "任务已入队",
  task_started: "工作流启动",
  graph_node_completed: "图节点已提交",
  task_awaiting_input: "等待人工审核",
  task_review_submitted: "审核决定已接收",
  task_succeeded: "规划已完成",
  task_failed: "任务失败",
};

const nodeLabels: Record<string, string> = {
  run_vertical_slice: "搜索、规划与校验",
  run_specialists: "景点、住宿与天气并行查询",
  build_materials: "合并路线与预算材料",
  run_plan_agent: "生成多 Agent 行程草案",
  validate_hard_plan: "执行硬约束校验",
  run_repair: "执行有界局部修复",
  prepare_human_review: "生成审核请求",
  human_review: "人工审核",
  apply_review_decision: "应用审核决定",
  apply_plan_revision: "应用局部修改",
};

const requestFieldLabels: Record<string, string> = {
  origin_city: "出发地",
  destination_city: "目的地",
  start_date: "出发日期",
  trip_days: "行程天数",
  adults: "成人",
  children: "儿童",
  seniors: "老人",
  budget_limit: "总预算",
  pace: "行程节奏",
  travel_style: "旅行主题",
};

const requestStatusLabels: Record<RequestFieldDecisionStatus, string> = {
  matched: "与表单一致",
  conflict: "需要选择",
  proposed: "原文新增",
  unmentioned: "沿用表单",
  needs_confirmation: "无法唯一确定",
};

const requestStatusClasses: Record<RequestFieldDecisionStatus, string> = {
  matched: "bg-emerald-100 text-emerald-800",
  conflict: "bg-amber-100 text-amber-900",
  proposed: "bg-cyan-100 text-cyan-900",
  unmentioned: "bg-slate-100 text-slate-600",
  needs_confirmation: "bg-rose-100 text-rose-800",
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
    rawText: "帮我规划一次历史文化之旅，节奏轻松一些。",
    originCity: "上海",
    destinationCity: "北京",
    startDate: defaultStartDate,
    tripDays: 2,
    adults: 2,
    children: 0,
    seniors: 0,
    rooms: 1,
    budgetLimit: "3000",
    pace: "relaxed",
    dataMode: "fixture",
  });
  const [phase, setPhase] = useState<WorkspacePhase>("idle");
  const [connection, setConnection] = useState<ConnectionState>("idle");
  const [events, setEvents] = useState<PlanningTaskEvent[]>([]);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<PlanningTaskSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [destinationResolution, setDestinationResolution] =
    useState<DestinationResolution | null>(null);
  const [selectedDestinationAdcode, setSelectedDestinationAdcode] =
    useState<string | null>(null);
  const [intakeDraft, setIntakeDraft] = useState<RequestConfirmationDraft | null>(null);
  const [intakeSelection, setIntakeSelection] =
    useState<RequestIntakeSelection>("proposal");
  const sourceRef = useRef<EventSource | null>(null);
  const terminalRef = useRef(false);
  const recoveryRef = useRef(false);
  const pendingReviewRef = useRef<{
    decisionId: string;
    reviewId: string;
    action: HumanReviewAction;
    reviewerId: string;
    comment?: string;
    revisionKey?: string;
    revisionRequest?: PlanRevisionRequest;
  } | null>(null);

  useEffect(() => {
    return () => sourceRef.current?.close();
  }, []);

  const updateValue = <Key extends keyof PlannerFormValues>(
    key: Key,
    value: PlannerFormValues[Key],
  ) => {
    setValues((current) => ({ ...current, [key]: value }));
    setIntakeDraft(null);
    setDestinationResolution(null);
    setSelectedDestinationAdcode(null);
    setError(null);
  };

  async function resolveIntakeDestination(
    draft: RequestConfirmationDraft,
    selection: RequestIntakeSelection,
  ) {
    const preview = previewRequestIntakeValues(values, draft, selection);
    const resolution = await resolveDestination(preview);
    setDestinationResolution(resolution);
    if (resolution.status === "resolved") {
      setSelectedDestinationAdcode(resolution.candidates[0].administrative_code);
      setError(null);
      return;
    }
    setSelectedDestinationAdcode(null);
    if (resolution.status === "ambiguous") {
      setError(null);
      return;
    }
    setError(
      resolution.status === "unsupported"
        ? "Fixture 模式仅覆盖北京、上海和成都。请选择实时 Provider，或改用演示城市。"
        : "没有解析到可规划的国内城市，请补充省份或检查名称。",
    );
  }

  async function changeIntakeSelection(selection: RequestIntakeSelection) {
    if (!intakeDraft) {
      return;
    }
    setIntakeSelection(selection);
    setPhase("submitting");
    try {
      await resolveIntakeDestination(intakeDraft, selection);
      setPhase("idle");
    } catch (selectionError) {
      setError(selectionError instanceof Error ? selectionError.message : "无法重新解析目的地。");
      setPhase("error");
    }
  }

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

  function connectToEvents(currentTaskId: string, afterSequence = 0) {
    setConnection("connecting");
    const source = new EventSource(planningEventsUrl(currentTaskId, afterSequence));
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
    setReviewError(null);
    setIntakeDraft(null);
    setPhase("submitting");
    setConnection("idle");

    try {
      const draft = await proposeRequestIntake(values);
      const selection = draft.proposal_can_confirm ? "proposal" : "form";
      setIntakeDraft(draft);
      setIntakeSelection(selection);
      await resolveIntakeDestination(draft, selection);
      setPhase("idle");
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

  async function confirmAndCreatePlanningTask() {
    if (!intakeDraft || !selectedDestinationAdcode) {
      setError("请先确认系统理解和正确的目的行政区。");
      return;
    }
    setPhase("submitting");
    setError(null);
    try {
      const confirmed = await confirmRequestIntake(
        intakeDraft.draft_id,
        intakeSelection,
        selectedDestinationAdcode,
      );
      const accepted = await createPlanningTask(confirmed);
      setTaskId(accepted.task_id);
      setPhase("streaming");
      connectToEvents(accepted.task_id);
    } catch (confirmationError) {
      setError(
        confirmationError instanceof Error
          ? confirmationError.message
          : `无法连接规划 API（${apiBaseUrl}）`,
      );
      setPhase("error");
      setConnection("closed");
    }
  }

  async function submitReview(
    action: HumanReviewAction,
    comment?: string,
    revisionSelection?: PlanRevisionSelection,
  ) {
    const review = snapshot?.result?.state.review_request;
    if (!taskId || !snapshot || !review) {
      setReviewError("当前没有可提交的审核请求。");
      return;
    }

    const normalizedComment = comment?.trim() || undefined;
    const revisionKey = revisionSelection
      ? `${revisionSelection.targetDate}:${revisionSelection.shiftMinutes}`
      : undefined;
    if (action === "request_revision" && !revisionSelection) {
      setReviewError("请选择修改日期和延后幅度。");
      return;
    }
    const existing = pendingReviewRef.current;
    let decision: NonNullable<typeof pendingReviewRef.current>;
    try {
      decision =
      existing &&
      existing.reviewId === review.review_id &&
      existing.action === action &&
      existing.comment === normalizedComment &&
      existing.revisionKey === revisionKey
        ? existing
        : {
            decisionId: `review-decision-${crypto.randomUUID().replaceAll("-", "")}`,
            reviewId: review.review_id,
            action,
            reviewerId: `web-reviewer-${crypto.randomUUID().replaceAll("-", "")}`,
            comment: normalizedComment,
            revisionKey,
            revisionRequest:
              action === "request_revision" && revisionSelection
                ? buildPlanRevisionRequest(snapshot, revisionSelection)
                : undefined,
          };
    } catch (revisionError) {
      setReviewError(
        revisionError instanceof Error ? revisionError.message : "无法构造结构化修改请求。",
      );
      return;
    }
    pendingReviewRef.current = decision;

    sourceRef.current?.close();
    terminalRef.current = false;
    recoveryRef.current = false;
    setReviewError(null);
    setPhase("streaming");
    setConnection("connecting");

    try {
      await submitPlanningTaskReview(taskId, decision);
      pendingReviewRef.current = null;
      connectToEvents(taskId, snapshot.event_count);
    } catch (reviewSubmitError) {
      setReviewError(
        reviewSubmitError instanceof Error
          ? reviewSubmitError.message
          : "审核决定提交失败，请重试。",
      );
      setPhase("complete");
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
    setReviewError(null);
    setDestinationResolution(null);
    setSelectedDestinationAdcode(null);
    setIntakeDraft(null);
    setIntakeSelection("proposal");
    pendingReviewRef.current = null;
  }

  const isBusy = ["submitting", "streaming", "loading_result"].includes(phase);
  const selectedDestination = destinationResolution?.candidates.find(
    (candidate) => candidate.administrative_code === selectedDestinationAdcode,
  );
  const previewValues = intakeDraft
    ? previewRequestIntakeValues(values, intakeDraft, intakeSelection)
    : values;

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
                规划{previewValues.destinationCity.trim() || "国内城市"}旅行
              </h2>
            </div>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-[11px] font-semibold text-slate-500">
              {previewValues.tripDays} 日 · {values.dataMode === "fixture" ? "可回放 Fixture" : "实时 Provider"}
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
            系统会先提议字段和约束、标出原文 evidence 与表单冲突；只有你确认后，才会调用旅行检索和规划链路。
          </p>

          <div className="mt-5 rounded-2xl border border-emerald-900/10 bg-emerald-50/70 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs font-bold uppercase tracking-[0.14em] text-emerald-900">城市事实与结构化输入</p>
              <span className="text-[10px] text-emerald-800/60">
                {values.dataMode === "fixture" ? "三城市 Fixture" : "高德 live 解析"}
              </span>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <span className="constraint-chip">
                目的地 · {selectedDestination?.qualified_name ?? `${previewValues.destinationCity}（待解析）`}
              </span>
              <span className="constraint-chip">行程 · {previewValues.tripDays} 天</span>
              <span className="constraint-chip constraint-chip-soft">
                同行 · {previewValues.adults} 成人 / {previewValues.children} 儿童 / {previewValues.seniors} 老人
              </span>
              {intakeDraft?.proposed_fields.travel_styles.length ? (
                <span className="constraint-chip constraint-chip-soft">
                  原文主题 · {intakeDraft.proposed_fields.travel_styles.join(" / ")}
                </span>
              ) : null}
            </div>
            {selectedDestination ? (
              <p className="mt-3 text-[11px] leading-5 text-emerald-900/70" data-testid="destination-resolution">
                已由 {selectedDestination.source.provider} 解析 · adcode {selectedDestination.administrative_code} · {selectedDestination.level}
              </p>
            ) : null}
            {destinationResolution?.status === "ambiguous" ? (
              <div className="mt-4" data-testid="destination-ambiguity">
                <p className="text-xs font-semibold text-amber-900">这个名称对应多个行政区，请先确认：</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {destinationResolution.candidates.map((candidate) => (
                    <button
                      className={
                        candidate.administrative_code === selectedDestinationAdcode
                          ? "rounded-full border border-emerald-700 bg-emerald-700 px-3 py-1.5 text-xs font-semibold text-white"
                          : "rounded-full border border-amber-300 bg-white px-3 py-1.5 text-xs font-semibold text-amber-900"
                      }
                      key={candidate.administrative_code}
                      onClick={() => setSelectedDestinationAdcode(candidate.administrative_code)}
                      type="button"
                    >
                      {candidate.qualified_name}
                    </button>
                  ))}
                </div>
                <p className="mt-2 text-[11px] text-amber-800/70">选择正确行政区后，再确认需求并开始规划。</p>
              </div>
            ) : null}
          </div>

          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            <label>
              <span className="field-label">目的城市</span>
              <input
                className="field-control"
                disabled={isBusy}
                onChange={(event) => updateValue("destinationCity", event.target.value)}
                placeholder="例如：泉州"
                required
                value={values.destinationCity}
              />
            </label>
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
              <span className="field-label">行程天数</span>
              <select
                className="field-control"
                disabled={isBusy}
                onChange={(event) => updateValue("tripDays", Number(event.target.value))}
                value={values.tripDays}
              >
                {[2, 3, 4, 5].map((count) => (
                  <option key={count} value={count}>{count} 天</option>
                ))}
              </select>
            </label>
            <label>
              <span className="field-label">成人数量</span>
              <select
                className="field-control"
                disabled={isBusy}
                onChange={(event) => updateValue("adults", Number(event.target.value))}
                value={values.adults}
              >
                {[0, 1, 2, 3, 4, 5, 6].map((count) => (
                  <option key={count} value={count}>{count} 人</option>
                ))}
              </select>
            </label>
            <label>
              <span className="field-label">儿童数量</span>
              <select
                className="field-control"
                disabled={isBusy}
                onChange={(event) => updateValue("children", Number(event.target.value))}
                value={values.children}
              >
                {[0, 1, 2, 3, 4].map((count) => (
                  <option key={count} value={count}>{count} 人</option>
                ))}
              </select>
            </label>
            <label>
              <span className="field-label">老人数量</span>
              <select
                className="field-control"
                disabled={isBusy}
                onChange={(event) => updateValue("seniors", Number(event.target.value))}
                value={values.seniors}
              >
                {[0, 1, 2, 3, 4].map((count) => (
                  <option key={count} value={count}>{count} 人</option>
                ))}
              </select>
            </label>
            <label>
              <span className="field-label">房间数量</span>
              <select
                className="field-control"
                disabled={isBusy}
                onChange={(event) => updateValue("rooms", Number(event.target.value))}
                value={values.rooms}
              >
                {[1, 2, 3, 4].map((count) => (
                  <option key={count} value={count}>{count} 间</option>
                ))}
              </select>
            </label>
            <label>
              <span className="field-label">行程节奏</span>
              <select
                className="field-control"
                disabled={isBusy}
                onChange={(event) =>
                  updateValue("pace", event.target.value as PlannerFormValues["pace"])
                }
                value={values.pace}
              >
                <option value="relaxed">轻松 · 每天 2–3 个主要活动</option>
                <option value="standard">标准 · 每天 3–4 个主要活动</option>
              </select>
            </label>
            <label>
              <span className="field-label">整趟预算目标</span>
              <div className="relative">
                <span className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-sm text-slate-400">¥</span>
                <input
                  className="field-control pl-8"
                  disabled={isBusy}
                  min="1"
                  onChange={(event) => updateValue("budgetLimit", event.target.value)}
                  placeholder="例如：3000"
                  required
                  step="1"
                  type="number"
                  value={values.budgetLimit}
                />
              </div>
            </label>
            <label className="sm:col-span-2">
              <span className="field-label">旅行数据模式</span>
              <select
                className="field-control"
                disabled={isBusy}
                onChange={(event) =>
                  updateValue("dataMode", event.target.value as PlannerFormValues["dataMode"])
                }
                value={values.dataMode}
              >
                <option value="fixture">Fixture · 北京 / 上海 / 成都，可回放且不调用外部 API</option>
                <option value="live">实时 Provider · 动态解析国内城市，会调用高德与 DeepSeek</option>
              </select>
            </label>
          </div>

          {values.dataMode === "live" ? (
            <p className="mt-3 text-[11px] leading-5 text-amber-700">
              当前选择会直接启用实时调用，并消耗高德与模型配额；Key 仍只保存在服务端，所有城市可输入，但规划质量不会被描述为全部已验证。
            </p>
          ) : null}

          {intakeDraft ? (
            <section
              className="mt-5 rounded-2xl border border-cyan-200 bg-cyan-50/70 p-4"
              data-testid="request-intake-confirmation"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-cyan-950">确认系统理解</p>
                  <p className="mt-1 text-[11px] leading-5 text-cyan-900/70">
                    预检尚未创建规划任务 · {intakeDraft.field_model} / {intakeDraft.constraint_model}
                  </p>
                </div>
                <span className="rounded-full bg-white px-3 py-1 text-[10px] font-semibold text-cyan-800">
                  {intakeDraft.model_call_count} 次模型调用
                </span>
              </div>

              <div className="mt-4 grid gap-2 sm:grid-cols-2">
                {intakeDraft.field_decisions.map((decision, index) => (
                  <div
                    className="rounded-xl border border-cyan-100 bg-white/85 p-3"
                    data-testid="request-field-decision"
                    key={`${decision.field}-${decision.evidence ?? "form"}-${index}`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-xs font-semibold text-slate-900">
                        {requestFieldLabels[decision.field] ?? decision.field}
                      </p>
                      <span className={`rounded-full px-2 py-1 text-[9px] font-semibold ${requestStatusClasses[decision.status]}`}>
                        {requestStatusLabels[decision.status]}
                      </span>
                    </div>
                    <p className="mt-2 text-[11px] leading-5 text-slate-600">
                      {decision.proposed_value ?? decision.raw_proposed_value
                        ? `原文提议：${decision.proposed_value ?? decision.raw_proposed_value}`
                        : "原文未提及"}
                      {decision.form_value !== null ? ` · 表单：${decision.form_value}` : ""}
                    </p>
                    {decision.evidence ? (
                      <p className="mt-1 text-[10px] leading-4 text-cyan-800">
                        evidence · “{decision.evidence}”{decision.evidence_mode === "inferred" ? " · 推断" : ""}
                      </p>
                    ) : null}
                  </div>
                ))}
              </div>

              {intakeDraft.constraint_decisions.length ? (
                <div className="mt-4">
                  <p className="text-[11px] font-semibold text-cyan-950">偏好与约束</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {intakeDraft.constraint_decisions.map((decision) => (
                      <span className="constraint-chip" key={decision.constraint.constraint_id}>
                        {decision.constraint.kind} · {String(decision.constraint.value)} · “{decision.evidence}”
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}

              {intakeDraft.clarifications.length ? (
                <ul className="mt-4 space-y-1 rounded-xl bg-amber-50 p-3 text-[11px] leading-5 text-amber-900">
                  {intakeDraft.clarifications.map((item) => <li key={item}>· {item}</li>)}
                </ul>
              ) : null}

              <fieldset className="mt-4 grid gap-2 sm:grid-cols-2">
                <legend className="mb-2 text-[11px] font-semibold text-cyan-950">采用哪组结构化字段</legend>
                <label className="flex items-start gap-2 rounded-xl border border-cyan-200 bg-white p-3 text-xs text-slate-700">
                  <input
                    checked={intakeSelection === "proposal"}
                    disabled={isBusy || !intakeDraft.proposal_can_confirm}
                    name="intake-selection"
                    onChange={() => void changeIntakeSelection("proposal")}
                    type="radio"
                  />
                  <span><strong>采用原文提议</strong><br />有效提议覆盖表单；模糊字段继续沿用已显示的表单值。</span>
                </label>
                <label className="flex items-start gap-2 rounded-xl border border-cyan-200 bg-white p-3 text-xs text-slate-700">
                  <input
                    checked={intakeSelection === "form"}
                    disabled={isBusy}
                    name="intake-selection"
                    onChange={() => void changeIntakeSelection("form")}
                    type="radio"
                  />
                  <span><strong>保留结构化表单</strong><br />不采用本次字段、主题或约束提议，并记录该决定。</span>
                </label>
              </fieldset>

              <button
                className="primary-button mt-4 w-full"
                data-testid="confirm-request-intake"
                disabled={isBusy || !selectedDestinationAdcode}
                onClick={() => void confirmAndCreatePlanningTask()}
                type="button"
              >
                确认理解并开始规划 <span aria-hidden="true">→</span>
              </button>
            </section>
          ) : null}

          {error ? (
            <div
              className="mt-5 rounded-2xl border border-rose-200 bg-rose-50 p-4"
              data-testid="planning-error"
              role="alert"
            >
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
                ? intakeDraft ? "正在确认并创建任务…" : "正在理解需求…"
                : phase === "streaming" || phase === "loading_result"
                  ? "Agent 正在规划…"
                  : intakeDraft ? "重新理解需求" : "理解旅行需求"}
              <span aria-hidden="true">→</span>
            </button>
            {phase === "complete" || phase === "error" ? (
              <button className="secondary-button" onClick={reset} type="button">重新开始</button>
            ) : null}
            <p className="text-[11px] text-slate-400">
              {values.destinationCity || "目的地待填写"} · {values.tripDays} 天 · 确认前不会进入旅行检索与规划
            </p>
          </div>
        </form>

        <PlanningTrace
          connection={connection}
          events={events}
          phase={phase}
          taskId={taskId}
        />
      </section>

      {snapshot ? (
        <PlanningResults
          onReview={submitReview}
          reviewBusy={phase === "streaming" || phase === "loading_result"}
          reviewError={reviewError}
          snapshot={snapshot}
        />
      ) : null}
    </>
  );
}
