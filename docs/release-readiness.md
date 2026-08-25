# Release readiness

## 结论

EzTrip 的本地单实例技术发布门禁为 **CLOSED**：冻结功能后的锁文件、静态检查、全量测试、真实 fixture 浏览器链路、依赖漏洞、空白凭据、临时 PostgreSQL/Alembic 与 clean-checkout 安装/启动均纳入复核。本结论只针对本地作品集与单用户演示，不把项目描述为生产级预订系统、全国城市质量保证或高可用服务。

## 2026-08-25 验证环境

- Windows / PowerShell
- Python 3.12.11，uv 0.8.15
- Node.js 22.19.0，pnpm 11.19.0
- Docker Engine 29.4.3，PostgreSQL 17 Alpine
- Chromium Playwright，单 worker

## 自动门禁结果

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

## 配置加固

- `DEEPSEEK_API_KEY`、`LANGSMITH_API_KEY`、`AMAP_MAPS_API_KEY` 的空字符串或纯空白现在统一归一化为 `None`；复制 `.env.example` 后不会把长度为 0 的 SecretStr 误判为已配置凭据。
- `.env.example` 默认 `LANGSMITH_TRACING=false`；只有配置有效 Key 并主动开启后才上传 trace。
- AMap endpoint 继续要求 HTTPS 且拒绝查询参数中的 Key；真实凭据只存在于未跟踪 `.env`。
- live 浏览器 canary 与 CI 隔离，需要显式设置 `EZTRIP_RUN_LIVE_BROWSER_CANARY=1`，并会消耗 DeepSeek/高德配额。

## Clean-checkout 复现

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

技术发布门禁关闭后，不再为 V1 新增 Agent、城市样本或旅行功能。下一阶段只整理可核验的架构说明、60–90 秒演示脚本、截图与简历/面试材料；任何能力数字必须链接到当前报告、fixture 评测或 live canary 证据。
