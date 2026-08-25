# 泉州 live 浏览器 canary（2026-08-25）

EZ-406C 用真实浏览器、真实 FastAPI/SSE、DeepSeek 与高德 Provider 跑通了一次中文自然语言到持久化 PlanVersion 的产品链路。机器可读摘要位于 [`evals/reports/live-browser-canary-2026-08-25.v1.json`](../../evals/reports/live-browser-canary-2026-08-25.v1.json)。报告不保存 API Key、原始模型回答、原始 Provider payload 或 LangSmith URL。

## 显式运行方式

该测试不进入默认 CI。只有显式设置开关后才会启动真实调用，并消耗模型与高德配额：

```powershell
Set-Location frontend
$env:EZTRIP_RUN_LIVE_BROWSER_CANARY = "1"
try {
  pnpm test:live-canary
} finally {
  Remove-Item Env:EZTRIP_RUN_LIVE_BROWSER_CANARY -ErrorAction SilentlyContinue
}
```

测试固定为 1 个 Chromium worker，输入“上海出发、1 名成人与 2 位老人、泉州 2 日、预算 3000 元、古建筑与闽南美食、轻松少走路”，并验证：

1. live Request Intake 能从中文原文提出字段、保留 evidence，并把“泉州”解析为 `350500`；
2. 创建的任务进入 URL，SSE 收敛到已保存草案或带稳定错误码的诚实降级；
3. 成功草案必须进入人工审核、保存 PlanVersion，并在浏览器刷新后恢复；
4. Provider 无法提供事实时必须显示可行动失败，不能改写成 fixture 或由模型补事实；
5. 从确认规划到成功草案或失败终态不得超过 120 秒。

## 本次成功观察

| 项目 | 实测值 |
| --- | --- |
| 城市 | 泉州市 / `350500` |
| 数据模式 | `live` |
| 浏览器全链路耗时 | 50 秒 |
| 确认后规划耗时 | 39 秒 |
| 任务终态 | `succeeded` |
| SSE 事件 | 12 |
| PlanVersion | 1 个 v1，2 日 |
| Specialist | Explore / Stay / Weather 均 `succeeded` |
| 路线矩阵 | 3/4 成功，1 条明确失败 |
| 校验终态 | `conflicted`，`can_finalize=false` |
| 人工决定 | `acknowledge_conflict`（保留待验证草案） |
| 恢复 | 审核后刷新 URL，仍显示审核完成 |

Hard Validator 没有把不完整材料包装成可定稿结果，而是保留三类缺口：`budget.incomplete_category_coverage`、`route.missing_for_grounded_item`、`opening_hours.evidence_missing`。

## 同轮保留的负结果

较早一次相同城市 canary 在 30 秒内因未取得任何可核验景点事实而结束：`planning-materials-blocked`、`retryable=true`、0 个 PlanVersion。浏览器显示“任务没有完成”，重启后端后仍可从 SQLite 任务账本读取相同失败快照。

后续重试取得了 live POI 并完成上述成功闭环。这说明产品的失败语义与恢复路径可用，也说明外部 Provider 结果存在点时波动；不能把一次成功重试写成稳定 SLA。

## 可声称与不可声称

本证据只支持：在 2026-08-25 的一次显式 opt-in 运行中，泉州 2 日中文请求能够到达真实 live Product Graph，保存带来源的 v1 草案，完成冲突确认并跨刷新恢复；同轮 Provider 空结果会明确、可重试地降级。

它不证明全国城市质量、行程准确率、实时价格/房态/营业时间、用户满意度、可用性比例或生产 SLA。北京、上海、成都的 fixture 回归仍与本次 live canary 分开表述。
