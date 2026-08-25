import { useState } from "react";

import { apiBaseUrl } from "@/lib/planning-task";
import { WeatherOutlook } from "@/components/weather-outlook";

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
  approve_draft: "行程已确认",
  acknowledge_conflict: "已保留当前方案",
  request_revision: "修改版已生成",
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

const repairIssueLabels: Record<string, string> = {
  "opening_hours.schedule_outside_verified_window": "营业时间冲突",
};

const validationIssueLabels: Record<string, string> = {
  "budget.incomplete_category_coverage": "预算采用规划估算",
  "budget.possible_overrun": "预算可能超出目标",
  "budget.deterministic_floor_exceeds_limit": "已知费用超过预算",
  "route.missing_for_grounded_item": "一段到达路线未取得",
  "route.excessive_transfer": "存在超长通勤",
  "route.insufficient_transfer_window": "活动间通勤时间不足",
  "opening_hours.evidence_missing": "开放时间尚未取得",
  "opening_hours.schedule_outside_verified_window": "安排时段不在开放时间内",
  "plan.activity_density_out_of_range": "活动数量不符合所选节奏",
  "constraint.hard_avoid_scheduled": "安排了明确要求避开的地点",
  "constraint.hard_must_visit_missing": "遗漏了明确要求必去的地点",
};

