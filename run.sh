#!/usr/bin/env bash
# OpenWebUI Lite —— 本地 LLM 聊天前端
# 默认 MOCK_LLM=1 离线演示（无需 Ollama）；接本地模型：取消该行即可
# 依赖：pip install fastapi uvicorn[standard] httpx
export MOCK_LLM=1
uvicorn main:app --reload --port 8000
