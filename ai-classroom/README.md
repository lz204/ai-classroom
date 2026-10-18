# AI Classroom 多智能体互动教学课堂

基于 **FastAPI + LangGraph** 的多智能体互动教学课堂 Demo：AI 主讲老师、助教、4 名性格不同的学生 Agent 与课堂管理员在同一课堂状态机中协同，完成「授课 → 提问 → 随堂测验（出题 / 作答 / 讲评 / 批改）→ 小组研讨 → 作业布置 → 学情档案」全流程；授课内容由本地教材知识库 RAG 检索生成，尽量还原真实课堂的互动与秩序感。P1 版本补齐了随堂测验闭环、性格互补分组研讨、课堂超时管控、学情档案、消息级流式分帧（前端打字机）与可选的 Redis 持久化。

> 完整的架构图、关键实现与踩坑复盘见设计文档：[《AI Classroom 多智能体互动教学课堂》项目文档](../AI_Classroom_多智能体互动教学课堂_项目文档.md)。

## 架构要点

```
Vue3 + Vite ──REST / WebSocket──► FastAPI 后端 ──► LangGraph StateGraph
                                                      ├─ teacher  主讲授课 / 出题 / 答疑
                                                      ├─ student  学生群（节点内并发提问 / 答题 / 分组研讨）
                                                      ├─ arbiter  发言仲裁与排序
                                                      ├─ ta       随堂测验批改
                                                      └─ manager  秩序管控 / 学情总结
                                                           │
                                          RAG：FAISS 向量 + BM25 关键词（+ 可选重排）
                                          记忆：滑窗 + LLM 摘要 + 实体关联（三层）
                                          存储：SessionStore（Redis 可选 / 进程内降级 + TTL）
```

- **后端**：FastAPI 提供 REST 接口（会话创建 / 状态查询 / 学情档案查询）与 WebSocket 实时推送；课堂会话、LangGraph 状态机、消息缓冲与学情档案由 `classroom.py` / `store.py` 统一管理。
- **编排**：LangGraph `StateGraph` 共 5 类节点 `teacher / student / arbiter / ta / manager`；条件路由以单调递增的 `round` 计数为唯一依据，`phase` 仅用于前端展示，避免课堂顺序来回跳变。课堂固定 7 轮（r1 讲解、r2 测验+作答、r3 讲评+追问、r4 分组研讨、r5 批改、r6 布置作业+学情总结、r7 学情总结）。属**固定工作流编排**（节点与路由由代码固定，LLM 只负责内容生成），不等同于自主 Agent。告警由 manager 处理完后按 `resume_target` 回到原定节点，**不会吞掉轮次**（回归测试 `tests/test_smoke.py` 第 7 组可复现：全部学生超时场景老师仍为 7 轮）。
- **RAG**：`backend/rag/ingest.py` 解析教材（txt / md / pdf / docx）并建 FAISS 索引；检索时 FAISS 向量召回 + BM25 关键词召回（jieba 分词）混合去重，重排模型未缓存时自动跳过，空检索时课堂照常进行。
- **随堂测验**：r2 老师随消息发布结构化 `quiz`（4 个选项、`answer_index` 0-3、`knowledge_point`、`quiz_id`），r2 学生逐个作答并回传 `answer`（`student_id / quiz_id / choice_index / choice_text`），r5 助教批改（`target` 含题目、正确答案与学生作答），前端 `QuizPanel` 展示题目、各选项人数与学生作答，正确率在学情档案中展示。
- **性格互补分组**：`backend/graph/grouping.py::build_groups` 按 expert+introvert、active+follower 互补配对，默认 2 组（`STUDENT_GROUP_COUNT`），讨论角度为「概念辨析 / 例题应用 / 拓展探究N」；r4 学生消息携带 `group` 字段，前端展示分组徽章。
- **课堂超时管控**：teacher 每轮检查 `STAGE_TIMEOUT_SECONDS`（默认 300s）与 `CLASS_TIMEOUT_SECONDS`（默认 1800s），超限产生 `overtime` / `class_timeout` 告警，由 manager 收束；学生 LLM 调用超时（30s）产生 `timeout` 告警。
- **学情档案**：课堂结束由 `backend/analytics.py::LearningProfile` 按规则统计生成，先于 `end` 帧下发 WS `profile` 帧并持久化，同时提供 `GET /api/classroom/{session_id}/profile` 查询；前端 `ProfileCard` 展示课堂概览、学生表现、测验正确率、知识点 / 薄弱点与小组统计。
- **流式输出**：每条逻辑消息拆成 `message_start → message_delta* → message_end` 三类帧，共用同一个自增 id；默认 12 字/片、片间 0.02s（仅 WS 在线时按节奏发送），前端按 id 聚合、逐帧追加并显示 CSS 打字机光标。属**消息级分片**，不是 LLM token 级流式输出。
- **对话记忆**：短期滑窗 + LLM 摘要压缩 + 实体（章节号 / 定理名等）关联，注入老师 Prompt；`LayeredMemory.to_dict()` 会随每轮写入 `classroom:{session_id}:memory` 记忆快照。
- **Redis 持久化（可选）**：`backend/store.py::SessionStore`，配置 `REDIS_URL` 时持久化会话状态 / 消息缓冲 / 学情档案 / 记忆快照，未配置或连接失败自动降级为进程内存储（打印 warning），TTL 默认 3600s（`CLASSROOM_TTL`，追加消息会续期）。
- **WebSocket 断线续传**：前端 30s 心跳（`ping` / `pong`）；每条帧先入缓冲、再尝试推送，断线重连时携带 `last_msg_id`，后端只补发未收到的帧。`lastMsgId` 只在 `message_end` / `alert` / `end` / `error` / 旧 `message` 帧上推进，半条流式消息会在重连后从 `message_start` 重发，前端按 id 重置内容、不重影。补发与实时推送共用会话级 `asyncio.Lock` 串行化；「开始授课」指令幂等；会话不存在时服务端先 accept 再 close(4004)，前端停止自动重连（自动重连上限 5 次）。
- **Mock LLM**：未配置 `LLM_API_KEY` 时自动降级为内置 Mock LLM，无 Key 也能跑通完整课堂流程（Mock 学生固定选 B 作答），便于本地演示与测试。
- **代码规模（按非空行统计）**：后端 Python 共 3258 行（其中 tests 718 行：`test_smoke.py` 290 + `test_store.py` 428），核心约 2540 行；前端 `src` 共 1538 行。

