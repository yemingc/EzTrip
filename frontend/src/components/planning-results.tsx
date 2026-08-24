import { useState } from "react";

import type {
  BudgetCategory,
  CandidatePoi,
  HumanReviewAction,
  PlanRevisionSelection,
  PlanningTaskSnapshot,
} from "@/lib/planning-task";

const reviewActionLabels: Record<HumanReviewAction, string> = {
  approve_draft: "已批准草案",
  acknowledge_conflict: "已确认冲突",
  request_revision: "已生成修改草案",
  cancel: "已取消规划",
};

const categoryLabels: Record<BudgetCategory, string> = {
  lodging: "住宿",
  transport: "交通",
  food: "餐饮",
  admission: "门票",
  activity: "活动",
  other: "其他",
};

const repairActionLabels: Record<string, string> = {
  rerun_constraint: "重新解析约束",
  rerun_explore: "重新搜索景点",
  rerun_stay: "重新推荐住宿",
  rerun_route: "重新计算路线",
  replan_day: "重排行程日",
  recalculate_budget: "重新计算预算",
  ask_user: "请求用户确认",
  none: "无需修复",
};

const repairNodeLabels: Record<string, string> = {
  constraint: "Constraint",
  explore: "Explore",
  stay: "Stay",
  weather: "Weather",
  route: "Route",
  plan: "Plan",
  budget: "Budget",
  validator: "Validator",
};

const repairIssueLabels: Record<string, string> = {
  "opening_hours.schedule_outside_verified_window": "营业时间冲突",
};

function formatDate(date: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "long",
    day: "numeric",
    weekday: "short",
  }).format(new Date(`${date}T12:00:00+08:00`));
}

function formatTime(dateTime: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  }).format(new Date(dateTime));
}

function formatMoney(value: string | number) {
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    maximumFractionDigits: 0,
  }).format(Number(value));
}

function SourceModeBadge({ mode }: { mode: string }) {
  return (
    <span className="rounded-full border border-amber-300/80 bg-amber-50 px-2.5 py-1 text-[10px] font-bold uppercase tracking-[0.14em] text-amber-800">
      {mode === "fixture" ? "Fixture 数据" : mode}
    </span>
  );
}

