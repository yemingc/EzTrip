# EzTrip frontend

Next.js App Router 产品工作台。当前支持自由填写国内单目的地、2–5 天行程和 fixture/live 数据模式，先调用服务端 City Resolver 展示规范城市、行政区代码与来源；重名行政区必须先选择候选。任务创建后消费真实 SSE 进度，展示 Product Graph V2 的 Explore/Stay/Weather 分支、路线/预算材料、Plan Agent、Hard Validator、有界 Repair trace、逐日草案、景点坐标与来源账本，并把人工批准、确认冲突、结构化局部修改或取消提交给后端恢复同一个 LangGraph checkpoint。

当前边界：旅行原文会进入 `TripRequest.raw_text`，但目的地、日期、人数和预算仍由已确认的结构化控件驱动，界面不声称已经完成中文 Request Intake；北京、上海、成都只表示无 Key 的 fixture 覆盖，其他国内城市需要选择 live 模式并由服务端提供有效高德/模型凭据。选择 live 本身就是该请求的显式启用，不再要求修改额外环境开关；“可输入”不等于所有城市质量均已验证，也不支持一次行程跨多个城市。坐标视图没有真实地图底图；住宿只显示位置锚点，价格、房态与预订均未验证；费用缺失不解释为零费用。天气工具在每次规划请求内主动执行并影响排程，不做后台定时提醒。首次草案的可修复 hard error 会进入有界 Repair Router，界面展示真实执行/复用节点和调用计数。`request_revision` 必须明确选择目标日期和延后 60/90/120 分钟，服务端保护其他日期、候选、费用与来源，重新执行 Hard Validator，生成 v2 后明确标记“尚未再次审核”。它不是自由文本重规划，revision 也不会自动重入 Repair Router。

## 本地启动

```powershell
Copy-Item .env.example .env.local
pnpm install
pnpm dev
```

浏览器访问 `http://localhost:3000`。

## 质量检查

```powershell
pnpm lint
pnpm typecheck
pnpm build
pnpm exec playwright install chromium
pnpm test:e2e
```

Playwright 测试运行真实 fixture FastAPI + SSE 链路，覆盖北京默认规划、上海三日规划、重名行政区确认、fixture 不支持城市的诚实提示、live 选择即显式启用的界面语义、营业时间冲突的确定性 Plan 修复、正常草案批准、软预算目标下费用事实缺失提醒、第二天整体延后并生成 v2，以及 390px 移动端视口。fixture 路径不会调用 DeepSeek 或实时高德。
