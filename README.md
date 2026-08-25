# EzTrip（易行）

面向中国城市自由行的可验证、可调整、可追溯旅行规划助手。

当前已完成 Gate 0 工程基线、规格级 smoke scenarios、可观测性探针、第一版旅行领域契约、高德官方 MCP / REST 探针、typed provider adapter、脱敏 fixture、`TripRequest → PlannerContext` 确定性编译层、首条三节点 LangGraph 主链、6 standard + 4 hard 的确定性基线、Constraint Agent、受 provider 候选约束的单 Planner 基线、开放式景点/餐饮 Explore Agent、住宿区域筛选 Stay Agent、确定性预算/基础计划 Validator、北京三日 Gate 2 最小纵向切片、基于 SQLite checkpoint 的可恢复主编排与原生 HITL、Explore/Stay/主动天气三分支并行编排、有界路线矩阵和确定性预算材料层、把这些专业信息包合成为同构完整 `TripPlan` 草案的 schema-constrained Plan Agent、阻止不可靠草案定稿的 Hard Validators、消费 typed issue 的有界 Repair Router，以及 Weather Provider 风险主动触发的局部修复协调器。模型路径都有真实 DeepSeek/LangSmith 隔离评测；纵向切片、恢复、fan-out、材料层、Plan Agent、Hard Validators、Repair Router 与 Weather Repair 都有 fixture 可重放证据。公平 30-case system comparison 已完成离线控制路径重放；另已执行 3-case × 2 repeated-live DeepSeek pilot：42/42 模型调用成功，三个 arm 都是 6/6 finalizable，未观察到成功率提升；Product 的 exact-repeat consistency 为 3/3，Single 为 2/3，但 Product 调用和累计模型延迟更高。该结果只属于既有开发案例，不能写成泛化或真实用户成功率。

产品调用层已切换到 Product Graph V2：一次 FastAPI 任务依次提交 `run_specialists → build_materials → run_plan_agent → validate_hard_plan → [run_repair] → HITL`。可修复 hard error 会进入最多两次/动作的 Repair Router；真实 executor 只重跑 Explore、Stay、Route、Budget 或 Plan 责任链，随后重新执行 Hard Validator，并显式记录执行节点、复用节点、调用计数和 issue diff。任务快照、可回放 SSE、四类人工审核决定、PlanVersion 与结构化局部修改继续复用原协议，并写入本地版本化 SQLite 任务账本；Next.js 把 `task_id` 写入 URL，可在执行、等待审核和审核完成后刷新恢复。Next.js 工作台支持自由填写国内单目的地、2–5 天行程和 fixture/live 数据模式；中文 Request Intake 与 Constraint Agent 先提议带原文 evidence 的字段和约束，确定性代码重算日期、人数、预算与节奏，并把歧义、缺失和表单冲突交给用户确认。确认前除有界 City Resolver 预检外不会进入旅行检索或规划；确认后的版本化 `TripRequest` 才提交 Product Graph。选择 live 即为该请求的显式外部调用授权，不需要额外修改环境开关，Key 始终只保存在服务端。北京、上海、成都只承担有界离线 fixture/E2E，不代表通用中文理解或 live 产品白名单。开放式自然语言重规划尚未接入。协议见 [`docs/api/planning-task-api.md`](docs/api/planning-task-api.md)，前端边界见 [`frontend/README.md`](frontend/README.md)。

## 技术基线

- Backend：Python 3.12、FastAPI、Pydantic v2、SQLAlchemy、Alembic、uv
- Frontend：Next.js 16、React 19、TypeScript、Tailwind CSS、pnpm
- Infrastructure：PostgreSQL 17、Docker Compose、GitHub Actions
- Agent infrastructure：LangGraph、DeepSeek OpenAI-compatible API、LangSmith

## 本地启动

### 1. 环境配置

```powershell
Copy-Item .env.example .env
```

`.env` 仅用于本地，不能提交。当前模板预留 DeepSeek、LangSmith 和高德官方 MCP 配置；真实 Key 只填写在本地 `.env`。

### 2. PostgreSQL

```powershell
docker compose up -d postgres
docker compose ps
```

开发数据库默认映射到主机 `55432` 端口，以避开本机可能已有的 PostgreSQL `5432` 服务；容器内部仍使用标准 `5432`。

### 3. Backend

