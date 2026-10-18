FROM python:3.11-slim

WORKDIR /app

# 安装后端依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# sentence-transformers / torch 运行时依赖 libgomp（slim 基础镜像默认不带），
# 缺失会导致 RAG 加载失败后静默降级为空检索
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 拷贝后端源码
COPY . .

EXPOSE 8000

# 启动 FastAPI 服务
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
