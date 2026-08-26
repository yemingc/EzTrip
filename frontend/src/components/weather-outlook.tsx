import { useState } from "react";

import type {
  CandidatePoi,
  PlanRevisionSelection,
  TripPlan,
  WeatherIndoorRecovery,
  WeatherRisk,
} from "@/lib/planning-task";

const riskLabels: Record<string, string> = {
  rain: "降雨",
  heat: "高温",
  cold: "低温",
  wind: "大风",
  thunderstorm: "雷雨",
  snow: "降雪",
  air_quality: "空气质量",
};

const riskIcons: Record<string, string> = {
  rain: "☂",
  heat: "☀",
  cold: "❄",
  wind: "≋",
  thunderstorm: "ϟ",
  snow: "❄",
  air_quality: "◎",
};

const severityPresentation: Record<
  string,
  { label: string; cardClass: string; badgeClass: string }
> = {
  low: {
    label: "注意天气",
    cardClass: "border-sky-200 bg-sky-50/70",
    badgeClass: "bg-sky-100 text-sky-800",
  },
  medium: {
    label: "建议调整",
    cardClass: "border-amber-200 bg-amber-50/70",
    badgeClass: "bg-amber-100 text-amber-900",
  },
  high: {
    label: "优先调整",
    cardClass: "border-orange-200 bg-orange-50/70",
    badgeClass: "bg-orange-100 text-orange-900",
  },
  extreme: {
    label: "避免户外",
    cardClass: "border-rose-200 bg-rose-50/70",
    badgeClass: "bg-rose-100 text-rose-900",
  },
};

const severityRank: Record<string, number> = {
  low: 1,
  medium: 2,
  high: 3,
  extreme: 4,
};

interface WeatherReplacementPair {
  replacedItemId: string;
  replacedTitle: string;
  replacement: CandidatePoi;
  distanceMeters: number;
  sameDistrict: boolean;
  weatherReason: string;
}

type WeatherReplacementProposal =
  | {
      status: "ready";
      proposalId: string;
      targetDate: string;
      replacements: WeatherReplacementPair[];
    }
  | {
      status: "insufficient";
      proposalId: string;
      targetDate: string;
      affectedCount: number;
      availableCount: number;
      replacements: WeatherReplacementPair[];
    };

interface RankedIndoorCandidate {
  candidate: CandidatePoi;
  distance: number;
  sameDistrict: boolean;
}

interface AffectedWeatherItem {
  item: TripPlan["days"][number]["items"][number];
  candidate: CandidatePoi;
}

interface WeatherDayRiskScope {
  targetDate: string;
  risks: WeatherRisk[];
}

function rankIndoorCandidates(
  affected: AffectedWeatherItem,
  candidates: CandidatePoi[],
): RankedIndoorCandidate[] {
  return candidates
    .map((candidate) => ({
      candidate,
      distance: distanceMeters(affected.candidate, candidate),
      sameDistrict:
        Boolean(affected.candidate.district) &&
        affected.candidate.district === candidate.district,
    }))
    .sort(
      (left, right) =>
        Number(right.sameDistrict) - Number(left.sameDistrict) ||
        left.distance - right.distance ||
        left.candidate.name.localeCompare(right.candidate.name, "zh-CN"),
    );
}

function weatherReasonFor(
  candidate: CandidatePoi,
  significantRisks: WeatherRisk[],
) {
  return [
    ...new Set(
      significantRisks
        .filter((risk) => riskAffectsCandidate(risk, candidate))
        .map((risk) => riskLabels[risk.risk_type] ?? "天气变化"),
    ),
  ].join("、");
}

