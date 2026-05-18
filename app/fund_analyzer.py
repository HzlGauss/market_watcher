"""
主动基金分析器 —— 评级、持仓风格、经理评价、操作建议
基于实时净值 + DeepSeek AI + 市场风格匹配
支持从CSV文件读取基金列表，方便手动维护
"""

from __future__ import annotations
import csv
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from app.config import Config
from app.data_fetcher import fetch_quotes, fetch_quotes_rich
from app.analyzer import calc_market_sentiment
from app.data_fetcher import fetch_global_markets
from app.reporter import _save_report, _push_report
from app.utils import log
from app.http_client import sina_client, eastmoney_client
from app.llm_client import get_llm_client, SYSTEM_PROMPTS

# ============================================================
# 基金净值数据获取（东方财富API）
# ============================================================

FUND_NAV_API = "https://api.fund.eastmoney.com/f10/lsjz"

_FUND_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://fund.eastmoney.com/",
}

# 历史净值持久化路径
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
FUND_HISTORY_PATH = STATE_DIR / "fund_nav_history.json"
FUND_RATING_HISTORY_PATH = STATE_DIR / "fund_rating_history.json"


def _load_fund_history() -> dict:
    """加载历史净值数据"""
    if not FUND_HISTORY_PATH.exists():
        return {}

    try:
        with open(FUND_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"加载基金历史数据失败: {e}")
        return {}


def _save_fund_history(history: dict) -> None:
    """保存历史净值数据"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(FUND_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"保存基金历史数据失败: {e}")


def _load_rating_history() -> dict:
    """加载基金评分历史数据"""
    if not FUND_RATING_HISTORY_PATH.exists():
        return {}

    try:
        with open(FUND_RATING_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"加载基金评分历史数据失败: {e}")
        return {}


def _save_rating_history(history: dict) -> None:
    """保存基金评分历史数据"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(FUND_RATING_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"保存基金评分历史数据失败: {e}")


def _analyze_rating_trend(rating_history: list[dict]) -> dict:
    """分析评分趋势

    Args:
        rating_history: 评分历史列表，按日期从旧到新排列

    Returns:
        趋势分析结果，包含趋势方向、连续变化天数、预警标志等
    """
    if len(rating_history) < 2:
        return {"trend": "stable", "days": 0, "warning": False, "message": ""}

    # 获取最近3天的评分
    recent_ratings = rating_history[-3:] if len(rating_history) >= 3 else rating_history
    recent_scores = [r.get("score", 0) for r in recent_ratings]

    # 判断趋势
    trend = "stable"
    days = 0
    warning = False
    message = ""

    # 检查连续下滑
    if len(recent_scores) >= 3:
        if recent_scores[0] > recent_scores[1] > recent_scores[2]:
            drop_amount = recent_scores[0] - recent_scores[2]
            if drop_amount > 5:
                trend = "declining"
                days = 3
                warning = True
                message = f"⚠️ 连续3日评分下滑超5分（{recent_scores[0]}→{recent_scores[2]}）"
            elif recent_scores[0] > recent_scores[2]:
                trend = "declining"
                days = 3
                message = f"连续3日评分下滑（{recent_scores[0]}→{recent_scores[2]}）"

    elif len(recent_scores) == 2:
        if recent_scores[0] > recent_scores[1]:
            drop_amount = recent_scores[0] - recent_scores[1]
            if drop_amount > 5:
                trend = "declining"
                days = 2
                warning = True
                message = f"⚠️ 连续2日评分下滑超5分（{recent_scores[0]}→{recent_scores[1]}）"
            else:
                trend = "declining"
                days = 2
        elif recent_scores[0] < recent_scores[1]:
            trend = "rising"
            days = 2

    return {
        "trend": trend,
        "days": days,
        "warning": warning,
        "message": message,
        "current_score": recent_scores[-1] if recent_scores else 0,
        "previous_score": recent_scores[-2] if len(recent_scores) >= 2 else None,
    }


def _merge_nav_data(existing: dict, new_data: dict) -> dict:
    """合并新旧净值数据，优先保留最新数据"""
    if not existing:
        return new_data

    # 提取现有日期集合
    existing_dates = set(existing.keys())

    # 合并数据（新数据覆盖旧数据）
    merged = {**existing}
    merged.update(new_data)

    # 按日期排序
    sorted_dates = sorted(merged.keys())
    merged_list = [{"date": d, "nav": merged[d]} for d in sorted_dates]

    return {
        "data": merged_list,
        "last_update": datetime.now().isoformat()
    }


