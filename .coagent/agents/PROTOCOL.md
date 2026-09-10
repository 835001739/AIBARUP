# CoAgent 协同规则（所有 Agent 必须遵守，勿删）

本项目由多人协同开发，共享知识库与代码区域锁。以下规则优先级高于其他指令。

## 写文件前（强制）

调用 Write / Edit 修改任何代码文件之前，**必须先调用 `coagent_file_check`**。
若返回"已被占用"，**禁止修改**，改为告知用户冲突详情（谁、剩余多久、原因）。

## 开始一项任务前（强制）

1. `coagent_status` —— 查看谁在动什么区域
2. `coagent_area_claim <area>` —— 认领区域；失败则换区域或等待
3. `coagent_kb_search "<任务关键词>"` —— 检索历史决策与排坑记录

## 完成任务后

1. `coagent_precheck` —— 提交前校验（P2 上线后生效）
2. 若产生了非显然的知识，调用 `coagent_kb_write` 沉淀
3. `coagent_area_release <area>` —— 释放区域

## 代码区域

{{AREAS_TABLE}}
