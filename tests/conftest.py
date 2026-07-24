"""测试隔离：将数据库重定向到每次运行独立的临时文件。

此前 openwebui-lite 的会话库默认落盘于模块同目录的 sessions.db，
跨运行复用会残留历史数据，偶发污染导致部分用例失败（开发库与测试库互相干扰）。
这里在导入 main 之前用 OPENWEBUI_DB_PATH 指向一次性临时库，保证每次
pytest 运行都从干净状态开始，且不影响开发用 sessions.db。
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

# 在导入 db/main 之前把项目根目录加入 sys.path，否则 `import db` 在 conftest
# 加载阶段就会因路径未设置而 ModuleNotFoundError（R2 修复：测试隔离 conftest
# 比测试文件更早执行，原先依赖测试文件自行加路径，导致收集期即失败、整组用例
# 无法运行）。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_DB_PATH = os.path.join(tempfile.gettempdir(), f"openwebui_test_{uuid.uuid4().hex}.db")
os.environ["OPENWEBUI_DB_PATH"] = _DB_PATH

import db as _db  # noqa: E402  (在 main 导入前确保 env 生效)

_db.init()
