@echo off
REM OpenWebUI Lite —— 本地 LLM 聊天前端（默认 MOCK 离线演示）
where python >nul 2>nul || (echo [错误] 未检测到 python，请先安装 Python 并勾选 "Add to PATH"。 & pause & exit /b)
set MOCK_LLM=1
echo 正在启动 OpenWebUI Lite（MOCK 离线模式）...
echo 服务地址: http://localhost:8000
start "OpenWebUI-Lite-Server" cmd /k "uvicorn main:app --reload --port 8000"
timeout /t 5 >nul
start http://localhost:8000
echo 浏览器已打开 http://localhost:8000
echo 服务日志在上方 "OpenWebUI-Lite-Server" 窗口；关闭该窗口即可停止服务。
pause
