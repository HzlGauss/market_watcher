#!/usr/bin/env python3
"""个股新闻（AKShare，免费，东财 stock_news_em）。

用法:
    py .claude/skills/akshare-news/stock_news.py <代码> [名称]

参数:
    代码   6 位 A 股代码（必填）
    名称   股票名称（可选，仅用于展示标签）

数据源: 东财 stock_news_em（标题/内容/发布时间/来源/链接，最近约 10 条）。
免费、实时，无需 MX_APIKEY，仅需 pip install akshare。
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

# 定位项目根目录（skills/akshare-news 上三级）
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

# 加载 .env（同花顺 API Key 等，供兜底源使用）
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


def _parse_args(argv):
    """解析命令行参数，返回 (code, name)。"""
    code = argv[0].strip() if argv else None
    name = argv[1].strip() if len(argv) > 1 else None
    return code, name


def _normalize_code(code):
    """把代码规整为 6 位纯数字（去掉 sh/sz 前缀、.SH/.SZ 后缀、非数字字符）。"""
    if not code:
        return None
    s = code.upper().replace("SH", "").replace("SZ", "").replace("BJ", "")
    s = "".join(ch for ch in s if ch.isdigit())
    if len(s) != 6:
        return None
    return s


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


def _print_hithink(code: str):
    """同花顺个股分时新闻（free，需 HITHINK_FINANCE_API_KEY）。"""
    try:
        from app import hithink
        items = hithink.fetch_news_stock_timeline(code, size=20)
    except Exception:
        items = []
    print(f"=== 个股分时新闻: {code}（数据源 同花顺 news/timeline）===\n")
    if not items:
        print("  ⚠️ 无同花顺分时新闻（未配置 HITHINK_FINANCE_API_KEY 或接口不可达）")
        print()
        return
    print(f"共 {len(items)} 条\n")
    for i, it in enumerate(items):
        title = str(it.get("title") or "").strip()
        source = str(it.get("source") or "").strip()
        summary = str(it.get("summary") or "").strip()
        meta = ", ".join(x for x in [source, _fmt_ts_ms(it.get("publish_time_ms"))] if x)
        print(f"  {i + 1}. {title}  [{meta}]")
        if summary:
            print(f"     {summary[:120]}")
        print()


def main():
    raw_code, name = _parse_args(sys.argv[1:])
    code = _normalize_code(raw_code)
    if not code:
        print("❌ 无效代码：请输入 6 位 A 股代码（如 600519 / 000001）")
        print(__doc__)
        return 2

    label = f"{name}({code})" if name else code

    # 1) AKShare 东财新闻（可选用，未安装 akshare 则跳过）
    if _ak is not None:
        print(f"=== 个股新闻: {label}（数据源 AKShare 东财）===\n")
        try:
            df = _ak.stock_news_em(symbol=code)
        except Exception as e:
            print(f"⚠️ 东财新闻拉取失败: {e}\n")
            df = None
        if df is not None and not df.empty:
            print(f"共 {len(df)} 条\n")
            for i, r in df.iterrows():
                title = str(r.get("新闻标题", "") or "").strip()
                source = str(r.get("文章来源", "") or "").strip()
                ts = r.get("发布时间")
                content = str(r.get("新闻内容", "") or "").strip()
                url = r.get("新闻链接")
                meta = ", ".join(x for x in [source, _fmt_ts(ts)] if x)
                print(f"  {i + 1}. {title}  [{meta}]")
                if content:
                    print(f"     {content[:120]}")
                if url:
                    print(f"     {url}")
                print()
        elif df is not None and df.empty:
            print("⚠️ 未获取到该股票的东财新闻\n")
    else:
        print("⚠️ 未安装 akshare，跳过东财新闻（pip install akshare）\n")

    # 2) 同花顺个股分时新闻（免费兜底/补充，需 HITHINK_FINANCE_API_KEY）
    _print_hithink(code)

    return 0


if __name__ == "__main__":
    sys.exit(main())
