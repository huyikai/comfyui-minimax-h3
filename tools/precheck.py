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
from notify import clip_label  # noqa: E402

# H3 会把引号里的字画进画面，这些词还会直接引出字幕。
BANNED = ("subtitle", "caption", "burned-in", "chinese text overlay", "text overlay")
# 占字数但不涨表演的空词。
EMPTY_WORDS = ("emotional", "cinematic", "beautiful", "expressive")
DIALOGUE_TAG = re.compile(r"<d>.*?</d>", re.S)
CJK = re.compile(r"[\u4e00-\u9fff]")

DIALOGUE_LINE = re.compile(r"<d>\s*\[[^\]]+\]\s*(.*?)\s*</d>", re.S)
SPEAK_ASSIGN = re.compile(
    r"Speaking assignment for this clip only:\s*(.*?)(?=\s*Live-action|$)", re.S
)
# 单独成句的亲属称谓＝在喊人，说这句的几乎一定是孩子或晚辈。
KIN_CALL = re.compile(r"^(妈妈|妈|母亲|娘|爸爸|爸|父亲|爹|奶奶|爷爷|姥姥|姥爷|外婆|外公)$")
CHILD_WORD = re.compile(r"\b(child|children|boy|girl|kid|toddler|baby|son|daughter)\b", re.I)
OFFSCREEN_WORD = re.compile(r"\b(off-?screen|off-?camera|voiceover|voice-over|from the left edge|from outside)\b", re.I)

BEAT = re.compile(r"\bFrom (\d+(?:\.\d+)?) to (\d+(?:\.\d+)?)\b")
LOCK_BLOCK = re.compile(r"Identity lock, reuse verbatim:\s*(.*?)(?=\s*Speaking assignment)", re.S)
LOCK_ENTRY = re.compile(r"\(([A-Z])\)\s*([^()]*)")
SUBTITLE_ROW = re.compile(r"^\|\s*对白字幕[^|]*\|([^|]*)\|", re.M)
BACKTICKED = re.compile(r"`([^`]+)`")
PUNCT = re.compile(r"[\s，。！？、；：,.!?;:—…「」『』“”\"'()（）]+")
NOTE_ITEM = re.compile(r"^[（(].*[)）]$|无对白|无底部")


def norm(s: str) -> str:
    return " ".join(s.split()).rstrip(" .,;")


def first_sentence(s: str) -> str:
    """锁句是一句话。后面跟的套话（This clip opens on a new cut.）不属于这个人的外观。"""
    return norm(norm(s).split(". ")[0])


def diverge(a: str, b: str) -> tuple[str, str]:
    """只显示两句从哪里开始不一样，省得在一模一样的前缀里找。"""
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    cut = a.rfind(" ", 0, i) + 1
    head = "…" if cut else ""
    return f"{head}{a[cut:cut + 64]}", f"{head}{b[cut:cut + 64]}"


def check_beats(clip_id: str, prompt: str, seconds: float) -> list[str]:
    """节拍要从 0 无缝铺到时长。漏一段，那段就由 H3 自由发挥——「演的不是那件事」多半是这里空的。"""
    beats = [(float(a), float(b)) for a, b in BEAT.findall(prompt)]
    if not beats:
        return [f"{clip_id}: 英文里没有 `From x to y` 节拍，整条交给 H3 自由发挥"]
    out: list[str] = []
    if beats[0][0] > 0.01:
        out.append(f"{clip_id}: 节拍从 {beats[0][0]:.2f}s 才开始，开头 {beats[0][0]:.2f}s 没写")
    for (_, end), (start, _) in zip(beats, beats[1:]):
        if end - start > 0.01:
            out.append(f"{clip_id}: 节拍在 {start:.2f}s–{end:.2f}s 重叠")
        elif start - end > 0.01:
            out.append(f"{clip_id}: 节拍在 {end:.2f}s–{start:.2f}s 空着，这段没人告诉 H3 演什么")
    if abs(beats[-1][1] - seconds) > 0.01:
        out.append(f"{clip_id}: 节拍写到 {beats[-1][1]:.2f}s，本条时长 {seconds:.2f}s，尾巴对不上")
    return out


def check_locks(clips: list[dict]) -> list[str]:
    """同段里同一个 ID 的 Identity lock 必须逐字一样，差一个词就是换脸。"""
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    out: list[str] = []
    for clip in clips:
        seg = str(clip["path"].parent)
        block = LOCK_BLOCK.search(clip["prompt"])
        if not block:
            continue
        for cid, desc in LOCK_ENTRY.findall(block.group(1)):
            desc = first_sentence(desc)
            if not desc:
                continue
            key = (seg, cid)
            if key not in seen:
                seen[key] = (clip["label"], desc)
            elif seen[key][1] != desc:
                first_clip, first_desc = seen[key]
                left, right = diverge(first_desc, desc)
                out.append(
                    f"{clip['label']}: ({cid}) 的锁句与 {first_clip} 不一致"
                    f"——确认是换了外观（会换脸），还是把姿态写进了锁句\n"
                    f"      {first_clip}：{left}\n"
                    f"      {clip['label']}：{right}"
                )
    return out