function affectedWeatherItems({
  plan,
  targetDate,
  risks,
  candidates,
  minimumSeverityRank,
}: {
  plan: TripPlan;
  targetDate: string;
  risks: WeatherRisk[];
  candidates: CandidatePoi[];
  minimumSeverityRank: number;
}) {
  const relevantRisks = risks.filter(
    (risk) => (severityRank[risk.severity] ?? 0) >= minimumSeverityRank,
  );
  if (!relevantRisks.length) return { affectedItems: [], relevantRisks };

  const day = plan.days.find((item) => item.date === targetDate);
  if (!day) return { affectedItems: [], relevantRisks };

  const candidateById = new Map(candidates.map((candidate) => [candidate.candidate_id, candidate]));
  const affectedItems = day.items
    .flatMap((item) => {
      if (item.kind !== "attraction" || !item.candidate_id) return [];
      const candidate = candidateById.get(item.candidate_id);
      if (
        !candidate ||
        !["outdoor", "mixed"].includes(candidate.environment) ||
        !relevantRisks.some((risk) => riskAffectsCandidate(risk, candidate))
      ) {
        return [];
      }
      return [{ item, candidate }];
    })
    .sort((left, right) => {
      const leftRank = left.candidate.environment === "outdoor" ? 0 : 1;
      const rightRank = right.candidate.environment === "outdoor" ? 0 : 1;
      return leftRank - rightRank || left.item.start_at.localeCompare(right.item.start_at);
    });
  return { affectedItems, relevantRisks };
}

function eligibleIndoorCandidates(plan: TripPlan, candidates: CandidatePoi[]) {
  return [
    ...new Map(
      candidates
        .filter(
          (candidate) =>
            candidate.environment === "indoor" &&
            candidate.city === plan.destination_city &&
            !candidate.categories.includes("餐饮服务"),
        )
        .map((candidate) => [candidate.candidate_id, candidate]),
    ).values(),
  ];
}

/*
 * Build one atomic day-scoped proposal. A proposal is only actionable when every
 * affected outdoor or mixed activity has a distinct grounded indoor candidate.
 */
function buildReplacementProposal({
  plan,
  targetDate,
  risks,
  candidates,
  replacementCandidates,
  minimumSeverityRank = 2,
  excludedCandidateIds = new Set<string>(),
}: {
  plan: TripPlan;
  targetDate: string;
  risks: WeatherRisk[];
  candidates: CandidatePoi[];
  replacementCandidates: CandidatePoi[];
  minimumSeverityRank?: number;
  excludedCandidateIds?: ReadonlySet<string>;
}): WeatherReplacementProposal | null {
  const { affectedItems, relevantRisks } = affectedWeatherItems({
    plan,
    targetDate,
    risks,
    candidates,
    minimumSeverityRank,
  });
  if (!affectedItems.length) return null;

  const indoorCandidates = eligibleIndoorCandidates(plan, replacementCandidates).filter(
    (candidate) => !excludedCandidateIds.has(candidate.candidate_id),
  );
  const availableById = new Map(
    indoorCandidates.map((candidate) => [candidate.candidate_id, candidate]),
  );
  const replacements: WeatherReplacementPair[] = [];
  for (const affected of affectedItems) {
    const ranked = rankIndoorCandidates(affected, [...availableById.values()]);
    const selected = ranked[0];
    if (!selected) break;
    availableById.delete(selected.candidate.candidate_id);
    replacements.push({
      replacedItemId: affected.item.item_id,
      replacedTitle: affected.item.title,
      replacement: selected.candidate,
      distanceMeters: selected.distance,
      sameDistrict: selected.sameDistrict,
      weatherReason: weatherReasonFor(affected.candidate, relevantRisks),
    });
  }
  if (replacements.length < affectedItems.length) {
    return {
      status: "insufficient",
      proposalId: `${plan.plan_id}:${targetDate}:${minimumSeverityRank}:insufficient:${affectedItems.length}:${replacements.length}`,
      targetDate,
      affectedCount: affectedItems.length,
      availableCount: replacements.length,
      replacements,
    };
  }
  return {
    status: "ready",
    proposalId: `${plan.plan_id}:${targetDate}:${minimumSeverityRank}:${replacements
      .map((item) => `${item.replacedItemId}:${item.replacement.candidate_id}`)
      .join(",")}`,
    targetDate,
    replacements,
  };
}

function buildReplacementProposalsByDate({
  plan,
  dayScopes,
  candidates,
  replacementCandidates,
}: {
  plan: TripPlan;
  dayScopes: WeatherDayRiskScope[];
  candidates: CandidatePoi[];
  replacementCandidates: CandidatePoi[];
}) {
  const proposals = new Map<string, WeatherReplacementProposal>();
  const usedCandidateIds = new Set<string>();
  for (const scope of dayScopes) {
    const proposal = buildReplacementProposal({
      plan,
      targetDate: scope.targetDate,
      risks: scope.risks,
      candidates,
      replacementCandidates,
      excludedCandidateIds: usedCandidateIds,
    });
    if (!proposal) continue;
    proposals.set(scope.targetDate, proposal);
    proposal.replacements.forEach((replacement) =>
      usedCandidateIds.add(replacement.replacement.candidate_id),
    );
  }
  return proposals;
}

