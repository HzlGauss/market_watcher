# Market Watcher — 发展路线规划

> 生成日期：2026-05-15
> 基于对现有代码架构的深度分析 + 开源生态调研 + TradingAgents 架构借鉴

---

## 一、当前项目能力总览

market_watcher 已具备一套完整的盯盘系统核心能力：

| 领域 | 已有能力 |
|------|---------|
| 数据获取 | 新浪财经实时行情 + K线 |
| 技术指标 | 纯 Python 自实现 RSI/MACD/KDJ/MA/布林带/OBV/量比/乖离率/共振信号 |
| 分析引擎 | 情绪评分、动态阈值、板块偏离度、板块轮动检测、持仓盈亏监控 |
| AI 分析 | DeepSeek 集成，早盘/午盘/收盘报告 + 盯盘 AI 研判 |
| 通知推送 | ServerChan 微信推送 |
| 基金分析 | 东方财富净值获取 + DeepSeek AI 评价 |
| 报告系统 | Markdown 简报 + 投资报告 |
| 状态管理 | 跨扫描状态对比、指标趋势追踪 |
| 测试覆盖 | 技术指标单元测试（1518 行） |

---

## 二、开源生态借鉴：6 个提升方向

### 方向一：数据源升级（TA-Lib + AKShare）

**现状问题**：
- `technical.py` 自实现约 1000 行技术指标，存在三个隐患
- 计算精度和边缘 case 处理不如专业库
- 不支持的指标需手写（ADX、CCI、Williams %R）
- K 线数据依赖新浪财经，稳定性一般

**建议方案**——双引擎策略：

```
TA-Lib 负责技术指标计算   → 150+ 指标，C 加速，性能提升 10-100x
AKShare 负责数据获取       → 同时覆盖 A 股/港股/美股/期货/北向资金
```

**`app/technical.py`**—策略模式切换引擎：

```python
class TechnicalEngine:
    def __init__(self, engine: str = "ta-lib"):
        if engine == "ta-lib":
            import talib
    def calc_rsi(self, closes, period=14):
        if self.engine == "ta-lib":
            return talib.RSI(np.array(closes), period)[-1]
        return _py_calc_rsi(closes, period)  # fallback
```

**`app/data_fetcher.py`**—AKShare 备选源：

```python
def fetch_quote_akshare(code: str) -> Quote | None:
    import akshare as ak
    df = ak.stock_zh_a_spot_em()
    row = df[df["代码"] == code]
    ...
```

**提升效果**：

| 维度 | 当前 | 升级后 |
|------|------|--------|
| 指标数量 | ~15 个 | 150+ 个 |
| 计算性能 | O(n) Python | C 级加速 |
| 数据可靠性 | 单源（新浪） | 双源（新浪 + AKShare） |
| 新增指标成本 | 手写实现 | 一行调用 |
| 数据覆盖 | A 股 | A 股 + 期货 + 外汇 + 宏观 |

---

### 方向二：VectorBT 策略回测与参数优化

**现状问题**：
- 核心缺失是回测能力
- 用 1000 行手写指标盯盘，却无法验证盯盘逻辑的历史有效性
- 阈值参数（涨幅预警 3%、跌幅预警 -2.5%）凭经验设置，没有数据支撑

**建议方案**——新建 `app/backtest.py`：

```python
import vectorbt as vbt

def backtest_threshold_strategy(quotes, klines, config) -> dict:
    price_df = pd.DataFrame({
        code: kl["close"] for code, kl in klines.items()
    })
    entries = ...  # 根据 threshold 生成买入信号
    exits = ...    # 根据止盈止损生成卖出信号

    portfolio = vbt.Portfolio.from_signals(
        price_df, entries, exits,
        init_cash=100_000, freq="D"
    )
    return {
        "sharpe": portfolio.sharpe_ratio(),
        "max_drawdown": portfolio.max_drawdown(),
        "total_return": portfolio.total_return(),
        "trade_stats": portfolio.trades().stats(),
    }
```

**提升效果**：
- 从"拍脑袋设阈值"升级为"数据驱动的参数优化"
- 动态阈值的有效性可通过历史数据验证
- 持仓止盈止损策略可找到最优参数组合
- VectorBT 百万级 K 线秒级跑完，不影响盯盘性能

---

### 方向三：QuantStats 专业绩效报告

**现状问题**：
- `reporter.py` 和 `presenter.py` 的简报是 LLM 生成的文本报告
- 缺乏量化绩效指标（夏普比率、最大回撤、胜率等）

**建议方案**——新建 `app/performance.py`：

