"""timeline.json 点过名的物件 / 开场状态，对不上 clip 就算事件链没铺满。

precheck 调用。没有时间线 → 警告，不当整单失败（可能还没跑 source_agent）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scratch import scratch_dir  # noqa: E402

SOURCE_SPAN = re.compile(
    r"全局时间[（(]源片[)）]：`?\s*(\d+\.\d+)\s*[–—\-至到~～]\s*(\d+\.\d+)"
)
BEAT = re.compile(r"\bFrom (\d+(?:\.\d+)?) to (\d+(?:\.\d+)?)\b")
EVENT_CHAIN_HEAD = re.compile(r"^\*\*事件链\*\*", re.M)

# 硬切往往落在区间末尾，最后 0.2s 的格常是下一条第一帧。
EDGE = 0.20
HEDGE = re.compile(r"似乎|好像|可能|或物体|看不清|模糊|隐约")

# 开场写成结果态。只查第一条 From 节拍，后面「抱稳」是合法的。
RESULT_OPENING = re.compile(
    r"already (holding|has the child|has her locked|has him locked)"
    r"|already in (his|her) arms"
    r"|locked in both arms"
    r"|开场已经抱"
    r"|已经把孩",
    re.I,
)
HELD_NOW = re.compile(
    r"\b(in (his|her) arms|holding the child|holds the child)\b|抱在怀里|已经抱着",
    re.I,
)
STILL_ON_POLE = re.compile(
    r"仍抱|还在柱|紧抓电线杆|抱着电线杆|抱在电线杆|抱住柱|抱在柱"
    r"|still hugging|hugging that pole|locked around the (pole|shaft)",
    re.I,
)
HAS_POLE_PROCESS = re.compile(
    r"\bpole\b|电线杆|抱柱|hugging|still separate|has not yet taken|还在靠近",
    re.I,
)


@dataclass(frozen=True)
class Marker:
    name: str
    timeline: tuple[str, ...]
    clip: tuple[str, ...]


# 高信号物件。福 / 帽徽 / 校徽不进表——点名反而会画出来。
MARKERS: tuple[Marker, ...] = (
    Marker(
        "电线杆/柱",
        (r"电线杆", r"水泥杆", r"抱柱", r"抱住柱", r"抱在柱"),
        (
            r"电线杆",
            r"水泥杆",
            r"抱柱",
            r"utility pole",
            r"concrete pole",
            r"hugging the (pole|shaft)",
            r"locked around the (pole|shaft)",
            r"\bpole\b",
        ),
    ),
    Marker(
        "拉绳/绳索",
        (r"拉绳", r"绳索", r"救生绳", r"橙色绳"),
        (r"拉绳", r"绳索", r"救生绳", r"橙.*绳", r"\brope\b"),
    ),
    Marker(
        "泳帽",
        (r"泳帽",),
        (r"泳帽", r"swim cap", r"bathing cap"),
    ),
    Marker(
        "书包",
        (r"书包", r"双肩包"),
        (r"书包", r"backpack", r"red pack", r"school pack"),
    ),
    Marker(
        "扶梯/梯子",
        (r"扶梯", r"梯子"),
        (r"扶梯", r"梯子", r"\bladder\b"),
    ),
    Marker(
        "推床/担架",
        (r"推床", r"担架"),
        (r"推床", r"担架", r"gurney", r"stretcher"),
    ),
    Marker(
        "安全帽/头盔",
        (r"安全帽", r"头盔"),
        (r"安全帽", r"头盔", r"helmet", r"hard hat"),
    ),
    Marker(
        "麦克风",
        (r"麦克风", r"话筒"),
        (r"麦克风", r"话筒", r"microphone", r"mic stand", r"\bmic\b"),
    ),
)


def load_timeline(work: str) -> dict | None:
    path = scratch_dir(work, "source") / "timeline.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def parse_span(text: str) -> tuple[float, float] | None:
    m = SOURCE_SPAN.search(text)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def cells_in_span(timeline: dict, t0: float, t1: float) -> list[tuple[float, str]]:
    """只收时间戳落在本条源片区间内的「它看见的」，避免 3 秒窗把邻条带进来。"""
    out: list[tuple[float, str]] = []
    for w in timeline.get("windows") or []:
        for key, val in (w.get("seen") or {}).items():
            try:
                t = float(key)
            except (TypeError, ValueError):
                continue
            if t0 - 1e-6 <= t < (t1 - EDGE) - 1e-6 and isinstance(val, str) and val.strip():
                out.append((t, val))
    out.sort()
    return out


def first_beat_body(prompt: str) -> str:
    hits = list(BEAT.finditer(prompt))
    if not hits:
        return ""
    end = hits[1].start() if len(hits) > 1 else len(prompt)
    return prompt[hits[0].start() : end]


def _search_any(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def marker_hits(cells: list[tuple[float, str]], marker: Marker) -> list[float]:
    times: list[float] = []
    for t, blob in cells:
        if HEDGE.search(blob):
            continue
        if _search_any(marker.timeline, blob):
            times.append(t)
    return times


def check_clip(
    clip_id: str, text: str, prompt: str, timeline: dict
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    span = parse_span(text)
    if span is None:
        warnings.append(f"{clip_id}: 读不到「全局时间（源片）」，跳过事件链对照")
        return errors, warnings

    t0, t1 = span
    cells = cells_in_span(timeline, t0, t1)
    if not cells:
        warnings.append(
            f"{clip_id}: 源片 {t0:.2f}–{t1:.2f}s 在 timeline 里没有「它看见的」，跳过事件链对照"
        )
        return errors, warnings

    clip_blob = text + "\n" + prompt
    named: list[str] = []
    for marker in MARKERS:
        hits = marker_hits(cells, marker)
        if not hits:
            continue
        named.append(marker.name)
        if _search_any(marker.clip, clip_blob):
            continue
        msg = (
            f"{clip_id}: timeline 在 {t0:.2f}–{t1:.2f}s 点名了「{marker.name}」"
            f"（{len(hits)} 格），对照和英文都没有"
        )
        if len(hits) >= 2:
            errors.append(msg)
        else:
            warnings.append(msg + "（只出现一格，可能是邻条边缘）")

    if named and not EVENT_CHAIN_HEAD.search(text):
        warnings.append(
            f"{clip_id}: timeline 点过名（{'、'.join(named)}），对照里没有「事件链」栏"
        )

    opening = cells[0][1]
    beat0 = first_beat_body(prompt)
    if STILL_ON_POLE.search(opening) and beat0:
        if RESULT_OPENING.search(beat0):
            errors.append(
                f"{clip_id}: 开场格还是抱柱/抓杆，第一条节拍写成了 already holding / 已经抱着"
            )
        elif HELD_NOW.search(beat0) and not HAS_POLE_PROCESS.search(beat0):
            errors.append(
                f"{clip_id}: 开场格还是抱柱/抓杆，第一条节拍直接写怀里抱着，过程被收成结果态"
            )

    return errors, warnings


def check_work(
    work: str, clips: list[dict]
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    timeline = load_timeline(work)
    if timeline is None:
        warnings.append(
            f"没有 {scratch_dir(work, 'source') / 'timeline.json'}，跳过事件链对照"
        )
        return errors, warnings
    for clip in clips:
        e, w = check_clip(clip["id"], clip["text"], clip["prompt"], timeline)
        errors.extend(e)
        warnings.extend(w)
    return errors, warnings


def _self_test() -> None:
    timeline = {
        "windows": [
            {
                "start": 10.0,
                "end": 13.0,
                "seen": {
                    "10.50": "小女孩仍抱在电线杆上",
                    "11.00": "男子接近电线杆",
                    "11.50": "男子游到电线杆旁",
                    "12.00": "男子将小女孩抱入怀中，女孩背着红色书包",
                },
            }
        ]
    }
    head = "- 全局时间（源片）：`10.50 – 13.13`\n\n**事件链**\n还在抱柱\n"
    bad_prompt = (
        "From 0.00 to 1.40 he already has the child locked in both arms. "
        "From 1.40 to 4.00 he holds her."
    )
    errs, _ = check_clip("swim/clip-04", head, bad_prompt, timeline)
    assert any("already holding" in x or "已经抱" in x for x in errs), errs

    good_prompt = (
        "A grey concrete utility pole. At 0.00 the child is still hugging that pole. "
        "From 0.00 to 0.80 the child stays locked on the pole; he has not yet taken "
        "the child, orange rope in his right hand, red pack. "
        "From 0.80 to 4.00 he pulls the child off the pole."
    )
    errs, warns = check_clip("swim/clip-04", head, good_prompt, timeline)
    assert not errs, (errs, warns)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--work", help="例如 人间隙/04-懦弱")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--dump", metavar="CLIP", help="打印某条源片区间里命中的物件格，例如 01-游泳/clip-04")
    args = p.parse_args(argv)
    if args.self_test:
        _self_test()
        print("[OK] event_chain self-test")
        return 0
    if args.dump:
        if not args.work:
            p.error("--dump 需要 --work")
        timeline = load_timeline(args.work)
        if timeline is None:
            print("没有 timeline.json")
            return 1
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import run_video_scripts as rvs

        rvs.CLIP_FILTER = args.work
        rvs.CLIP_EXCLUDE = ""
        clip = next(c for c in rvs.load_clips() if args.dump.replace("\\", "/") in c["id"].replace("\\", "/"))
        span = parse_span(clip["text"])
        print(f"{clip['id']} {span}")
        cells = cells_in_span(timeline, *span)
        for t, blob in cells:
            hits = [m.name for m in MARKERS if not HEDGE.search(blob) and _search_any(m.timeline, blob)]
            mark = f"  [{', '.join(hits)}]" if hits else ""
            print(f"  {t:.2f}{mark} {blob[:120]}")
        return 0
    if not args.work:
        p.error("需要 --work 或 --self-test")
    timeline = load_timeline(args.work)
    if timeline is None:
        print("没有 timeline.json")
        return 1
    print(f"windows {len(timeline.get('windows') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
