export default function Home() {
  return (
    <main className="min-h-screen bg-[radial-gradient(circle_at_top_left,_#dff4ed,_#f7f5ed_45%,_#edf2f7)] px-6 py-10 text-slate-950 sm:px-10 lg:px-16">
      <div className="mx-auto flex min-h-[calc(100vh-5rem)] max-w-6xl flex-col justify-between rounded-[2rem] border border-white/80 bg-white/70 p-7 shadow-[0_30px_80px_rgba(15,23,42,0.12)] backdrop-blur sm:p-10 lg:p-14">
        <header className="flex items-center justify-between gap-6">
          <div className="flex items-center gap-3">
            <span className="grid size-11 place-items-center rounded-2xl bg-emerald-950 text-sm font-bold tracking-wide text-white">
              EZ
            </span>
            <div>
              <p className="text-lg font-semibold tracking-tight">EzTrip</p>
              <p className="text-xs text-slate-500">Multi-Agent Travel Ops</p>
            </div>
          </div>
          <span className="rounded-full border border-emerald-900/15 bg-emerald-50 px-4 py-2 text-xs font-medium text-emerald-900">
            Gate 0 · 工程骨架
          </span>
        </header>

        <section className="grid gap-12 py-16 lg:grid-cols-[1.25fr_0.75fr] lg:items-end">
          <div>
            <p className="mb-5 text-sm font-semibold uppercase tracking-[0.22em] text-emerald-800">
              Plan · Validate · Recover
            </p>
            <h1 className="max-w-4xl text-5xl font-semibold leading-[1.06] tracking-[-0.045em] sm:text-6xl lg:text-7xl">
              旅行计划不只要生成，
              <span className="text-emerald-800">还要经得起变化。</span>
            </h1>
            <p className="mt-7 max-w-2xl text-base leading-8 text-slate-600 sm:text-lg">
              面向中国用户的多 Agent 旅行规划项目。把景点、酒店、天气、预算与行程约束汇入同一条可观测工作流，并在天气或预算变化时主动发现影响、解释冲突并重排。
            </p>
          </div>

          <div className="rounded-3xl bg-slate-950 p-6 text-slate-100 shadow-2xl shadow-slate-900/15">
            <div className="mb-7 flex items-center justify-between">
              <p className="text-sm font-medium">系统状态</p>
              <span className="flex items-center gap-2 text-xs text-emerald-300">
                <span className="size-2 rounded-full bg-emerald-400" />
                Foundation ready
              </span>
            </div>
            <dl className="space-y-5 text-sm">
              <div className="flex justify-between border-b border-white/10 pb-4">
                <dt className="text-slate-400">API contract</dt>
                <dd>FastAPI</dd>
              </div>
              <div className="flex justify-between border-b border-white/10 pb-4">
                <dt className="text-slate-400">Workflow target</dt>
                <dd>LangGraph</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-slate-400">State store</dt>
                <dd>PostgreSQL</dd>
              </div>
            </dl>
          </div>
        </section>

        <section className="grid gap-4 border-t border-slate-200 pt-7 md:grid-cols-3">
          {[
            ["真实数据边界", "区分实时 API、沙盒数据与模型推断，避免把生成内容包装成事实。"],
            ["确定性硬约束", "预算、营业时间和空间可达性由可测试的规则校验。"],
            ["可观测修复", "保留 Agent 决策、工具调用和重排前后的证据链。"],
          ].map(([title, description]) => (
            <article key={title} className="rounded-2xl border border-slate-200/80 bg-white/75 p-5">
              <h2 className="font-semibold">{title}</h2>
              <p className="mt-2 text-sm leading-6 text-slate-600">{description}</p>
            </article>
          ))}
        </section>
      </div>
    </main>
  );
}
