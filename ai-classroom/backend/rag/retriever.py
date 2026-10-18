"""混合检索封装：FAISS 向量检索 + BM25 关键词检索 + cross-encoder 重排（可选）。

设计要点（与项目文档 6.2.2 一致）：
- 中文 BM25 必须先用 jieba 分词再建索引，否则按单字匹配导致检索失真；
- 重排模型未缓存时自动跳过（避免大模型下载阻塞授课）；
- 索引不存在/模型不可用时空检索兜底，不影响课堂主流程。
"""
import os
import re
import threading

# 先加载配置（确保 HF 镜像端点等环境变量在 langchain/huggingface 导入前生效）
from config import EMBEDDING_MODEL, RERANKER_MODEL, VECTOR_INDEX_PATH

import jieba
from langchain_community.retrievers.bm25 import BM25Retriever
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings


def _jieba_tokenize(text: str) -> list[str]:
    """中文分词：索引与查询走同一套分词逻辑"""
    return [tok.strip() for tok in jieba.cut(text) if tok.strip()]


class HybridRetriever:
    """FAISS + BM25 混合检索器（懒加载：首次检索时初始化，失败自动降级为空检索）"""

    def __init__(self, index_path: str = VECTOR_INDEX_PATH,
                 embedding_model: str = EMBEDDING_MODEL,
                 reranker_model: str = RERANKER_MODEL):
        self._index_path = index_path
        self._embedding_model = embedding_model
        self._reranker_model = reranker_model
        self._vector_retriever = None
        self._bm25_retriever = None
        self._reranker = None
        self._loaded = False
        self._load_failed = False
        # 加载锁：预热线程与课堂线程可能同时触发加载，需串行化避免重复加载
        self._load_lock = threading.Lock()

    # ---------- 初始化 ----------
    def _ensure_loaded(self):
        """首次检索前初始化；初始化失败则标记不可用（空检索兜底）"""
        if self._loaded or self._load_failed:
            return
        with self._load_lock:
            # 双重检查：等待锁期间可能已被其他线程加载完成
            if self._loaded or self._load_failed:
                return
            try:
                self._load()
                self._loaded = True
            except Exception:
                # 索引缺失或模型未下载等情况：本次会话内不再尝试，直接走空检索
                self._load_failed = True

    def _load(self):
        index_path = self._index_path
        if not os.path.isdir(index_path):
            raise FileNotFoundError(f"向量索引不存在: {index_path}，请先执行 python rag/ingest.py")

        # 1. 向量检索器（本地自建索引加载 pickle 文件，需显式开启反序列化）
        embeddings = HuggingFaceEmbeddings(model_name=self._embedding_model)
        vectorstore = FAISS.load_local(
            index_path, embeddings, allow_dangerous_deserialization=True
        )
        self._vector_retriever = vectorstore.as_retriever(search_kwargs={"k": 10})

        # 2. BM25 关键词检索器（语料来自 ingest 阶段写入同目录的 chunks.txt）
        chunks_path = os.path.join(index_path, "chunks.txt")
        if os.path.exists(chunks_path):
            with open(chunks_path, encoding="utf-8") as f:
                corpus = [line.strip() for line in f if line.strip()]
            self._bm25_retriever = BM25Retriever.from_texts(
                corpus, preprocess_func=_jieba_tokenize
            )
            self._bm25_retriever.k = 10

        # 3. 重排模型（可选）：未缓存时跳过
        self._reranker = self._load_reranker()

    def _load_reranker(self):
        """加载重排模型；仅当模型已在本地缓存时才加载。

        若未缓存，直接返回 None 跳过重排——避免 CrossEncoder 触发 2GB 模型下载，
        长时间阻塞初始化（与项目文档 6.2.2"模型未缓存时跳过重排"一致）。
        """
        try:
            from huggingface_hub import try_to_load_from_cache
        except Exception:
            return None
        # 模型 config.json 未在本地缓存 => 跳过重排
        if try_to_load_from_cache(self._reranker_model, "config.json") is None:
            return None
        try:
            from sentence_transformers import CrossEncoder
            return CrossEncoder(self._reranker_model)
        except Exception:
            # 加载失败跳过重排，不影响检索主流程
            return None

    # ---------- 检索 ----------
    def retrieve(self, query: str, top_k: int = 4) -> list:
        """混合检索：向量 + BM25 召回后按内容去重，可选重排后取 top_k。"""
        self._ensure_loaded()
        if not self._loaded or self._vector_retriever is None:
            return []

        candidates, seen = [], set()
        retrievers = [self._vector_retriever]
        if self._bm25_retriever is not None:
            retrievers.append(self._bm25_retriever)
        for retriever in retrievers:
            for doc in retriever.invoke(query):
                # 归一化后去重：向量 chunk（保留原始换行）与 BM25 行（换行已替换为空格）内容等价
                key = re.sub(r"\s+", "", doc.page_content)
                if key and key not in seen:
                    seen.add(key)
                    candidates.append(doc)

        # 重排：模型不可用时直接取前 top_k
        if self._reranker is None:
            return candidates[:top_k]
        pairs = [[query, doc.page_content] for doc in candidates]
        scores = self._reranker.predict(pairs)
        ranked = sorted(zip(scores, candidates), key=lambda x: -x[0])
        return [doc for _, doc in ranked[:top_k]]

    @staticmethod
    def format_context(docs: list) -> str:
        """把文档片段与章节号拼成参考内容，便于前端展示引用出处。"""
        parts = []
        for doc in docs:
            meta = doc.metadata or {}
            chapter = meta.get("chapter", "")
            section = meta.get("section", "")
            loc = " ".join(x for x in (chapter, section) if x)
            prefix = f"[{loc}]" if loc else ""
            parts.append(f"{prefix} {doc.page_content}".strip())
        return "\n".join(parts)
