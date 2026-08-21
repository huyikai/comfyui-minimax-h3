#!/usr/bin/env python3
"""读源片：探时长、找硬切、按秒抽帧、OCR 出屏幕字时间线。

拆段和写稿要的三样东西——切点在哪、每一拍说了什么、黄字什么时候出——都由这里产出。
所有中间物写进 .scratch/<作品>/，结论请自己抄进 video-script 那边的 00-overview.md。

    python tools/read_source.py 原片.mp4 --work 人间隙/05-某作品 --ocr
    python tools/read_source.py 原片.mp4 --work 人间隙/05-某作品 --at 0.4,7,8,20 --ocr
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scratch import frame_name, scratch_dir  # noqa: E402

# 屏幕字常待的几块。整帧 OCR 认不出小字时按块放大重认。
REGIONS = {
    "top": (0.00, 0.00, 1.00, 0.34),
    "bot": (0.08, 0.76, 0.92, 0.98),
    "tl": (0.00, 0.00, 0.42, 0.28),
    "tr": (0.62, 0.00, 1.00, 0.22),
    "title_left": (0.02, 0.10, 0.48, 0.34),
    "title_right": (0.52, 0.10, 0.98, 0.40),
    "low_left": (0.02, 0.70, 0.48, 0.92),
}


def probe(src: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,size:stream=width,height,codec_name,avg_frame_rate",
            "-of", "json", str(src),
        ],
        capture_output=True, text=True, encoding="utf-8",
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr[-500:]}")
    return json.loads(out.stdout)


def scene_cuts(src: Path, threshold: float) -> list[float]:
    out = subprocess.run(
        ["ffmpeg", "-i", str(src), "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    cuts = []
    for line in out.stderr.splitlines():
        if "pts_time:" in line and "showinfo" in line:
            try:
                cuts.append(float(line.split("pts_time:")[1].split()[0]))
            except ValueError:
                pass
    return cuts


def grab(src: Path, seconds: float, dest: Path) -> bool:
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{seconds:.2f}", "-i", str(src), "-frames:v", "1", "-q:v", "3", str(dest)],
        capture_output=True,
    )
    return dest.exists() and dest.stat().st_size > 1000


def parse_times(spec: str, duration: float, step: float) -> list[float]:
    if spec:
        return [float(x) for x in spec.replace("，", ",").split(",") if x.strip()]
    times = [0.3]
    t = step
    while t < duration:
        times.append(round(t, 2))
        t += step
    times.append(round(max(0.0, duration - 0.2), 2))
    return times


def run_ocr(frames: list[Path], regions: bool) -> tuple[list[str], dict]:
    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()

    def read(arr) -> list[dict]:
        result, _ = ocr(arr)
        return [{"text": it[1], "score": round(float(it[2]), 3)} for it in (result or [])]

    lines: list[str] = []
    detail: dict = {}
    for path in frames:
        im = Image.open(path).convert("RGB")
        whole = read(np.array(im))
        texts = [r["text"] for r in whole]
        if regions:
            w, h = im.size
            rec = {}
            for name, (x0, y0, x1, y1) in REGIONS.items():
                crop = im.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
                # 小字放大两倍再认，命中率明显高一截。
                up = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS)
                rec[name] = read(np.array(up))
            detail[path.name] = rec
        seconds = int(path.stem[1:]) / 100
        lines.append(f"{seconds:7.2f}s  " + " | ".join(texts))
        print(lines[-1], flush=True)
    return lines, detail


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", type=Path)
    p.add_argument("--work", required=True, help="作品名，例如 人间隙/05-某作品")
    p.add_argument("--step", type=float, default=1.0, help="按固定间隔抽帧的秒数，默认 1.0")
    p.add_argument("--at", default="", help="只抽这些秒，逗号分隔；给了就忽略 --step")
    p.add_argument("--scene-threshold", type=float, default=0.25)
    p.add_argument("--no-scene", action="store_true", help="跳过硬切检测")
    p.add_argument("--ocr", action="store_true", help="抽完帧顺带 OCR")
    p.add_argument("--regions", action="store_true", help="OCR 时按分区放大重认，慢但认得全")
    args = p.parse_args(argv)

    src = args.video.expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"找不到源片：{src}")

    out = scratch_dir(args.work)
    meta = probe(src)
    (out / "probe.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    duration = float(meta["format"]["duration"])
    stream = meta["streams"][0]
    print(f"[PROBE] {duration:.2f}s {stream['width']}x{stream['height']} {stream['avg_frame_rate']}fps")

    if not args.no_scene:
        cuts = scene_cuts(src, args.scene_threshold)
        (out / "scene.txt").write_text("\n".join(f"{c:.3f}" for c in cuts) + "\n", encoding="utf-8")
        print(f"[SCENE] {len(cuts)} 处硬切 -> {out / 'scene.txt'}")

    frame_dir = scratch_dir(args.work, "frames")
    frames: list[Path] = []
    for t in parse_times(args.at, duration, args.step):
        dest = frame_dir / frame_name(t)
        if grab(src, t, dest):
            frames.append(dest)
        else:
            print(f"[MISS] {t:.2f}s")
    print(f"[FRAMES] {len(frames)} 张 -> {frame_dir}")

    if args.ocr:
        lines, detail = run_ocr(frames, args.regions)
        (out / "ocr.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[OCR] {len(lines)} 行 -> {out / 'ocr.txt'}")
        if detail:
            (out / "ocr-regions.json").write_text(
                json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
