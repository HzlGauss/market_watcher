"""
智能荐股引擎 —— A股底部反转机会发现

在全市场 A 股（排除 ST）中筛选出现底部反转信号的标的。
四阶段过滤：
  0. 流动性 + ST + 市值预过滤（一口调用全市场快照）
  1. 日线K线获取（仅对候选集）
  2. 量价特征过滤（近期下跌 + 缩量止跌/筑底特征）
  3. 技术指标共振（RSI/MACD/KDJ/均线/OBV 多指标确认）

与 etf_screener.py 共享技术评分逻辑。
"""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import time
import re

from app.utils import log
from app.technical import (
    calc_rsi, calc_macd, calc_kdj, calc_ma_alignment, calc_obv,
    rsi_signal,
)
from app.models import KlineData


def _is_near_limit_up(change_pct: float, code: str) -> bool:
    """判断当日涨幅是否已接近涨停板（追不上了，排除）"""
    code = str(code).strip()
    if code.startswith(('300', '301', '688')):
        return change_pct >= 19.0   # 创业板/科创板 20cm
    if code.startswith(('8', '4')):
        return change_pct >= 28.0   # 北交所/三板 30cm
    return change_pct >= 9.5        # 主板 10cm


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _detect_market(code: str) -> str:
    """根据代码判断市场 (SH/SZ)"""
    if code.startswith(('60', '68', '51', '56', '58')):
        return 'SH'
    if code.startswith(('00', '30', '15', '16', '18')):
        return 'SZ'
    return 'SH'


# Sina 行情 API 常量
SINA_API = "https://hq.sinajs.cn/list="


# ============================================================
# 数据模型
# ============================================================

# 复用 ETF 的候选模型（字段完全匹配）
# 为避免循环依赖，直接在此定义

class StockCandidate:
    """A股候选标的"""
    __slots__ = (
        'code', 'name', 'price', 'change_pct', 'volume', 'amount',
        'market_cap', 'pe_ratio', 'decline_20d', 'decline_5d',
        'rsi', 'macd_signal', 'kdj_signal', 'ma_alignment', 'obv_signal',
        'volume_shrink', 'amplitude_narrow', 'path_label', 'score', 'signals', 'signal_level',
    )

    def __init__(
        self, code: str = "", name: str = "", price: float = 0.0,
        change_pct: float = 0.0, volume: float = 0.0, amount: float = 0.0,
        market_cap: float = 0.0, pe_ratio: Optional[float] = None,
        decline_20d: float = 0.0, decline_5d: float = 0.0,
        rsi: Optional[float] = None, macd_signal: str = "",
        kdj_signal: str = "", ma_alignment: str = "", obv_signal: str = "",
        volume_shrink: bool = False, amplitude_narrow: bool = False,
        path_label: str = "", score: int = 0, signal_level: str = "",
    ):
        self.code = code
        self.name = name
        self.price = price
        self.change_pct = change_pct
        self.volume = volume
        self.amount = amount
        self.market_cap = market_cap
        self.pe_ratio = pe_ratio
        self.decline_20d = decline_20d
        self.decline_5d = decline_5d
        self.rsi = rsi
        self.macd_signal = macd_signal
        self.kdj_signal = kdj_signal
        self.ma_alignment = ma_alignment
        self.obv_signal = obv_signal
        self.volume_shrink = volume_shrink
        self.amplitude_narrow = amplitude_narrow
        self.path_label = path_label
        self.score = score
        self.signals = []
        self.signal_level = signal_level

    @property
    def amount_yi(self) -> float:
        return self.amount / 1e8

    @property
    def market_cap_yi(self) -> float:
        return self.market_cap / 1e8


# ============================================================
# 第0步：全市场股票快照 + 预过滤
# ============================================================

def _retry_with_backoff(fn, name: str, max_retries: int = 3, base_delay: float = 2.0):
    """带指数退避的重试"""
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                delay = base_delay * (2 ** (attempt - 2))
                log.info(f"{name}: 第{attempt}次重试(等{delay:.0f}s)...")
                time.sleep(delay)
            return fn()
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                log.debug(f"{name}: 第{attempt}次失败 - {e}")
    raise last_error  # type: ignore[misc]