```powershell
Set-Location backend
uv sync --all-groups
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

健康检查：`GET http://localhost:8000/api/health`

需求提议：`POST http://localhost:8000/api/request-intakes`；确认需求：`POST /api/request-intakes/{draft_id}/confirm`；规划任务：`POST /api/planning-tasks`；SSE：`GET /api/planning-tasks/{task_id}/events`；人工审核：`POST /api/planning-tasks/{task_id}/review-decisions`

### 4. Frontend

```powershell
Set-Location frontend
pnpm install
pnpm dev
```

页面：`http://localhost:3000`

## 验证

Backend：

```powershell
Set-Location backend
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest
```

Frontend：

```powershell
Set-Location frontend
pnpm lint
pnpm typecheck
pnpm build
pnpm exec playwright install chromium
pnpm test:e2e
```

`test:e2e` 会同时启动 fixture backend 与 frontend，不调用 DeepSeek 或实时高德，也不需要 API Key。

## Gate 0 smoke scenarios

`evals/cases/smoke/` 固化了 3 条端到端规格场景：正常北京三日游、确定性预算冲突、天气工具主动发现风险。它们定义跨组件输入、注入条件和必须/禁止行为；Product Graph V2 已在北京两日 fixture 浏览器链路中验证主动天气查询与 Hard Validator，但三条 smoke 仍是更广的规格级基线，不等同于全部 live 产品能力。

```powershell
Set-Location backend
uv run pytest tests/test_smoke_cases.py --no-cov
```

## Gate 0 architecture and observability probe

公开架构决策位于 [`docs/adr/`](docs/adr/)。隔离的三节点探针用于验证 `DeepSeek LLM → weather fixture tool → DeepSeek LLM` 的 LangSmith trace 层级、metadata、错误记录和上传前脱敏；它不是旅行规划实现，也不会调用高德或真实天气数据。

探针会产生少量 DeepSeek API 用量并向配置的 LangSmith Cloud 项目上传固定合成输入。CI 只运行 fake model 和 fixture 测试，不读取真实 Key、调用模型或上传 trace。

```powershell
Set-Location backend
uv run python -m scripts.run_observability_probe
uv run python -m scripts.run_observability_probe --force-tool-error
```

## V1 domain contracts

[`docs/contracts/`](docs/contracts/) 保存从 Pydantic 模型机械导出的 JSON Schema bundle 和可解析示例，覆盖旅行请求、约束、候选 POI/住宿、天气风险、路线、费用、逐日计划、校验问题与计划版本。契约会拒绝未知字段和关键跨字段冲突，并明确区分 live、fixture、用户输入与估算数据。

这些对象目前是后续 Agent、provider normalizer、确定性校验器和 API 的共享数据边界，不表示对应工作流已经实现。

```powershell
Set-Location backend
uv run python -m scripts.export_domain_schemas
uv run pytest tests/test_domain_contract_examples.py tests/test_domain_models.py --no-cov
```

## PlannerContext 确定性编译

[`docs/planning/planner-context.md`](docs/planning/planner-context.md) 说明结构化 `TripRequest` 如何被机械编译成日期与住宿晚数、人数与间夜、预算参考尺度、约束作用域、澄清问题和逐项能力门禁。相同输入会得到相同哈希和结果；房间数、预算或未确认约束不会被模型静默猜测。

```powershell
Set-Location backend
uv run python -m scripts.export_planner_context_example
uv run pytest tests/test_planner_context.py tests/test_domain_contract_examples.py --no-cov
```

当前编译器不负责解析中文原话、调用模型、搜索景点或生成行程；它为下一阶段的 LangGraph 澄清路由与候选搜索提供稳定输入。

## 首条可执行 LangGraph 主链

[`docs/planning/minimal-planning-graph.md`](docs/planning/minimal-planning-graph.md) 实现 `compile_context → clarification_gate → candidate_search`。条件路由按具体能力决定是否继续：缺少预算不影响景点查询，未配置城市则在 provider 调用前停止。候选只来自已确认 `must_visit` 约束与高德 live/fixture adapter；provider 失败会保存为 typed failure，不伪造推荐。

```powershell
Set-Location backend
uv run python -m scripts.run_minimal_planning_graph
```

默认命令完全离线，并明确关闭 LangSmith 上传。当前只返回带来源的必去景点候选，不做开放式推荐、排序、天气/路线汇合、预算分配或逐日排程。

