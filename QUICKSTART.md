# 快速开始指南

## 安装

### 1. 基础安装（仅运行）

```bash
# 克隆或下载项目后
pip install requests python-dotenv
```

### 2. 开发安装（完整功能）

```bash
# 安装所有依赖包括开发工具
pip install -e ".[dev]"
```

## 配置

### 1. 环境变量

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

编辑 `.env` 文件，添加必要的 API 密钥：

```env
# DeepSeek AI (用于 AI 分析)
DEEPSEEK_API_KEY=sk-xxx

# Server 酱 (用于微信推送)
SCT_SENDKEY=SCTxxx

# 东方财富妙想 (用于基金深度数据，可选)
MX_APIKEY=xxx
```

### 2. 标的列表

编辑 `watchlist.csv`：

```csv
name,code,market,type
沪深 300ETF,510300,SH，宽基 ETF
上证 50ETF,510050,SH，宽基 ETF
```

### 3. 持仓信息（可选）

编辑 `holdings.csv`：

```csv
name,code,market,amount,cost
沪深 300ETF,510300,SH,10000,4.850
```

## 运行

### Windows

双击运行：
```
start_monitoring.bat
```

### Mac / Linux

```bash
chmod +x start_monitoring.sh
./start_monitoring.sh
```

### 手动运行

```bash
python __main__.py
```

## 菜单选项

启动后显示：

```
  1. 🌅 Morning Brief    — 早报（隔夜美股 + 今日策略）
  2. ☀️ Midday Review   — 午评（上午复盘 + 下午预测）
  3. 🌙 Evening Review  — 晚评（全天总结 + 明日策略）
  4. 📊 Monitor Mode    — 盯盘模式（15 分钟扫描一次）
  0. ❌ Exit
```

## 开发命令

### 代码格式化

```bash
# 格式化所有 Python 文件
black .

# 排序 imports
isort .
```

### 代码检查

```bash
# 检查代码质量
ruff check .

# 类型检查
mypy app/
```

### 运行测试

```bash
# 运行所有测试
pytest

# 运行测试并查看覆盖率
pytest --cov=app --cov-report=term-missing
```

### 构建项目

```bash
# 安装为可编辑包
pip install -e .

# 构建分发包
python -m build
```

## 项目结构

```
market_watcher/
├── app/                      # 核心代码
│   ├── __init__.py          # 包入口
│   ├── config.py            # 配置管理
│   ├── models.py            # 数据模型
│   ├── data_fetcher.py      # 数据获取
│   ├── analyzer.py          # 分析引擎
│   ├── ai_analyzer.py       # AI 分析
│   ├── notifier.py          # 通知推送
│   ├── presenter.py         # 展示层
│   ├── helpers.py           # 辅助函数
│   ├── utils.py             # 工具函数
│   └── ...
├── tests/                    # 测试代码
├── watchlist.csv            # 标的列表
├── holdings.csv             # 持仓信息
├── watchlist_config.json    # 主配置文件
├── .env                     # 环境变量（API 密钥）
├── pyproject.toml           # 项目配置
└── __main__.py              # 程序入口
```

## 常见问题

### Q: 如何修改扫描间隔？

A: 编辑 `watchlist_config.json`：

```json
{
  "盯盘设置": {
    "扫描间隔分钟": 10
  }
}
```

### Q: 如何禁用 AI 分析？

A: 编辑 `watchlist_config.json`：

```json
{
  "大模型分析": {
    "启用": false
  }
}
```

### Q: 如何添加新的标的？

A: 编辑 `watchlist.csv`，添加一行：

```csv
name,code,market,type
新标的，000001,SH，行业 ETF
```

### Q: 测试是否正常工作？

A: 运行简单测试：

```bash
python -c "from app.config import Config; from pathlib import Path; c = Config(Path('watchlist_config.json')); print(f'Loaded {len(c.watch_items)} items')"
```

## 获取帮助

- 查看 [README.md](README.md) 了解详细功能
- 查看 [CONTRIBUTING.md](CONTRIBUTING.md) 了解开发流程
- 查看 [OPTIMIZATION_SUMMARY.md](OPTIMIZATION_SUMMARY.md) 了解优化详情
- 在 GitHub 提交 [Issue](https://github.com/yourusername/market-watcher/issues)

## 下一步

- 📝 阅读完整文档
- 🔧 自定义配置满足需求
- 🧪 编写自己的测试用例
- 🚀 贡献代码改进项目
