# 📈 Stock Market Monitoring Radar v2

Modular refactored version. Interactive menu for generating investment reports or starting monitoring scans.

## Quick Start

### Windows
```bash
Double click start_monitoring.bat
```

### Mac / Linux
```bash
chmod +x start_monitoring.sh
./start_monitoring.sh
```

### Manual
```bash
cd market_watcher
python __main__.py
```

### Menu After Startup

After starting, the program shows an interactive menu:

```
  1. 🌅 Morning Brief    — Overnight US stocks + today's strategy, generate anytime
  2. ☀️ Midday Review   — Morning review + afternoon prediction
  3. 🌙 Evening Review  — Full day summary + next day strategy
  4. 📊 Monitor Mode    — Auto-scan every 15 minutes, push alerts to WeChat
  0. ❌ Exit
```

Select 1/2/3 → Generate report immediately and push to WeChat, then return to menu
Select 4 → Enter monitoring loop, press Ctrl+C to return to menu

## 功能特性

- ✅ **14个监控标的** — 宽基ETF + 行业ETF + 港股ETF + 指数
- ✅ **实时行情** — 新浪财经接口
- ✅ **智能异动分析** — 涨跌幅/量价关系/振幅/趋势翻转/板块异动
- ✅ **动态阈值** — 根据市场情绪自动调整报警线
- ✅ **DeepSeek AI研判** — 盘面特征+异动解读+操作参考
- ✅ **微信推送** — 异动时自动推送到手机
- ✅ **全市场情绪评分** — 结合指数涨跌校准
- ✅ **北向资金追踪** — 沪股通+深股通净流入
- ✅ **Markdown简报** — 每15分钟自动生成
- ✅ **三大投资报告** — 早报(08:25)、午评(11:35)、晚评(16:00)，含隔夜美股分析

## 项目结构

```
market_watcher/
├── __main__.py              ← Entry + scheduling loop
├── app/                     ← Core code package
│   ├── config.py            ← Config loading (type-safe)
│   ├── models.py            ← Data classes (dataclass)
│   ├── data_fetcher.py      ← Data fetching (Sina + North Flow)
│   ├── analyzer.py          ← Analysis engine (sentiment/thresholds/alerts)
│   ├── ai_analyzer.py       ← DeepSeek AI analysis
│   ├── notifier.py          ← WeChat push notifications
│   ├── presenter.py         ← Output rendering (console + briefs)
│   └── utils.py             ← Utility functions (logging/formatting)
├── watchlist_config.json    ← Configurable watch list
├── .env                     ← API keys
├── state/                   ← State data (auto-generated)
├── monitoring_briefs/       ← Brief files (auto-generated)
├── start_monitoring.bat      ← Windows startup script
└── start_monitoring.sh       ← Mac/Linux startup script
```

## 配置说明

### watchlist_config.json

```json
{
  "标的列表": [
    {"name": "沪深300ETF", "code": "510300", "market": "SH", "type": "宽基ETF"}
  ],
  "提醒阈值": {"涨幅预警": 4.0, "跌幅预警": -3.0},
  "动态阈值": {"启用": true, "情绪调整强度": 1.5},
  "大模型分析": {"启用": true, "分析时机": "仅异动时"},
  "推送通知": {"启用": true, "方式": "server酱"},
  "北向资金": {"启用": true, "更新间隔分钟": 30},
  "投资报告": {
    "早报": {"启用": true, "推送时间": "08:25"},
    "午评": {"启用": true, "推送时间": "11:35"},
    "晚评": {"启用": true, "推送时间": "16:00"}
  }
}
```

### .env（API密钥）

```env
DEEPSEEK_API_KEY=sk-xxx     # DeepSeek AI
SCT_SENDKEY=SCTxxx           # Server酱 微信推送
```

## 数据来源

| 数据 | 来源 |
|------|------|
| 实时行情 | 新浪财经 API |
| AI分析 | DeepSeek API |
| 北向资金 | 东方财富 API |

## 架构说明

v2 重构将单体1129行拆分为11个独立模块：

| 模块 | 行数 | 职责 |
|------|:----:|------|
| `__main__.py` | 301 | 入口 + 三报调度 + 盯盘循环 |
| `reporter.py` | 418 | 早报/午评/晚评 投资报告 |
| `analyzer.py` | 248 | 情绪/阈值/异动分析 |
| `presenter.py` | 253 | 控制台+简报输出 |
| `data_fetcher.py` | 205 | 行情+北向+全球市场获取 |
| `config.py` | 140 | 类型安全配置加载 |
| `ai_analyzer.py` | 141 | DeepSeek AI分析 |
| `models.py` | 103 | dataclass数据模型 |
| `utils.py` | 81 | 日志/格式化工具 |
| `notifier.py` | 75 | Server酱推送 |
