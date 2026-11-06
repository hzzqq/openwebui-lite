#!/usr/bin/env bash
# OpenWebUI Lite —— 本地 LLM 聊天前端
# 默认 MOCK_LLM=1 离线演示（无需 Ollama）；接本地模型：取消该行即可
# 依赖：pip install fastapi uvicorn[standard] httpx
export MOCK_LLM=1
# 端口：优先读 .port.env（port-doctor 分配），缺失则回退默认端口
if [ -f .port.env ]; then set -a; . ./.port.env; set +a; fi
PORT="${PORT:-8000}"

uvicorn main:app --reload --port "$PORT"