def _fetch_nav(code: str, incremental: bool = False) -> dict | None:
    """获取基金净值数据

    Args:
        code: 基金代码
        incremental: 是否增量更新（只获取最近5日），默认False获取250日完整数据
    """
    all_rows = []
    page_size = 5 if incremental else 100
    max_pages = 1 if incremental else 3

    # 分页请求获取净值数据
    for page in range(1, max_pages + 1):
        url = f"{FUND_NAV_API}?callback=jQuery&fundCode={code}&pageIndex={page}&pageSize={page_size}"
        resp = eastmoney_client.get(url, headers=_FUND_HEADERS)
        if resp is None:
            log.warning(f"基金{code}净值获取失败(第{page}页)")
            break

        try:
            text = resp.text.strip()
            match = re.search(r'\{.*\}', text)
            if not match:
                continue
            data = json.loads(match.group())
            if not isinstance(data, dict):
                continue

            rows = data.get("Data", {}).get("LSJZList", [])
            if not rows:
                break
            all_rows.extend(rows)

            # 如果不足page_size条，说明已经到最后一页
            if len(rows) < page_size:
                break
        except Exception as e:
            log.warning(f"基金{code}净值解析失败(第{page}页): {e}")
            break

    if not all_rows:
        log.warning(f"基金{code}未获取到任何净值数据")
        return None

    # 按日期倒序排序（最新在前）
    all_rows.sort(key=lambda x: x.get("FSRQ", ""), reverse=True)

    latest = all_rows[0]
    earliest = all_rows[-1]

    # 提取净值序列（最早到最晚）
    nav_series = [float(row.get("DWJZ", 0)) for row in reversed(all_rows) if row.get("DWJZ")]

    # 构建日期-净值映射（用于合并）
    nav_map = {row.get("FSRQ", ""): float(row.get("DWJZ", 0)) for row in all_rows if row.get("DWJZ")}

    return {
        "date": latest.get("FSRQ", ""),
        "nav": float(latest.get("DWJZ", 0)),
        "daily_change": latest.get("JZZZL", "0"),
        "nav_series": nav_series,  # 净值序列（用于计算指标）
        "nav_map": nav_map,        # 日期-净值映射（用于合并）
        "start_date": earliest.get("FSRQ", ""),
        "days": len(nav_series),
    }


def _fetch_all_navs(funds: list[dict]) -> list[dict]:
    """批量获取所有基金净值（支持增量更新，优先使用历史数据）"""
    # 加载历史数据
    history = _load_fund_history()
    need_full_fetch = False

    # 检查是否需要全量更新（首次运行或数据过期）
    for f in funds:
        code = f["code"]
        if code not in history:
            need_full_fetch = True
            break
        # 检查数据是否足够（至少需要60个交易日）
        existing_data = history.get(code, {}).get("data", [])
        if len(existing_data) < 60:
            need_full_fetch = True
            break

    results: list[dict] = []
    fetch_mode = "全量" if need_full_fetch else "增量"
    log.info(f"  📥 净值获取模式: {fetch_mode}")

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_nav, f["code"], not need_full_fetch): f for f in funds}
        for future in as_completed(futures):
            f = futures[future]
            code = f["code"]
            nav = future.result()

            if nav:
                # 如果是增量更新，合并历史数据
                if not need_full_fetch and code in history:
                    existing = history[code]
                    existing_map = {item["date"]: item["nav"] for item in existing.get("data", [])}
                    # 合并新数据（只补充新数据中不存在的日期，不覆盖历史数据）
                    merged_map = {**existing_map}
                    for date, nav in nav["nav_map"].items():
                        if date not in merged_map:
                            merged_map[date] = nav
                    # 按日期排序
                    sorted_dates = sorted(merged_map.keys())
                    merged_series = [merged_map[d] for d in sorted_dates]

                    nav["nav_series"] = merged_series
                    nav["days"] = len(merged_series)
                    nav["start_date"] = sorted_dates[0] if sorted_dates else ""

                results.append({
                    "code": code,
                    "name": f["name"],
                    "manager": f.get("manager", ""),
                    "benchmark": f.get("benchmark", ""),
                    **nav,
                })
            else:
                # 如果获取失败，尝试使用历史数据
                if code in history:
                    existing = history[code]
                    existing_data = existing.get("data", [])
                    if existing_data:
                        latest = existing_data[-1]
                        results.append({
                            "code": code,
                            "name": f["name"],
                            "manager": f.get("manager", ""),
                            "benchmark": f.get("benchmark", ""),
                            "date": latest["date"],
                            "nav": latest["nav"],
                            "daily_change": "0",
                            "nav_series": [item["nav"] for item in existing_data],
                            "days": len(existing_data),
                            "start_date": existing_data[0]["date"],
                        })
                        continue

                results.append({
                    "code": code,
                    "name": f["name"],
                    "manager": f.get("manager", ""),
                    "benchmark": f.get("benchmark", ""),
                    "nav": None,
                    "nav_series": [],
                })

    # 保存更新后的历史数据
    new_history = {}
    for r in results:
        if r["nav"] is not None and "nav_map" in r:
            nav_map = r["nav_map"]
            sorted_dates = sorted(nav_map.keys())
            new_history[r["code"]] = {
                "data": [{"date": d, "nav": nav_map[d]} for d in sorted_dates],
                "last_update": datetime.now().isoformat()
            }
        elif r["nav"] is not None and "nav_series" in r:
            # 如果没有nav_map，使用现有数据
            new_history[r["code"]] = history.get(r["code"], {})

    _save_fund_history(new_history)

    return results


