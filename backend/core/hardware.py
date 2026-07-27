"""本机能跑多大的检索模型。

实测（M 系列 / 48 GiB，峰值 RSS）：

    只 import torch                    195 MiB
    + sentence-transformers            379 MiB   ← 地板在这里
    + bge-small-zh-v1.5                606 MiB
    + bge-base-zh-v1.5                 613 MiB
    + bge-reranker-base               1037 MiB
    + bge-reranker-v2-m3              1118 MiB

所以「换小模型省内存」这个直觉基本不成立：向量模型小一号只省 7 MiB，重排小一号省 81 MiB，
真正占地方的是 torch 本身。分档的意义在内存很紧的机器上——那时候 1.1 GB 加上浏览器就够呛了，
而且换小模型确实能少下几百 MB 磁盘。

选型不做静默魔法：只有配置写成 auto 才由这里决定，写死模型名就照配置来。
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

# 分档的内存线。低于 SMALL_RAM_GIB 换小模型，低于 MINIMAL_RAM_GIB 连重排一起关掉——
# 那时候留着重排会把每次检索拖到几秒，得不偿失。
SMALL_RAM_GIB = 8.0
MINIMAL_RAM_GIB = 4.0

FULL = {"embedding": "BAAI/bge-base-zh-v1.5", "reranker": "BAAI/bge-reranker-v2-m3"}
# reranker-base 的阈值也标定过（同样 0.3），所以降档不会丢掉「查不到返回空」这个能力。
# 代价是它跨语言不可靠：同一批 chunk，中文问法 0.0095、英文问法 0.9977。
SMALL = {"embedding": "BAAI/bge-small-zh-v1.5", "reranker": "BAAI/bge-reranker-base"}
MINIMAL = {"embedding": "BAAI/bge-small-zh-v1.5", "reranker": ""}


@dataclass(frozen=True)
class Hardware:
    total_ram_gib: float
    cpu_count: int
    accelerator: str  # cuda | mps | cpu
    tier: str         # full | small | minimal

    def as_dict(self) -> dict[str, object]:
        return {
            "total_ram_gib": round(self.total_ram_gib, 1), "cpu_count": self.cpu_count,
            "accelerator": self.accelerator, "tier": self.tier,
        }


def _total_ram_gib() -> float:
    """拿不到就返回 0，调用方按「不确定」处理，不去猜一个数。"""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip()) / (1024 ** 3)
        except Exception:
            return 0.0
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 ** 2)
    except OSError:
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
    except (ValueError, OSError, AttributeError):
        return 0.0


def _accelerator() -> str:
    """只看有没有加速器，不 import 大件之外的东西。torch 缺失时按 cpu 算。"""
    try:
        import torch
    except Exception:
        return "cpu"
    try:
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def probe() -> Hardware:
    ram = _total_ram_gib()
    accelerator = _accelerator()
    # 有独显或统一内存的机器不按内存降档：那类机器跑这两个模型都没问题。
    # 读不到内存（ram == 0）时也按 full 走——宁可让加载失败后自然降级，也不凭猜降档。
    if accelerator != "cpu" or ram == 0.0 or ram >= SMALL_RAM_GIB:
        tier = "full"
    elif ram >= MINIMAL_RAM_GIB:
        tier = "small"
    else:
        tier = "minimal"
    return Hardware(total_ram_gib=ram, cpu_count=os.cpu_count() or 1, accelerator=accelerator, tier=tier)


def resolve(kind: str, configured: str, hardware: Hardware) -> str:
    """把配置里的 auto 换成具体模型名；写死的名字原样返回。"""
    if configured.strip().lower() != "auto":
        return configured
    return {"full": FULL, "small": SMALL, "minimal": MINIMAL}[hardware.tier][kind]