function CoordinateOverview({ candidates }: { candidates: CandidatePoi[] }) {
  const longitudes = candidates.map((item) => item.location.longitude);
  const latitudes = candidates.map((item) => item.location.latitude);
  const minLongitude = Math.min(...longitudes);
  const maxLongitude = Math.max(...longitudes);
  const minLatitude = Math.min(...latitudes);
  const maxLatitude = Math.max(...latitudes);

  const pointPosition = (candidate: CandidatePoi) => {
    const longitudeRange = maxLongitude - minLongitude || 1;
    const latitudeRange = maxLatitude - minLatitude || 1;
    return {
      left: 15 + ((candidate.location.longitude - minLongitude) / longitudeRange) * 70,
      top: 16 + ((maxLatitude - candidate.location.latitude) / latitudeRange) * 46,
    };
  };

  return (
    <article className="overflow-hidden rounded-[1.75rem] border border-slate-200 bg-[#e8eee8] shadow-sm lg:col-span-2">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-900/8 px-6 py-5">
        <div>
          <p className="eyebrow">Spatial evidence</p>
          <h3 className="mt-1 text-lg font-semibold tracking-tight">景点坐标概览</h3>
        </div>
        <span className="rounded-full bg-white/75 px-3 py-1.5 text-xs font-medium text-slate-600">
          坐标视图 · 无地图底图
        </span>
      </div>
      <div className="relative min-h-72 overflow-hidden p-6 sm:min-h-80">
        <div className="absolute inset-0 opacity-70 [background-image:linear-gradient(rgba(15,23,42,.07)_1px,transparent_1px),linear-gradient(90deg,rgba(15,23,42,.07)_1px,transparent_1px)] [background-size:32px_32px]" />
        <div className="absolute -right-16 -top-20 size-72 rounded-full border-[42px] border-emerald-900/5" />
        <svg
          className="absolute inset-0 size-full"
          aria-hidden="true"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
        >
          {candidates.length > 1 ? (
            <path
              d={`M ${pointPosition(candidates[0]).left} ${pointPosition(candidates[0]).top} L ${pointPosition(candidates[1]).left} ${pointPosition(candidates[1]).top}`}
              fill="none"
              stroke="rgba(6,78,59,.32)"
              strokeDasharray="2 2"
              strokeWidth="0.7"
            />
          ) : null}
        </svg>
        {candidates.map((candidate, index) => {
          const position = pointPosition(candidate);
          return (
            <div
              className="absolute z-10 -translate-x-1/2 -translate-y-1/2"
              key={candidate.candidate_id}
              style={{ left: `${position.left}%`, top: `${position.top}%` }}
            >
              <div className="group relative">
                <span className="grid size-11 place-items-center rounded-full border-4 border-white bg-emerald-950 text-sm font-bold text-white shadow-lg">
                  {index + 1}
                </span>
                <div className="absolute left-1/2 top-13 w-48 -translate-x-1/2 rounded-2xl border border-white/80 bg-white/92 p-3 text-center shadow-lg backdrop-blur">
                  <p className="text-sm font-semibold text-slate-950">{candidate.name}</p>
                  <p className="mt-1 text-[11px] text-slate-500">
                    {candidate.location.longitude.toFixed(4)}, {candidate.location.latitude.toFixed(4)}
                  </p>
                </div>
              </div>
            </div>
          );
        })}
        <p className="absolute bottom-4 left-5 z-10 max-w-sm rounded-xl bg-slate-950/82 px-3 py-2 text-[11px] leading-5 text-white/80 backdrop-blur">
          位置点来自 provider 返回值；这里只做相对位置表达，不声称路线距离或真实地图能力。
        </p>
      </div>
    </article>
  );
}

