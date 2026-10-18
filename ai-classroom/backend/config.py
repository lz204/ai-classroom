"""全局配置：从项目根目录 .env 加载环境变量（后端统一以 backend/ 为工作目录运行）"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 后端目录（backend/）与项目根目录
BACKEND_DIR = Path(__file__).resolve().parent
# 项目根目录（backend/ 的上一级）
BASE_DIR = BACKEND_DIR.parent
# 加载项目根目录 .env（存在时才加载，允许直接使用系统环境变量）
load_dotenv(BASE_DIR / ".env")

# 若 .env 配置了 HF 镜像端点（国内网络加速），同步到环境变量供 HuggingFace 下载使用
if os.getenv("HF_ENDPOINT"):
    os.environ.setdefault("HF_ENDPOINT", os.getenv("HF_ENDPOINT"))


def _get_bool(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量"""
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# ============ LLM ============
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen-plus")
# LLM 请求超时（秒）：课堂 prompt 较长（含 RAG 参考内容），生成耗时需放宽
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))

# ============ 嵌入与检索 ============
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-zh")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-large")
# 向量索引路径（相对 backend 目录，与 .env 约定一致）
VECTOR_INDEX_PATH = os.getenv("VECTOR_INDEX_PATH", "../knowledge_base/faiss_index")
# 若路径为相对路径，统一基于 backend/ 解析（例如 ../knowledge_base = 项目根下 knowledge_base）
if not os.path.isabs(VECTOR_INDEX_PATH):
    VECTOR_INDEX_PATH = str((BACKEND_DIR / VECTOR_INDEX_PATH).resolve())

# ============ 服务与存储 ============
REDIS_URL = os.getenv("REDIS_URL", "").strip()
BACKEND_PORT = int(os.getenv("BACKEND_PORT", "8000"))
DEBUG = _get_bool("DEBUG", False)

# ============ 课堂配置 ============
# 学生性格与数量（用于初始化学生智能体群）
STUDENT_PERSONALITIES = os.getenv(
    "STUDENT_PERSONALITIES", "introvert,active,expert,follower"
).split(",")
STUDENT_PERSONALITIES = [p.strip() for p in STUDENT_PERSONALITIES if p.strip()]
# 学生分组数（性格互补分组研讨：expert+introvert / active+follower 配对）
STUDENT_GROUP_COUNT = int(os.getenv("STUDENT_GROUP_COUNT", "2"))

# ============ 课堂超时与流式输出 ============
# 单个教学环节超时（秒）：超时后由管理员 Agent 收束；测试可调小
STAGE_TIMEOUT_SECONDS = float(os.getenv("STAGE_TIMEOUT_SECONDS", "300"))
# 整堂课超时（秒）
CLASS_TIMEOUT_SECONDS = float(os.getenv("CLASS_TIMEOUT_SECONDS", "1800"))
# 消息流式分片：每片字符数与片间延迟（秒），用于前端打字机效果
STREAM_CHUNK_SIZE = int(os.getenv("STREAM_CHUNK_SIZE", "12"))
STREAM_INTERVAL_SECONDS = float(os.getenv("STREAM_INTERVAL_SECONDS", "0.02"))

# ============ 持久化 ============
# 会话元数据 / 消息缓冲 / 学情档案在 Redis（或进程内降级）中的 TTL（秒）
CLASSROOM_TTL = int(os.getenv("CLASSROOM_TTL", "3600"))
