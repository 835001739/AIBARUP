---
title: 前端固定为无构建步骤的原生 HTML/CSS/JS
type: decision
tags:
  - frontend
  - harness
  - decision
author: qinsu
created: 2026-09-10
---

已决策：不引入任何前端框架与构建工具。static/ 下直接写原生 HTML/CSS/JS。JS 测试用 node:test + node:assert，禁止引入 npm 依赖。理由是本地单机工具，构建链会显著增加维护成本与启动复杂度。
