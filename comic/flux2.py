"""M17 · FLUX.2 Klein 结构控制工作流（ComfyUI）。

背景与动机
==========
AIBARUP 旧的工作流 (M14+ ``comic.pose.build_pose_workflow``) 基于 SDXL + ControlNet +
IPAdapter 三件套：锁姿势要 OpenPose ControlNet、锁脸要 IPAdapter FaceID、SDXL
底模 + Animagine。整条链路节点 15+，要 3 个独立模型权重。

2026 BFL 发布 **FLUX.2 [klein]** (Apache 2.0)，把"参考图编辑 + 文本编辑"统一进单模型：
- 单模型（7.8GB 蒸馏版 / ~13GB 基础版），零额外 ControlNet / IPAdapter 权重；
- 通过 ``ReferenceLatent`` 节点把多张参考图 VAE 编码挂到 conditioning，
  **架构级**实现"结构控制 + 外观参考"；
- 实测：参考图能锁住构图 / 姿势 / 光照 / 色调，而不只是外观；
  4 步蒸馏版 + MPS 推理，1024×1024 约 80 秒（M3 Pro 36GB）。

为什么不是"真 ControlNet"？
--------------------------
FLUX.2 至今没有 ComfyUI 原生 ControlNet 节点（``comfy/controlnet.py`` 只到
``ControlNetFlux``，仅支持 Flux1 架构）。社区唯一的 Klein 真 ControlNet
(DiffSynth-Studio/Template-KleinBase4B-ControlNet) 是 DiffSynth 生态，
需要 24-40GB CUDA GPU，且无 ComfyUI 节点。

在本机 (M3 Pro 36GB / 磁盘剩 28GB) 上：
- FLUX.2-dev bf16 23.8GB → 装不下、跑不动；
- FLUX.2-dev fp8 → MPS 不支持；
- → 只能走 Klein + ReferenceLatent 路线（"架构级 ControlNet"，零下载）。

参考 ComfyUI / 历史契约
-----------------------
节点拓扑严格对齐 ``comic.pose.build_pose_workflow`` 的命名与编号习惯，
``comic.workflow_ui.api_to_ui_graph`` 自动 API→UI 转换，
``sync.comfyui_link.build_editor_link_graph`` 自动生成 ComfyUI 深链接。

依赖（已在已部署的 ComfyUI v0.30.0 上验证）
-------------------------------------------
核心节点 (ComfyUI 自带):
    UNETLoader, CLIPLoader(type=flux2), VAELoader,
    EmptyFlux2LatentImage, Flux2Scheduler,
    ReferenceLatent, VAEEncode, VAEDecode,
    CLIPTextEncode, LoadImage, SaveImage,
    CFGGuider, BasicGuider, KSamplerSelect, RandomNoise, SamplerCustomAdvanced
预处理器 (comfyui_controlnet_aux 自带):
    DWPreprocessor (DWPose), Canny, DepthAnything
模型 (本地已存在):
    models/diffusion_models/flux-2-klein-4b.safetensors    # 7.8GB
    models/text_encoders/qwen_3_4b.safetensors              # 8.0GB
    models/vae/flux2-vae.safetensors                        # 0.3GB
"""

from __future__ import annotations

from typing import Any, Sequence

# ---- 模型文件名常量 -----------------------------------------------------------

# 蒸馏 4B（Apache 2.0, 消费级硬件可跑，本机主用）
DEFAULT_DIFFUSION = "flux-2-klein-4b.safetensors"
DEFAULT_TEXT_ENCODER = "qwen_3_4b.safetensors"
DEFAULT_VAE = "flux2-vae.safetensors"

# 基础 4B（未蒸馏，多样性更高；可同 slot 通过 UNETLoader 切换）
# BASE_DIFFUSION = "flux-2-klein-base-4b.safetensors"

# ---- 采样默认值 --------------------------------------------------------------
# 蒸馏版：4 步 / CFG=1.0（CFG Guider 对 distilled 必须是 1.0；非 1 会出图失真）
DEFAULT_STEPS_DISTILLED = 4
DEFAULT_CFG_DISTILLED = 1.0
# 基础版：50 步 / CFG=4.0（DiffSynth 模板推荐值；ComfyUI Flux2Scheduler 默认 20 步）
DEFAULT_STEPS_BASE = 20
DEFAULT_CFG_BASE = 4.0

DEFAULT_SAMPLER = "euler"
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 1024

