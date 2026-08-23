import type {
  BudgetCategory,
  CandidatePoi,
  PlanningTaskSnapshot,
} from "@/lib/planning-task";

const categoryLabels: Record<BudgetCategory, string> = {
  lodging: "住宿",
  transport: "交通",
  food: "餐饮",
  admission: "门票",
  activity: "活动",
  other: "其他",
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

export function PlanningResults({ snapshot }: { snapshot: PlanningTaskSnapshot }) {
  const state = snapshot.result?.state;
  const verticalSlice = state?.vertical_slice;
  if (!state || !verticalSlice) {
    return null;
  }

  const { plan, validation } = verticalSlice;
  const review = state.review_request;
  const candidates = verticalSlice.upstream.candidates;
  const budget = validation.budget;
  const hasBudget = budget.status !== "not_requested";
  const displayCity = plan.destination_city.replace(/市$/, "");

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

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.7fr)_minmax(320px,.75fr)]">
        <article className="rounded-[1.75rem] border border-slate-200 bg-white p-5 shadow-sm sm:p-7">
          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="eyebrow">Itinerary</p>
              <h3 className="mt-1 text-lg font-semibold">可追溯行程时间线</h3>
            </div>
            <span className="text-xs text-slate-400">草案 · 待人工确认</span>
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
                  {day.items.map((item) => (
                    <div className="relative rounded-2xl bg-slate-50 px-4 py-4" key={item.item_id}>
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
                          上一站至此：{item.route_from_previous.duration_minutes} 分钟 · {item.route_from_previous.distance_meters} 米
                        </p>
                      ) : null}
                    </div>
                  ))}
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
                <h3 className="mt-2 text-lg font-semibold">{review ? "等待你的确认" : "审核状态"}</h3>
              </div>
              <span className="flex size-10 items-center justify-center rounded-full bg-amber-300 text-lg text-slate-950">!</span>
            </div>
            <p className="mt-5 text-sm leading-6 text-slate-300">
              {review?.prompt ?? "当前任务没有生成待审核请求。"}
            </p>
            {review ? (
              <div className="mt-5 grid grid-cols-2 gap-2">
                <button className="rounded-xl bg-white/10 px-3 py-3 text-xs font-semibold text-white/45" disabled>
                  批准草案
                </button>
                <button className="rounded-xl border border-white/10 px-3 py-3 text-xs font-semibold text-white/45" disabled>
                  请求修改
                </button>
              </div>
            ) : null}
            <p className="mt-3 text-[11px] leading-5 text-slate-500">
              操作按钮将在 EZ-403 接通审核恢复 API；当前不会伪造已执行的人工决策。
            </p>
          </article>

          <article className="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm">
            <p className="eyebrow">Budget guard</p>
            <div className="mt-3 flex items-end justify-between gap-4">
              <div>
                <p className="text-2xl font-semibold tracking-tight">
                  {hasBudget && budget.total_limit !== null ? formatMoney(budget.total_limit) : "未设置"}
                </p>
                <p className="mt-1 text-xs text-slate-500">整趟行程硬预算</p>
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
                当前产品 API 工作流尚未注入天气风险；这不代表旅行日期天气良好。天气 Agent 将在后续完整编排中接入。
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
