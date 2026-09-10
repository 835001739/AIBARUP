"""core.errors：错误码与 HTTP 状态码的对应关系。

这些用例不只是"保证现在对"，更重要的是**锁住两类曾经真实发生过的错误**：

1. ``AIBARError("not_found", ...)`` 漏写 status，于是 404 语义的错误按 400 返回
   （``sync/comfyui_link.py`` 里就有一处，前端和监控全被误导）。
2. 同一个 ``provider_unavailable``，一处 400 一处 503，前端没法统一处理。
"""
from __future__ import annotations

import pytest

from core import errors
from core.errors import AIBARError


def test_status_follows_code_when_omitted():
    """省略 status 时由错误码推导，不再一律 400。"""
    assert AIBARError("not_found", "图片不存在").status == 404
    assert AIBARError("forbidden", "不许动").status == 403
    assert AIBARError("unavailable", "服务不可用").status == 503
    assert AIBARError("provider_unavailable", "没有可用 Provider").status == 503
    assert AIBARError("provider_busy", "忙").status == 503
    assert AIBARError("internal_error", "炸了").status == 500


def test_explicit_status_still_wins():
    """显式传的 status 优先，方便个别场景覆盖约定。"""
    assert AIBARError("not_found", "x", 410).status == 410
    assert AIBARError("invalid_input", "x", 422).status == 422


def test_unknown_code_falls_back_to_400():
    """没登记的错误码按"请求有问题"处理，保持既有行为不变。"""
    assert AIBARError("invalid_input", "参数不对").status == 400
    assert AIBARError("empty_result", "没结果").status == 400
    assert AIBARError("some_brand_new_code", "?").status == 400


def test_factories_produce_agreed_code_and_status():
    assert errors.not_found("没了").code == "not_found"
    assert errors.not_found("没了").status == 404

    assert errors.forbidden("不行").code == "forbidden"
    assert errors.forbidden("不行").status == 403

    assert errors.invalid("参数错").code == "invalid_input"
    assert errors.invalid("参数错").status == 400


def test_unavailable_factory_keeps_specific_code_but_stays_503():
    """前端按 code == 'provider_unavailable' 分流，所以换 code 不能把状态带回 400。"""
    exc = errors.unavailable("没有可用的视觉 Provider", "provider_unavailable")
    assert exc.code == "provider_unavailable"
    assert exc.status == 503

    default = errors.unavailable("依赖挂了")
    assert default.code == "unavailable"
    assert default.status == 503


def test_message_and_str_unchanged():
    """status 的推导不能影响异常本身的语义。"""
    exc = errors.not_found("词条不存在")
    assert exc.message == "词条不存在"
    assert str(exc) == "词条不存在"


def test_code_status_table_is_intact():
    """防止有人误删表项导致状态码悄悄退回 400。"""
    for code, status in (
        ("not_found", 404),
        ("forbidden", 403),
        ("unauthorized", 401),
        ("unavailable", 503),
        ("provider_unavailable", 503),
        ("provider_busy", 503),
        ("timeout", 504),
        ("internal_error", 500),
    ):
        assert errors.CODE_STATUS[code] == status

    assert errors.DEFAULT_STATUS == 400


@pytest.mark.parametrize(
    "code",
    ["not_found", "provider_unavailable", "unavailable", "forbidden", "internal_error"],
)
def test_registered_codes_never_default_to_400(code):
    """反证：这些码在表里的值都 != 400，所以"漏写 status"不会退化成旧行为。"""
    assert AIBARError(code, "x").status != 400
