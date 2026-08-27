"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { PlanningResults } from "@/components/planning-results";
import {
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
  type RequestFieldDecision,
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
  task_created: "已收到旅行需求",
  task_started: "开始生成行程",
  graph_node_completed: "完成一项规划步骤",
  task_awaiting_input: "行程等待确认",
  task_review_submitted: "已收到你的选择",
  task_succeeded: "行程已完成",
  task_failed: "行程生成失败",
};

const nodeLabels: Record<string, string> = {
  run_vertical_slice: "整理旅行方案",
  run_specialists: "查找景点、住宿与天气",
  build_materials: "计算路线与预算",
  run_plan_agent: "安排每日行程",
  validate_hard_plan: "检查时间与行程要求",
  run_repair: "调整不合理安排",
  prepare_human_review: "准备确认",
  human_review: "等待你的确认",
  apply_review_decision: "保存你的选择",
  apply_plan_revision: "更新所选行程",
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

function friendlyClarification(value: string) {
  const withFieldLabels = Object.entries(requestFieldLabels).reduce(
    (current, [field, label]) => current.replaceAll(field, label),
    value,
  );
  return withFieldLabels
    .replaceAll("原文", "旅行需求")
    .replaceAll("结构化表单", "当前填写")
    .replaceAll("V1 请求契约", "当前规划条件")
    .replaceAll(",", "，");
}

const constraintKindLabels: Record<string, string> = {
  avoid: "不想去",
  interest: "感兴趣",
  must_visit: "一定要去",
  preference: "偏好",
};

function requestValueLabel(field: string, value: unknown) {
  if (value === null || value === undefined) return "";
  if (field === "pace") {
    return value === "relaxed" ? "轻松" : value === "standard" ? "标准" : String(value);
  }
  return Array.isArray(value) ? value.join("、") : String(value);
}

function requestFieldValueFromPreview(
  values: PlannerFormValues,
  field: RequestFieldDecision["field"],
) {
  const fieldValues: Partial<Record<RequestFieldDecision["field"], unknown>> = {
    origin_city: values.originCity.trim() || "未填写",
    destination_city: values.destinationCity.trim() || "未填写",
    start_date: values.startDate,
    trip_days: values.tripDays,
    adults: values.adults,
    children: values.children,
    seniors: values.seniors,
    budget_limit: values.budgetLimit.trim() || "未填写",
    pace: values.pace,
  };
  return fieldValues[field];
}

const requestStatusClasses: Record<RequestFieldDecisionStatus, string> = {
  matched: "bg-emerald-100 text-emerald-800",
  conflict: "bg-amber-100 text-amber-900",
  proposed: "bg-cyan-100 text-cyan-900",
  unmentioned: "bg-slate-100 text-slate-600",
  needs_confirmation: "bg-rose-100 text-rose-800",
};

const planningTaskIdPattern = /^planning-task-[a-f0-9]{32}$/;

function writeTaskIdToUrl(taskId: string | null) {
  const url = new URL(window.location.href);
  if (taskId) {
    url.searchParams.set("task_id", taskId);
  } else {
    url.searchParams.delete("task_id");
  }
  const nextUrl = `${url.pathname}${url.search}${url.hash}`;
  const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (nextUrl === currentUrl) {
    return;
  }
  window.history.replaceState(
    window.history.state,
    "",
    nextUrl,
  );
}

function connectionLabel(state: ConnectionState) {
  return {
    idle: "等待开始",
    connecting: "正在连接",
    open: "实时更新中",
    reconnecting: "正在恢复进度",
    closed: "进度已更新",
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
            <p className="text-[11px] font-bold tracking-[0.16em] text-emerald-300">规划进度</p>
            <h2 className="mt-2 text-xl font-semibold tracking-tight">正在准备你的行程</h2>
          </div>
          <span className="flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-[11px] text-slate-300">
            <span className={`size-2 rounded-full ${active ? "animate-pulse bg-emerald-400" : "bg-slate-500"}`} />
            {connectionLabel(connection)}
          </span>
        </div>

        <div className="mt-4 min-h-5 text-[11px] text-slate-500">
          {taskId ? "本次行程可以在刷新页面后继续查看" : "填写需求后即可开始"}
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
                  {event.node && event.kind === "graph_node_completed" ? (
                    <p className="mt-1 text-xs font-medium text-emerald-300/80">
                      {nodeLabels[event.node] ?? "更新行程进度"}
                    </p>
                  ) : null}
                </div>
              </li>
            ))
          ) : (
            <li className="rounded-2xl border border-dashed border-white/10 bg-white/[.025] p-5">
              <p className="text-sm font-medium text-slate-300">开始后，这里会显示规划进度</p>
              <p className="mt-2 text-xs leading-5 text-slate-500">
                景点、住宿、路线和预算会依次整理，完成后即可查看并调整。
              </p>
            </li>
          )}
        </ol>
      </div>
    </aside>
  );
}