def _get_stock_list(max_stocks: int = 300) -> list[dict]:
    """获取全市场A股（排除ST），预过滤后返回候选池

    预过滤条件：
    - 排除 ST/*ST 股票（名称含 ST）
    - 排除北交所（8开头）、三板（4开头）
    - 成交额 > 3000万
    - 总市值 > 10亿
    - 当日涨幅 < 5%（已启动的不追）

    主路径：全市场快照（带重试）
    备用路径：代码列表 → Sina 批量报价

    Returns:
        候选股票列表，按成交额降序，最多 max_stocks 只
    """
    import akshare as ak

    # ---- 方法1：全市场快照（多次重试，长间隔）----
    try:
        log.info("正在获取全市场A股快照...")
        df = _retry_with_backoff(
            lambda: ak.stock_zh_a_spot_em(),
            name="全市场快照",
            max_retries=5,
            base_delay=5.0,  # 5s → 10s → 20s → 40s → 80s
        )
        log.info(f"全市场快照获取成功: {len(df)}只")
    except Exception as e:
        log.warning(f"全市场快照获取失败(已重试3次): {e}，尝试备用方案")
        return _get_stock_list_fallback_via_sina(max_stocks)

    if df is None or df.empty:
        return _get_stock_list_fallback_via_sina(max_stocks)

    candidates = []
    for _, row in df.iterrows():
        code = str(row.get('代码', ''))
        name = str(row.get('名称', ''))

        if 'ST' in name.upper():
            continue
        if code.startswith(('8', '4', '1', '2')):
            continue

        amount = _safe_float(row.get('成交额'))
        if not amount or amount < 30_000_000:
            continue

        market_cap = _safe_float(row.get('总市值'))
        if market_cap and market_cap < 1_000_000_000:
            continue

        change_pct = _safe_float(row.get('涨跌幅')) or 0
        if _is_near_limit_up(change_pct, code):
            continue

        candidates.append({
            'code': code,
            'name': name,
            'price': _safe_float(row.get('最新价')) or 0,
            'amount': amount,
            'volume': _safe_float(row.get('成交量')) or 0,
            'change_pct': change_pct,
            'market_cap': market_cap or 0,
            'pe_ratio': _safe_float(row.get('市盈率(动态)')),
        })

    candidates.sort(key=lambda x: x['amount'], reverse=True)
    log.info(f"A股预过滤: {len(candidates)}只 (无ST+成交>3000万+市值>10亿, 涨<5%)")
    return candidates[:max_stocks]


def _get_stock_list_fallback_via_sina(max_stocks: int) -> list[dict]:
    """备用方案：代码列表 + Sina 批量报价

    1. 通过 stock_info_a_code_name 获取全A股代码（轻量，全天可用）
    2. 筛选掉ST
    3. 用 Sina 财经 API 批量获取行情（全天可用，每次 200 只）
    4. 过滤后返回候选池
    """
    import akshare as ak

    # Step 1: 获取代码列表（带重试）
    try:
        log.info("备用方案：获取A股代码列表...")
        df = _retry_with_backoff(
            lambda: ak.stock_info_a_code_name(),
            name="A股代码列表",
            max_retries=2,
            base_delay=2.0,
        )
    except Exception as e:
        log.warning(f"代码列表获取失败: {e}")
        return []

    if df is None or df.empty:
        return []

    # 提取代码（排除ST），按代码开头混合采样确保沪深覆盖
    import random
    sh_codes = []   # 60xxxx, 68xxxx
    sz_codes_00 = [] # 00xxxx
    sz_codes_30 = [] # 30xxxx
    for _, row in df.iterrows():
        code = str(row.get('code', row.get('代码', '')))
        name = str(row.get('name', row.get('名称', '')))
        if 'ST' in name.upper():
            continue
        code = code.strip()
        if not code or len(code) != 6 or code.startswith(('8', '4', '1', '2')):
            continue
        if code.startswith('60') or code.startswith('68'):
            sh_codes.append(code)
        elif code.startswith('00'):
            sz_codes_00.append(code)
        elif code.startswith('30'):
            sz_codes_30.append(code)

    # 按市值比例分配名额：沪市≈60%，深市主板≈25%，创业板≈15%
    total = max_stocks * 4
    n_sh = int(total * 0.6)
    n_sz_00 = int(total * 0.25)
    # 创业板补满
    random.shuffle(sh_codes)
    random.shuffle(sz_codes_00)
    random.shuffle(sz_codes_30)

    codes = sh_codes[:n_sh] + sz_codes_00[:n_sz_00] + sz_codes_30
    codes = codes[:total]
    random.shuffle(codes)  # 最终打散，避免同一板块批量请求

    log.info(f"代码采样: 沪市{len(sh_codes[:n_sh])} + 深主{len(sz_codes_00[:n_sz_00])} + 创{len(sz_codes_30[:total-n_sh-n_sz_00])} = {len(codes)}只")
    log.info(f"将获取 {len(codes)} 只股票的Sina行情（分批，每批200只）...")

    # Step 2: 批量获取 Sina 行情
    candidates = []
    batch_size = 200
    for batch_start in range(0, len(codes), batch_size):
        batch = codes[batch_start:batch_start + batch_size]
        batch_candidates = _fetch_sina_batch_quotes(batch)
        for c in batch_candidates:
            candidates.append(c)
        time.sleep(0.3)  # 批次间隔

    # 按成交额降序
    candidates.sort(key=lambda x: x.get('amount', 0), reverse=True)
    result = candidates[:max_stocks]

    log.info(f"Sina备用: {len(candidates)}只有效行情 → 返回{len(result)}只候选")
    return result


