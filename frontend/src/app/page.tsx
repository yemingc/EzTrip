import { TripPlannerWorkspace } from "@/components/trip-planner-workspace";

export const dynamic = "force-dynamic";

const CHINA_TIME_ZONE = "Asia/Shanghai";
const DAY_IN_MILLISECONDS = 24 * 60 * 60 * 1000;

function dateInChinaAfter(days: number, now: Date) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: CHINA_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(now.getTime() + days * DAY_IN_MILLISECONDS));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

export default function Home() {
  const now = new Date();
  const earliestStartDate = dateInChinaAfter(0, now);
  const defaultStartDate = dateInChinaAfter(7, now);

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
            <p className="text-[10px] font-semibold tracking-[0.12em] text-slate-400">旅行规划助手</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="hidden rounded-full border border-emerald-900/10 bg-white/60 px-3 py-2 text-[11px] font-medium text-slate-600 backdrop-blur sm:inline-flex">
            国内城市行程规划
          </span>
          <a
            className="rounded-full bg-slate-950 px-4 py-2 text-[11px] font-semibold text-white transition hover:bg-emerald-950"
            href="#planner"
          >
            开始规划
          </a>
        </div>
      </header>

      <section className="relative mx-auto grid max-w-[1480px] gap-8 px-4 pb-8 pt-6 sm:px-6 sm:pb-10 sm:pt-14 lg:grid-cols-[minmax(0,1.3fr)_minmax(340px,.7fr)] lg:px-8 lg:pb-14 lg:pt-20">
        <div>
          <p className="eyebrow">从旅行想法到每日安排</p>
          <h1 className="mt-3 max-w-4xl text-[2.65rem] font-semibold leading-[1.02] tracking-[-0.055em] sm:mt-5 sm:text-6xl lg:text-[5.25rem]">
            把想去的地方，
            <span className="text-emerald-800">变成一份好用的行程。</span>
          </h1>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-slate-600 sm:mt-6 sm:text-lg sm:leading-8">
            填写目的地、日期、预算和旅行偏好，获得按天安排的景点、通勤、住宿和附近用餐建议。生成后还可以确认或局部调整。
          </p>
        </div>

        <div className="hidden self-end rounded-[1.75rem] border border-white/70 bg-white/62 p-5 shadow-[0_20px_60px_rgba(15,23,42,.07)] backdrop-blur sm:block sm:p-6">
          <div className="flex items-center justify-between">
            <p className="text-sm font-semibold">你可以这样使用</p>
            <span className="flex items-center gap-2 text-[11px] font-semibold text-emerald-800">
              <span className="size-2 rounded-full bg-emerald-500" />
              行程可调整
            </span>
          </div>
          <dl className="mt-5 space-y-4 text-xs">
            <div className="flex items-start justify-between gap-6 border-b border-slate-200/70 pb-4">
              <dt className="text-slate-500">按天规划</dt>
              <dd className="text-right font-semibold">安排景点与通勤时间</dd>
            </div>
            <div className="flex items-start justify-between gap-6 border-b border-slate-200/70 pb-4">
              <dt className="text-slate-500">住宿与用餐</dt>
              <dd className="text-right font-semibold">结合每天路线推荐</dd>
            </div>
            <div className="flex items-start justify-between gap-6">
              <dt className="text-slate-500">确认与修改</dt>
              <dd className="text-right font-semibold text-emerald-800">保留其他日期，只改所选行程</dd>
            </div>
          </dl>
        </div>
      </section>

      <div id="planner">
        <TripPlannerWorkspace
          defaultStartDate={defaultStartDate}
          earliestStartDate={earliestStartDate}
        />
      </div>
    </main>
  );
}
