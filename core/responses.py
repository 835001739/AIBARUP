"""统一 API 响应结构。

成功：{"ok": true, "data": ...}
失败：{"ok": false, "error": {"code": "...", "message": "..."}}

所有新增接口必须沿用该结构，不得自定义 envelope。
"""

from __future__ import annotations

from typing import Any

from flask import jsonify

from .errors import AIBARError


def ok(data: Any = None, status: int = 200):
    """成功响应。data 为 None 时返回空对象，便于前端统一取 ``res.data``。"""
    return jsonify({"ok": True, "data": data if data is not None else {}}), status


def fail(code: str, message: str, status: int = 400):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), status


def from_exception(exc: Exception):
    """把异常转为统一失败响应。未知异常统一为 internal_error，不泄漏堆栈。"""
    if isinstance(exc, AIBARError):
        return fail(exc.code, exc.message, exc.status)
    return fail("internal_error", "服务内部错误，请稍后重试", 500)
