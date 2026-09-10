"""M14+ · 姿势可控出图（ControlNet OpenPose + IPAdapter FaceID）。

解决什么问题
------------
M14 组图模块靠「同一段人物锚点 + 同一个种子」让连播起来像同一个人，但**动作只能靠
文本描述去碰运气**——写「抬起右手」模型可能抬左手、可能抬一半、可能整只手消失。
文本对「精确姿势」的控制力天生很弱。

正确的解法是把姿势从**文本**搬到**结构化的骨架图**上：

- **ControlNet（OpenPose）**吃一张骨架图，把人物的肢体摆位直接约束成骨架那样
  ——姿势从此是「画出来的」，不是「写出来的」，可精确到肘关节角度；
- **IPAdapter FaceID Plus v2**吃一张角色参考图，把脸锁住
  ——姿势怎么变脸都不漂，这才是「人物一致性」的正解（比同种子 + 文本锚点稳得多）。

两者是**正交**的：ControlNet 改 conditioning（控姿势），IPAdapter 改 model（锁脸），
互不干扰，一起接到 KSampler 上即可。

预置姿势库
----------
``POSES`` 里是 OpenPose 18 关键点格式的归一化坐标（x 向右、y 向下，0~1）。
``render_pose_skeleton`` 把它们渲染成 OpenPose 标准配色的骨架 PNG，
落盘到 ComfyUI 的 ``input/`` 目录，工作流里的 ``LoadImage`` 就能选到。

想加姿势？往 ``POSES`` 里加一条即可；想完全自定义？调 ``add_custom_pose``
传入 18 点坐标列表，或直接把骨架 PNG 丢进 ComfyUI input 目录。

设计约束（沿用 HARNESS / PRD）：
- **纯规则、可离线**：不依赖任何 AI 服务，骨架渲染是纯几何计算；
- **绝不抛异常**：路径/磁盘异常一律降级为 None 并记日志，不阻断调用方；
- **幂等**：重复调 ``ensure_*`` 不会产生重复文件（按内容哈希命名 / 覆盖写）；
- **用户可覆盖**：生成的 PNG 与 JSON 都在用户自己的 ComfyUI 目录里，可手改。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from core.logging_setup import get_logger, safe_log

_LOGGER = get_logger("aibar.comic.pose")

# ---------------------------------------------------------------- OpenPose 18 关键点

# 索引与 COCO/OpenPose body_25 的前 18 点一致（ControlNet openpose 就是在这套上训练的）
NOSE, NECK, R_SHOULDER, R_ELBOW, R_WRIST = 0, 1, 2, 3, 4
L_SHOULDER, L_ELBOW, L_WRIST, R_HIP, R_KNEE = 5, 6, 7, 8, 9
R_ANKLE, L_HIP, L_KNEE, L_ANKLE, R_EYE = 10, 11, 12, 13, 14
L_EYE, R_EAR, L_EAR = 15, 16, 17

KEYPOINT_NAMES = (
    "nose", "neck", "r_shoulder", "r_elbow", "r_wrist",
    "l_shoulder", "l_elbow", "l_wrist", "r_hip", "r_knee",
    "r_ankle", "l_hip", "l_knee", "l_ankle", "r_eye",
    "l_eye", "r_ear", "l_ear",
)

# 骨架连线：顺序与 OpenPose 官方一致。改顺序会换掉配色，ControlNet 的识别率会掉。
POSE_PAIRS = [
    (NECK, R_SHOULDER), (NECK, L_SHOULDER),
    (R_SHOULDER, R_ELBOW), (R_ELBOW, R_WRIST),
    (L_SHOULDER, L_ELBOW), (L_ELBOW, L_WRIST),
    (NECK, R_HIP), (R_HIP, R_KNEE), (R_KNEE, R_ANKLE),
    (NECK, L_HIP), (L_HIP, L_KNEE), (L_KNEE, L_ANKLE),
    (NECK, NOSE), (NOSE, R_EYE), (R_EYE, R_EAR),
    (NOSE, L_EYE), (L_EYE, L_EAR),
]

# OpenPose 官方配色（RGB）。模型在这套颜色上训练，换成黑白会明显掉精度。
POSE_COLORS = (
    (255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0),
    (170, 255, 0), (85, 255, 0), (0, 255, 0), (0, 255, 85),
    (0, 255, 170), (0, 255, 255), (0, 170, 255), (0, 85, 255),
    (255, 0, 170), (170, 0, 255), (255, 0, 255), (85, 0, 255),
    (0, 0, 255),
)

# ---------------------------------------------------------------- 预置姿势库

# 坐标是归一化的（x 向右、y 向下，0~1）。**注意左右是「人物自己的左右」**：
# r_* 在图像左侧（正面视角），和照镜子相反——这与 OpenPose 的标注约定一致。
POSES: dict[str, dict[str, Any]] = {
    "stand": {
        "label": "站立",
        "desc": "正面站立，双臂自然下垂",
        "points": {
            "nose": (0.50, 0.17), "neck": (0.50, 0.26),
            "r_shoulder": (0.41, 0.29), "r_elbow": (0.35, 0.42), "r_wrist": (0.32, 0.55),
            "l_shoulder": (0.59, 0.29), "l_elbow": (0.65, 0.42), "l_wrist": (0.68, 0.55),
            "r_hip": (0.44, 0.54), "r_knee": (0.44, 0.72), "r_ankle": (0.44, 0.90),
            "l_hip": (0.56, 0.54), "l_knee": (0.56, 0.72), "l_ankle": (0.56, 0.90),
            "r_eye": (0.47, 0.155), "l_eye": (0.53, 0.155),
            "r_ear": (0.45, 0.17), "l_ear": (0.55, 0.17),
        },
    },
    "arms_crossed": {
        "label": "抱臂",
        "desc": "双臂交叉在胸前",
        "points": {
            "nose": (0.50, 0.17), "neck": (0.50, 0.26),
            "r_shoulder": (0.41, 0.29), "r_elbow": (0.40, 0.40), "r_wrist": (0.58, 0.40),
            "l_shoulder": (0.59, 0.29), "l_elbow": (0.60, 0.40), "l_wrist": (0.42, 0.42),
            "r_hip": (0.44, 0.54), "r_knee": (0.44, 0.72), "r_ankle": (0.44, 0.90),
            "l_hip": (0.56, 0.54), "l_knee": (0.56, 0.72), "l_ankle": (0.56, 0.90),
            "r_eye": (0.47, 0.155), "l_eye": (0.53, 0.155),
            "r_ear": (0.45, 0.17), "l_ear": (0.55, 0.17),
        },
    },
    "raise_arms": {
        "label": "张开双臂",
        "desc": "双臂向两侧张开抬起",
        "points": {
            "nose": (0.50, 0.17), "neck": (0.50, 0.26),
            "r_shoulder": (0.41, 0.29), "r_elbow": (0.26, 0.26), "r_wrist": (0.13, 0.20),
            "l_shoulder": (0.59, 0.29), "l_elbow": (0.74, 0.26), "l_wrist": (0.87, 0.20),
            "r_hip": (0.44, 0.54), "r_knee": (0.44, 0.72), "r_ankle": (0.44, 0.90),
            "l_hip": (0.56, 0.54), "l_knee": (0.56, 0.72), "l_ankle": (0.56, 0.90),
            "r_eye": (0.47, 0.155), "l_eye": (0.53, 0.155),
            "r_ear": (0.45, 0.17), "l_ear": (0.55, 0.17),
        },
    },
    "wave": {
        "label": "挥手",
        "desc": "右手举过头顶挥动",
        "points": {
            "nose": (0.50, 0.17), "neck": (0.50, 0.26),
            "r_shoulder": (0.41, 0.29), "r_elbow": (0.32, 0.22), "r_wrist": (0.26, 0.10),
            "l_shoulder": (0.59, 0.29), "l_elbow": (0.65, 0.42), "l_wrist": (0.68, 0.55),
            "r_hip": (0.44, 0.54), "r_knee": (0.44, 0.72), "r_ankle": (0.44, 0.90),
            "l_hip": (0.56, 0.54), "l_knee": (0.56, 0.72), "l_ankle": (0.56, 0.90),
            "r_eye": (0.47, 0.155), "l_eye": (0.53, 0.155),
            "r_ear": (0.45, 0.17), "l_ear": (0.55, 0.17),
        },
    },
    "point_forward": {
        "label": "指向前方",
        "desc": "右臂前伸指向前方",
        "points": {
            "nose": (0.50, 0.17), "neck": (0.50, 0.26),
            "r_shoulder": (0.42, 0.29), "r_elbow": (0.36, 0.30), "r_wrist": (0.22, 0.30),
            "l_shoulder": (0.58, 0.29), "l_elbow": (0.62, 0.42), "l_wrist": (0.64, 0.54),
            "r_hip": (0.45, 0.54), "r_knee": (0.45, 0.72), "r_ankle": (0.45, 0.90),
            "l_hip": (0.56, 0.54), "l_knee": (0.56, 0.72), "l_ankle": (0.56, 0.90),
            "r_eye": (0.47, 0.155), "l_eye": (0.53, 0.155),
            "r_ear": (0.45, 0.17), "l_ear": (0.55, 0.17),
        },
    },
    "walk": {
        "label": "走路",
        "desc": "行走中，前后摆臂迈步",
        "points": {
            "nose": (0.50, 0.17), "neck": (0.50, 0.26),
            "r_shoulder": (0.41, 0.29), "r_elbow": (0.36, 0.42), "r_wrist": (0.41, 0.55),
            "l_shoulder": (0.59, 0.29), "l_elbow": (0.64, 0.42), "l_wrist": (0.59, 0.55),
            "r_hip": (0.45, 0.54), "r_knee": (0.53, 0.70), "r_ankle": (0.60, 0.88),
            "l_hip": (0.55, 0.54), "l_knee": (0.47, 0.72), "l_ankle": (0.41, 0.90),
            "r_eye": (0.47, 0.155), "l_eye": (0.53, 0.155),
            "r_ear": (0.45, 0.17), "l_ear": (0.55, 0.17),
        },
    },
    "run": {
        "label": "跑步",
        "desc": "奔跑中，身体前倾，双臂弯曲摆动",
        "points": {
            "nose": (0.56, 0.19), "neck": (0.53, 0.28),
            "r_shoulder": (0.45, 0.31), "r_elbow": (0.36, 0.36), "r_wrist": (0.47, 0.35),
            "l_shoulder": (0.60, 0.32), "l_elbow": (0.70, 0.38), "l_wrist": (0.58, 0.44),
            "r_hip": (0.47, 0.56), "r_knee": (0.60, 0.66), "r_ankle": (0.68, 0.80),
            "l_hip": (0.55, 0.56), "l_knee": (0.46, 0.74), "l_ankle": (0.36, 0.88),
            "r_eye": (0.53, 0.175), "l_eye": (0.59, 0.175),
            "r_ear": (0.51, 0.19), "l_ear": (0.61, 0.19),
        },
    },
    "jump": {
        "label": "跳跃",
        "desc": "腾空跃起，双臂上扬双腿收屈",
        "points": {
            "nose": (0.50, 0.15), "neck": (0.50, 0.24),
            "r_shoulder": (0.41, 0.27), "r_elbow": (0.33, 0.19), "r_wrist": (0.28, 0.08),
            "l_shoulder": (0.59, 0.27), "l_elbow": (0.67, 0.19), "l_wrist": (0.72, 0.08),
            "r_hip": (0.44, 0.50), "r_knee": (0.38, 0.64), "r_ankle": (0.44, 0.78),
            "l_hip": (0.56, 0.50), "l_knee": (0.62, 0.64), "l_ankle": (0.56, 0.78),
            "r_eye": (0.47, 0.135), "l_eye": (0.53, 0.135),
            "r_ear": (0.45, 0.15), "l_ear": (0.55, 0.15),
        },
    },
    "punch_right": {
        "label": "右手出拳",
        "desc": "侧身，右臂前冲出拳",
        "points": {
            "nose": (0.52, 0.17), "neck": (0.50, 0.26),
            "r_shoulder": (0.45, 0.29), "r_elbow": (0.58, 0.30), "r_wrist": (0.74, 0.31),
            "l_shoulder": (0.53, 0.30), "l_elbow": (0.42, 0.39), "l_wrist": (0.32, 0.45),
            "r_hip": (0.45, 0.54), "r_knee": (0.56, 0.70), "r_ankle": (0.66, 0.88),
            "l_hip": (0.55, 0.54), "l_knee": (0.46, 0.72), "l_ankle": (0.38, 0.90),
            "r_eye": (0.49, 0.155), "l_eye": (0.55, 0.155),
            "r_ear": (0.47, 0.17), "l_ear": (0.57, 0.17),
        },
    },
    "kick": {
        "label": "踢腿",
        "desc": "单腿支撑，右腿前踢",
        "points": {
            "nose": (0.48, 0.17), "neck": (0.48, 0.26),
            "r_shoulder": (0.39, 0.29), "r_elbow": (0.31, 0.26), "r_wrist": (0.24, 0.20),
            "l_shoulder": (0.57, 0.29), "l_elbow": (0.66, 0.34), "l_wrist": (0.72, 0.28),
            "r_hip": (0.44, 0.54), "r_knee": (0.60, 0.58), "r_ankle": (0.78, 0.50),
            "l_hip": (0.52, 0.54), "l_knee": (0.52, 0.72), "l_ankle": (0.52, 0.90),
            "r_eye": (0.45, 0.155), "l_eye": (0.51, 0.155),
            "r_ear": (0.43, 0.17), "l_ear": (0.53, 0.17),
        },
    },
    "guard": {
        "label": "防御姿态",
        "desc": "双拳护住面部，重心下沉",
        "points": {
            "nose": (0.50, 0.17), "neck": (0.50, 0.27),
            "r_shoulder": (0.41, 0.30), "r_elbow": (0.38, 0.40), "r_wrist": (0.46, 0.25),
            "l_shoulder": (0.59, 0.30), "l_elbow": (0.62, 0.40), "l_wrist": (0.54, 0.25),
            "r_hip": (0.44, 0.56), "r_knee": (0.42, 0.73), "r_ankle": (0.42, 0.90),
            "l_hip": (0.56, 0.56), "l_knee": (0.58, 0.73), "l_ankle": (0.58, 0.90),
            "r_eye": (0.47, 0.155), "l_eye": (0.53, 0.155),
            "r_ear": (0.45, 0.17), "l_ear": (0.55, 0.17),
        },
    },
    "sword_ready": {
        "label": "持剑准备",
        "desc": "双手握剑柄置于身侧，待机",
        "points": {
            "nose": (0.50, 0.17), "neck": (0.50, 0.26),
            "r_shoulder": (0.41, 0.29), "r_elbow": (0.38, 0.42), "r_wrist": (0.52, 0.46),
            "l_shoulder": (0.59, 0.29), "l_elbow": (0.62, 0.42), "l_wrist": (0.55, 0.44),
            "r_hip": (0.44, 0.54), "r_knee": (0.42, 0.72), "r_ankle": (0.42, 0.90),
            "l_hip": (0.56, 0.54), "l_knee": (0.58, 0.72), "l_ankle": (0.58, 0.90),
            "r_eye": (0.47, 0.155), "l_eye": (0.53, 0.155),
            "r_ear": (0.45, 0.17), "l_ear": (0.55, 0.17),
        },
    },
    "sword_strike": {
        "label": "挥剑劈砍",
        "desc": "双手举剑过头向下劈",
        "points": {
            "nose": (0.52, 0.18), "neck": (0.51, 0.27),
            "r_shoulder": (0.43, 0.30), "r_elbow": (0.44, 0.19), "r_wrist": (0.58, 0.11),
            "l_shoulder": (0.60, 0.30), "l_elbow": (0.60, 0.20), "l_wrist": (0.62, 0.13),
            "r_hip": (0.45, 0.55), "r_knee": (0.48, 0.72), "r_ankle": (0.52, 0.90),
            "l_hip": (0.57, 0.55), "l_knee": (0.58, 0.74), "l_ankle": (0.60, 0.90),
            "r_eye": (0.49, 0.165), "l_eye": (0.55, 0.165),
            "r_ear": (0.47, 0.18), "l_ear": (0.57, 0.18),
        },
    },
    "draw_bow": {
        "label": "拉弓",
        "desc": "双臂一前一后拉满弓",
        "points": {
            "nose": (0.46, 0.17), "neck": (0.47, 0.26),
            "r_shoulder": (0.40, 0.29), "r_elbow": (0.30, 0.30), "r_wrist": (0.19, 0.31),
            "l_shoulder": (0.55, 0.29), "l_elbow": (0.64, 0.32), "l_wrist": (0.52, 0.30),
            "r_hip": (0.43, 0.54), "r_knee": (0.47, 0.72), "r_ankle": (0.52, 0.90),
            "l_hip": (0.55, 0.54), "l_knee": (0.51, 0.72), "l_ankle": (0.46, 0.90),
            "r_eye": (0.43, 0.155), "l_eye": (0.49, 0.155),
            "r_ear": (0.41, 0.17), "l_ear": (0.51, 0.17),
        },
    },
    "sit": {
        "label": "坐姿",
        "desc": "端坐，双腿并拢弯曲",
        "points": {
            "nose": (0.50, 0.20), "neck": (0.50, 0.29),
            "r_shoulder": (0.41, 0.32), "r_elbow": (0.36, 0.45), "r_wrist": (0.44, 0.56),
            "l_shoulder": (0.59, 0.32), "l_elbow": (0.64, 0.45), "l_wrist": (0.56, 0.56),
            "r_hip": (0.44, 0.60), "r_knee": (0.42, 0.72), "r_ankle": (0.44, 0.88),
            "l_hip": (0.56, 0.60), "l_knee": (0.58, 0.72), "l_ankle": (0.56, 0.88),
            "r_eye": (0.47, 0.185), "l_eye": (0.53, 0.185),
            "r_ear": (0.45, 0.20), "l_ear": (0.55, 0.20),
        },
    },
    "crouch": {
        "label": "蹲下",
        "desc": "深蹲，重心压低蓄力",
        "points": {
            "nose": (0.50, 0.24), "neck": (0.50, 0.33),
            "r_shoulder": (0.41, 0.36), "r_elbow": (0.34, 0.48), "r_wrist": (0.40, 0.58),
            "l_shoulder": (0.59, 0.36), "l_elbow": (0.66, 0.48), "l_wrist": (0.60, 0.58),
            "r_hip": (0.43, 0.64), "r_knee": (0.38, 0.72), "r_ankle": (0.44, 0.90),
            "l_hip": (0.57, 0.64), "l_knee": (0.62, 0.72), "l_ankle": (0.56, 0.90),
            "r_eye": (0.47, 0.225), "l_eye": (0.53, 0.225),
            "r_ear": (0.45, 0.24), "l_ear": (0.55, 0.24),
        },
    },
    "kneel": {
        "label": "跪下",
        "desc": "单膝跪地",
        "points": {
            "nose": (0.50, 0.22), "neck": (0.50, 0.31),
            "r_shoulder": (0.41, 0.34), "r_elbow": (0.36, 0.46), "r_wrist": (0.42, 0.57),
            "l_shoulder": (0.59, 0.34), "l_elbow": (0.65, 0.46), "l_wrist": (0.58, 0.57),
            "r_hip": (0.45, 0.60), "r_knee": (0.44, 0.80), "r_ankle": (0.46, 0.93),
            "l_hip": (0.56, 0.58), "l_knee": (0.64, 0.70), "l_ankle": (0.66, 0.90),
            "r_eye": (0.47, 0.205), "l_eye": (0.53, 0.205),
            "r_ear": (0.45, 0.22), "l_ear": (0.55, 0.22),
        },
    },
    "bow": {
        "label": "鞠躬",
        "desc": "上身前倾行礼",
        "points": {
            "nose": (0.52, 0.38), "neck": (0.50, 0.34),
            "r_shoulder": (0.41, 0.36), "r_elbow": (0.38, 0.48), "r_wrist": (0.36, 0.60),
            "l_shoulder": (0.59, 0.36), "l_elbow": (0.62, 0.48), "l_wrist": (0.64, 0.60),
            "r_hip": (0.44, 0.52), "r_knee": (0.44, 0.72), "r_ankle": (0.44, 0.90),
            "l_hip": (0.56, 0.52), "l_knee": (0.56, 0.72), "l_ankle": (0.56, 0.90),
            "r_eye": (0.50, 0.375), "l_eye": (0.56, 0.375),
            "r_ear": (0.48, 0.39), "l_ear": (0.58, 0.39),
        },
    },
    "think": {
        "label": "思考",
        "desc": "右手托腮沉思",
        "points": {
            "nose": (0.50, 0.17), "neck": (0.50, 0.26),
            "r_shoulder": (0.41, 0.29), "r_elbow": (0.40, 0.40), "r_wrist": (0.50, 0.23),
            "l_shoulder": (0.59, 0.29), "l_elbow": (0.62, 0.42), "l_wrist": (0.56, 0.52),
            "r_hip": (0.44, 0.54), "r_knee": (0.44, 0.72), "r_ankle": (0.44, 0.90),
            "l_hip": (0.56, 0.54), "l_knee": (0.56, 0.72), "l_ankle": (0.56, 0.90),
            "r_eye": (0.47, 0.155), "l_eye": (0.53, 0.155),
            "r_ear": (0.45, 0.17), "l_ear": (0.55, 0.17),
        },
    },
    "salute": {
        "label": "敬礼",
        "desc": "右手举至太阳穴",
        "points": {
            "nose": (0.50, 0.17), "neck": (0.50, 0.26),
            "r_shoulder": (0.41, 0.29), "r_elbow": (0.38, 0.32), "r_wrist": (0.47, 0.18),
            "l_shoulder": (0.59, 0.29), "l_elbow": (0.65, 0.42), "l_wrist": (0.67, 0.54),
            "r_hip": (0.44, 0.54), "r_knee": (0.44, 0.72), "r_ankle": (0.44, 0.90),
            "l_hip": (0.56, 0.54), "l_knee": (0.56, 0.72), "l_ankle": (0.56, 0.90),
            "r_eye": (0.47, 0.155), "l_eye": (0.53, 0.155),
            "r_ear": (0.45, 0.17), "l_ear": (0.55, 0.17),
        },
    },
    "dance": {
        "label": "舞蹈",
        "desc": "一手上扬一手续势，双腿交叉",
        "points": {
            "nose": (0.50, 0.16), "neck": (0.50, 0.25),
            "r_shoulder": (0.41, 0.28), "r_elbow": (0.33, 0.20), "r_wrist": (0.25, 0.10),
            "l_shoulder": (0.59, 0.28), "l_elbow": (0.70, 0.34), "l_wrist": (0.80, 0.38),
            "r_hip": (0.45, 0.52), "r_knee": (0.50, 0.70), "r_ankle": (0.56, 0.88),
            "l_hip": (0.56, 0.52), "l_knee": (0.58, 0.70), "l_ankle": (0.46, 0.88),
            "r_eye": (0.47, 0.145), "l_eye": (0.53, 0.145),
            "r_ear": (0.45, 0.16), "l_ear": (0.55, 0.16),
        },
    },
    "carry": {
        "label": "扛举",
        "desc": "右臂高举托举物件",
        "points": {
            "nose": (0.50, 0.18), "neck": (0.50, 0.27),
            "r_shoulder": (0.41, 0.30), "r_elbow": (0.34, 0.21), "r_wrist": (0.42, 0.10),
            "l_shoulder": (0.59, 0.30), "l_elbow": (0.66, 0.42), "l_wrist": (0.68, 0.54),
            "r_hip": (0.44, 0.55), "r_knee": (0.42, 0.73), "r_ankle": (0.42, 0.90),
            "l_hip": (0.56, 0.55), "l_knee": (0.58, 0.73), "l_ankle": (0.58, 0.90),
            "r_eye": (0.47, 0.165), "l_eye": (0.53, 0.165),
            "r_ear": (0.45, 0.18), "l_ear": (0.55, 0.18),
        },
    },
    "fall_back": {
        "label": "后仰跌倒",
        "desc": "重心后倒，四肢张开失衡",
        "points": {
            "nose": (0.62, 0.34), "neck": (0.56, 0.36),
            "r_shoulder": (0.48, 0.39), "r_elbow": (0.38, 0.30), "r_wrist": (0.28, 0.22),
            "l_shoulder": (0.52, 0.44), "l_elbow": (0.62, 0.50), "l_wrist": (0.72, 0.56),
            "r_hip": (0.44, 0.52), "r_knee": (0.30, 0.62), "r_ankle": (0.20, 0.74),
            "l_hip": (0.48, 0.58), "l_knee": (0.36, 0.74), "l_ankle": (0.26, 0.86),
            "r_eye": (0.60, 0.325), "l_eye": (0.66, 0.325),
            "r_ear": (0.58, 0.35), "l_ear": (0.68, 0.35),
        },
    },
}


# ---------------------------------------------------------------- 坐标转换


def pose_to_keypoints(pose: dict[str, Any]) -> list[list[float]]:
    """把命名点字典转成 OpenPose 18 点数组（缺失的点补 ``[0, 0]``）。

    缺失点用 ``[0,0]`` 而不是跳过：ControlNet 的输入张量长度固定是 18，
    少一个点会导致后续维度对不上。``[0,0]`` 在渲染时会被当作「不画」处理。
    """
    points = pose.get("points") or {}
    out: list[list[float]] = []
    for name in KEYPOINT_NAMES:
        pt = points.get(name)
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            out.append([float(pt[0]), float(pt[1])])
        else:
            out.append([0.0, 0.0])
    return out


def list_poses() -> list[dict[str, Any]]:
    """预置姿势清单（供前端下拉框使用）。"""
    return [
        {"key": key, "label": val.get("label") or key, "desc": val.get("desc") or ""}
        for key, val in POSES.items()
    ]


# ---------------------------------------------------------------- 骨架渲染


def render_pose_skeleton(
    keypoints: list[list[float]],
    width: int = 768,
    height: int = 1024,
    stick_width: int | None = None,
    background: tuple[int, int, int] = (0, 0, 0),
) -> bytes | None:
    """把 OpenPose 18 点渲染成骨架 PNG，返回 PNG 字节；失败返回 None。

    用**纯黑底 + OpenPose 官方配色**，因为 ControlNet openpose 就是在这类
    预处理输出上训练的——换成白底或单色线会显著降低控制精度。

    Args:
        keypoints: 18 个点，每点 ``[x, y]`` 归一化到 0~1；``[0, 0]`` 表示缺失。
        width / height: 输出尺寸。默认 768×1024（3:4），与组图/分镜的竖构图一致。
        stick_width: 骨架线宽；省略时按画面宽度自适应（约 ``width/110``）。
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        safe_log(_LOGGER, 30, "pose_render_no_pillow", error_type=type(exc).__name__)
        return None

    if len(keypoints) < 18:
        safe_log(_LOGGER, 30, "pose_render_bad_input", got=len(keypoints))
        return None

    try:
        img = Image.new("RGB", (int(width), int(height)), background)
        draw = ImageDraw.Draw(img)
        if stick_width is None:
            stick_width = max(3, int(width / 110))
        radius = max(3, int(stick_width * 1.5))

        def px(pt: list[float]) -> tuple[float, float]:
            return (float(pt[0]) * width, float(pt[1]) * height)

        def valid(pt: list[float]) -> bool:
            # [0,0] 是缺失标记；真实落在原点的概率可以忽略
            return bool(pt) and (float(pt[0]) > 1e-6 or float(pt[1]) > 1e-6)

        # 先画连线（在下层），再画关节（在上层），避免关节点被线盖住
        for idx, (a, b) in enumerate(POSE_PAIRS):
            if a >= len(keypoints) or b >= len(keypoints):
                continue
            pa, pb = keypoints[a], keypoints[b]
            if not (valid(pa) and valid(pb)):
                continue
            color = POSE_COLORS[idx % len(POSE_COLORS)]
            draw.line([px(pa), px(pb)], fill=color, width=stick_width)

            # 四肢中段加一个淡点，帮助模型判断肢体走向（OpenPose 原版也有）
            mid = [(pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0]
            if idx not in (0, 1, 12):  # 躯干与脖子不加
                draw.ellipse(
                    [mid[0] * width - radius * 0.5, mid[1] * height - radius * 0.5,
                     mid[0] * width + radius * 0.5, mid[1] * height + radius * 0.5],
                    fill=color,
                )

        for pt in keypoints:
            if not valid(pt):
                continue
            x, y = px(pt)
            draw.ellipse(
                [x - radius, y - radius, x + radius, y + radius],
                fill=(255, 255, 255),
            )

        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        safe_log(_LOGGER, 30, "pose_render_failed", error_type=type(exc).__name__)
        return None


# ---------------------------------------------------------------- 姿势库落盘


# 骨架图在 ComfyUI input 目录里的子目录名（避免和用户的图片混在一起）
POSE_SUBDIR = "aibar_poses"

# 落盘文件名前缀（LoadImage 下拉框里好认）
POSE_FILE_PREFIX = "pose_"


def _pose_hash(keypoints: list[list[float]]) -> str:
    """骨架内容哈希：内容不变则文件名不变（幂等，不会重复堆文件）。"""
    raw = json.dumps([[round(float(p[0]), 4), round(float(p[1]), 4)] for p in keypoints])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def ensure_pose_library(width: int = 768, height: int = 1024) -> dict[str, Any]:
    """把全部预置姿势渲染成骨架 PNG，落盘到 ComfyUI ``input/aibar_poses/``。

    Returns:
        ``{"dir", "written", "skipped", "failed", "poses": [{key, file, path}]}``。
        目录不可用时返回 ``{"dir": None, ...}``，**绝不抛异常**。
    """
    from sync import paths as _sync_paths

    base = _sync_paths.input_dir()
    result: dict[str, Any] = {
        "dir": str(base) if base else None,
        "written": 0,
        "skipped": 0,
        "failed": 0,
        "poses": [],
    }
    if base is None:
        safe_log(_LOGGER, 30, "pose_library_skip", reason="input_dir_unavailable")
        return result

    target_dir = base / POSE_SUBDIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        safe_log(_LOGGER, 30, "pose_library_mkdir_failed", error_type=type(exc).__name__)
        return result

    for key, pose in POSES.items():
        kps = pose_to_keypoints(pose)
        png = render_pose_skeleton(kps, width=width, height=height)
        if not png:
            result["failed"] += 1
            continue
        filename = "%s%s_%s.png" % (POSE_FILE_PREFIX, key, _pose_hash(kps))
        dest = target_dir / filename
        try:
            if dest.is_file() and dest.read_bytes() == png:
                result["skipped"] += 1
            else:
                dest.write_bytes(png)
                result["written"] += 1
        except Exception as exc:
            safe_log(_LOGGER, 30, "pose_library_write_failed", pose=key, error_type=type(exc).__name__)
            result["failed"] += 1
            continue
        result["poses"].append({
            "key": key,
            "label": pose.get("label") or key,
            # LoadImage 下拉框里显示的相对路径（ComfyUI 以 input/ 为根）
            "file": "%s/%s" % (POSE_SUBDIR, filename),
            "path": str(dest),
        })

    safe_log(
        _LOGGER, 20, "pose_library_ready",
        written=result["written"], skipped=result["skipped"], failed=result["failed"],
    )
    return result


def add_custom_pose(
    key: str,
    keypoints: list[list[float]],
    label: str = "",
    desc: str = "",
    width: int = 768,
    height: int = 1024,
) -> dict[str, Any]:
    """渲染并落盘一个自定义姿势（用户自己给的 18 点坐标）。

    Args:
        key: 姿势标识（英文/数字，会进文件名）。
        keypoints: 18 个点 ``[x, y]``，归一化 0~1；缺失点写 ``[0, 0]``。
        label / desc: 展示用中文名与说明。

    Returns:
        ``{"ok", "key", "file", "path", "reason"}``；失败时 ``ok=False`` 并带 reason。
    """
    from sync import paths as _sync_paths

    if not key or not isinstance(keypoints, list) or len(keypoints) < 18:
        return {"ok": False, "key": key, "reason": "需要 key 与 18 个关键点"}

    base = _sync_paths.input_dir()
    if base is None:
        return {"ok": False, "key": key, "reason": "ComfyUI input 目录不可用"}

    png = render_pose_skeleton(keypoints, width=width, height=height)
    if not png:
        return {"ok": False, "key": key, "reason": "骨架渲染失败"}

    target_dir = base / POSE_SUBDIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return {"ok": False, "key": key, "reason": "目录创建失败：%s" % type(exc).__name__}

    filename = "%s%s_%s.png" % (POSE_FILE_PREFIX, key, _pose_hash(keypoints))
    dest = target_dir / filename
    try:
        dest.write_bytes(png)
    except Exception as exc:
        return {"ok": False, "key": key, "reason": "写盘失败：%s" % type(exc).__name__}

    # 同步进内存里的 POSES，后续 list_poses / 组图面板都能直接看到
    points = {KEYPOINT_NAMES[i]: (keypoints[i][0], keypoints[i][1]) for i in range(18)}
    POSES[key] = {"label": label or key, "desc": desc or "自定义姿势", "points": points}

    return {
        "ok": True,
        "key": key,
        "label": POSES[key]["label"],
        "file": "%s/%s" % (POSE_SUBDIR, filename),
        "path": str(dest),
    }


# ---------------------------------------------------------------- 工作流

POSE_WORKFLOW_FILENAME = "aibar_pose_consistent.json"
# 纯 openpose 控姿版（不锁脸）：只要姿势、不要人物一致性时用这个。
# 与 POSE_WORKFLOW_FILENAME 分开落盘，互不覆盖。
POSE_OPENPOSE_FILENAME = "aibar_pose_openpose.json"

# 默认模型。选它们的理由：
# - animagine_xl_3.1：SDXL 动漫底模，契合漫画/动画人物场景（写实可换 sd_xl_base_1.0）；
# - xinsir_controlnet-union-sdxl-1.0：SDXL 多合一 ControlNet，切到 openpose 类型即可控姿势；
# - ip-adapter-faceid-plusv2_sdxl：锁脸最强的一档，配 insightface 的人脸特征；
# - CLIP-ViT-H：FaceID **PLUS** 版本必须给 clip_vision，否则退化为纯 FaceID。
DEFAULT_CHECKPOINT = "animagine_xl_3.1.safetensors"
DEFAULT_CONTROLNET = "xinsir_controlnet-union-sdxl-1.0.safetensors"
DEFAULT_CLIP_VISION = "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"

# 采样默认值：SDXL 常规配置
DEFAULT_STEPS = 28
DEFAULT_CFG = 6.5
DEFAULT_SAMPLER = "dpmpp_2m"
DEFAULT_SCHEDULER = "karras"
DEFAULT_WIDTH = 896
DEFAULT_HEIGHT = 1152

# IPAdapter FaceID Plus v2 默认强度：用户可调，但默认偏强才能稳。
# 经验值：lora_strength 0.85 + weight/weight_faceidv2 0.85 + embeds_scaling=K+V w/ C penalty
# 在 SDXL/MPS 上能给出比较稳的脸锁；偏小脸会模糊、偏大出图失真。
DEFAULT_IPADAPTER_WEIGHT = 0.85
DEFAULT_FACEIDV2_WEIGHT = 0.85
DEFAULT_LORA_STRENGTH = 0.85


def build_pose_workflow(
    reference_image: str = "",
    pose_image: str = "",
    positive: str = "",
    negative: str = "",
    checkpoint: str = DEFAULT_CHECKPOINT,
    controlnet: str = DEFAULT_CONTROLNET,
    clip_vision: str = DEFAULT_CLIP_VISION,
    seed: int = 0,
    steps: int = DEFAULT_STEPS,
    cfg: float = DEFAULT_CFG,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    controlnet_strength: float = 0.9,
    ipadapter_weight: float = DEFAULT_IPADAPTER_WEIGHT,
    faceidv2_weight: float = DEFAULT_FACEIDV2_WEIGHT,
    lora_strength: float = DEFAULT_LORA_STRENGTH,
    insightface_provider: str = "CPU",
    embeds_scaling: str = "K+V w/ C penalty",
) -> dict[str, Any]:
    """构造「参考图锁脸 + 骨架图控姿势」的 API 格式工作流。

    节点拓扑（两条正交的链，最后汇入 KSampler）：::

        1  CheckpointLoaderSimple ─┬─ MODEL ─→ 3 IPAdapterUnifiedLoaderFaceID
          (CLIP)                  │             (MODEL, IPADAPTER)
                                  │                   │
        4  LoadImage(参考图) ─────┴────────────→ 5 IPAdapterFaceID ── MODEL' ──┐
        2  CLIPVisionLoader ─────────────────────→ 5                          │
                                                                              ├→ 13 KSampler
        9  CLIPTextEncode(正) ─┐                                              │
        10 CLIPTextEncode(负) ─┴→ 11 ControlNetApplyAdvanced ── CONDITIONING ─┘
        6  LoadImage(骨架图) ─────→ 11
        7  ControlNetLoader → 8 SetUnionControlNetType(openpose) → 11

    Args:
        reference_image: 角色参考图（ComfyUI ``input/`` 下的相对路径），用于锁脸；
            留空则跳过 IPAdapter（只控姿势，不锁脸）。
        pose_image: 骨架图（ComfyUI ``input/`` 下的相对路径）；留空则跳过 ControlNet。
        positive / negative: 提示词。留空则由 AIBAR 的 runner 注入。
    """
    graph: dict[str, Any] = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "12": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": int(width), "height": int(height), "batch_size": 1},
        },
        "9": {
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "正向提示词"},
            "inputs": {"text": positive, "clip": ["1", 1]},
        },
        "10": {
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "负面提示词"},
            "inputs": {"text": negative, "clip": ["1", 1]},
        },
    }

    model_ref: list[Any] = ["1", 0]     # 可能被 IPAdapter 替换
    positive_ref: list[Any] = ["9", 0]
    negative_ref: list[Any] = ["10", 0]

    # ---- 锁脸链：IPAdapter FaceID Plus v2（需要参考图才启用）----
    if reference_image:
        graph["2"] = {
            "class_type": "CLIPVisionLoader",
            "inputs": {"clip_name": clip_vision},
        }
        graph["3"] = {
            "class_type": "IPAdapterUnifiedLoaderFaceID",
            "inputs": {
                "model": ["1", 0],
                "preset": "FACEID PLUS V2",
                "lora_strength": float(lora_strength),
                "provider": insightface_provider,
            },
        }
        graph["4"] = {
            "class_type": "LoadImage",
            "inputs": {"image": reference_image},
        }
        graph["5"] = {
            "class_type": "IPAdapterFaceID",
            "inputs": {
                "model": ["3", 0],
                "ipadapter": ["3", 1],
                "image": ["4", 0],
                "weight": float(ipadapter_weight),
                "weight_faceidv2": float(faceidv2_weight),
                "weight_type": "linear",
                "combine_embeds": "concat",
                "start_at": 0.0,
                "end_at": 1.0,
                "embeds_scaling": embeds_scaling,
                "clip_vision": ["2", 0],
            },
        }
        model_ref = ["5", 0]

    # ---- 控姿势链：ControlNet Union(openpose)（需要骨架图才启用）----
    if pose_image:
        graph["6"] = {
            "class_type": "LoadImage",
            "inputs": {"image": pose_image},
        }
        graph["7"] = {
            "class_type": "ControlNetLoader",
            "inputs": {"control_net_name": controlnet},
        }
        graph["8"] = {
            "class_type": "SetUnionControlNetType",
            "inputs": {"control_net": ["7", 0], "type": "openpose"},
        }
        graph["11"] = {
            "class_type": "ControlNetApplyAdvanced",
            "inputs": {
                "positive": positive_ref,
                "negative": negative_ref,
                "control_net": ["8", 0],
                "image": ["6", 0],
                "strength": float(controlnet_strength),
                "start_percent": 0.0,
                "end_percent": 1.0,
            },
        }
        positive_ref = ["11", 0]
        negative_ref = ["11", 1]

    graph["13"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": model_ref,
            "seed": int(seed),
            "steps": int(steps),
            "cfg": float(cfg),
            "sampler_name": DEFAULT_SAMPLER,
            "scheduler": DEFAULT_SCHEDULER,
            "positive": positive_ref,
            "negative": negative_ref,
            "latent_image": ["12", 0],
            "denoise": 1.0,
        },
    }
    graph["14"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["13", 0], "vae": ["1", 2]},
    }
    graph["15"] = {
        "class_type": "SaveImage",
        "inputs": {"images": ["14", 0], "filename_prefix": "AIBAR_pose"},
    }
    return graph


