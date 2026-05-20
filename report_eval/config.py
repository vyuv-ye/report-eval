"""配置加载模块，支持 config.yaml 和环境变量。"""
import os
from pathlib import Path
from typing import Optional

import yaml

_CONFIG: Optional[dict] = None


def _find_config_file() -> Optional[Path]:
    candidates = [
        Path.cwd() / "config.yaml",
        Path(__file__).parent.parent / "config.yaml",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def load_config() -> dict:
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG

    cfg_path = _find_config_file()
    if cfg_path:
        with open(cfg_path, "r", encoding="utf-8") as f:
            _CONFIG = yaml.safe_load(f) or {}
    else:
        _CONFIG = {}

    return _CONFIG


def get_llm_config() -> dict:
    cfg = load_config()
    llm = cfg.get("llm", {})
    return {
        "api_key": os.environ.get("LLM_API_KEY") or llm.get("api_key", ""),
        "base_url": os.environ.get("LLM_BASE_URL") or llm.get("base_url", "https://ark.cn-beijing.volces.com/api/v3/chat/completions"),
        "model": os.environ.get("LLM_MODEL") or llm.get("model", "ep-20250122140342-xfg2r"),
        "max_tokens": int(os.environ.get("LLM_MAX_TOKENS", 0) or llm.get("max_tokens", 4096)),
        "timeout": int(os.environ.get("LLM_TIMEOUT", 0) or llm.get("timeout", 120)),
    }