## 10-case 可执行基线

[`docs/evaluation/planning-seed-baseline.md`](docs/evaluation/planning-seed-baseline.md) 固化 6 条 standard 和 4 条 hard 中国旅行请求，覆盖北京/上海/成都、亲子、老人、低预算、缺预算、缺房间数、未确认约束、未配置城市和 provider timeout。

```powershell
Set-Location backend
uv run python -m scripts.run_planning_seed_eval
```

当前机械报告为 10/10 cases、120/120 deterministic checks、6/6 candidate sources traceable。输入已经结构化且数据来自明确 fixture/scenario，因此这些数字不能写成行程准确率或 Agent 成功率；它们是后续增加 Agent 前的可重放基线。

## 首个 schema-constrained Agent

[`docs/agents/constraint-agent.md`](docs/agents/constraint-agent.md) 实现独立的 `propose_constraints → deterministic_normalizer` Agent 子图。DeepSeek 只提交原文证据和约束语义，不能直接设置 ID、source 或 confirmed；推断约束固定保持未确认并进入 HITL。

[`docs/evaluation/constraint-agent-baseline.md`](docs/evaluation/constraint-agent-baseline.md) 记录同一 10-case 上的真实 `deepseek-v4-pro` point-in-time 结果：9/10 exact cases，semantic precision/recall 均为 0.9375，confirmation accuracy 与 clarification case rate 均为 1.0000，共 10362 tokens，端到端 p50/p95 为 3166/4668 ms。唯一失败是老人“希望减少台阶”的 hard/soft 歧义；该失败被保留，没有为追求满分修改标签。

```powershell
Set-Location backend
uv run pytest tests/test_constraint_agent.py tests/test_constraint_agent_evaluation.py --no-cov
uv run python -m scripts.run_constraint_agent_eval --live
```

测试默认使用 fake model。`--live` 会产生 DeepSeek 用量并上传 LangSmith trace，不能在 CI 运行。当前 Agent 只替换已有 `TripRequest` 的 constraints slice，尚未接入产品 API、主 planning Graph 或完整行程生成。

## 单 Planner 基线

[`docs/agents/single-planner.md`](docs/agents/single-planner.md) 实现 `propose_schedule → deterministic_normalizer` 子图。模型只能为 provider 已返回的 candidate ID 提议 day、start time 和审计理由；代码要求每个候选恰好出现一次，并复制候选名称/来源、检查旅行日期和时间线，再组装只覆盖已有候选的部分 `DayPlan`。

[`docs/evaluation/single-planner-baseline.md`](docs/evaluation/single-planner-baseline.md) 记录同一 10-case 上的真实 `deepseek-v4-pro` point-in-time 结果：6 条候选就绪请求调用模型并生成部分 DayPlan，4 条请求按上游状态停止；10/10 路由与结构检查通过，6/6 candidates covered/grounded/source-traceable，共 6235 tokens，模型调用 p50/p95 为 2797/3968 ms。

```powershell
Set-Location backend
uv run pytest tests/test_single_planner.py tests/test_single_planner_evaluation.py --no-cov
uv run python -m scripts.run_single_planner_eval --live
```

这些指标验证候选 grounding、停止路由和契约，不是行程质量分数。该单 Planner 基线每条成功案例只有一个 fixture 必去候选，且自身没有营业时间、路线、天气、酒店或预算输入，因此输出不能称为完整旅行计划；预算与基础冲突由下一节的独立确定性 Validator 处理。

## Explore Agent

[`docs/agents/explore-agent.md`](docs/agents/explore-agent.md) 实现 `propose_queries → search_candidates → select_candidates → validate_selection` 子图。模型可以根据已确认约束和旅行风格设计景点/餐饮搜索策略，并且只能排序 Provider 返回的 candidate ID；代码负责候选事实、来源、跨查询去重和证据引用校验。

[`docs/evaluation/explore-agent-baseline.md`](docs/evaluation/explore-agent-baseline.md) 记录 6 条北京/上海/成都开发案例的真实 `deepseek-v4-pro` point-in-time 结果：6/6 cases、9/9 required query kinds、12/12 grounded/source-traceable/labelled-relevant recommendations、9/9 required recommendation groups，17679 tokens，单案例两次模型调用合计延迟 p50/p95 为 7408/7742 ms。

