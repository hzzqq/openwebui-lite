@echo off
REM OpenWebUI Lite —— 本地 LLM 聊天前端（默认 MOCK 离线演示）
set MOCK_LLM=1
uvicorn main:app --reload --port 8000
pause
