# -*- coding: utf-8 -*-
"""LLM 调用封装模块。

设计目标：
1. 只依赖 `requests`，直接调用 OpenAI 兼容的 /chat/completions 接口，
   不引入任何重型 Agent 框架，便于答辩时讲清每一步逻辑。
2. 通过环境变量读取配置，绝不硬编码 API Key。
3. 使用 tenacity 对「网络异常 / 限流 / 服务端错误」做指数退避重试。
4. 通过修改 base_url 与 model 即可无缝切换 DeepSeek / 通义千问 Qwen / OpenAI。

环境变量：
    LLM_API_KEY    必填，API Key（也兼容 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY）
    LLM_BASE_URL   选填，默认 https://api.deepseek.com
    LLM_MODEL      选填，默认 deepseek-chat（DeepSeek-V3 系列）
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """LLM 调用相关的统一异常基类。"""


class LLMConfigError(LLMError):
    """配置缺失（例如没有设置 API Key），不可重试。"""


class LLMRequestError(LLMError):
    """可重试的请求错误：网络超时、限流、5xx 等。"""


class LLMClient:
    """极简的 LLM 客户端，只暴露一个 `chat` 方法。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
        max_retries: int = 3,
    ) -> None:
        # 优先级：显式参数 > 通用环境变量 > 厂商专用环境变量 > 默认值
        self.api_key = (
            api_key
            or os.getenv("LLM_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        self.base_url = (
            base_url
            or os.getenv("LLM_BASE_URL")
            or "https://api.deepseek.com"
        ).rstrip("/")
        self.model = model or os.getenv("LLM_MODEL") or "deepseek-chat"
        self.timeout = timeout
        self.max_retries = max(1, max_retries)

        # tenacity 的 Retrying 对象：指数退避，最多 max_retries 次
        # wait_exponential 让重试间隔为 1s、2s、4s...，避免把服务打爆
        self._retrying = Retrying(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(LLMRequestError),
            reraise=True,
            before_sleep=self._log_retry,
        )

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        """发送对话并返回模型回复文本。

        Args:
            messages: OpenAI 格式的消息列表，如
                [{"role": "system", "content": "..."},
                 {"role": "user", "content": "..."}]
            temperature: 采样温度，代码审查建议用较低值以获得稳定输出。
            max_tokens: 单次回复的最大 token 数。

        Returns:
            模型回复的纯文本内容。

        Raises:
            LLMConfigError: 未配置 API Key。
            LLMError: 重试耗尽后仍然失败。
        """
        if not self.api_key:
            raise LLMConfigError(
                "未检测到 API Key。请设置环境变量 LLM_API_KEY（或 DEEPSEEK_API_KEY）。\n"
                "PowerShell 示例：$env:LLM_API_KEY=\"sk-xxxx\"\n"
                "也可使用 --offline 参数在没有 Key 的情况下体验工具链。"
            )

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            # 通过 Retrying 包裹真正发请求的逻辑，实现自动重试
            return self._retrying(self._raw_chat, payload)
        except RetryError as exc:  # 理论上 reraise=True 不会走到这，兜底处理
            raise LLMError("LLM 调用重试耗尽：%s" % exc) from exc
        except LLMRequestError as exc:
            raise LLMError("LLM 调用失败：%s" % exc) from exc

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _raw_chat(self, payload: Dict[str, Any]) -> str:
        """真正发起一次 HTTP 请求（可能被 tenacity 多次调用）。"""
        url = "%s/chat/completions" % self.base_url
        headers = {
            "Authorization": "Bearer %s" % self.api_key,
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                url, headers=headers, json=payload, timeout=self.timeout
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            # 网络问题属于可重试错误
            raise LLMRequestError("网络超时或连接失败：%s" % exc) from exc

        # 限流 / 服务端错误：可重试
        if resp.status_code == 429 or resp.status_code >= 500:
            raise LLMRequestError(
                "HTTP %s：%s" % (resp.status_code, resp.text[:200])
            )
        # 其余 4xx：客户端问题（如 Key 错误、模型不存在），不重试
        if resp.status_code >= 400:
            raise LLMConfigError(
                "HTTP %s：%s" % (resp.status_code, resp.text[:200])
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMRequestError("返回内容不是合法 JSON：%s" % resp.text[:200]) from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMRequestError("响应结构异常：%s" % json.dumps(data)[:200]) from exc

        if not content:
            raise LLMRequestError("模型返回了空内容")
        return content

    @staticmethod
    def _log_retry(retry_state: Any) -> None:
        """重试前打印一条日志，便于观察 Agent 的容错行为。"""
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        logger.warning(
            "LLM 调用失败，准备第 %s 次重试：%s",
            retry_state.attempt_number,
            exc,
        )
