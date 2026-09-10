"""AIBAR 全局配置。

所有配置来自环境变量（可通过项目根目录 .env 提供），缺失时使用安全默认值。
设计原则：任何一项配置缺失都不能让应用崩溃，只降级对应能力。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)


def _ensure_loopback_no_proxy() -> None:
    """确保回环地址永远不走外层代理。

    背景：AIBAR 常被代理/沙箱 shell（如 WorkBuddy CLI）拉起，注入
    ``HTTP_PROXY=http://127.0.0.1:53449`` 等变量。``requests`` 默认
    ``trust_env=True`` 会读取这些变量，导致对 ComfyUI(127.0.0.1:8188)
    这类本机服务的探测被投递到代理并返回 502。这里补 NO_PROXY 兜底，
    一次性覆盖所有对本机服务的 requests 调用。
    """
    extra = ("127.0.0.1", "localhost", "::1")
    for key in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(key, "")
        parts = [p for p in current.split(",") if p.strip()]
        for token in extra:
            if token not in parts:
                parts.append(token)
        os.environ[key] = ",".join(parts)


_ensure_loopback_no_proxy()


def _str(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


class Config:
    """集中配置。模块只读取属性，不直接读环境变量。"""

    ROOT = ROOT
    RESOURCES_DIR = ROOT / "resources"
    DATA_DIR = ROOT / "data"
    TMP_DIR = ROOT / "tmp"
    STATIC_DIR = ROOT / "static"
    GALLERY_DIR = STATIC_DIR / "gallery"
    UPLOAD_DIR = DATA_DIR / "reverse_uploads"
    DB_PATH = Path(_str("AIBAR_DB", str(DATA_DIR / "aibar.db")))

    # ---- 站点 ----
    HOST = _str("AIBAR_HOST", "127.0.0.1")
    PORT = _int("AIBAR_PORT", 8099)
    # 对外绝对基址：ComfyUI 前端桥需要用它反向拉取工作流 JSON，
    # 反向代理/局域网访问时可用 AIBAR_PUBLIC_BASE 显式覆盖。
    AIBAR_PUBLIC_BASE = _str("AIBAR_PUBLIC_BASE", "")

    # ---- ComfyUI（M1）----
    COMFYUI_HOST = _str("COMFYUI_HOST", "127.0.0.1")
    COMFYUI_PORT = _int("COMFYUI_PORT", 8188)
    COMFYUI_DIR = Path(_str("COMFYUI_DIR", "")) if _str("COMFYUI_DIR") else None
    # 允许单独覆盖三个源目录（优先级高于自动探测）
    COMFYUI_WORKFLOWS_DIR = _str("COMFYUI_WORKFLOWS_DIR", "")
    COMFYUI_OUTPUT_DIR = _str("COMFYUI_OUTPUT_DIR", "")
    COMFYUI_MODELS_DIR = _str("COMFYUI_MODELS_DIR", "")
    COMFYUI_PYTHON = _str("COMFYUI_PYTHON", "")
    COMFYUI_AUTOSTART = _bool("COMFYUI_AUTOSTART", True)
    COMFYUI_TIMEOUT = _float("COMFYUI_TIMEOUT", 3.0)

    # ---- 跨域（ComfyUI 前端桥梁要跨域读取本站的工作流 JSON）----
    # 默认只放行 ComfyUI 自身来源；需要局域网/其它端口时用 CORS_ALLOWED_ORIGINS
    # 逗号分隔覆盖，置 "*" 表示全放开（仅建议在完全可信的本机环境使用）。
    CORS_ALLOWED_ORIGINS = tuple(
        part.strip()
        for part in (
            _str("CORS_ALLOWED_ORIGINS", "")
            or (
                f"http://{COMFYUI_HOST}:{COMFYUI_PORT},"
                f"http://127.0.0.1:{COMFYUI_PORT},"
                f"http://localhost:{COMFYUI_PORT}"
            )
        ).split(",")
        if part.strip()
    )

    # ---- 同步引擎（M4）----
    SYNC_INTERVAL = _int("SYNC_INTERVAL", 10)
    SYNC_AUTO = _bool("SYNC_AUTO", True)
    SEED_LIMIT = _int("SEED_LIMIT", 60)

    # ---- 反推上传（M9）----
    REVERSE_UPLOAD_MAX_MB = _int("REVERSE_UPLOAD_MAX_MB", 20)
    REVERSE_UPLOAD_TTL_HOURS = _int("REVERSE_UPLOAD_TTL_HOURS", 24)
    REVERSE_ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".webp")

    # ---- 本地视觉 Provider（M10）----
    LOCAL_VISION_ENABLED = _bool("LOCAL_VISION_ENABLED", False)
    LOCAL_VISION_ENDPOINT = _str("LOCAL_VISION_ENDPOINT", "")
    LOCAL_VISION_MODEL = _str("LOCAL_VISION_MODEL", "")
    LOCAL_VISION_TIMEOUT = _int("LOCAL_VISION_TIMEOUT", 120)

    # ---- 外部视觉 Provider（M9）----
    OPENAI_VISION_ENABLED = _bool("OPENAI_VISION_ENABLED", False)
    OPENAI_VISION_ENDPOINT = _str("OPENAI_VISION_ENDPOINT", "https://api.openai.com/v1")
    OPENAI_VISION_MODEL = _str("OPENAI_VISION_MODEL", "gpt-4o-mini")
    OPENAI_VISION_API_KEY = _str("OPENAI_VISION_API_KEY", "")
    OPENAI_VISION_TIMEOUT = _int("OPENAI_VISION_TIMEOUT", 120)

    # ---- 可选 AI 语义扩写（M6.4）----
    AI_PROVIDER_ENABLED = _bool("AI_PROVIDER_ENABLED", False)
    AI_PROVIDER_ENDPOINT = _str("AI_PROVIDER_ENDPOINT", "")
    AI_PROVIDER_MODEL = _str("AI_PROVIDER_MODEL", "")
    AI_PROVIDER_API_KEY = _str("AI_PROVIDER_API_KEY", "")
    AI_PROVIDER_TIMEOUT = _int("AI_PROVIDER_TIMEOUT", 60)

    @classmethod
    def comfyui_base(cls) -> str:
        return f"http://{cls.COMFYUI_HOST}:{cls.COMFYUI_PORT}"

    @classmethod
    def ensure_dirs(cls) -> None:
        """保证运行期目录存在。任何目录创建失败都不应阻断启动。"""
        for path in (
            cls.DATA_DIR,
            cls.TMP_DIR,
            cls.GALLERY_DIR,
            cls.UPLOAD_DIR,
            cls.DB_PATH.parent,
        ):
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass


# 兼容旧写法：允许 `from config import CFG` 与 `import config`
CFG = Config