export function TripPlannerWorkspace({
  defaultStartDate,
  earliestStartDate,
}: {
  defaultStartDate: string;
  earliestStartDate: string;
}) {
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
  const resultsRef = useRef<HTMLElement | null>(null);
  const lastRevealedResultRef = useRef<string | null>(null);
  const pendingReviewRef = useRef<{
    decisionId: string;
    reviewId: string;
    action: HumanReviewAction;
    reviewerId: string;
    comment?: string;
    revisionKey?: string;
    revisionRequest?: PlanRevisionRequest;
  } | null>(null);

  const revealResults = useCallback(() => {
    window.requestAnimationFrame(() => {
      resultsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      resultsRef.current?.focus({ preventScroll: true });
    });
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
        ? "示例体验仅支持北京、上海和成都。请选择“实时规划”，或改用示例城市。"
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

  const loadFinalSnapshot = useCallback(async (currentTaskId: string) => {
    setPhase("loading_result");
    try {
      const result = await getPlanningTask(currentTaskId);
      setSnapshot(result);
      if (result.status === "failed") {
        setError(result.failure?.user_message ?? "行程生成失败，请稍后重试。");
        setPhase("error");
      } else {
        setPhase("complete");
      }
    } catch (snapshotError) {
      setError(snapshotError instanceof Error ? snapshotError.message : "无法读取规划结果");
      setPhase("error");
    }
  }, []);

  const connectToEvents = useCallback((
    currentTaskId: string,
    afterSequence = 0,
    knownEventCount = 0,
  ) => {
    sourceRef.current?.close();
    setConnection("connecting");
    const source = new EventSource(planningEventsUrl(currentTaskId, afterSequence));
    sourceRef.current = source;
    let receivedSequence = afterSequence;

    source.onopen = () => setConnection("open");
    source.onerror = () => {
      if (!terminalRef.current) {
        setConnection("reconnecting");
        if (!recoveryRef.current) {
            recoveryRef.current = true;
          void getPlanningTask(currentTaskId)
            .then((currentSnapshot) => {
              if (
                ["awaiting_input", "succeeded", "failed"].includes(currentSnapshot.status) &&
                receivedSequence >= currentSnapshot.event_count
              ) {
                terminalRef.current = true;
                source.close();
                setConnection("closed");
                setSnapshot(currentSnapshot);
                if (currentSnapshot.status === "failed") {
                  setError(currentSnapshot.failure?.user_message ?? "行程生成失败，请稍后重试。");
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
        setError("规划进度暂时无法读取，请刷新页面重试。");
        setPhase("error");
        return;
      }
      setEvents((current) =>
        current.some((item) => item.event_id === event.event_id)
          ? current
          : [...current, event].sort((left, right) => left.sequence - right.sequence),
      );
      receivedSequence = Math.max(receivedSequence, event.sequence);

      if (
        ["task_awaiting_input", "task_succeeded", "task_failed"].includes(event.kind) &&
        event.sequence >= knownEventCount
      ) {
        terminalRef.current = true;
        source.close();
        setConnection("closed");
        void loadFinalSnapshot(currentTaskId);
      }
    };

    for (const kind of eventKinds) {
      source.addEventListener(kind, receiveEvent as EventListener);
    }
  }, [loadFinalSnapshot]);

  useEffect(() => {
    let cancelled = false;
    const restoredTaskId = new URL(window.location.href).searchParams.get("task_id");
    if (!restoredTaskId) {
      return () => sourceRef.current?.close();
    }
    if (!planningTaskIdPattern.test(restoredTaskId)) {
      queueMicrotask(() => {
        if (!cancelled) {
          setError("链接中的行程信息无效，请重新开始规划。");
          setPhase("error");
          setConnection("closed");
        }
      });
      return () => {
        cancelled = true;
        sourceRef.current?.close();
      };
    }

    void getPlanningTask(restoredTaskId)
      .then((restoredSnapshot) => {
        if (cancelled) {
          return;
        }
        terminalRef.current = false;
        recoveryRef.current = false;
        setTaskId(restoredTaskId);
        setConnection("connecting");
        setSnapshot(restoredSnapshot);
        if (restoredSnapshot.status === "failed") {
          setError(restoredSnapshot.failure?.user_message ?? "行程生成失败，请重新开始。");
          setPhase("error");
        } else if (["queued", "running"].includes(restoredSnapshot.status)) {
          setPhase("streaming");
        } else {
          setPhase("complete");
        }
        connectToEvents(restoredTaskId, 0, restoredSnapshot.event_count);
      })
      .catch((restoreError) => {
        if (cancelled) {
          return;
        }
        setError(
          restoreError instanceof Error
            ? `无法恢复之前的行程：${restoreError.message}`
            : "无法恢复之前的行程。",
        );
        setPhase("error");
        setConnection("closed");
      });

    return () => {
      cancelled = true;
      sourceRef.current?.close();
    };
  }, [connectToEvents]);

  useEffect(() => {
    if (phase !== "complete" || !snapshot || !taskId) {
      return;
    }
    const resultKey = `${taskId}:${snapshot.plan_versions.at(-1)?.version_id ?? snapshot.updated_at}`;
    if (lastRevealedResultRef.current === resultKey) {
      return;
    }
    lastRevealedResultRef.current = resultKey;
    const timer = window.setTimeout(revealResults, 0);
    return () => window.clearTimeout(timer);
  }, [phase, revealResults, snapshot, taskId]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    sourceRef.current?.close();
    terminalRef.current = false;
    recoveryRef.current = false;
    setEvents([]);
    setSnapshot(null);
    setTaskId(null);
    lastRevealedResultRef.current = null;
    writeTaskIdToUrl(null);
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
          : "暂时无法连接行程服务，请稍后重试。",
      );
      setPhase("error");
      setConnection("closed");
    }
  }

  async function confirmAndCreatePlanningTask() {
    if (!intakeDraft || !selectedDestinationAdcode) {
      setError("请先确认旅行信息和正确的目的地。");
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
      writeTaskIdToUrl(accepted.task_id);
      setTaskId(accepted.task_id);
      setPhase("streaming");
      connectToEvents(accepted.task_id);
    } catch (confirmationError) {
      setError(
        confirmationError instanceof Error
          ? confirmationError.message
          : "暂时无法连接行程服务，请稍后重试。",
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
      setReviewError("当前没有等待确认的行程。");
      return;
    }

    const normalizedComment = comment?.trim() || undefined;
    const revisionKey = revisionSelection
      ? revisionSelection.kind === "replace_activity"
        ? `${revisionSelection.targetDate}:replace:${revisionSelection.replacedItemId}:${revisionSelection.replacementCandidateId}`
        : revisionSelection.kind === "replace_day_activities"
          ? `${revisionSelection.targetDate}:replace-day:${revisionSelection.replacements
              .map(
                (item) => `${item.replacedItemId}:${item.replacementCandidateId}`,
              )
              .join(",")}`
          : `${revisionSelection.targetDate}:shift:${revisionSelection.shiftMinutes}`
      : undefined;
    if (action === "request_revision" && !revisionSelection) {
      setReviewError("请选择局部修改方式及其目标。");
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
        revisionError instanceof Error ? revisionError.message : "无法准备本次修改，请重试。",
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
          : "本次选择未能保存，请重试。",
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
    lastRevealedResultRef.current = null;
    writeTaskIdToUrl(null);
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
  const conflictDecisions =
    intakeDraft?.field_decisions.filter((decision) => decision.status === "conflict") ?? [];
  const travelStyles = intakeDraft?.proposed_fields.travel_styles ?? [];
  const preferenceLabels = intakeDraft
    ? [
        ...travelStyles.map((style) => `旅行主题 · ${style}`),
        ...intakeDraft.constraint_decisions
          .filter((decision) => !travelStyles.includes(String(decision.constraint.value)))
          .map(
            (decision) =>
              `${constraintKindLabels[decision.constraint.kind] ?? "旅行偏好"} · ${String(decision.constraint.value)}`,
          ),
      ]
    : [];
  const visibleClarifications =
    intakeDraft?.clarifications.filter(
      (item) => !conflictDecisions.some((decision) => item.startsWith(`${decision.field} `)),
    ) ?? [];
  const planningIsComplete = phase === "complete" && Boolean(snapshot);

  return (
    <>
      {taskId && phase !== "error" ? (
        <section
          className={`mx-auto grid max-w-[1480px] gap-5 px-4 pb-8 sm:px-6 lg:px-8 ${
            planningIsComplete
              ? ""
              : "lg:grid-cols-[minmax(0,.8fr)_minmax(380px,1.2fr)]"
          }`}
          data-testid="planning-task-summary"
        >
          <article
            aria-live="polite"
            className="rounded-[1.75rem] border border-emerald-900/10 bg-white/90 p-6 shadow-[0_20px_55px_rgba(15,23,42,.08)] sm:p-7"
          >
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <p className="eyebrow">{planningIsComplete ? "行程已生成" : "正在生成行程"}</p>
                <h2 className="mt-2 text-2xl font-semibold tracking-[-0.03em] text-slate-950">
                  {previewValues.destinationCity} · {previewValues.tripDays} 天
                </h2>
                <p className="mt-2 text-sm leading-6 text-slate-500">
                  {planningIsComplete
                    ? "结果已经准备好，可以查看、确认或局部调整。"
                    : "已收起填写内容，景点、路线、住宿和预算正在整理。"}
                </p>
              </div>
              <span
                className={`rounded-full px-3 py-1.5 text-[11px] font-semibold ${
                  planningIsComplete
                    ? "bg-emerald-100 text-emerald-800"
                    : "bg-cyan-100 text-cyan-900"
                }`}
              >
                {planningIsComplete ? "可以查看" : connectionLabel(connection)}
              </span>
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              <span className="constraint-chip">
                {previewValues.adults} 成人 / {previewValues.children} 儿童 / {previewValues.seniors} 老人
              </span>
              <span className="constraint-chip constraint-chip-soft">
                预算 · {previewValues.budgetLimit || "未填写"}
              </span>
              <span className="constraint-chip constraint-chip-soft">
                节奏 · {previewValues.pace === "relaxed" ? "轻松" : "标准"}
              </span>
            </div>
            {planningIsComplete ? (
              <div className="mt-5 flex flex-wrap gap-3">
                <button
                  className="primary-button"
                  data-testid="view-planning-results"
                  onClick={revealResults}
                  type="button"
                >
                  查看行程 <span aria-hidden="true">↓</span>
                </button>
                <button className="secondary-button" onClick={reset} type="button">
                  规划新行程
                </button>
              </div>
            ) : null}
          </article>

          {planningIsComplete ? (
            <details className="rounded-2xl border border-slate-200 bg-white/80 p-4 shadow-sm">
              <summary className="cursor-pointer text-sm font-semibold text-slate-700">
                查看生成过程
              </summary>
              <div className="mt-4">
                <PlanningTrace
                  connection={connection}
                  events={events}
                  phase={phase}
                  taskId={taskId}
                />
              </div>
            </details>
          ) : (
            <PlanningTrace
              connection={connection}
              events={events}
              phase={phase}
              taskId={taskId}
            />
          )}
        </section>
      ) : (
      <section className="mx-auto grid max-w-[1480px] gap-5 px-4 pb-8 sm:px-6 lg:grid-cols-[minmax(0,1.2fr)_minmax(380px,.8fr)] lg:px-8">
        <form
          className="rounded-[1.75rem] border border-white/80 bg-white/88 p-6 shadow-[0_24px_70px_rgba(15,23,42,.08)] backdrop-blur sm:p-8"
          onSubmit={submit}
        >
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="eyebrow">填写旅行信息</p>
              <h2 className="mt-2 text-2xl font-semibold tracking-[-0.03em] text-slate-950">
                规划{previewValues.destinationCity.trim() || "国内城市"}旅行
              </h2>
            </div>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-[11px] font-semibold text-slate-500">
              {previewValues.tripDays} 天行程
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
            点击“检查旅行需求”后，我们会整理目的地、日期和偏好，供你确认后再生成行程。
          </p>

          <div className="mt-5 rounded-2xl border border-emerald-900/10 bg-emerald-50/70 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs font-bold tracking-[0.1em] text-emerald-900">确认行程信息</p>
              <span className="text-[10px] text-emerald-800/60">
                {values.dataMode === "fixture" ? "示例体验" : "实时规划"}
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
                  旅行主题 · {intakeDraft.proposed_fields.travel_styles.join(" / ")}
                </span>
              ) : null}
            </div>
            {selectedDestination ? (
              <p className="mt-3 text-[11px] leading-5 text-emerald-900/70" data-testid="destination-resolution">
                已确认目的地：{selectedDestination.qualified_name}
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
                min={earliestStartDate}
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
              <span className="field-label">规划方式</span>
              <select
                className="field-control"
                disabled={isBusy}
                onChange={(event) =>
                  updateValue("dataMode", event.target.value as PlannerFormValues["dataMode"])
                }
                value={values.dataMode}
              >
                <option value="fixture">示例体验 · 北京 / 上海 / 成都</option>
                <option value="live">实时规划 · 支持国内城市</option>
              </select>
            </label>
          </div>

          {values.dataMode === "live" ? (
            <p className="mt-3 text-[11px] leading-5 text-amber-700">
              将查询实时地点和路线信息，生成时间可能稍长。开放时间、票价和房态请在出发前再次确认。
            </p>
          ) : null}

          <div className="mt-6 flex flex-wrap items-center gap-3">
            <button
              className="primary-button"
              data-testid="submit-planning-task"
              disabled={isBusy}
              type="submit"
            >
              {phase === "submitting"
                ? intakeDraft ? "正在确认信息…" : "正在整理需求…"
                : phase === "streaming" || phase === "loading_result"
                  ? "正在生成行程…"
                  : intakeDraft ? "重新检查需求" : "检查旅行需求"}
              <span aria-hidden="true">→</span>
            </button>
            {phase === "complete" || phase === "error" ? (
              <button className="secondary-button" onClick={reset} type="button">重新开始</button>
            ) : null}
            <p className="text-[11px] text-slate-400">
              {values.destinationCity || "目的地待填写"} · {values.tripDays} 天
            </p>
          </div>

          {intakeDraft ? (
            <p className="mt-3 text-xs font-medium text-cyan-800" role="status">
              需求已整理，请在下方核对后生成行程。
            </p>
          ) : null}

          {intakeDraft ? (
            <section
              className="mt-3 rounded-2xl border border-cyan-200 bg-cyan-50/70 p-4"
              data-testid="request-intake-confirmation"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-cyan-950">确认旅行信息</p>
                  <p className="mt-1 text-[11px] leading-5 text-cyan-900/70">
                    请检查下面的信息是否符合你的想法
                  </p>
                </div>
              </div>

              <div className="mt-4 grid gap-2 sm:grid-cols-2">
                {intakeDraft.field_decisions
                  .filter((decision) => decision.field !== "travel_style")
                  .map((decision, index) => {
                    const finalValue = requestFieldValueFromPreview(previewValues, decision.field);
                    const sourceText =
                      decision.status === "conflict"
                        ? intakeSelection === "proposal"
                          ? "采用需求值"
                          : "保留表单值"
                        : decision.status === "matched"
                          ? "需求与表单一致"
                          : decision.status === "proposed"
                            ? "来自旅行需求"
                            : decision.status === "needs_confirmation"
                              ? "请重点核对"
                              : "来自当前填写";
                    return (
                      <div
                        className="flex items-center justify-between gap-3 rounded-xl border border-cyan-100 bg-white/85 px-3 py-2.5"
                        data-testid="request-field-decision"
                        key={`${decision.field}-${decision.evidence ?? "form"}-${index}`}
                      >
                        <div className="min-w-0">
                          <p className="text-[10px] font-semibold text-slate-500">
                            {requestFieldLabels[decision.field] ?? decision.field}
                          </p>
                          <p className="mt-0.5 truncate text-sm font-semibold text-slate-950">
                            {requestValueLabel(decision.field, finalValue)}
                          </p>
                        </div>
                        <span
                          className={`shrink-0 rounded-full px-2 py-1 text-[9px] font-semibold ${requestStatusClasses[decision.status]}`}
                        >
                          {sourceText}
                        </span>
                      </div>
                    );
                  })}
              </div>

              {preferenceLabels.length ? (
                <div className="mt-4">
                  <p className="text-[11px] font-semibold text-cyan-950">偏好和要求</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {preferenceLabels.map((label) => (
                      <span className="constraint-chip" key={label}>
                        {label}
                      </span>
                    ))}
                  </div>
                  <p className="mt-2 text-[10px] leading-4 text-cyan-800/70">
                    无论下面选择哪组差异值，这些偏好和要求都会保留。
                  </p>
                </div>
              ) : null}

              {visibleClarifications.length ? (
                <ul className="mt-4 space-y-1 rounded-xl bg-amber-50 p-3 text-[11px] leading-5 text-amber-900">
                  {visibleClarifications.map((item) => (
                    <li key={item}>· {friendlyClarification(item)}</li>
                  ))}
                </ul>
              ) : null}

              {conflictDecisions.length ? (
                <fieldset
                  className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3"
                  data-testid="request-conflict-selection"
                >
                  <legend className="px-1 text-[11px] font-semibold text-amber-950">
                    {conflictDecisions.length} 项信息与当前填写不同
                  </legend>
                  <div className="mt-2 space-y-2">
                    {conflictDecisions.map((decision) => (
                      <p className="text-[11px] leading-5 text-amber-900" key={decision.field}>
                        <strong>{requestFieldLabels[decision.field] ?? decision.field}</strong>：需求中为
                        “{requestValueLabel(decision.field, decision.proposed_value ?? decision.raw_proposed_value)}”，
                        当前填写为“{requestValueLabel(decision.field, decision.form_value)}”
                      </p>
                    ))}
                  </div>
                  <div className="mt-3 grid gap-2 sm:grid-cols-2">
                    <label className="flex items-start gap-2 rounded-xl border border-amber-200 bg-white p-3 text-xs text-slate-700">
                      <input
                        checked={intakeSelection === "proposal"}
                        disabled={isBusy || !intakeDraft.proposal_can_confirm}
                        name="intake-selection"
                        onChange={() => void changeIntakeSelection("proposal")}
                        type="radio"
                      />
                      <span>
                        <strong>采用需求中的差异值</strong>
                        <br />
                        其余未提及内容继续使用当前填写。
                      </span>
                    </label>
                    <label className="flex items-start gap-2 rounded-xl border border-amber-200 bg-white p-3 text-xs text-slate-700">
                      <input
                        checked={intakeSelection === "form"}
                        disabled={isBusy}
                        name="intake-selection"
                        onChange={() => void changeIntakeSelection("form")}
                        type="radio"
                      />
                      <span>
                        <strong>保留表单中的差异值</strong>
                        <br />
                        仍会保留旅行需求中的主题和偏好。
                      </span>
                    </label>
                  </div>
                </fieldset>
              ) : (
                <p
                  className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-xs leading-5 text-emerald-900"
                  data-testid="merged-intake-notice"
                >
                  已合并旅行需求与当前填写，未发现冲突。
                </p>
              )}

              <button
                className="primary-button mt-4 w-full"
                data-testid="confirm-request-intake"
                disabled={isBusy || !selectedDestinationAdcode}
                onClick={() => void confirmAndCreatePlanningTask()}
                type="button"
              >
                确认信息并生成行程 <span aria-hidden="true">→</span>
              </button>
            </section>
          ) : null}

          {error ? (
            <div
              className="mt-5 rounded-2xl border border-rose-200 bg-rose-50 p-4"
              data-testid="planning-error"
              role="alert"
            >
              <p className="text-sm font-semibold text-rose-900">行程没有生成</p>
              <p className="mt-1 text-xs leading-5 text-rose-700">{error}</p>
            </div>
          ) : null}
        </form>

        <PlanningTrace
          connection={connection}
          events={events}
          phase={phase}
          taskId={taskId}
        />
      </section>
      )}

      {snapshot ? (
        <PlanningResults
          onReview={submitReview}
          reviewBusy={phase === "streaming" || phase === "loading_result"}
          reviewError={reviewError}
          sectionRef={resultsRef}
          snapshot={snapshot}
        />
      ) : null}
    </>
  );
}