# 工作流文件名（落在 ComfyUI user/default/workflows/ 下；AIBAR-Bridge 也读这里）
FLUX2_WORKFLOW_FILENAME = "aibar_flux2_structural.json"
# M17.3 · 姿势参考 + 人物一致
FLUX2_POSE_WORKFLOW_FILENAME = "aibar_flux2_pose_consistent.json"
# ReferenceLatent 无权重参数，靠「同一张图重复几遍」加权；上限防止注意力被参考图吃满
MAX_CHARACTER_REFS = 3
# 人物参考链的起始节点 id（步长 4），避让 build_flux2_workflow 的 6-13
CHARACTER_REF_ID_BASE = 100


def _add_reference(
    graph: dict[str, Any],
    image: str,
    pos_ref: list[Any],
    neg_ref: list[Any],
    base: int,
    title: str = "",
    vae_node: str = "3",
) -> tuple[list[Any], list[Any]]:
    """往图上追加一条「参考图 → VAE 编码 → 双 ReferenceLatent」链并返回新的 conditioning。

    Klein 的 ``ReferenceLatent`` **没有权重参数**（``object_info`` 只有
    ``conditioning`` + ``latent``），所以「这条链有多重要」只能靠**张数**表达：
    同一张图重复喂 N 次 = 权重 ×N。这是人物一致性的主要调节杆。

    Args:
        graph: 被就地修改的 API 图。
        image: ComfyUI ``input/`` 下的图片文件名。
        pos_ref / neg_ref: 上一条链的输出 conditioning（首次传 CLIPTextEncode 的 ``["4",0]``）。
        base: 该链起始节点 id；按步长 4 分配
            ``base``=LoadImage、``+1``=VAEEncode、``+2``=ReferenceLatent(+)、``+3``=ReferenceLatent(-)。
            ``build_flux2_workflow`` 用 6/10（既有编号，单测钉死），
            ``build_flux2_pose_workflow`` 用 100/104/108…（支持任意多张）。
        title: 节点标题（画布上显示，便于区分「人物 / 姿势」）。
        vae_node: VAE 节点 id（两张图必须共用同一个 ``flux2-vae``，否则 latent space 不一致）。

    Returns:
        新的 ``(pos_ref, neg_ref)``，可直接喂给下一条链或 Guider。
    """
    load_id = str(base)
    enc_id = str(base + 1)
    pos_id = str(base + 2)
    neg_id = str(base + 3)
    label = title or "参考图"

    graph[load_id] = {"class_type": "LoadImage", "inputs": {"image": image}}
    graph[enc_id] = {
        "class_type": "VAEEncode",
        "inputs": {"pixels": [load_id, 0], "vae": [vae_node, 0]},
    }
    graph[pos_id] = {
        "class_type": "ReferenceLatent",
        "_meta": {"title": "%s(+)" % label},
        "inputs": {"conditioning": pos_ref, "latent": [enc_id, 0]},
    }
    graph[neg_id] = {
        "class_type": "ReferenceLatent",
        "_meta": {"title": "%s(-)" % label},
        "inputs": {"conditioning": neg_ref, "latent": [enc_id, 0]},
    }
    return [pos_id, 0], [neg_id, 0]