function buildLowRiskBackupsByDate({
  plan,
  dayScopes,
  candidates,
  replacementCandidates,
}: {
  plan: TripPlan;
  dayScopes: WeatherDayRiskScope[];
  candidates: CandidatePoi[];
  replacementCandidates: CandidatePoi[];
}) {
  const indoorCandidates = eligibleIndoorCandidates(plan, replacementCandidates);
  const rankedPairs = dayScopes.flatMap((scope) => {
    const { affectedItems, relevantRisks } = affectedWeatherItems({
      plan,
      targetDate: scope.targetDate,
      risks: scope.risks,
      candidates,
      minimumSeverityRank: 1,
    });
    return affectedItems.flatMap((affected) =>
      rankIndoorCandidates(affected, indoorCandidates).map((ranked) => ({
        targetDate: scope.targetDate,
        affected,
        ranked,
        weatherReason: weatherReasonFor(affected.candidate, relevantRisks),
      })),
    );
  });
  rankedPairs.sort(
    (left, right) =>
      Number(right.ranked.sameDistrict) - Number(left.ranked.sameDistrict) ||
      left.ranked.distance - right.ranked.distance ||
      left.targetDate.localeCompare(right.targetDate) ||
      left.affected.item.start_at.localeCompare(right.affected.item.start_at) ||
      left.ranked.candidate.name.localeCompare(right.ranked.candidate.name, "zh-CN"),
  );

  const backups = new Map<string, WeatherReplacementPair[]>();
  const usedItemIds = new Set<string>();
  const usedCandidateIds = new Set<string>();
  for (const pair of rankedPairs) {
    if (
      usedItemIds.has(pair.affected.item.item_id) ||
      usedCandidateIds.has(pair.ranked.candidate.candidate_id)
    ) {
      continue;
    }
    usedItemIds.add(pair.affected.item.item_id);
    usedCandidateIds.add(pair.ranked.candidate.candidate_id);
    const dayBackups = backups.get(pair.targetDate) ?? [];
    dayBackups.push({
      replacedItemId: pair.affected.item.item_id,
      replacedTitle: pair.affected.item.title,
      replacement: pair.ranked.candidate,
      distanceMeters: pair.ranked.distance,
      sameDistrict: pair.ranked.sameDistrict,
      weatherReason: pair.weatherReason,
    });
    backups.set(pair.targetDate, dayBackups);
  }
  return backups;
}

function formatDate(date: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "long",
    day: "numeric",
    weekday: "short",
  }).format(new Date(`${date}T12:00:00+08:00`));
}

function strongestSeverity(risks: WeatherRisk[]) {
  return risks.reduce(
    (strongest, risk) =>
      (severityRank[risk.severity] ?? 0) > (severityRank[strongest] ?? 0)
        ? risk.severity
        : strongest,
    "low",
  );
}

