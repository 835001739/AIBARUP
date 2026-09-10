#!/usr/bin/env python3
"""M11.2 视觉 Provider 安装脚本：ComfyUI 自定义节点 + GGUF 模型（Metal）。

用法::

    python scripts/install_providers.py --check-only
    python scripts/install_providers.py --provider comfyui_qwen3vl_8b --download
    python scripts/install_providers.py --download --comfyui-dir /path/to/ComfyUI

行为约束（PRD M11.2）：
- **默认不下载**：只有显式 ``--download`` 才会真正拉取模型；
- 安装前检查磁盘剩余空间，不足时直接中止，不产生半成品；
- 下载走 ``.part`` 断点续传，完成后核验大小与校验和，再**原子更名**；
- 失败时保留 ``.part`` 供续传，**不把半成品注册为 ready**，**不删除用户既有资产**；
- 清单只写仓库、commit、文件名与状态，**不写密钥与本机绝对路径**；
- 只支持 GGUF + Metal，不引入其他推理后端。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from config import Config  # noqa: E402

MANIFEST_FILENAME = "provider_manifest.json"
# 支持 HF 镜像（HF_ENDPOINT 是 huggingface_hub 的事实标准变量），
# 国内直连 huggingface.co 常被限流，可用 https://hf-mirror.com
HF_BASE = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
CHUNK_SIZE = 1 << 20
# 下载前额外预留 10% 空间，避免下到一半磁盘写满
RESERVE_RATIO = 1.1
MIN_FREE_BYTES = 2 << 30
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 120


# ---------------------------------------------------------------- 清单


def manifest_path() -> Path:
    return Path(Config.RESOURCES_DIR) / MANIFEST_FILENAME


def load_manifest() -> dict:
    try:
        with open(manifest_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(data: dict) -> bool:
    """原子写回清单（不含密钥与绝对路径）。"""
    path = manifest_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 路径


def comfyui_root(override: str) -> Path | None:
    if override:
        return Path(override).expanduser()
    if Config.COMFYUI_DIR:
        return Path(Config.COMFYUI_DIR)
    return None


def models_root(root: Path) -> Path:
    if Config.COMFYUI_MODELS_DIR:
        return Path(Config.COMFYUI_MODELS_DIR)
    return root / "models"


def gguf_dir(root: Path) -> Path:
    subdir = "LLM/GGUF"
    return models_root(root) / subdir


def node_dir(root: Path, repo: str) -> Path:
    return root / "custom_nodes" / repo.split("/")[-1]


# ---------------------------------------------------------------- Git 节点


def _git(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"无法执行 git：{type(exc).__name__}"
    return result.returncode, (result.stdout or result.stderr or "").strip()


def check_node(entry: dict, root: Path) -> dict[str, Any]:
    """检查自定义节点是否已安装且处于清单指定的 commit。"""
    target = node_dir(root, str(entry.get("node_repo") or ""))
    wanted = str(entry.get("node_commit") or "")
    state = {
        "repo": entry.get("node_repo"),
        "commit": wanted,
        "installed": False,
        "current_commit": None,
        "status": "not_installed",
        "detail": "",
    }
    if not (target / ".git").is_dir():
        state["detail"] = "节点目录不存在"
        return state
    state["installed"] = True
    code, output = _git(["rev-parse", "HEAD"], target)
    if code == 0:
        state["current_commit"] = output.strip()[:40]
    if state["current_commit"] == wanted:
        state["status"] = "ready"
    else:
        state["status"] = "outdated"
        state["detail"] = f"当前 HEAD 与清单 commit 不一致：{state['current_commit']}"
    return state


def install_node(entry: dict, root: Path) -> dict[str, Any]:
    """克隆或把节点切换到清单指定的 commit。"""
    repo = str(entry.get("node_repo") or "")
    wanted = str(entry.get("node_commit") or "")
    target = node_dir(root, repo)
    if not repo or not wanted:
        return {"status": "failed", "detail": "清单缺少 node_repo 或 node_commit"}

    if not (target / ".git").is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
        code, output = _git(["clone", f"https://github.com/{repo.strip('/')}.git", target.name], target.parent)
        if code != 0:
            return {"status": "failed", "detail": output[:200] or "克隆失败"}
    code, output = _git(["fetch", "--all", "--tags"], target)
    if code != 0:
        return {"status": "failed", "detail": output[:200] or "fetch 失败"}
    code, output = _git(["checkout", wanted], target)
    if code != 0:
        return {"status": "failed", "detail": output[:200] or "切换 commit 失败"}
    return {"status": "ready", "detail": "", "commit": wanted}


# ---------------------------------------------------------------- 文件核验


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_ready(dest: Path, expected_size: int | None, checksum: str | None) -> bool:
    if not dest.is_file():
        return False
    if expected_size and dest.stat().st_size != expected_size:
        return False
    if checksum:
        try:
            return _sha256(dest).lower() == str(checksum).lower()
        except OSError:
            return False
    return True


def resolve_url(repo: str, filename: str) -> str:
    return f"{HF_BASE}/{repo.strip('/')}/resolve/main/{filename}"


# ---------------------------------------------------------------- 断点续传


def _open_stream(url: str, resume: int) -> tuple[Any, str]:
    """打开下载流；失败返回 ``(None, 原因)``。"""
    headers = {"Range": f"bytes={resume}-"} if resume else {}
    try:
        response = requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return None, f"无法连接下载源（{type(exc).__name__}）"
    return response, ""


def download_file(
    url: str,
    dest: Path,
    expected_size: int | None = None,
    checksum: str | None = None,
) -> dict[str, Any]:
    """带断点续传、大小/校验和核验与原子更名的下载。

    失败时**保留** ``.part`` 文件以便续传，绝不删除任何已存在的文件。
    """
    if _file_ready(dest, expected_size, checksum):
        return {"status": "skipped", "name": dest.name, "bytes": dest.stat().st_size}

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    resume = part.stat().st_size if part.is_file() else 0

    response, error = _open_stream(url, resume)
    if response is None:
        return {"status": "failed", "name": dest.name, "reason": error}
    if resume and response.status_code == 200:
        # 服务端忽略 Range：放弃续传，从头开始
        response.close()
        resume = 0
        response, error = _open_stream(url, 0)
        if response is None:
            return {"status": "failed", "name": dest.name, "reason": error}
    status = response.status_code
    if (resume and status != 206) or (not resume and status >= 400):
        response.close()
        return {"status": "failed", "name": dest.name, "reason": f"下载源返回状态码 {status}"}

    try:
        with open(part, "ab" if resume else "wb") as fh:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                fh.write(chunk)
    except (OSError, requests.RequestException) as exc:
        return {"status": "failed", "name": dest.name, "reason": f"写入中断（{type(exc).__name__}），已保留 .part"}
    finally:
        response.close()

    try:
        size = part.stat().st_size
    except OSError:
        return {"status": "failed", "name": dest.name, "reason": "写入后无法读取 .part"}

    if expected_size and size != expected_size:
        return {
            "status": "failed",
            "name": dest.name,
            "reason": f"大小不匹配（{size} / {expected_size}），已保留 .part 以便续传",
        }
    if checksum:
        try:
            actual = _sha256(part)
        except OSError:
            return {"status": "failed", "name": dest.name, "reason": "校验时无法读取 .part"}
        if actual.lower() != str(checksum).lower():
            return {"status": "failed", "name": dest.name, "reason": "校验和不匹配，已保留 .part"}

    try:
        os.replace(part, dest)
    except OSError:
        return {"status": "failed", "name": dest.name, "reason": "原子更名失败，文件保留为 .part"}
    return {"status": "downloaded", "name": dest.name, "bytes": size}


# ---------------------------------------------------------------- 磁盘


def free_bytes(path: Path) -> int:
    """取目标路径所在卷的剩余空间；路径不存在时向上取到最近的已存在目录。"""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(str(probe)).free
    except OSError:
        return 0


def needed_bytes(tasks: list[dict[str, Any]]) -> int:
    total = 0
    for task in tasks:
        expected = int(task.get("expected_size") or 0)
        if expected <= 0:
            continue
        dest = Path(task["dest"])
        have = dest.stat().st_size if dest.is_file() else 0
        part = dest.with_name(dest.name + ".part")
        have = max(have, part.stat().st_size if part.is_file() else 0)
        total += max(0, expected - have)
    return total


# ---------------------------------------------------------------- 报告


def _human(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f}{unit}"
        value /= 1024
    return f"{value:.2f}TB"


def build_tasks(entry: dict, root: Path) -> list[dict[str, Any]]:
    """构造该 Provider 需要下载的文件清单。

    清单字段采用 ``model_filename`` / ``model_repo``（主模型）与
    ``mmproj_filename`` / ``mmproj_repo``（多模态投影）两段命名，
    不再使用已被废弃的 ``filename`` / ``repo`` 短名。
    """
    directory = gguf_dir(root)
    specs = (
        ("model_filename", "model_repo", "expected_size", "checksum"),
        ("mmproj_filename", "mmproj_repo", "mmproj_expected_size", "mmproj_checksum"),
    )
    tasks: list[dict[str, Any]] = []
    for name_key, repo_key, size_key, csum_key in specs:
        filename = str(entry.get(name_key) or "")
        repo = str(entry.get(repo_key) or "")
        if not filename or not repo:
            continue
        tasks.append(
            {
                "name": filename,
                "url": resolve_url(repo, filename),
                "dest": directory / filename,
                "expected_size": entry.get(size_key),
                "checksum": entry.get(csum_key),
            }
        )
    return tasks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 ComfyUI 视觉 Provider 节点与 GGUF 模型")
    parser.add_argument("--check-only", action="store_true", help="只检查与报告，不安装不下载")
    parser.add_argument("--provider", action="append", dest="providers", help="只处理指定 Provider（可重复）")
    parser.add_argument("--comfyui-dir", default="", help="ComfyUI 根目录（默认读取 COMFYUI_DIR）")
    parser.add_argument("--download", action="store_true", help="真正执行下载（默认关闭）")
    args = parser.parse_args(argv)

    manifest = load_manifest()
    providers = manifest.get("providers")
    if not isinstance(providers, dict) or not providers:
        print("清单为空或缺少 providers，无法继续")
        return 2

    keys = args.providers or sorted(providers)
    unknown = [key for key in keys if key not in providers]
    if unknown:
        print(f"未知的 Provider：{', '.join(unknown)}")
        return 2

    root = comfyui_root(args.comfyui_dir)
    if root is None:
        print("未指定 ComfyUI 目录：请加 --comfyui-dir 或设置环境变量 COMFYUI_DIR")
        return 2
    if not root.is_dir():
        print(f"ComfyUI 目录不存在：{root}")
        return 2

    do_download = args.download and not args.check_only
    report: list[dict[str, Any]] = []
    failures = 0

    for key in keys:
        entry = providers[key]
        node_state = check_node(entry, root)
        if do_download and node_state["status"] != "ready":
            outcome = install_node(entry, root)
            if outcome["status"] != "ready":
                failures += 1
                node_state = {**node_state, "status": "failed", "detail": outcome.get("detail", "")}
            else:
                node_state = check_node(entry, root)

        tasks = build_tasks(entry, root)
        for task in tasks:
            task["present"] = _file_ready(task["dest"], task["expected_size"], task["checksum"])

        pending = needed_bytes(tasks)
        required = max(int(pending * RESERVE_RATIO), MIN_FREE_BYTES) if pending else 0
        disk_free = free_bytes(gguf_dir(root))
        disk_ok = required <= disk_free

        results: list[dict[str, Any]] = []
        if do_download and tasks:
            if not disk_ok:
                failures += 1
                for task in tasks:
                    results.append(
                        {
                            "name": task["name"],
                            "status": "failed",
                            "reason": f"磁盘空间不足（需要 {_human(required)}，剩余 {_human(disk_free)}）",
                        }
                    )
            else:
                for task in tasks:
                    if task["present"]:
                        results.append({"name": task["name"], "status": "skipped"})
                        continue
                    outcome = download_file(
                        task["url"], task["dest"], task["expected_size"], task["checksum"]
                    )
                    results.append(outcome)
                    if outcome["status"] == "failed":
                        failures += 1
        else:
            for task in tasks:
                results.append({"name": task["name"], "status": "skipped" if task["present"] else "missing"})

        node_ready = node_state["status"] == "ready"
        files_ready = all(
            _file_ready(task["dest"], task["expected_size"], task["checksum"]) for task in tasks
        )
        status = "ready" if (node_ready and files_ready and tasks) else (
            "incomplete" if (node_ready or files_ready) else "not_installed"
        )
        if any(item["status"] == "failed" for item in results) or node_state["status"] == "failed":
            status = "failed"

        entry["install_status"] = status
        entry["verified_at"] = _timestamp()
        report.append(
            {
                "key": key,
                "node": node_state,
                "disk_free": disk_free,
                "disk_required": required,
                "disk_ok": disk_ok,
                "files": results,
                "status": status,
            }
        )

    if do_download:
        save_manifest(manifest)

    _print_report(report, root, do_download)
    return 1 if failures else 0


def _print_report(report: list[dict[str, Any]], root: Path, downloaded: bool) -> None:
    print(f"ComfyUI 目录：{root}")
    print(f"模式：{'安装/下载' if downloaded else '仅检查'}")
    print("-" * 72)
    for item in report:
        node = item["node"]
        print(f"[{item['status']}] {item['key']}")
        print(f"    节点 {node['repo']} @ {node['commit'][:12]} -> {node['status']}"
              + (f"（{node['detail']}）" if node.get("detail") else ""))
        for file_item in item["files"]:
            extra = file_item.get("reason") or (
                f"{_human(file_item['bytes'])}" if file_item.get("bytes") else ""
            )
            print(f"    模型 {file_item['name']} -> {file_item['status']} {extra}".rstrip())
        if item["files"]:
            print(
                f"    磁盘 剩余 {_human(item['disk_free'])} / 需要 {_human(item['disk_required'])}"
                f" -> {'充足' if item['disk_ok'] else '不足'}"
            )
    print("-" * 72)
    print("提示：节点依赖 Metal 版 llama-cpp-python，请在 ComfyUI 的 Python 环境中安装后重启 ComfyUI。")


if __name__ == "__main__":
    sys.exit(main())