## 开发方式与个人贡献

- 项目 2026-08 有一个基于智谱 GLM 的原型版本；2026-10-19 使用 AI 编码助手重构为阿里百炼 DashScope/Qwen，并补齐 P0/P1 能力（断线续传、随堂测验、分组研讨、流式分帧、学情档案、Redis 持久化）。
- 本人负责方案设计与技术选型、核心实现与联调、测试与性能自测、文档与部署配置验收；AI 用于编码加速与 Code Review 辅助，产出代码由本人逐模块阅读、修改并跑回归验证，可逐行讲解 / 现场修改复跑。
- 前端基于 Vue3 + Vite + Element Plus。**Git 历史只覆盖 2026-10-19 的重构（5 个 commit）**，2026-08 的原型开发不在仓库历史中；Docker 配置已编写，但本机无 Docker，未实机部署验证。

## 目录结构

```
ai-classroom/
├── backend/                    # FastAPI 后端
│   ├── main.py                 # 入口：REST 路由 + WebSocket 端点（心跳 / 断线续传 / 4004）
│   ├── classroom.py            # 课堂会话、流式分帧推送、断线补发、profile 下发
│   ├── config.py               # 配置加载（从项目根 .env 读取）
│   ├── llm_client.py           # LLM 客户端（DashScope OpenAI 兼容 / 内置 Mock）
│   ├── analytics.py            # 学情档案 LearningProfile（规则统计）
│   ├── store.py                # SessionStore：Redis 持久化 / 进程内降级 + TTL
│   ├── agents/                 # teacher / student / ta / manager 各角色 Agent
│   ├── graph/                  # 状态机、分组、发言队列与仲裁器
│   │   ├── workflow.py         # 7 轮闭环、round 路由、resume_target、测验 / 分组
│   │   ├── grouping.py         # 性格互补分组 build_groups
│   │   └── speaker_queue.py    # 发言队列与 arbiter 仲裁器
│   ├── rag/                    # ingest.py 建索引、retriever.py 混合检索
│   ├── memory/                 # 三层对话记忆（滑窗 / 摘要 / 实体）
│   ├── api/routes.py           # REST 路由（含 /profile）
│   ├── tests/
│   │   ├── test_smoke.py       # P0/P1 冒烟回归（8 组，Mock LLM）
│   │   └── test_store.py       # 存储层单测（11 项）
│   ├── requirements.txt        # 后端依赖清单
│   └── Dockerfile
├── frontend/                   # Vue3 + Vite + Element Plus 前端
│   ├── src/
│   │   ├── views/Classroom.vue             # 课堂主界面
│   │   ├── components/                     # MessageBubble / QuizPanel / ProfileCard
│   │   ├── composables/useMessageStream.js # 流式帧按 id 聚合
│   │   ├── utils/format.js                 # 阶段 / 性格 / 时长 / 选项格式化
│   │   └── websocket/index.js              # WS 封装（心跳 / 重连 / last_msg_id）
│   ├── Dockerfile
│   └── nginx.conf              # Nginx 静态资源 + API/WS 反向代理配置
├── knowledge_base/             # 教材原始资料（示例教材：高等数学_极限与连续.txt）
├── docker-compose.yml          # backend + frontend + redis 一键部署（本机未验证）
├── Makefile                    # 常用命令
├── .env.example                # 环境变量模板（复制为 .env 后填写）
└── README.md
```