```python
import quantstats as qs

def generate_performance_report(holdings, klines, output_dir) -> Path:
    portfolio_returns = ...
    report_path = output_dir / f"performance_{datetime.now():%Y%m%d}.html"
    qs.reports.html(
        portfolio_returns,
        benchmark="沪深300",
        output=str(report_path),
        title="持仓组合绩效分析"
    )
    return report_path
```

**报告包含**：

| 指标 | 说明 |
|------|------|
| 夏普比率、Sortino 比率 | 风险调整后收益 |
| 最大回撤及回撤期 | 风险暴露程度 |
| 月/周/日收益分布 | 收益稳定性 |
| 滚动夏普比率 | 策略一致性 |
| 盈亏比、胜率 | 交易质量 |
| Calmar 比率 | 收益/回撤比 |
| 统计显著性检验 | 结果是否可靠 |

**与 AI 报告的关系**：QuantStats 补充量化数据维度，LLM 输出策略解读维度，两者互补。

---

### 方向四：Web 可视化大屏（Plotly/Dash）

**现状问题**：
- 纯控制台 + Markdown 简报
- 无法实时交互
- 适合看盘但不适合复盘分析和团队共享

**建议方案**——渐进式实施：

| 阶段 | 内容 | 工作量 |
|------|------|--------|
| Phase 1 | 本地 Flask 服务 + 现有行情表格 | 1-2 天 |
| Phase 2 | Plotly 分时图 + K 线图 | 2-3 天 |
| Phase 3 | WebSocket 实时推送 | 1-2 天 |
| Phase 4 | 移动端 PWA + Docker 部署 | 2-3 天 |

```
新架构：
Flask/FastAPI 服务
├── WebSocket 实时推送行情（复用 _run_once 逻辑）
├── Plotly 交互图表（分时图、板块轮动热力图、持仓盈亏曲线、情绪得分走势）
└── PWA 移动端（手机查看 / 异动推送）
```

**现有代码零废弃**：`_run_once` 逻辑可完全复用，Web 端只需新增展示层。

---

### 方向五：xalpha 基金穿透分析

**现状问题**：
- `fund_analyzer.py` 只做净值获取 + AI 文字分析
- 缺少组合视角和定量分析

**建议方案**：

```python
import xalpha as xa

def analyze_fund_portfolio(config: Config) -> dict:
    fund_infos = [xa.fundinfo(f.code) for f in config.fund_holdings]
    portfolio = xa.mul(status=records)  # 从 CSV 账单读取
    stock_holdings = portfolio.get_stock_holdings()
    # → 输出：["贵州茅台 5.2%", "宁德时代 3.8%", ...]

    return {
        "total_value": summary["total_value"],
        "daily_pnl": summary["daily_pnl"],
        "top_stocks": stock_holdings[:10],
        "concentration": _calc_concentration(stock_holdings),
        "overlap_with_watchlist": _check_overlap(
            stock_holdings, config.watch_items
        ),
    }
```

**独特价值**：
- 基金穿透：发现不同基金的实际持仓重叠度
- QDII 净值预测：`xa.QDIIPredict("SH501018").get_t0_rate()` 实时估算
- 定投回测：验证定投方案的历史表现
- 与盯盘联动：基金底层持仓股出现在 watchlist 异动时自动预警

---

### 方向六：Grafana 监控大屏

**适用场景**：
- 需要长时间运行、多设备查看的正式部署环境
- 已有时序数据库（InfluxDB / DolphinDB）的基础设施

**方案**：将扫描结果写入时序数据库，Grafana 配置监控面板

---

## 三、TradingAgents 架构借鉴：5 个可复用模式

### 借鉴一：多 Agent 辩论式 AI 分析（最高价值）

**现状问题**：
- `ai_analyzer.py` 一次 LLM 调用生成全部研判
- 没有多视角碰撞、无法反向思考、输出中庸

**TradingAgents 架构**：

```
Analyst Team（4 人并行）     Researcher Team（对抗辩论）       Risk Team（三档辩论）
Market Analyst  ──┐         Bull Researcher ──┐             Aggressive ──┐
Sentiment Analyst ─┤→汇总    ├─→ 经理裁判 →    Neutral    ├─→ PM 裁定
News Analyst     ─┤        Bear Researcher ──┘             Conservative ─┘
Fundamentals     ─┘
```

**建议方案**——新建 `app/ai_debate.py`：

