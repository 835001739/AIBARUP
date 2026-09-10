"""M6 · AI 绘画提示词工作台（Prompt Studio）。

只包含四个职责单一的模块，彼此之间单向依赖：

```
library   —— 版本化知识库（resources/prompt_library.json）的加载与查询
engine    —— 确定性规则扩写引擎
providers —— 可插拔扩写 Provider（rules 必实现，openai_compatible 可选并自动回退）
routes    —— Flask Blueprint，对外暴露 /api/prompt-library 与 /api/prompts/*
```

外部（如 ``app.py``）只需 ``from prompt.routes import bp`` 完成装配。
"""