function weatherMetric(risk: WeatherRisk, key: string) {
  const value = risk.metrics?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function weatherFact(risk: WeatherRisk) {
  const forecastSummary = risk.threshold_description.match(
    /(?:高德预报)?天气包含[:：]\s*(.+)$/,
  )?.[1];
  if (forecastSummary) return forecastSummary.replaceAll("/", "转");

  const dayTemperature = weatherMetric(risk, "day_temperature_c");
  if (dayTemperature !== null) return `最高 ${dayTemperature}℃`;
  const nightTemperature = weatherMetric(risk, "night_temperature_c");
  if (nightTemperature !== null) return `最低 ${nightTemperature}℃`;
  const windPower = weatherMetric(risk, "wind_power_level");
  if (windPower !== null) return `最大风力 ${windPower} 级`;

  if (risk.risk_type === "rain") return "有降雨";
  if (risk.risk_type === "snow") return "有降雪";
  return riskLabels[risk.risk_type] ?? "天气有变化";
}

function friendlyWeatherCopy(value: string) {
  return value.replaceAll(",", "，").replaceAll("Provider", "天气服务");
}

function canonicalActivityType(value: string) {
  return value.normalize("NFKC").toLocaleLowerCase().replaceAll(/[\p{P}\p{Z}]/gu, "");
}

function riskAffectsCandidate(risk: WeatherRisk, candidate: CandidatePoi) {
  if (!risk.affected_activity_types?.length) {
    return candidate.environment === "outdoor" || candidate.environment === "mixed";
  }
  const candidateTypes = new Set(
    [candidate.environment, ...candidate.categories, ...candidate.tags].map(
      canonicalActivityType,
    ),
  );
  if (candidate.environment === "outdoor" || candidate.environment === "mixed") {
    ["outdoor", "户外", "室外"].map(canonicalActivityType).forEach((item) =>
      candidateTypes.add(item),
    );
  }
  return risk.affected_activity_types.some((item) =>
    candidateTypes.has(canonicalActivityType(item)),
  );
}

function distanceMeters(origin: CandidatePoi, destination: CandidatePoi) {
  const earthRadiusMeters = 6_371_000;
  const toRadians = (value: number) => (value * Math.PI) / 180;
  const latitudeDelta = toRadians(
    destination.location.latitude - origin.location.latitude,
  );
  const longitudeDelta = toRadians(
    destination.location.longitude - origin.location.longitude,
  );
  const originLatitude = toRadians(origin.location.latitude);
  const destinationLatitude = toRadians(destination.location.latitude);
  const value =
    Math.sin(latitudeDelta / 2) ** 2 +
    Math.cos(originLatitude) *
      Math.cos(destinationLatitude) *
      Math.sin(longitudeDelta / 2) ** 2;
  return 2 * earthRadiusMeters * Math.asin(Math.sqrt(value));
}

function formatDistance(value: number) {
  if (value < 1_000) return `约 ${Math.max(100, Math.round(value / 100) * 100)} 米`;
  return `约 ${(value / 1_000).toFixed(1)} 公里`;
}

function lowRiskAdvice(risks: WeatherRisk[]) {
  if (risks.some((risk) => risk.risk_type === "rain")) {
    return "小雨通常不必调整全天行程，建议携带雨具，并在出发前复核最新预报。";
  }
  return "当前天气风险较低，通常可以保留原计划，并在出发前复核最新预报。";
}

function ReplacementPairList({
  replacements,
  mode = "proposal",
  reviewBusy = false,
  onSelect,
}: {
  replacements: WeatherReplacementPair[];
  mode?: "proposal" | "backup";
  reviewBusy?: boolean;
  onSelect?: (replacement: WeatherReplacementPair) => void;
}) {
  return (
    <div className="mt-3 space-y-2" data-testid="weather-replacement-list">
      {replacements.map((replacement) => (
        <div
          className="rounded-xl border border-emerald-100 bg-emerald-50/60 px-3 py-2.5"
          data-testid="weather-replacement-option"
          key={replacement.replacedItemId}
        >
          <p className="text-xs font-semibold leading-5 text-slate-900">
            {replacement.replacedTitle}
            <span className="mx-2 text-emerald-700" aria-hidden="true">
              →
            </span>
            {replacement.replacement.name}
          </p>
          <p className="mt-1 text-[11px] leading-5 text-slate-600">
            室内场馆
            {replacement.sameDistrict
              ? ` · 同在${replacement.replacement.district}`
              : ` · 距原地点直线${formatDistance(replacement.distanceMeters)}`}
            {mode === "backup"
              ? ` · 适合${replacement.weatherReason}天气`
              : ` · 因${replacement.weatherReason}调整`}
          </p>
          {onSelect ? (
            <button
              className="mt-2 rounded-lg bg-emerald-900 px-3 py-2 text-[11px] font-semibold text-white transition hover:bg-emerald-800 disabled:opacity-50"
              disabled={reviewBusy}
              onClick={() => onSelect(replacement)}
              type="button"
            >
              {reviewBusy ? "正在调整…" : "使用这个备选"}
            </button>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export function WeatherOutlook({
  plan,
  candidates,
  replacementCandidates,
  recovery,
  canRequestRevision,
  reviewBusy,
  reviewError,
  onRequestRevision,
}: {
  plan: TripPlan;
  candidates: CandidatePoi[];
  replacementCandidates: CandidatePoi[];
  recovery: WeatherIndoorRecovery | null;
  canRequestRevision: boolean;
  reviewBusy: boolean;
  reviewError: string | null;
  onRequestRevision: (
    comment: string,
    revision: PlanRevisionSelection,
  ) => void | Promise<void>;
}) {
  const [dismissedProposalIds, setDismissedProposalIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [expandedLowRiskDates, setExpandedLowRiskDates] = useState<Set<string>>(
    () => new Set(),
  );
  const riskById = new Map(plan.weather_risks.map((risk) => [risk.risk_id, risk]));
  const candidateById = new Map(candidates.map((candidate) => [candidate.candidate_id, candidate]));
  const daySummaries = plan.days
    .map((day, index) => {
      const risks = day.weather_risk_ids
        .map((riskId) => riskById.get(riskId))
        .filter((risk): risk is WeatherRisk => risk !== undefined);
      const affectedPlans = day.items
        .filter((item) => {
          if (item.kind !== "attraction" || !item.candidate_id) return false;
          const candidate = candidateById.get(item.candidate_id);
          return (
            candidate !== undefined &&
            risks.some((risk) => riskAffectsCandidate(risk, candidate))
          );
        })
        .map((item) => item.title);
      const requiresAdjustment =
        affectedPlans.length > 0 &&
        risks.some((risk) => (severityRank[risk.severity] ?? 0) >= 2);
      return { day, index, risks, affectedPlans, requiresAdjustment };
    })
    .filter(({ risks }) => risks.length > 0);
  const adjustmentDayCount = daySummaries.filter(
    ({ requiresAdjustment }) => requiresAdjustment,
  ).length;
  const noticeDayCount = daySummaries.length - adjustmentDayCount;
  const replacementProposalsByDate = buildReplacementProposalsByDate({
    plan,
    dayScopes: daySummaries
      .filter(({ requiresAdjustment }) => requiresAdjustment)
      .map(({ day, risks }) => ({ targetDate: day.date, risks })),
    candidates,
    replacementCandidates,
  });
  const lowRiskBackupsByDate = buildLowRiskBackupsByDate({
    plan,
    dayScopes: daySummaries
      .filter(({ requiresAdjustment }) => !requiresAdjustment)
      .map(({ day, risks }) => ({ targetDate: day.date, risks })),
    candidates,
    replacementCandidates,
  });
  const latestRetrievedAt = plan.weather_risks.reduce<string | null>((latest, risk) => {
    if (!latest || new Date(risk.source.retrieved_at) > new Date(latest)) {
      return risk.source.retrieved_at;
    }
    return latest;
  }, null);
  const requestWeatherRevision = (proposal: Extract<WeatherReplacementProposal, { status: "ready" }>) =>
    onRequestRevision(
      `采用全天室内方案：${proposal.replacements
        .map((item) => `${item.replacedTitle}改为${item.replacement.name}`)
        .join("；")}。`,
      {
        kind: "replace_day_activities",
        targetDate: proposal.targetDate,
        replacements: proposal.replacements.map((item) => ({
          replacedItemId: item.replacedItemId,
          replacementCandidateId: item.replacement.candidate_id,
        })),
      },
    );
  const requestLowRiskRevision = (
    targetDate: string,
    replacement: WeatherReplacementPair,
  ) =>
    onRequestRevision(
      `采用雨天备选：${replacement.replacedTitle}改为${replacement.replacement.name}。`,
      {
        kind: "replace_activity",
        targetDate,
        replacedItemId: replacement.replacedItemId,
        replacementCandidateId: replacement.replacement.candidate_id,
      },
    );

  return (
    <article
      className="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm"
      data-testid="weather-outlook"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="eyebrow">出行天气</p>
          <h3 className="mt-2 font-semibold">逐日天气提醒</h3>
        </div>
        {daySummaries.length ? (
          <span className="rounded-full bg-amber-50 px-3 py-1 text-[11px] font-semibold text-amber-900">
            {adjustmentDayCount
              ? `${adjustmentDayCount} 天建议调整${noticeDayCount ? ` · ${noticeDayCount} 天注意天气` : ""}`
              : `${noticeDayCount} 天有天气提醒`}
          </span>
        ) : null}
      </div>

      {daySummaries.length ? (
        <div className="mt-5 space-y-4">
          {daySummaries.map(({ day, index, risks, affectedPlans, requiresAdjustment }) => {
            const severity = strongestSeverity(risks);
            const presentation = severityPresentation[severity] ?? severityPresentation.low;
            const isLowRisk = (severityRank[severity] ?? 0) < 2;
            const advice = isLowRisk
              ? [lowRiskAdvice(risks)]
              : [...new Set(risks.map((risk) => friendlyWeatherCopy(risk.advisory)))];
            const proposal = replacementProposalsByDate.get(day.date) ?? null;
            const lowRiskBackups = lowRiskBackupsByDate.get(day.date) ?? [];
            const lowRiskExpanded = expandedLowRiskDates.has(day.date);
            const showProposal =
              canRequestRevision &&
              proposal?.status === "ready" &&
              !dismissedProposalIds.has(proposal.proposalId);
            return (
              <section
                className={`rounded-2xl border p-4 ${presentation.cardClass}`}
                data-testid="weather-day-card"
                key={day.date}
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-[11px] font-semibold tracking-[0.16em] text-slate-500">
                      第 {index + 1} 天
                    </p>
                    <p className="mt-1 text-base font-semibold text-slate-950">
                      {formatDate(day.date)}
                    </p>
                  </div>
                  <span
                    className={`rounded-full px-3 py-1 text-[11px] font-semibold ${presentation.badgeClass}`}
                  >
                    {presentation.label}
                  </span>
                </div>

                <div className="mt-4 grid gap-2 sm:grid-cols-2">
                  {risks.map((risk) => (
                    <div
                      className="rounded-xl border border-white/80 bg-white/80 px-3 py-2.5"
                      key={risk.risk_id}
                    >
                      <p className="text-xs font-semibold text-slate-900">
                        <span aria-hidden="true" className="mr-1.5">
                          {riskIcons[risk.risk_type] ?? "•"}
                        </span>
                        {riskLabels[risk.risk_type] ?? "天气变化"}
                      </p>
                      <p className="mt-1 text-sm font-semibold text-slate-700">
                        {weatherFact(risk)}
                      </p>
                    </div>
                  ))}
                </div>

                <div
                  className="mt-4 border-t border-slate-900/10 pt-3"
                  data-testid="weather-advice"
                >
                  <p className="text-xs font-semibold text-slate-900">当天怎么安排</p>
                  <ul className="mt-2 space-y-1.5 text-xs leading-5 text-slate-700">
                    {advice.map((item) => (
                      <li className="flex gap-2" key={item}>
                        <span aria-hidden="true" className="text-emerald-700">
                          →
                        </span>
                        <span>{item}</span>
                      </li>
                    ))}
                  </ul>
                </div>

                {requiresAdjustment && affectedPlans.length ? (
                  <p
                    className="mt-3 rounded-xl bg-white/70 px-3 py-2 text-[11px] leading-5 text-slate-600"
                    data-testid="weather-affected-plans"
                  >
                    优先检查：{affectedPlans.join("、")}
                  </p>
                ) : null}

                {isLowRisk && canRequestRevision && lowRiskBackups.length ? (
                  <div className="mt-3" data-testid="low-risk-weather-backup">
                    {!lowRiskExpanded ? (
                      <button
                        className="rounded-xl border border-sky-300 bg-white px-3 py-2.5 text-xs font-semibold text-sky-900 transition hover:bg-sky-50"
                        onClick={() =>
                          setExpandedLowRiskDates((current) => {
                            const next = new Set(current);
                            next.add(day.date);
                            return next;
                          })
                        }
                        type="button"
                      >
                        查看雨天备选
                      </button>
                    ) : (
                      <div
                        className="rounded-2xl border border-sky-200 bg-white p-4"
                        data-testid="low-risk-weather-backup-panel"
                      >
                        <p className="text-[11px] font-bold tracking-[0.12em] text-sky-800">
                          雨天可以这样换
                        </p>
                        <p className="mt-2 text-xs leading-5 text-slate-700">
                          如果临近出发时雨势加大，可以按需把下面的活动改到室内。
                        </p>
                        <ReplacementPairList
                          mode="backup"
                          onSelect={(replacement) =>
                            void requestLowRiskRevision(day.date, replacement)
                          }
                          replacements={lowRiskBackups}
                          reviewBusy={reviewBusy}
                        />
                        <div className="mt-3 flex flex-wrap gap-2">
                          <button
                            className="rounded-xl border border-slate-300 bg-white px-3 py-2.5 text-xs font-semibold text-slate-700 transition hover:bg-slate-50"
                            onClick={() =>
                              setExpandedLowRiskDates((current) => {
                                const next = new Set(current);
                                next.delete(day.date);
                                return next;
                              })
                            }
                            type="button"
                          >
                            收起备选
                          </button>
                        </div>
                        {reviewError ? (
                          <p className="mt-3 text-[11px] leading-5 text-rose-700" role="alert">
                            {reviewError}
                          </p>
                        ) : null}
                      </div>
                    )}
                  </div>
                ) : null}

                {showProposal ? (
                  <div
                    className="mt-3 rounded-2xl border border-emerald-200 bg-white p-4"
                    data-testid="weather-replacement-proposal"
                  >
                    <p className="text-[11px] font-bold tracking-[0.12em] text-emerald-800">
                      全天室内方案
                    </p>
                    <p className="mt-2 text-sm font-semibold leading-6 text-slate-950">
                      当天 {proposal.replacements.length} 个受天气影响的活动将一起调整
                    </p>
                    {recovery?.status === "recovered" && recovery.observations.length ? (
                      <p
                        className="mt-2 rounded-xl bg-emerald-50 px-3 py-2 text-[11px] leading-5 text-emerald-900"
                        data-testid="weather-recovery-summary"
                      >
                        已根据当天活动区域补充找到 {recovery.observations.length}
                        个室内地点，再按距离生成这份方案。
                      </p>
                    ) : null}
                    <ReplacementPairList replacements={proposal.replacements} />
                    <p className="mt-2 text-[11px] leading-5 text-slate-600">
                      已是室内的活动会保留；确认一次即可重新安排当天时间、路线和附近用餐建议。
                    </p>
                    <div className="mt-3 grid gap-2 sm:grid-cols-2">
                      <button
                        className="rounded-xl bg-emerald-900 px-3 py-2.5 text-xs font-semibold text-white transition hover:bg-emerald-800 disabled:opacity-50"
                        disabled={reviewBusy}
                        onClick={() => void requestWeatherRevision(proposal)}
                        type="button"
                      >
                        {reviewBusy ? "正在生成调整版…" : "采用全天室内方案"}
                      </button>
                      <button
                        className="rounded-xl border border-slate-300 bg-white px-3 py-2.5 text-xs font-semibold text-slate-700 transition hover:bg-slate-50 disabled:opacity-50"
                        disabled={reviewBusy}
                        onClick={() =>
                          setDismissedProposalIds((current) => {
                            const next = new Set(current);
                            next.add(proposal.proposalId);
                            return next;
                          })
                        }
                        type="button"
                      >
                        保留原安排
                      </button>
                    </div>
                    {reviewError ? (
                      <p className="mt-3 text-[11px] leading-5 text-rose-700" role="alert">
                        {reviewError}
                      </p>
                    ) : null}
                  </div>
                ) : canRequestRevision && proposal?.status === "insufficient" ? (
                  <p
                    className="mt-3 rounded-xl border border-amber-200 bg-white/70 px-3 py-2.5 text-[11px] leading-5 text-slate-600"
                    data-testid="weather-replacement-insufficient"
                  >
                    目前还没有找到足够合适且不重复的室内地点，暂时无法重新安排全天。
                    {recovery?.status === "insufficient"
                      ? " 建议先保留当前行程，临近出发时再根据天气调整。"
                      : " 你也可以在行程修改中单独更换某个活动。"}
                  </p>
                ) : requiresAdjustment && affectedPlans.length && canRequestRevision && !proposal ? (
                  <p className="mt-3 text-[11px] leading-5 text-slate-500">
                    当前候选中没有可直接替换的室内场馆。
                  </p>
                ) : null}
              </section>
            );
          })}
        </div>
      ) : (
        <div className="mt-5 rounded-2xl border border-slate-200 bg-slate-50 p-4">
          <p className="text-sm font-semibold text-slate-800">当前没有逐日风险信息</p>
          <p className="mt-2 text-xs leading-5 text-slate-600">
            这不代表天气一定适宜：可能尚未进入短期预报范围，也可能当天没有触发风险提醒。请在临近出发时查看最新预报。
          </p>
        </div>
      )}

      {latestRetrievedAt ? (
        <p className="mt-4 text-[10px] leading-5 text-slate-400">
          天气更新于
          {new Date(latestRetrievedAt).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}
          ，预报可能变化，出发当天请再次确认。
        </p>
      ) : null}
    </article>
  );
}