def build_flux2_workflow(
    reference_image: str = "",
    control_image: str = "",
    positive: str = "",
    negative: str = "",
    diffusion: str = DEFAULT_DIFFUSION,
    text_encoder: str = DEFAULT_TEXT_ENCODER,
    vae: str = DEFAULT_VAE,
    seed: int = 0,
    steps: int = DEFAULT_STEPS_DISTILLED,
    cfg: float = DEFAULT_CFG_DISTILLED,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    sampler_name: str = DEFAULT_SAMPLER,
    use_basic_guider: bool = False,
) -> dict[str, Any]:
    """构造 FLUX.2 Klein 结构控制工作流（API 格式）。

    节点拓扑::

        1  UNETLoader ─┬─ MODEL ─┐
          (diffusion)  │        │
        2  CLIPLoader  │  CLIP ─┤
          (text_enc)   │        │
        3  VAELoader ──┴─ VAE ──┤
                                │
        4  CLIPTextEncode(+) ───┤
        5  CLIPTextEncode(-) ───┤
                                │
        6  LoadImage(reference)─┤
        7  VAEEncode(6,3) ──→ 8 ReferenceLatent(4,7) ──→ 12 ReferenceLatent(8,11)
                                │                       │
        10 LoadImage(control) ──┤                       │
        11 VAEEncode(10,3) ────→│                       │
                                │                       │
        14 EmptyFlux2LatentImage(w,h,1) ──────────────→│
        15 Flux2Scheduler(steps,w,h) ──→ sigmas ──────→│
        16 CFGGuider(model=1, pos=12, neg=13, cfg) ──→│
          (或 BasicGuider(model=1, cond=12))           │
        17 KSamplerSelect(euler) ──→ sampler ─────────→│
        18 RandomNoise(seed) ──→ noise ───────────────→│
                                │                       │
        19 SamplerCustomAdvanced(18,16,17,15,14) ──→ 20 VAEDecode ──→ 21 SaveImage

    Args:
        reference_image: 角色/外观参考图（ComfyUI ``input/`` 下文件名）。
            留空则跳过整条参考图链（纯文生图）。
        control_image: 结构控制图（canny/depth/dwpose/anything 图）。
            留空则跳过结构控制链（仅外观参考）。
        positive / negative: 提示词；留空由 AIBAR runner 注入。
        diffusion / text_encoder / vae: 模型文件名。
        seed / steps / cfg / width / height / sampler_name: 采样参数。
        use_basic_guider: True → BasicGuider（只吃 positive，等价 cfg=1）。
            蒸馏版 Klein 推荐 CFG=1 + CFGGuider；如果你确认不要负向也可切 BasicGuider。
    """
    graph: dict[str, Any] = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": diffusion, "weight_dtype": "default"},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": text_encoder, "type": "flux2"},
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": vae},
        },
        "4": {
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "正向提示词"},
            "inputs": {"text": positive, "clip": ["2", 0]},
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "负面提示词"},
            "inputs": {"text": negative, "clip": ["2", 0]},
        },
        "14": {
            "class_type": "EmptyFlux2LatentImage",
            "inputs": {"width": int(width), "height": int(height), "batch_size": 1},
        },
        "15": {
            "class_type": "Flux2Scheduler",
            "inputs": {"steps": int(steps), "width": int(width), "height": int(height)},
        },
    }

    pos_ref: list[Any] = ["4", 0]
    neg_ref: list[Any] = ["5", 0]

    # ---- 外观参考链（reference_image）---- base=6 → 节点 6/7/8/9（编号被单测钉死，勿改）
    if reference_image:
        pos_ref, neg_ref = _add_reference(
            graph, reference_image, pos_ref, neg_ref, base=6, title="外观参考"
        )

    # ---- 结构控制链（control_image）---- base=10 → 节点 10/11/12/13
    if control_image:
        pos_ref, neg_ref = _add_reference(
            graph, control_image, pos_ref, neg_ref, base=10, title="结构控制"
        )

    # ---- 采样器 + 解码 + 保存 ----
    if use_basic_guider:
        graph["16"] = {
            "class_type": "BasicGuider",
            "inputs": {"model": ["1", 0], "conditioning": pos_ref},
        }
    else:
        graph["16"] = {
            "class_type": "CFGGuider",
            "inputs": {
                "model": ["1", 0],
                "positive": pos_ref,
                "negative": neg_ref,
                "cfg": float(cfg),
            },
        }
    graph["17"] = {
        "class_type": "KSamplerSelect",
        "inputs": {"sampler_name": sampler_name},
    }
    graph["18"] = {
        "class_type": "RandomNoise",
        "inputs": {"noise_seed": int(seed)},
    }
    graph["19"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["18", 0],
            "guider": ["16", 0],
            "sampler": ["17", 0],
            "sigmas": ["15", 0],
            "latent_image": ["14", 0],
        },
    }
    graph["20"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["19", 0], "vae": ["3", 0]},
    }
    graph["21"] = {
        "class_type": "SaveImage",
        "inputs": {"images": ["20", 0], "filename_prefix": "AIBAR_flux2"},
    }
    return graph