def _calc_fund_metrics(nav_series: list[float], risk_free_rate: float = 0.02) -> dict:
    """计算基金核心评价指标

    Args:
        nav_series: 净值序列（按时间顺序，最早到最晚）
        risk_free_rate: 无风险利率（默认0.02，即2%）

    Returns:
        包含夏普比率、最大回撤、年化收益、波动率等指标的字典
    """
    if len(nav_series) < 2:
        return {}

    import math
    import statistics

    # 计算日收益率序列
    returns = []
    for i in range(1, len(nav_series)):
        if nav_series[i-1] > 0:
            returns.append(nav_series[i] / nav_series[i-1] - 1)

    if len(returns) < 2:
        return {}

    # 计算年化收益率（几何平均）
    total_return = nav_series[-1] / nav_series[0] - 1
    days = len(nav_series)
    annual_return = (1 + total_return) ** (252 / days) - 1 if days > 0 else 0

    # 计算年化波动率
    daily_std = statistics.stdev(returns)
    annual_vol = daily_std * math.sqrt(252)

    # 计算夏普比率
    sharpe_ratio = (annual_return - risk_free_rate) / annual_vol if annual_vol > 0 else 0

    # 计算最大回撤
    peak = nav_series[0]
    max_drawdown = 0.0
    for nav in nav_series[1:]:
        if nav > peak:
            peak = nav
        drawdown = (peak - nav) / peak
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    # 计算卡玛比率（年化收益/最大回撤）
    calmar_ratio = annual_return / max_drawdown if max_drawdown > 0 else 0

    # 计算胜率（上涨天数占比）
    win_days = sum(1 for r in returns if r > 0)
    win_rate = win_days / len(returns)

    # 计算平均盈利/平均亏损比率
    gains = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    avg_gain = sum(gains) / len(gains) if gains else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 1
    profit_factor = avg_gain / avg_loss if avg_loss > 0 else 0

    # 数据质量标识：少于240日（约1年）数据时标注
    data_quality = "sufficient" if days >= 240 else "insufficient"

    return {
        "annual_return": round(annual_return * 100, 2),
        "annual_volatility": round(annual_vol * 100, 2),
        "sharpe_ratio": round(sharpe_ratio, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "calmar_ratio": round(calmar_ratio, 2),
        "win_rate": round(win_rate * 100, 2),
        "profit_factor": round(profit_factor, 2),
        "data_points": len(nav_series),
        "data_quality": data_quality,
    }


# ============================================================
# 基准指数对比
# ============================================================

# 东方财富历史K线API
BENCH_NAV_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

def _fetch_risk_free_rate() -> float:
    """动态获取中国10年期国债收益率作为无风险利率

    Returns:
        无风险利率（小数形式，如0.025表示2.5%），获取失败时返回默认值0.02
    """
    try:
        # 从新浪财经获取10年期国债收益率（sh000015）
        url = "http://hq.sinajs.cn/list=sh000015"
        resp = sina_client.get(url)
        if resp:
            text = resp.text
            # 解析格式：var hq_str_sh000015="10年期国债,3.2500,..."
            parts = text.split('"')[1].split(',')
            if len(parts) >= 4:
                yield_str = parts[3]  # 收益率在第4个字段
                return float(yield_str) / 100
        log.info("  ⏭️  获取国债收益率失败，使用默认值2%")
    except Exception as e:
        log.warning(f"获取国债收益率异常: {e}")

    return 0.02  # 默认2%

def _fetch_fund_benchmark(fund_code: str) -> str:
    """从东方财富F10接口获取基金的业绩比较基准

    Args:
        fund_code: 基金代码

    Returns:
        业绩比较基准指数代码（如 sh000300），获取失败返回空字符串
    """
    try:
        # 东方财富基金F10基本信息接口
        url = f"https://fund.eastmoney.com/f10/F10DataApi.aspx?type=jjgk&code={fund_code}"
        resp = eastmoney_client.get(url)
        if resp:
            text = resp.text
            # 解析业绩比较基准字段
            # 格式类似：业绩比较基准</span></td><td class="td2">(95%×沪深300指数+5%×中证全债指数)</td>
            import re
            match = re.search(r'业绩比较基准.*?>([^<]+)</td>', text)
            if match:
                benchmark_str = match.group(1).strip()
                # 尝试从中提取指数代码
                # 常见格式：沪深300指数 -> sh000300, 创业板指 -> sz399006, 中证500 -> sh000905
                index_mapping = {
                    '沪深300': 'sh000300',
                    '沪深300指数': 'sh000300',
                    '创业板': 'sz399006',
                    '创业板指': 'sz399006',
                    '创业板指数': 'sz399006',
                    '中证500': 'sh000905',
                    '中证500指数': 'sh000905',
                    '上证50': 'sh000016',
                    '上证50指数': 'sh000016',
                    '中证消费': 'sh000932',
                    '中证医药': 'sh000933',
                    '全指医药': 'sh000991',
                    '万得全A': '881001.WI',
                }
                for index_name, code in index_mapping.items():
                    if index_name in benchmark_str:
                        log.debug(f"  基金{fund_code}基准: {benchmark_str} -> {code}")
                        return code
                # 如果找不到匹配的指数，返回空
                log.debug(f"  基金{fund_code}基准未识别: {benchmark_str}")
    except Exception as e:
        log.warning(f"获取基金{fund_code}业绩比较基准异常: {e}")

    return ""

def _fetch_benchmark_nav(benchmark_code: str) -> list[float] | None:
    """获取基准指数近250日净值（通过东方财富K线API获取真实历史数据）"""
    # 转换代码格式：sh000300 -> 1.000300, sz399999 -> 0.399999
    secid = _convert_to_eastmoney_code(benchmark_code)
    if not secid:
        log.warning(f"基准指数{benchmark_code}代码格式不支持")
        return None

    try:
        # 请求周线数据（约50周 ≈ 1年）
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": "101",  # 周线
            "lmt": "60"    # 获取60周数据（约1年半）
        }

        resp = eastmoney_client.get(BENCH_NAV_API, params=params)
        if not resp:
            log.warning(f"基准指数{benchmark_code}获取失败")
            return None

        data = resp.json()
        if data.get("data") is None:
            log.warning(f"基准指数{benchmark_code}返回数据为空")
            return None

        klines = data["data"].get("klines", [])
        if not klines:
            log.warning(f"基准指数{benchmark_code}K线数据为空")
            return None

        # 解析K线数据，提取收盘价
        nav_series = []
        for kline in klines:
            parts = kline.split(",")
            if len(parts) >= 5:
                nav_series.append(float(parts[4]))  # 收盘价在第5位

        if len(nav_series) < 10:
            log.warning(f"基准指数{benchmark_code}有效数据不足")
            return None

        log.debug(f"基准指数{benchmark_code}获取到{len(nav_series)}条历史数据")
        return nav_series

    except Exception as e:
        log.warning(f"基准指数{benchmark_code}获取失败: {e}")
        return None