```powershell
Set-Location backend
uv run pytest tests/test_explore_agent.py tests/test_explore_agent_evaluation.py --no-cov
uv run python -m scripts.run_explore_agent_eval --live
```

候选目录是显式 fixture，且该 6-case 套件用于提示词开发后回归，不是未触碰 holdout；这些指标不能写成实时推荐准确率或泛化能力。Explore Agent 现已通过 specialist fan-out 和材料层进入 Product Graph V2 的路线、预算与排程阶段。

## Stay Agent

[`docs/agents/stay-agent.md`](docs/agents/stay-agent.md) 实现 `propose_queries → search_candidates → select_candidates → validate_selection` 子图。模型只提出住宿区域/关键词并排序 Provider 已返回的 candidate ID；代码在调用前检查城市与房间数、限定最小 `StaySearchProvider` 接口、保存来源/查询 lineage，并逐条核验证据。

[`docs/evaluation/stay-agent-baseline.md`](docs/evaluation/stay-agent-baseline.md) 记录 4 条可执行住宿案例和 2 条调用前阻断案例的真实 `deepseek-v4-pro` point-in-time 结果：6/6 cases，8 次模型调用，13/13 上下文引用，8/8 grounded/source-traceable/labelled-relevant recommendations，12321 tokens，模型调用合计延迟 p50/p95 为 7755/8184 ms。

```powershell
Set-Location backend
uv run pytest tests/test_stay_agent.py tests/test_stay_agent_evaluation.py --no-cov
uv run python -m scripts.run_stay_agent_eval --live
```

Stay Agent 不提供酒店价格或预订：高德住宿 POI 只用于候选位置/区域，输出价格字段为空、availability 为 `unknown`、booking 为 `false`。评测使用显式 fixture 目录并属于开发集回归，不是 OTA 数据质量、实时可订性或推荐准确率。Stay Agent 现已通过 specialist fan-out 进入 Product Graph V2，并作为路线矩阵的住宿位置锚点。

## 确定性预算与基础计划校验

[`docs/planning/deterministic-plan-validator.md`](docs/planning/deterministic-plan-validator.md) 实现普通代码 `validate_trip_plan(request, plan)`。它跨 `TripRequest` 与 `TripPlan` 检查请求 ID、目的地和日期一致性、重复 candidate ID、推荐来源模式，并用 `Decimal` 重新汇总预算范围内的 `CostItem`。

Validator 会区分费用下界已超预算、区间上界可能超支和预算类别缺失。缺失类别不会被静默当作 0 元；硬预算错误返回 typed `ValidationIssue` 并阻止 finalization，软预算超支返回 warning。没有预算的请求可以继续，但不会获得预算满足保证。

```powershell
Set-Location backend
uv run pytest tests/test_plan_validator.py --no-cov
uv run python -m scripts.export_plan_validation_example
```

提交的示例故意只含门票 `CostItem`，而请求预算还包含交通、餐饮和活动，因此报告稳定返回 `budget.incomplete_category_coverage`。这证明系统不会因为已知费用低于 3000 元就误称预算满足。

## 北京三日 Gate 2 纵向切片

[`docs/evaluation/beijing-three-day-gate2.md`](docs/evaluation/beijing-three-day-gate2.md) 把结构化请求、候选搜索、Single Planner、完整三日 `TripPlan` 组装与 Validator 接成首条端到端可重放路径。正常案例形成三个有 provider 来源的逐日景点并由 `CostItem` 重算 500 元 fixture 总额；硬预算案例保留相同必去候选和完整费用类别，以 900 元 fixture 下界对 300 元预算返回 600 元缺口并阻止 finalization。

```powershell
Set-Location backend
uv run python -m scripts.run_vertical_slice_eval
uv run pytest tests/test_vertical_slice.py --no-cov
```

提交报告为 2/2 cases、20/20 checks、6/6 candidate sources traceable、2/2 exact replays。这是结构、grounding、预算算术和失败语义的 Gate 2 证据；固定 Planner 提案、POI 与费用都是明确 fixture，不能写成模型行程准确率、实时价格或生产 SLA。

## 可恢复主编排与 HITL