```python
def debate_analysis(quotes, alerts, tech_summaries, config) -> DebateResult:
    llm = get_llm_client(config)

    # Phase 1: 三位分析师并行分析
    tech_report = _technical_analyst(quotes, tech_summaries, llm)
    sentiment_report = _sentiment_analyst(quotes, stats, llm)
    news_report = _news_analyst(alerts, llm)

    # Phase 2: 多/空研究员辩论（2 轮）
    bull_case = _bull_researcher(tech_report, sentiment_report, news_report, llm)
    bear_case = _bear_researcher(tech_report, sentiment_report, news_report, llm)

    for round in range(2):
        bull_rebuttal = _bull_rebut(bear_case, debate_history, llm)
        bear_rebuttal = _bear_rebut(bull_case, debate_history, llm)

    # Phase 3: 首席策略师裁定
    final_verdict = _chief_strategist(
        tech_report, sentiment_report, news_report,
        bull_case, bear_case, debate_history, llm
    )

    return DebateResult(
        bull_case=bull_case, bear_case=bear_case,
        debate_rounds=debate_history, final_verdict=final_verdict,
    )
```

**角色设计**：

| 角色 | Prompt 原则 | 核心指令 |
|------|-------------|---------|
| 技术分析师 | 只看 K 线和技术指标 | "不要考虑消息面" |
| 情绪分析师 | 只看市场情绪数据 | "评估涨跌比、情绪评分、板块轮动" |
| 多头研究员 | 刻意寻找看多理由 | "即使市场看起来很差" |
| 空头研究员 | 刻意寻找看空理由 | "即使市场看起来很好" |
| 首席策略师 | 综合裁定 | "给出最终研判和操作建议" |

**提升效果**：
- 避免"中庸答案"：多/空对抗迫使 LLM 思考极端场景
- 质量可验证：报告包含完整的多空博弈过程
- 部署成本不变：同一个 LLM，调用次数从 1 次变为 6-8 次

---

### 借鉴二：决策记忆与反思系统

**现状问题**：
- `state_manager.py` 只存技术指标快照
- 不存决策本身，不会反思"之前的判断对不对"

**TradingAgents 设计**：

```
TradingMemoryLog（追加式 Markdown 文件）
Phase A（扫描时）: store_decision(ticker, date, decision) → 写 [pending] 记录
Phase B（下次扫描）: batch_update_with_outcomes() → 获取实际涨跌幅，LLM 反思
Phase C（决策时）: get_past_context(ticker) → 注入历史到新 prompt
```

**建议方案**——扩展 `app/state_manager.py`：

```python
DECISION_LOG = BASE_DIR / "state" / "decision_log.md"

def store_alert_decision(code, name, alert_messages, decision):
    """记录本次异动判断（Phase A）"""
    entry = f"[{datetime.now():%Y-%m-%d %H:%M} | {code} | {decision} | pending]\n...\n"
    with open(DECISION_LOG, "a") as f:
        f.write(entry)

def resolve_alert_decisions(quotes):
    """解析 pending：判断上次的异动判断是否准确（Phase B）"""
    for entry in _parse_decision_log():
        if not entry["pending"]:
            continue
        quote = next((q for q in quotes if q.code == entry["code"]), None)
        reflection = _reflect_on_decision(entry["decision"], quote.change_pct, llm)
        _update_entry(entry, quote.change_pct, reflection)

def get_decision_context(code) -> str:
    """获取历史决策上下文（Phase C）"""
    entries = [e for e in _parse_decision_log() if e["code"] == code and not e["pending"]]
    return "\n".join(f"- [{e['date']}] 判断:{e['decision']} 实际:{e['outcome']:+.2f}% 反思:{e['reflection']}"
                     for e in entries[-5:])
```

**提升效果**：

| 指标 | 当前 | 升级后 |
|------|------|--------|
| 决策可追溯 | 无记录 | 完整的判断→结果→反思链 |
| 自我进化 | 每次独立判断 | LLM 参考"上次错了"从而提高准确率 |
| 数据驱动 | 只看指标变化 | 叠加历史判断准确率作为权重 |

---

### 借鉴三：三档风险画像辩论

**现状问题**：
- `notifier.py` 和 `analyzer.py` 的风险判断一刀切
- `stop_loss_pct = -5%` 适合所有人？激进/保守交易者应不同

**建议方案**——新建 `app/risk_debate.py`：

```python
def assess_risk(quote, tech, holding, config) -> RiskVerdict:
    """三档风险评估：激进/中性/保守各自判断 → 综合建议"""
    llm = get_llm_client(config)

    aggressive = _aggressive_assessment(quote, tech, holding, llm)
    neutral = _neutral_assessment(quote, tech, holding, llm)
    conservative = _conservative_assessment(quote, tech, holding, llm)

    verdict = _synthesize_risk(aggressive, neutral, conservative,
                                stop_loss=config.stop_loss_pct,
                                take_profit=config.take_profit_pct)
    return verdict
```

