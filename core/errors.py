"""统一错误类型。业务代码抛 AIBARError，路由层转成统一响应结构。"""

from __future__ import annotations


#: 错误码 → HTTP 状态码。只在**调用方没有显式给 status** 时生效。
#:
#: 为什么要有这张表：此前 ``status`` 的默认值写死成 400，于是
#: ``AIBARError("not_found", "图片不存在")`` 会返回 400 —— 一个 404 语义的错误
#: 顶着 400 出去，前端按 code 分流的逻辑还能用，但监控、缓存与调用方全被误导。
#: 让错误码自己决定状态码，漏写参数就不再是隐患。
CODE_STATUS: dict[str, int] = {
    "not_found": 404,
    "forbidden": 403,
    "unauthorized": 401,
    "unavailable": 503,
    "provider_unavailable": 503,
    "provider_busy": 503,
    "timeout": 504,
    "internal_error": 500,
}

#: 未登记的错误码一律按"请求有问题"处理
DEFAULT_STATUS = 400


class AIBARError(Exception):
    """带错误码的业务异常。

    Attributes:
        code: 稳定的英文错误码，例如 ``invalid_input`` / ``not_found``。
        message: 面向用户的中文说明，可直接展示。
        status: HTTP 状态码。省略时由 :data:`CODE_STATUS` 按 code 推导。
    """

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = CODE_STATUS.get(code, DEFAULT_STATUS) if status is None else status


def invalid(message: str = "请求参数不合法", code: str = "invalid_input") -> AIBARError:
    return AIBARError(code, message, 400)


def not_found(message: str = "资源不存在") -> AIBARError:
    return AIBARError("not_found", message)


def forbidden(message: str = "操作未被允许") -> AIBARError:
    return AIBARError("forbidden", message)


def unavailable(message: str = "依赖服务不可用", code: str = "unavailable") -> AIBARError:
    """依赖不可用（默认 503）。

    ``code`` 可换成更具体的错误码（例如 ``provider_unavailable``），
    状态码仍然由 :data:`CODE_STATUS` 推导，不会退回 400。
    """
    return AIBARError(code, message)
