---
title: 建表只写 core/db.py，业务包禁止 CREATE TABLE
type: convention
tags:
  - db
  - harness
  - migration
author: qinsu
created: 2026-09-10
---

所有建表/索引/列迁移必须写进 core/db.py 的 _TABLES / _INDEXES / _COLUMN_MIGRATIONS 三个结构里，业务包内禁止出现 CREATE TABLE。查询统一走 core.db.execute/query_all/query_one，禁止业务包自己 sqlite3.connect。迁移必须幂等。