def ensure_flux2_workflow(
    reference_image: str = "",
    control_image: str = "",
    positive: str = "",
    negative: str = "",
    **kwargs: Any,
) -> str | None:
    """把 FLUX.2 Klein 结构控制工作流写入 ComfyUI 工作流目录（幂等覆盖写）。

    Returns:
        工作流文件名；目录不可用时返回 None。**绝不抛异常**。

    与 ``comic.pose.ensure_pose_workflow`` 同款：
    两张图都留空时，自动扫描 ComfyUI ``input/`` 选第一张 PNG 作参考图，
    保证落盘文件是带参考链的完整版（用户后续可调参或换图）。
    """
    from sync import paths as _sync_paths

    if not reference_image and not control_image:
        try:
            candidates = _sync_paths.input_dir()
            for p in sorted(candidates.glob("*.png"))[:1]:
                reference_image = p.name
                break
        except Exception:  # noqa: BLE001
            pass

    try:
        wf_dir = _sync_paths.workflows_dir()
        wf_dir.mkdir(parents=True, exist_ok=True)
        target = wf_dir / FLUX2_WORKFLOW_FILENAME
        import json

        graph = build_flux2_workflow(
            reference_image=reference_image,
            control_image=control_image,
            positive=positive,
            negative=negative,
            **kwargs,
        )
        target.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        return target.name
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------- M17.3 · 姿势参考 + 人物一致性工作流
def build_flux2_pose_workflow(
    character_images: Sequence[str] = (),
    pose_image: str = "",
    positive: str = "",
    negative: str = "",
    diffusion: str = DEFAULT_DIFFUSION,
    text_encoder: str = DEFAULT_TEXT_ENCODER,
    vae: str = DEFAULT_VAE,
    seed: int = 0,
    steps: int = DEFAULT_STEPS_DISTILLED,
    cfg: float = DEFAULT_CFG_DISTILLED,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    sampler_name: str = DEFAULT_SAMPLER,
    use_basic_guider: bool = False,
) -> dict[str, Any]:
    """**姿势参考 + 人物一致性** 工作流：多张人物参考图锁身份，一张姿势图锁动作。

    与 :func:`build_flux2_workflow` 的关系：那个是「1 外观 + 1 结构」固定两链（已接进
    视频转绘 M17.2，编号被单测钉死）；这个是**通用多链版**——人物参考可以 1~3 张，
    姿势图固定接在链条最后。

    人物一致性怎么保证（三根杠杆）
    ----------------------------
    1. **多张参考图串联**：``ReferenceLatent`` 官方描述就是
       "you can chain multiple to set multiple reference images"。同一个人物喂 2-3 张
       （正面立绘 / 半身 / 上一帧产出图）比单张稳得多。
    2. **重复加权**：ReferenceLatent **没有 weight 参数**，所以
       ``character_images=["a.png", "a.png", "a.png"]`` 就是「身份权重 ×3」，
       ``["a.png", "pose.png"]`` 则是身份与姿势各占一半。这是唯一能调的平衡杆。
    3. **确定性种子**：同一人物固定 ``seed``（可用 ``comic.actors.actor_seed(actor)``
       派生），换姿势不换种子 → 脸/发色/服装不漂。

    链序语义：人物参考在前（先立住身份），姿势图在最后（再改动作）——
    与 :func:`build_flux2_workflow` 的「外观先锁、结构后锁」保持一致。

    ⚠️ 实测限制（2026-09-08，M3 Pro / Klein 4B / 1024×1024 / 4 步 / CFG 1.0）
    ------------------------------------------------------------------------
    本工作流能可靠做到**人物一致**，但**做不到严格姿势控制**。同人设图 ×2 + 同 seed
    + 同提示词，只换姿势图，出图像素级比对（MAD，平均绝对差）：

    ======================================  ======  ==========
    对比                                      MAD   输入图差异
    ======================================  ======  ==========
    骨架 A vs 骨架 B                          1.97   5.73
    真实帧 A vs 真实帧 B                      3.33  25.98
    有骨架 vs 无骨架                          9.67  —
    骨架 vs 真实帧（当姿势参考）              66.7  52.63
    ======================================  ======  ==========

    两点结论：

    - **姿势参考基本被忽略**：真实帧输入差 25.98，输出只差 3.33（骨架 5.73→1.97），
      输出差异远小于输入差异 ⇒ 模型没有跟随姿势，`ReferenceLatent` 是**外观/整体
      构图迁移**机制，不是 ControlNet。
    - **姿势参考用真实照片反而更糟**：出图会被整个拖向那张照片
      （与人设参考图的平均色差从 1.3 恶化到 60），**人物一致性同时丢失**。
      用骨架图当姿势参考至少能保住人物（色差 1.3），代价是姿势无效。

    ⇒ 需要**严格姿势控制**时走 M14（`comic/pose.py`，SDXL + ControlNet-Union
    openpose + IPAdapter FaceID，真 ControlNet，骨架图原样吃）。
    本函数定位是「同一个人物的稳定续写 / 分镜」，不是「摆姿势」。

    Args:
        character_images: 人物参考图文件名列表（ComfyUI ``input/`` 下），1~3 张。
            超过 :data:`MAX_CHARACTER_REFS` 的部分会被截断。
        pose_image: 姿势参考图（DWPose 骨架图 / 姿势照 / 构图参考）。留空 = 纯人物续写。
        positive / negative: 提示词；负向建议带上人物卡的反漂移负向词。
        seed: 同一人物请固定住（见上文第 3 点）。
        其余参数同 :func:`build_flux2_workflow`。

    Returns:
        API 格式工作流 dict。节点 id：主干 1-5 / 14-21，
        人物链 100+、姿势链紧随其后。
    """
    graph: dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": diffusion, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": text_encoder, "type": "flux2"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "4": {
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "正向提示词"},
            "inputs": {"text": positive, "clip": ["2", 0]},
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "负面提示词"},
            "inputs": {"text": negative, "clip": ["2", 0]},
        },
        "14": {
            "class_type": "EmptyFlux2LatentImage",
            "inputs": {"width": int(width), "height": int(height), "batch_size": 1},
        },
        "15": {
            "class_type": "Flux2Scheduler",
            "inputs": {"steps": int(steps), "width": int(width), "height": int(height)},
        },
    }

    pos_ref: list[Any] = ["4", 0]
    neg_ref: list[Any] = ["5", 0]

    # ---- 人物一致性链（可多张，串联加权）----
    base = CHARACTER_REF_ID_BASE
    for idx, img in enumerate(list(character_images)[:MAX_CHARACTER_REFS]):
        img = str(img or "").strip()
        if not img:
            continue
        pos_ref, neg_ref = _add_reference(
            graph, img, pos_ref, neg_ref, base=base, title="人物参考%d" % (idx + 1)
        )
        base += 4

    # ---- 姿势参考链（接在最后，决定这一张的动作）----
    if str(pose_image or "").strip():
        pos_ref, neg_ref = _add_reference(
            graph, str(pose_image).strip(), pos_ref, neg_ref, base=base, title="姿势参考"
        )

    # ---- 采样器 + 解码 + 保存 ----
    if use_basic_guider:
        graph["16"] = {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": pos_ref}}
    else:
        graph["16"] = {
            "class_type": "CFGGuider",
            "inputs": {"model": ["1", 0], "positive": pos_ref, "negative": neg_ref, "cfg": float(cfg)},
        }
    graph["17"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler_name}}
    graph["18"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}}
    graph["19"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["18", 0],
            "guider": ["16", 0],
            "sampler": ["17", 0],
            "sigmas": ["15", 0],
            "latent_image": ["14", 0],
        },
    }
    graph["20"] = {"class_type": "VAEDecode", "inputs": {"samples": ["19", 0], "vae": ["3", 0]}}
    graph["21"] = {
        "class_type": "SaveImage",
        "inputs": {"images": ["20", 0], "filename_prefix": "AIBAR_flux2_pose"},
    }
    return graph


