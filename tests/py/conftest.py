"""pytest 全局夹具与测试环境修正。

这里只做两件与业务无关的事：

1. 修正宿主 ``sitecustomize.py`` 沙箱 shim 的已知缺陷：
   ``Path.mkdir(exist_ok=True)`` 在目标目录已存在时，shim 仍会把请求上报给
   broker 并抛 ``PermissionError(EEXIST)``，导致 pytest 的 ``tmp_path`` 系列
   夹具在第二次运行起全部报错。这里补一层最小兼容：仅当 ``exist_ok=True``
   且目标确实是已存在的目录时忽略该错误，其余情况原样抛出。

2. 提供数据库隔离夹具：把 ``Config.DB_PATH`` 指向临时目录并重置
   ``core.db`` 的线程本地连接，确保测试不污染仓库里的 ``data/``。
"""

from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

_ORIGINAL_MKDIR = pathlib.Path.mkdir

# 2) 把 pytest 的临时根目录收敛到项目内 ``tmp/pytest``。
#    宿主沙箱只允许进程写入工作区路径，系统临时目录（/var/folders/...）下的
#    SQLite 建库与 WAL 文件创建会被拒绝，因此必须在 pytest 解析 basetemp 之前
#    改写 TEMP 根目录，让所有 ``tmp_path`` 夹具都落在工作区内。
_PROJECT_TMP_ROOT = pathlib.Path(__file__).resolve().parents[2] / "tmp" / "pytest"
_PROJECT_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(_PROJECT_TMP_ROOT)
os.environ["TEMP"] = str(_PROJECT_TMP_ROOT)
os.environ["TMP"] = str(_PROJECT_TMP_ROOT)
tempfile.tempdir = str(_PROJECT_TMP_ROOT)


def _mkdir_ignore_existing(self, mode=0o777, parents=False, exist_ok=False):
    try:
        return _ORIGINAL_MKDIR(self, mode=mode, parents=parents, exist_ok=exist_ok)
    except PermissionError:
        # shim 抛的是纯字符串 PermissionError（errno 为 None），只能按语义判断：
        # 调用方明确允许目录已存在，且目标确实是一个目录时，静默成功。
        if exist_ok and _ORIGINAL_MKDIR is not None and self.is_dir():
            return None
        raise


# 只在 shim 确实包裹了 mkdir 时打补丁（判断依据：其 __module__ 不在本文件）
if _ORIGINAL_MKDIR.__module__ != __name__:
    pathlib.Path.mkdir = _mkdir_ignore_existing  # type: ignore[assignment]


# ---------------------------------------------------------------- 数据库隔离


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """把数据库与运行期目录重定向到 tmp_path，返回数据目录路径。

    测试用例需要数据库时统一使用本夹具，禁止直接写仓库里的 ``data/``。
    """
    from config import Config
    from core import db as core_db

    data_dir = tmp_path / "data"
    (data_dir / "reverse_uploads").mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "aibar.db"

    monkeypatch.setattr(Config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Config, "DB_PATH", db_file)
    monkeypatch.setattr(Config, "UPLOAD_DIR", data_dir / "reverse_uploads")
    monkeypatch.setattr(Config, "GALLERY_DIR", tmp_path / "static" / "gallery")
    monkeypatch.setattr(Config, "TMP_DIR", tmp_path / "tmp")

    # core.db 使用线程本地连接，切换 DB_PATH 后必须重置，否则仍连旧库
    core_db._local.conn = None
    core_db.migrate()
    yield data_dir
    core_db._local.conn = None
