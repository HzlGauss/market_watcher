# 贡献指南

感谢您对本项目的关注！本文档将帮助您快速上手项目开发。

## 开发环境搭建

### 1. 克隆项目

```bash
git clone https://github.com/yourusername/market-watcher.git
cd market-watcher
```

### 2. 创建虚拟环境

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Mac/Linux
python3 -m venv venv
source venv/bin/activate
```

### 3. 安装开发依赖

```bash
pip install -e ".[dev]"
```

### 4. 配置环境变量

复制 `.env.example` 为 `.env`，并填写必要的 API 密钥：

```bash
cp .env.example .env
```

## 代码规范

本项目使用以下工具保证代码质量：

- **Black**: 代码格式化
- **Ruff**: 代码检查
- **mypy**: 类型检查
- **isort**: import 排序

### 运行代码检查

```bash
# 格式化代码
black .

# 检查代码
ruff check .

# 类型检查
mypy app/

# 排序 imports
isort .
```

### 运行测试

```bash
# 运行所有测试
pytest

# 运行测试并生成覆盖率报告
pytest --cov=app --cov-report=html

# 运行单个测试文件
pytest tests/test_config.py -v
```

## 项目结构

```
market_watcher/
├── app/                      # 核心代码包
│   ├── __init__.py          # 包入口，导出公共 API
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
│   ├── __init__.py
│   ├── test_config.py
│   ├── test_utils.py
│   └── ...
├── pyproject.toml           # 项目配置和依赖
├── README.md                # 项目说明
└── CONTRIBUTING.md          # 贡献指南（本文档）
```

## 开发流程

### 1. 创建功能分支

```bash
git checkout -b feature/your-feature-name
```

### 2. 开发和测试

- 编写代码
- 编写对应的单元测试
- 确保所有测试通过
- 运行代码检查工具

### 3. 提交代码

```bash
git add .
git commit -m "feat: add your feature description"
```

### 4. 推送并创建 Pull Request

```bash
git push origin feature/your-feature-name
```

然后在 GitHub 上创建 Pull Request。

## Commit 信息规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

- `feat`: 新功能
- `fix`: 修复 bug
- `docs`: 文档更新
- `style`: 代码格式调整
- `refactor`: 代码重构
- `test`: 测试相关
- `chore`: 构建/工具相关

示例：
```
feat: add new market analysis feature
fix: resolve data fetching error
docs: update README.md
```

## 代码风格指南

### 类型注解

所有公共函数和类必须添加类型注解：

```python
def calculate_profit(price: float, cost: float) -> float:
    """计算盈亏"""
    return (price - cost) * 100
```

### 文档字符串

所有公共模块、类和函数必须包含文档字符串：

```python
class Config:
    """应用配置类，封装所有可配置项"""
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项
        
        Args:
            key: 配置键
            default: 默认值
        
        Returns:
            配置值
        """
        return self._raw.get(key, default)
```

### 错误处理

使用适当的异常处理，记录详细的错误信息：

```python
try:
    result = risky_operation()
except SpecificError as e:
    log.error(f"Operation failed: {e}")
    raise CustomError(f"Failed to process: {e}") from e
```

## 测试指南

### 编写测试

- 测试文件放在 `tests/` 目录
- 测试类以 `Test` 开头
- 测试函数以 `test_` 开头
- 使用 `pytest` 的 `assert` 语法
- 使用 `tmp_path`  fixture 创建临时文件

示例：

```python
def test_config_load(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"标的列表": []}')
    
    with pytest.raises(ConfigValidationError):
        Config(config_file)
```

### 测试覆盖率

目标覆盖率：80% 以上

```bash
pytest --cov=app --cov-report=term-missing
```

## 发布流程

### 1. 更新版本号

在 `app/__init__.py` 中更新 `__version__`

### 2. 更新 CHANGELOG

记录所有变更

### 3. 创建 Git Tag

```bash
git tag -a v2.0.0 -m "Release version 2.0.0"
git push origin v2.0.0
```

## 问题反馈

遇到问题？请通过以下方式反馈：

1. 查看 [Issues](https://github.com/yourusername/market-watcher/issues)
2. 创建新的 Issue，提供详细信息：
   - 问题描述
   - 复现步骤
   - 环境信息（Python 版本、操作系统）
   - 错误日志

## 许可证

本项目采用 MIT 许可证。
