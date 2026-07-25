"""log_utils + 访问日志中间件测试（R1 新能力：openwebui-lite 可观测性）。"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from log_utils import setup_logging, capture_logs
import main
from fastapi.testclient import TestClient


def test_setup_logging_writes_to_file(tmp_path):
    log_file = tmp_path / "srv.log"
    setup_logging("INFO", log_file=str(log_file))
    logging.getLogger("openwebui").info("服务启动")
    assert log_file.exists()
    assert "服务启动" in log_file.read_text(encoding="utf-8")


def test_capture_logs_captures_records():
    with capture_logs() as buf:
        logging.getLogger("openwebui").warning("slow")
    assert "slow" in buf.getvalue()
    assert "WARNING" in buf.getvalue()


def test_access_log_middleware_logs_request():
    # R1 验收：任何请求都应被访问日志中间件记录（方法 + 路径 + 状态码 + 耗时）
    os.environ["MOCK_LLM"] = "1"
    client = TestClient(main.app)
    with capture_logs() as buf:
        r = client.get("/api/version")
        assert r.status_code == 200
    assert "/api/version" in buf.getvalue()
