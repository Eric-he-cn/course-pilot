"""出网地址校验：解析域名并挡掉指向内网、回环与元数据端点的目标。

网页抓取与 MCP 调用共用这一份。两条路都是「地址由用户或外部内容给出，请求由服务端发出」，
差一处校验就是一个 SSRF 入口，不该各写各的。
"""
from __future__ import annotations

import ipaddress
import socket


class BlockedAddress(RuntimeError):
    """目标地址不允许访问；code 供上层翻成自己的错误类型。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolved_public_ips(host: str, port: int, *, allow_loopback: bool = False) -> list[str]:
    """解析出的每一个地址都要校验：只看第一条就是 DNS rebinding 的入口。

    allow_loopback 只放开 127.0.0.0/8 与 ::1，给「本机跑着一个服务」这一种情形用。
    私网、链路本地（含 169.254.169.254 这类元数据端点）无论开关如何都拒绝。
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise BlockedAddress("dns_failed", f"无法解析域名 {host}：{error}") from error
    addresses = []
    for info in infos:
        address = info[4][0]
        # 十进制/八进制/短写法（2130706433、0x7f000001、127.1）过不了 ipaddress
        # 但过得了解析器，所以校验只能放在解析之后。
        parsed = ipaddress.ip_address(address)
        if not parsed.is_global and not (allow_loopback and parsed.is_loopback):
            raise BlockedAddress("blocked_address", f"{host} 解析到非公网地址 {address}，已拒绝")
        addresses.append(address)
    if not addresses:
        raise BlockedAddress("dns_failed", f"域名 {host} 没有解析结果")
    return addresses
