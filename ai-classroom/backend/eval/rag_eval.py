# -*- coding: utf-8 -*-
"""RAG 检索评测：正常集 HitRate@4 / MRR@4 + 风险集「无充分证据率」。

评测集：``eval/data/rag_eval_set.json``
- 40 条正常 query：覆盖《高等数学_极限与连续》第1/2章 10 个小节（每节 4 条），
  标注 expected_sections（期望命中的节号）与 expected_keywords（正文关键词）；
- 20 条风险 query：无关问题 / 注入越界 / 空查询。

相关性判定（top_k=4）：
- 结果 metadata.section 落在 expected_sections 内 → 相关；
- 或结果正文包含任一 expected_keywords → 相关（BM25 召回结果无章节元数据，此时只能按关键词判定）；
- 仅同章不同节不算命中（避免因同章 chunk 多而虚高）。

风险集「无充分证据率」：当前 HybridRetriever 无相似度分数与相关性阈值（已知缺口），
这里用启发式代理——检索结果为空，或 query 分词与 top_k 内容的词面重合率 < 阈值（默认 0.2），
即视为「无充分证据」。该指标只反映「检索器是否倾向于把无关问题也硬凑出证据」，
生产化需要给召回加相似度阈值。

运行方式（在 backend/ 目录下）：
    python eval/rag_eval.py                          # 默认索引 config.VECTOR_INDEX_PATH
    python eval/rag_eval.py --index E:\\ai_classroom_index
    python eval/rag_eval.py --limit 5                # 调试：只跑前 5 条
索引不存在 / 无法加载时打印 SKIP 并以退出码 0 结束（CI 无向量索引也能跑，不失败）。

输出：``eval/reports/rag_eval_latest.md``（表格）与 ``rag_eval_latest.json``（原始指标）。
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVAL_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import EMBEDDING_MODEL, RERANKER_MODEL, VECTOR_INDEX_PATH  # noqa: E402

DATASET_PATH = EVAL_DIR / "data" / "rag_eval_set.json"
REPORTS_DIR = EVAL_DIR / "reports"
DEFAULT_TOP_K = 4
DEFAULT_EVIDENCE_THRESHOLD = 0.2
# 判定「索引可用」所需的文件：任一存在即尝试加载（加载失败会再降级为 SKIP）
_INDEX_FILES = ("index.faiss", "index.pkl")
_VALID_TYPES = ("normal", "risk")
# 评测默认关闭重排：传入一个未缓存的模型名，HybridRetriever 会按既有设计跳过重排。
# 原因：①结果可复现（不同机器是否缓存 reranker 会改变指标）；②CI/CPU 环境快；
# 如需测完整检索链路（含 bge-reranker-large 重排），用 --with-reranker（CPU 上耗时显著增加）。
RERANKER_DISABLED_MODEL = "__reranker_disabled_for_eval__"


# ============ 数据集 ============


def load_eval_set(path: Path = DATASET_PATH) -> dict:
    """加载并校验评测集（结构非法直接抛 ValueError，避免跑出无意义的指标）。"""
    if not path.is_file():
        raise FileNotFoundError(f"评测集不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"评测集格式错误（items 为空）: {path}")

    seen_ids = set()
    for item in items:
        item_id = item.get("id")
        if not item_id or item_id in seen_ids:
            raise ValueError(f"评测集 id 缺失或重复: {item_id!r}")
        seen_ids.add(item_id)
        if item.get("type") not in _VALID_TYPES:
            raise ValueError(f"{item_id}: type 必须是 {_VALID_TYPES}, 实际 {item.get('type')!r}")
        if not isinstance(item.get("query"), str):
            raise ValueError(f"{item_id}: query 必须是字符串")
        if item["type"] == "normal":
            sections = item.get("expected_sections")
            keywords = item.get("expected_keywords")
            if not sections or not isinstance(sections, list):
                raise ValueError(f"{item_id}: 正常 query 必须提供 expected_sections")
            if not keywords or not isinstance(keywords, list):
                raise ValueError(f"{item_id}: 正常 query 必须提供 expected_keywords")
    return data


# ============ 索引可用性 ============


def check_index(index_path: str) -> tuple[bool, str]:
    """检查向量索引是否可用（不做加载，避免 CI 无索引时导入 embedding 依赖）。"""
    path = Path(index_path)
    if not path.is_dir():
        return False, f"索引目录不存在: {path}"
    if not any((path / name).is_file() for name in _INDEX_FILES):
        return False, f"索引目录缺少 {'/'.join(_INDEX_FILES)}: {path}"
    return True, ""


# ============ 相关性 / 证据判定 ============


def is_relevant(doc, item: dict) -> bool:
    """结果相关性：命中期望节号，或正文命中期望关键词（同章不同节不算命中）。"""
    metadata = getattr(doc, "metadata", None) or {}
    section = metadata.get("section") or ""
    if section and section in item["expected_sections"]:
        return True
    content = getattr(doc, "page_content", "") or ""
    return any(keyword in content for keyword in item["expected_keywords"])


def query_tokens(query: str) -> set[str]:
    """查询分词（长度>=2 的 token），用于风险集词面重合度启发式。"""
    query = (query or "").strip()
    if not query:
        return set()
    try:
        import jieba

        return {tok.strip() for tok in jieba.cut(query) if len(tok.strip()) >= 2}
    except Exception:  # jieba 不可用时退化为中文字符串/英文单词
        import re

        return set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}", query))


def evidence_score(tokens: set[str], docs: list) -> float:
    """词面证据分：top_k 结果中命中的 query token 占全部 token 的最大比例。"""
    if not tokens:
        return 0.0
    best = 0.0
    for doc in docs:
        content = getattr(doc, "page_content", "") or ""
        matched = sum(1 for token in tokens if token in content)
        best = max(best, matched / len(tokens))
    return best


# ============ 结果整理 ============


def _doc_sections(docs: list) -> list[str]:
    """结果命中的节号列表（BM25 结果无元数据记为空串，去重保序）。"""
    sections = []
    for doc in docs:
        metadata = getattr(doc, "metadata", None) or {}
        section = metadata.get("section") or ""
        if section and section not in sections:
            sections.append(section)
    return sections


def _retrieve(retriever, query: str, top_k: int) -> list:
    """单条检索；异常按空结果处理（评测不应因个别 query 崩溃）。"""
    try:
        return retriever.retrieve(query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 - 评测需容错并继续统计
        print(f"[warn] 检索失败 query={query!r}: {type(exc).__name__}: {exc}")
        return []


def _evaluate_normal(retriever, items: list[dict], top_k: int) -> dict:
    """正常集：HitRate@k（top-k 至少 1 条相关）与 MRR@k（首个相关结果名次倒数）。"""
    hits = 0
    reciprocal_ranks = 0.0
    details = []
    for item in items:
        docs = _retrieve(retriever, item["query"], top_k)
        first_rank = None
        for rank, doc in enumerate(docs, start=1):
            if is_relevant(doc, item):
                first_rank = rank
                break
        hit = first_rank is not None
        hits += int(hit)
        reciprocal_ranks += (1.0 / first_rank) if first_rank else 0.0
        details.append(
            {
                "id": item["id"],
                "query": item["query"],
                "expected_sections": item["expected_sections"],
                "hit": hit,
                "first_relevant_rank": first_rank,
                "got_sections": _doc_sections(docs),
                "result_count": len(docs),
            }
        )
    total = len(items)
    return {
        "count": total,
        "hit_count": hits,
        "hit_rate": round(hits / total, 4) if total else 0.0,
        "mrr": round(reciprocal_ranks / total, 4) if total else 0.0,
        "misses": [detail for detail in details if not detail["hit"]],
        "details": details,
    }


def _evaluate_risk(
    retriever, items: list[dict], top_k: int, evidence_threshold: float
) -> dict:
    """风险集：无充分证据率（检索为空或词面证据分低于阈值）。"""
    insufficient = 0
    empty_results = 0
    details = []
    for item in items:
        query = item["query"]
        tokens = query_tokens(query)
        # 空/纯空白 query 不做检索：无输入自然无证据
        docs = _retrieve(retriever, query, top_k) if tokens else []
        score = evidence_score(tokens, docs)
        if not docs:
            empty_results += 1
        no_evidence = (not docs) or score < evidence_threshold
        insufficient += int(no_evidence)
        details.append(
            {
                "id": item["id"],
                "query": query,
                "risk_type": item.get("risk_type", "risk"),
                "result_count": len(docs),
                "evidence_score": round(score, 4),
                "no_evidence": no_evidence,
                "got_sections": _doc_sections(docs),
            }
        )
    total = len(items)
    return {
        "count": total,
        "no_evidence_count": insufficient,
        "no_evidence_rate": round(insufficient / total, 4) if total else 0.0,
        "empty_result_count": empty_results,
        "with_results_count": total - empty_results,
        "evidence_threshold": evidence_threshold,
        "details": details,
    }


# ============ 报告输出 ============


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _md_escape(text: str) -> str:
    """Markdown 表格单元格转义（竖线/换行）。"""
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(metrics: dict) -> str:
    """把指标渲染成 Markdown 报告（汇总表 + 逐条明细 + 已知缺口）。"""
    lines = ["# RAG 检索评测报告", ""]
    if metrics["status"] != "ok":
        lines.extend(
            [
                f"- 状态：**SKIPPED**（{metrics.get('reason', '')}）",
                f"- 索引路径：`{metrics.get('index_path', '')}`",
                f"- 生成时间：{metrics.get('generated_at', '')}",
                "",
                "> 向量索引不存在时跳过评测（不判失败）。构建索引："
                "`cd backend && python rag/ingest.py --path ../knowledge_base`",
            ]
        )
        return "\n".join(lines) + "\n"

    normal = metrics["normal"]
    risk = metrics["risk"]
    lines.extend(
        [
            f"- 生成时间：{metrics['generated_at']}",
            f"- 索引路径：`{metrics['index_path']}`",
            f"- Embedding 模型：`{metrics['embedding_model']}`",
            f"- 重排：{'已启用（`' + str(metrics.get('reranker_model')) + '`）' if metrics.get('reranker_enabled') else '未启用（默认关闭以保证可复现/CI 速度；用 --with-reranker 测完整链路）'}",
            f"- 数据集：`{metrics['dataset']}`（正常 {normal['count']} 条 / 风险 {risk['count']} 条）",
            f"- top_k：{metrics['top_k']}，风险证据阈值：{risk['evidence_threshold']}",
            f"- 评测耗时：{metrics['elapsed_seconds']:.2f}s（单条平均 {metrics['avg_query_ms']:.1f}ms）",
            "",
            "## 总体指标",
            "",
            "| 指标 | 数值 | 说明 |",
            "| --- | --- | --- |",
            f"| 正常集 HitRate@{metrics['top_k']} | {normal['hit_rate']:.4f} "
            f"({normal['hit_count']}/{normal['count']}) | top-{metrics['top_k']} 至少 1 条相关 |",
            f"| 正常集 MRR@{metrics['top_k']} | {normal['mrr']:.4f} | 首个相关结果名次倒数均值 |",
            f"| 风险集 无充分证据率 | {risk['no_evidence_rate']:.4f} "
            f"({risk['no_evidence_count']}/{risk['count']}) | 启发式代理，非真实置信度 |",
            f"| 风险集 有召回结果率 | {risk['with_results_count'] / risk['count']:.4f} "
            f"({risk['with_results_count']}/{risk['count']}) | 检索器无阈值，通常接近 100% |",
            "",
            "## 正常集逐条结果",
            "",
            "| id | query | 命中 | 首个相关名次 | 检索到节号 | 结果数 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for detail in normal["details"]:
        lines.append(
            f"| {detail['id']} | {_md_escape(detail['query'])} | "
            f"{'✅' if detail['hit'] else '❌'} | {detail['first_relevant_rank'] or '-'} | "
            f"{_md_escape(','.join(detail['got_sections']) or '-')} | {detail['result_count']} |"
        )

    lines.extend(
        [
            "",
            "## 风险集逐条结果",
            "",
            "| id | query | 类型 | 结果数 | 词面证据分 | 无充分证据 | 检索到节号 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for detail in risk["details"]:
        query = detail["query"] or "（空字符串）"
        lines.append(
            f"| {detail['id']} | {_md_escape(query)} | {_md_escape(detail['risk_type'])} | "
            f"{detail['result_count']} | {detail['evidence_score']:.2f} | "
            f"{'是' if detail['no_evidence'] else '否'} | "
            f"{_md_escape(','.join(detail['got_sections']) or '-')} |"
        )

    lines.extend(
        [
            "",
            "## 指标定义与已知缺口",
            "",
            "1. **相关性判定**：结果 `metadata.section` ∈ expected_sections，或正文命中任一 "
            "expected_keywords；仅同章不同节不算命中（避免同章 chunk 多导致虚高）。",
            "2. **BM25 结果无章节元数据**：混合检索去重后可能保留 BM25 chunk，此时只能按关键词判定。",
            "3. **风险集无充分证据率是启发式代理**：当前 HybridRetriever 不输出相似度分数、"
            "无相关性阈值（已知缺口），指标按「检索为空或 query 分词与 top-k 内容词面重合率 "
            f"< {risk['evidence_threshold']}」统计；生产化需给召回加分数阈值/拒答策略。",
            "4. **空 query 不做检索**，直接计为无充分证据（避免把空输入喂给 embedding）。",
            "5. 评测默认**关闭 reranker**（`--with-reranker` 开启）：不同环境是否缓存 "
            "`BAAI/bge-reranker-large` 会改变指标，默认关闭保证可复现与 CI 速度；"
            "关闭时结果为向量 + BM25 合并去重后的 top_k。",
            "",
            "复现命令：",
            "",
            "```bash",
            "cd ai-classroom/backend",
            f"python eval/rag_eval.py --index \"{metrics['index_path']}\" --top-k {metrics['top_k']}",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_reports(metrics: dict, output_md: Path, output_json: Path) -> None:
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(render_markdown(metrics), encoding="utf-8")
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ============ 入口 ============


def run_rag_eval(
    index_path: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    evidence_threshold: float = DEFAULT_EVIDENCE_THRESHOLD,
    limit: int | None = None,
    output_md: Path | None = None,
    output_json: Path | None = None,
    with_reranker: bool = False,
) -> dict:
    """执行 RAG 评测并写报告；索引不可用时返回 status=skipped（不抛异常）。

    :param with_reranker: 是否启用 cross-encoder 重排（默认关闭，保证可复现与 CI 速度）
    """
    index_path = str(index_path or VECTOR_INDEX_PATH)
    output_md = output_md or (REPORTS_DIR / "rag_eval_latest.md")
    output_json = output_json or (REPORTS_DIR / "rag_eval_latest.json")
    started = time.perf_counter()
    data = load_eval_set()
    items = data["items"]
    if limit:
        items = items[:limit]
    normal_items = [item for item in items if item["type"] == "normal"]
    risk_items = [item for item in items if item["type"] == "risk"]

    available, reason = check_index(index_path)
    if not available:
        metrics = {
            "status": "skipped",
            "reason": reason,
            "index_path": index_path,
            "generated_at": _iso_now(),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        _write_reports(metrics, output_md, output_json)
        print(f"SKIP: RAG 评测跳过（{reason}）")
        return metrics

    # 索引可用才导入 RAG 依赖（CI 无索引时无需加载 langchain/sentence-transformers）
    from rag.retriever import HybridRetriever  # noqa: E402

    if with_reranker:
        retriever = HybridRetriever(index_path=index_path)
    else:
        # 默认关闭重排（见 RERANKER_DISABLED_MODEL 注释），只走向量 + BM25 合并取 top_k
        retriever = HybridRetriever(
            index_path=index_path, reranker_model=RERANKER_DISABLED_MODEL
        )
    # 探测加载状态：retrieve 失败时 retriever 内部会置 _load_failed（空检索兜底）
    probe = next((item["query"] for item in normal_items if item["query"].strip()), "")
    first_docs = _retrieve(retriever, probe, top_k) if probe else []
    if not getattr(retriever, "_loaded", False):
        metrics = {
            "status": "skipped",
            "reason": "索引加载失败（可能缺少 embedding 模型缓存或索引文件损坏）",
            "index_path": index_path,
            "generated_at": _iso_now(),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        _write_reports(metrics, output_md, output_json)
        print(f"SKIP: RAG 评测跳过（{metrics['reason']}）")
        return metrics

    normal = _evaluate_normal(retriever, normal_items, top_k)
    risk = _evaluate_risk(retriever, risk_items, top_k, evidence_threshold)
    elapsed = time.perf_counter() - started
    metrics = {
        "status": "ok",
        "generated_at": _iso_now(),
        "index_path": index_path,
        "embedding_model": EMBEDDING_MODEL,
        "reranker_enabled": with_reranker,
        "reranker_model": RERANKER_MODEL if with_reranker else None,
        "dataset": str(DATASET_PATH),
        "top_k": top_k,
        "normal": normal,
        "risk": risk,
        "elapsed_seconds": round(elapsed, 3),
        "avg_query_ms": round(
            elapsed * 1000 / max(len(normal_items) + len(risk_items), 1), 2
        ),
        "probe_result_count": len(first_docs),
    }
    _write_reports(metrics, output_md, output_json)

    print(
        f"RAG 评测完成：HitRate@{top_k}={normal['hit_rate']:.4f} "
        f"({normal['hit_count']}/{normal['count']}), MRR@{top_k}={normal['mrr']:.4f}, "
        f"风险无充分证据率={risk['no_evidence_rate']:.4f} "
        f"({risk['no_evidence_count']}/{risk['count']}), 耗时 {elapsed:.1f}s"
    )
    print(f"报告已写入: {output_md}")
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAG 检索评测（HitRate@4 / MRR@4 / 风险无证据率）")
    parser.add_argument(
        "--index",
        default=VECTOR_INDEX_PATH,
        help=f"向量索引目录（默认 config.VECTOR_INDEX_PATH: {VECTOR_INDEX_PATH}）",
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="取前 K 条结果")
    parser.add_argument(
        "--evidence-threshold",
        type=float,
        default=DEFAULT_EVIDENCE_THRESHOLD,
        help="风险集词面证据分阈值（低于视为无充分证据）",
    )
    parser.add_argument("--limit", type=int, default=None, help="调试用：只评测前 N 条")
    parser.add_argument(
        "--with-reranker",
        action="store_true",
        help="启用 cross-encoder 重排（默认关闭；CPU 上会明显变慢）",
    )
    parser.add_argument("--output", default=str(REPORTS_DIR / "rag_eval_latest.md"))
    parser.add_argument("--json-output", default=str(REPORTS_DIR / "rag_eval_latest.json"))
    args = parser.parse_args(argv)

    metrics = run_rag_eval(
        index_path=args.index,
        top_k=args.top_k,
        evidence_threshold=args.evidence_threshold,
        limit=args.limit,
        output_md=Path(args.output),
        output_json=Path(args.json_output),
        with_reranker=args.with_reranker,
    )
    # SKIP 不算失败（CI 无索引时返回 0）
    return 0 if metrics["status"] in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
