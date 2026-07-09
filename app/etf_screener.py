"""
智能ETF筛选引擎 —— 底部反转机会发现

在全市场 ETF 中筛选出现底部反转信号的标的。
三阶段过滤：
  1. 流动性过滤（日成交 > 1000万，排除僵尸ETF）
  2. 量价特征过滤（近期下跌 + 缩量止跌/筑底特征）
  3. 技术指标共振（RSI/MACD/KDJ/均线/OBV 多指标确认）
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.utils import log
from app.technical import (
    calc_rsi, calc_macd, calc_kdj, calc_ma_alignment, calc_obv,
    calc_sma, rsi_signal,
)
from app.models import KlineData


# ============================================================
# 数据模型
# ============================================================

@dataclass
class ETFCandidate:
    """ETF 候选标的"""
    code: str = ""
    name: str = ""
    price: float = 0.0
    change_pct: float = 0.0
    volume: float = 0.0          # 成交量
    amount: float = 0.0           # 成交额（元）
    decline_20d: float = 0.0      # 近20日跌幅(%)
    decline_5d: float = 0.0       # 近5日跌幅(%)
    rsi: Optional[float] = None
    macd_signal: str = ""
    kdj_signal: str = ""
    ma_alignment: str = ""
    obv_signal: str = ""
    volume_shrink: bool = False   # 缩量筑底
    amplitude_narrow: bool = False  # 振幅收敛
    score: int = 0                # 综合得分 0-100
    signals: list[str] = field(default_factory=list)
    signal_level: str = ""        # 强信号/中信号/弱信号

    @property
    def amount_yi(self) -> float:
        """成交额（亿元）"""
        return self.amount / 1e8


# ============================================================
# 第0步：获取ETF全列表
# ============================================================

# ETF 代码前缀（A股上市ETF的代码规律）
ETF_CODE_PATTERNS = (
    '51',    # 510xxx, 512xxx, 513xxx, 515xxx, 516xxx, 517xxx, 518xxx (SH)
    '159',   # 159xxx (SZ)
    '56',    # 560xxx, 561xxx, 562xxx, 563xxx (SH)
    '588',   # 588xxx (SH)
)


def _strip_code_prefix(code: str) -> str:
    """去除代码中的市场前缀（sz159998 → 159998, sh510300 → 510300）"""
    code = str(code).strip()
    if code.lower().startswith(('sz', 'sh')):
        return code[2:]
    return code


def _get_etf_list() -> list[dict]:
    """获取全市场 ETF 列表（仅取流动性合格的）

    Returns:
        ETF 基础信息列表，按成交额降序
    """
    import akshare as ak

    # ---- 方法1: 新浪 ETF 分类（全天可用，非交易时段也能用）----
    try:
        df = ak.fund_etf_category_sina(symbol='ETF基金')
        if df is not None and not df.empty:
            etfs = []
            for _, row in df.iterrows():
                raw_code = str(row.get('代码', ''))
                code = _strip_code_prefix(raw_code)
                if not code:
                    continue
                amount = _safe_float(row.get('成交额'))
                if amount and amount >= 10_000_000:
                    etfs.append({
                        'code': code,
                        'name': str(row.get('名称', '')),
                        'price': _safe_float(row.get('最新价')) or 0,
                        'amount': amount,
                        'volume': _safe_float(row.get('成交量')) or 0,
                        'change_pct': _safe_float(row.get('涨跌额')) or 0,
                    })

            etfs.sort(key=lambda x: x['amount'], reverse=True)
            log.info(f"ETF流动性过滤: {len(etfs)}只 (成交额>1000万, 来源: Sina)")
            if etfs:
                return etfs
            log.warning("Sina ETF列表为空(过滤后)，尝试全市场快照")
    except Exception as e:
        log.warning(f"Sina ETF列表获取失败: {e}，尝试全市场快照")

    # ---- 方法2: 东方财富全市场快照（仅交易时段可靠）----
    try:
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            log.warning("全市场行情数据也为空，无法获取ETF列表")
            return []

        etf_mask = df['代码'].astype(str).str.startswith(ETF_CODE_PATTERNS)
        df_etf = df[etf_mask]

        if df_etf.empty:
            log.warning("未从全市场数据中找到ETF")
            return []

        etfs = []
        for _, row in df_etf.iterrows():
            code = str(row.get('代码', ''))
            amount = _safe_float(row.get('成交额'))
            if amount and amount >= 10_000_000:
                etfs.append({
                    'code': code,
                    'name': str(row.get('名称', '')),
                    'price': _safe_float(row.get('最新价')) or 0,
                    'amount': amount,
                    'volume': _safe_float(row.get('成交量')) or 0,
                    'change_pct': _safe_float(row.get('涨跌幅')) or 0,
                })

        etfs.sort(key=lambda x: x['amount'], reverse=True)
        log.info(f"ETF流动性过滤: {len(etfs)}只 (成交额>1000万, 来源: 全市场快照)")
        return etfs

    except Exception as e:
        log.warning(f"全市场快照也失败: {e}")

    log.warning("所有ETF列表获取方式均失败")
    return []


# ============================================================
# 第1步：日线K线获取（带缓存）
# ============================================================

def _fetch_etf_klines(code: str, days: int = 60) -> list[KlineData]:
    """获取单只 ETF 日线 K 线"""
    try:
        import akshare as ak
        # 计算起止日期
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days + 10)).strftime('%Y%m%d')

        df = ak.fund_etf_hist_em(
            symbol=str(code), period='daily',
            start_date=start_date, end_date=end_date, adjust='qfq'
        )

        if df is None or df.empty:
            return []

        # 取最近 days 条
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
        log.debug(f"ETF K线获取失败 {code}: {e}")
        return []


# ============================================================
# 第2步：量价特征过滤
# ============================================================

def _check_price_volume_pattern(klines: list[KlineData], quote: dict) -> tuple[bool, float, float, bool, bool]:
    """检查量价筑底特征

    Returns:
        (通过, 近20日跌幅%, 近5日跌幅%, 缩量筑底, 振幅收敛)
    """
    if len(klines) < 25:
        return False, 0, 0, False, False

    closes = [k.close for k in klines if k.close is not None]
    volumes = [k.volume for k in klines if k.volume is not None]
    highs = [k.high for k in klines if k.high is not None]
    lows = [k.low for k in klines if k.low is not None]

    if len(closes) < 25:
        return False, 0, 0, False, False

    # 近20日跌幅（从20日前收盘到今日收盘）
    if closes[-21] and closes[-21] > 0:
        decline_20d = (closes[-1] - closes[-21]) / closes[-21] * 100
    else:
        decline_20d = 0

    # 排除：上涨趋势中（不找"底部"）
    if decline_20d > 5:
        return False, decline_20d, 0, False, False

    # 近5日跌幅
    if closes[-6] and closes[-6] > 0:
        decline_5d = (closes[-1] - closes[-6]) / closes[-6] * 100
    else:
        decline_5d = 0

    # 缩量筑底：近5日均量 < 近20日均量 * 0.7
    vol_5d_avg = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0
    vol_20d_avg = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
    volume_shrink = vol_5d_avg < vol_20d_avg * 0.7 if vol_20d_avg > 0 else False

    # 振幅收敛：近5日均振幅 < 近20日均振幅 * 0.7
    amps_5d = [(h - l) / c for h, l, c in zip(highs[-5:], lows[-5:], closes[-5:]) if c and c > 0]
    amps_20d = [(h - l) / c for h, l, c in zip(highs[-20:], lows[-20:], closes[-20:]) if c and c > 0]
    amp_5d = sum(amps_5d) / len(amps_5d) if amps_5d else 0
    amp_20d = sum(amps_20d) / len(amps_20d) if amps_20d else 1
    amplitude_narrow = amp_5d < amp_20d * 0.7 if amp_20d > 0 else False

    # 通过条件：下跌 + (缩量 或 振幅收敛)
    passed = (decline_20d < 0 or decline_5d < 0) and (volume_shrink or amplitude_narrow)
    return passed, decline_20d, decline_5d, volume_shrink, amplitude_narrow


# ============================================================
# 第3步：技术指标共振评分
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

    # --- RSI (0-25分) ---
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

    # --- MACD (0-25分) ---
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
        # 柱状图在缩小（从空头向多头转变）
        score += 10
        signals.append("MACD收敛")

    # --- KDJ (0-15分) ---
    kdj = calc_kdj(highs, lows, closes)
    indicators['kdj_k'] = kdj.k
    indicators['kdj_signal'] = kdj.signal
    if kdj.signal == "金叉" and kdj.k and kdj.k < 50:
        score += 15
        signals.append(f"KDJ低位金叉(K={kdj.k:.0f})")
    elif kdj.signal == "超卖":
        score += 10
        signals.append("KDJ超卖")

    # --- 均线排列 (0-15分) ---
    ma = calc_ma_alignment(klines)
    indicators['ma_alignment'] = ma.alignment
    indicators['ma_alignment_detail'] = ma.detail
    if ma.alignment == "多头排列":
        score += 10  # 已确认多头，加分但不加分太多（已不是"底部"）
        signals.append("均线多头排列")
    elif ma.alignment == "多头回调":
        score += 15  # 回调中找到支撑，更好的买点
        signals.append("均线多头回调")
    elif ma.alignment == "缠绕" and "偏多" in ma.detail:
        score += 8
        signals.append("均线偏多震荡")

    # --- OBV 资金流向 (0-10分) ---
    obv = calc_obv(klines)
    indicators['obv_signal'] = obv.signal
    if obv.signal in ("底背离",):
        score += 10
        signals.append(f"OBV{obv.signal}(吸筹)")
    elif obv.signal in ("资金转向流入", "资金持续流入"):
        score += 7
        signals.append(f"OBV{obv.signal}")

    # --- 跌幅加分 (0-10分，超跌加分) ---
    if decline_20d < -15:
        score += 10
        signals.append(f"超跌({decline_20d:.0f}%)")
    elif decline_20d < -10:
        score += 5
    elif decline_20d < -5:
        score += 3

    return min(score, 100), signals, indicators


# ============================================================
# 第4步：综合排名
# ============================================================

def _determine_level(score: int, signals: list[str]) -> str:
    """根据得分和信号数确定等级"""
    if score >= 65 and len(signals) >= 3:
        return "强信号 ⭐⭐⭐"
    elif score >= 45:
        return "中信号 ⭐⭐"
    elif score >= 25:
        return "弱信号 ⭐"
    else:
        return "关注"


# ============================================================
# 主入口
# ============================================================

def screen_etf_bottom_reversal(
    max_candidates: int = 15,
    max_kline_fetch: int = 80,
) -> list[ETFCandidate]:
    """全市场 ETF 底部反转筛选

    Args:
        max_candidates: 最多返回多少个候选
        max_kline_fetch: 最多拉取多少只ETF的K线（控制API调用量）

    Returns:
        ETFCandidate 列表，按得分降序
    """
    log.info("========== ETF底部反转筛选 ==========")

    # 第0步：获取ETF列表（已做流动性过滤）
    etf_list = _get_etf_list()
    if not etf_list:
        log.warning("无法获取ETF列表，筛选中止")
        return []

    log.info(f"候选池: {len(etf_list)}只 (成交额>1000万)，将对前{max_kline_fetch}只进行深度分析")

    # 限制深度分析数量
    candidates_to_check = etf_list[:max_kline_fetch]
    log.info(f"开始逐只分析...")

    results: list[ETFCandidate] = []
    checked = 0
    passed_stage2 = 0
    import time

    for etf in candidates_to_check:
        checked += 1
        code = etf['code']
        name = etf['name']

        # 第1步：获取K线
        klines = _fetch_etf_klines(code, days=60)
        if not klines or len(klines) < 25:
            continue

        # 第2步：量价特征过滤
        passed, decline_20d, decline_5d, vol_shrink, amp_narrow = _check_price_volume_pattern(klines, etf)
        if not passed:
            continue
        passed_stage2 += 1

        # 第3步：技术指标共振评分
        score, signals, indicators = _score_technical_signals(klines, decline_20d)

        # 构建候选
        candidate = ETFCandidate(
            code=code,
            name=name,
            price=etf['price'],
            change_pct=_safe_float(etf.get('change_pct', 0)),
            volume=etf['volume'],
            amount=etf['amount'],
            decline_20d=round(decline_20d, 1),
            decline_5d=round(decline_5d, 1),
            rsi=indicators.get('rsi'),
            macd_signal=indicators.get('macd_signal', ''),
            kdj_signal=indicators.get('kdj_signal', ''),
            ma_alignment=indicators.get('ma_alignment', ''),
            obv_signal=indicators.get('obv_signal', ''),
            volume_shrink=vol_shrink,
            amplitude_narrow=amp_narrow,
            score=score,
            signals=signals,
            signal_level=_determine_level(score, signals),
        )
        results.append(candidate)

        # 进度日志
        if checked % 10 == 0:
            log.info(f"  已分析: {checked}/{len(candidates_to_check)}, "
                     f"通过量价: {passed_stage2}, 入选: {len(results)}")

        # 频率限制
        time.sleep(0.5)

    # 按得分降序排名
    results.sort(key=lambda x: x.score, reverse=True)
    results = results[:max_candidates]

    log.info(f"筛选完成: 分析{checked}只 → 量价通过{passed_stage2}只 → 最终{len(results)}只")
    return results


# ============================================================
# 报告生成
# ============================================================

def generate_etf_screen_report(
    candidates: list[ETFCandidate],
    output_dir: Path | None = None,
) -> str:
    """生成 ETF 筛选报告（Markdown 格式）

    Args:
        candidates: 筛选结果
        output_dir: 输出目录（可选，不传则不写文件）

    Returns:
        Markdown 报告内容
    """
    now = datetime.now()
    lines = [
        f"# 🔍 ETF 底部反转机会筛选",
        f"",
        f"**筛选时间**: {now.strftime('%Y-%m-%d %H:%M')}",
        f"**筛选范围**: 全市场 ETF（日成交额 > 1000万）",
        f"**筛选逻辑**: 下跌→缩量筑底/振幅收敛→技术指标共振（RSI+MACD+KDJ+均线+OBV）",
        f"**结果**: {len(candidates)} 只候选",
        f"",
        f"---",
        f"",
    ]

    if not candidates:
        lines.append("> ⚠️ 未发现符合条件的底部反转 ETF 机会")
        lines.append("")
        report = "\n".join(lines)
        if output_dir:
            _write_report(report, output_dir, now)
        return report

    # 按等级分组
    strong = [c for c in candidates if c.signal_level == "强信号 ⭐⭐⭐"]
    medium = [c for c in candidates if c.signal_level == "中信号 ⭐⭐"]
    weak = [c for c in candidates if c.signal_level in ("弱信号 ⭐", "关注")]

    for label, group in [("⭐⭐⭐ 强信号", strong), ("⭐⭐ 中信号", medium), ("⭐ 弱信号/关注", weak)]:
        if not group:
            continue
        lines.append(f"## {label} ({len(group)}只)")
        lines.append("")
        lines.append("| # | 代码 | 名称 | 现价 | 涨跌 | 20日跌 | 5日跌 | "
                     "成交额 | RSI | MACD | KDJ | 均线 | OBV | 得分 |")
        lines.append("| ---: | --- | --- | ---: | ---: | ---: | ---: | "
                     "---: | ---: | --- | --- | --- | --- | ---: |")

        for i, c in enumerate(group, 1):
            chg = f"{c.change_pct:+.2f}%" if c.change_pct else "--"
            rsi_str = f"{c.rsi:.0f}" if c.rsi is not None else "--"
            lines.append(
                f"| {i} | {c.code} | {c.name} | {c.price:.3f} | {chg} | "
                f"{c.decline_20d:+.1f}% | {c.decline_5d:+.1f}% | "
                f"{c.amount_yi:.1f}亿 | {rsi_str} | {c.macd_signal} | "
                f"{c.kdj_signal} | {c.ma_alignment} | {c.obv_signal} | "
                f"{c.score} |"
            )

        lines.append("")
        lines.append("**触发信号详情**:")
        lines.append("")
        for c in group:
            tags = ' + '.join(c.signals) if c.signals else '无明显信号'
            vol_note = ""
            if c.volume_shrink:
                vol_note += "缩量筑底 "
            if c.amplitude_narrow:
                vol_note += "振幅收敛 "
            if vol_note:
                vol_note = f" [量价: {vol_note.strip()}]"
            lines.append(f"- **{c.name}**({c.code}): {tags}{vol_note}")
        lines.append("")

    lines.append("---")
    lines.append(f"*报告由盯盘雷达自动生成 · {now.strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")

    report = "\n".join(lines)

    if output_dir:
        _write_report(report, output_dir, now)

    return report


def _write_report(report: str, output_dir: Path, now: datetime):
    """写入报告文件"""
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"etf_screen_{now.strftime('%Y%m%d_%H%M')}.md"
        filepath = output_dir / filename
        filepath.write_text(report, encoding='utf-8')
        log.info(f"ETF筛选报告已保存: {filepath}")
    except Exception as e:
        log.error(f"ETF筛选报告写入失败: {e}")


# ============================================================
# 工具函数
# ============================================================

def _safe_float(val) -> Optional[float]:
    """安全浮点转换"""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
