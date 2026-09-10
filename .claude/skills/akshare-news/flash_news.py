#!/usr/bin/env python3
"""实时财经快讯流（AKShare，免费，东财全球快讯 + 新浪快讯）。

用法:
    py .claude/skills/akshare-news/flash_news.py [关键词] [条数]

参数:
    关键词   自然语言词，在标题/摘要/内容里子串匹配过滤（可选，缺省返回最新全局快讯）
    条数     最多显示的快讯条数（可选，默认 50）

数据源:
    - 东财 stock_info_global_em：全球快讯约 200 条（标题/摘要/发布时间/链接）
    - 新浪 stock_info_global_sina：最新 20 条电报式快讯（时间/内容）
    两者免费、实时，无需 MX_APIKEY，仅需 pip install akshare。

输出: 分两段——【东财全球快讯】（带链接可深挖）+【新浪快讯】（电报式最新）。
"""
import os
import sys

# 强制 UTF-8 输出，避免控制台中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 禁用 akshare/tqdm 进度条，避免污染输出
os.environ.setdefault("TQDM_DISABLE", "1")

try:
    import akshare as ak
except ImportError:
    print("❌ 未安装 akshare，请先执行: pip install akshare")
    sys.exit(1)

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


def main():
    keyword, limit = _parse_args(sys.argv[1:])
    label = keyword or "全部"
    print(f"=== 实时财经快讯: {label}（最多 {limit} 条，数据源 AKShare 东财/新浪）===\n")

    # 东财全球快讯（主）
    em_df = _fetch_em()
    em_rows = _render_em(em_df, keyword, limit) if em_df is not None else []

    # 新浪快讯（补充，最新 20 条电报式）
    sina_df = _fetch_sina()
    sina_rows = _render_sina(sina_df, keyword, limit) if sina_df is not None else []

    if not em_rows and not sina_rows:
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