---

### 借鉴四：结构化 ScanState 管道

**现状问题**：
- `_run_once()` 是长过程式函数，数据流不透明
- 新手难理解"行情数据从哪来到哪去"

**建议方案**——新建 `app/pipeline.py`：

```python
@dataclass
class ScanState:
    config: Config
    holdings: list[Holding]
    watch_items: list[WatchItem]
    quotes: list[Quote] = field(default_factory=list)
    tech_summaries: dict[str, TechnicalSummary] = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)
    stats: Optional[AnalysisStats] = None
    trend_map: dict[str, TrendInfo] = field(default_factory=dict)
    ai_result: Optional[DebateResult] = None
    brief_path: Optional[Path] = None
    push_ok: bool = False

def run_pipeline(config, north_fetcher) -> ScanState:
    state = ScanState(config=config, holdings=...)
    state.quotes = _fetch_data(state)           # Phase 1
    state.tech_summaries = _calc_tech(state)    # Phase 1b
    state.alerts, state.stats = _analyze(state) # Phase 2
    state.ai_result = _debate_analyze(state)    # Phase 3
    _output_results(state)                      # Phase 4
    return state
```

**好处**：每个阶段可独立测试、数据流可视化、便于扩展。

---

### 借鉴五：LLM 自主工具调用

**现状问题**：
- 当前把所有数据预处理好塞进 prompt
- LLM 只能"读"不能"查"

**建议方案**：

```python
tools = [
    Tool(name="get_technical_indicator", func=lambda code, ind: _lookup_tech(code, ind)),
    Tool(name="get_historical_performance", func=lambda code, days: _calc_performance(code, days)),
    Tool(name="get_sector_info", func=lambda code: _get_sector(code)),
    Tool(name="compare_with_index", func=lambda code, idx: _calc_alpha(code, idx)),
]
```

让 LLM 可以：发现 RSI 超买 → 自己查"上次超买后怎么走"；发现异动 → 自己查"是否是板块效应"。

---

## 四、综合优先级路线图

### 优先级矩阵

| 优先级 | 项目 | 价值 | 工作量 | 分类 |
|--------|------|------|--------|------|
| 🥇 P0 | 多 Agent 辩论式 AI | 🔥🔥🔥 | 2-3 天 | TradingAgents 借鉴 |
| 🥇 P0 | 决策记忆与反思 | 🔥🔥🔥 | 1-2 天 | TradingAgents 借鉴 |
| 🥇 P0 | TA-Lib + AKShare 数据源升级 | 🔥🔥🔥 | 2-3 天 | 开源生态 |
| 🥈 P1 | QuantStats 绩效报告 | 🔥🔥 | 0.5 天 | 开源生态 |
| 🥈 P1 | 三档风险辩论 | 🔥🔥 | 1-2 天 | TradingAgents 借鉴 |
| 🥈 P1 | 结构化 ScanState | 🔥🔥 | 2-3 天 | TradingAgents 借鉴 |
| 🥈 P1 | VectorBT 策略回测 | 🔥🔥 | 3-5 天 | 开源生态 |
| 🥉 P2 | xalpha 基金穿透 | 🔥 | 1-2 天 | 开源生态 |
| 🥉 P2 | LLM 自主工具调用 | 🔥 | 3-5 天 | TradingAgents 借鉴 |
| 🥉 P2 | Web Dashboard | 🔥 | 分批实施 | 开源生态 |

### 推荐起步路径

```
本周（P0）
├── QuantStats 绩效报告（半天，见效最快）
├── 决策记忆与反思系统（1-2 天）
└── TA-Lib + AKShare 数据源升级（2-3 天）

下月（P1）
├── 多 Agent 辩论式 AI 分析（2-3 天）
├── 三档风险辩论（1-2 天）
└── VectorBT 策略回测（3-5 天）

后续（P2）
├── 结构化 ScanState 管道（2-3 天）
├── xalpha 基金穿透（1-2 天）
├── LLM 自主工具调用（3-5 天）
└── Web Dashboard（分批实施）
```

---

## 五、关键设计原则

1. **增量演进，不重写**：所有改进基于现有代码增量修改，保持 `_run_once` 主循环稳定
2. **轻量优先**：优先纯 Python 方案，避免引入重型框架依赖
3. **可测试**：每个新模块都有对应的 `tests/` 单元测试
4. **可配置**：新增功能通过 `watchlist_config.json` 开关控制，默认关闭
5. **向下兼容**：改进不破坏现有的控制台输出和 Markdown 简报

---

*本文档综合了 2026-05-15 的开源生态调研与 TradingAgents 架构分析生成。*
