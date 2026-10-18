# AI Classroom 常用命令（Windows 下请在 Git Bash / PowerShell 中执行）
# 环境：项目根目录

.PHONY: install ingest up down dev-backend dev-frontend clean

# 安装依赖
install:
	pip install -r backend/requirements.txt
	cd frontend && npm install

# 构建知识库索引（将教材 PDF 放入 knowledge_base/ 后执行）
ingest:
	cd backend && python rag/ingest.py --path ../knowledge_base

# Docker 一键启动
up:
	docker-compose up -d

# 停止并移除容器
down:
	docker-compose down

# 开发模式：后端
dev-backend:
	cd backend && uvicorn main:app --reload --port 8000

# 开发模式：前端
dev-frontend:
	cd frontend && npm run dev

# 清理 Python/Node 缓存
clean:
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf frontend/dist