export function PlanningResults({
  snapshot,
  onReview,
  reviewBusy,
  reviewError,
}: {
  snapshot: PlanningTaskSnapshot;
  onReview: (
    action: HumanReviewAction,
    comment?: string,
    revision?: PlanRevisionSelection,
  ) => void | Promise<void>;
  reviewBusy: boolean;
  reviewError: string | null;
}) {
  const [showRevisionForm, setShowRevisionForm] = useState(false);
  const [revisionComment, setRevisionComment] = useState("");
  const [revisionTargetDate, setRevisionTargetDate] = useState("");
  const [revisionShiftMinutes, setRevisionShiftMinutes] = useState(120);
  const state = snapshot.result?.state;
  const verticalSlice = state?.vertical_slice;
  const productPlan = state?.plan;
  const productValidation = state?.validation;
  if (!state || (!verticalSlice && (!productPlan || !productValidation))) {
    return null;
  }

  const latestVersion = snapshot.plan_versions.at(-1);
  const revisionResult = state.revision_result;
  const basePlan = productPlan ?? verticalSlice?.plan;
  const baseValidation = productValidation ?? verticalSlice?.validation;
  if (!basePlan || !baseValidation) {
    return null;
  }
  const plan = latestVersion?.plan ?? basePlan;
  const validation = revisionResult?.validation ?? baseValidation;
  const review = state.review_request;
  const reviewOutcome = snapshot.review_outcome;
  const candidates =
    state.materials?.shortlist.poi_candidates ?? verticalSlice?.upstream.candidates ?? [];
  const specialists = state.specialists;
  const materials = state.materials;
  const repair = state.repair;
  const stay = materials?.shortlist.primary_stay;
  const budget = validation.budget;
  const hasBudget = budget.status !== "not_requested";
  const displayCity = plan.destination_city.replace(/市$/, "");
  const selectedRevisionDate = revisionTargetDate || plan.days.at(-1)?.date || plan.start_date;
  const fromVersionNumber = reviewOutcome
    ? snapshot.plan_versions.find(
        (item) => item.version_id === reviewOutcome.plan_diff.from_version_id,
      )?.version_number
    : undefined;
  const toVersionNumber = reviewOutcome
    ? snapshot.plan_versions.find(
        (item) => item.version_id === reviewOutcome.plan_diff.to_version_id,
      )?.version_number
    : undefined;

  return (
    <section className="mx-auto mt-8 max-w-[1480px] px-4 pb-12 sm:px-6 lg:px-8" data-testid="planning-results">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="eyebrow">Planning output</p>
          <h2 className="mt-2 text-3xl font-semibold tracking-[-0.035em] text-slate-950">
            {displayCity} · {plan.days.length} 日规划草案
          </h2>
          <p className="mt-2 text-sm text-slate-500">
            任务 {snapshot.task_id.slice(0, 18)}… · 更新于 {new Date(snapshot.updated_at).toLocaleString("zh-CN")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <SourceModeBadge mode={snapshot.data_mode} />
          <span className={`status-pill status-${validation.status}`}>
            {validation.status === "passed"
              ? "确定性校验通过"
              : validation.status === "warning"
                ? "校验有提醒"
                : "存在硬冲突"}
          </span>
        </div>
      </div>

      {state.workflow_version === "product-planning-graph-v2" && specialists && materials ? (
        <article
          className="mb-5 rounded-[1.75rem] border border-emerald-900/10 bg-emerald-950 p-5 text-white shadow-sm sm:p-6"
          data-testid="product-graph-summary"
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-[11px] font-bold uppercase tracking-[0.18em] text-emerald-300">
                Product Graph V2
              </p>
              <h3 className="mt-2 text-lg font-semibold">一次请求内完成多 Agent 协作</h3>
            </div>
            <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-emerald-100">
              {specialists.total_model_call_count} 次模型调用 · {specialists.total_provider_call_count} 次工具调用
            </span>
          </div>
          <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            {specialists.branches.map((branch) => (
              <div className="rounded-2xl bg-white/7 p-4" key={branch.specialist}>
                <p className="text-[10px] font-bold uppercase tracking-[0.16em] text-emerald-300">
                  {branch.specialist}
                </p>
                <p className="mt-2 text-sm font-semibold">{branch.status}</p>
              </div>
            ))}
            <div className="rounded-2xl bg-white/7 p-4">
              <p className="text-[10px] font-bold uppercase tracking-[0.16em] text-emerald-300">
                Route / Budget
              </p>
              <p className="mt-2 text-sm font-semibold">
                {materials.route_matrix.succeeded_edge_count}/{materials.route_matrix.expected_edge_count} 路线
              </p>
            </div>
            <div className="rounded-2xl bg-white/7 p-4">
              <p className="text-[10px] font-bold uppercase tracking-[0.16em] text-emerald-300">
                Hard Validator
              </p>
              <p className="mt-2 text-sm font-semibold">{validation.status}</p>
            </div>
          </div>
          <p className="mt-4 text-xs leading-5 text-emerald-100/65">
            {stay
              ? `住宿锚点：${stay.name}（${stay.area_name}）；价格与可订状态未验证，不提供预订。`
              : "当前没有可用住宿锚点；系统不会伪造酒店推荐。"}
          </p>
        </article>
      ) : null}

      {repair ? (
        <article
          className="mb-5 rounded-[1.75rem] border border-cyan-200 bg-cyan-50/75 p-5 shadow-sm sm:p-6"
          data-testid="product-repair-summary"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="eyebrow">Bounded repair trace</p>
              <h3 className="mt-2 text-lg font-semibold text-slate-950">有界自动修复</h3>
              <p className="mt-2 max-w-3xl text-xs leading-5 text-slate-600">
                硬校验发现可修复错误后，只重跑对应责任节点；未受影响的 Agent 与工具结果继续复用。
              </p>
            </div>
            <span className="rounded-full border border-cyan-200 bg-white px-3 py-1.5 text-xs font-semibold text-cyan-900">
              {repair.attempts.length} 次修复 · {repair.outcome}
            </span>
          </div>

          {repair.attempts.length ? (
            <div className="mt-5 grid gap-3">
              {repair.attempts.map((attempt) => (
                <div
                  className="rounded-2xl border border-cyan-100 bg-white/90 p-4"
                  key={`${attempt.attempt_index}-${attempt.repair_action}`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-sm font-semibold text-slate-950">
                      #{attempt.attempt_index} {repairActionLabels[attempt.repair_action] ?? attempt.repair_action}
                      <span className="ml-2 font-mono text-[11px] font-normal text-slate-400">
                        {attempt.repair_action}
                      </span>
                    </p>
                    <span className="text-[11px] font-medium text-cyan-900">
                      {attempt.model_call_count} 次模型调用 · {attempt.provider_call_count} 次工具调用
                    </span>
                  </div>
                  <div className="mt-3 grid gap-3 text-xs leading-5 text-slate-600 sm:grid-cols-2">
                    <p>
                      <span className="font-semibold text-slate-900">实际执行：</span>
                      {attempt.executed_nodes.map((node) => repairNodeLabels[node] ?? node).join("、") || "无"}
                    </p>
                    <p>
                      <span className="font-semibold text-slate-900">复用结果：</span>
                      {attempt.reused_nodes.map((node) => repairNodeLabels[node] ?? node).join("、") || "无"}
                    </p>
                  </div>
                  {attempt.resolved_issue_codes.length ? (
                    <div className="mt-3 flex flex-wrap gap-2">
                      {attempt.resolved_issue_codes.map((code) => (
                        <span
                          className="rounded-full bg-emerald-100 px-3 py-1.5 text-[11px] font-semibold text-emerald-900"
                          key={code}
                        >
                          {repairIssueLabels[code] ?? code}已修复
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          ) : (
            <p className="mt-4 text-sm text-slate-600">本次计划无需进入修复循环。</p>
          )}
        </article>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.7fr)_minmax(320px,.75fr)]">
        <article className="rounded-[1.75rem] border border-slate-200 bg-white p-5 shadow-sm sm:p-7">
          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="eyebrow">Itinerary</p>
              <h3 className="mt-1 text-lg font-semibold">可追溯行程时间线</h3>
            </div>
            <span className="text-xs text-slate-400">
              {revisionResult ? "v2 修改草案 · 尚未再次审核" : "草案 · 待人工确认"}
            </span>
          </div>

          <div className="mt-7 space-y-8">
            {plan.days.map((day, dayIndex) => (
              <section className="grid gap-4 sm:grid-cols-[112px_1fr]" key={day.date}>
                <div>
                  <p className="text-xs font-bold uppercase tracking-[0.18em] text-emerald-800">
                    Day {dayIndex + 1}
                  </p>
                  <p className="mt-1 text-sm font-semibold text-slate-800">{formatDate(day.date)}</p>
                </div>
                <div className="space-y-3 border-l border-slate-200 pl-5">
                  {day.departure_from_stay_at ? (
                    <p className="rounded-xl border border-dashed border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-900">
                      建议 {formatTime(day.departure_from_stay_at)} 从住宿锚点出发前往首站
                    </p>
                  ) : null}
                  {day.items.map((item, itemIndex) => (
                    <div
                      className="relative rounded-2xl bg-slate-50 px-4 py-4"
                      data-testid="itinerary-item"
                      key={item.item_id}
                    >
                      <span className="absolute -left-[1.68rem] top-6 size-3 rounded-full border-2 border-white bg-emerald-700 ring-1 ring-emerald-800/20" />
                      <div className="flex flex-wrap justify-between gap-3">
                        <div>
                          <p className="text-xs font-semibold text-emerald-800">
                            {formatTime(item.start_at)} — {formatTime(item.end_at)}
                          </p>
                          <h4 className="mt-1 text-base font-semibold text-slate-950">{item.title}</h4>
                        </div>
                        {item.source ? <SourceModeBadge mode={item.source.data_mode} /> : null}
                      </div>
                      {item.notes.length ? (
                        <p className="mt-2 text-xs leading-5 text-slate-500">{item.notes.join(" · ")}</p>
                      ) : null}
                      {item.route_from_previous ? (
                        <p className="mt-2 text-xs text-slate-500">
                          {itemIndex === 0 ? "住宿锚点至此" : "上一站至此"}：
                          {item.route_from_previous.duration_minutes} 分钟 · {item.route_from_previous.distance_meters} 米
                        </p>
                      ) : null}
                    </div>
                  ))}
                  {day.meal_recommendations.length ? (
                    <div
                      className="rounded-2xl border border-amber-200 bg-amber-50/80 p-4"
                      data-testid="meal-recommendations"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <h4 className="text-sm font-semibold text-amber-950">附近用餐建议</h4>
                        <span className="text-[10px] font-semibold text-amber-700">推荐 · 不占活动名额</span>
                      </div>
                      <div className="mt-3 grid gap-2">
                        {day.meal_recommendations.map((recommendation) => {
                          const anchor = day.items.find(
                            (item) => item.candidate_id === recommendation.anchor_candidate_id,
                          );
                          return (
                            <div
                              className="rounded-xl border border-amber-100 bg-white/85 p-3"
                              key={recommendation.recommendation_id}
                            >
                              <div className="flex flex-wrap items-start justify-between gap-2">
                                <div>
                                  <p className="text-sm font-semibold text-slate-950">
                                    {recommendation.candidate.name}
                                  </p>
                                  <p className="mt-1 text-[11px] text-slate-500">
                                    距“{anchor?.title ?? "当日景点"}”直线约 {recommendation.straight_line_distance_meters} 米
                                  </p>
                                </div>
                                <SourceModeBadge mode={recommendation.candidate.source.data_mode} />
                              </div>
                            </div>
                          );
                        })}
                      </div>
                      <p className="mt-3 text-[10px] leading-4 text-amber-800/70">
                        仅按 Provider 坐标计算附近备选；价格、营业时间、排队和可订状态尚未验证。
                      </p>
                    </div>
                  ) : (
                    <p className="rounded-xl border border-dashed border-slate-200 px-3 py-2 text-xs text-slate-400">
                      当前没有 3 公里内且来源可追溯的餐饮候选，不随机填充全城餐厅。
                    </p>
                  )}
                </div>
              </section>
            ))}
          </div>
        </article>

        <div className="space-y-5">
          <article className="rounded-[1.75rem] bg-slate-950 p-6 text-white shadow-sm">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-[11px] font-bold uppercase tracking-[0.18em] text-emerald-300">Human review</p>
                <h3 className="mt-2 text-lg font-semibold">
                  {reviewOutcome ? "审核已完成" : review ? "等待你的确认" : "审核状态"}
                </h3>
              </div>
              <span className="flex size-10 items-center justify-center rounded-full bg-amber-300 text-lg text-slate-950">!</span>
            </div>
            <p className="mt-5 text-sm leading-6 text-slate-300">
              {reviewOutcome
                ? reviewOutcome.plan_diff.summary.join(" ")
                : review?.prompt ?? "当前任务没有生成待审核请求。"}
            </p>
            {reviewOutcome ? (
              <div className="mt-5 rounded-2xl border border-emerald-300/15 bg-emerald-300/10 p-4">
                <p className="text-sm font-semibold text-emerald-200">
                  {reviewActionLabels[reviewOutcome.action]}
                </p>
                <p className="mt-1 text-[11px] text-slate-400">
                  {new Date(reviewOutcome.decided_at).toLocaleString("zh-CN")} · {reviewOutcome.reviewer_id.slice(0, 20)}…
                </p>
              </div>
            ) : review && snapshot.status === "awaiting_input" ? (
              <div className="mt-5 space-y-3">
                <div className="grid grid-cols-2 gap-2">
                  {review.allowed_actions.includes("approve_draft") ? (
                    <button
                      className="rounded-xl bg-emerald-300 px-3 py-3 text-xs font-semibold text-slate-950 transition hover:bg-emerald-200 disabled:opacity-50"
                      disabled={reviewBusy}
                      onClick={() => void onReview("approve_draft")}
                      type="button"
                    >
                      批准草案
                    </button>
                  ) : null}
                  {review.allowed_actions.includes("acknowledge_conflict") ? (
                    <button
                      className="rounded-xl bg-amber-300 px-3 py-3 text-xs font-semibold text-slate-950 transition hover:bg-amber-200 disabled:opacity-50"
                      disabled={reviewBusy}
                      onClick={() => void onReview("acknowledge_conflict")}
                      type="button"
                    >
                      确认已知冲突
                    </button>
                  ) : null}
                  <button
                    className="rounded-xl border border-white/15 px-3 py-3 text-xs font-semibold text-white transition hover:bg-white/5 disabled:opacity-50"
                    disabled={reviewBusy}
                    onClick={() => setShowRevisionForm((current) => !current)}
                    type="button"
                  >
                    局部修改
                  </button>
                  <button
                    className="rounded-xl border border-white/10 px-3 py-3 text-xs font-semibold text-slate-400 transition hover:bg-white/5 disabled:opacity-50"
                    disabled={reviewBusy}
                    onClick={() => void onReview("cancel")}
                    type="button"
                  >
                    取消规划
                  </button>
                </div>
                {showRevisionForm ? (
                  <div className="rounded-2xl border border-white/10 bg-white/5 p-3">
                    <div className="grid grid-cols-2 gap-2">
                      <label className="text-[11px] font-semibold text-slate-300">
                        目标日期
                        <select
                          aria-label="修改目标日期"
                          className="mt-2 w-full rounded-xl border border-white/10 bg-slate-900 p-2.5 text-xs text-white outline-none focus:border-emerald-300"
                          onChange={(event) => setRevisionTargetDate(event.target.value)}
                          value={selectedRevisionDate}
                        >
                          {plan.days.map((day, index) => (
                            <option key={day.date} value={day.date}>
                              第 {index + 1} 天 · {day.date.slice(5)}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="text-[11px] font-semibold text-slate-300">
                        整体延后
                        <select
                          aria-label="活动延后时间"
                          className="mt-2 w-full rounded-xl border border-white/10 bg-slate-900 p-2.5 text-xs text-white outline-none focus:border-emerald-300"
                          onChange={(event) => setRevisionShiftMinutes(Number(event.target.value))}
                          value={revisionShiftMinutes}
                        >
                          <option value={60}>60 分钟</option>
                          <option value={90}>90 分钟</option>
                          <option value={120}>120 分钟</option>
                        </select>
                      </label>
                    </div>
                    <label className="text-[11px] font-semibold text-slate-300" htmlFor="revision-comment">
                      <span className="mt-3 block">修改说明</span>
                    </label>
                    <textarea
                      className="mt-2 min-h-20 w-full resize-y rounded-xl border border-white/10 bg-slate-900 p-3 text-xs leading-5 text-white outline-none focus:border-emerald-300"
                      id="revision-comment"
                      maxLength={500}
                      onChange={(event) => setRevisionComment(event.target.value)}
                      placeholder="例如：第二天想晚一点出发。"
                      value={revisionComment}
                    />
                    <button
                      className="mt-2 w-full rounded-xl bg-white px-3 py-2.5 text-xs font-semibold text-slate-950 disabled:opacity-40"
                      disabled={reviewBusy || !revisionComment.trim()}
                      onClick={() =>
                        void onReview("request_revision", revisionComment, {
                          targetDate: selectedRevisionDate,
                          shiftMinutes: revisionShiftMinutes,
                        })
                      }
                      type="button"
                    >
                      生成局部修改草案
                    </button>
                    <p className="mt-2 text-[10px] leading-4 text-slate-500">
                      系统只调整目标日现有活动时间；其他日期、候选、费用与来源均受保护。
                    </p>
                  </div>
                ) : null}
                {reviewError ? (
                  <p className="rounded-xl bg-rose-400/10 px-3 py-2 text-[11px] leading-5 text-rose-200" role="alert">
                    {reviewError}
                  </p>
                ) : null}
              </div>
            ) : null}
            <p className="mt-3 text-[11px] leading-5 text-slate-500">
              {reviewOutcome
                ? reviewOutcome.action === "request_revision"
                  ? "v2 由同一 checkpoint 的确定性 revision node 生成，尚未再次批准，也不代表预订。"
                  : "决定已通过 review-resume API 写入同一 checkpoint；审批不代表预订或付款。"
                : "审核动作会恢复同一个 LangGraph checkpoint，不会重跑景点搜索或 Planner。"}
            </p>
          </article>

          <article className="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="eyebrow">Plan lineage</p>
                <h3 className="mt-2 font-semibold">计划版本</h3>
              </div>
              <span className="rounded-full bg-slate-950 px-3 py-1.5 text-[11px] font-bold text-white">
                v{snapshot.plan_versions.at(-1)?.version_number ?? 1}
              </span>
            </div>
            {reviewOutcome ? (
              <div className="mt-4 rounded-2xl bg-slate-50 p-4">
                <p className="text-sm font-semibold text-slate-900">
                  v{fromVersionNumber ?? latestVersion?.version_number ?? 1} → v
                  {toVersionNumber ?? latestVersion?.version_number ?? 1}
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  {reviewOutcome.plan_diff.plan_changed
                    ? `计划已修改 · ${reviewOutcome.plan_diff.changed_dates.length} 个受影响日期`
                    : "计划未修改 · 0 个受影响日期"}
                </p>
                {reviewOutcome.plan_diff.changed_dates.length ? (
                  <p className="mt-2 text-[11px] text-slate-400">
                    受影响：{reviewOutcome.plan_diff.changed_dates.join("、")}
                  </p>
                ) : null}
              </div>
            ) : (
              <p className="mt-4 text-xs leading-5 text-slate-500">
                初始 provider-grounded 草案已登记为 v1；审核决定产生前不会制造新版本。
              </p>
            )}
            <p className="mt-3 break-all font-mono text-[10px] leading-4 text-slate-400">
              {snapshot.plan_versions.at(-1)?.version_id ?? "version unavailable"}
            </p>
          </article>

          <article className="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm">
            <p className="eyebrow">Budget guard</p>
            <div className="mt-3 flex items-end justify-between gap-4">
              <div>
                <p className="text-2xl font-semibold tracking-tight">
                  {hasBudget && budget.total_limit !== null ? formatMoney(budget.total_limit) : "未设置"}
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  {materials?.budget_allocation.hard_limit ? "整趟行程硬预算" : "整趟行程规划目标"}
                </p>
              </div>
              <span className={`status-pill status-${budget.status}`}>
                {budget.status === "not_requested"
                  ? "未请求校验"
                  : budget.status === "incomplete"
                    ? "事实不完整"
                    : budget.status}
              </span>
            </div>
            {budget.status === "incomplete" ? (
              <div className="mt-5 rounded-2xl border border-amber-200 bg-amber-50 p-4">
                <p className="text-sm font-semibold text-amber-950">费用事实缺失，不等于 0 元</p>
                <p className="mt-1 text-xs leading-5 text-amber-800">
                  待补：{budget.missing_categories.map((item) => categoryLabels[item]).join("、")}
                </p>
              </div>
            ) : (
              <p className="mt-5 text-xs leading-5 text-slate-500">
                当前未提供预算时，系统不会凭空生成或宣称费用总额。
              </p>
            )}
          </article>

          <article className="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm">
            <p className="eyebrow">Weather readiness</p>
            <h3 className="mt-2 font-semibold">天气风险</h3>
            {plan.weather_risks.length ? (
              <div className="mt-4 space-y-3">
                {plan.weather_risks.map((risk) => (
                  <div className="rounded-2xl bg-sky-50 p-4 text-sm" key={risk.risk_id}>
                    <p className="font-semibold">{risk.risk_type} · {risk.severity}</p>
                    <p className="mt-1 text-xs leading-5 text-slate-600">{risk.advisory}</p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="mt-3 text-xs leading-5 text-slate-500">
                天气工具本次没有返回风险；这不等于旅行日期一定天气良好，出发前仍应复核。
              </p>
            )}
          </article>
        </div>

        <CoordinateOverview candidates={candidates} />

        <article className="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm">
          <p className="eyebrow">Source ledger</p>
          <h3 className="mt-2 text-lg font-semibold">事实来源</h3>
          <div className="mt-5 space-y-3">
            {candidates.map((candidate) => (
              <div className="rounded-2xl border border-slate-100 bg-slate-50 p-4" key={candidate.candidate_id}>
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-sm font-semibold">{candidate.name}</p>
                    <p className="mt-1 text-xs leading-5 text-slate-500">
                      {candidate.district ?? "行政区未知"} · {candidate.address ?? "地址未知"}
                    </p>
                  </div>
                  <SourceModeBadge mode={candidate.source.data_mode} />
                </div>
                <p className="mt-3 break-all text-[10px] leading-4 text-slate-400">
                  {candidate.source.provider} / {candidate.source.provider_id ?? "无 provider_id"}
                </p>
              </div>
            ))}
          </div>
          <p className="mt-4 text-[11px] leading-5 text-slate-400">
            最近一次数据检索：{candidates[0] ? new Date(candidates[0].source.retrieved_at).toLocaleString("zh-CN") : "无"}
          </p>
        </article>

        <article className="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm lg:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="eyebrow">Deterministic validation</p>
              <h3 className="mt-2 text-lg font-semibold">规则校验报告</h3>
            </div>
            <span className="text-xs text-slate-400">{validation.validator_version}</span>
          </div>
          {validation.issues.length ? (
            <div className="mt-5 grid gap-3 sm:grid-cols-2">
              {validation.issues.map((issue) => (
                <div className="rounded-2xl border border-rose-100 bg-rose-50 p-4" key={issue.issue_id}>
                  <p className="text-xs font-bold uppercase tracking-[0.12em] text-rose-700">{issue.rule_code}</p>
                  <p className="mt-2 text-sm leading-6 text-rose-950">{issue.message}</p>
                  <p className="mt-2 text-[11px] text-rose-700/70">责任节点：{issue.responsible_node}</p>
                </div>
              ))}
            </div>
          ) : (
            <div className="mt-5 rounded-2xl bg-emerald-50 p-4 text-sm leading-6 text-emerald-950">
              当前草案通过全部已执行的确定性规则。通过不等于信息完备，未接入的天气和费用事实仍保持显式未知。
            </div>
          )}
          <div className="mt-4 flex flex-wrap gap-2">
            {validation.passed_rule_codes.map((rule) => (
              <span className="rounded-full bg-slate-100 px-3 py-1.5 text-[11px] font-medium text-slate-600" key={rule}>
                ✓ {rule}
              </span>
            ))}
          </div>
        </article>
      </div>
    </section>
  );
}
