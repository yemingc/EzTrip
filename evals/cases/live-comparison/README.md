# Live comparison pilot cases

`suite.v1.json` 冻结 EZ-502 repeated-live pilot 的 3 个开发案例、2 次重复、公平性不变量和模型调用/token 硬预算。

它引用既有 Plan/Explore/Stay development fixtures，数据角色是 `repeated_development_pilot`，不是 blind holdout。预检与未来 runner 计算的 dataset SHA-256 同时覆盖 suite 及其引用库存；任何输入事实变化都会改变实验身份。

当前只有零外部调用的协议与 preflight，没有提交 live 结果。执行边界见 [`docs/evaluation/live-comparison-pilot-protocol.md`](../../../docs/evaluation/live-comparison-pilot-protocol.md)。