def _fetch_sina_batch_quotes(codes: list[str]) -> list[dict]:
    """用新浪财经 API 批量获取股票行情

    Sina 返回格式（每行）:
    var hq_str_sh600036="招商银行,35.820,35.500,35.300,..."
    字段: 名称,今开,昨收,现价,最高,最低,买一,卖一,成交量(手),成交额(万),...
    """
    from app.http_client import sina_client

    # 构建 Sina 请求：sh600036,sz000333,...
    sina_codes = []
    for code in codes:
        market = _detect_market(code)
        sina_codes.append(f"{market.lower()}{code}")

    url = SINA_API + ",".join(sina_codes)
    resp = sina_client.get(url)

    if resp is None:
        log.warning("Sina批量行情请求失败")
        return []

    resp.encoding = "gbk"
    text = resp.text.strip()
    if not text:
        return []

    results = []
    lines = text.strip().split("\n")

    for i, line in enumerate(lines):
        if i >= len(sina_codes):
            break

        match = re.search(r'"([^"]*)"', line)
        if not match:
            continue

        fields = match.group(1).split(",")
        if len(fields) < 10:
            continue

        code = codes[i]
        name = fields[0].strip() if fields[0] else ""

        price = _safe_float(fields[3])
        pre_close = _safe_float(fields[2])
        volume = _safe_float(fields[8])  # 手
        amount_raw = _safe_float(fields[9])  # 万元 → 转为元

        # 计算涨跌幅
        change_pct = 0.0
        if price and pre_close and pre_close > 0:
            change_pct = round((price - pre_close) / pre_close * 100, 2)

        # 成交额转为元
        amount = (amount_raw or 0) * 10_000

        # 流动性和涨幅过滤
        if not amount or amount < 30_000_000:
            continue
        if _is_near_limit_up(change_pct, code):
            continue

        results.append({
            'code': code,
            'name': name,
            'price': price or 0,
            'amount': amount,
            'volume': (volume or 0) * 100,  # 手 → 股
            'change_pct': change_pct,
            'market_cap': 0,   # Sina 不提供
            'pe_ratio': None,  # Sina 不提供
        })

    return results


# ============================================================
# 第1步：K线获取
# ============================================================

def _fetch_stock_klines(code: str, days: int = 60) -> list[KlineData]:
    """获取单只个股日线 K 线（带重试）"""
    try:
        import akshare as ak

        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days + 10)).strftime('%Y%m%d')

        def _do_fetch():
            return ak.stock_zh_a_hist(
                symbol=str(code), period='daily',
                start_date=start_date, end_date=end_date, adjust='qfq'
            )

        df = _retry_with_backoff(
            _do_fetch,
            name=f"K线({code})",
            max_retries=2,
            base_delay=1.5,  # 每秒1次的限流，1.5s足够
        )

        if df is None or df.empty:
            return []

        df = df.tail(days)
        klines = []
        for _, row in df.iterrows():
            klines.append(KlineData(
                date=str(row.get('日期', '')),
                open=_safe_float(row.get('开盘')),
                high=_safe_float(row.get('最高')),
                low=_safe_float(row.get('最低')),
                close=_safe_float(row.get('收盘')),
                volume=_safe_float(row.get('成交量')),
            ))
        return klines

    except ImportError:
        return []
    except Exception as e:
        log.debug(f"K线获取失败 {code}: {e}")
        return []


# ============================================================
# 第2步：量价筑底特征
# ============================================================

