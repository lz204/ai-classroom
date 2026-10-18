# AI Classroom 多智能体互动教学课堂

基于 **FastAPI + LangGraph** 的多智能体互动教学课堂 Demo：AI 主讲老师、助教、多名性格不同的学生 Agent 与课堂管理员在同一课堂状态机中协同，完成「授课 → 提问 → 答疑 → 小组研讨 → 作业批改 → 学情总结」全流程；授课内容由本地教材知识库 RAG 检索生成，尽量还原真实课堂的互动与秩序感。

> 完整的架构图、关键实现与踩坑复盘见设计文档：[《AI Classroom 多智能体互动教学课堂》项目文档](../AI_Classroom_多智能体互动教学课堂_项目文档.md)。

## 架构要点

```
Vue3 + Vite ──REST / WebSocket──► FastAPI 后端 ──► LangGraph StateGraph
                                                      ├─ teacher  主讲授课 / 答疑
                                                      ├─ student  学生群（节点内并发提问）
                                                      ├─ arbiter  发言仲裁与排序
                                                      ├─ ta       作业批改
                                                      └─ manager  秩序管控 / 学情总结
                                                           │
                                          RAG：FAISS 向量 + BM25 关键词（+ 可选重排）
                                          记忆：滑窗 + LLM 摘要 + 实体关联（三层）
```

- **后端**：FastAPI 提供 REST 接口（会话创建 / 状态查询）与 WebSocket 实时推送。
- **编排**：LangGraph `StateGraph` 共 5 类节点 `teacher / student / arbiter / ta / manager`；条件路由以单调递增的 `round` 计数为唯一依据，`phase` 仅用于前端展示，避免课堂顺序来回跳变。
- **RAG**：`backend/rag/ingest.py` 解析教材（txt / md / pdf / docx）并建 FAISS 索引；检索时 FAISS 向量召回 + BM25 关键词召回（jieba 分词）混合去重，重排模型未缓存时自动跳过。
- **对话记忆**：短期滑窗 + LLM 摘要压缩 + 实体（章节号 / 定理名等）关联，注入老师 Prompt；当前为进程内实现。
- **WebSocket 断线续传**：前端 30s 心跳（`ping` / `pong`）；发送中的消息统一先入缓冲，断线重连时携带 `last_msg_id`，后端只补发未收到的消息（去重不重复），缓冲在 Redis 未配置时使用进程内实现。
- **Mock LLM**：未配置 `LLM_API_KEY` 时自动降级为内置 Mock LLM，无 Key 也能跑通完整课堂流程，便于本地演示与测试。

## 目录结构

```
ai-classroom/
├── backend/                    # FastAPI 后端
│   ├── main.py                 # 入口：REST 路由 + WebSocket 端点（心跳 / 断线续传）
│   ├── classroom.py            # 课堂会话、消息推送、断线重传缓冲
│   ├── config.py               # 配置加载（从项目根 .env 读取）
│   ├── llm_client.py           # LLM 客户端（DashScope OpenAI 兼容 / 内置 Mock）
│   ├── agents/                 # teacher / student / ta / manager 各角色 Agent
│   ├── graph/                  # LangGraph 状态机、发言队列与仲裁器
│   ├── rag/                    # ingest.py 建索引、retriever.py 混合检索
│   ├── memory/                 # 三层对话记忆
│   ├── api/                    # REST 路由
│   ├── tests/test_smoke.py     # P0 回归冒烟测试（Mock LLM，无需 Key / 索引）
│   ├── requirements.txt        # 后端依赖清单
│   └── Dockerfile
├── frontend/                   # Vue3 + Vite + Element Plus 前端
│   ├── src/                    # 课堂界面、对话气泡、REST/WS 封装
│   ├── Dockerfile
│   └── nginx.conf              # 生产镜像中的 Nginx 配置（静态资源 + API/WS 反向代理）
├── knowledge_base/             # 教材 / 题库原始资料（示例教材：高等数学_极限与连续.txt）
├── docker-compose.yml          # backend + frontend + redis 一键部署
├── Makefile                    # 常用命令
├── .env.example                # 环境变量模板（复制为 .env 后填写）
└── README.md
```