[`docs/planning/stateful-checkpoint-hitl.md`](docs/planning/stateful-checkpoint-hitl.md) 在 Gate 2 外增加 `run_vertical_slice → prepare_human_review → interrupt → apply_review_decision` 主图；结构化修改决定会继续进入 `apply_plan_revision`。LangGraph 原生 `interrupt` 和 `Command(resume=...)` 配合 SQLite checkpoint，使 graph/runtime 关闭并重建后仍能从相同 `thread_id` 继续；内部候选搜索和 Planner 子图不会继承检查点，因此恢复与局部时间调整都不会重复调用 provider 或模型。

```powershell
Set-Location backend
uv run python -m scripts.run_checkpoint_hitl_eval
uv run pytest tests/test_stateful_planning.py --no-cov
```

原始 checkpoint 报告为 2/2 cases、20/20 checks、2/2 runtime reconstructions，恢复阶段 provider/model 调用均为 0。正常批准和冲突确认仍明确记录 v1→v1 零变更；结构化修改则校验 base version、目标/保护 scope，只改变一个目标日期并生成 v2。该段报告描述旧 Gate 2 checkpoint 基线；Product Graph V2 的 revision 已复用持久化路线/营业证据并重新执行 `hard-trip-plan-validator-v1`。EZ-407A 另用本地 SQLite 任务账本原子保存 API snapshot、连续 SSE 事件和已接受审核决定，使等待审核与完成态可跨后端重启恢复；重启时仍在 queued/running 的任务会转为可重试的 `planning-task-interrupted`，不会自动重放可能计费的模型或 Provider 阶段。两类 SQLite 都只是本地单实例恢复证据，不是生产级多进程并发、高可用、加密或无限期数据保留方案。

## Specialist 并行 fan-out

[`docs/planning/specialist-fanout.md`](docs/planning/specialist-fanout.md) 实现 `compile_context → [Explore Agent, Stay Agent, Weather tool] → merge_specialists`。三个分支各写一个 reducer 累积结果，merge 强制恰好一个有序结果；单分支超时转换为 typed partial failure，其他结果不会被覆盖。天气是主动 Provider 查询而非额外 LLM Agent，eligible 请求无需包含天气提示词。

[`docs/evaluation/specialist-fanout-baseline.md`](docs/evaluation/specialist-fanout-baseline.md) 冻结 5 条 complete/partial/blocked 案例。2026-08-21 的 DeepSeek/LangSmith 点时报告为 5/5 cases、15/15 分支状态、2/2 类型化 Provider 故障、4/4 非故障分支保留；13 次模型调用都有 usage 记录，共 18857 tokens，fan-out p50/p95 为 7044/8984 ms。

```powershell
Set-Location backend
uv run pytest tests/test_specialist_fanout.py tests/test_specialist_fanout_eval.py --no-cov
uv run python -m scripts.run_specialist_fanout_eval
uv run python -m scripts.run_specialist_fanout_eval --live
```

离线与 live 评测都使用显式 fixture Provider；`--live` 只把 Explore/Stay 模型替换为 DeepSeek 并上传 LangSmith trace。这证明并发、合并、降级、成本记录与恢复契约，不证明实时高德效果或多 Agent 已提升最终行程质量。

## 路线矩阵与预算材料层

[`docs/planning/route-budget-materials.md`](docs/planning/route-budget-materials.md) 把 specialist 信息包收敛成最多 4 个 POI 与 1 个住宿锚点，并生成最多 20 条有向公交路线。默认并发上限为 4；单条 Provider timeout 保留为 typed partial failure，城市能力被阻断时路线调用固定为 0。`budget-allocator-v1` 则用 `Decimal`、版本化类别权重和间夜/人次/天数尺度生成目标 envelope，不让模型做金额算术或猜测缺失房间数。

[`docs/evaluation/route-budget-materials-baseline.md`](docs/evaluation/route-budget-materials-baseline.md) 的可重放 fixture 套件为 5/5 cases、42/42 路线边、1/1 类型化单边超时、2/2 预算精确回总额、1/1 blocked case 零路线调用，并且 5/5 cases 遵守并发上限。

```powershell
Set-Location backend
uv run pytest tests/test_planning_materials.py tests/test_planning_material_eval.py --no-cov
uv run python -m scripts.run_planning_material_eval
```

该层没有新增 LLM 调用，提交指标不是模型准确率。路线 fixture 不代表当前高德实时耗时，预算目标也不是价格或可行性保证；输出仍是最终 Plan Agent 之前的材料，而不是完整 `TripPlan`。