def _check_price_volume_pattern(klines: list[KlineData]) -> tuple[bool, float, float, bool, bool, str]:
    """检查量价筑底特征（双路径）

    路径1 筑底中：还在下跌/盘整 + 缩量或振幅收敛（磨底阶段）
    路径2 确认反转：中期明显下跌过 + 近5日止跌反弹 + 量能不萎缩（反转已启动）

    Returns:
        (通过, 近20日跌幅%, 近5日跌幅%, 缩量筑底, 振幅收敛, 路径标签)
    """
    if len(klines) < 25:
        return False, 0, 0, False, False, ""

    closes = [k.close for k in klines if k.close is not None]
    volumes = [k.volume for k in klines if k.volume is not None]
    highs = [k.high for k in klines if k.high is not None]
    lows = [k.low for k in klines if k.low is not None]

    if len(closes) < 25:
        return False, 0, 0, False, False, ""

    # 近20日收益率
    if closes[-21] and closes[-21] > 0:
        decline_20d = (closes[-1] - closes[-21]) / closes[-21] * 100
    else:
        decline_20d = 0

    # 排除：持续上涨中（不是底部）
    if decline_20d > 12:
        return False, decline_20d, 0, False, False, ""

    # 近5日收益率
    if closes[-6] and closes[-6] > 0:
        decline_5d = (closes[-1] - closes[-6]) / closes[-6] * 100
    else:
        decline_5d = 0

    # 成交量统计
    vol_5d_avg = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0
    vol_20d_avg = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
    volume_shrink = vol_5d_avg < vol_20d_avg * 0.7 if vol_20d_avg > 0 else False
    vol_rising = vol_5d_avg >= vol_20d_avg * 0.5 if vol_20d_avg > 0 else False

    # 振幅
    amps_5d = [(h - l) / c for h, l, c in zip(highs[-5:], lows[-5:], closes[-5:]) if c and c > 0]
    amps_20d = [(h - l) / c for h, l, c in zip(highs[-20:], lows[-20:], closes[-20:]) if c and c > 0]
    amp_5d = sum(amps_5d) / len(amps_5d) if amps_5d else 0
    amp_20d = sum(amps_20d) / len(amps_20d) if amps_20d else 1
    amplitude_narrow = amp_5d < amp_20d * 0.7 if amp_20d > 0 else False

    # ---- 路径1: 筑底中 ----
    path1 = (decline_20d < 2 or decline_5d < 0) and (volume_shrink or amplitude_narrow)

    # ---- 路径2: 确认反转（中期跌过 + 近5日回升 + 量能不萎缩） ----
    # 个股反弹幅度放宽到 12%（主板一个板 = 10%）
    path2 = (decline_20d <= -5 and 0 < decline_5d <= 12 and vol_rising)

    if path1:
        return True, decline_20d, decline_5d, volume_shrink, amplitude_narrow, "筑底中"
    if path2:
        return True, decline_20d, decline_5d, volume_shrink, amplitude_narrow, "确认反转"
    return False, decline_20d, decline_5d, volume_shrink, amplitude_narrow, ""


# ============================================================
# 第3步：技术指标共振评分（与 ETF 共用逻辑）
# ============================================================

