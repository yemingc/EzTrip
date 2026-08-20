# ADR-001：使用 LangGraph 显式状态图作为工作流骨架

- 状态：Accepted
- 日期：2026-08-20

## 背景

旅行规划同时包含语义理解、外部数据查询、预算与时间计算、硬约束校验、失败修复和用户确认。若多个角色只通过自然语言互相对话，状态、分支条件和失败责任很难稳定复现，也无法证明多 Agent 比单次生成更可靠。

## 候选方案

1. 单 Prompt 一次生成完整攻略；
2. 多角色自由对话后由 Manager 汇总；
3. LangGraph 显式状态图，节点只读写 typed state，确定性 edge 控制执行与修复范围。

## 决策

采用方案 3。LangGraph 负责共享状态、条件分支、并行、checkpoint 和 HITL 中断；Agent 只是图中的一种节点，不拥有隐式的全局状态。只有互不依赖的候选搜索可以并行，预算、时间、距离和硬约束结果必须在汇合后由普通代码校验。

Gate 0 三节点探针只验证 `model → fixture tool → model finalizer` 的 trace 层级，不代表最终旅行图已经实现。

## 后果

- 优点：执行轨迹可回放；失败可定位到节点；可对单 Agent 与多 Agent 做消融；局部修复不必重跑整图。
- 代价：需要维护 state schema、reducer、节点幂等性和迁移策略；简单需求的框架开销高于一次模型调用。
- 约束：不因使用 LangGraph 就宣称多 Agent 有收益，必须用固定 cases、trace、延迟和成本数据验证。

## 重新评估条件

若 10-case 基线显示状态图没有带来可解释的约束或恢复收益，删除无收益 Agent，保留更小的确定性 workflow。

## 参考

- [LangGraph custom workflows](https://docs.langchain.com/oss/python/langchain/multi-agent/custom-workflow)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
