"""HTTP 工具函数。"""
from typing import Optional

import requests
from loguru import logger

REQUEST_TIMEOUT = 30
REQUEST_RETRIES = 2


def get_file_content_from_url(file_url: str) -> Optional[str]:
    """从 URL 下载文本内容，失败返回 None。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    for attempt in range(REQUEST_RETRIES + 1):
        try:
            response = requests.get(
                file_url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True
            )
            response.raise_for_status()
            response.encoding = response.apparent_encoding or "utf-8"
            return response.text
        except requests.exceptions.Timeout:
            logger.warning(f"请求超时 ({attempt + 1}/{REQUEST_RETRIES}): {file_url}")
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP 请求失败: {e}")
            return None
        except requests.exceptions.ConnectionError:
            logger.warning(f"连接失败 ({attempt + 1}/{REQUEST_RETRIES}): {file_url}")
        except Exception as e:
            logger.error(f"获取文件异常: {e}")
            return None

    return None
