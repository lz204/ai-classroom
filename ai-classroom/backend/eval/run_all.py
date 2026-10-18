# -*- coding: utf-8 -*-
"""一键评测入口：RAG 检索评测（索引可用时）+ Agent/Tool 评测（Mock，无需网络），汇总为一份报告。

运行方式（在 backend/ 目录下）：
    python eval/run_all.py
    python eval/run_all.py --skip-agent        # 只跑 RAG
    python eval/run_all.py --limit 5           # 调试：各评测只跑前 5 条

行为约定（CI 友好，任何"没数据"的情况都不判失败）：
- 向量索引不存在/无法加载：RAG 评测打印 SKIP，继续跑 Agent 评测；
- tool_agent.py 尚未就绪（P2-B 未合入）或导入失败：Agent 评测 SKIP，继续输出 RAG 结果；
- 输出：``eval/reports/latest.md``（汇总）+ ``latest.json``（机器可读），子报告为
  ``rag_eval_latest.md`` / ``agent_eval_latest.md``。
"""
import argparse
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent
for _path in (BACKEND_DIR, EVAL_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

REPORTS_DIR = EVAL_DIR / "reports"


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _md_escape(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _run_rag(limit: int | None, with_reranker: bool = False) -> dict:
    """运行 RAG 评测；异常统一转为 skipped（不中断汇总）。"""
    try:
        from rag_eval import run_rag_eval

        return run_rag_eval(limit=limit, with_reranker=with_reranker)
    except Exception as exc:  # noqa: BLE001 - 评测汇总需容错
        print(f"SKIP: RAG 评测执行失败（{type(exc).__name__}: {exc}）")
        return {"status": "skipped", "reason": f"{type(exc).__name__}: {exc}"}


def _run_agent(limit: int | None) -> dict:
    """运行 Agent 评测；tool_agent.py 未就绪（P2-B 未合入）时 SKIP，不中断汇总。"""
    try:
        from agent_eval import run_agent_eval
    except Exception as exc:  # noqa: BLE001 - P2-B 未就绪时优雅跳过
        print(f"SKIP: Agent 评测跳过（依赖未就绪：{type(exc).__name__}: {exc}）")
        return {"status": "skipped", "reason": f"依赖未就绪: {type(exc).__name__}: {exc}"}
    try:
        return run_agent_eval(limit=limit)
    except Exception as exc:  # noqa: BLE001 - 评测汇总需容错
        print(f"SKIP: Agent 评测执行失败（{type(exc).__name__}: {exc}）")
        return {"status": "skipped", "reason": f"{type(exc).__name__}: {exc}"}


def _rag_row(rag: dict) -> str:
    if rag.get("status") != "ok":
        return "SKIPPED"
    top_k = rag["top_k"]
    return (
        f"HitRate@{top_k}={rag['normal']['hit_rate']:.4f}, "
        f"MRR@{top_k}={rag['normal']['mrr']:.4f}, "
        f"风险无证据率={rag['risk']['no_evidence_rate']:.4f}"
    )


def _agent_row(agent: dict) -> str:
    if agent.get("status") != "ok":
        return "SKIPPED"
    return (
        f"任务成功率={agent['success_rate']:.4f}, "
        f"工具调用成功率={agent['tool_call_success_rate']:.4f}, "
        f"平均步数={agent['avg_steps']:.2f}, P95={agent['p95_latency_ms']:.2f}ms"
    )


def render_latest_markdown(rag: dict, agent: dict, elapsed_seconds: float) -> str:
    """渲染汇总报告（总览表 + 两个子评测的关键指标 + SKIP 原因 + 复现命令）。"""
    lines = [
        "# 项目评测汇总报告",
        "",
        f"- 生成时间：{_iso_now()}",
        f"- 环境：Python {platform.python_version()} / {platform.system()} {platform.machine()}",
        f"- 总耗时：{elapsed_seconds:.2f}s",
        "",
        "## 结果总览",
        "",
        "| 评测 | 状态 | 关键指标 |",
        "| --- | --- | --- |",
        f"| RAG 检索 | {'OK' if rag.get('status') == 'ok' else 'SKIP'} | {_rag_row(rag)} |",
        f"| Agent/Tool | {'OK' if agent.get('status') == 'ok' else 'SKIP'} | {_agent_row(agent)} |",
        "",
        "## RAG 检索评测",
        "",
    ]
    if rag.get("status") == "ok":
        top_k = rag["top_k"]
        lines.extend(
            [
                f"- 索引：`{rag['index_path']}`",
                f"- Embedding：`{rag['embedding_model']}`",
                f"- 重排：{'已启用' if rag.get('reranker_enabled') else '未启用（默认；--with-reranker 开启）'}",
                f"- 数据集：正常 {rag['normal']['count']} 条 / 风险 {rag['risk']['count']} 条",
                "",
                "| 指标 | 数值 |",
                "| --- | --- |",
                f"| HitRate@{top_k} | {rag['normal']['hit_rate']:.4f} "
                f"({rag['normal']['hit_count']}/{rag['normal']['count']}) |",
                f"| MRR@{top_k} | {rag['normal']['mrr']:.4f} |",
                f"| 风险集无充分证据率 | {rag['risk']['no_evidence_rate']:.4f} "
                f"({rag['risk']['no_evidence_count']}/{rag['risk']['count']}) |",
                "",
                "> 已知缺口：风险集无充分证据率为启发式代理（retriever 无相似度阈值），"
                "详见 `rag_eval_latest.md`。",
            ]
        )
    else:
        lines.append(f"- **SKIPPED**：{rag.get('reason', '索引不可用')}")
        lines.append("- 构建索引后可复跑：`cd backend && python rag/ingest.py --path ../knowledge_base`")

    lines.extend(["", "## Agent/Tool 评测", ""])
    if agent.get("status") == "ok":
        lines.extend(
            [
                f"- 模式：{agent['mode']}",
                f"- 任务数：{agent['task_count']} 条",
                "",
                "| 指标 | 数值 |",
                "| --- | --- |",
                f"| 任务成功率 | {agent['success_rate']:.4f} "
                f"({agent['success_count']}/{agent['task_count']}) |",
                f"| 工具调用成功率 | {agent['tool_call_success_rate']:.4f} "
                f"({agent['tool_calls_ok']}/{agent['tool_calls_total']}) |",
                f"| 平均步数 | {agent['avg_steps']:.2f} |",
                f"| P95 延迟 | {agent['p95_latency_ms']:.2f} ms（本地 Mock 时延） |",
                f"| degraded 次数 | {agent['degraded_count']} |",
                "",
                "> 说明：Mock LLM 确定性策略，验证 Agent 编排与工具链路，"
                "不代表真实 LLM 质量；详见 `agent_eval_latest.md`。",
            ]
        )
    else:
        lines.append(f"- **SKIPPED**：{agent.get('reason', '依赖未就绪')}")

    lines.extend(
        [
            "",
            "## 子报告",
            "",
            "- `eval/reports/rag_eval_latest.md` / `rag_eval_latest.json`",
            "- `eval/reports/agent_eval_latest.md` / `agent_eval_latest.json`",
            "",
            "复现命令：",
            "",
            "```bash",
            "cd ai-classroom/backend",
            "python eval/run_all.py",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键评测：RAG 检索 + Agent/Tool（Mock）")
    parser.add_argument("--skip-rag", action="store_true", help="跳过 RAG 评测")
    parser.add_argument("--skip-agent", action="store_true", help="跳过 Agent 评测")
    parser.add_argument("--limit", type=int, default=None, help="调试用：各评测只跑前 N 条")
    parser.add_argument(
        "--with-reranker",
        action="store_true",
        help="RAG 评测启用 cross-encoder 重排（默认关闭以保证可复现/CI 速度）",
    )
    parser.add_argument("--output", default=str(REPORTS_DIR / "latest.md"))
    parser.add_argument("--json-output", default=str(REPORTS_DIR / "latest.json"))
    args = parser.parse_args(argv)

    started = time.perf_counter()
    if args.skip_rag:
        print("SKIP: 按参数跳过 RAG 评测")
        rag = {"status": "skipped", "reason": "按 --skip-rag 跳过"}
    else:
        rag = _run_rag(args.limit, with_reranker=args.with_reranker)
    if args.skip_agent:
        print("SKIP: 按参数跳过 Agent 评测")
        agent = {"status": "skipped", "reason": "按 --skip-agent 跳过"}
    else:
        agent = _run_agent(args.limit)
    elapsed = time.perf_counter() - started

    output_md = Path(args.output)
    output_json = Path(args.json_output)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(render_latest_markdown(rag, agent, elapsed), encoding="utf-8")
    summary = {
        "generated_at": _iso_now(),
        "elapsed_seconds": round(elapsed, 3),
        "env": {
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.machine()}",
        },
        "rag": {
            "status": rag.get("status"),
            "reason": rag.get("reason"),
            "metrics": (
                {
                    "hit_rate": rag["normal"]["hit_rate"],
                    "mrr": rag["normal"]["mrr"],
                    "risk_no_evidence_rate": rag["risk"]["no_evidence_rate"],
                    "top_k": rag["top_k"],
                    "reranker_enabled": rag.get("reranker_enabled", False),
                    "count": rag["normal"]["count"] + rag["risk"]["count"],
                }
                if rag.get("status") == "ok"
                else None
            ),
        },
        "agent": {
            "status": agent.get("status"),
            "reason": agent.get("reason"),
            "metrics": (
                {
                    "success_rate": agent["success_rate"],
                    "tool_call_success_rate": agent["tool_call_success_rate"],
                    "avg_steps": agent["avg_steps"],
                    "p95_latency_ms": agent["p95_latency_ms"],
                    "degraded_count": agent["degraded_count"],
                    "task_count": agent["task_count"],
                }
                if agent.get("status") == "ok"
                else None
            ),
        },
    }
    output_json.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n================ 评测汇总 ================")
    print(f"RAG 检索   : {'OK' if rag.get('status') == 'ok' else 'SKIP'}  {_rag_row(rag)}")
    print(f"Agent/Tool : {'OK' if agent.get('status') == 'ok' else 'SKIP'}  {_agent_row(agent)}")
    print(f"总耗时     : {elapsed:.2f}s")
    print(f"汇总报告   : {output_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