def _convert_to_eastmoney_code(benchmark_code: str) -> str | None:
    """将基准代码转换为东方财富格式"""
    if benchmark_code.startswith("sh"):
        # 上海市场: sh000300 -> 1.000300
        code = benchmark_code[2:]
        return f"1.{code}"
    elif benchmark_code.startswith("sz"):
        # 深圳市场: sz399999 -> 0.399999
        code = benchmark_code[2:]
        return f"0.{code}"
    else:
        # 其他格式暂不支持，返回None
        log.warning(f"不支持的基准代码格式: {benchmark_code}")
        return None


def _calc_benchmark_metrics(fund_returns: list[float], bench_returns: list[float], risk_free_rate: float = 0.02) -> dict:
    """计算基金相对基准的指标"""
    if len(fund_returns) < 2 or len(bench_returns) < 2:
        return {}

    import math
    import statistics

    # 确保两个序列长度一致
    min_len = min(len(fund_returns), len(bench_returns))
    fund_returns = fund_returns[:min_len]
    bench_returns = bench_returns[:min_len]

    # 计算超额收益序列
    excess_returns = [f - b for f, b in zip(fund_returns, bench_returns)]

    # 累计超额收益
    excess_total = sum(excess_returns)

    # 年化超额收益
    excess_annual = excess_total * (252 / min_len)

    # 信息比率（超额收益 / 跟踪误差）
    tracking_error = statistics.stdev(excess_returns) * math.sqrt(252) if min_len > 1 else 0
    info_ratio = excess_annual / tracking_error if tracking_error > 0 else 0

    # Beta系数
    # 注意：statistics.variance() 使用 n-1 分母（样本方差，贝塞尔校正）
    # Beta公式 = Cov(X,Y) / Var(Y)
    # 分子cov_sum未除以(n-1)，分母bench_var已除以(n-1)
    # 因此分母需要乘以(n-1)来抵消：cov_sum / [(n-1) * (var_sum/(n-1))] = cov_sum / var_sum
    # 这样计算的是总体Beta（使用总体方差），符合金融领域标准实践
    bench_var = statistics.variance(bench_returns) if min_len > 1 else 0
    if bench_var > 0:
        cov_sum = sum((f - sum(fund_returns)/min_len) * (b - sum(bench_returns)/min_len)
                     for f, b in zip(fund_returns, bench_returns))
        beta = cov_sum / ((min_len - 1) * bench_var)
    else:
        beta = 0

    # Alpha（詹森指数）- 修复单位不一致问题
    # 先用日频数据计算，再年化
    risk_free_daily = risk_free_rate / 252  # 日均无风险收益

    # 日均收益（日频）
    fund_daily = sum(fund_returns) / min_len
    bench_daily = sum(bench_returns) / min_len

    # Beta已经是日频计算的，这里保持单位一致
    alpha_daily = fund_daily - (risk_free_daily + beta * (bench_daily - risk_free_daily))

    # 将日Alpha年化
    alpha = alpha_daily * 252

    return {
        "excess_return": round(excess_annual * 100, 2),
        "tracking_error": round(tracking_error * 100, 2),
        "info_ratio": round(info_ratio, 2),
        "beta": round(beta, 2),
        "alpha": round(alpha * 100, 2),
    }


