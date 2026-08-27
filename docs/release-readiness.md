# Release readiness

## 结论

EzTrip 在 `3060834` 上完成的本地单实例发布基线为 **CLOSED**。Budget Estimate V2 在 `fa1f0e2` 上的本地增量技术门禁、用户浏览器验收以及 [PR #50](https://github.com/yemingc/EzTrip/pull/50) 的远端 CI 均为 **PASSED**；PR #50 已于 2026-08-27 通过 merge commit `0a9edd3` 合并到 `main`。本结论只针对本地作品集与单用户演示，不把项目描述为生产级预订系统、全国城市质量保证或高可用服务。

## 2026-08-25 基线验证环境

- Windows / PowerShell
- Python 3.12.11，uv 0.8.15
- Node.js 22.19.0，pnpm 11.19.0
- Docker Engine 29.4.3，PostgreSQL 17 Alpine
- Chromium Playwright，单 worker

## 基线自动门禁结果（`3060834`）

| 门禁 | 结果 |
| --- | --- |
| Backend lock / Ruff / format / Mypy | 通过；Mypy 覆盖 `app scripts` |
| Backend tests | 441/441 通过，app branch coverage 85% |
| Python dependency audit | `pip-audit`：0 个已知漏洞 |
| Frontend install / lint / typecheck / build | 通过，使用 frozen pnpm lockfile |
| Node dependency audit | `pnpm audit --audit-level high`：0 个已知漏洞 |
| Product browser E2E | 16/16 通过；真实 fixture FastAPI + SSE + Chromium |
| Live browser acceptance | 泉州 opt-in canary 1/1 通过；默认无开关时 1 skipped |
| PostgreSQL migration | 无持久卷临时 PostgreSQL 17 从空库升级到 `0001_baseline (head)` |
| Tracked secret review | 只发现测试占位值、文档省略号与 `planning-task-*` 正则误报；未发现提交的真实 Key |

CI 同步执行 Python/Node 依赖审计。pytest 已从存在 `PYSEC-2026-1845` 的 8.4.2 升级到已修复的 9.1.1，并通过全量回归。

## 2026-08-27 Budget Estimate V2 增量门禁（`fa1f0e2`）

本轮只验证 `250e89a` 与 `fa1f0e2` 引入的行程关联预算估算及其当前回归状态。预算金额来自版本化 fixture/reference 区间和确定性聚合，会随住宿晚数、排程景点、路线矩阵、人数、天数及结构化修订重新计算；它不是实时房价、票价、成交价或预订报价。

| 门禁 | 当前结果 |
| --- | --- |
| Checkout | `codex/budget-estimate-v2`，提交 `fa1f0e2`；工作区在验证前干净 |
| Backend lock / Ruff / format / Mypy | 通过；Mypy 覆盖 `app scripts` |
| Backend tests | 453/453 通过，app branch coverage 85%；保留 2 条上游弃用 warning |
| Python dependency audit | `pip-audit`：0 个已知漏洞 |
| Frontend lint / typecheck / build | 通过；Next.js 16.3.1 production build 成功 |
| Node dependency audit | `pnpm audit --audit-level high`：0 个已知漏洞 |
| Product browser E2E | 22/22 通过；隔离冷启动 fixture FastAPI + SSE + Chromium，覆盖需求确认、首个进度事件、审核、刷新恢复、结构化 v2 修订和 390px 视口 |
| 当前开发服务 | `GET /api/health`、首页与 `/docs` 均返回 HTTP 200 |
| Remote branch / PR / CI | PASSED / MERGED；PR #50 的 Backend、Frontend 与 Product browser E2E 三项 CI 均通过，并已合并到 `main` |
| User browser acceptance | PASSED；用户于 2026-08-27 明确回复“测试通过”。这是用户验收声明，不把未报告的具体场景推断为已验证 |

隔离浏览器复核为避免复用开发中的 `3000/8000` 服务，在本机测试工件目录使用 `3100/8100`、独立 SQLite 任务账本与 checkpoint；临时配置和截图均未写入仓库。它不调用 DeepSeek、实时高德或 live canary。随后 PR #50 在 GitHub 托管的 clean checkout 中完成锁文件安装、Backend/Frontend 门禁、22/22 Product browser E2E 和 PostgreSQL 17 空库 Alembic 迁移。live canary 与实时 Provider 刷新没有在本轮重跑，不能沿用 2026-08-25 的点时结果冒充当前市场或 Provider 质量。

## 配置加固

- `DEEPSEEK_API_KEY`、`LANGSMITH_API_KEY`、`AMAP_MAPS_API_KEY` 的空字符串或纯空白现在统一归一化为 `None`；复制 `.env.example` 后不会把长度为 0 的 SecretStr 误判为已配置凭据。
- `.env.example` 默认 `LANGSMITH_TRACING=false`；只有配置有效 Key 并主动开启后才上传 trace。
- AMap endpoint 继续要求 HTTPS 且拒绝查询参数中的 Key；真实凭据只存在于未跟踪 `.env`。
- live 浏览器 canary 与 CI 隔离，需要显式设置 `EZTRIP_RUN_LIVE_BROWSER_CANARY=1`，并会消耗 DeepSeek/高德配额。

## 2026-08-25 基线 clean-checkout 复现

发布提交使用独立、无项目 `.env`、无既有 `node_modules`、`.venv`、`.next` 或 `tmp` 状态的 Git worktree 复现以下路径：

1. `uv sync --locked --all-groups`；
2. 根 `.env.example` 安全加载测试与 `pip-audit`；
3. `pnpm install --frozen-lockfile`、production build；
4. `pnpm test:e2e` 启动真实 fixture FastAPI/SSE 与 Chromium，16/16；
5. 后端 `GET /api/health` 返回 200；
6. 临时 PostgreSQL 17 空库执行 `alembic upgrade head`。

## 已知边界

- SQLite 任务账本与 LangGraph checkpoint 只保证本地单实例；没有多 worker 协调、账号、加密、保留期、高可用或外部副作用 exactly-once。
- 确认前 Request Intake 草案仍在进程内存；任务创建后才具有 URL/SQLite 恢复。
- 两条第三方弃用 warning 来自 LangSmith 与 Starlette/httpx 兼容层，当前测试通过；它们不是本项目代码错误，但后续依赖升级需要复核。
- PostgreSQL 当前只有迁移基线；产品任务耐久状态使用本地 SQLite，不应把 Alembic 通过写成分布式持久化能力。
- 一次泉州 live canary 与同轮一次 Provider 空结果只证明点时链路与诚实降级，不证明全国成功率、实时价格、营业时间、房态或生产 SLA。

## 停止规则

Budget Estimate V2 的本地技术门禁、用户浏览器验收与 PR #50 远端 CI 均已通过并合并到 `main`，不再为 V1 新增 Agent、城市样本或旅行功能。下一阶段整理可核验的架构说明、60–90 秒演示脚本、截图与简历/面试材料。任何能力数字必须链接到当前报告、fixture 评测或 live canary 证据。
