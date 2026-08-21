"""All intermediates land under one gitignored scratch tree, one dir per work."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".scratch"


def slug(work: str) -> str:
    """`人间隙/04-懦弱` 和 `人间隙-04-懦弱` 都归到同一个目录。"""
    s = work.replace("\\", "/").strip("/").replace("/", "-")
    return re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", s) or "未命名"


def scratch_dir(work: str, sub: str = "") -> Path:
    d = SCRATCH / slug(work)
    if sub:
        d = d / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def frame_name(seconds: float, prefix: str = "f") -> str:
    """按厘秒命名，排序即时间序：3.75s -> f00375.jpg"""
    return f"{prefix}{int(round(seconds * 100)):06d}.jpg"
