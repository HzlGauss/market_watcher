"""
Unified HTTP Client - Use requests.Session for connection reuse, support retry mechanism
"""

from __future__ import annotations
import requests
from requests.adapters import HTTPAdapter, Retry
from typing import Optional, Dict, Any, Union
from functools import wraps
from time import sleep
from app.utils import log


class HttpClient:
    """Unified HTTP client with connection pooling and retry mechanism"""

    def __init__(
        self,
        base_url: str = "",
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 10,
        max_retries: int = 2,
    ):
        self._base_url = base_url
        self._timeout = timeout
        self._session = self._create_session(max_retries)
        if headers:
            self._session.headers.update(headers)

    def _create_session(self, max_retries: int) -> requests.Session:
        """Create configured Session with connection pool and retry"""
        session = requests.Session()

        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=1,
            status_forcelist=[429, 456, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )

        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=retry_strategy,
        )

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> Optional[requests.Response]:
        """GET request"""
        full_url = f"{self._base_url}{url}" if self._base_url else url
        try:
            resp = self._session.get(
                full_url,
                params=params,
                headers=headers,
                timeout=timeout or self._timeout,
            )
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            log.warning(f"HTTP GET failed: {url} - {e}")
            return None

    def post(
        self,
        url: str,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> Optional[requests.Response]:
        """POST request"""
        full_url = f"{self._base_url}{url}" if self._base_url else url
        try:
            resp = self._session.post(
                full_url,
                data=data,
                json=json,
                headers=headers,
                timeout=timeout or self._timeout,
            )
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            log.warning(f"HTTP POST failed: {url} - {e}")
            return None

    def close(self) -> None:
        """Close Session"""
        self._session.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

# Default headers
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}

# Sina Finance client
sina_client = HttpClient(
    headers={**DEFAULT_HEADERS, "Referer": "https://finance.sina.com.cn"},
    timeout=10,
    max_retries=2,
)

# East Money client
eastmoney_client = HttpClient(
    headers={**DEFAULT_HEADERS, "Referer": "https://data.eastmoney.com/"},
    timeout=10,
    max_retries=2,
)

# ServerChan push client
serverchan_client = HttpClient(
    base_url="https://sctapi.ftqq.com",
    headers={**DEFAULT_HEADERS},
    timeout=10,
    max_retries=2,
)

# DeepSeek LLM client
llm_client = HttpClient(
    base_url="https://api.deepseek.com",
    headers={**DEFAULT_HEADERS, "Content-Type": "application/json"},
    timeout=30,
    max_retries=2,
)