def _score_technical_signals(klines: list[KlineData], decline_20d: float) -> tuple[int, list[str], dict]:
    """技术指标共振评分

    Returns:
        (score 0-100, signals列表, indicators字典)
    """
    if len(klines) < 30:
        return 0, [], {}

    closes = [k.close for k in klines if k.close is not None]
    highs = [k.high for k in klines if k.high is not None]
    lows = [k.low for k in klines if k.low is not None]

    if len(closes) < 26 or len(highs) < 9 or len(lows) < 9:
        return 0, [], {}

    score = 0
    signals = []
    indicators = {}

    # RSI (0-25)
    rsi = calc_rsi(closes)
    indicators['rsi'] = rsi
    indicators['rsi_signal'] = rsi_signal(rsi)
    if rsi is not None:
        if rsi < 30:
            score += 25
            signals.append(f"RSI超卖({rsi:.0f})")
        elif rsi < 40:
            score += 15
            signals.append(f"RSI偏低({rsi:.0f})")
        elif rsi < 50:
            score += 5

    # MACD (0-25)
    macd = calc_macd(closes)
    indicators['macd_dif'] = macd.dif
    indicators['macd_signal'] = macd.signal
    if macd.signal == "金叉":
        score += 25
        signals.append("MACD金叉")
    elif macd.signal == "多头":
        score += 15
        signals.append("MACD多头")
    elif macd.signal == "空头" and macd.histogram and macd.histogram > 0:
        score += 10
        signals.append("MACD收敛")

    # KDJ (0-15)
    kdj = calc_kdj(highs, lows, closes)
    indicators['kdj_k'] = kdj.k
    indicators['kdj_signal'] = kdj.signal
    if kdj.signal == "金叉" and kdj.k and kdj.k < 50:
        score += 15
        signals.append(f"KDJ低位金叉(K={kdj.k:.0f})")
    elif kdj.signal == "超卖":
        score += 10
        signals.append("KDJ超卖")

    # 均线排列 (0-15)
    ma = calc_ma_alignment(klines)
    indicators['ma_alignment'] = ma.alignment
    indicators['ma_alignment_detail'] = ma.detail
    if ma.alignment == "多头排列":
        score += 10
        signals.append("均线多头排列")
    elif ma.alignment == "多头回调":
        score += 15
        signals.append("均线多头回调")
    elif ma.alignment == "缠绕" and "偏多" in ma.detail:
        score += 8
        signals.append("均线偏多震荡")

    # OBV (0-10)
    obv = calc_obv(klines)
    indicators['obv_signal'] = obv.signal
    if obv.signal in ("底背离",):
        score += 10
        signals.append(f"OBV{obv.signal}(吸筹)")
    elif obv.signal in ("资金转向流入", "资金持续流入", "资金加速流入"):
        score += 7
        signals.append(f"OBV{obv.signal}")

    # 超跌加分 (0-10)
    if decline_20d < -20:
        score += 10
        signals.append(f"超跌({decline_20d:.0f}%)")
    elif decline_20d < -12:
        score += 5
    elif decline_20d < -5:
        score += 3

    return min(score, 100), signals, indicators


def _determine_level(score: int, signals: list[str]) -> str:
    if score >= 65 and len(signals) >= 3:
        return "强信号"
    elif score >= 45:
        return "中信号"
    elif score >= 25:
        return "弱信号"
    else:
        return "关注"


# ============================================================
# 主入口
# ============================================================

def screen_stock_bottom_reversal(
    max_candidates: int = 20,
    max_kline_fetch: int = 100,
) -> list[StockCandidate]:
    """全市场A股底部反转筛选（排除ST）

    Args:
        max_candidates: 最多返回多少个候选
        max_kline_fetch: 最多拉取多少只股票的K线

    Returns:
        StockCandidate 列表，按得分降序
    """
    log.info("========== A股底部反转筛选 ==========")

    # 第0步：全市场快照 + 预过滤
    stock_list = _get_stock_list(max_stocks=max_kline_fetch)
    if not stock_list:
        log.warning("无法获取A股列表，筛选中止")
        return []

    log.info(f"候选池: {len(stock_list)}只，开始K线分析...")

    results: list[StockCandidate] = []
    checked = 0
    passed_stage2 = 0
    import time

    for stock in stock_list:
        checked += 1
        code = stock['code']

        # 第1步：K线
        klines = _fetch_stock_klines(code, days=60)
        if not klines or len(klines) < 25:
            continue

        # 第2步：量价特征
        passed, dec_20d, dec_5d, vol_shrink, amp_narrow, path_label = _check_price_volume_pattern(klines)
        if not passed:
            continue
        passed_stage2 += 1

        # 第3步：技术评分
        score, signals, indicators = _score_technical_signals(klines, dec_20d)

        c = StockCandidate(
            code=code, name=stock['name'],
            price=stock['price'], change_pct=stock['change_pct'],
            volume=stock['volume'], amount=stock['amount'],
            market_cap=stock.get('market_cap', 0),
            pe_ratio=stock.get('pe_ratio'),
            decline_20d=round(dec_20d, 1), decline_5d=round(dec_5d, 1),
            rsi=indicators.get('rsi'),
            macd_signal=indicators.get('macd_signal', ''),
            kdj_signal=indicators.get('kdj_signal', ''),
            ma_alignment=indicators.get('ma_alignment', ''),
            obv_signal=indicators.get('obv_signal', ''),
            volume_shrink=vol_shrink,
            amplitude_narrow=amp_narrow,
            path_label=path_label,
            score=score,
            signal_level=_determine_level(score, signals),
        )
        c.signals = signals
        results.append(c)

        if checked % 20 == 0:
            log.info(f"  已分析: {checked}/{len(stock_list)}, "
                     f"通过量价: {passed_stage2}, 入选: {len(results)}")

        # 频率限制 — AKShare 个股接口约 1 次/秒，留 0.7s 余量
        time.sleep(0.7)

    results.sort(key=lambda x: x.score, reverse=True)
    results = results[:max_candidates]

    log.info(f"筛选完成: 分析{checked}只 → 量价通过{passed_stage2}只 → 最终{len(results)}只")
    return results


