from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TushareError(RuntimeError):
    """Tushare 返回错误或响应结构异常。"""


def load_env(path: str | Path = ".env") -> None:
    """加载简单 KEY=VALUE 文件；现有环境变量优先。"""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(slots=True)
class TushareClient:
    token: str
    endpoint: str = "https://api.tushare.pro"
    timeout: float = 30.0

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        """优先使用 Python 默认 CA；macOS 独立 Python 缺失时使用系统 CA。"""
        paths = ssl.get_default_verify_paths()
        if paths.cafile and Path(paths.cafile).is_file():
            return ssl.create_default_context()
        system_ca = Path("/etc/ssl/cert.pem")
        if system_ca.is_file():
            return ssl.create_default_context(cafile=str(system_ca))
        return ssl.create_default_context()

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "TushareClient":
        load_env(env_path)
        token = os.environ.get("TUSHARE_TOKEN", "").strip()
        if not token:
            raise TushareError("未找到 TUSHARE_TOKEN，请检查 .env 或环境变量")
        return cls(token=token)

    def query(
        self,
        api_name: str,
        *,
        fields: list[str] | tuple[str, ...] = (),
        **params: Any,
    ) -> list[dict[str, Any]]:
        payload = json.dumps(
            {
                "api_name": api_name,
                "token": self.token,
                "params": {key: value for key, value in params.items() if value is not None},
                "fields": ",".join(fields),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "APlan/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=self._ssl_context(),
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TushareError(f"Tushare 网络请求失败：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise TushareError("Tushare 返回了非 JSON 响应") from exc

        if result.get("code") != 0:
            raise TushareError(f"Tushare 接口 {api_name} 返回错误：{result.get('msg', '未知错误')}")
        data = result.get("data") or {}
        columns = data.get("fields") or []
        items = data.get("items") or []
        if not isinstance(columns, list) or not isinstance(items, list):
            raise TushareError(f"Tushare 接口 {api_name} 响应结构异常")
        return [dict(zip(columns, item, strict=False)) for item in items]