## 环境要求

| 组件 | 要求 | 说明 |
|------|------|------|
| Python | 3.10+ | 本项目自带虚拟环境 `.venv`（Python 3.13）；Windows 激活命令 `.venv\Scripts\activate` |
| Node.js | 18+ | 前端构建（Docker 镜像使用 `node:20-alpine`） |
| Redis | 可选 | 配置 `REDIS_URL` 时 WebSocket 断线消息缓冲走 Redis；**未配置时使用进程内缓冲**（单进程部署可用） |
| 网络 / 模型 | 首次需下载嵌入模型 | `bge-large-zh` 约 1.2GB（CPU 可跑，无需 GPU）；可选重排模型 `bge-reranker-large` 未缓存时自动跳过 |
| 索引路径 | 纯英文路径 | faiss 的 C 层读写不支持中文目录，而本项目路径含「ai实验」，故 `VECTOR_INDEX_PATH` 必须指向纯英文目录（如 `E:\ai_classroom_index`） |

## 快速开始（本地开发）

以下命令的相对路径与当前仓库结构一致；Windows / Git Bash 可直接执行，Linux / macOS 把 `copy` 换成 `cp` 即可。

### 1. 安装后端依赖

`requirements.txt` 位于 `backend/` 下，请进入 `backend/` 执行（若使用项目自带虚拟环境，先激活：项目根目录 `.venv\Scripts\activate`）：

```bash
cd backend
pip install -r requirements.txt
```

### 2. 配置环境变量

在**项目根目录**复制环境变量模板，并填入真实的 `LLM_API_KEY`：

```bash
# 项目根目录（ai-classroom/）
copy .env.example .env        # Linux / macOS：cp .env.example .env
```

- `LLM_API_KEY`：阿里百炼（DashScope）API Key，留空时自动使用内置 Mock LLM（无 Key 也能跑通全流程）。
- `LLM_BASE_URL` / `LLM_MODEL`：默认 `https://dashscope.aliyuncs.com/compatible-mode/v1` / `qwen-plus`。
- `VECTOR_INDEX_PATH`：**必须是纯英文绝对路径**（例如 `E:\ai_classroom_index`）。faiss C 层不支持中文目录，而本项目路径含「ai实验」。
- `REDIS_URL`：本机没有 Redis 时请留空（留空即使用进程内缓冲），否则断线重传会尝试连接 Redis。
- 真实的 Key 只放在 `.env`（已被 `.gitignore` 忽略），不要写入 `.env.example` 或提交到仓库。

### 3. 构建知识库索引

在 `backend/` 下执行（脚本会读取 `knowledge_base/` 下的 txt / md / pdf / docx，按章节切分后写入 FAISS 索引）：

```bash
cd backend
python rag/ingest.py --path ../knowledge_base
```

- 索引输出到 `.env` 中的 `VECTOR_INDEX_PATH`，同目录还会生成 `chunks.txt` 供 BM25 检索使用。
- 首次运行会下载 `bge-large-zh` 嵌入模型（约 1.2GB），请保证网络可用（可在 `.env` 设置 `HF_ENDPOINT=https://hf-mirror.com` 使用国内镜像）。
- 若想显式指定输出目录，可加 `--output`，例如 `python rag/ingest.py --path ../knowledge_base --output ../knowledge_base/faiss_index`（注意输出目录同样不要包含中文路径）。
- 不建索引也能启动：RAG 会走空检索兜底，只是失去教材引用能力。

### 4. 启动后端

```bash
cd backend
uvicorn main:app --reload --port 8000
```

- 健康检查：<http://localhost:8000/api/health>
- 接口文档（Swagger）：<http://localhost:8000/docs>

### 5. 启动前端（另开一个终端）

```bash
cd frontend
npm install
npm run dev
```