# ============================================================
# 报告生成
# ============================================================

def generate_stock_screen_report(
    candidates: list[StockCandidate],
    output_dir: Path | None = None,
) -> str:
    """生成股票筛选报告（Markdown）"""
    now = datetime.now()
    lines = [
        f"# 🔍 A股底部反转机会筛选",
        f"",
        f"**筛选时间**: {now.strftime('%Y-%m-%d %H:%M')}",
        f"**筛选范围**: 全市场A股（排除ST，成交额>3000万，市值>10亿）",
        f"**筛选逻辑**: 不追高→缩量筑底/振幅收敛→技术指标共振",
        f"**结果**: {len(candidates)} 只候选",
        f"",
        f"---",
        f"",
    ]

    if not candidates:
        lines.append("> 未发现符合条件的底部反转股票")
        lines.append("")
        report = "\n".join(lines)
        if output_dir:
            _write_report(report, output_dir, now, 'stock')
        return report

    strong = [c for c in candidates if c.signal_level == "强信号"]
    medium = [c for c in candidates if c.signal_level == "中信号"]
    weak = [c for c in candidates if c.signal_level in ("弱信号", "关注")]

    for label, group, stars in [
        ("强信号", strong, "⭐⭐⭐"),
        ("中信号", medium, "⭐⭐"),
        ("弱信号/关注", weak, "⭐"),
    ]:
        if not group:
            continue
        lines.append(f"## {stars} {label} ({len(group)}只)")
        lines.append("")
        lines.append("| # | 代码 | 名称 | 现价 | 涨跌 | 20日跌 | 5日跌 | 市值 | "
                     "成交额 | PE | RSI | MACD | KDJ | 均线 | OBV | 路径 | 得分 |")
        lines.append("| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | "
                     "---: | ---: | ---: | --- | --- | --- | --- | --- | ---: |")

        for i, c in enumerate(group, 1):
            chg = f"{c.change_pct:+.2f}%" if c.change_pct else "--"
            rsi_str = f"{c.rsi:.0f}" if c.rsi is not None else "--"
            pe_str = f"{c.pe_ratio:.1f}" if c.pe_ratio else "--"
            mcap_str = f"{c.market_cap_yi:.0f}亿" if c.market_cap > 0 else "--"
            lines.append(
                f"| {i} | {c.code} | {c.name} | {c.price:.2f} | {chg} | "
                f"{c.decline_20d:+.1f}% | {c.decline_5d:+.1f}% | {mcap_str} | "
                f"{c.amount_yi:.1f}亿 | {pe_str} | {rsi_str} | {c.macd_signal} | "
                f"{c.kdj_signal} | {c.ma_alignment} | {c.obv_signal} | "
                f"{c.path_label or '--'} | {c.score} |"
            )
        lines.append("")
        lines.append("**触发信号**:")
        lines.append("")
        for c in group:
            tags = ' + '.join(c.signals) if c.signals else '无明显信号'
            vol_note = ""
            if c.volume_shrink:
                vol_note += "缩量 "
            if c.amplitude_narrow:
                vol_note += "振幅收敛 "
            if vol_note:
                vol_note = f" [{vol_note.strip()}]"
            lines.append(f"- **{c.name}**({c.code}): {tags}{vol_note}")
        lines.append("")

    lines.append("---")
    lines.append(f"*报告由盯盘雷达自动生成 · {now.strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")

    report = "\n".join(lines)

    if output_dir:
        _write_report(report, output_dir, now, 'stock')

    return report


def _write_report(report: str, output_dir: Path, now: datetime, prefix: str):
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{prefix}_screen_{now.strftime('%Y%m%d_%H%M')}.md"
        (output_dir / filename).write_text(report, encoding='utf-8')
        log.info(f"筛选报告已保存: {output_dir / filename}")
    except Exception as e:
        log.error(f"报告写入失败: {e}")
