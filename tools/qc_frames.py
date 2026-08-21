#!/usr/bin/env python3
"""成片抽检帧：给一串秒数，批量抽帧到 .scratch/<作品>/qc/ 供读图核对。

    python tools/qc_frames.py 成片.mp4 --work 人间隙/04-懦弱 --at 1.2,4,10.8
    python tools/qc_frames.py 成片.mp4 --work 人间隙/04-懦弱 --every 12 --tail
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scratch import frame_name, scratch_dir  # noqa: E402


def duration_of(src: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise RuntimeError(f"读不出时长：{src}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", type=Path)
    p.add_argument("--work", required=True, help="作品名，例如 人间隙/04-懦弱")
    p.add_argument("--at", default="", help="抽这些秒，逗号分隔")
    p.add_argument("--every", type=float, default=0.0, help="每隔多少秒抽一张")
    p.add_argument("--tail", action="store_true", help="额外抽片尾字卡那一帧")
    p.add_argument("--tag", default="q", help="文件名前缀，用于区分几轮抽检")
    args = p.parse_args(argv)

    src = args.video.expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"找不到视频：{src}")
    total = duration_of(src)

    times: list[float] = [float(x) for x in args.at.replace("，", ",").split(",") if x.strip()]
    if args.every > 0:
        t = args.every
        while t < total:
            times.append(round(t, 2))
            t += args.every
    if args.tail:
        times.append(round(max(0.0, total - 1.0), 2))
    if not times:
        raise SystemExit("给个 --at 或 --every")

    out = scratch_dir(args.work, "qc")
    for t in sorted(set(times)):
        dest = out / frame_name(t, args.tag)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src),
             "-frames:v", "1", "-q:v", "3", str(dest)],
            capture_output=True,
        )
        mark = "" if dest.exists() and dest.stat().st_size > 1000 else "  <- 抽失败"
        print(f"{t:7.2f}s  {dest}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