## 多 Agent Plan Agent

[`docs/agents/plan-agent.md`](docs/agents/plan-agent.md) 实现 `propose_schedule → normalize_schedule` 子图。模型只能为 planning shortlist 中的 POI 提议日期、时间和理由；代码要求每个候选恰好一次，回填候选名称/来源、住宿锚点路线与相邻路线、逐日天气风险、完整日期和稳定 ID，最后复用 deterministic Validator 生成同构 `TripPlan` 草案。材料为 partial/blocked 时返回 typed skip，模型调用固定为 0。

[`docs/evaluation/plan-agent-baseline.md`](docs/evaluation/plan-agent-baseline.md) 冻结北京、上海、成都 4 条可规划案例与 2 条停止路由案例。fixture 与 2026-08-21 的 `deepseek-v4-pro`/LangSmith 点时报告均为 6/6 cases；真实模型路径 12/12 candidates covered/grounded/source-traceable/route-backed，天气保留率 1.0000，2/2 非就绪案例零模型调用，共 9067 tokens，模型延迟 p50/p95 为 3457/3800 ms。

```powershell
Set-Location backend
uv run python -m scripts.export_plan_agent_schemas
uv run python -m scripts.run_plan_agent_eval
uv run python -m scripts.run_plan_agent_eval --live
uv run pytest tests/test_plan_agent.py tests/test_plan_agent_evaluation.py --no-cov
```

预算 allocation 仍是目标 envelope，不是价格；Plan Agent 不会据此制造 `CostItem` 或宣称预算满足。住宿候选当前仅作路线锚点，不代表价格、库存或预订。Plan Agent 的 1.0000 指标只是 grounding/lineage 回归，不是行程主观质量准确率或生产 SLA；其草案由下一节独立 Hard Validator 再做定稿门禁。

## Hard Validators

[`docs/planning/hard-validators.md`](docs/planning/hard-validators.md) 实现零模型调用的 `validate_hard_trip_plan(request, plan, materials, opening_hours)`。它在基础 Validator 之上机械检查：已确认 hard must/avoid、shortlist 精确覆盖、候选与路线来源血缘、POI/住宿跨城、住宿锚点与相邻活动路线、转场时间窗、带 Provider 来源的营业时间窗口，以及硬预算是否拥有完整价格事实。

每个错误都输出稳定 `ValidationIssue`、`responsible_node` 和 `repair_action`。例如缺路线归给 Route，转场或营业窗口冲突归给 Plan，跨城 POI/住宿分别归给 Explore/Stay；没有营业时间证据不会被当作“全天开放”。

```powershell
Set-Location backend
uv run python -m scripts.export_hard_validator_schemas
uv run python -m scripts.run_hard_validator_eval
uv run pytest tests/test_hard_validator.py tests/test_hard_validator_evaluation.py --no-cov
```

提交的 fixture 报告记录 12/12 cases、22/22 预期 issue 责任路由、12/12 确定性重放和 0 Validator 模型调用。这些数字证明规则和责任标注契约，不代表实时营业时间正确、价格完整或自动修复成功；下一节的 Repair Router 独立验证执行闭环。

## Repair Router

[`docs/planning/repair-router.md`](docs/planning/repair-router.md) 实现零模型调用的 `run_repair_router(...)`：只处理 hard error，按 Constraint → Explore → Stay → Route → Budget → Plan 的依赖顺序选择 typed `repair_action`，同类最多执行两次，并在每次修复后重新运行 Hard Validator。

Router 要求 executor 明确上报实际执行与复用节点，并比较七个 pipeline 产物的语义指纹。Route 修复若偷偷改写 Stay，或失败尝试返回部分状态，都会被协议错误拒绝。`ASK_USER` 在调用任何 Agent 前进入 HITL；warning 不会触发自动循环。

```powershell
Set-Location backend
uv run python -m scripts.export_repair_router_schemas
uv run python -m scripts.run_repair_router_eval
uv run pytest tests/test_repair_router.py tests/test_repair_router_evaluation.py --no-cov
```

提交的隔离 fixture 报告记录 9/9 cases、9/9 exact action + executed-node routes、9/9 两次重试上限、9/9 未受影响节点复用和 9/9 确定性重放。Router 自身 0 次模型调用；该套件的 fixture executor 只模拟责任节点产物，因此这些数字仍只是编排契约回归。

