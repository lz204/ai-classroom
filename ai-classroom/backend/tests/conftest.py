# -*- coding: utf-8 -*-
"""pytest 全局初始化：测试隔离（不联网 / 不连 Redis / 不加载真实向量索引 / 强制 Mock LLM）。

conftest.py 会在收集测试模块之前导入，因此这里设置的环境变量先于任何业务模块
（config / llm_client / retriever ...）生效，保证 pytest 与直接 `python tests/xxx.py`
的行为一致——CI 环境没有任何密钥、.env 与向量索引也能全绿。

说明：test_smoke.py / test_store.py 自身也会设置同样的环境变量；此处统一兜底，
新增测试（如 test_tool_agent.py）无需重复设置。
"""
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    # backend 下模块以顶层名导入（import config / import store / import classroom）
    sys.path.insert(0, str(BACKEND_DIR))

# 向量索引指向不存在的目录：retriever 走空检索兜底，不下载/加载 embedding 模型
os.environ["VECTOR_INDEX_PATH"] = str(BACKEND_DIR / "_no_index_for_tests")
# 不连 Redis：SessionStore 使用进程内实现
os.environ["REDIS_URL"] = ""
# 关闭流式打字延迟，避免测试等待
os.environ["STREAM_INTERVAL_SECONDS"] = "0"
# 空 Key 强制使用 MockLLM，绝不消耗真实 API 额度（必须在 import llm_client 前生效）
os.environ["LLM_API_KEY"] = ""
