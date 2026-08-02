"""联网检索与网页抓取适配器（SerpAPI + 带 SSRF 防护的抓取）。"""
from __future__ import annotations

import html
import re

import httpx

from contracts.web import WebAccessError, WebPage, WebResult, WebSearchOutcome
from core.netguard import BlockedAddress, resolved_public_ips

_SEARCH_ENDPOINT = "https://serpapi.com/search"
# 抓回的正文进上下文，必须有上界。
FETCH_MAX_BYTES = 1024 * 1024
TEXT_MAX_CHARS = 12_000

_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_BLANKS = re.compile(r"\n{3,}")


def _resolved_ips(host: str, port: int) -> list[str]:
    """地址校验与 MCP 共用 core.netguard；网页抓取一律只连公网。"""
    try:
        return resolved_public_ips(host, port)
    except BlockedAddress as error:
        raise WebAccessError(error.code, str(error)) from error


def _html_to_text(body: str) -> tuple[str, str]:
    title_match = _TITLE.search(body)
    title = html.unescape(_TAG.sub("", title_match.group(1))).strip()[:200] if title_match else ""
    text = _SCRIPT_STYLE.sub(" ", body)
    text = _TAG.sub("\n", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return title, _BLANKS.sub("\n\n", text).strip()


class HttpWebAccess:
    """SerpAPI 检索 + 抓取。抓取连的是已校验过的 IP，域名只用于 Host 与 SNI。"""

    def __init__(
        self, *, api_key: str, connect_timeout_seconds: float = 10, total_timeout_seconds: float = 20,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = httpx.Timeout(total_timeout_seconds, connect=connect_timeout_seconds)
        self._client = client
        self._last_error: str | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout, follow_redirects=False)
        return self._client

    def health(self) -> dict[str, object]:
        return {"configured": bool(self._api_key), "provider": "serpapi", "last_error_code": self._last_error}

    def search(self, *, query: str, limit: int = 5) -> WebSearchOutcome:
        if not self._api_key:
            raise WebAccessError("not_configured", "联网检索未配置（缺 RESEARCH_SERPAPI_API_KEY）")
        try:
            response = self._http().get(
                _SEARCH_ENDPOINT,
                params={"q": query, "api_key": self._api_key, "num": max(1, min(limit, 10)), "hl": "zh-cn"},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            self._last_error = "search_failed"
            raise WebAccessError("search_failed", f"联网检索失败：{type(error).__name__}") from error
        results = [
            WebResult(
                title=str(item.get("title") or "").strip()[:200],
                url=str(item.get("link") or "").strip(),
                snippet=" ".join(str(item.get("snippet") or "").split())[:400],
            )
            for item in (payload.get("organic_results") or [])[:limit]
        ]
        self._last_error = None
        return WebSearchOutcome(query=query, results=results)

    def fetch(self, *, url: str) -> WebPage:
        parsed = httpx.URL(url)
        if parsed.scheme not in {"http", "https"}:
            raise WebAccessError("unsupported_scheme", f"只支持 http/https，收到 {parsed.scheme or '空'}")
        host = parsed.host
        if not host:
            raise WebAccessError("invalid_url", "URL 缺少主机名")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        # 校验完不能再把域名交给 httpx（那是第二次解析，中间的窗口就是 rebinding）：
        # 直接连已校验过的 IP，域名只用于 Host 头与 TLS SNI。
        # 逐个尝试而不是盲取第一条——解析结果里 IPv6 常排在前面，未必可路由。
        extensions = {"sni_hostname": host} if parsed.scheme == "https" else {}
        last_error: Exception | None = None
        response = None
        for address in _resolved_ips(host, port):
            literal = f"[{address}]" if ":" in address else address
            try:
                response = self._http().get(
                    parsed.copy_with(host=literal, port=parsed.port),
                    headers={"Host": host, "Accept": "text/html,text/plain;q=0.9"}, extensions=extensions,
                )
                break
            except httpx.HTTPError as error:
                last_error = error
        if response is None:
            raise WebAccessError("fetch_failed", f"抓取失败：{type(last_error).__name__}") from last_error
        if response.is_redirect:
            location = response.headers.get("location", "")
            # 不自动跟随：重定向目标交回模型，它再调一次会重新走完整套校验。
            return WebPage(url=url, title="", text="", redirect_to=str(parsed.join(location)) if location else None)
        if response.status_code >= 400:
            raise WebAccessError("http_error", f"目标返回 {response.status_code}")
        body = response.content[:FETCH_MAX_BYTES].decode(response.encoding or "utf-8", errors="replace")
        content_type = response.headers.get("content-type", "")
        if "html" in content_type:
            title, text = _html_to_text(body)
        else:
            title, text = "", body
        return WebPage(url=url, title=title, text=text[:TEXT_MAX_CHARS], truncated=len(text) > TEXT_MAX_CHARS)
