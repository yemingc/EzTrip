# Single Planner V1

EZ-102 建立一个可与后续多 Agent 系统比较的单 Planner 基线。它接收已经编译的 `PlannerContext` 和上游 provider 返回的 `CandidatePOI`，只负责把现有候选放入旅行日期；它不搜索新景点，也不生成完整 `TripPlan`。

## 子图与职责边界

```text
PlannerContext + provider candidates
              |
              v
       propose_schedule (DeepSeek)
              |
              v
 deterministic_normalizer
              |
              v
 partial DayPlan[] + auditable decisions
```

模型的 forced-tool schema 只允许四个字段：

- `candidate_id`：必须来自输入候选；
- `day_number`：必须落在旅行日期范围；
- `start_time`：08:00–21:30 的整点或半点；
- `reason`：模型的排程理由，只保存在 decision 中供审计。

模型不能提交标题、provider 来源、结束时间、稳定 item ID 或费用。确定性 normalizer 会：

1. 要求输入候选和提案 candidate ID 集合完全一致，拒绝新增、遗漏和重复；
2. 从 `CandidatePOI` 复制 title 与 `SourceReference`，不接受模型改写；
3. 把 `day_number` 映射到 `PlannerContext` 的真实日期；
4. 使用候选建议时长；缺失时明确采用 V1 固定 120 分钟草案；
5. 生成稳定 item ID，并由 `DayPlan` 契约拒绝跨日、乱序和重叠时间线。

## 为什么只输出部分 DayPlan

当前 candidate search 只查询已确认的 `must_visit`，10 条 planning-seed 中有 6 条候选就绪，且每条只有一个候选。为了不把“待补充活动”伪装成完整行程，V1 只返回实际安排了候选的日期。没有候选、需要澄清或 provider 失败的请求不会调用 Planner 模型。

因此当前可以说“实现 provider-grounded 的单 Planner 候选放置基线”，不能说“已经生成完整 2–3 天游程”。完整日覆盖、开放式推荐、营业时间、路线、天气、住宿和预算可行性仍需后续节点与确定性校验器。

## 运行

离线测试：

```powershell
Set-Location backend
uv run pytest tests/test_single_planner.py tests/test_single_planner_evaluation.py --no-cov
```

真实模型基线：

```powershell
uv run python -m scripts.run_single_planner_eval --live
```

`--live` 会产生 DeepSeek 用量并上传 LangSmith trace；CI 只使用 fake model。当前子图由评测 runner 接在 minimal planning Graph 之后，尚未暴露为 FastAPI 或产品主链。
