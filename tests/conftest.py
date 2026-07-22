"""测试隔离：将数据库重定向到每次运行独立的临时文件。

此前 openwebui-lite 的会话库默认落盘于模块同目录的 sessions.db，
跨运行复用会残留历史数据，偶发污染导致部分用例失败（开发库与测试库互相干扰）。
这里在导入 main 之前用 OPENWEBUI_DB_PATH 指向一次性临时库，保证每次
pytest 运行都从干净状态开始，且不影响开发用 sessions.db。
"""

import os
import tempfile
import uuid

_DB_PATH = os.path.join(tempfile.gettempdir(), f"openwebui_test_{uuid.uuid4().hex}.db")
os.environ["OPENWEBUI_DB_PATH"] = _DB_PATH

import db as _db  # noqa: E402  (在 main 导入前确保 env 生效)

_db.init()
