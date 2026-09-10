"""AIBAR Bridge —— ComfyUI 前端桥梁扩展（不提供任何节点）。

用途
----
让 ComfyUI 前端支持 AIBAR 站点的深链接：从 URL 参数拉取工作流 JSON 与提示词，
自动载入画布并填好提示词，从而实现「点一个链接 → ComfyUI 已就绪可直接出图」。

    http://127.0.0.1:8188/
        ?aibar_wf=<工作流 JSON 的绝对 URL>
        &aibar_prompt=<正向提示词>
        &aibar_neg=<负向提示词>
        &aibar_target=<可选，指定写入提示词的节点 id>
        &aibar_name=<可选，仅用于展示>

实现说明
--------
- 本包**只注册静态资源目录**（``js/``），不注册任何节点，因此不会进入节点图谱、
  不会影响任何已有工作流。ComfyUI 通过 ``WEB_DIRECTORY`` 机制把 ``js/`` 挂到
  ``/extensions/AIBAR-Bridge/`` 并自动注入。
- 提示词的写入只改 widget 的值，不改写画布结构。
"""

WEB_DIRECTORY = "./js"

# 空映射：声明存在的目的是让 ComfyUI 的加载器把本模块识别为「合法的自定义节点包」，
# 从而在读取 WEB_DIRECTORY 之后正常返回，不会因为缺少节点定义而被判为加载失败。
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = ["WEB_DIRECTORY", "NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