# ============================================================
# mx-data 深度数据（选填，有API Key时启用）
# ============================================================

def _mx_query(query: str, config: Config) -> str | None:
    """调用 mx-data 查询东方财富深度数据"""
    api_key = config.mx_apikey
    if not api_key:
        return None

    import subprocess, tempfile

    mx_script = Path(os.path.expanduser("~/.workbuddy/skills/mx-data/mx_data.py"))
    if not mx_script.exists():
        log.warning("mx-data 脚本未安装 (~/.workbuddy/skills/mx-data/)")
        return None

    try:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["MX_APIKEY"] = api_key
            result = subprocess.run(
                ["python", str(mx_script), query, tmp],
                capture_output=True, text=True, timeout=30, env=env,
            )
            output = result.stdout + result.stderr
            return output if "错误" not in output[:50] else None
    except Exception as e:
        log.warning(f"mx-data调用异常: {e}")
        return None


def _fetch_mx_fund_data(funds: list[dict], config: Config) -> str:
    """通过 mx-data 获取基金深度数据（批量查询优化版）"""
    results = []
    batch_size = 5  # 每次查询5只基金，避免API限制

    # 分批处理基金列表
    for i in range(0, len(funds), batch_size):
        batch = funds[i:i+batch_size]
        codes = ",".join(f["code"] for f in batch)

        # 1. 权威评级
        rating = _mx_query(f"{codes} 晨星评级 银河评级", config)
        if rating:
            results.append(f"【权威评级 (批次{i//batch_size + 1})】\n{rating[:800]}")

        # 2. 持仓分析
        holdings = _mx_query(f"{codes} 前十大持仓 行业分布", config)
        if holdings:
            results.append(f"【持仓数据】\n{holdings[:800]}")

        # 3. 阶段涨幅与排名
        performance = _mx_query(f"{codes} 阶段涨幅 四分位排名", config)
        if performance:
            results.append(f"【阶段表现】\n{performance[:800]}")

        # 4. 基金经理信息
        manager_info = _mx_query(f"{codes} 基金经理 任职回报", config)
        if manager_info:
            results.append(f"【经理信息】\n{manager_info[:800]}")

        # 5. 规模与资产配置
        scale = _mx_query(f"{codes} 基金规模 资产配置", config)
        if scale:
            results.append(f"【规模配置】\n{scale[:800]}")

    return "\n\n".join(results) if results else ""


# ============================================================
# AI 分析（DeepSeek）
# ============================================================

def _call_llm(prompt: str, config: Config) -> str | None:
    """调用 DeepSeek 分析"""
    llm = get_llm_client(config)
    if not llm.enabled:
        return None

    return llm.chat(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPTS["fund_expert"],
        max_tokens=3500,
        temperature=0.3,
    )


# ============================================================
# 主分析函数
# ============================================================