def ensure_flux2_pose_workflow(
    character_images: Sequence[str] = (),
    pose_image: str = "",
    positive: str = "",
    negative: str = "",
    **kwargs: Any,
) -> str | None:
    """把姿势参考工作流写入 ComfyUI 工作流目录（幂等覆盖写）。

    与 :func:`ensure_flux2_workflow` 同款：**两张图都留空时自动扫描 ComfyUI ``input/``**
    取 PNG——取 1 张当人物参考、有第 2 张就当姿势参考，保证落盘的是完整链。

    Returns:
        工作流文件名；目录不可用时返回 None。**绝不抛异常**。
    """
    from sync import paths as _sync_paths

    chars = [str(x or "").strip() for x in (character_images or ())]
    chars = [c for c in chars if c]
    if not chars and not str(pose_image or "").strip():
        try:
            found = sorted(_sync_paths.input_dir().glob("*.png"))
            if found:
                chars = [found[0].name]
            if len(found) > 1:
                pose_image = found[1].name
        except Exception:  # noqa: BLE001
            pass

    try:
        wf_dir = _sync_paths.workflows_dir()
        wf_dir.mkdir(parents=True, exist_ok=True)
        target = wf_dir / FLUX2_POSE_WORKFLOW_FILENAME
        import json

        graph = build_flux2_pose_workflow(
            character_images=chars,
            pose_image=pose_image,
            positive=positive,
            negative=negative,
            **kwargs,
        )
        target.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        return target.name
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "DEFAULT_DIFFUSION",
    "DEFAULT_TEXT_ENCODER",
    "DEFAULT_VAE",
    "DEFAULT_STEPS_DISTILLED",
    "DEFAULT_CFG_DISTILLED",
    "DEFAULT_STEPS_BASE",
    "DEFAULT_CFG_BASE",
    "DEFAULT_SAMPLER",
    "DEFAULT_WIDTH",
    "DEFAULT_HEIGHT",
    "FLUX2_WORKFLOW_FILENAME",
    "FLUX2_POSE_WORKFLOW_FILENAME",
    "MAX_CHARACTER_REFS",
    "CHARACTER_REF_ID_BASE",
    "build_flux2_workflow",
    "ensure_flux2_workflow",
    "build_flux2_pose_workflow",
    "ensure_flux2_pose_workflow",
]