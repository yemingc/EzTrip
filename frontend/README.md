# EzTrip frontend

Next.js App Router 产品工作台。当前支持创建北京两日 fixture 规划任务、消费真实 SSE 进度、展示逐日草案、确定性预算校验、景点坐标与来源账本，并把人工批准、确认冲突、请求修改或取消提交给后端恢复同一个 LangGraph checkpoint。

当前边界：旅行原文会进入 `TripRequest.raw_text`，但已确认的结构化控件才驱动工作流，界面不声称已经完成中文抽取；坐标视图没有真实地图底图；空天气与费用结果保持“未知”，不解释为天气良好或零费用。当前四类审核动作只结束本轮审核，不触发预订；`request_revision` 会记录评论和 `revision_requested`，但不会伪装成已经生成 v2，结构化修改契约与局部重算属于下一增量。

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

Playwright 测试运行真实 fixture FastAPI + SSE 链路，覆盖正常草案批准、硬预算但费用事实缺失后的冲突确认、修改请求不伪造新版本，以及 390px 移动端视口。fixture 路径不会调用 DeepSeek 或实时高德。