## 环境要求

| 组件      | 要求        | 说明                                                                                                                        |
| ------- | --------- | ------------------------------------------------------------------------------------------------------------------------- |
| Python  | 3.10+     | 本项目自带虚拟环境 `.venv`（Python 3.13）；Windows 激活命令 `.venv\Scripts\activate`                                                      |
| Node.js | 18+       | 前端构建（Docker 镜像使用 `node:20-alpine`）；本机实测 Node 24                                                                         |
| Redis   | 可选        | 配置 `REDIS_URL` 时持久化会话状态 / 消息缓冲 / 学情档案 / 记忆快照；**未配置或连接失败时自动降级为进程内存储**（单进程可用，重启丢失）                                                 |
| 网络 / 模型 | 首次需下载嵌入模型 | `bge-large-zh` 约 1.2GB（CPU 可跑，无需 GPU；热检索实测 203–241ms/次）；可选重排模型 `bge-reranker-large` 约 2.1GB，未缓存时自动跳过                                         |
| 索引路径    | 纯英文路径     | faiss 的 C 层读写不支持中文目录，而本项目路径含「ai实验」，故 `VECTOR_INDEX_PATH` 必须指向纯英文目录（如 `E:\ai_classroom_index`）                            |
| 测试机     | i7-6700HQ / 11.8GB RAM / 无 GPU / Windows | 本项目 README 与设计文档中的实测数据均来自该环境（Python 3.13 + `.venv`、Node 24）                                                            |

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
- `LLM_TIMEOUT`：LLM 单次调用超时（秒，默认 60s）。
- `VECTOR_INDEX_PATH`：**必须是纯英文绝对路径**（例如 `E:\ai_classroom_index`）。faiss C 层不支持中文目录，而本项目路径含「ai实验」。
- `EMBEDDING_MODEL` / `RERANKER_MODEL`：默认 `BAAI/bge-large-zh` / `BAAI/bge-reranker-large`。
- `REDIS_URL`：**可选**。留空即使用进程内存储（会话状态 / 消息缓冲 / 学情档案 / 记忆快照），重启或跨进程查询会丢失；填写后走 Redis 持久化。本机没有 Redis 时请留空。
- `CLASSROOM_TTL`：会话状态 / 消息缓冲 / 学情档案在存储中的 TTL（秒，默认 3600）。
- `STUDENT_PERSONALITIES` / `STUDENT_GROUP_COUNT`：学生性格列表（默认 `introvert,active,expert,follower` 4 人）与研讨分组数（默认 2 组）。
- `STAGE_TIMEOUT_SECONDS` / `CLASS_TIMEOUT_SECONDS`：单教学环节 / 整堂课超时阈值（秒，默认 300 / 1800）。
- `STREAM_CHUNK_SIZE` / `STREAM_INTERVAL_SECONDS`：流式分片大小（默认 12 字）与片间延迟（默认 0.02s，设为 0 可关闭打字节奏）。
- `BACKEND_PORT`：后端监听端口（默认 8000）。
- 真实的 Key 只放在 `.env`（已被 `.gitignore` 忽略），不要写入 `.env.example` 或提交到仓库。

### 3. 构建知识库索引

在 `backend/` 下执行（脚本会读取 `knowledge_base/` 下的 txt / md / pdf / docx，按章节切分后写入 FAISS 索引）：

```bash
cd backend
python rag/ingest.py --path ../knowledge_base
```