def check_dupes(clips: list[dict]) -> list[str]:
    """同一句台词出现在两条 = 跟读。"""
    where: dict[str, list[str]] = {}
    for clip in clips:
        for line in DIALOGUE_LINE.findall(clip["prompt"]):
            where.setdefault(norm(line), []).append(clip["label"])
    return [
        f"「{line}」同时出现在 {' 和 '.join(hits)}，后一条会跟读"
        for line, hits in where.items()
        if len(hits) > 1
    ]


def bare(s: str) -> str:
    """比对只看字，不看标点——总表把一句拆成两张字幕卡是排版选择，不是错。"""
    return PUNCT.sub("", s)


def check_dialogue_sync(seg_dir: Path, clips: list[dict]) -> list[str]:
    """段总表「对白字幕」那栏是对白的唯一依据，两边必须对得上。"""
    ov = seg_dir / "00-overview.md"
    if not ov.exists():
        return []
    row = SUBTITLE_ROW.search(ov.read_text(encoding="utf-8"))
    if not row:
        return ["段总表「屏幕字」表里没有「对白字幕」那行，对白没有唯一依据"]
    cell = row.group(1)
    # 有的段加了反引号，有的没加，都按 / 分条。
    items = BACKTICKED.findall(cell) or cell.split("/")
    # 「（本段画面无底部对白）」这类备注不是台词。
    listed = [x for x in (norm(i) for i in items) if x and not NOTE_ITEM.match(x)]
    written = [norm(x) for c in clips for x in DIALOGUE_LINE.findall(c["prompt"])]
    if not listed:
        return ["段总表的对白字幕栏是空的，对白没有唯一依据"]

    def unmatched(these: list[str], those: list[str]) -> list[str]:
        return [
            a for a in these
            if not any(bare(a) in bare(b) or bare(b) in bare(a) for b in those)
        ]

    out = [
        f"段总表列了「{line}」，但没有任何 clip 的 `<d>` 写它——会漏一句话"
        for line in unmatched(listed, written)
    ]
    out += [
        f"clip 里写了「{line}」，段总表的对白字幕栏没有——两边改岔了"
        for line in unmatched(written, listed)
    ]
    return out


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


def check_speakers(clip_id: str, prompt: str) -> tuple[list[str], list[str], list[str]]:
    """把「这句话 → 挂在谁身上」摆出来。

    对不对是语义问题，机器判不了；但把三样东西并排放在一行，
    「30 岁男人喊妈妈」这种错一眼就能看见。
    """
    lines = [" ".join(m.split()) for m in DIALOGUE_LINE.findall(prompt)]
    if not lines:
        return [], [], []

    match = SPEAK_ASSIGN.search(prompt)
    assign = " ".join(match.group(1).split()) if match else ""
    if not assign:
        return [], [], [f"{clip_id}: 有 `<d>` 对白却没写 Speaking assignment，H3 会自己挑一张嘴"]

    short = assign if len(assign) <= 96 else assign[:95] + "…"
    notes = [f"{clip_id}  「{line}」  ← {short}" for line in lines]
    warns = [
        f"{clip_id}: 「{line}」是在喊人，说话人却指派给了成年人。"
        f"确认这句是不是该由孩子或画外声说"
        for line in lines
        if KIN_CALL.match(line)
        and not CHILD_WORD.search(assign)
        and not OFFSCREEN_WORD.search(assign)
    ]
    return notes, warns, []


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
    speakers: list[str] = []
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
        label = clip_label(clip["id"])
        clip["prompt"], clip["label"] = prompt, label
        for item in check_prompt(prompt):
            errors.append(f"{clip['id']}: {item}")
        notes, warns, fails = check_speakers(label, prompt)
        speakers.extend(notes)
        warnings.extend(warns)
        errors.extend(fails)
        warnings.extend(check_beats(label, prompt, seconds))
        if "## 中文对照" not in text:
            warnings.append(f"{clip['id']}: 没有中文对照，改稿时无从下手")

    ready = [c for c in clips if c.get("prompt")]
    warnings.extend(check_locks(ready))
    warnings.extend(check_dupes(ready))
    for seg in sorted({c["path"].parent for c in ready}):
        for item in check_segment(seg):
            warnings.append(f"{seg.name}: {item}")
        in_seg = [c for c in ready if c["path"].parent == seg]
        for item in check_dialogue_sync(seg, in_seg):
            warnings.append(f"{seg.name}: {item}")

    work_ov = clips[0]["path"].parent.parent / "00-overview.md"
    if not work_ov.exists():
        errors.append(f"缺作品总表 {work_ov}")
    elif not rvs.extract_end_card(work_ov.read_text(encoding="utf-8")):
        warnings.append("作品总表里没有片尾字卡")

    print(f"[SCAN] {len(clips)} 条 clip，生成总时长 {total:.2f}s（不含片尾）")
    if speakers:
        print(f"\n[SPEAK] 对白归属，逐句核一遍「这句话该由谁说」：")
        for item in speakers:
            print(f"  {item}")
        print()
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
