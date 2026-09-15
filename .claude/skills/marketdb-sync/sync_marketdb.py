#!/usr/bin/env python3
"""手动增量更新本地 marketdb 行情库（薄封装 tools/marketdb_local.py sync）。

用法:
    python .claude/skills/marketdb-sync/sync_marketdb.py

等价于:
    python tools/marketdb_local.py sync

输出: auto-sync 决策（skip/incremental/full）+ 各表行数状态。
路径全部动态定位（不写死），可跨环境运行。
"""
import subprocess
import sys
from pathlib import Path

# 定位项目根（skills/marketdb-sync -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
_TOOL = _ROOT / "tools" / "marketdb_local.py"


def main() -> int:
    if not _TOOL.exists():
        print(f"❌ 未找到工具脚本: {_TOOL}")
        return 1
    return subprocess.run([sys.executable, str(_TOOL), "sync"]).returncode


if __name__ == "__main__":
    sys.exit(main())