- 当前仓库 `knowledge_base/` 仅含 1 份示例教材《高等数学_极限与连续.txt》（42 行 / 7141 字节），构建后为 10 个 chunks。
- 索引输出到 `.env` 中的 `VECTOR_INDEX_PATH`，同目录还会生成 `chunks.txt` 供 BM25 检索使用。
- 首次运行会下载 `bge-large-zh` 嵌入模型（约 1.2GB），CPU 首次加载约 52s；首次检索需额外建立 BM25 / jieba 索引，约 8.3s。请保证网络可用（可在 `.env` 设置 `HF_ENDPOINT=https://hf-mirror.com` 使用国内镜像）。
- 若想显式指定输出目录，可加 `--output`，例如 `python rag/ingest.py --path ../knowledge_base --output ../knowledge_base/faiss_index`（注意输出目录同样不要包含中文路径）。
- 不建索引也能启动：RAG 会走空检索兜底，只是失去教材引用能力（课堂流程照常跑完）。

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
2. 进入课堂后前端自动连接 WebSocket 并发送 `start`，实时展示老师 / 学生 / 助教 / 管理员发言、随堂测验面板与秩序告警。
3. 课堂按固定 7 轮推进：老师讲解 → 随堂测验（学生作答、面板实时更新）→ 讲评与追问 → 分组研讨 → 助教批改 → 布置作业 → 学情总结，结束后先收到学情档案（`profile`）再收到结束帧（`end`），页面下方展示学情档案卡片。
4. 网络断开后前端会自动重连（指数退避 1/2/4/8s，最多 5 次）并按 `last_msg_id` 补发消息；会话不存在（服务端 close 4004）时停止重连并提示重新发起授课。当前版本会话 id 未持久化，刷新页面会丢失课堂视图，需重新发起授课。

### Makefile 常用命令（项目根目录执行）

| 命令                      | 作用                                                                                   |
| ----------------------- | ------------------------------------------------------------------------------------ |
| `make install`          | 安装后端依赖（`pip install -r backend/requirements.txt`）与前端依赖（`cd frontend && npm install`） |
| `make ingest`           | 构建知识库索引（`cd backend && python rag/ingest.py --path ../knowledge_base`）               |
| `make dev-backend`      | 启动后端（`cd backend && uvicorn main:app --reload --port 8000`）                          |
| `make dev-frontend`     | 启动前端（`cd frontend && npm run dev`）                                                   |
| `make up` / `make down` | Docker Compose 启动 / 停止（本机未验证）                                                         |
| `make clean`            | 清理 Python / Node 缓存（`find`、`rm` 命令，建议在 Git Bash 中执行）                                 |

## 测试

回归测试使用内置 Mock LLM，**不需要真实 API Key、不需要向量索引、不需要 Redis**，几秒内可跑完：

- `backend/tests/test_smoke.py`：8 组用例——SessionStore 消息缓冲语义、Mock LLM 下完整课堂流程（流式帧协议、7 轮 phase、随堂测验、分组研讨、学情档案、重复 start 幂等）、断线续传不丢尾消息、补发只发给当前连接（多连接不串台）、补发与实时推送串行化（不乱序）、发送异常不中止课堂、全部学生超时（告警不吞轮次、老师仍 7 轮）、环节超时告警（overtime）。
- `backend/tests/test_store.py`：11 项存储层单测——JSON CRUD、会话 meta / profile、消息追加与副本、单会话清理、TTL 过期、追加续期、purge_expired、Redis 未配置降级、Redis 连接失败降级（含 warning）、Redis 成功路径（key / TTL / pipeline）、LayeredMemory 快照序列化。

```bash
cd backend
python tests/test_smoke.py     # 预期最后一行：ALL P0/P1 REGRESSION TESTS PASSED
python tests/test_store.py     # 预期最后一行：ALL P1 STORE TESTS PASSED
```

前端构建验证：`cd frontend && npm run build`（本机 Vite 输出约 11.9s、1602 modules；产物 JS 约 1.0MB（gzip 约 344KB）/ CSS 约 368KB（gzip 约 50KB），构建通过）。

性能基线（本机实测；Mock 与真实 API 两种口径，复现命令见项目文档附录 B.9）：

