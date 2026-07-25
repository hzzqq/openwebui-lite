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


def test_script_syntax_valid():
    # 若环境有 node，做语法校验；否则跳过（不视为失败）
    node = subprocess.run(
        ["node", "--check", "-"], input=_script().encode("utf-8"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if node.returncode == 127:
        import pytest
        pytest.skip("node 不可用，跳过 JS 语法校验")
    # 非零且非 127 => 语法错误
    assert node.returncode == 0, "index.html 的 <script> 存在语法错误"
