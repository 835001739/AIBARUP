"""``core/fileops.py`` 测试：**清理型删除绝不能外泄异常**。

背景（2026-09-01 线上故障）：删除文件在某些运行环境（沙箱 / 安全钩子）会以
``SystemExit`` 中断，而 ``SystemExit`` 是 **BaseException** 子类，``except OSError``
和 ``except Exception`` 全都抓不到。原本「先删旧图再写新图」的落盘写法，
因此在删除被拦时把已经花几十秒 GPU 完成的出图整单打成 failed。

本文件锁住的行为：**任何删除失败都只降级记日志，绝不外泄、绝不中断主流程。**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import fileops


def _boom_unlink(*_a, **_kw):
    """模拟「删除被安全策略拦截」：抛 BaseException 子类而非 OSError。"""
    raise SystemExit(1)


# ------------------------------------------------------------------ purge_stale


def test_purge_stale_removes_matches_and_keeps_current(tmp_path: Path):
    d = tmp_path / "out"
    d.mkdir()
    (d / "7_aaaaaaaaaaaaaaaa.png").write_bytes(b"old1")
    (d / "7_bbbbbbbbbbbbbbbb.png").write_bytes(b"old2")
    (d / "8_cccccccccccccccc.png").write_bytes(b"other")  # 不属于该页
    keep = d / "7_dddddddddddddddd.png"
    keep.write_bytes(b"new")

    removed = fileops.purge_stale(d, "7_*.png", keep=keep)

    assert removed == 2
    assert keep.is_file()
    # 只删该页的旧图，其他页不受影响
    assert sorted(p.name for p in d.glob("*.png")) == [
        "7_dddddddddddddddd.png",
        "8_cccccccccccccccc.png",
    ]


def test_purge_stale_never_raises_on_systemexit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "unlink", _boom_unlink)
    d = tmp_path / "out"
    d.mkdir()
    keep = d / "7_dddddddddddddddd.png"
    keep.write_bytes(b"new")
    (d / "7_aaaaaaaaaaaaaaaa.png").write_bytes(b"old")

    # 关键：不抛 SystemExit，返回 0（一个都没删掉）
    assert fileops.purge_stale(d, "7_*.png", keep=keep) == 0


def test_purge_stale_continues_after_single_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # 逐文件独立容错：第一张删失败，不影响后面的继续删。
    real_unlink = Path.unlink
    calls = {"n": 0}

    def _fail_first(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SystemExit(1)
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", _fail_first)
    d = tmp_path / "out"
    d.mkdir()
    (d / "7_aaaaaaaaaaaaaaaa.png").write_bytes(b"old1")
    (d / "7_bbbbbbbbbbbbbbbb.png").write_bytes(b"old2")
    (d / "7_cccccccccccccccc.png").write_bytes(b"old3")

    removed = fileops.purge_stale(d, "7_*.png")

    assert calls["n"] == 3          # 三张都尝试过，没有在第一张就崩掉
    assert removed == 2             # 第一张失败，后两张成功


def test_purge_stale_on_missing_dir_returns_zero(tmp_path: Path):
    assert fileops.purge_stale(tmp_path / "nope", "*.png") == 0


# ----------------------------------------------------------------- unlink_quiet


def test_unlink_quiet_removes_and_returns_true(tmp_path: Path):
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    assert fileops.unlink_quiet(p) is True
    assert not p.exists()


def test_unlink_quiet_missing_file_returns_false(tmp_path: Path):
    assert fileops.unlink_quiet(tmp_path / "nope.png") is False


def test_unlink_quiet_returns_false_on_systemexit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "unlink", _boom_unlink)
    p = tmp_path / "a.png"
    p.write_bytes(b"x")

    # 关键：不抛，返回 False —— 调用方可以据此决定降级策略
    assert fileops.unlink_quiet(p) is False
