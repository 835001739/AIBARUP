"""日志配置。

隐私约束（PRD M9.9 / M10.4）：日志绝不记录提示词正文、图片内容、
API Key 和本机绝对路径。只记录可用于排障的元数据字段。
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """幂等地配置根 logger，返回应用主 logger ``aibar``。"""
    global _CONFIGURED
    logger = logging.getLogger("aibar")
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root = logging.getLogger()
        root.handlers = [handler]
        root.setLevel(level)
        # 抑制第三方库的噪声
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        _CONFIGURED = True
    return logger


def get_logger(name: str = "aibar") -> logging.Logger:
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)


def safe_log(logger: logging.Logger, level: int, event: str, **fields) -> None:
    """结构化安全日志：只接受已列举的元数据字段。"""
    payload = " ".join(f"{k}={v}" for k, v in sorted(fields.items()))
    logger.log(level, "%s %s", event, payload)
