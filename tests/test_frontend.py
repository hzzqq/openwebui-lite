"""前端静态校验：确保 index.html 暴露了后端已具备但此前未接入的能力，
并修复了隐性 bug（stale emptyEl 引用）。

这些断言是确定性的、无 DOM 依赖的冒烟测试，配合 `node --check`（若可用）
对 <script> 做语法校验，保证前端改动不回退。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static" / "index.html"
HTML = STATIC.read_text(encoding="utf-8")


def _script() -> str:
    # 抽取 <script>...</script> 之间内容做语法检查
    start = HTML.find("<script>")
    end = HTML.find("</script>")
    assert start != -1 and end != -1, "未找到 <script> 块"
    return HTML[start + len("<script>"):end]


def test_regenerate_endpoint_wired():
    # R1：前端必须接入后端 /api/sessions/{id}/regenerate（c112 已提供但此前未暴露）
    assert "/regenerate" in HTML, "前端未接入重新生成端点"
    assert "function regenerate(" in HTML, "未定义 regenerate 函数"


def test_json_export_wired():
    # R1：前端应支持导出 JSON（与后端 /export?format=json 对称）
    assert "/export?format=json" in HTML, "前端未接入 JSON 导出"


def test_stale_empty_el_fixed():
    # R2：修复 stale emptyEl 引用——新对话后空状态占位无法隐藏的真实 bug
    assert "const emptyEl" not in HTML, "不应再保留模块级 emptyEl 变量"
    assert "if (emptyEl)" not in HTML, "不应再引用已失效的 emptyEl"
    assert "function hideEmpty(" in HTML, "应提供按 id 动态隐藏空状态的 hideEmpty"
    assert "hideEmpty();" in HTML, "addMessage/历史加载应调用 hideEmpty"


def test_accessibility_live_region():
    # R3：消息区应声明 aria-live，提升屏幕阅读器可用性
    assert 'aria-live="polite"' in HTML, "消息容器缺少 aria-live"


def test_copy_button_present():
    # R3：每条消息应提供复制按钮
    assert 'class = "copy"' in HTML or 'className = "copy"' in HTML, "消息缺少复制按钮"


def test_conversation_roles_use_assistant_contract():
    """R2 契约修复：conversation 入栈必须用后端允许的 role（assistant）。

    原实现把回复以 role:"bot" 入栈，第二轮 send() 整体 POST /api/chat 时被
    Pydantic 的 Literal["system","user","assistant"] 校验拒绝（422），且 "bot"
    永久留在 conversation，之后每次发送都 422，只能刷新页面恢复。
    """
    assert 'role: "bot", content' not in HTML, "conversation 不得入栈非法 role 'bot'"
    # 3 处 = send 成功 / send 手动中止（部分回复入栈，c165 R1）/ regenerate 成功
    assert HTML.count('conversation.push({ role: "assistant"') == 3, \
        "全部 assistant 入栈点（含 c165 中止保留分支）都必须使用后端合法 role"
    assert 'm.role === "assistant"' in HTML, "refreshRegenBtn 应按 assistant 判断（原 'bot' 判断使刷新后按钮永不出现）"


def test_backend_mock_stream_nonblocking():
    """R2：_mock_stream 在 async 生成器内必须用 await asyncio.sleep 让出控制权。

    阻塞的 time.sleep 会卡死整个事件循环：一次 mock 回复约 5 秒内所有并发
    请求（health/models/其他会话）全部停摆。
    """
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert "time.sleep(0.012)" not in src, "async 生成器内不得使用阻塞 time.sleep"
    assert "await asyncio.sleep(0.012)" in src, "应使用 await asyncio.sleep 让出事件循环"


def test_script_syntax_valid():
    # 若环境有 node，做语法校验；否则跳过（不视为失败）
    try:
        node = subprocess.run(
            ["node", "--check", "-"], input=_script().encode("utf-8"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        # R2 修复（Windows 可移植性）：POSIX 下找不到 node 返回 127，而
        # Windows 上 subprocess 直接抛 FileNotFoundError——原先未捕获，
        # 导致无 node 的 Windows 环境该用例硬失败而非按设计跳过。
        import pytest
        pytest.skip("node 不可用，跳过 JS 语法校验")
    if node.returncode == 127:
        import pytest
        pytest.skip("node 不可用，跳过 JS 语法校验")
    # 非零且非 127 => 语法错误
    assert node.returncode == 0, "index.html 的 <script> 存在语法错误"


def test_abort_keeps_partial_reply():
    """R1（c165）验证：手动停止生成必须保留已生成的部分回复，而非整段
    丢弃并把气泡覆盖为「连接异常：The user aborted a request.」。

    - send 链路：中止分支识别 AbortError，部分内容入栈 conversation 并
      persistAssistant 入库（与正常完成路径一致）；
    - regenerate 链路：中止分支提示「本次未保存，原回复已保留」（后端
      c164 起改为成功才删旧，刷新后回到完整旧回复，不产生双 assistant）。
    """
    assert HTML.count('e.name === "AbortError"') == 2, \
        "send 与 regenerate 的 catch 均应识别 AbortError（手动停止）"
    assert "已手动停止，以上为已生成的部分回复" in HTML, \
        "send 中止后应保留已生成部分并明确提示"
    assert "已停止（尚未生成内容）。" in HTML, \
        "无任何生成内容时中止也应给出明确提示"
    assert "本次未保存，原回复已恢复" in HTML, \
        "regenerate 中止应说明未保存且本地已恢复原回复（c166）"
    # acc 必须在 try 外声明（否则 catch 分支拿不到已生成部分）
    assert HTML.count("  let acc = \"\";\n\n  try {") == 2, \
        "send/regenerate 的 acc 应提升到 try 外声明"


def test_regenerate_restores_old_reply_on_failure():
    """R2（c166）：regenerate 失败/中止/连接异常路径必须本地补回旧回复
    （否则下一次 send 整体覆盖保存会把它从 DB 抹掉）。"""
    assert "const removedMsg = conversation[idx];" in HTML
    assert "conversation.push(removedMsg);" in HTML
    assert "let sawError = false;" in HTML
    assert "原回复已恢复" in HTML
    assert "刷新可见" not in HTML, "旧文案「刷新可见」应替换为「已恢复」语义"


def test_delete_current_session_guards_and_sync():
    """R2（c166）：删除当前会话需 streaming 守卫，且同步到后端已重建的会话
    （不再调 newChat 二次建孤儿空会话）。"""
    assert "正在生成回复，请先停止后再删除当前会话" in HTML
    assert "async function reloadCurrentSession()" in HTML
    assert "if (s.id === currentSessionId) {" in HTML
    assert "if (s.id === current) newChat();" not in HTML
