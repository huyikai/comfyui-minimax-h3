#!/usr/bin/env python3
"""抽检帧：批量抽帧到 .scratch/<作品>/qc/ 供读图核对。

合剪层面，给一串秒数：

    python tools/qc_frames.py 成片.mp4 --work 人间隙/04-懦弱 --at 1.2,4,10.8
    python tools/qc_frames.py 成片.mp4 --work 人间隙/04-懦弱 --every 12 --tail

分片层面，每条 clip 拼一张横条，一条一图地看：

    python tools/qc_frames.py --dir ~/Downloads/人间隙-04-懦弱 --work 人间隙/04-懦弱
"""

from __future__ import annotations

import argparse
import shutil
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


def grab(src: Path, t: float, dest: Path) -> bool:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src),
         "-frames:v", "1", "-q:v", "3", str(dest)],
        capture_output=True,
    )
    return dest.exists() and dest.stat().st_size > 1000


def strip_clip(src: Path, out_dir: Path, per_clip: int) -> Path | None:
    """一条 clip 抽 per_clip 帧横排成一张，避开头尾那几帧。"""
    from PIL import Image

    total = duration_of(src)
    times = [round(total * (i + 0.5) / per_clip, 2) for i in range(per_clip)]
    tmp = out_dir / f".{src.stem}"
    tmp.mkdir(parents=True, exist_ok=True)
    shots = []
    for i, t in enumerate(times):
        shot = tmp / f"{i}.jpg"
        if grab(src, t, shot):
            shots.append(shot)
    if not shots:
        shutil.rmtree(tmp, ignore_errors=True)
        return None

    images = [Image.open(f) for f in shots]
    height = max(im.height for im in images)
    canvas = Image.new("RGB", (sum(im.width for im in images), height))
    x = 0
    for im in images:
        canvas.paste(im, (x, 0))
        x += im.width
        im.close()
    dest = out_dir / f"{src.stem}.jpg"
    canvas.save(dest, quality=85)
    shutil.rmtree(tmp, ignore_errors=True)
    return dest


def run_dir(clip_dir: Path, work: str, per_clip: int) -> int:
    videos = sorted(v for v in clip_dir.glob("*.mp4") if "成片" not in v.stem)
    if not videos:
        raise SystemExit(f"目录里没有 mp4：{clip_dir}")
    out = scratch_dir(work, "qc/clips")
    for v in videos:
        dest = strip_clip(v, out, per_clip)
        print(f"{v.stem:28} {dest or '抽失败'}")
    print(f"\n{len(videos)} 条 -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", type=Path, nargs="?")
    p.add_argument("--work", required=True, help="作品名，例如 人间隙/04-懦弱")
    p.add_argument("--dir", type=Path, help="分片目录：每条 clip 拼一张横条")
    p.add_argument("--per-clip", type=int, default=3, help="--dir 时每条 clip 抽几帧，默认 3")
    p.add_argument("--at", default="", help="抽这些秒，逗号分隔")
    p.add_argument("--every", type=float, default=0.0, help="每隔多少秒抽一张")
    p.add_argument("--tail", action="store_true", help="额外抽片尾字卡那一帧")
    p.add_argument("--tag", default="q", help="文件名前缀，用于区分几轮抽检")
    args = p.parse_args(argv)

    if args.dir:
        return run_dir(args.dir.expanduser().resolve(), args.work, max(1, args.per_clip))
    if not args.video:
        raise SystemExit("给个视频，或用 --dir 逐段抽")

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
        mark = "" if grab(src, t, dest) else "  <- 抽失败"
        print(f"{t:7.2f}s  {dest}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