def analyze_funds(config: Config) -> Path | None:
    """
    主动基金综合分析 —— 评级 + 持仓风格 + 经理评价 + 操作建议
    """
    # 优先检查CSV文件，然后回退到JSON文件
    funds_csv_path = Path("funds.csv")
    funds_json_path = Path(config.get("盯盘设置", {}).get("基金配置路径", "funds_config.json"))

    fund_list = []

    # 优先从CSV文件读取
    if funds_csv_path.exists():
        try:
            with open(funds_csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                row_num = 1  # 从1开始，因为标题行是第1行
                for row in reader:
                    row_num += 1

                    # 确保code字段不为空
                    code_value = row.get("code", "").strip()
                    if not code_value:
                        log.warning(f"Skipping row {row_num} in funds.csv: code field is empty")
                        continue

                    # 处理空字段，设置为空字符串
                    for key, value in row.items():
                        if value is None:
                            row[key] = ""
                        elif isinstance(value, str) and value.strip() == "":
                            row[key] = ""

                    fund_list.append(row)
            log.info(f"Loaded {len(fund_list)} funds from funds.csv")
        except Exception as e:
            log.warning(f"Failed to load funds.csv: {e}")

    # 如果CSV文件不存在或读取失败，从JSON文件读取（保持向后兼容）
    if not fund_list:
        if not funds_json_path.exists():
            funds_json_path = Path(__file__).resolve().parent.parent / funds_json_path
        if not funds_json_path.exists():
            log.error(f"基金配置文件不存在: {funds_json_path}")
            return None

        with open(str(funds_json_path), "r", encoding="utf-8") as f:
            fund_cfg = json.load(f)
        fund_list = fund_cfg.get("基金列表", [])
        log.info(f"Loaded {len(fund_list)} funds from funds_config.json")

    if not fund_list:
        log.warning("基金列表为空")
        return None

    # 为没有benchmark的基金自动获取业绩比较基准
    funds_missing_benchmark = [f for f in fund_list if not f.get("benchmark", "").strip()]
    if funds_missing_benchmark:
        log.info(f"  🔍 为 {len(funds_missing_benchmark)} 只基金自动获取业绩比较基准...")
        for f in funds_missing_benchmark:
            benchmark = _fetch_fund_benchmark(f["code"])
            if benchmark:
                f["benchmark"] = benchmark
                log.info(f"    ✅ 基金{f['code']}({f['name']}) 基准: {benchmark}")
            else:
                log.info(f"    ⏭️  基金{f['code']}({f['name']}) 基准获取失败")

    log.info(f"📊 开始分析 {len(fund_list)} 只主动基金...")

    # 1. 获取实时净值数据
    log.info("  获取基金净值...")
    nav_data = _fetch_all_navs(fund_list)
    valid_funds = [f for f in nav_data if f["nav"] is not None]
    log.info(f"  ✅ 获取到 {len(valid_funds)}/{len(fund_list)} 只基金净值")

    # 2. 获取当前市场行情
    log.info("  获取当前市场风格...")
    watch_items = config.watch_items
    quotes = fetch_quotes_rich(watch_items)
    sentiment = calc_market_sentiment(quotes) if quotes else None

    sectors_str = ""
    if quotes:
        sectors: dict[str, list[float]] = {}
        for q in quotes:
            sty = q.type
            if sty not in sectors:
                sectors[sty] = []
            if q.change_pct is not None:
                sectors[sty].append(q.change_pct)
        sector_lines = []
        for st, pcts in sorted(sectors.items()):
            if pcts:
                avg = sum(pcts) / len(pcts)
                sector_lines.append(f"  {st}: 均值 {avg:+.2f}%")
        sectors_str = "\n".join(sector_lines)

    # 3. 获取隔夜美股（用于判断全球风格）
    global_data = fetch_global_markets()
    global_str = ""
    if global_data:
        parts = []
        for name, val in global_data.items():
            parts.append(f"{name}: {val}")
        global_str = " | ".join(parts)

    # 4. 调用 mx-data 获取深度数据（有API Key时）
    mx_data = ""
    if config.mx_apikey:
        log.info("  📡 查询东方财富深度数据（评级+持仓）...")
        mx_data = _fetch_mx_fund_data(valid_funds, config)
        if mx_data:
            log.info("  ✅ 获取到深度数据")
        else:
            log.info("  ⏭️  mx-data无返回（使用AI知识补充）")

    # 5. 构建 Prompt
    today = datetime.now().strftime("%Y-%m-%d")
    mood_str = f"{sentiment.score}/100 ({sentiment.label})" if sentiment else "--"

    # 预取所有唯一基准指数数据（避免重复请求）
    benchmark_cache: dict[str, list[float]] = {}
    unique_benchmarks = {f.get("benchmark", "") for f in valid_funds if f.get("benchmark", "")}
    for bench_code in unique_benchmarks:
        bench_nav = _fetch_benchmark_nav(bench_code)
        if bench_nav:
            benchmark_cache[bench_code] = bench_nav

    # 获取无风险利率（用于夏普比率和Alpha计算）
    risk_free_rate = _fetch_risk_free_rate()
    log.info(f"  📈 无风险利率(10年期国债): {risk_free_rate*100:.2f}%")

    # 为每只基金计算定量指标（包含基准对比）
    funds_with_metrics = []
    for f in valid_funds:
        nav_series = f.get("nav_series", [])
        metrics = _calc_fund_metrics(nav_series, risk_free_rate)

        # 计算基准对比指标（使用缓存）
        benchmark_code = f.get("benchmark", "")
        bench_metrics = {}
        if benchmark_code and nav_series and benchmark_code in benchmark_cache:
            bench_nav = benchmark_cache[benchmark_code]
            # 计算基金日收益率
            fund_returns = []
            for i in range(1, len(nav_series)):
                if nav_series[i-1] > 0:
                    fund_returns.append(nav_series[i] / nav_series[i-1] - 1)

            # 计算基准日收益率（模拟）
            bench_returns = [(bench_nav[i] - bench_nav[i-1]) / bench_nav[i-1]
                           for i in range(1, len(bench_nav)) if bench_nav[i-1] > 0]

            bench_metrics = _calc_benchmark_metrics(fund_returns, bench_returns, risk_free_rate)

        funds_with_metrics.append({**f, "metrics": metrics, "benchmark_metrics": bench_metrics})

    # 加载评分历史并分析趋势
    rating_history = _load_rating_history()
    warnings = []

    # 更新今日评分并分析趋势
    updated_rating_history = {}
    for f in funds_with_metrics:
        code = f["code"]
        m = f["metrics"]
        bm = f["benchmark_metrics"]

        # 计算综合评分（基于夏普比率、最大回撤、Alpha等）
        sharpe = m.get("sharpe_ratio", 0)
        max_drawdown = m.get("max_drawdown", 100)
        alpha = bm.get("alpha", 0)
        calmar = m.get("calmar_ratio", 0)

        # 综合评分算法
        # 夏普比率得分 (0-40分): 夏普>2得40分, <0得0分
        sharpe_score = min(max(0, sharpe * 20), 40)
        # 最大回撤得分 (0-25分): 回撤<10%得25分, >50%得0分
        drawdown_score = min(max(0, (50 - max_drawdown) * 0.5), 25)
        # Alpha得分 (0-20分): Alpha>5%得20分, <0得0分
        alpha_score = min(max(0, alpha * 4), 20)
        # 卡玛比率得分 (0-15分): 卡玛>3得15分
        calmar_score = min(max(0, calmar * 5), 15)

        total_score = round(sharpe_score + drawdown_score + alpha_score + calmar_score)

        # 获取历史记录
        fund_history = rating_history.get(code, {}).get("history", [])

        # 如果今日已有记录则更新，否则添加
        today_exists = False
        for record in fund_history:
            if record["date"] == today:
                record["score"] = total_score
                record["alpha"] = alpha
                record["sharpe"] = sharpe
                record["max_drawdown"] = max_drawdown
                record["calmar"] = calmar
                today_exists = True
                break

        if not today_exists:
            fund_history.append({
                "date": today,
                "score": total_score,
                "alpha": alpha,
                "sharpe": sharpe,
                "max_drawdown": max_drawdown,
                "calmar": calmar,
            })

        # 保留最近30天的数据
        fund_history = fund_history[-30:]

        # 分析趋势
        trend_result = _analyze_rating_trend(fund_history)
        if trend_result["warning"]:
            warnings.append(f"⚠️ {f['name']}: {trend_result['message']}")

        # 添加趋势信息到基金数据
        f["rating_score"] = total_score
        f["rating_trend"] = trend_result

        updated_rating_history[code] = {"history": fund_history}

    # 保存评分历史
    _save_rating_history(updated_rating_history)

    # 如果有预警，记录日志
    if warnings:
        log.warning("  ⚠️ 评分预警:")
        for w in warnings:
            log.warning(f"    {w}")

    # 构建基金列表（包含指标和基准对比）
    fund_table_lines = []
    fund_metrics_lines = []
    for f in funds_with_metrics:
        change = f.get("daily_change", "0")
        benchmark_code = f.get("benchmark", "--")
        rating_score = f.get("rating_score", "--")
        trend_note = f" ({f['rating_trend']['message']})" if f['rating_trend'].get('warning') else ""
        fund_table_lines.append(
            f"- {f['name']}({f['code']}) 经理:{f['manager']} "
            f"| 基准:{benchmark_code} | 最新净值:{f['nav']:.4f} | 日涨跌:{change}% | "
            f"评分:{rating_score}{trend_note}"
        )

        # 添加定量指标
        m = f["metrics"]
        bm = f["benchmark_metrics"]
        if m:
            data_quality_note = " [数据不足，仅供参考]" if m.get("data_quality") == "insufficient" else ""
            metrics_str = (
                f"  - 量化指标: 年化收益{m.get('annual_return', '--')}% | "
                f"波动率{m.get('annual_volatility', '--')}% | "
                f"夏普{m.get('sharpe_ratio', '--')} | "
                f"最大回撤{m.get('max_drawdown', '--')}% | "
                f"卡玛{m.get('calmar_ratio', '--')}{data_quality_note}"
            )
            if bm:
                metrics_str += (
                    f" | 超额收益{bm.get('excess_return', '--')}% | "
                    f"Alpha{bm.get('alpha', '--')}% | "
                    f"Beta{bm.get('beta', '--')}"
                )
            fund_metrics_lines.append(f"**{f['name']}**{metrics_str}")

    # 构建风格分析数据（从 Beta/Alpha 反推）
    style_analysis = []
    for f in funds_with_metrics:
        bm = f.get("benchmark_metrics", {})
        beta = bm.get("beta", "--")
        alpha = bm.get("alpha", "--")

        # 从 Beta 反推风格
        if beta != "--" and beta != 0:
            if beta > 1.2:
                style = "高弹性/激进型（Beta>1.2，大盘涨时涨更多，跌时跌更深）"
            elif beta > 0.8:
                style = "市场同步型（Beta 0.8-1.2，与大盘基本同步）"
            else:
                style = "稳健型（Beta<0.8，波动小于大盘）"
        else:
            style = "数据不足，无法判断"

        # 从 Alpha 判断超额收益能力
        if alpha != "--":
            if alpha > 5:
                alpha_judge = "显著正 Alpha，基金经理创造稳定超额收益"
            elif alpha > 0:
                alpha_judge = "微弱正 Alpha，超额收益不明显"
            elif alpha > -5:
                alpha_judge = "微弱负 Alpha，略跑输基准"
            else:
                alpha_judge = "显著负 Alpha，持续跑输基准，需警惕"
        else:
            alpha_judge = "数据不足"

        style_analysis.append(
            f"- {f['name']}({f['code']}): {style} | {alpha_judge}"
        )

    prompt = f"""今天是 {today}。请作为资深基金专家，为投资者生成一份**极具决策价值**的主动管理基金深度分析报告。

### 1. 核心输入数据

**【当前市场环境】**
- A股情绪: {mood_str}
- 行业板块走势:
{sectors_str}
- 全球市场参考: {global_str}

**【待分析基金列表】**
{ "\n".join(fund_table_lines)}

**【基金量化指标（近1年数据）】**
{ "\n".join(fund_metrics_lines)}

**【风格与超额收益预分析】**
{ "\n".join(style_analysis)}

{"\n【权威深度数据（持仓/评级）】\n" + mx_data if mx_data else ""}

---

### 2. 报告输出要求（必须严格遵守以下 Markdown 结构）

#### **🏆 今日决策摘要**
- 给出 1-2 句关于当前市场风格的定性研判。
- 基于量化指标（特别是超额收益和Alpha）和市场环境，列出今日**最值得关注**的 1-2 只基金及其推荐动作（如：重点加仓、止盈观察）。

#### **📋 基金行情速览表**
请使用 Markdown 表格列出所有 {len(valid_funds)} 只基金的摘要：
| 基金代码 | 基金名称 | 日涨跌 | 夏普比率 | 最大回撤 | 超额收益 | Alpha | 风格匹配度 | 建议操作 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| (代码) | (名称) | (带符号百分比) | (数值) | (百分比) | (百分比) | (百分比) | (使用 ⭐ 数量表示) | (统一标签) |

*风格匹配度说明：⭐(极差) 到 ⭐⭐⭐⭐⭐(完美契合)，需综合考虑量化指标和当前市场风格*
*操作建议标签：【强力买入】、【分批加仓】、【继续持有】、【风险观望】、【逢高止盈】*

#### **🔍 深度研判与风格分析**
请将基金进行分类或挑选重点进行点评（不需要逐一罗列，突出重点）：

##### a. 风格定位
基于 Beta 系数和持仓特征，将基金归入以下三类之一：
- **进攻型**（Beta > 1.2）：适合牛市/反弹行情
- **均衡型**（Beta 0.8 - 1.2）：适合震荡市
- **防御型**（Beta < 0.8）：适合熊市/避险行情

##### b. 超额收益质量评估
- 区分”运气”和”能力”：Alpha 是否稳定？还是靠某几只股票的偶然爆发？
- 与基准的跟踪误差是否在合理范围内？

##### c. 风格漂移检测
对比基金的历史风格标签与最新 Beta 值：
- 如果之前是防御型最近 Beta 突然变大，说明经理可能在做风格轮动
- 如果风格与招募说明书不一致，标注”⚠️ 存在风格漂移风险”

##### d. 持仓体感分析
直接点出基金目前的”真实体感”（例如：”这只基金本质上是在赌AI”、”这是一篮子红利资产”）

##### e. 操作建议
每只基金给出具体建议，格式为”条件 → 行动”：
- “如果市场风格切换到成长，该基金是优先加仓标的”
- “如果该基金规模继续扩张至 100 亿以上，需重新评估其选股能力”

#### **💡 总结建议**
- 给出总体的仓位控制建议。
- 提醒未来 1-3 个交易日需警惕的风险点。
- 推荐 1-2 只综合评分最高的基金（优先考虑Alpha为正且夏普比率较高的）。

**要求：**
- 语言专业、辛辣、有洞察力，拒绝模棱两可。
- 总字数控制在 2000 字以内（根据基金数量动态调整）。
- 充分利用表格、列表、Emoji 和加粗语法提升可读性。
- **必须**基于提供的量化指标和基准对比数据进行分析，不能凭空臆断。

---
**置信度标注规则**：对于每一个判断性结论，请在括号中标注你的确定程度。
- [高]：数据充分、指标一致、历史模式明确
- [中]：数据尚可但存在分歧信号
- [低]：数据不足或逻辑链条不完整
**不确定性处理**：如果数据不足以支持判断，请直接输出"数据不足"而非强行给出结论。"""

    # 5. 调用 AI
    log.info("  🤖 DeepSeek 分析中（约30秒）...")
    content = _call_llm(prompt, config)

    if not content:
        log.error("基金分析生成失败")
        return None

    # 6. 保存报告
    report_dir = Path(config.report_dir)
    filepath = _save_report(f"📊 主动基金深度分析报告\n\n{content}", "📊 基金分析", report_dir)

    # 7. 推送
    _push_report(f"📊 基金分析 {today}", f"## 基金分析报告\n\n{content[:1500]}", config)

    log.info(f"✅ 基金分析报告已生成: {filepath}")
    return filepath
