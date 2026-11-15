#!/usr/bin/env python3
"""热门深度专题（同花顺 news/topic/hot-list）。

用法:
    py .claude/skills/akshare-news/topic_news.py [数量] [--important] [--all]

参数:
    数量          最多显示的专题条数（可选，默认 10，单页上限 10）
    --important   只显示重要内容（important_flag 过滤）
    --all         沿游标翻页拉全量（默认只取首页 10 条）

数据源: 同花顺 news/topic/hot-list（热门栏目 tab_id=1），需 HITHINK_FINANCE_API_KEY。
返回热门深度专题（标题/内容类型/跳转链接/置顶与重要标识/来源时间）。
"""
import os
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/akshare-news 上三级），加载 .env
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
try:
    from app.utils import load_env
    load_env(_ROOT)
except Exception:
    pass

# 同花顺热门专题栏目 ID（tab_id），示例值已验证可用
_HITHINK_TOPIC_TAB_ID = int(os.environ.get("HITHINK_TOPIC_TAB_ID", "1"))

DEFAULT_LIMIT = 10
_MAX_PAGES = 5  # --all 时的翻页上限，防无界循环


def _parse_args(argv):
    """解析命令行参数，返回 (limit, important, all)。"""
    limit = DEFAULT_LIMIT
    important = False
    fetch_all = False
    for a in argv:
        a = a.strip()
        if a == "--important":
            important = True
        elif a == "--all":
            fetch_all = True
        elif a.isdigit():
            limit = max(1, int(a))
    return limit, important, fetch_all


def _fmt_ts(ts):
    """把 'YYYY-MM-DD HH:MM:SS' 精简为 'MM-DD HH:MM'；未知格式原样返回前 16 位。"""
    if not ts:
        return "?"
    s = str(ts).strip()
    if len(s) >= 16:
        return s[5:16]
    return s or "?"


def _fetch_topics(limit, important, fetch_all):
    """拉热门深度专题，返回 items 列表。失败返回 None。"""
    try:
        from app import hithink
    except Exception as e:
        print(f"⚠️ 加载同花顺客户端失败: {e}")
        return None

    items = []
    cursor = ""
    for _ in range(_MAX_PAGES):
        data = hithink.fetch_news_topic_hot_list(
            _HITHINK_TOPIC_TAB_ID, cursor=cursor, limit=limit, important_only=important
        )
        if not data:
            break
        batch = data.get("items") or []
        items.extend(batch)
        cursor = data.get("next_cursor") or ""
        has_more = data.get("has_more")
        if not fetch_all or not has_more or not cursor:
            break
    return items or None


def _render(items, limit):
    """渲染专题条目为 (title, type, url, important, pinned, updated)。"""
    rows = []
    for it in items:
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        rows.append((
            title,
            str(it.get("type") or "").strip(),
            str(it.get("url") or "").strip(),
            it.get("important_flag"),
            it.get("pinned_flag"),
            it.get("source_updated_at"),
        ))
        if len(rows) >= limit:
            break
    return rows


def main():
    limit, important, fetch_all = _parse_args(sys.argv[1:])
    items = _fetch_topics(limit, important, fetch_all)
    if not items:
        print("⚠️ 未获取到热门深度专题（未配置 HITHINK_FINANCE_API_KEY 或接口不可达）")
        return 1

    rows = _render(items, len(items) if fetch_all else limit)
    print(f"=== 热门深度专题（同花顺，tab_id={_HITHINK_TOPIC_TAB_ID}，"
          f"{'重要' if important else '全部'}，共 {len(rows)} 条）===\n")
    for i, (title, typ, url, important_flag, pinned_flag, updated) in enumerate(rows, 1):
        flags = []
        if pinned_flag:
            flags.append("置顶")
        if important_flag:
            flags.append("重要")
        meta = "、".join(x for x in [typ, _fmt_ts(updated)] + flags if x)
        print(f"  {i}. {title}  [{meta}]")
        if url:
            print(f"     {url}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
