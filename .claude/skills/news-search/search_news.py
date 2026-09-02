#!/usr/bin/env python3
"""财经资讯/新闻/公告/研报搜索（妙想 Miaoxiang fin_search）

用法:
    py search_news.py <关键词> [小时]

参数:
    关键词  自然语言搜索词（如「宁德时代 减持」「半导体 利好」「贵州茅台 研报 目标价」）
    小时    只保留最近 N 小时内的资讯（可选，默认最近 7 天）

示例:
    py search_news.py 宁德时代 减持
    py search_news.py 半导体板块 利好 24
    py search_news.py 贵州茅台 研报 目标价

输出: 按类型分组（研报/公告/新闻/其他）的资讯列表，含评级/机构/关联证券。
"""
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 定位项目根目录（skills/news-search 上三级：news-search -> skills -> .claude -> 项目根）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app.config import Config
from app.utils import load_env
from app.miaoxiang import MXClient

# 类型标签映射（妙想 informationType -> 中文分组）
_TYPE_LABEL = {
    "REPORT": "研报",
    "ANNOUNCEMENT": "公告",
    "NEWS": "新闻",
}


def _parse_args(argv):
    """解析命令行参数，返回 (keyword, hours)"""
    if not argv:
        return None, None
    keyword = argv[0].strip()
    hours = None
    for a in argv[1:]:
        a = a.strip()
        if a.isdigit():
            hours = max(1, int(a))
    return keyword, hours


def _format_items(items):
    """把 fin_search_structured 结果按类型分组渲染为文本"""
    if not items:
        return "⚠️ 未搜索到相关资讯\n"

    # 按类型分组，保持原顺序
    groups = {"研报": [], "公告": [], "新闻": [], "其他": []}
    for it in items:
        label = _TYPE_LABEL.get(it.get("information_type"), "其他")
        groups.setdefault(label, []).append(it)

    lines = []
    for label, arr in groups.items():
        if not arr:
            continue
        lines.append(f"【{label}】{len(arr)} 条")
        for i, it in enumerate(arr, 1):
            meta = []
            if it.get("rating"):
                meta.append(it["rating"])
            if it.get("ins_name"):
                meta.append(it["ins_name"])
            if it.get("source"):
                meta.append(it["source"])
            if it.get("date"):
                meta.append(it["date"][:10])
            meta_str = f"  [{', '.join(meta)}]" if meta else ""
            lines.append(f"  {i}. {it.get('title', '?')}{meta_str}")

            # 关联证券
            secus = it.get("secu_list") or []
            if secus:
                secu_str = "、".join(
                    f"{s.get('name') or ''}({s.get('code') or ''})".strip("()")
                    for s in secus[:5]
                )
                if secu_str:
                    lines.append(f"     关联: {secu_str}")

            if it.get("url"):
                lines.append(f"     {it['url']}")
            if it.get("content"):
                lines.append(f"     {it['content'][:120]}")
        lines.append("")
    return "\n".join(lines)


def main():
    keyword, hours = _parse_args(sys.argv[1:])
    if not keyword:
        print(__doc__)
        return 2

    load_env(_ROOT)
    config = Config(_ROOT / "watchlist_config.json")
    api_keys = config.mx_apikeys
    if not api_keys:
        print("❌ 未配置 MX_APIKEY（请在 .env 中设置 MX_APIKEY / MX_APIKEY_2）")
        return 1

    mx = MXClient(api_keys)

    window = f"最近{hours}小时" if hours else "最近7天"
    print(f"=== 资讯搜索: {keyword}（{window}，已去重）===\n")

    items = mx.fin_search_structured(keyword, hours=hours)
    if not items:
        # 结构化解析失败时回退自然语言原始结果
        text = mx.fin_search_as_text(keyword, hours=hours)
        print(text or "❌ 无返回（非交易时间 / 无数据）")
        return 0

    print(_format_items(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
