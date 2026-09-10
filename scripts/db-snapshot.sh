#!/bin/bash
# 按需提交主数据库快照
#
# 背景：AIBARUP 运行时 SQLite 持续写 WAL，data/aibar.db 几乎每次运行都会变。
# 若让它跟随日常提交，每次都是 22MB 全量 diff，且会把无关的代码提交污染成数据提交。
# 做法：日常用 skip-worktree 屏蔽，需要留档时跑本脚本，临时解封 → 提交 → 推送 → 重新屏蔽。
#
# 用法：bash scripts/db-snapshot.sh
set -euo pipefail

cd "$(dirname "$0")/.."
DB="data/aibar.db"

git update-index --no-skip-worktree "$DB"
trap 'git update-index --skip-worktree "$DB" 2>/dev/null || true' EXIT

git add "$DB"
if git diff --cached --quiet; then
  echo "数据库无变化，无需快照"
  exit 0
fi

SIZE=$(du -h "$DB" | cut -f1)
git commit -m "数据库快照 $(date '+%Y-%m-%d %H:%M')（${SIZE}）"
git push origin main
echo "✓ 已推送数据库快照（${SIZE}）"