浏览器访问 <http://localhost:5173>。Vite 已把 `/api`、`/ws` 代理到 `http://localhost:8000`。

### 使用方式

1. 打开 <http://localhost:5173>，输入授课主题（例如「高等数学 第一章 极限与连续」）创建课堂。
2. 进入课堂后前端自动连接 WebSocket 并发送 `start`，实时展示老师 / 学生 / 助教 / 管理员发言与秩序告警。
3. 网络断开后前端会自动重连（指数退避，最长 8s）并按 `last_msg_id` 补发消息；当前版本会话 id 未持久化，刷新页面会丢失课堂视图，需重新发起授课。

### Makefile 常用命令（项目根目录执行）

| 命令 | 作用 |
|------|------|
| `make install` | 安装后端依赖（`pip install -r backend/requirements.txt`）与前端依赖（`cd frontend && npm install`） |
| `make ingest` | 构建知识库索引（`cd backend && python rag/ingest.py --path ../knowledge_base`） |
| `make dev-backend` | 启动后端（`cd backend && uvicorn main:app --reload --port 8000`） |
| `make dev-frontend` | 启动前端（`cd frontend && npm run dev`） |
| `make up` / `make down` | Docker Compose 启动 / 停止 |
| `make clean` | 清理 Python / Node 缓存（`find`、`rm` 命令，建议在 Git Bash 中执行） |

## 测试

P0 回归冒烟测试使用内置 Mock LLM，**不需要真实 API Key、不需要向量索引**，几秒内可跑完，覆盖：断线缓冲 LRANGE 语义、Mock LLM 下的完整课堂流程（老师 / 学生 / 助教 / 管理员消息与 phase/round 字段）、断线重连续传不丢尾消息。

```bash
cd backend
python tests/test_smoke.py
```

预期最后一行输出：`ALL P0 REGRESSION TESTS PASSED`。

## API / WebSocket 协议简表

