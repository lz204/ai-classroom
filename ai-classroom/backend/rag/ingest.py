"""知识库索引脚本：解析教材/课件（PDF / txt / md / docx），保留章节元数据，构建 FAISS 向量索引。

用法（在 backend/ 目录下执行）：
    python rag/ingest.py --path ../knowledge_base

说明（与项目文档 6.2.1 / 7.2 一致）：
- PDF 用 PyMuPDF 提取文本，docx 用 python-docx，txt/md 直接读取；
- 识别章节标题写入 metadata（chapter/section）；
- RecursiveCharacterTextSplitter 按章节递归切分，保证知识点不被跨 chunk 切断；
- 同时输出 chunks.txt 供 BM25 检索使用。
"""
import argparse
import os
import re
import sys
from pathlib import Path

# 兼容以脚本方式运行：把 backend/ 根目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 先加载配置（确保 HF 镜像端点等环境变量在 langchain/huggingface 导入前生效）
from config import EMBEDDING_MODEL, VECTOR_INDEX_PATH

import fitz
from langchain_core.documents import Document
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

# 支持的文件扩展名
SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md", ".markdown", ".docx"}

# 章节标题识别：第3章 极限 / 3.2 函数的极限
_RE_CHAPTER = re.compile(r"^\s*(第[0-9一二三四五六七八九十]+章[^\n]{0,20})$")
_RE_SECTION = re.compile(r"^\s*((?:\d+\.)+\d+)\s+([^\n]{0,20})$")


def read_lines(path: Path) -> list[str]:
    """按文件类型读取文本行。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        doc = fitz.open(path)
        lines = []
        for page in doc:
            lines.extend(page.get_text("text").splitlines())
        return lines
    if suffix == ".docx":
        from docx import Document as DocxDocument

        doc = DocxDocument(str(path))
        lines = [p.text for p in doc.paragraphs]
        # 表格中的文本也纳入
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    lines.append(cell.text)
        return lines
    # txt / md：直接按行读取
    return path.read_text(encoding="utf-8").splitlines()


def extract_blocks(path: Path) -> list[tuple[str, dict]]:
    """提取文本并按章节标题打上 metadata，返回 [(文本行, {"chapter": ..., "section": ...})]。"""
    cur_chapter, cur_section = "", ""
    blocks: list[tuple[str, dict]] = []
    for line in read_lines(path):
        text = line.strip()
        if not text:
            continue
        m = _RE_CHAPTER.match(text)
        if m:
            cur_chapter, cur_section = m.group(1).strip(), ""
            continue
        m2 = _RE_SECTION.match(text)
        if m2:
            cur_section = m2.group(1)
            continue
        # 有章节归属才纳入正文（跳过前言/目录等无章节文本）
        if cur_chapter:
            blocks.append((text, {"chapter": cur_chapter, "section": cur_section}))
    return blocks


def build_documents(path: Path, chunk_size: int = 500, chunk_overlap: int = 50) -> list[Document]:
    """把单个文件切分为带章节元数据的文档块。"""
    blocks = extract_blocks(path)
    # 同一章节内的行合并成段落，避免标题与正文碎片化
    groups: list[tuple[str, dict]] = []
    for text, meta in blocks:
        if groups and groups[-1][1] == meta:
            groups[-1] = (groups[-1][0] + "\n" + text, meta)
        else:
            groups.append((text, meta))

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", "；", "，", " "],
    )
    documents = []
    for text, meta in groups:
        for piece in splitter.split_text(text):
            piece = piece.strip()
            if piece:
                documents.append(Document(page_content=piece, metadata=dict(meta)))
    return documents


def main():
    parser = argparse.ArgumentParser(description="构建知识库 FAISS 索引")
    parser.add_argument("--path", default="../knowledge_base", help="教材/课件所在目录")
    parser.add_argument("--output", default=VECTOR_INDEX_PATH, help="索引输出目录")
    parser.add_argument("--chunk-size", type=int, default=500)
    args = parser.parse_args()

    input_dir = Path(args.path)
    if not input_dir.is_dir():
        print(f"输入目录不存在: {input_dir}")
        sys.exit(1)

    files = sorted(
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not files:
        print(f"目录下未找到支持的文档（{sorted(SUPPORTED_SUFFIXES)}）: {input_dir}")
        sys.exit(1)

    all_docs: list[Document] = []
    for f in files:
        docs = build_documents(f, chunk_size=args.chunk_size)
        all_docs.extend(docs)
        print(f"  {f.name}: {len(docs)} chunks")

    if not all_docs:
        print("未提取到有效文本，请检查文档是否包含可解析的正文内容")
        sys.exit(1)

    # 构建向量索引（首次运行会下载 embedding 模型，约 1.3GB）
    print(f"加载嵌入模型 {EMBEDDING_MODEL} ...")
    embeddings = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)
    vectorstore = FAISS.from_documents(all_docs, embeddings)

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    vectorstore.save_local(str(output))

    # 输出 chunks.txt 供 BM25 检索使用（每行一个 chunk 原文）
    with open(output / "chunks.txt", "w", encoding="utf-8") as f:
        for doc in all_docs:
            f.write(doc.page_content.replace("\n", " ") + "\n")

    print(f"已索引 {len(files)} 个文件，生成 {len(all_docs)} 个 chunks，"
          f"FAISS 索引保存至 {output}")


if __name__ == "__main__":
    main()
