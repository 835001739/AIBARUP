---
title: 统一 API 契约 ok/fail + AIBARError
type: convention
tags:
  - api
  - harness
  - contract
author: qinsu
created: 2026-09-10
---

成功返回 {ok:true,data:...}，失败返回 {ok:false,error:{code,message}}。路由必须用 core.responses.ok()/fail()，业务异常抛 core.errors.AIBARError(code,message,status)。每个 Blueprint 注册 errorhandler(AIBARError)，兜底 Exception 转 internal_error 且不泄漏堆栈。错误码用英文下划线，如 invalid_input/not_found/provider_offline。
