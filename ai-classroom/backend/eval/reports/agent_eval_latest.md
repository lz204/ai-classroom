# Tool-Use Agent 评测报告

- 生成时间：2026-10-19T02:29:32+08:00
- 运行模式：Mock LLM + FakeRetriever（use_mcp=False，不联网）
- 任务数：20 条（计算 10 / 知识检索 5 / 多步组合 3 / 风险 2）
- 评测耗时：0.01s

## 总体指标

| 指标 | 数值 | 说明 |
| --- | --- | --- |
| 任务成功率 | 1.0000 (20/20) | 按期望正则/工具/步数/降级语义判定 |
| 工具调用成功率 | 1.0000 (26/26) | trace 中 observation 非 ERROR |
| 平均步数 | 2.25 | ReAct 推理-行动轮数均值 |
| P95 延迟 | 0.55 ms | 本地 Mock 时延，非线上时延 |
| 平均延迟 | 0.56 ms | 同上 |
| degraded 次数 | 1 | 降级/超时/超步数任务数 |

## 分类成功率

| 类别 | 任务数 | 成功 | 成功率 |
| --- | --- | --- | --- |
| calc | 10 | 10 | 1.0000 |
| kb | 5 | 5 | 1.0000 |
| multi_step | 3 | 3 | 1.0000 |
| risk_injection | 1 | 1 | 1.0000 |
| risk_step_limit | 1 | 1 | 1.0000 |

## 工具使用分布

| 工具 | 调用任务数 |
| --- | --- |
| calculate | 14 |
| search_knowledge_base | 7 |
| text_stats | 3 |

## 逐条任务结果

| id | 类别 | 成功 | 步数 | 工具调用(成功/总数) | degraded | 延迟(ms) | 工具链 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C01 | calc | ✅ | 2 | 1/1 | 否 | 0.50 | calculate | - |
| C02 | calc | ✅ | 2 | 1/1 | 否 | 0.36 | calculate | - |
| C03 | calc | ✅ | 2 | 1/1 | 否 | 0.31 | calculate | - |
| C04 | calc | ✅ | 2 | 1/1 | 否 | 0.37 | calculate | - |
| C05 | calc | ✅ | 2 | 1/1 | 否 | 0.26 | calculate | - |
| C06 | calc | ✅ | 2 | 1/1 | 否 | 0.25 | calculate | - |
| C07 | calc | ✅ | 2 | 1/1 | 否 | 0.25 | calculate | - |
| C08 | calc | ✅ | 2 | 1/1 | 否 | 0.24 | calculate | - |
| C09 | calc | ✅ | 2 | 1/1 | 否 | 0.24 | calculate | - |
| C10 | calc | ✅ | 2 | 1/1 | 否 | 0.25 | calculate | - |
| K01 | kb | ✅ | 2 | 1/1 | 否 | 4.51 | search_knowledge_base | - |
| K02 | kb | ✅ | 2 | 1/1 | 否 | 0.51 | search_knowledge_base | - |
| K03 | kb | ✅ | 2 | 1/1 | 否 | 0.48 | search_knowledge_base | - |
| K04 | kb | ✅ | 2 | 1/1 | 否 | 0.46 | search_knowledge_base | - |
| K05 | kb | ✅ | 2 | 1/1 | 否 | 0.47 | search_knowledge_base | - |
| M01 | multi_step | ✅ | 3 | 2/2 | 否 | 0.30 | calculate -> text_stats | - |
| M02 | multi_step | ✅ | 3 | 2/2 | 否 | 0.48 | search_knowledge_base -> calculate | - |
| M03 | multi_step | ✅ | 4 | 3/3 | 否 | 0.55 | search_knowledge_base -> calculate -> text_stats | - |
| X01 | risk_injection | ✅ | 2 | 1/1 | 否 | 0.22 | calculate | - |
| X02 | risk_step_limit | ✅ | 3 | 3/3 | 是 | 0.27 | text_stats | - |

## 说明与已知局限

1. **Mock LLM 是确定性策略**：普通任务按关键词触发单个工具调用；多步/风险任务使用脚本化 Mock LLM 按预定序列驱动，用于验证 Agent 编排与工具链路，不代表真实 LLM 的function calling 质量。
2. **工具调用成功率**按 trace 的 observation 是否以 `ERROR:` 开头统计；参数 JSON 解析失败、未知工具、工具异常都会被 Agent 收敛为 ERROR 观察，不抛给上层。
3. **MCP 未参与评测**：`use_mcp=False`（评测不依赖子进程/网络），MCP Server 的stdio roundtrip 与 ToolAgent 自动降级由 `tests/test_tool_agent.py` 覆盖。
4. **延迟是本地 mock 时延**（毫秒级），仅用于观察 Agent 自身开销，不代表线上 LLM 时延；P95 基于 20 条样本，统计波动较大。
5. 风险类任务的判定为「行为约束」：注入任务要求参数被 schema 白名单过滤且不泄露敏感信息；超步数任务要求 `max_steps` 触发 degraded 并说明原因（而非死循环/异常）。

复现命令：

```bash
cd ai-classroom/backend
python eval/agent_eval.py
```
