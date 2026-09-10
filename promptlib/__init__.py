"""M7 · 多模态精品提示词数据库与自动学习。

包内单向依赖（HARNESS §2）：

``config / core``  ←  ``promptlib.entries``  ←  ``promptlib.learning``  ←  ``promptlib.routes``

- ``entries``：词条检索、分面统计、增删改、收藏、回填记录、内置种子导入，是唯一的写入入口；
- ``learning``：自动学习流水线（规范化 → 分类 → 切分 → 过滤 → 去重 → 评分 → 入库/候选），
  对外提供 ``ingest_segments`` / ``learn_from_generation`` 供 M9 反推模块复用；
- ``routes``：Blueprint ``promptlib``（前缀 ``/api``），只做入参校验与响应封装。

M9 反推入库只允许调用 ``learning`` 暴露的接口，不得直接写表。
"""
