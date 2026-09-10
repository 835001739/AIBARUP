"""M1-M4 同步引擎包：ComfyUI 控制、工作流/图库解析扫描、后台轮询。

依赖方向：本包只能依赖 ``config`` 与 ``core``，不得反向依赖其他业务包。
导入约定：路由通过 ``from sync.routes import bp`` 装配，其余模块按需导入，
包初始化不做任何 IO，避免 import 期副作用。
"""
