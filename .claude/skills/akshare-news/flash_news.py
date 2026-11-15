#!/usr/bin/env python3
"""实时财经快讯流（东财/新浪 AKShare + 同花顺快讯）。

用法:
    py .claude/skills/akshare-news/flash_news.py [关键词] [条数]

参数:
    关键词   自然语言词，在标题/摘要/内容里子串匹配过滤（可选，缺省返回最新全局快讯）
    条数     最多显示的快讯条数（可选，默认 50）

数据源:
    - 东财 stock_info_global_em：全球快讯约 200 条（标题/摘要/发布时间/链接）
    - 新浪 stock_info_global_sina：最新 20 条电报式快讯（时间/内容）
    - 同花顺 news/flash/list：实时快讯（标题/摘要/栏目标签，tag_id=21103），需 HITHINK_FINANCE_API_KEY
    前两者免费、实时，无需 MX_APIKEY，仅需 pip install akshare；同花顺源为可选补充。

输出: 分三段——【东财全球快讯】（带链接可深挖）+【新浪快讯】（电报式最新）+【同花顺快讯】（栏目标签）。
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

# 禁用 akshare/tqdm 进度条，避免污染输出
os.environ.setdefault("TQDM_DISABLE", "1")

# 定位项目根目录（skills/akshare-news 上三级），加载 .env 供同花顺兜底源使用
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
try:
    from app.utils import load_env
    load_env(_ROOT)
except Exception:
    pass

_ak = None
try:
    import akshare as ak
    _ak = ak
except ImportError:
    pass

# 同花顺快讯栏目 ID（快讯），由同花顺资讯接口提供，示例值已验证可用
_HITHINK_FLASH_TAG_ID = int(os.environ.get("HITHINK_FLASH_TAG_ID", "21103"))

DEFAULT_LIMIT = 50


def _parse_args(argv):
    """解析命令行参数，返回 (keyword, limit)。数字参数当作条数，其余拼接为关键词。"""
    keyword_parts = []
    limit = DEFAULT_LIMIT
    for a in argv:
        a = a.strip()
        if a.isdigit():
            limit = max(1, int(a))
        elif a:
            keyword_parts.append(a)
    keyword = " ".join(keyword_parts) if keyword_parts else None
    return keyword, limit


def _match(text, keyword):
    """关键词匹配：按空格拆分做 AND，全部命中才算匹配；无关键词恒真。"""
    if not keyword:
        return True
    return all(t in text for t in keyword.split())


def _fmt_ts(ts):
    """把 'YYYY-MM-DD HH:MM:SS' 精简为 'MM-DD HH:MM'。"""
    if not ts:
        return "?"
    s = str(ts).strip()
    if len(s) >= 16:
        return s[5:16]
    return s[:10]


def _fmt_ts_ms(ms):
    """毫秒时间戳 → 'MM-DD HH:MM'。"""
    if not ms:
        return "?"
    from datetime import datetime, timezone, timedelta
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone(timedelta(hours=8)))
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def _fetch_em():
    """拉东财全球快讯 DataFrame，失败返回 None。"""
    try:
        df = ak.stock_info_global_em()
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        print(f"⚠️ 东财全球快讯拉取失败: {e}")
        return None


def _fetch_sina():
    """拉新浪快讯 DataFrame，失败返回 None。"""
    try:
        df = ak.stock_info_global_sina()
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        print(f"⚠️ 新浪快讯拉取失败: {e}")
        return None


def _render_em(df, keyword, limit):
    """渲染东财全球快讯（标题/摘要/发布时间/链接）。"""
    rows = []
    for _, r in df.iterrows():
        title = str(r.get("标题", "") or "").strip()
        summary = str(r.get("摘要", "") or "").strip()
        if not _match(title + " " + summary, keyword):
            continue
        rows.append((r.get("发布时间"), title, summary, r.get("链接")))
        if len(rows) >= limit:
            break
    return rows


def _render_sina(df, keyword, limit):
    """渲染新浪快讯（时间/内容）。"""
    rows = []
    for _, r in df.iterrows():
        content = str(r.get("内容", "") or "").strip()
        if not _match(content, keyword):
            continue
        rows.append((r.get("时间"), content))
        if len(rows) >= limit:
            break
    return rows


def _fetch_hithink_flash():
    """拉同花顺实时快讯（tag_id=21103），失败返回 None。"""
    try:
        from app import hithink
        return hithink.fetch_news_flash_list(_HITHINK_FLASH_TAG_ID, "0")
    except Exception as e:
        print(f"⚠️ 同花顺快讯拉取失败: {e}")
        return None


def _render_hithink_flash(items, keyword, limit):
    """渲染同花顺快讯（标题/摘要/栏目标签/时间/链接）。"""
    rows = []
    for it in items or []:
        title = str(it.get("title") or "").strip()
        summary = str(it.get("summary") or "").strip()
        tags = "、".join(t.get("name", "") for t in (it.get("tags") or []) if t.get("name"))
        if not _match(title + " " + summary + " " + tags, keyword):
            continue
        rows.append((it.get("create_time_ms"), title, summary, tags, it.get("encoded_url")))
        if len(rows) >= limit:
            break
    return rows


def main():
    keyword, limit = _parse_args(sys.argv[1:])
    label = keyword or "全部"
    print(f"=== 实时财经快讯: {label}（最多 {limit} 条，数据源 东财/新浪/同花顺）===\n")

    # 东财全球快讯（主）
    em_df = _fetch_em() if _ak is not None else None
    em_rows = _render_em(em_df, keyword, limit) if em_df is not None else []

    # 新浪快讯（补充，最新 20 条电报式）
    sina_df = _fetch_sina() if _ak is not None else None
    sina_rows = _render_sina(sina_df, keyword, limit) if sina_df is not None else []

    # 同花顺快讯（补充，栏目标签，需 HITHINK_FINANCE_API_KEY）
    hithink_items = _fetch_hithink_flash()
    hithink_rows = _render_hithink_flash(hithink_items, keyword, limit) if hithink_items else []

    if _ak is None and not hithink_rows:
        print("⚠️ 未安装 akshare 且无同花顺快讯（pip install akshare，或配置 HITHINK_FINANCE_API_KEY）")
        return 1
    if not em_rows and not sina_rows and not hithink_rows:
        print("⚠️ 未获取到任何快讯（网络异常 / 无数据 / 关键词无命中）")
        return 1

    if em_rows:
        print(f"【东财全球快讯】{len(em_rows)} 条")
        for i, (ts, title, summary, url) in enumerate(em_rows, 1):
            print(f"  {i}. {title}  [{_fmt_ts(ts)}]")
            if summary:
                print(f"     {summary[:120]}")
            if url:
                print(f"     {url}")
        print()

    if sina_rows:
        print(f"【新浪快讯】{len(sina_rows)} 条")
        for i, (ts, content) in enumerate(sina_rows, 1):
            print(f"  {i}. [{_fmt_ts(ts)}] {content[:160]}")
        print()

    if hithink_rows:
        print(f"【同花顺快讯】{len(hithink_rows)} 条（栏目 tag_id={_HITHINK_FLASH_TAG_ID}）")
        for i, (ms, title, summary, tags, url) in enumerate(hithink_rows, 1):
            meta = "、".join(x for x in [tags, _fmt_ts_ms(ms)] if x)
            print(f"  {i}. {title}  [{meta}]")
            if summary:
                print(f"     {summary[:120]}")
            if url:
                print(f"     {url}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