- **Mock（4 学生、25 条逻辑消息）**：关闭打字节奏 4.06s，`STREAM_INTERVAL_SECONDS=0.02` / 12 字每片时 9.98s；单堂课 192 个 WS 帧（25 start + 140 delta + 25 end + profile + end）、消息缓冲 30,775 B（约 30.8KB/堂）。
- **真实 API + RAG 全链路（P1 单次冒烟）**：阿里百炼 `qwen-plus` + 本地 FAISS 索引与 `bge-large-zh`（跳过重排模型），`STREAM_INTERVAL_SECONDS=0`，4 学生完整课堂约 **162.5s / 25 次 LLM 调用 / 616 个 WS 帧**；老师 7 轮 phase 正确、r2 结构化测验解析成功、4 名学生作答全部正确（accuracy=1.0）、2 个互补分组、学情档案完整，RAG 引用命中教材章节（第1章 1.2/1.5、第2章 2.1/2.3 等）。**该组数据为本地单次冒烟、无留存日志、非统计结论**；Mock 与真实 API 口径不同，不要混用。

## API / WebSocket 协议简表

### REST

| 方法     | 路径                                       | 说明                                                                                                 |
| ------ | ---------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `GET`  | `/api/health`                            | 健康检查，返回 `{"status": "ok"}`                                                                         |
| `POST` | `/api/classroom/start`                   | 创建课堂会话；请求体 `{"topic": "授课主题"}`，返回 `{"session_id": "...", "topic": "..."}`                         |
| `GET`  | `/api/classroom/{session_id}`            | 查询会话状态（`running` / `round` / `phase` / `msg_count` / `message_count` / `profile_ready` 等；本进程无会话时回落到存储层快照） |
| `GET`  | `/api/classroom/{session_id}/profile`    | 查询学情档案（课堂结束后生成；进程内或持久化存储中查询，未生成 / 会话不存在返回 404）                                                  |

另有 `GET /` 返回服务入口说明，`GET /docs` 为 OpenAPI 文档。

### WebSocket

连接地址：`/ws/{session_id}?last_msg_id=0`

- `last_msg_id`：已收到的最大**完整帧** id。后端会补发该 id 之后缓冲区中的全部帧；传 `0`（例如页面刷新后前端从 0 重新开始）则补发缓冲区中的全部历史帧。缓冲整堂课保留、不裁剪（受 `CLASSROOM_TTL` 过期约束）。
- `lastMsgId` 推进规则：前端只在 `message_end` / `alert` / `end` / `error` / 旧 `message` 帧上推进，`message_start` / `message_delta` / `profile` 不推进——断线时若半条流式消息只收到部分分片，重连后服务端会从该消息的 `message_start` 完整重发。
- 客户端 → 服务端：
  - `{"type": "ping"}`：心跳（前端每 30s 一次），服务端回 `{"type": "pong"}`；
  - `{"type": "start"}`：启动课堂流程（异步执行，不阻塞消息接收；重复发送幂等）。
- 服务端 → 客户端（除 `pong` 外均带自增 `id`）：

| 帧 `type`        | 说明                                                                                        |
| -------------- | ----------------------------------------------------------------------------------------- |
| `message_start` | 一条逻辑消息开始，携带 `speaker`、`speaker_name`、`citation`、`quiz`、`answer`、`group`、`phase`、`round`、`id` |
| `message_delta` | 流式分片，携带 `id` + `delta`（默认 12 字/片）                                                         |
| `message_end`   | 消息结束，携带完整 `content` 与元数据（前端据此收尾气泡、推进 `lastMsgId`）                                         |
| `message`       | 兼容旧协议的一次性完整消息帧（当前后端不再产生，前端保留兼容处理分支）                                                                    |
| `profile`       | 学情档案（课堂结束后先于 `end` 帧下发）                                                                  |
| `alert`         | 秩序告警，`alert.type` 为 `timeout`（学生走神）/ `overtime`（环节超时）/ `class_timeout`（整堂课超时）                |
| `end`           | 课堂流程结束                                                                                    |
| `error`         | 课堂运行异常                                                                                    |
| `pong`          | 心跳响应                                                                                      |

- 告警字段：`timeout` 为 `{type, student}`；`overtime` 为 `{type, stage, elapsed, limit, round}`；`class_timeout` 为 `{type, elapsed, limit}`。
- 会话不存在时服务端先 `accept` 再 `close(code=4004, reason="会话不存在")`；前端收到 4004 后停止自动重连并提示重新发起授课。

## Docker 部署

