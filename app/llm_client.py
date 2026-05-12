"""
统一LLM客户端 —— 封装DeepSeek API调用，消除重复代码
"""

from __future__ import annotations
import os
from typing import Optional, Dict, Any, List
from enum import Enum

from app.http_client import llm_client as http_client
from app.config import Config
from app.utils import log


class LLMModel(Enum):
    """支持的LLM模型"""
    DEEPSEEK_CHAT = "deepseek-chat"
    DEEPSEEK_CODER = "deepseek-coder"


class LLMClient:
    """统一LLM客户端"""

    def __init__(self, api_key: Optional[str] = None, model: str = "deepseek-chat"):
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self._model = model
        self._enabled = bool(self._api_key)

    @property
    def enabled(self) -> bool:
        """是否启用"""
        return self._enabled

    def chat(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 500,
        temperature: float = 0.3,
        stop: Optional[List[str]] = None,
        timeout: int = 60,
    ) -> Optional[str]:
        """
        调用LLM进行对话

        Args:
            prompt: 用户提示词
            system_prompt: 系统提示词（角色设定）
            max_tokens: 最大生成token数
            temperature: 温度系数，越低越确定性
            stop: 停止词列表
            timeout: 请求超时时间（秒）

        Returns:
            LLM返回的内容，失败返回None
        """
        if not self._enabled:
            log.warning("LLM未启用（缺少API Key）")
            return None

        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if stop:
            payload["stop"] = stop

        try:
            resp = http_client.post("/chat/completions", json=payload, headers=headers, timeout=timeout)
            if resp and resp.status_code == 200:
                result = resp.json()
                content = result["choices"][0]["message"]["content"]
                return content.strip() if content else None
            else:
                status = resp.status_code if resp else "None"
                log.warning(f"LLM请求失败 (HTTP {status})")
                return None
        except Exception as e:
            log.warning(f"LLM调用异常: {e}")
            return None


# 全局默认LLM客户端实例
_default_llm_client: Optional[LLMClient] = None


def get_llm_client(config: Optional[Config] = None) -> LLMClient:
    """
    获取全局LLM客户端实例

    Args:
        config: 配置对象，可选，用于获取模型名称

    Returns:
        LLMClient实例
    """
    global _default_llm_client

    if _default_llm_client is None:
        model = config.llm_model if config else "deepseek-chat"
        _default_llm_client = LLMClient(model=model)

    return _default_llm_client


# 预设的系统提示词
SYSTEM_PROMPTS = {
    "analyst": "你是一位专业的A股市场实时分析师。请基于提供的实时盯盘数据，给出专业、简洁的盘面研判。语言简洁专业，基于数据说话，不做无依据预测。",

    "strategist": "你是一位拥有20年经验的A股首席策略分析师。你的报告以专业、务实、可执行著称。每次分析不超过300字，语言精炼，有数据支撑，有操作建议。不模棱两可，不堆砌术语，让普通投资者也能看懂。",

    "fund_expert": "你是一位拥有15年经验的基金研究专家，曾任晨星（Morningstar）高级分析师。你熟悉中国公募基金行业的所有明星基金经理。你的分析风格：客观、专业、数据驱动。你擅长将复杂的基金数据转化为极具决策价值的可视化Markdown报告，能够准确识别市场风格与基金持仓的匹配度。",
}