Product Graph V2 另接入真实产品 executor：`rerun_explore`、`rerun_stay` 会调用对应 Agent，再按依赖重建路线与 Plan；`rerun_route`、`recalculate_budget` 和 `replan_day` 分别只执行允许的下游责任链。默认北京 fixture 特意让天坛 09:00 草案与 10:00 开放证据冲突，Router 仅执行 Plan 的确定性同日修复并把天坛移到 10:00，Explore/Stay/Weather/Route/Budget 全部复用，模型和 Provider 调用均为 0。其他定向分支由产品 executor 单测覆盖；这不等同于 live 自动修复成功率。

## Weather Repair Coordinator

[`docs/planning/weather-repair.md`](docs/planning/weather-repair.md) 实现 `run_weather_repair(...)`：最新 Weather Provider 风险无需用户追加天气文本，即可按 severity、aware datetime 重叠和候选活动环境定位受影响 item，并主动创建只包含风险、日期和 item ID 的局部重规划任务。

Coordinator 自身零模型调用。执行器候选方案必须消除原风险，通过作用域保护和 Hard Validator；同日时间调整等 minor 方案可自动采用，跨日移动、候选集合变化或多日变化标记为 major，只暴露 `pending_confirmation` 方案并保留原计划。失败、越界改写或无效方案最多尝试两次。

```powershell
Set-Location backend
uv run python -m scripts.export_weather_repair_schemas
uv run python -m scripts.run_weather_repair_eval
uv run pytest tests/test_weather_repair.py tests/test_weather_repair_evaluation.py --no-cov
```

提交的 fixture 报告记录 10/10 cases、5/5 非影响场景零误触发、5 个 Provider 风险主动任务、1 个 minor 自动采用、1 个 major HITL、3 个有界双重试场景、风险来源追踪率 1.0000 和 10/10 确定性重放。该报告证明触发与安全契约，不代表实时天气准确率或真实重规划成功率。

## AMap live contract probe

[`docs/probes/amap-mcp-live-probe-2026-08-20.md`](docs/probes/amap-mcp-live-probe-2026-08-20.md) 记录一次固定北京真实探针：官方 MCP 当前发现 15 个工具，并验证 POI、天气、距离、步行和公交响应；版本化 fixture 只保留字段白名单并执行凭据/PII 脱敏。CI 回放 fixture，不读取 Key 或访问高德。

真实探针会消耗少量高德配额，必须在本地配置 Key 并显式确认：

```powershell
Set-Location backend
uv run python -m scripts.run_amap_mcp_probe --live --write-fixture
```

这只证明 provider 字段可用性和已知边界，不表示 Agent 或地图搜索产品功能已经实现。

## AMap typed provider adapter

[`docs/providers/amap-provider-contract.md`](docs/providers/amap-provider-contract.md) 说明业务层如何通过同一 port 使用 live MCP/REST 与离线 fixture。adapter 当前把高德结果归一化成 `CandidatePOI`、`WeatherRisk` 和 `RouteLeg`，并统一处理来源时间、响应哈希、字段漂移、超时、限流与认证失败。

默认 fixture smoke 不联网：

```powershell
Set-Location backend
uv run python -m scripts.run_amap_provider_smoke
```

显式 `--live` 才会读取本地 Key 并消耗少量高德配额：

```powershell
uv run python -m scripts.run_amap_provider_smoke --live
```

天气风险由 provider 数据触发并归一化为结构化 severity、时间范围和受影响活动类型，不需要用户先追加“第二天下雨”。Product Graph V2 会在每次规划请求内主动运行天气分支并把风险交给 Plan Agent；本项目定位为规划器，因此不建设定时 WeatherWatch 或通知中心。Weather Repair Coordinator 已能在隔离场景生成局部候选方案，但尚未接入产品总图。

## Product Graph V2

产品任务执行器现在运行 `run_specialists → build_materials → run_plan_agent → validate_hard_plan → [run_repair] → prepare_human_review → interrupt`。Explore、Stay 和零模型天气工具并行完成；材料层随后构造有界路线矩阵和预算目标；Plan Agent 只能安排 shortlist candidate ID，代码回填名称、来源和路线；Hard Validator 再检查硬约束、候选覆盖、路线血缘、营业时间证据与预算事实。只有存在可自动修复的 hard error 才提交 `run_repair`，warning 直接保留给审核界面。