### REST

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/health` | 健康检查，返回 `{"status": "ok"}` |
| `POST` | `/api/classroom/start` | 创建课堂会话；请求体 `{"topic": "授课主题"}`，返回 `{"session_id": "...", "topic": "..."}` |
| `GET` | `/api/classroom/{session_id}` | 查询会话状态（`running` / `round` / `phase` / `msg_count` / `message_count` 等） |

另有 `GET /` 返回服务入口说明，`GET /docs` 为 OpenAPI 文档。

### WebSocket

连接地址：`/ws/{session_id}?last_msg_id=0`

- `last_msg_id`：已收到的最大消息 id。后端会补发该 id 之后缓冲区中的消息；传 `0`（例如页面刷新后前端从 0 重新开始）则补发缓冲区中的全部历史消息。
- 客户端 → 服务端：
  - `{"type": "ping"}`：心跳（前端每 30s 一次），服务端回 `{"type": "pong"}`；
  - `{"type": "start"}`：启动课堂流程（异步执行，不阻塞消息接收）。
- 服务端 → 客户端（推送消息带自增 `id`，心跳 `pong` 除外）：
  - `message`：老师 / 学生 / 助教 / 管理员发言，含 `speaker`、`content`、`citation`、`phase`、`round`、`id`；
  - `alert`：秩序告警（如学生 Agent 超时走神）；
  - `end`：课堂流程结束；
  - `error`：课堂运行异常；
  - `pong`：心跳响应。

## Docker 部署

> **说明：本机没有 Docker，以下 Docker 配置未在本机验证**，仅做过 YAML 语法与静态路径检查。首次构建镜像会下载全部 Python / Node 依赖，耗时较长。

前置条件：安装 Docker（Docker Desktop 或 Docker Engine + Compose v2，`docker compose version` 可用；旧版命令为 `docker-compose`）。

### 1. 准备环境变量

在项目根目录准备 `.env`（同「快速开始」第 2 步），至少填写 `LLM_API_KEY`；`docker compose` 会自动用它做变量替换。

### 2. 预先构建向量索引

容器启动时**不会**构建索引，`knowledge_base/`（含 `faiss_index/`）通过 volume 挂载进容器，因此需要在宿主机先构建：

```bash
cd backend
python rag/ingest.py --path ../knowledge_base --output ../knowledge_base/faiss_index
```

- compose 把 `./knowledge_base` 挂载到容器 `/app/knowledge_base`，而容器内 `VECTOR_INDEX_PATH=knowledge_base/faiss_index`（相对后端工作目录 `/app`），实际读取 `/app/knowledge_base/faiss_index`——容器内是纯英文路径，规避了 faiss C 层不支持中文目录的问题。
- 若宿主机项目路径含中文导致 faiss 写入失败，可先构建到纯英文目录，再把索引产物复制到挂载目录（复制由操作系统完成，不受 faiss 中文路径限制）：

  ```bat
  cd backend
  python rag/ingest.py --path ../knowledge_base --output E:\ai_classroom_index
  mkdir ..\knowledge_base\faiss_index
  copy E:\ai_classroom_index\index.faiss ..\knowledge_base\faiss_index\
  copy E:\ai_classroom_index\index.pkl ..\knowledge_base\faiss_index\
  copy E:\ai_classroom_index\chunks.txt ..\knowledge_base\faiss_index\
  ```

### 3. 启动与访问

```bash
# 项目根目录执行
docker compose up -d          # 旧版 Docker：docker-compose up -d
```

| 服务 | 地址 | 说明 |
|------|------|------|
| frontend | <http://localhost> | Nginx 托管前端静态资源，并反向代理 `/api`、`/ws` 到 backend |
| backend | <http://localhost:8000/api/health> | FastAPI 后端 |
| redis | `localhost:6379` | 供 WebSocket 断线消息缓冲使用 |

停止并移除容器：`docker compose down`（旧版：`docker-compose down`）。

### 4. 首次启动注意事项

- 容器内首次检索会下载 `bge-large-zh` 嵌入模型（约 1.2GB）；下载缓存位于容器层，**重建容器需要重新下载**。
- `docker-compose.yml` 已把 `HF_ENDPOINT` 透传到 backend 容器（默认 `https://hf-mirror.com`，可在 `.env` 覆盖为其他镜像端点）；基础镜像额外安装了 `libgomp1`，避免 sentence-transformers/torch 在 slim 镜像中因缺 `libgomp.so.1` 导致 RAG 静默降级。
- RAG 预热在后台线程进行，后端接口可先访问；模型 / 索引加载期间检索请求会等待预热完成，加载失败则自动走空检索兜底。
- 镜像构建上下文：`backend/`（`COPY requirements.txt`，即 `backend/requirements.txt`）与 `frontend/`（Node 构建后由 Nginx 托管），`backend/.dockerignore`、`frontend/.dockerignore` 已排除缓存与大文件。

## 已知限制

- 知识库目前仅含 1 份示例教材（`knowledge_base/高等数学_极限与连续.txt`），检索质量受语料规模限制。
- 默认仅 4 个学生 Agent（`introvert` / `active` / `expert` / `follower` 四种性格），可通过 `.env` 的 `STUDENT_PERSONALITIES` 调整。
- LLM 输出为非流式：每条消息整体生成后再通过 WebSocket 推送（消息级实时，非 token 级流式）。
- 前端「答题 / 作业」面板为骨架占位，尚未接入答题交互。
- 三层记忆为进程内实现，进程 / 容器重启后丢失（生产可序列化到 Redis）。
- 未配置 Redis 时，断线消息缓冲仅在单进程内有效；多 worker 部署需配置 Redis。
- 会话 id 未持久化到 URL / 浏览器存储，刷新页面会丢失当前课堂视图，需要重新发起授课。
- 暂无鉴权、多班级、权限体系，定位为 Demo 演示，不建议直接用于真实教学场景。

## 相关文档

- [项目设计文档（架构、关键技术、踩坑复盘、性能数据）](../AI_Classroom_多智能体互动教学课堂_项目文档.md)
