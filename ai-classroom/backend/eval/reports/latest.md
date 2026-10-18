# 项目评测汇总报告

- 生成时间：2026-10-19T02:29:32+08:00
- 环境：Python 3.13.0 / Windows AMD64
- 总耗时：35.57s

## 结果总览

| 评测 | 状态 | 关键指标 |
| --- | --- | --- |
| RAG 检索 | OK | HitRate@4=1.0000, MRR@4=0.9563, 风险无证据率=0.9500 |
| Agent/Tool | OK | 任务成功率=1.0000, 工具调用成功率=1.0000, 平均步数=2.25, P95=0.55ms |

## RAG 检索评测

- 索引：`E:\ai_classroom_index`
- Embedding：`BAAI/bge-large-zh`
- 重排：未启用（默认；--with-reranker 开启）
- 数据集：正常 40 条 / 风险 20 条

| 指标 | 数值 |
| --- | --- |
| HitRate@4 | 1.0000 (40/40) |
| MRR@4 | 0.9563 |
| 风险集无充分证据率 | 0.9500 (19/20) |

> 已知缺口：风险集无充分证据率为启发式代理（retriever 无相似度阈值），详见 `rag_eval_latest.md`。

## Agent/Tool 评测

- 模式：Mock LLM + FakeRetriever（use_mcp=False，不联网）
- 任务数：20 条

| 指标 | 数值 |
| --- | --- |
| 任务成功率 | 1.0000 (20/20) |
| 工具调用成功率 | 1.0000 (26/26) |
| 平均步数 | 2.25 |
| P95 延迟 | 0.55 ms（本地 Mock 时延） |
| degraded 次数 | 1 |

> 说明：Mock LLM 确定性策略，验证 Agent 编排与工具链路，不代表真实 LLM 质量；详见 `agent_eval_latest.md`。

## 子报告

- `eval/reports/rag_eval_latest.md` / `rag_eval_latest.json`
- `eval/reports/agent_eval_latest.md` / `agent_eval_latest.json`

复现命令：

```bash
cd ai-classroom/backend
python eval/run_all.py
```
