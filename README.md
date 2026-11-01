# AI Classroom 多智能体互动教学课堂 [![CI](https://github.com/lz204/ai-classroom/actions/workflows/ci.yml/badge.svg)](https://github.com/lz204/ai-classroom/actions/workflows/ci.yml)

> 2026 届计科毕业生 AI 应用 / AI Agent 方向面试项目：LangGraph 多智能体课堂 + RAG + Tool-Use Agent（Function Calling / MCP）工程 Demo。

## 项目一句话

AI 主讲老师、助教、4 名性格差异化学生 Agent 与课堂管理员在同一 LangGraph 状态机中完成「授课 → 提问 → 随堂测验 → 分组研讨 → 作业/学情」7 轮闭环；授课内容由本地教材知识库 RAG 生成。课堂外另有独立 Tool-Use Agent 模块，演示 Function Calling + ReAct 与 MCP 工具调用。

## 仓库结构

```
.
├── README.md                          # 本文件（GitHub 落地页）
├── AI_Classroom_..._项目文档.md        # 详细技术文档（架构 / 实现 / 评测口径 / 面试问答）
├── .github/workflows/ci.yml           # GitHub Actions CI（backend + frontend）
└── ai-classroom/                      # 项目主体
    ├── backend/                       # FastAPI + LangGraph + RAG + ToolAgent/MCP + eval + pytest
    ├── frontend/                      # Vue3 + Vite + Element Plus（课堂界面 / Agent 工具演示面板）
    ├── knowledge_base/                # 示例教材（1 份，索引后 10 chunks）
    ├── docker-compose.yml             # Docker Compose 配置（本机未实机验证）
    └── README.md                      # 运行 / 测试 / 部署详细说明
```

## 3 个可验证亮点

1. **7 轮课堂状态机**：`round` 单调路由 + manager 告警 `resume_target` 回跳；pytest 回归可复现「全部学生超时告警下老师仍为 7 轮」。
2. **WebSocket 流式 + 断线续传一致性**：帧先入缓冲再发送、`last_msg_id` 去重补发、会话级 `_send_lock` 串行化；并发回归用例覆盖「补发只发当前连接 / 补发与实时推送不乱序 / 发送异常不中止课堂」。
3. **Tool-Use Agent + 手写最小 MCP**：Function Calling + ReAct 循环（max_steps / 整体与单工具超时 / 重复动作检测 / 参数白名单 / 异常降级）；stdio JSON-RPC 2.0 的 MCP Server/Client，连接失败自动降级本地工具。另有 RAG/Agent 离线评测（`backend/eval/`）与 pytest 28 项 / 覆盖率 74% / ruff / CI。

## 快速开始（3 条命令）

```bash
# 1) 后端（演示模式：无 --reload，Windows 下 MCP 子进程可用）
cd ai-classroom && make demo-backend

# 2) 前端（另开终端）→ http://localhost:5173
cd ai-classroom && make dev-frontend

# 3) 回归测试 + 离线评测（Mock，无需 Key / 索引 / Redis）
cd ai-classroom && python -m pytest --cov && cd backend && python eval/run_all.py
```

> Windows 下 `uvicorn --reload` 会切换 SelectorEventLoop，MCP 子进程不可用（自动降级为本地工具、不崩）；演示 MCP 请用 `make demo-backend`。无 Key 时 LLM 自动走内置 Mock，全流程仍可跑通。

## 技术栈关键词

LangGraph / FastAPI / WebSocket / FAISS + BM25 / Redis（可降级）/ Vue3 + Vite + Element Plus / Function Calling + ReAct / MCP（stdio JSON-RPC 2.0）/ pytest + coverage + ruff / GitHub Actions CI

## 诚实边界

- **Demo 定位**：课堂主流程是固定工作流编排（非端到端自主 Agent）；ToolAgent 为独立演示模块，不接入课堂自动流程；无鉴权 / 多班级，按单客户端使用。
- **Docker**：Compose 配置已编写，本机无 Docker，未实机部署验证。
- **评测口径**：RAG 指标基于 1 份教材 / 10 chunks 演示语料，风险指标为词面重合启发式代理；Agent 评测为 Mock LLM + FakeRetriever（20 任务），只验证编排与工具链路——均不代表真实 LLM 或线上效果。
- **真实 API 性能数据**为本地单次冒烟、无留存日志、非统计结论。

## 文档入口

- [ai-classroom/README.md](ai-classroom/README.md)：安装、启动、测试与评测、Docker、API/WS 协议、已知限制
- [项目技术文档](AI_Classroom_多智能体互动教学课堂_项目文档.md)：架构图、关键技术实现、工程问题记录、性能与评测数据、面试问答