> **说明：本机没有 Docker，以下 Docker 配置未在本机验证、未实机部署**，仅做过 YAML 语法与静态路径检查。首次构建镜像会下载全部 Python / Node 依赖，耗时较长。

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

| 服务       | 地址                                 | 说明                                                              |
| -------- | ---------------------------------- | --------------------------------------------------------------- |
| frontend | <http://localhost>                 | Nginx 托管前端静态资源，并反向代理 `/api`、`/ws` 到 backend                     |
| backend  | <http://localhost:8000/api/health> | FastAPI 后端                                                      |
| redis    | `localhost:6379`                   | 会话状态 / 消息缓冲 / 学情档案 / 记忆快照的持久化存储（可选依赖；未配置时后端自动降级为进程内存储）          |

停止并移除容器：`docker compose down`（旧版：`docker-compose down`）。

### 4. 首次启动注意事项

- 容器内首次检索会下载 `bge-large-zh` 嵌入模型（约 1.2GB）；下载缓存位于容器层，**重建容器需要重新下载**。
- `docker-compose.yml` 已把 `HF_ENDPOINT` 透传到 backend 容器（默认 `https://hf-mirror.com`，可在 `.env` 覆盖为其他镜像端点）；基础镜像额外安装了 `libgomp1`，避免 sentence-transformers/torch 在 slim 镜像中因缺 `libgomp.so.1` 导致 RAG 静默降级。
- RAG 预热在后台线程进行，后端接口可先访问；模型 / 索引加载期间检索请求会等待预热完成，加载失败则自动走空检索兜底。
- 镜像构建上下文：`backend/`（`COPY requirements.txt`，即 `backend/requirements.txt`）与 `frontend/`（Node 构建后由 Nginx 托管），`backend/.dockerignore`、`frontend/.dockerignore` 已排除缓存与大文件。
- compose 中 backend 通过环境变量 `REDIS_URL=redis://redis:6379` 连接 Redis；若 Redis 不可用，`SessionStore` 会打印 warning 并自动降级为进程内存储，课堂仍可跑，但重启后状态 / 档案 / 缓冲会丢失。

## 已知限制

- 知识库目前仅含 1 份示例教材（`knowledge_base/高等数学_极限与连续.txt`，42 行 / 7141 字节，索引后 10 chunks），检索质量受语料规模限制。
- 学生为 LLM 模拟，默认 4 个（`introvert` / `active` / `expert` / `follower` 四种性格），可通过 `.env` 的 `STUDENT_PERSONALITIES` 调整；学生发言与真实学生仍有差距。
- 流式输出是「消息级分片 + 前端打字机」，不是 LLM token 级流式（LLM 仍整条生成后再分片推送）。
- 学情档案为规则统计（非 LLM 生成），统计口径有限（如「提问数」按内容是否含问号统计，正确率仅在存在作答时计算）。
- 同一 `session_id` 仅保留最近一个 WebSocket 活动连接（Demo 按单客户端使用）；多客户端同时打开会互相接管连接。
- 会话 id 未持久化到 URL / 浏览器存储，刷新页面会丢失当前课堂视图，需要重新发起授课。
- 未配置 Redis 时，会话状态 / 消息缓冲 / 学情档案 / 记忆快照仅在单进程内有效，重启后丢失；配置 Redis 后仍以单进程课堂执行为前提（未做多 worker 调度）。
- 会话与缓冲带 TTL（`CLASSROOM_TTL` 默认 3600s，追加消息会续期），过期后无法再查询状态与档案；课堂运行中的活跃会话会持续续期。
- 记忆快照已写入 SessionStore，`LayeredMemory.from_dict` 具备反序列化能力，但当前课堂启动流程仍新建记忆实例，未自动从存储恢复。
- 举手 / 发言队列是调度器内部按「性格优先级 + 先来先到」排序学生发言（`SpeakerQueue`），并非真实学生的自主举手请求。
- 暂无鉴权、多班级、权限体系，定位为 Demo 演示，不建议直接用于真实教学场景；Docker 配置未在本机验证（本机无 Docker）。
- Mock 模式下学生固定选 B、学情档案数据为模拟值，不代表真实模型表现。
- 多智能体为**固定工作流编排**（**非自主 Agent**：节点与 `round` 路由由代码固定，LLM 只生成内容、不自主决定拓扑），不等同于 AutoGPT 类系统。

## 相关文档

- [项目设计文档（架构、关键技术、踩坑复盘、性能数据）](../AI_Classroom_多智能体互动教学课堂_项目文档.md)
