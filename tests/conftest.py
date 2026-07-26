"""测试隔离（R2 修复：避免复用被其它运行污染的 on-disk sessions.db）。

原 test_api.py 直接复用模块同目录的 sessions.db；verify_all 冒烟或重复运行后
该文件可能残留历史会话/消息，使依赖全局计数（list_sessions / detail / pagination
等）的断言偶发失败（如 assert 11 == 10）。这里把 OPENWEBUI_DB_PATH 指向每进程
唯一的临时库，保证每次测试进程都从干净状态开始（与 CI 中独立 pytest 运行一致）。
"""

import os
import tempfile

_TMP_DB = os.path.join(tempfile.gettempdir(), f"openwebui_test_{os.getpid()}.db")
os.environ["OPENWEBUI_DB_PATH"] = _TMP_DB
for _suf in ("", "-wal", "-shm"):
    try:
        os.remove(_TMP_DB + _suf)
    except OSError:
        pass
