#!/usr/bin/env python3
"""开跑前的静态检查：脚本能不能被生成程序读懂、有没有会烧字幕的写法。

用 run_video_scripts 自己的解析函数，所以这里过了，那边就一定读得出来。

    python tools/precheck.py --work 人间隙/04-懦弱
    python tools/precheck.py --work 人间隙/04-懦弱 --strict   # 警告也算不过
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_video_scripts as rvs  # noqa: E402

# H3 会把引号里的字画进画面，这些词还会直接引出字幕。
BANNED = ("subtitle", "caption", "burned-in", "chinese text overlay", "text overlay")
# 占字数但不涨表演的空词。
EMPTY_WORDS = ("emotional", "cinematic", "beautiful", "expressive")
DIALOGUE_TAG = re.compile(r"<d>.*?</d>", re.S)
CJK = re.compile(r"[\u4e00-\u9fff]")


def check_prompt(prompt: str) -> list[str]:
    bad: list[str] = []
    low = prompt.lower()
    for word in BANNED:
        if word in low:
            bad.append(f"英文里出现 `{word}`，会引出烧死的字幕")
    for word in EMPTY_WORDS:
        if re.search(rf"\b{word}\b", low):
            bad.append(f"英文里出现空词 `{word}`")
    stripped = DIALOGUE_TAG.sub("", prompt)
    if CJK.search(stripped):
        loose = CJK.findall(stripped)
        bad.append(f"`<d>` 之外出现汉字 {''.join(loose[:12])}，会被画进画面")
    if "identity lock" not in low:
        bad.append("没写 Identity lock，跨条容易换脸")
    return bad


def check_segment(seg_dir: Path) -> list[str]:
    ov = seg_dir / "00-overview.md"
    if not ov.exists():
        return [f"缺 {ov.name}，出不了字幕和黄字"]
    text = ov.read_text(encoding="utf-8")
    warn: list[str] = []
    if "屏幕字" not in text:
        warn.append("段总表没有「屏幕字」表，黄字标题取不到")
    if not re.search(r"`clip-\d+\.md`[：:]\s*(过去|现在)", text):
        warn.append("段总表没标每条的过去/现在，黄字不知道该在哪条出")
    return warn


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", required=True, help="脚本库里的相对路径，例如 人间隙/04-懦弱")
    p.add_argument("--strict", action="store_true", help="把警告也当成不通过")
    args = p.parse_args(argv)

    rvs.CLIP_FILTER = args.work
    rvs.CLIP_EXCLUDE = ""
    try:
        clips = rvs.load_clips()
    except RuntimeError as e:
        print(f"[FAIL] {e}")
        return 2

    errors: list[str] = []
    warnings: list[str] = []
    total = 0.0

    for clip in clips:
        text = clip["text"]
        try:
            prompt = rvs.extract_prompt(text)
        except ValueError as e:
            errors.append(f"{clip['id']}: {e}")
            continue
        try:
            seconds = rvs.extract_duration(text)
        except ValueError as e:
            errors.append(f"{clip['id']}: {e}")
            continue
        total += seconds
        for item in check_prompt(prompt):
            errors.append(f"{clip['id']}: {item}")
        if "## 中文对照" not in text:
            warnings.append(f"{clip['id']}: 没有中文对照，改稿时无从下手")

    for seg in sorted({c["path"].parent for c in clips}):
        for item in check_segment(seg):
            warnings.append(f"{seg.name}: {item}")

    work_ov = clips[0]["path"].parent.parent / "00-overview.md"
    if not work_ov.exists():
        errors.append(f"缺作品总表 {work_ov}")
    elif not rvs.extract_end_card(work_ov.read_text(encoding="utf-8")):
        warnings.append("作品总表里没有片尾字卡")

    print(f"[SCAN] {len(clips)} 条 clip，生成总时长 {total:.2f}s（不含片尾）")
    for item in warnings:
        print(f"[WARN] {item}")
    for item in errors:
        print(f"[FAIL] {item}")
    if errors:
        print(f"[RESULT] 不通过：{len(errors)} 个错误，{len(warnings)} 个警告")
        return 1
    if warnings and args.strict:
        print(f"[RESULT] 严格模式不通过：{len(warnings)} 个警告")
        return 1
    print(f"[RESULT] 通过，{len(warnings)} 个警告")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
