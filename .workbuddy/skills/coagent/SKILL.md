---
name: coagent
description: 多人协同开发协议 —— 认领区域、检查占用、检索共享知识库。修改代码前必须调用 coagent_file_check。
---

# CoAgent 协同协议

- 修改代码文件前先调用 `coagent_file_check`，被占用则停止并告知用户
- 开始任务前 `coagent_status` → `coagent_area_claim` → `coagent_kb_search`
- 结束后 `coagent_kb_write` 沉淀知识 → `coagent_area_release` 释放
