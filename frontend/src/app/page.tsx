import { TripPlannerWorkspace } from "@/components/trip-planner-workspace";

function dateAfterToday(days: number) {
  const result = new Date();
  result.setUTCDate(result.getUTCDate() + days);
  return result.toISOString().slice(0, 10);
}

export default function Home() {
  return (
    <main className="min-h-screen overflow-hidden bg-[#f4f3ed] text-slate-950">
      <div className="pointer-events-none absolute inset-x-0 top-0 h-[680px] bg-[radial-gradient(circle_at_12%_10%,rgba(16,185,129,.16),transparent_28%),radial-gradient(circle_at_82%_0%,rgba(14,116,144,.10),transparent_26%)]" />

      <header className="relative mx-auto flex max-w-[1480px] items-center justify-between gap-4 px-4 py-5 sm:px-6 lg:px-8">
        <div className="flex items-center gap-3">
          <span className="grid size-11 place-items-center rounded-2xl bg-emerald-950 text-sm font-black tracking-[-0.04em] text-white shadow-lg shadow-emerald-950/15">
            EZ
          </span>
          <div>
            <p className="text-lg font-semibold tracking-[-0.03em]">EzTrip</p>
            <p className="text-[10px] font-semibold uppercase tracking-[0.19em] text-slate-400">Multi-Agent Travel Ops</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="hidden rounded-full border border-emerald-900/10 bg-white/60 px-3 py-2 text-[11px] font-medium text-slate-600 backdrop-blur sm:inline-flex">
            Planning API v1
          </span>
          <a
            className="rounded-full bg-slate-950 px-4 py-2 text-[11px] font-semibold text-white transition hover:bg-emerald-950"
            href="#planner"
          >
            打开工作台
          </a>
        </div>
      </header>

      <section className="relative mx-auto grid max-w-[1480px] gap-8 px-4 pb-10 pt-10 sm:px-6 sm:pt-14 lg:grid-cols-[minmax(0,1.3fr)_minmax(340px,.7fr)] lg:px-8 lg:pb-14 lg:pt-20">
        <div>
          <p className="eyebrow">Plan · Validate · Review</p>
          <h1 className="mt-5 max-w-4xl text-5xl font-semibold leading-[1.02] tracking-[-0.055em] sm:text-6xl lg:text-[5.25rem]">
            一份旅行计划，
            <span className="text-emerald-800">一条完整证据链。</span>
          </h1>
          <p className="mt-6 max-w-2xl text-base leading-8 text-slate-600 sm:text-lg">
            面向中国用户的多 Agent 旅行规划助手。把景点事实、行程草案、预算规则与人工审核组织进可观测工作流，并明确区分实时数据、测试数据和未知信息。
          </p>
        </div>

        <div className="self-end rounded-[1.75rem] border border-white/70 bg-white/62 p-5 shadow-[0_20px_60px_rgba(15,23,42,.07)] backdrop-blur sm:p-6">
          <div className="flex items-center justify-between">
            <p className="text-sm font-semibold">本阶段真实能力</p>
            <span className="flex items-center gap-2 text-[11px] font-semibold text-emerald-800">
              <span className="size-2 rounded-full bg-emerald-500" />
              EZ-402
            </span>
          </div>
          <dl className="mt-5 space-y-4 text-xs">
            <div className="flex items-start justify-between gap-6 border-b border-slate-200/70 pb-4">
              <dt className="text-slate-500">任务进度</dt>
              <dd className="text-right font-semibold">真实 SSE 事件</dd>
            </div>
            <div className="flex items-start justify-between gap-6 border-b border-slate-200/70 pb-4">
              <dt className="text-slate-500">景点来源</dt>
              <dd className="text-right font-semibold">高德协议 Fixture</dd>
            </div>
            <div className="flex items-start justify-between gap-6">
              <dt className="text-slate-500">最终确认</dt>
              <dd className="text-right font-semibold text-amber-700">等待 EZ-403 接通</dd>
            </div>
          </dl>
        </div>
      </section>

      <div id="planner">
        <TripPlannerWorkspace defaultStartDate={dateAfterToday(7)} />
      </div>
    </main>
  );
}
