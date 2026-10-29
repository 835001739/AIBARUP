"""视频转绘的 Z-Image Turbo 姿势控制工作流。

已提取的 OpenPose/DWPose 骨架必须原样送入 Fun ControlNet；再次运行姿态预处理器
会把彩色骨架当成普通图片检测，常见结果是全黑控制图。该模块只负责构造与校验
ComfyUI API 图，不访问文件、数据库或网络。
"""

from __future__ import annotations

from typing import Any

from core.errors import AIBARError

DEFAULT_DIFFUSION = "z_image_turbo_bf16.safetensors"
DEFAULT_TEXT_ENCODER = "qwen_3_4b.safetensors"
DEFAULT_VAE = "ae.safetensors"
DEFAULT_CONTROL_PATCH = "Z-Image-Turbo-Fun-Controlnet-Union-2.1-2602-8steps.safetensors"
DEFAULT_STEPS = 8
DEFAULT_CFG = 1.0
DEFAULT_CONTROL_STRENGTH = 0.8
DEFAULT_SAMPLER = "euler"
DEFAULT_SCHEDULER = "simple"


def _latent_dimension(value: int) -> int:
    """把画布尺寸夹到 ComfyUI latent 支持的 8 像素步长。"""
    return max(64, (int(value) // 8) * 8)


def build_zimage_pose_workflow(
    *,
    pose_image: str,
    positive: str,
    negative: str = "",
    seed: int = 0,
    width: int = 896,
    height: int = 1152,
    strength: float = DEFAULT_CONTROL_STRENGTH,
    diffusion: str = DEFAULT_DIFFUSION,
    text_encoder: str = DEFAULT_TEXT_ENCODER,
    vae: str = DEFAULT_VAE,
    control_patch: str = DEFAULT_CONTROL_PATCH,
) -> dict[str, Any]:
    """构造 Z-Image Turbo + Fun ControlNet 的骨架控姿 API 工作流。

    Args:
        pose_image: ComfyUI ``input/`` 下已经生成好的彩色骨架文件名。
        positive/negative: 正向与负向提示词。
        seed: 采样种子。
        width/height: 输出画布尺寸，会对齐到 8 像素步长。
        strength: Fun ControlNet 结构控制强度。

    Returns:
        可直接提交给 ComfyUI ``/prompt`` 的 API 格式工作流。
    """
    if not str(pose_image or "").strip():
        raise AIBARError("invalid_input", "Z-Image 姿势工作流缺少骨架图")

    graph: dict[str, Any] = {
        "1": {
            "class_type": "UNETLoader",
            "_meta": {"title": "Z-Image Turbo 模型"},
            "inputs": {"unet_name": diffusion, "weight_dtype": "default"},
        },
        "2": {
            "class_type": "CLIPLoader",
            "_meta": {"title": "Qwen 文本编码器（必须 lumina2）"},
            "inputs": {"clip_name": text_encoder, "type": "lumina2", "device": "default"},
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": vae},
        },
        "4": {
            "class_type": "ModelPatchLoader",
            "_meta": {"title": "Z-Image Fun ControlNet 2.1"},
            "inputs": {"name": control_patch},
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "正向提示词"},
            "inputs": {"text": positive, "clip": ["2", 0]},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "负向提示词"},
            "inputs": {"text": negative, "clip": ["2", 0]},
        },
        "7": {
            "class_type": "LoadImage",
            "_meta": {"title": "已提取骨架图"},
            "inputs": {"image": pose_image},
        },
        "8": {
            "class_type": "AIO_Preprocessor",
            "_meta": {"title": "骨架直通（不要改成 DWPose）"},
            "inputs": {
                "image": ["7", 0],
                "preprocessor": "none",
                "resolution": max(_latent_dimension(width), _latent_dimension(height)),
            },
        },
        "9": {
            "class_type": "ZImageFunControlnet",
            "_meta": {"title": "Z-Image 姿势控制"},
            "inputs": {
                "model": ["1", 0],
                "model_patch": ["4", 0],
                "vae": ["3", 0],
                "strength": float(strength),
                "image": ["8", 0],
            },
        },
        "10": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": _latent_dimension(width),
                "height": _latent_dimension(height),
                "batch_size": 1,
            },
        },
        "11": {
            "class_type": "KSampler",
            "_meta": {"title": "Z-Image Turbo 采样"},
            "inputs": {
                "model": ["9", 0],
                "seed": int(seed),
                "steps": DEFAULT_STEPS,
                "cfg": DEFAULT_CFG,
                "sampler_name": DEFAULT_SAMPLER,
                "scheduler": DEFAULT_SCHEDULER,
                "positive": ["5", 0],
                "negative": ["6", 0],
                "latent_image": ["10", 0],
                "denoise": 1.0,
            },
        },
        "12": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["11", 0], "vae": ["3", 0]},
        },
        "13": {
            "class_type": "SaveImage",
            "inputs": {"images": ["12", 0], "filename_prefix": "AIBAR_ZImage_pose"},
        },
    }
    assert_zimage_pose_workflow(graph, pose_image)
    return graph


def assert_zimage_pose_workflow(graph: dict[str, Any], pose_image: str) -> None:
    """硬校验骨架图确实经直通节点进入 Fun ControlNet 与采样器。"""
    load = graph.get("7") or {}
    passthrough = graph.get("8") or {}
    control = graph.get("9") or {}
    sampler = graph.get("11") or {}
    if (
        load.get("class_type") != "LoadImage"
        or (load.get("inputs") or {}).get("image") != pose_image
        or passthrough.get("class_type") != "AIO_Preprocessor"
        or (passthrough.get("inputs") or {}).get("image") != ["7", 0]
        or (passthrough.get("inputs") or {}).get("preprocessor") != "none"
        or control.get("class_type") != "ZImageFunControlnet"
        or (control.get("inputs") or {}).get("image") != ["8", 0]
        or float((control.get("inputs") or {}).get("strength") or 0.0) <= 0.0
        or (sampler.get("inputs") or {}).get("model") != ["9", 0]
        or (sampler.get("inputs") or {}).get("positive") != ["5", 0]
        or (sampler.get("inputs") or {}).get("latent_image") != ["10", 0]
    ):
        raise AIBARError("invalid_workflow", "Z-Image 骨架控制链未正确接入采样器")


__all__ = [
    "DEFAULT_CFG",
    "DEFAULT_CONTROL_PATCH",
    "DEFAULT_CONTROL_STRENGTH",
    "DEFAULT_DIFFUSION",
    "DEFAULT_STEPS",
    "DEFAULT_TEXT_ENCODER",
    "DEFAULT_VAE",
    "assert_zimage_pose_workflow",
    "build_zimage_pose_workflow",
]
