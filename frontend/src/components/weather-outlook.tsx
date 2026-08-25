import type { CandidatePoi, TripPlan, WeatherRisk } from "@/lib/planning-task";

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

export function WeatherOutlook({
  plan,
  candidates,
}: {
  plan: TripPlan;
  candidates: CandidatePoi[];
}) {
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
          const environment = candidateById.get(item.candidate_id)?.environment;
          return environment === "outdoor" || environment === "mixed";
        })
        .map((item) => item.title);
      return { day, index, risks, affectedPlans };
    })
    .filter(({ risks }) => risks.length > 0);
  const latestRetrievedAt = plan.weather_risks.reduce<string | null>((latest, risk) => {
    if (!latest || new Date(risk.source.retrieved_at) > new Date(latest)) {
      return risk.source.retrieved_at;
    }
    return latest;
  }, null);

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
            {daySummaries.length} 天建议调整安排
          </span>
        ) : null}
      </div>

      {daySummaries.length ? (
        <div className="mt-5 space-y-4">
          {daySummaries.map(({ day, index, risks, affectedPlans }) => {
            const severity = strongestSeverity(risks);
            const presentation = severityPresentation[severity] ?? severityPresentation.low;
            const advice = [...new Set(risks.map((risk) => friendlyWeatherCopy(risk.advisory)))];
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

                {affectedPlans.length ? (
                  <p
                    className="mt-3 rounded-xl bg-white/70 px-3 py-2 text-[11px] leading-5 text-slate-600"
                    data-testid="weather-affected-plans"
                  >
                    优先检查：{affectedPlans.join("、")}
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
