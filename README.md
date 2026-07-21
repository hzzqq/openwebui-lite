# OpenWebUI Lite

对接本地 [Ollama](https://ollama.com) 的轻量 LLM 聊天前端 MVP（极简版 Open WebUI）。
纯 Python + FastAPI 单文件后端，搭配一个内联 CSS/JS 的单页前端，**无前端构建步骤**。

## 功能

- **模型列表**：前端加载时调用 Ollama `/api/tags` 拉取可用模型，下拉选择；连不上则回退默认模型名。
- **流式对话（SSE）**：`POST /api/chat` 接收 `{model, messages}`，转发到 Ollama `/api/chat`（`stream=true`），增量 token 通过 SSE 逐字推送到前端渲染。
- **历史会话**：服务端内存保存多轮 `messages`，支持「新对话 / 清空」。
- **离线演示模式**：设置 `MOCK_LLM=1` 时不连 Ollama，直接以 SSE 分片返回一段预设中文流式文本，无模型也能演示 UI 与流式效果。
- **UI**：中文界面，含模型选择、消息区、输入框、发送/新对话按钮，深色设计风格。

## 运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动（默认 8000 端口）
uvicorn main:app --reload

# 3. 打开浏览器
#    http://localhost:8000
```

## Ollama 配置

- 确保本地已启动 Ollama，默认地址 `http://localhost:11434`。
- 若 Ollama 在其他地址，设置环境变量：`OLLAMA_HOST=http://<ip>:11434`。
- 拉取一个模型示例：`ollama pull qwen2`，启动：`ollama run qwen2`。
- 前端右上角下拉框会自动列出 `/api/tags` 的模型；若连不上则给默认模型名下拉，可直接选用。

## MOCK 模式（离线演示）

无需安装/启动 Ollama，也能演示完整 UI 与流式效果：

```bash
# Windows PowerShell
$env:MOCK_LLM="1"
uvicorn main:app --reload

# Linux / macOS
MOCK_LLM=1 uvicorn main:app --reload
```

开启后：
- 模型下拉显示 `mock-model (离线演示)` 及默认列表。
- 任意发送消息都会收到一段预设中文回复，按字分片经 SSE 逐字渲染。
- 顶部状态显示「MOCK 离线模式」（黄色）。

## 接口一览

| 方法 | 路径           | 说明                                          |
|------|----------------|-----------------------------------------------|
| GET  | `/`            | 托管单页前端 `static/index.html`              |
| GET  | `/api/models`  | 返回可用模型列表（`{models, mock}`）          |
| POST | `/api/chat`    | 流式 SSE 对话，body：`{model, messages}`      |
| GET  | `/api/history` | 返回当前会话历史 `{session_id, messages, model}` |
| POST | `/api/new`     | 新对话（清空内存历史）                        |
| POST | `/api/clear`   | 清空历史（同 new）                            |

## 目录结构

```
openwebui-lite/
├── main.py            # FastAPI 应用（含全部接口 + 前端托管）
├── static/
│   └── index.html     # 单页前端（内联 CSS/JS）
├── requirements.txt
└── README.md
```

> 说明：MVP 采用内存存储会话，重启进程后历史清空。如需持久化可替换为 SQLite。

## 近期迭代（自驱动开发 10 轮）

- 接入 **SQLite 会话持久化**（`db.py`）：对话历史进程重启后不丢失（修复了 DML 未 commit 导致持久化失效的 bug）
- 前端增强：**停止生成**（AbortController 中断流式）、**自动标题**（取首条消息）、生成中状态指示
- 保留 `MOCK_LLM=1` 离线演示模式（无需 Ollama 即可体验完整 UI 与流式）
- 附 `run.sh` / `run.bat` 一键启动（默认 MOCK 演示）