def ensure_pose_workflow(
    reference_image: str = "",
    pose_image: str = "",
    positive: str = "",
    negative: str = "",
    filename: str = POSE_WORKFLOW_FILENAME,
    auto_fill: bool = True,
    **kwargs: Any,
) -> str | None:
    """把姿势工作流写入 ComfyUI 工作流目录（幂等覆盖写）。

    Returns:
        工作流文件名；目录不可用时返回 None。**绝不抛异常**。

    **关键行为**：当两个图像路径都留空时，**自动扫描** ComfyUI input 目录：
    - 第一张 `actor_*.png` 或类似名字的图作为参考图（锁脸链）；
    - 第一张预置骨架 PNG 作为姿势图（控姿势链）；
    保证落盘的工作流文件是**完整 15 节点链路**而不是最小 7 节点 —— AIBAR 的
    组图 generate_group 直接读这个文件出图，缺链路就锁不住脸也控不住姿势。
    用户后续可以在 UI 里改图，或者调 ``build_pose_workflow`` + 自己的参数重生成。

    Args:
        filename: 落盘文件名，默认 :data:`POSE_WORKFLOW_FILENAME`。
        auto_fill: 是否启用「图像留空时自动扫描 input 目录补齐」的行为，默认 True。
            **落纯 openpose 版时必须传 ``auto_fill=False``**——否则留空的
            ``reference_image`` 会被自动扫描补上第一张 ``actor_*.png``，
            结果落出来的还是 15 节点锁脸版（本函数早期就踩过这个坑）。

    落两种版本的正确写法::

        # A. 锁脸 + 控姿（15 节点）
        ensure_pose_workflow(reference_image="aibar_poses/actor_ref.png",
                             pose_image="aibar_poses/pose_0000.png")
        # B. 纯 openpose 控姿（11 节点，不锁脸）—— 必须关掉 auto_fill
        ensure_pose_workflow(pose_image="aibar_poses/pose_0000.png",
                             filename=POSE_OPENPOSE_FILENAME, auto_fill=False)
    """
    from sync import paths as _sync_paths

    base = _sync_paths.workflows_dir()
    if base is None:
        safe_log(_LOGGER, 30, "pose_wf_skip", reason="workflows_dir_unavailable")
        return None

    # 智能默认：补足缺失的链路输入
    # auto_fill=False 时完全不扫描 —— 调用方要的就是「缺链」的工作流（如纯 openpose 版）
    if auto_fill and (not reference_image or not pose_image):
        try:
            pose_root = _sync_paths.input_dir() / POSE_SUBDIR if _sync_paths.input_dir() else None
            if pose_root and pose_root.is_dir():
                # 参考图：优先 actor_*.png 这种「角色定妆图」命名约定；否则取第一个 PNG
                if not reference_image:
                    candidates = sorted(pose_root.glob("*.png"))
                    actor_files = [p for p in candidates if p.name.startswith(("actor_", "ref_", "face_"))]
                    ref = actor_files[0] if actor_files else (candidates[0] if candidates else None)
                    if ref:
                        reference_image = "%s/%s" % (POSE_SUBDIR, ref.name)
                # 姿势图：第一张预置骨架
                if not pose_image:
                    first = next(iter(pose_root.glob("pose_*.png")), None)
                    if first:
                        pose_image = "%s/%s" % (POSE_SUBDIR, first.name)
        except Exception as exc:
            safe_log(_LOGGER, 30, "pose_wf_auto_fill_failed", error_type=type(exc).__name__)

    graph = build_pose_workflow(
        reference_image=reference_image,
        pose_image=pose_image,
        positive=positive,
        negative=negative,
        **kwargs,
    )
    target = base / filename
    try:
        target.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        safe_log(_LOGGER, 30, "pose_wf_write_failed", error_type=type(exc).__name__)
        return None
    safe_log(
        _LOGGER, 20, "pose_wf_written",
        path=str(target), nodes=len(graph),
        reference_image=reference_image or "(none)",
        pose_image=pose_image or "(none)",
    )
    return filename


__all__ = [
    "POSES",
    "POSE_PAIRS",
    "POSE_COLORS",
    "KEYPOINT_NAMES",
    "POSE_SUBDIR",
    "POSE_WORKFLOW_FILENAME",
    "POSE_OPENPOSE_FILENAME",
    "DEFAULT_CHECKPOINT",
    "DEFAULT_CONTROLNET",
    "DEFAULT_CLIP_VISION",
    "pose_to_keypoints",
    "list_poses",
    "render_pose_skeleton",
    "ensure_pose_library",
    "add_custom_pose",
    "build_pose_workflow",
    "ensure_pose_workflow",
]
