import { useState } from "react";

import { apiBaseUrl } from "@/lib/planning-task";

import type {
  BudgetCategory,
  CandidatePoi,
  CandidateStay,
  HumanReviewAction,
  PlanRevisionSelection,
  PlanValidation,
  PlanningTaskSnapshot,
  TripPlan,
  ValidationIssue,
} from "@/lib/planning-task";

const reviewActionLabels: Record<HumanReviewAction, string> = {
  approve_draft: "已批准草案",
  acknowledge_conflict: "已保留待验证草案",
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

const validationIssueLabels: Record<string, string> = {
  "budget.incomplete_category_coverage": "预算采用规划估算",
  "budget.possible_overrun": "预算可能超出目标",
  "budget.deterministic_floor_exceeds_limit": "确定费用已超过硬预算",
  "route.missing_for_grounded_item": "一段到达路线未取得",
  "route.excessive_transfer": "存在超长通勤",
  "route.insufficient_transfer_window": "活动间通勤时间不足",
  "opening_hours.evidence_missing": "开放时间尚未取得",
  "opening_hours.schedule_outside_verified_window": "安排时段不在开放时间内",
  "plan.activity_density_out_of_range": "活动数量不符合所选节奏",
  "constraint.hard_avoid_scheduled": "安排了明确要求避开的地点",
  "constraint.hard_must_visit_missing": "遗漏了明确要求必去的地点",
};

const verificationGapCodes = new Set([
  "route.missing_for_grounded_item",
  "opening_hours.evidence_missing",
]);

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

function candidateTag(candidate: CandidatePoi, prefix: string) {
  return candidate.tags.find((tag) => tag.startsWith(prefix))?.slice(prefix.length) ?? null;
}

function issueTitle(issue: ValidationIssue) {
  return validationIssueLabels[issue.rule_code] ?? issue.message;
}

function validationHeading(validation: PlanValidation) {
  if (validation.status === "passed") return "确定性校验通过";
  if (validation.status === "warning") return "方案可用 · 有估算提醒";
  const blockingIssues = validation.issues.filter((issue) => issue.severity === "error");
  if (blockingIssues.length && blockingIssues.every((issue) => verificationGapCodes.has(issue.rule_code))) {
    return "关键事实待确认";
  }
  return "约束问题待处理";
}

function evidenceText(
  issue: ValidationIssue,
  itemTitleById: Map<string, string>,
) {
  return issue.evidence.map((evidence) => {
    const values = evidence.observed_value.split(",");
    const resolvedValues = values.map((value) => {
      const trimmed = value.trim();
      if (itemTitleById.has(trimmed)) return itemTitleById.get(trimmed);
      if (trimmed in categoryLabels) return categoryLabels[trimmed as BudgetCategory];
      return trimmed;
    });
    return `${evidence.description}：${resolvedValues.join("、")}`;
  });
}

function AmapPlanOverview({
  plan,
  candidates,
  stay,
  dataMode,
}: {
  plan: TripPlan;
  candidates: CandidatePoi[];
  stay: CandidateStay | null | undefined;
  dataMode: "live" | "fixture";
}) {
  const [failedMapUrl, setFailedMapUrl] = useState<string | null>(null);
  const candidateById = new Map(candidates.map((candidate) => [candidate.candidate_id, candidate]));
  const seen = new Set<string>();
  const orderedCandidates = plan.days
    .flatMap((day) => day.items)
    .flatMap((item) => {
      if (!item.candidate_id || seen.has(item.candidate_id)) return [];
      const candidate = candidateById.get(item.candidate_id);
      if (!candidate) return [];
      seen.add(item.candidate_id);
      return [candidate];
    })
    .slice(0, 9);
  const params = new URLSearchParams();
  for (const candidate of orderedCandidates) {
    params.append("poi", `${candidate.location.longitude},${candidate.location.latitude}`);
  }
  if (stay) {
    params.set("stay", `${stay.location.longitude},${stay.location.latitude}`);
  }
  const mapUrl = `${apiBaseUrl}/api/maps/static-plan?${params.toString()}`;
  const mapFailed = failedMapUrl === mapUrl;

  return (
    <article className="overflow-hidden rounded-[1.75rem] border border-slate-200 bg-white shadow-sm lg:col-span-2">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-6 py-5">
        <div>
          <p className="eyebrow">AMap spatial context</p>
          <h3 className="mt-1 text-lg font-semibold tracking-tight">高德地图 · 行程空间分布</h3>
        </div>
        <span className="rounded-full bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-800">
          {orderedCandidates.length
            ? `H 住宿 · 1–${orderedCandidates.length} 行程顺序`
            : "暂无地图点位"}
        </span>
      </div>
      {dataMode === "live" && orderedCandidates.length && !mapFailed ? (
        <div className="relative bg-slate-100">
          {/* The backend proxies AMap so the service key never reaches the browser. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            alt={`${plan.destination_city}行程高德地图，包含住宿和活动位置`}
            className="block min-h-72 w-full object-cover sm:min-h-96"
            data-testid="amap-static-map"
            onError={() => setFailedMapUrl(mapUrl)}
            src={mapUrl}
          />
          <p className="absolute bottom-4 left-4 max-w-xl rounded-xl bg-slate-950/85 px-3 py-2 text-[11px] leading-5 text-white/85 backdrop-blur">
            底图与标注由高德静态地图返回；绿色连接线只表示游览顺序，实际道路与时长以每段路线卡片为准。
          </p>
        </div>
      ) : (
        <div className="grid min-h-72 place-items-center bg-slate-100 p-8 text-center">
          <div className="max-w-lg">
            <p className="text-sm font-semibold text-slate-800">
              {dataMode === "fixture" ? "Fixture 模式不调用真实地图服务" : "高德地图底图暂时不可用"}
            </p>
            <p className="mt-2 text-xs leading-5 text-slate-500">
              {dataMode === "fixture"
                ? "切换到实时 Provider 后，页面会通过服务端代理加载高德底图，Key 不会发送到浏览器。"
                : "行程坐标和路线事实仍保留；请稍后刷新地图，不会用坐标网格冒充真实底图。"}
            </p>
          </div>
        </div>
      )}
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
  const exploreRecommendations =
    specialists?.branches.find((branch) => branch.specialist === "explore")?.explore_result
      ?.recommendations ?? [];
  const stayRecommendations =
    specialists?.branches.find((branch) => branch.specialist === "stay")?.stay_result
      ?.recommendations ?? [];
  const exploreRecommendationById = new Map(
    exploreRecommendations.map((recommendation) => [
      recommendation.candidate.candidate_id,
      recommendation,
    ]),
  );
  const candidateById = new Map(candidates.map((candidate) => [candidate.candidate_id, candidate]));
  const primaryStayRecommendation = stay
    ? stayRecommendations.find(
        (recommendation) => recommendation.candidate.candidate_id === stay.candidate_id,
      )
    : undefined;
  const alternativeStays = stayRecommendations
    .filter((recommendation) => recommendation.candidate.candidate_id !== stay?.candidate_id)
    .slice(0, 2);
  const blockingIssues = validation.issues.filter((issue) => issue.severity === "error");
  const warningIssues = validation.issues.filter((issue) => issue.severity === "warning");
  const itemTitleById = new Map(
    plan.days.flatMap((day) => day.items.map((item) => [item.item_id, item.title] as const)),
  );
  const budgetAllocations = materials?.budget_allocation.allocations ?? [];
  const reviewSummary = blockingIssues.length
    ? `当前草案有 ${blockingIssues.length} 项关键问题尚未解决：${blockingIssues
        .map(issueTitle)
        .join("、")}。${warningIssues.length ? `另有 ${warningIssues.length} 项估算提醒。` : ""}`
    : review?.prompt ?? "当前任务没有生成待审核请求。";
  const displayCity = plan.destination_city.replace(/市$/, "");
  const materialIssueLabels: Record<string, string> = {
    specialist_incomplete: "部分 Agent 数据未完整返回",
    route_matrix_incomplete: "部分到达路线尚未验证",
    budget_not_allocated: "预算目标尚未完整分配",
    stay_anchor_missing: "住宿锚点尚未确认",
    activity_coverage_insufficient: "主要活动数量低于所选节奏目标",
    excessive_transfer: "存在超长通勤候选",
  };
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
            {validationHeading(validation)}
          </span>
        </div>
      </div>

      {materials?.status === "partial" ? (
        <article
          className="mb-5 rounded-[1.75rem] border border-amber-200 bg-amber-50 p-5 text-amber-950 shadow-sm sm:p-6"
          data-testid="degraded-draft-notice"
        >
          <p className="eyebrow text-amber-700">Usable draft · facts incomplete</p>
          <h3 className="mt-2 text-lg font-semibold">已先生成可编辑方案，以下事实仍需确认</h3>
          <p className="mt-2 text-sm leading-6 text-amber-900/80">
            系统没有用模型补写缺失事实；草案会继续经过确定性校验，并在需要时交给你审核。
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            {materials.issues.map((issue) => (
              <span
                className="rounded-full border border-amber-200 bg-white px-3 py-1.5 text-xs font-semibold"
                key={issue}
              >
                {materialIssueLabels[issue] ?? issue}
              </span>
            ))}
          </div>
        </article>
      ) : null}

      <article
        className="mb-5 overflow-hidden rounded-[1.75rem] border border-emerald-900/10 bg-white shadow-sm"
        data-testid="stay-recommendation"
      >
        <div className="grid lg:grid-cols-[minmax(0,1.35fr)_minmax(320px,.65fr)]">
          <div className="p-5 sm:p-7">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="eyebrow">Recommended stay</p>
                <h3 className="mt-2 text-xl font-semibold tracking-tight">住宿推荐</h3>
              </div>
              <span className="rounded-full bg-emerald-950 px-3 py-1.5 text-[11px] font-semibold text-white">
                行程住宿锚点
              </span>
            </div>
            {stay ? (
              <div className="mt-5">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div>
                    <p className="text-2xl font-semibold tracking-tight text-slate-950">{stay.name}</p>
                    <p className="mt-2 text-sm text-slate-500">
                      {stay.area_name} · {stay.address ?? "详细地址暂缺"}
                    </p>
                  </div>
                  <SourceModeBadge mode={stay.source.data_mode} />
                </div>
                <div className="mt-4 rounded-2xl bg-emerald-50 p-4">
                  <p className="text-[11px] font-bold uppercase tracking-[0.14em] text-emerald-800">
                    Stay Agent 推荐理由
                  </p>
                  <p className="mt-2 text-sm leading-6 text-emerald-950">
                    {primaryStayRecommendation?.proposal.reason ??
                      "作为当前路线计算的住宿锚点，便于评估每天前往首站的交通时间。"}
                  </p>
                </div>
                <div className="mt-4 flex flex-wrap gap-2">
                  {[...new Set(stay.tags.map((tag) => tag.replace(/^category:/, "")))]
                    .filter((tag) => tag && tag !== "住宿服务")
                    .slice(0, 4)
                    .map((tag) => (
                      <span className="rounded-full bg-slate-100 px-3 py-1.5 text-[11px] text-slate-600" key={tag}>
                        {tag}
                      </span>
                    ))}
                  <span className="rounded-full border border-dashed border-amber-300 px-3 py-1.5 text-[11px] text-amber-800">
                    房价与房态待验证
                  </span>
                </div>
              </div>
            ) : (
              <p className="mt-5 rounded-2xl bg-slate-50 p-4 text-sm text-slate-500">
                当前没有可追溯的酒店候选，行程仍可作为景点草案查看。
              </p>
            )}
          </div>
          <div className="border-t border-slate-200 bg-slate-50 p-5 sm:p-7 lg:border-l lg:border-t-0">
            <p className="text-xs font-semibold text-slate-800">其他住宿候选</p>
            {alternativeStays.length ? (
              <div className="mt-3 space-y-3">
                {alternativeStays.map((recommendation) => (
                  <div className="rounded-2xl border border-slate-200 bg-white p-4" key={recommendation.candidate.candidate_id}>
                    <p className="text-sm font-semibold text-slate-950">{recommendation.candidate.name}</p>
                    <p className="mt-1 line-clamp-2 text-[11px] leading-5 text-slate-500">
                      {recommendation.proposal.reason}
                    </p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="mt-3 text-xs leading-5 text-slate-500">本次 Provider 没有返回其他可靠住宿候选。</p>
            )}
            <p className="mt-4 text-[10px] leading-4 text-slate-400">
              推荐只用于规划路线；未接入酒店价格、房态或预订交易。
            </p>
          </div>
        </div>
      </article>

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
            景点、住宿、天气与路线结果均保留独立来源；上方住宿卡片展示 Stay Agent 的选择理由与能力边界。
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
                  {day.items.map((item, itemIndex) => {
                    const candidate = item.candidate_id ? candidateById.get(item.candidate_id) : undefined;
                    const recommendation = item.candidate_id
                      ? exploreRecommendationById.get(item.candidate_id)
                      : undefined;
                    const rating = candidate ? candidateTag(candidate, "rating:") : null;
                    const level = candidate ? candidateTag(candidate, "level:") : null;
                    return (
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
                      {candidate ? (
                        <div className="mt-3" data-testid="activity-description">
                          <p className="text-xs leading-5 text-slate-600">
                            {candidate.categories.slice(0, 3).join("、") || "景点候选"}
                            {candidate.address ? `；位于${candidate.address}` : "；详细地址暂缺"}。
                          </p>
                          <div className="mt-2 flex flex-wrap gap-2">
                            {level ? (
                              <span className="rounded-full bg-emerald-100 px-2.5 py-1 text-[10px] font-semibold text-emerald-800">
                                {level} 级景区
                              </span>
                            ) : null}
                            {rating ? (
                              <span className="rounded-full bg-amber-100 px-2.5 py-1 text-[10px] font-semibold text-amber-800">
                                高德评分 {rating}
                              </span>
                            ) : null}
                            <span className="rounded-full bg-white px-2.5 py-1 text-[10px] text-slate-500">
                              {candidate.environment === "indoor"
                                ? "室内"
                                : candidate.environment === "outdoor"
                                  ? "户外"
                                  : candidate.environment === "mixed"
                                    ? "室内外混合"
                                    : "环境类型未知"}
                            </span>
                          </div>
                        </div>
                      ) : null}
                      {recommendation ? (
                        <div className="mt-3 rounded-xl border border-emerald-100 bg-emerald-50 p-3" data-testid="activity-reason">
                          <p className="text-[10px] font-bold uppercase tracking-[0.12em] text-emerald-700">
                            Explore Agent 推荐理由
                          </p>
                          <p className="mt-1 text-xs leading-5 text-emerald-950">
                            {recommendation.proposal.reason}
                          </p>
                        </div>
                      ) : null}
                      {item.route_from_previous ? (
                        <p className="mt-2 text-xs text-slate-500">
                          {itemIndex === 0 ? "住宿锚点至此" : "上一站至此"}：
                          {item.route_from_previous.duration_minutes} 分钟 · {item.route_from_previous.distance_meters} 米
                        </p>
                      ) : null}
                      {item.notes.length ? (
                        <details className="mt-3 text-[11px] text-slate-400">
                          <summary className="cursor-pointer">数据与验证边界</summary>
                          <p className="mt-2 leading-5">{item.notes.join(" · ")}</p>
                        </details>
                      ) : null}
                      </div>
                    );
                  })}
                  {(day.meal_recommendations ?? []).length ? (
                    <div
                      className="rounded-2xl border border-amber-200 bg-amber-50/80 p-4"
                      data-testid="meal-recommendations"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <h4 className="text-sm font-semibold text-amber-950">附近用餐建议</h4>
                        <span className="text-[10px] font-semibold text-amber-700">推荐 · 不占活动名额</span>
                      </div>
                      <div className="mt-3 grid gap-2">
                        {(day.meal_recommendations ?? []).map((recommendation) => {
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
                  {reviewOutcome
                    ? "审核已完成"
                    : review
                      ? blockingIssues.length
                        ? validationHeading(validation)
                        : "等待你的确认"
                      : "审核状态"}
                </h3>
              </div>
              <span className="flex size-10 items-center justify-center rounded-full bg-amber-300 text-lg text-slate-950">!</span>
            </div>
            <p className="mt-5 text-sm leading-6 text-slate-300">
              {reviewOutcome
                ? reviewOutcome.plan_diff.summary.join(" ")
                : reviewSummary}
            </p>
            {!reviewOutcome && review && validation.issues.length ? (
              <div className="mt-4 space-y-2" data-testid="review-issue-summary">
                {validation.issues.map((issue) => (
                  <div
                    className={
                      issue.severity === "error"
                        ? "rounded-xl border border-rose-300/20 bg-rose-300/10 p-3"
                        : "rounded-xl border border-amber-300/20 bg-amber-300/10 p-3"
                    }
                    key={issue.issue_id}
                  >
                    <p className="text-xs font-semibold text-white">{issueTitle(issue)}</p>
                    <p className="mt-1 text-[11px] leading-5 text-slate-300">{issue.message}</p>
                  </div>
                ))}
              </div>
            ) : null}
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
                      保留待验证草案
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

          <article className="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm" data-testid="budget-estimate">
            <p className="eyebrow">Planning estimate</p>
            <div className="mt-3 flex items-end justify-between gap-4">
              <div>
                <p className="text-sm font-semibold text-slate-800">预算估算</p>
                <p className="text-2xl font-semibold tracking-tight">
                  {hasBudget && (materials?.budget_allocation.total_limit ?? budget.total_limit) !== null
                    ? formatMoney(materials?.budget_allocation.total_limit ?? budget.total_limit ?? 0)
                    : "未设置"}
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  {materials?.budget_allocation.hard_limit ? "整趟行程硬预算上限" : "整趟行程建议分配"}
                </p>
              </div>
              <span className="status-pill status-warning">
                {budgetAllocations.length ? "规划估算" : "待补估算"}
              </span>
            </div>
            {budgetAllocations.length ? (
              <div className="mt-5 grid grid-cols-2 gap-2">
                {budgetAllocations.map((allocation) => (
                  <div className="rounded-2xl bg-slate-50 p-3" key={allocation.category}>
                    <p className="text-[11px] text-slate-500">{categoryLabels[allocation.category]}</p>
                    <p className="mt-1 text-sm font-semibold text-slate-950">
                      {formatMoney(allocation.target_amount)}
                    </p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="mt-5 text-xs leading-5 text-slate-500">
                当前没有可用预算目标，系统不会把缺失价格当成 0 元。
              </p>
            )}
            <p className="mt-4 text-[10px] leading-5 text-slate-400">
              这是根据总预算、人数、天数和类别权重生成的规划估算，不代表实时票价、餐费或交通结算金额。
              {budget.missing_categories.length
                ? ` 尚未取得实时价格：${budget.missing_categories.map((item) => categoryLabels[item]).join("、")}。`
                : ""}
            </p>
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

        <AmapPlanOverview
          candidates={candidates}
          dataMode={snapshot.data_mode}
          plan={plan}
          stay={stay}
        />

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
                <div
                  className={
                    issue.severity === "error"
                      ? "rounded-2xl border border-rose-100 bg-rose-50 p-4"
                      : "rounded-2xl border border-amber-100 bg-amber-50 p-4"
                  }
                  key={issue.issue_id}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className={issue.severity === "error" ? "text-sm font-semibold text-rose-950" : "text-sm font-semibold text-amber-950"}>
                      {issueTitle(issue)}
                    </p>
                    <span className={issue.severity === "error" ? "text-[10px] font-bold text-rose-700" : "text-[10px] font-bold text-amber-700"}>
                      {issue.severity === "error" ? "阻止最终确认" : "信息提醒"}
                    </span>
                  </div>
                  <p className={issue.severity === "error" ? "mt-2 text-xs leading-5 text-rose-900" : "mt-2 text-xs leading-5 text-amber-900"}>
                    {issue.message}
                  </p>
                  {evidenceText(issue, itemTitleById).map((evidence) => (
                    <p className="mt-2 rounded-xl bg-white/70 px-3 py-2 text-[11px] leading-5 text-slate-600" key={evidence}>
                      {evidence}
                    </p>
                  ))}
                  <p className="mt-2 font-mono text-[10px] text-slate-400">
                    {issue.rule_code} · 责任节点 {issue.responsible_node}
                  </p>
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