fixture 模式使用明确标记的北京合成数据：首日中雨由天气 Provider 主动返回，混合型故宫安排在首日，户外天坛安排到次日。fixture 住宿只充当位置锚点，不包含价格、房态或预订能力。live 模式使用 DeepSeek/LangSmith 与高德官方 MCP/REST adapter；由于当前高德 V1 contract 没有稳定的 typed 营业时间字段，live 结果会由 Hard Validator 显式报告证据缺失，而不会伪造营业时间。

结构化 `shift_day_later` 修改从同一 checkpoint 恢复，不重跑外部 Provider 或模型，并基于已持久化的材料和营业证据重新执行 Hard Validator。Repair Router 已接入首次草案定稿前的产品链；Weather Repair、中文 Constraint Agent 入口和开放式增删景点仍未接入这条产品图。

## 30-case system comparison 协议

[`docs/evaluation/system-comparison-protocol.md`](docs/evaluation/system-comparison-protocol.md) 冻结 20 standard + 10 hard case，并规定 `single_agent_tools`、`product_graph_no_hard_gate`、`product_graph_bounded_repair` 三组必须共享请求、Provider fixture、完整 `TripPlan` 契约、post-run evaluator 和 live 模型名称。旧 Single Planner 只输出部分 `DayPlan`，不会被错误复用为公平对照。

三组 fixture runner 已共享冻结请求、工具快照、完整 `TripPlan` 和 `hard-trip-plan-validator-v1`。EZ-406A.2 让部分路线失败的材料继续生成安全草案后，当前结果为 Single Agent 5/29、无 Hard Gate Product Graph 5/29、完整 Product Graph 21/29；后两组配对仍为 16 个改善、0 个恶化，即 `+55.17` 个百分点。这只证明 Hard Validator 与 bounded Repair 在该开发集故障注入上的恢复价值。Single 与无 Gate Product 没有差异，所以这份报告不能用于声称 Specialist Agent 提升了模型质量，更不能当作真实用户成功率。报告未调用 DeepSeek、高德或 LangSmith。

```powershell
Set-Location backend
uv run python -m scripts.run_system_comparison_eval
uv run pytest tests/test_comparison_evaluation_contract.py tests/test_system_comparison_evaluation.py --no-cov
```

3-case × 2 repeated-live pilot 已在冻结 Provider catalogs 上完成。42/42 次 DeepSeek 调用成功，三个 arm 都是 6/6 finalizable，因此没有 finalization lift；Product 重复计划一致性为 3/3，Single 为 2/3，同时 Product logical calls 为 30、Single 为 12。结果、token、累计模型延迟与限制见 [`docs/evaluation/live-comparison-pilot-result.md`](docs/evaluation/live-comparison-pilot-result.md)。真实刷新仍要求显式 opt-in，不进入 CI：

```powershell
Set-Location backend
uv run python -m scripts.plan_live_system_comparison
uv run python -m scripts.run_live_system_comparison_pilot --live --confirm-max-model-calls 54
```

## 当前边界

- 不提供订票、订房、支付或实时房价；
- 当前健康页不是旅行规划产品完成度；
- 当前三节点探针只证明观测链路可接入，不证明模型规划质量；
- Request Intake 与 Constraint Agent 已接入 Web 确认入口，但 fixture 解析只覆盖冻结的中文场景，live 抽取路径尚未执行本轮 canary；确认前的 Request Intake 草案仍保存在单进程内存，刷新后需要重新理解需求；确认并创建任务后，URL、snapshot、SSE 与审核幂等状态可跨刷新和单实例后端重启恢复；
- Product Graph V2 已在产品 API 中连通 Explore、Stay、主动 Weather、路线/预算材料、Plan Agent、Hard Validator、真实责任节点 Repair Router、checkpoint HITL 和结构化 revision；Weather Repair Coordinator 仍是独立能力，定时 WeatherWatch 已从计划中取消；
- 高德 live fixture 是 2026-08-20 的点时样本，不是当前天气、实时酒店价格或生产 SLA；
- 后续功能必须通过真实 provider contract、固定评测和可回放 trace 验证后再写入项目成果。
