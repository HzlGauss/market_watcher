# Market Watcher 升级规划

> 生成日期: 2026-05-15
> 基于: 项目源码全量审查 + 业界开源项目对比分析

---

## 目录

- [一、项目现状与核心问题](#一项目现状与核心问题)
- [二、参考项目价值矩阵](#二参考项目价值矩阵)
- [三、升级方向（6 个方向）](#三升级方向6-个方向)
  - [方向一：数据源 + 技术指标双引擎升级](#方向一数据源--技术指标双引擎升级-p0)
  - [方向二：QuantStats 绩效报告](#方向二quantstats-绩效报告-p0)
  - [方向三：Backtrader 策略回测](#方向三backtrader-策略回测-p1)
  - [方向四：基金分析升级 - xalpha](#方向四基金分析升级---xalpha-p1)
  - [方向五：统计工具链升级](#方向五统计工具链升级-p1)
  - [方向六：Web 可视化大屏](#方向六web-可视化大屏-p2-p3)
- [四、TradingAgents 多智能体架构借鉴](#四tradingagents-多智能体架构借鉴)
- [五、前置条件：历史数据持久化](#五前置条件历史数据持久化)
- [六、实施路线图](#六实施路线图)
- [七、附录：参考资源](#七附录参考资源)

---

## 一、项目现状与核心问题

### 1.1 架构评价

```
market-watcher v2.0.0
├── __main__.py          # CLI 入口 + 监控主循环
├── app/
│   ├── config.py        # JSON + CSV 配置加载
│   ├── data_fetcher.py  # 新浪财经行情 + 东方财富北向资金
│   ├── models.py        # 所有 dataclass 定义
│   ├── technical.py     # 纯 Python 技术指标实现 (1400 行)
│   ├── analyzer.py      # 情绪评估 + 动态阈值 + 板块轮动
│   ├── ai_analyzer.py   # DeepSeek 单轮调用分析
│   ├── reporter.py      # 早/中/晚报生成
│   ├── fund_analyzer.py # 基金净值 + AI 分析
│   ├── presenter.py     # 控制台输出 + Markdown 简报
│   ├── state_manager.py # JSON 状态文件读写
│   ├── notifier.py      # ServerChan 微信推送
│   ├── broker_api.py    # 东方财富持仓同步 (mock)
│   ├── llm_client.py    # DeepSeek API 封装
│   └── http_client.py   # HTTP 会话管理
└── tests/
```

**现有优势**：
- 模块化清晰，类型注解全覆盖
- 纯 Python 技术指标实现（无外部依赖）
- DeepSeek 生成早/中/晚报 + 基金分析
- 动态阈值 + 板块轮动检测 + 持仓盈亏跟踪
- CSV 配置，可手动维护

### 1.2 关键短板

| 维度 | 现状 | 问题 |
|------|------|------|
| **数据源** | 仅新浪财经 | 不稳定、字段有限、无期货/宏观数据 |
| **技术指标** | 纯 Python 手写 1400 行 | 精度有限、无 ADX/CCI/Williams %R、无 K 线形态识别 |
| **回测能力** | 无 | 无法验证盯盘逻辑的历史有效性 |
| **绩效分析** | 只有 LLM 文本报告 | 缺少夏普比率、最大回撤等量化指标 |
| **可视化** | 纯控制台 | 无法交互、不适合复盘和共享 |
| **基金分析** | 净值 + AI 文字 | 缺持仓穿透、组合视角、QDII 实时估算 |
| **统计工具** | 手写 Pearson / VaR | 缺协整检验、时间序列分析、p-value |
| **LLM 架构** | 单 prompt 调用 | 缺乏多角色分工、缺乏决策记忆和反思 |
| **历史数据** | JSON 存最近几次快照 | 没有持久化存储 |

### 1.3 技术债明细

- `app/technical.py` 约 1400 行纯 Python 指标实现，维护成本高
- `app/broker_api.py` 的 `get_holdings()` 返回 mock 数据，非真实 API
- `_calc_returns` / `_pearson` / `calc_portfolio_var` 手写标准统计函数
- 所有模块强依赖新浪财经，无 failover 机制
- LLM 分析是单轮单角色，缺乏 TradingAgents 式的多视角碰撞

---

## 二、参考项目价值矩阵

| 项目/库 | Star | 核心价值 | 可复用位置 |
|---------|------|---------|-----------|
| **AKShare** | 高 | 免费开放的宽财经数据接口（A 股/期货/宏观） | 替换/补充 `data_fetcher.py` 的新浪行情源 |
| **TA-Lib** | 行业标准 | 150+ 指标 + K 线形态识别，C 加速 | 替换 `technical.py` 手写实现 |
| **QuantStats** | ~5k | 一键生成专业级绩效报告 | 新增 `performance.py` 嵌入晚报 |
| **PyFolio** | ~5k | 风险分解与收益归因 | 新增模块，风险暴露数据喂给 LLM |
| **Backtrader** | ~13k | 事件驱动回测框架 | 新增 `backtest.py` 验证策略 |
| **VectorBT** | ~5k | 向量化极速参数扫描 | 配合 Backtrader 做参数优化 |
| **xalpha** | ~1.3k | 基金全流程管理（持仓穿透/QDII 预估） | 增强 `fund_analyzer.py` |
| **statsmodels** | 行业标准 | 时间序列 + 协整检验 | 增强相关性分析 |
| **scipy** | 行业标准 | 科学计算 | 替换手写 Pearson / VaR / Z-score |
| **Plotly** | ~16k | 交互式 Web 图表 | 简报附带 HTML 图表 / Web 看板 |
| **TradingAgents** | 75k | 多角色 LLM 协作 + 决策记忆 | 重构 `ai_analyzer.py` |
| **Grafana+InfluxDB** | 行业标准 | 监控大屏 | 长期：旁路写入时序数据库 |

---

## 三、升级方向（6 个方向）

### 方向一：数据源 + 技术指标双引擎升级（P0）

**目标**：解决数据可靠性 + 技术指标能力不足两个核心痛点。

#### 3.1.1 AKShare 数据源

```python
# 方案: AKShare 做主源，新浪做 fallback
def fetch_quotes(items):
    if akshare_available:
        return _fetch_akshare(items)   # 主源: 附带 PE/市值/换手率
    return _fetch_sina(items)          # fallback: 现有逻辑保留
```

**注意**：
- AKShare 的东方财富后端有频率限制（~3 次/秒），批量 30+ 标的需分批 + sleep
- `app/models.py` 的 `Quote` 需扩展字段：`pe_ratio`、`turnover_rate`、`market_cap`
- AKShare 的 `stock_zh_a_spot_em()` 返回全市场数据，可在内存做本地过滤

#### 3.1.2 TA-Lib 策略引擎

```python
# 策略模式: TA-Lib 优先，纯 Python fallback
class TechnicalEngine:
    def __init__(self, engine="ta-lib"):
        self.engine = engine
        if engine == "ta-lib":
            import talib  # lazy import
    
    def calc_rsi(self, closes, period=14):
        if self.engine == "ta-lib":
            import numpy as np
            return talib.RSI(np.array(closes, dtype=float), period)[-1]
        return _py_calc_rsi(closes, period)  # 纯 Python fallback
    
    def calc_macd(self, closes):
        if self.engine == "ta-lib":
            dif, dea, hist = talib.MACD(np.array(closes, dtype=float))
            return MACDResult(dif=dif[-1], dea=dea[-1], histogram=hist[-1], ...)
        return _py_calc_macd(closes)
    
    # 新增: TA-Lib 独有的能力
    def recognize_candlestick(self, opens, highs, lows, closes):
        """K 线形态识别"""
        if self.engine == "ta-lib":
            return {
                "hammer": talib.CDLHAMMER(opens, highs, lows, closes)[-1],
                "engulfing": talib.CDLENGULFING(opens, highs, lows, closes)[-1],
                "doji": talib.CDLDOJI(opens, highs, lows, closes)[-1],
                "morning_star": talib.CDLMORNINGSTAR(opens, highs, lows, closes)[-1],
            }
        return {}
```

**新增指标清单（TA-Lib 独有，当前不支持）**：

| 类别 | 指标 | 用途 |
|------|------|------|
| 趋势 | ADX | 趋势强度判定（当前缺） |
| 动量 | CCI | 商品通道指数 |
| 动量 | Williams %R | 威廉指标 |
| 动量 | STOCHRSI | RSI 的随机指标 |
| 形态 | CDLHAMMER | 锤子线（反转信号） |
| 形态 | CDLENGULFING | 吞没形态 |
| 形态 | CDLDOJI | 十字星 |
| 形态 | CDLMORNINGSTAR | 晨星形态 |
| 形态 | CDLEVENINGSTAR | 晚星形态 |
| 统计 | LINEARREG | 线性回归 |
| 统计 | STDDEV | 标准差 |

**前置依赖**：
```bash
# macOS
brew install ta-lib
pip install TA-Lib numpy
# Linux
apt-get install ta-lib  # 或源码编译
pip install TA-Lib numpy
```

**影响文件**：
- `app/technical.py` — 策略模式 + TA-Lib 后端
- `app/data_fetcher.py` — AKShare 后端
- `app/models.py` — Quote 扩展 + CandlestickPattern 模型

---

### 方向二：QuantStats 绩效报告（P0）

**目标**：半天内为持仓报表增加专业级量化指标，与 LLM 文本互补。

#### 新增 `app/performance.py`

```python
import quantstats as qs
import pandas as pd
from pathlib import Path

def generate_performance_report(
    holdings: list[Holding],
    klines: dict[str, pd.DataFrame],
    benchmark_code: str = "000300",  # 沪深300
    output_dir: Path = Path("investment_reports"),
) -> dict:
    """
    生成持仓组合绩效报告
    
    Returns:
        - html_path: 完整 HTML 报告路径
        - summary: 关键指标的 Markdown 摘要（用于推送）
    """
    # 1. 构建持仓组合日收益率序列
    portfolio_returns = _build_portfolio_returns(holdings, klines)
    
    # 2. 获取基准收益率
    benchmark_returns = _get_benchmark_returns(benchmark_code)
    
    # 3. QuantStats 生成报告
    report_path = output_dir / f"performance_{datetime.now():%Y%m%d}.html"
    qs.reports.html(
        portfolio_returns,
        benchmark=benchmark_returns,
        output=str(report_path),
        title="持仓组合绩效分析",
    )
    
    # 4. 提取关键指标用于推送（ServerChan 不支持 HTML）
    summary = _extract_key_metrics(portfolio_returns, benchmark_returns)
    
    return {"html_path": report_path, "summary": summary}

def _extract_key_metrics(returns, benchmark):
    """提取关键指标用于 Markdown 推送"""
    return {
        "sharpe": qs.stats.sharpe(returns),
        "sortino": qs.stats.sortino(returns),
        "max_drawdown": qs.stats.max_drawdown(returns),
        "calmar": qs.stats.calmar(returns),
        "win_rate": qs.stats.win_rate(returns),
        "monthly_returns": qs.stats.monthly_returns(returns),
    }
```

#### 集成方式

```
晚报 reporter.py
├── 方向三 / QuantStats: 绩效指标表格部分
│   ├── 本周夏普: 1.25 | 近一月: 0.89
│   ├── 最大回撤: -8.3% | 当前回撤: -2.1%
│   └── 月胜率: 62% | 盈亏比: 1.8
├── AI 分析: "本周期货比率 1.25 属于良好水平..."
```

**注意事项**：
- HTML 报告无法通过 ServerChan 微信推送（纯文本限制）
- 推送时提取关键指标的 Markdown 摘要
- 全文 HTML 报告保存到 `investment_reports/`

---

### 方向三：Backtrader 策略回测（P1）

**目标**：验证当前盯盘逻辑在历史上的有效性，用数据替代"拍脑袋"。

#### 为什么选 Backtrader 而非 VectorBT

当前盯盘逻辑包含**状态依赖**：
- 板块轮动检测需要连续多次扫描的板块均值
- 连续 RSI 趋势需要历史 RSI 序列
- `generate_position_advice()` 依赖多个指标的组合判断

Backtrader 的 `next()` 事件驱动模式更适合这类时序依赖逻辑。VectorBT 适合后期参数优化阶段。

#### 新增 `app/backtest.py`

```python
import backtrader as bt

class CurrentStrategy(bt.Strategy):
    """复现 market-watcher 当前的盯盘逻辑"""
    
    params = (
        ("up_warn", 3.0),    # 涨幅预警阈值
        ("down_warn", -2.5), # 跌幅预警阈值
        ("rsi_period", 14),  # RSI 周期
    )
    
    def __init__(self):
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)
        self.macd = bt.indicators.MACD(self.data.close)
        self.bbands = bt.indicators.BollingerBands(self.data.close)
    
    def next(self):
        # 复现 analyzer.py 的阈值判断逻辑
        change_pct = (self.data.close[0] - self.data.close[-1]) / self.data.close[-1] * 100
        
        if change_pct >= self.params.up_warn:
            self.buy(size=0.1)  # 预警信号触发
        elif change_pct <= self.params.down_warn:
            self.sell(size=0.1)
```

**回测对象**：
1. `thresholds` 的动态调节在历史上是否有超额收益（对比固定阈值）
2. 持仓止盈止损参数组合（当前 -5%/+10% 是否为最优）
3. `generate_position_advice()` 中 RSI<30 加仓/RSI>70 减仓规则
4. 板块轮动检测策略的历史表现

**前置条件**：
- AKShare 提供连续的日K历史数据
- 本地数据缓存层（见第五章）

---

### 方向四：基金分析升级 - xalpha（P1）

**目标**：从"净值 + AI 文字"升级为"持仓穿透 + 组合视角 + QDII 预估"。

#### 核心能力增强

| 当前能力 | 增强后 |
|---------|--------|
| 获取最新净值 | + 持仓穿透到底层股票 |
| LLM 文字分析 | + 组合收益定量汇总 |
| 单独基金分析 | + 基金间持仓重叠度检测 |
| — | + QDII 实时净值估算 |
| — | + 定投/网格历史回测 |

#### 集成方式

```python
import xalpha as xa

def analyze_fund_portfolio(config):
    """穿透分析基金组合的底层股票持仓"""
    
    # 1. 获取基金信息
    fund_infos = [xa.fundinfo(f.code) for f in config.fund_holdings]
    
    # 2. 组合收益汇总
    records = _load_trade_records()  # 从 CSV 账单读取
    portfolio = xa.mul(status=records)
    summary = portfolio.summary()
    
    # 3. 穿透到底层股票
    stock_holdings = portfolio.get_stock_holdings()
    # → ["贵州茅台 5.2%", "宁德时代 3.8%", ...]
    
    # 4. 检测基金间持仓重叠
    overlaps = _detect_overlap(fund_infos)
    
    # 5. 与盯盘联动：底层持仓出现在 watchlist 异动时预警
    watchlist_codes = {item.code for item in config.watch_items}
    at_risk = [s for s in stock_holdings if s["code"] in watchlist_codes]
    if at_risk:
        push_alert(f"基金底层持仓异动: {at_risk}")
    
    return {
        "total_value": summary["total_value"],
        "daily_pnl": summary["daily_pnl"],
        "top_stocks": stock_holdings[:10],
        "stock_watchlist_overlap": at_risk,
    }
```

**重要限制**：xalpha 的 `get_stock_holdings()` 依赖基金季报，数据延迟 15-45 天。需在 LLM prompt 中标明时效性。

---

### 方向五：统计工具链升级（P1）

**目标**：将手写统计函数替换为 scipy/statsmodels，增加协整检验等高级分析。

#### 替换清单

| 当前（手写） | 行数 | 替换方案 | 增益 |
|-------------|------|---------|------|
| `_pearson()` | 25 | `scipy.stats.pearsonr` | +p-value 显著性检验 |
| 写死 Z-score 1.645/2.326 | — | `scipy.stats.norm.ppf` | 任意置信度 |
| `_calc_returns()` | 6 | `pandas.Series.pct_change` | 更健壮 |
| `_parse_float()` | 10 | 保留（安全转换） | — |

#### 新增分析能力

```python
# 协整检验（比相关系数更精准）
from statsmodels.tsa.stattools import coint

def check_cointegration(returns_a: pd.Series, returns_b: pd.Series) -> dict:
    """检测两只股票是否存在统计套利关系"""
    score, pvalue, _ = coint(returns_a, returns_b)
    return {
        "cointegrated": pvalue < 0.05,
        "p_value": round(pvalue, 4),
        "interpretation": "存在长期均衡关系" if pvalue < 0.05 else "不存在协整关系"
    }

# 滚动波动率
def rolling_volatility(returns: pd.Series, window=20):
    """替代当前固定窗口 VaR"""
    return returns.rolling(window).std() * np.sqrt(252)
```

#### 新增持仓分析能力

| 分析 | 方法 | 当前状态 |
|------|------|---------|
| 持仓集中度 | Herfindahl-Hirschman Index | 无 |
| 风格因子暴露 | Fama-French 回归 | 无 |
| 滚动夏普比率 | 60 天窗口 | 无 |
| 下行风险 | Sortino 比率分母 | 当前用手写 VaR |

---

### 方向六：Web 可视化大屏（P2-P3）

**目标**：从纯 CLI 逐步升级为可交互的 Web 看板。

#### 渐进式路径

| Phase | 内容 | 工作量 | 独立可交付 |
|-------|------|--------|-----------|
| Phase 0 | `save_brief()` 输出附带 Plotly HTML 图表 | 2h | ✅ 是 |
| Phase 1 | 本地 Flask/FastAPI 服务 + 现有行情表格 | 1-2 天 | ✅ 是 |
| Phase 2 | Plotly 分时图 + K 线图 + 板块热力图 | 2-3 天 | ✅ 是 |
| Phase 3 | WebSocket 实时推送当前 `_run_once` 结果 | 1-2 天 | 否 |
| Phase 4 | PWA 移动端 + Docker 部署 | 2-3 天 | 否 |

#### Phase 0 实现（2 小时内可交付）

```python
# app/chart.py
import plotly.graph_objects as go
from plotly.subplots import make_subplots

def kline_chart(klines: list[KlineData], code: str, title: str = "") -> str:
    """生成 K 线图 HTML 片段"""
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.7, 0.3],
    )
    
    # K 线 + 均线
    fig.add_trace(go.Candlestick(
        x=[k.date for k in klines],
        open=[k.open for k in klines],
        high=[k.high for k in klines],
        low=[k.low for k in klines],
        close=[k.close for k in klines],
        name=code,
    ), row=1, col=1)
    
    # 成交量
    fig.add_trace(go.Bar(
        x=[k.date for k in klines],
        y=[k.volume for k in klines],
        name="成交量",
    ), row=2, col=1)
    
    return fig.to_html(include_plotlyjs="cdn", full_html=False)
```

```python
# presenter.py 追加
def save_brief_with_charts(quotes, alerts, stats, brief_dir, tech_summaries):
    """现有 save_brief() + 附加交互式图表"""
    md_path = save_brief(quotes, alerts, stats, brief_dir)
    
    html_path = brief_dir / f"charts_{datetime.now():%Y%m%d_%H%M}.html"
    charts = []
    for code in [q.code for q in quotes[:6]]:  # 只前 6 只
        klines = fetch_historical_kline(code, ...)
        if klines:
            charts.append(kline_chart(klines, code))
    
    with open(html_path, "w") as f:
        f.write("<html><body>" + "".join(charts) + "</body></html>")
    
    return md_path, html_path
```

**核心原则**：现有业务逻辑零废弃，`_run_once` 复用，Web 层只加展示。

---

## 四、TradingAgents 多智能体架构借鉴

TradingAgents（75k Stars）是一个多角色 LLM 交易决策框架，其方法论比具体的架构更有借鉴价值。

### 4.1 多角色 prompt 拆分

**当前问题**：`ai_analyzer.py` 是单 prompt 调用 LLM，一个模型同时扮演分析师、策略师、顾问，定位模糊。

**改造目标**：

```
当前:
  一个 LLM 调用 → 一段分析文本

改造后:
  盘中扫描
  ├── 技术面 Agent: "RSI 超买 + MACD 死叉，短期偏空"
  │   (复用 technical.py 的指标数据 + 新增 K 线形态识别)
  ├── 情绪 Agent: "北向资金流出 30 亿，上涨比例 35%"
  │   (复用 analyzer.py 的 sentiment + 北向资金数据)
  ├── 多空观点碰撞
  │   ├── 看多方: "缩量下跌，抛压减弱，可能触底"
  │   └── 看空方: "MACD 死叉 + RSI 下破 50，短期偏空"
  └── 综合输出: 结构化异动解读

  晚报
  ├── 绩效 Agent: "本周夏普 1.25，最大回撤 -3.2%"
  │   (QuantStats 算出数据，LLM 做解读)
  ├── 持仓 Agent: "各持仓的盈亏归因和操作建议"
  │   (基于 holdings 数据 + technical.py 信号)
  ├── 风险 Agent: "VaR -2.1%，集中度警告"
  │   (statsmodels / scipy 算出指标，LLM 做评价)
  └── 综合输出: 结构化晚报
```

#### 4.1.1 实现方案

`app/ai_analyzer.py` 重构为多 Agent 编排器：

```python
class MultiAgentAnalyzer:
    """多角色 AI 分析引擎"""
    
    def __init__(self, config):
        self.llm = get_llm_client(config)
    
    def analyze_intraday(self, quotes, alerts, stats, tech_summaries):
        """盘中分析：多角色协作"""
        # 步骤 1: 各 Agent 并行输出
        tech_report = self._technical_agent(quotes, tech_summaries, alerts)
        sentiment_report = self._sentiment_agent(quotes, stats)
        
        # 步骤 2: 多空辩论
        bull_bear = self._bull_bear_debate(quotes, tech_summaries, alerts)
        
        # 步骤 3: 综合输出（200 字以内）
        return self._synthesize(tech_report, sentiment_report, bull_bear)
    
    def _technical_agent(self, quotes, tech_summaries, alerts):
        prompt = f"""你是技术面分析师。以下数据是当前盘面的技术指标快照。
        
{_format_tech_data(quotes, tech_summaries, alerts)}

请从技术面角度分析（100 字以内）：
1. 当前最重要的技术信号（MACD 位置、RSI 状态、均线排列）
2. K 线形态上是否有值得关注的反转/持续信号
3. 后续需要重点观察的技术位（支撑/压力、均线交叉）

注意：{_tech_limitations()}
"""
        return self.llm.chat(prompt, ...)
    
    def _sentiment_agent(self, quotes, stats):
        prompt = f"""你是市场情绪分析师。当前市场情绪评分 {stats.sentiment.score}/100。
上涨 {stats.up} / 下跌 {stats.down} / 平盘 {stats.flat}。
{'北向资金: ' + str(stats.north_flow) if stats.north_flow else ''}

请从情绪和资金角度分析（80 字以内）：
1. 市场整体情绪的合理性（是否过度乐观/悲观）
2. 资金流向反映了什么信号
"""
        return self.llm.chat(prompt, ...)
```

#### 4.1.2 Prompt 工程：指标局限性说明

TradingAgents 的 Technical Analyst prompt 为每个指标标注了用法和陷阱。借鉴到当前项目：

```python
def _tech_limitations():
    """技术指标的局限性说明，附在 prompt 末尾，防止 LLM 误判"""
    return """
技术面数据说明（阅读时请注意这些陷阱）:
  - RSI: 强趋势中 RSI 可长时间维持在超买/超卖区域，不必然意味着反转
  - MACD 金叉: 零轴下方的金叉可靠性低于零轴上方的金叉
  - 布林带收口: 预示即将变盘，但变盘方向不确定
  - 量价背离: 底部背离不保证立即反弹，可能继续背离
  - 均线死叉: 在震荡市中均线会频繁金叉死叉，信号噪音大
"""
```

### 4.2 决策记忆与反思

**TradingAgents 的做法**：每次运行将决策和结果写入 `trading_memory.md`，下次运行同类标的时注入历史反思。

**当前项目采用**：

```python
# app/memory.py
class DecisionMemory:
    """决策记忆系统：记录 AI 分析的关键预测并跟踪结果"""
    
    MEMORY_FILE = Path("~/.market_watcher/memory/trade_ideas.md").expanduser()
    
    def record_analysis(self, date: str, code: str, analysis: str, 
                        prediction: str, key_levels: dict):
        """记录一次 AI 分析的关键预测"""
        entry = f"""
## [{date}] {code}
**预测**: {prediction}
**关键价位**: {key_levels}
**分析原文摘要**: {analysis[:200]}
**跟踪结果**: pending
---
"""
        self.MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(self.MEMORY_FILE, "a") as f:
            f.write(entry)
    
    def get_recent_predictions(self, code: str, days=30):
        """获取同标的历史预测，用于反思注入"""
        entries = self._parse_memory()
        return [e for e in entries if e["code"] == code 
                and e["date"] >= (datetime.now() - timedelta(days=days))]
    
    def reflect(self, code: str):
        """生成反思：之前的预测是否兑现"""
        predictions = self.get_recent_predictions(code)
        # 将历史 + 实际走势注入晚报 prompt
        return format_reflection(predictions)
```

### 4.3 环境变量覆写模式

TradingAgents 的 `_ENV_OVERRIDES` 模式值得参考：

```python
# 当前: Config 直接读 os.environ
# 改为: 统一的 env → config-key 映射
_ENV_OVERRIDES = {
    "MW_SCAN_INTERVAL": "scan_interval",
    MW_DEEPSEEK_KEY": "deepseek_key",
    "MW_SCT_SENDKEY": "sct_sendkey",
    "MW_LLM_MODEL": "llm_model",
    "MW_TA_LIB_ENABLED": "ta_lib_enabled",
}
```

### 4.4 TradingAgents 不适合照搬的部分

| 特性 | 原因 | 替代方案 |
|------|------|---------|
| LangGraph 编排 | 监控系统不是交易流水线 | 简单 Python 编排 |
| 多轮辩论 | 盘中场景延迟太高 | 单轮多角色各输一段 |
| 多 LLM Provider | 当前 DeepSeek 够用 | 保留扩展点即可 |
| Alpha Vantage/yFinance | AKShare 更适合 A 股 | 不做替换 |

---

## 五、前置条件：历史数据持久化

所有方向（QuantStats 除外）都依赖**连续的历史数据**。当前 JSON 状态文件只存最近几次扫描。

### 5.1 本地缓存层

```python
# 方案: Parquet 格式按日存储，路径按市场+代码+周期组织
data/
├── cache/
│   ├── SH/
│   │   ├── 600036.parquet  # day kline for 招商银行
│   │   └── 510300.parquet  # day kline for 沪深300ETF
│   └── SZ/
│       ├── 000333.parquet
│       └── 159915.parquet
│
├── state/
│   └── market_state.json   # 现有状态文件不变
│
└── memory/
    └── trade_ideas.md      # 决策记忆系统
```

**为何用 Parquet**：
- 列式存储，读取指定字段快
- 自带压缩，磁盘占用小
- pandas 原生支持，Backtrader/QuantStats 直接消费
- 追加写入不重写整个文件

### 5.2 InfluxDB 旁路（可选，为 Grafana 预留）

```python
# app/influx_writer.py (新增，可选)
from influxdb_client import InfluxDBClient, Point

class MetricsWriter:
    """旁路写入时序数据库"""
    
    def write_quote(self, quote: Quote, tech: TechnicalSummary):
        point = Point("quote") \
            .tag("code", quote.code) \
            .tag("name", quote.name) \
            .field("price", quote.price or 0) \
            .field("change_pct", quote.change_pct or 0) \
            .field("volume", quote.volume or 0) \
            .field("rsi", tech.rsi or 0) \
            .field("macd_dif", tech.macd_dif or 0) \
            .time(datetime.utcnow())
        self.client.write_api().write(bucket="market", record=point)
```

---

## 六、实施路线图

### 6.1 推荐实施顺序

```
第 1 周                        第 2 周                        第 3-4 周
┌─────────────────────────┐   ┌─────────────────────────┐   ┌─────────────────────────┐
│ AKShare 数据源 + 缓存层   │ → │ TA-Lib 分批替换          │ → │ Backtrader 回测          │
│ 替换新浪做主源           │   │ 先换 RSI/MACD，逐步扩展  │   │ 验证阈值有效性 + 参数优化│
├─────────────────────────┤   ├─────────────────────────┤   ├─────────────────────────┤
│ QuantStats 绩效报告      │ → │ xalpha 基金穿透          │   │ Web Phase 0-1            │
│ 独立上线                 │   │ + 持仓重叠检测           │   │ + 交互式 K 线图          │
├─────────────────────────┤   ├─────────────────────────┤   │                         │
│ scipy 替换手写统计函数    │   │ statsmodels 协整检验     │   │                         │
├─────────────────────────┤   ├─────────────────────────┤   ├─────────────────────────┤
│ AI 分析多角色拆分         │   │ 决策记忆系统上线         │   │ TradingAgents 式多角色   │
│ (盘中:技术/情绪)         │   │ + Prompt 指标说明       │   │ 晚报 Agent 完整版        │
└─────────────────────────┘   └─────────────────────────┘   └─────────────────────────┘
```

### 6.2 模块改动映射

| 文件 | 改动类型 | 涉及方向 |
|------|---------|---------|
| `app/data_fetcher.py` | 增加 AKShare 后端 | 方向一 |
| `app/technical.py` | 策略模式 + TA-Lib + 形态识别 | 方向一 |
| `app/models.py` | Quote 扩展 + CandlestickPattern | 方向一 |
| **新建 `app/performance.py`** | 全新建模 | 方向二 |
| **新建 `app/backtest.py`** | 全新建模 | 方向三 |
| `app/fund_analyzer.py` | 引入 xalpha 增强 | 方向四 |
| `app/technical.py` statsmodels 段 | 新增 | 方向五 |
| **新建 `app/chart.py`** | 全新建模 | 方向六 |
| `app/presenter.py` | 增加 HTML 输出 | 方向六 |
| **新建 `app/memory.py`** | 全新建模 | TradingAgents 借鉴 |
| `app/ai_analyzer.py` | 重构为多 Agent 编排器 | TradingAgents 借鉴 |
| `app/config.py` | 增加 Env-var 覆写 | TradingAgents 借鉴 |
| **新建 `app/influx_writer.py`** | 可选，Grafana 旁路 | 方向六扩展 |
| `pyproject.toml` | 增加可选依赖分组 | 所有方向 |

### 6.3 依赖引入策略

```toml
# pyproject.toml 的 optional-dependencies

[project.optional-dependencies]
# P0 - 立即需要
core = [
    "akshare>=1.14.0",
    "TA-Lib>=0.4.28",
    "numpy>=1.24.0",
    "scipy>=1.11.0",
]

# P1 - 绩效报告
performance = [
    "quantstats>=0.0.62",
    "pandas>=2.0.0",
    "pyfolio>=0.9.6",
]

# P1 - 回测
backtest = [
    "backtrader>=1.9.78",
    "pandas>=2.0.0",
]

# P1 - 基金分析
fund = [
    "xalpha>=0.11.0",
]

# P1 - 时间序列
timeseries = [
    "statsmodels>=0.14.0",
]

# P2 - 可视化
viz = [
    "plotly>=5.18.0",
    "flask>=3.0.0",      # Phase 1
    "flask-socketio",     # Phase 3
]

# P3 - 时序数据库
influx = [
    "influxdb-client>=1.40.0",
]

# 全量安装
all = [
    "akshare", "TA-Lib", "numpy", "scipy",
    "quantstats", "pandas", "pyfolio",
    "backtrader", "xalpha", "statsmodels",
    "plotly", "flask", "flask-socketio",
    "influxdb-client",
]
```

这样用户可以选择性安装：`pip install .[core]` 或 `pip install .[performance]`。

### 6.4 每阶段可交付成果

| 阶段 | 完成后能做什么 |
|------|---------------|
| **W1 结束** | 数据源切换到 AKShare + 新浪 fallback；持仓报告附带夏普/回撤指标；相关系数带 p-value；AI 分析拆为技术和情绪两个角色 |
| **W2 结束** | TA-Lib 支撑 50+ 指标 + K 线形态识别；基金分析可穿透到底层股票；持仓间检测协整关系；决策记忆系统开始记录和反思 |
| **W3-4 结束** | 可回测当前阈值的历史表现；本地 Web 可看 K 线图；晚报有完整的多 Agent 分析；配置支持环境变量覆写 |

---

## 七、附录：参考资源

### 开源项目

| 项目 | 地址 | Stars | 借鉴点 |
|------|------|-------|--------|
| AKShare | https://github.com/akfamily/akshare | 高 | A 股/期货/宏观数据源 |
| TA-Lib | https://github.com/TA-Lib/ta-lib-python | 行业标准 | 150+ 技术指标 + 形态识别 |
| QuantStats | https://github.com/ranaroussi/quantstats | ~5k | 绩效报告生成 |
| PyFolio | https://github.com/quantopian/pyfolio | ~5k | 风险归因 |
| Backtrader | https://github.com/mementum/backtrader | ~13k | 策略回测 |
| VectorBT | https://github.com/polakowo/vectorbt | ~5k | 极速参数扫描 |
| xalpha | https://github.com/refraction-ray/xalpha | ~1.3k | 基金持仓穿透 |
| TradingAgents | https://github.com/TauricResearch/TradingAgents | 75k | 多角色 LLM + 决策记忆 |
| Pan1Watch | 参考 | — | Web Dashboard 架构 |
| Plotly | https://github.com/plotly/plotly.py | ~16k | 交互式图表 |

### 论文

- TradingAgents: Multi-Agents LLM Financial Trading Framework (arXiv:2412.20138)
- Trading-R1 Technical Report (arXiv:2509.11420)

### 数据源备选

- Tushare（需积分，但 API 统一，适合生产环境）
- yfinance（美股/港股补充）
- Alpha Vantage（美股，TradingAgents 使用）

---

> 本文档于 2026-05-15 综合多轮讨论整理。
> 建议在开始实施每个方向前，先阅读对应模块的源码并更新本文档。
