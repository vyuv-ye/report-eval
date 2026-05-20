"""LLM 客户端，通过 OpenAI 兼容接口调用大模型。"""
import json
import time
from typing import Optional

import requests
from loguru import logger

from report_eval.config import get_llm_config


class LLMClient:
    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        cfg = get_llm_config()
        self.api_key = api_key or cfg["api_key"]
        self.base_url = base_url or cfg["base_url"]
        self.model = model or cfg["model"]
        self._default_timeout = cfg["timeout"]
        self._default_max_tokens = cfg["max_tokens"]

    def chat(
        self,
        system_prompt: str,
        user_query: str,
        max_tokens: int = None,
        timeout: int = None,
        retries: int = 2,
    ) -> Optional[str]:
        """
        发送对话请求，返回模型回复文本。失败返回 None。
        """
        max_tokens = max_tokens or self._default_max_tokens
        timeout = timeout or self._default_timeout

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_query},
            ],
            "temperature": 0.8,
            "max_tokens": max_tokens,
        }

        for attempt in range(retries):
            try:
                resp = requests.post(
                    self.base_url,
                    data=json.dumps(payload),
                    headers=headers,
                    timeout=timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except Exception as e:
                logger.warning(f"LLM 请求失败 (attempt {attempt + 1}/{retries}): {e}")
                if attempt < retries - 1:
                    time.sleep(3)

        return None


_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