const validationIssueDescriptions: Record<string, string> = {
  "budget.incomplete_category_coverage": "预算按总额、人数和天数估算，实际费用可能不同。",
  "budget.possible_overrun": "当前安排可能超过预算目标，可以调整住宿、交通或活动。",
  "budget.deterministic_floor_exceeds_limit": "已知费用已超过预算上限，需要减少或更换部分安排。",
  "route.missing_for_grounded_item": "部分行程的通勤时间暂未取得，出发前请再次确认。",
  "route.excessive_transfer": "部分活动之间距离较远，建议更换地点或调整日期。",
  "route.insufficient_transfer_window": "两项活动之间预留的通勤时间不足。",
  "opening_hours.evidence_missing": "部分地点没有可确认的开放时间，请在出发前查询。",
  "opening_hours.schedule_outside_verified_window": "活动时间可能与开放时间冲突，建议调整时段。",
  "plan.activity_density_out_of_range": "每天的主要活动数量与所选行程节奏不一致。",
  "constraint.hard_avoid_scheduled": "行程包含了你明确不想去的地点。",
  "constraint.hard_must_visit_missing": "行程遗漏了你明确要求前往的地点。",
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

function candidateTag(candidate: CandidatePoi, prefix: string) {
  return candidate.tags.find((tag) => tag.startsWith(prefix))?.slice(prefix.length) ?? null;
}

function issueTitle(issue: ValidationIssue) {
  return validationIssueLabels[issue.rule_code] ?? issue.message;
}

function validationHeading(validation: PlanValidation) {
  if (validation.status === "passed") return "行程检查通过";
  if (validation.status === "warning") return "行程可用 · 请留意提示";
  const blockingIssues = validation.issues.filter((issue) => issue.severity === "error");
  if (blockingIssues.length && blockingIssues.every((issue) => verificationGapCodes.has(issue.rule_code))) {
    return "出发前需要确认";
  }
  return "行程要求待处理";
}

function friendlyReason(reason: string) {
  return reason
    .replaceAll("住宿锚点", "住宿地点")
    .replaceAll("路线矩阵", "每日路线")
    .replaceAll("Provider observations", "可选地点")
    .replaceAll("Provider", "数据来源");
}

function sourceLabel(dataMode: string) {
  return dataMode === "live" ? "高德地图" : "示例数据";
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
          <p className="eyebrow">行程地图</p>
          <h3 className="mt-1 text-lg font-semibold tracking-tight">住宿与景点位置</h3>
        </div>
        <span className="rounded-full bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-800">
          {orderedCandidates.length
            ? `H 住宿 · 1–${orderedCandidates.length} 游览顺序`
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
            连线表示游览顺序，实际道路和通勤时间以行程卡片为准。
          </p>
        </div>
      ) : (
        <div className="grid min-h-72 place-items-center bg-slate-100 p-8 text-center">
          <div className="max-w-lg">
            <p className="text-sm font-semibold text-slate-800">
              {dataMode === "fixture" ? "示例体验暂不显示地图" : "地图暂时无法加载"}
            </p>
            <p className="mt-2 text-xs leading-5 text-slate-500">
              {dataMode === "fixture"
                ? "选择“实时规划”后，可查看带底图的住宿和景点位置。"
                : "行程内容不受影响，可以稍后刷新重试。"}
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
  const [revisionMode, setRevisionMode] = useState<"shift_day_later" | "replace_activity">(
    "shift_day_later",
  );
  const [replacementItemId, setReplacementItemId] = useState("");
  const [replacementCandidateId, setReplacementCandidateId] = useState("");
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
  const materials = revisionResult?.revised_materials ?? state.materials;
  const candidates =
    materials?.shortlist.poi_candidates ?? verticalSlice?.upstream.candidates ?? [];
  const specialists = state.specialists;
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
  const budgetAllocations = materials?.budget_allocation.allocations ?? [];
  const reviewSummary = blockingIssues.length
    ? `当前方案有 ${blockingIssues.length} 项重要问题尚未解决：${blockingIssues
        .map(issueTitle)
        .join("、")}。${warningIssues.length ? `另有 ${warningIssues.length} 项估算提醒。` : ""}`
    : "这份行程已准备好，请确认或调整。";
  const displayCity = plan.destination_city.replace(/市$/, "");
  const materialIssueLabels: Record<string, string> = {
    specialist_incomplete: "部分旅行信息暂未取得",
    route_matrix_incomplete: "部分到达路线尚未确认",
    budget_not_allocated: "预算分配暂不完整",
    stay_anchor_missing: "住宿地点尚未确认",
    activity_coverage_insufficient: "主要活动数量低于所选节奏目标",
    excessive_transfer: "存在超长通勤候选",
  };
  const selectedRevisionDate = revisionTargetDate || plan.days.at(-1)?.date || plan.start_date;
  const selectedRevisionDay = plan.days.find((day) => day.date === selectedRevisionDate);
  const replacementTargets =
    selectedRevisionDay?.items.filter(
      (item) => item.kind === "attraction" && item.candidate_id !== null,
    ) ?? [];
  const selectedReplacementItemId = replacementItemId || replacementTargets[0]?.item_id || "";
  const exploreObservations =
    specialists?.branches.find((branch) => branch.specialist === "explore")?.explore_result
      ?.observations ?? [];
  const scheduledCandidateIds = new Set(
    plan.days.flatMap((day) =>
      day.items.flatMap((item) => (item.candidate_id ? [item.candidate_id] : [])),
    ),
  );
  const eligibleReplacementCandidates = exploreObservations
    .map((item) => item.candidate)
    .filter(
      (candidate) =>
        !scheduledCandidateIds.has(candidate.candidate_id) &&
        !candidate.categories.includes("餐饮服务") &&
        candidate.city === plan.destination_city,
    );
  const selectedReplacementCandidateId =
    replacementCandidateId || eligibleReplacementCandidates[0]?.candidate_id || "";
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
          <p className="eyebrow">行程方案</p>
          <h2 className="mt-2 text-3xl font-semibold tracking-[-0.035em] text-slate-950">
            {displayCity} · {plan.days.length} 天行程
          </h2>
          <p className="mt-2 text-sm text-slate-500">
            更新于 {new Date(snapshot.updated_at).toLocaleString("zh-CN")}
          </p>
        </div>
        <div className="flex items-center gap-2">
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
          <p className="eyebrow text-amber-700">部分信息需要确认</p>
          <h3 className="mt-2 text-lg font-semibold">行程已经生成，请留意以下提示</h3>
          <p className="mt-2 text-sm leading-6 text-amber-900/80">
            路线、开放时间或费用信息可能不完整，出发前请根据提示再次确认。
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
                <p className="eyebrow">住宿建议</p>
                <h3 className="mt-2 text-xl font-semibold tracking-tight">住宿推荐</h3>
              </div>
              <span className="rounded-full bg-emerald-950 px-3 py-1.5 text-[11px] font-semibold text-white">
                适合作为行程起点
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
                </div>
                <div className="mt-4 rounded-2xl bg-emerald-50 p-4">
                  <p className="text-[11px] font-bold tracking-[0.1em] text-emerald-800">
                    推荐理由
                  </p>
                  <p className="mt-2 text-sm leading-6 text-emerald-950">
                    {friendlyReason(primaryStayRecommendation?.proposal.reason ??
                      "位置便于前往每天的首个景点，可以减少早晨通勤时间。")}
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
                    价格和空房情况以预订平台为准
                  </span>
                </div>
              </div>
            ) : (
              <p className="mt-5 rounded-2xl bg-slate-50 p-4 text-sm text-slate-500">
                暂时没有找到合适的住宿，可以先查看景点行程。
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
                      {friendlyReason(recommendation.proposal.reason)}
                    </p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="mt-3 text-xs leading-5 text-slate-500">暂时没有其他合适的住宿推荐。</p>
            )}
            <p className="mt-4 text-[10px] leading-4 text-slate-400">
              价格和空房情况请在预订平台查询。
            </p>
          </div>
        </div>
      </article>

      {repair?.attempts.length ? (
        <article
          className="mb-5 rounded-[1.75rem] border border-cyan-200 bg-cyan-50/75 p-5 shadow-sm sm:p-6"
          data-testid="product-repair-summary"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="eyebrow">行程已自动调整</p>
              <h3 className="mt-2 text-lg font-semibold text-slate-950">已处理不合理的时间安排</h3>
              <p className="mt-2 max-w-3xl text-xs leading-5 text-slate-600">
                只调整了受影响的日期，其他行程保持不变。
              </p>
            </div>
            <span className="rounded-full border border-cyan-200 bg-white px-3 py-1.5 text-xs font-semibold text-cyan-900">
              已调整 {repair.attempts.length} 次
            </span>
          </div>

          <div className="mt-4 flex flex-wrap gap-2">
            {[...new Set(repair.attempts.flatMap((attempt) => attempt.resolved_issue_codes))].map((code) => (
              <span
                className="rounded-full bg-white px-3 py-1.5 text-[11px] font-semibold text-cyan-900"
                key={code}
              >
                {repairIssueLabels[code] ?? "行程安排问题"}已解决
              </span>
            ))}
          </div>
        </article>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.7fr)_minmax(320px,.75fr)]">
        <article className="rounded-[1.75rem] border border-slate-200 bg-white p-5 shadow-sm sm:p-7">
          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="eyebrow">每日安排</p>
              <h3 className="mt-1 text-lg font-semibold">行程时间线</h3>
            </div>
            <span className="text-xs text-slate-400">
              {revisionResult ? "修改版 · 等待再次确认" : "等待你的确认"}
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
                      建议 {formatTime(day.departure_from_stay_at)} 从住宿地点出发前往首站
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
                                    : "室内外信息暂缺"}
                            </span>
                          </div>
                        </div>
                      ) : null}
                      {recommendation ? (
                        <div className="mt-3 rounded-xl border border-emerald-100 bg-emerald-50 p-3" data-testid="activity-reason">
                          <p className="text-[10px] font-bold tracking-[0.1em] text-emerald-700">
                            推荐理由
                          </p>
                          <p className="mt-1 text-xs leading-5 text-emerald-950">
                            {friendlyReason(recommendation.proposal.reason)}
                          </p>
                        </div>
                      ) : null}
                      {item.route_from_previous ? (
                        <p className="mt-2 text-xs text-slate-500">
                          {itemIndex === 0 ? "从住宿地点出发" : "从上一站出发"}：
                          {item.route_from_previous.duration_minutes} 分钟 · {item.route_from_previous.distance_meters} 米
                        </p>
                      ) : null}
                      </div>
                    );
                  })}
                  {(day.meal_recommendations ?? []).length ? (
                    <div
                      className="rounded-2xl border border-amber-200 bg-amber-50/80 p-4"
                      data-testid="meal-recommendations"
                    >
                      <h4 className="text-sm font-semibold text-amber-950">附近用餐建议</h4>
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
                              </div>
                            </div>
                          );
                        })}
                      </div>
                      <p className="mt-3 text-[10px] leading-4 text-amber-800/70">
                        按当天景点位置推荐，营业时间和排队情况请到店前确认。
                      </p>
                    </div>
                  ) : (
                    <p className="rounded-xl border border-dashed border-slate-200 px-3 py-2 text-xs text-slate-400">
                      附近暂时没有合适的用餐推荐。
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
                <p className="text-[11px] font-bold tracking-[0.14em] text-emerald-300">确认与调整</p>
                <h3 className="mt-2 text-lg font-semibold">
                  {reviewOutcome
                    ? "本次选择已保存"
                    : review
                      ? blockingIssues.length
                        ? validationHeading(validation)
                        : "等待你的确认"
                      : "行程状态"}
                </h3>
              </div>
              <span className="flex size-10 items-center justify-center rounded-full bg-amber-300 text-lg text-slate-950">!</span>
            </div>
            <p className="mt-5 text-sm leading-6 text-slate-300">
              {reviewOutcome
                ? reviewOutcome.action === "request_revision"
                  ? "修改版已经生成，请再次确认新的行程安排。"
                  : reviewOutcome.action === "approve_draft"
                    ? "行程已经确认，开放时间、票价和房态请在出发前再次核对。"
                    : reviewOutcome.action === "acknowledge_conflict"
                      ? "已保留当前方案，请在出发前确认标记的信息。"
                      : "本次规划已取消。"
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
                      确认行程
                    </button>
                  ) : null}
                  {review.allowed_actions.includes("acknowledge_conflict") ? (
                    <button
                      className="rounded-xl bg-amber-300 px-3 py-3 text-xs font-semibold text-slate-950 transition hover:bg-amber-200 disabled:opacity-50"
                      disabled={reviewBusy}
                      onClick={() => void onReview("acknowledge_conflict")}
                      type="button"
                    >
                      保留当前方案
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
                    <label className="text-[11px] font-semibold text-slate-300">
                      修改方式
                      <select
                        aria-label="修改方式"
                        className="mt-2 w-full rounded-xl border border-white/10 bg-slate-900 p-2.5 text-xs text-white outline-none focus:border-emerald-300"
                        onChange={(event) =>
                          setRevisionMode(
                            event.target.value as "shift_day_later" | "replace_activity",
                          )
                        }
                        value={revisionMode}
                      >
                        <option value="shift_day_later">整日延后</option>
                        <option value="replace_activity">替换一个活动</option>
                      </select>
                    </label>
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
                      {revisionMode === "shift_day_later" ? (
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
                      ) : null}
                    </div>
                    {revisionMode === "replace_activity" ? (
                      eligibleReplacementCandidates.length ? (
                        <div className="mt-3 grid grid-cols-2 gap-2">
                          <label className="text-[11px] font-semibold text-slate-300">
                            被替换活动
                            <select
                              aria-label="被替换活动"
                              className="mt-2 w-full rounded-xl border border-white/10 bg-slate-900 p-2.5 text-xs text-white outline-none focus:border-emerald-300"
                              onChange={(event) => setReplacementItemId(event.target.value)}
                              value={selectedReplacementItemId}
                            >
                              {replacementTargets.map((item) => (
                                <option key={item.item_id} value={item.item_id}>
                                  {item.title}
                                </option>
                              ))}
                            </select>
                          </label>
                          <label className="text-[11px] font-semibold text-slate-300">
                            可选地点
                            <select
                              aria-label="可选地点"
                              className="mt-2 w-full rounded-xl border border-white/10 bg-slate-900 p-2.5 text-xs text-white outline-none focus:border-emerald-300"
                              onChange={(event) => setReplacementCandidateId(event.target.value)}
                              value={selectedReplacementCandidateId}
                            >
                              {eligibleReplacementCandidates.map((candidate) => (
                                <option key={candidate.candidate_id} value={candidate.candidate_id}>
                                  {candidate.name} · {candidate.district ?? "区域待确认"}
                                </option>
                              ))}
                            </select>
                          </label>
                        </div>
                      ) : (
                        <p className="mt-3 rounded-xl bg-amber-300/10 px-3 py-2 text-[11px] leading-5 text-amber-100">
                          暂时没有合适的替换地点，可以保留当前安排或重新生成行程。
                        </p>
                      )
                    ) : null}
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
                      disabled={
                        reviewBusy ||
                        !revisionComment.trim() ||
                        (revisionMode === "replace_activity" &&
                          (!selectedReplacementItemId || !selectedReplacementCandidateId))
                      }
                      onClick={() =>
                        void onReview(
                          "request_revision",
                          revisionComment,
                          revisionMode === "replace_activity"
                            ? {
                                kind: "replace_activity",
                                targetDate: selectedRevisionDate,
                                replacedItemId: selectedReplacementItemId,
                                replacementCandidateId: selectedReplacementCandidateId,
                              }
                            : {
                                kind: "shift_day_later",
                                targetDate: selectedRevisionDate,
                                shiftMinutes: revisionShiftMinutes,
                              },
                        )
                      }
                      type="button"
                    >
                      生成修改版
                    </button>
                    <p className="mt-2 text-[10px] leading-4 text-slate-500">
                      {revisionMode === "replace_activity"
                        ? "替换地点后，只会重新安排所选日期，其他日期保持不变。"
                        : "只会调整所选日期的活动时间，其他日期保持不变。"}
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
              确认行程只保存本次安排，不会产生预订或付款。
            </p>
          </article>

          <article className="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="eyebrow">修改记录</p>
                <h3 className="mt-2 font-semibold">行程版本</h3>
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
                当前为初版，修改后可以在这里查看受影响的日期。
              </p>
            )}
          </article>

          <article className="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm" data-testid="budget-estimate">
            <p className="eyebrow">预算参考</p>
            <div className="mt-3 flex items-end justify-between gap-4">
              <div>
                <p className="text-sm font-semibold text-slate-800">预算估算</p>
                <p className="text-2xl font-semibold tracking-tight">
                  {hasBudget && (materials?.budget_allocation.total_limit ?? budget.total_limit) !== null
                    ? formatMoney(materials?.budget_allocation.total_limit ?? budget.total_limit ?? 0)
                    : "未设置"}
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  {materials?.budget_allocation.hard_limit ? "整趟行程预算上限" : "整趟行程建议分配"}
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
                当前没有可用的预算目标。
              </p>
            )}
            <p className="mt-4 text-[10px] leading-5 text-slate-400">
              根据总预算、人数和天数估算，实际费用以出行时为准。
              {budget.missing_categories.length
                ? ` 尚未取得实时价格：${budget.missing_categories.map((item) => categoryLabels[item]).join("、")}。`
                : ""}
            </p>
          </article>

          <WeatherOutlook candidates={candidates} plan={plan} />
        </div>

        <AmapPlanOverview
          candidates={candidates}
          dataMode={snapshot.data_mode}
          plan={plan}
          stay={stay}
        />

        <article className="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm">
          <p className="eyebrow">地点信息</p>
          <h3 className="mt-2 text-lg font-semibold">信息来源</h3>
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
                  <span className="text-[10px] font-medium text-slate-400">
                    {sourceLabel(candidate.source.data_mode)}
                  </span>
                </div>
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
              <p className="eyebrow">出发前检查</p>
              <h3 className="mt-2 text-lg font-semibold">行程检查</h3>
            </div>
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
                    {validationIssueDescriptions[issue.rule_code] ?? issue.message}
                  </p>
                </div>
              ))}
            </div>
          ) : (
            <div className="mt-5 rounded-2xl bg-emerald-50 p-4 text-sm leading-6 text-emerald-950">
              未发现时间、通勤或预算方面的明显冲突。开放时间、票价和房态可能变化，出发前请再次确认。
            </div>
          )}
        </article>
      </div>
    </section>
  );
}
