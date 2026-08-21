r"""读原片：0.5 秒抽帧 + OCR + 转写 + SDK 按窗读横条，吐时间线。

写稿主对话读 timeline.json / timeline.md，不要通读 frames/。
读图 agent 只描述画面，禁止判定说话人。

    .\.venv\Scripts\python.exe tools\source_agent.py 原片.mp4 --work "人间隙/04-懦弱"
    .\.venv\Scripts\python.exe tools\source_agent.py 原片.mp4 --work "人间隙/04-懦弱" `
      --windows 0.00-3.00,6.50-9.50,62.77-65.77

同步 API 在 Windows 上有 select() 管道 bug，这里走异步。需要 CURSOR_API_KEY。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cursor_sdk_util import (  # noqa: E402
    model_selection,
    prompt_with_retry,
    run_tag,
    tokens_of,
)
from read_source import grab, probe, run_ocr, scene_cuts  # noqa: E402
from scratch import frame_name, scratch_dir  # noqa: E402

STEP = 0.5
WINDOW = 3.0
HOP = 2.5
OVERLAP = 0.5
CELLS = 6
CELL_H = 360
CUT_NEAR = 0.25
SPEAKER_LINE = re.compile(r"说话人")

SEEN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[:：]\s*(.+)$")
PEOPLE = re.compile(r"成人\s*(\d+)\s*孩子\s*(\d+)")
MOUTH = re.compile(r"^\s*[-–—]?\s*(.+?)\s*[:：]\s*(开|闭|出画)\s*[—\-–]*\s*(.*)$")
TEXT = re.compile(
    r"^\s*[-–—]?\s*(对白|黄字|水印|无)\s*[:：]\s*(.*)$"
)

PROMPT = """\
你在替一条要复刻的原片做读图。只描述这一窗里实际看见的。
**不要写剧本，不要判定谁在说话，不要写「说话人」。**
画面上谁嘴在动 ≠ 字幕上那句就是他说的。画外音很常见。

这张图是同一时间窗的 {n} 帧横向拼接，从左到右每隔 0.5 秒。
整窗 {start:.2f}s–{end:.2f}s。不是 {n} 个镜头，也不是 {n} 个人。

## 这一窗的 OCR（机械认字，可能有错或漏）

{ocr}

## 这一窗的转写（人声，BGM 大时不可靠）

{asr}

## 怎么回

**分两步，顺序不要颠倒。**

第一步，先只描述各格里看见什么——人、衣服、站位、嘴张没张、屏幕上有没有字。
不要用上面 OCR/转写里的词硬套，不要写「符合」「一致」。

第二步填字段。「看见了什么字」以你眼睛为准；分类可以对照 OCR。

严格按下面格式回，不要加别的段落，不要出现「说话人」三个字：

看图:
{cell_lines}
人数: 成人 N 孩子 M — 一句话（虚焦路人不算）
口型:
  - 左/中/右谁: 开|闭|出画 — 哪几格
画面字:
  - 对白|黄字|水印|无: 原文 — 位置（底/左上/中）
动作: 这一窗在干什么，不要编情节
拿不准: 无 | 逗号分隔。只写会改读片结论的：窗内人数变了、关键的字完全看不见。口型略糊、远景路人、压缩发糊不要写。
"""


def cs(t: float) -> str:
    return f"{int(round(t * 100)):05d}"


def parse_windows(spec: str) -> list[tuple[float, float]]:
    out = []
    for part in spec.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        if not b:
            raise SystemExit(f"--windows 要写成 起-止，收到 {part!r}")
        out.append((float(a), float(b)))
    return out


def load_cuts(path: Path) -> list[float]:
    if not path.exists():
        return []
    return [float(x) for x in path.read_text(encoding="utf-8").split() if x.strip()]


def load_ocr(path: Path) -> list[tuple[float, str]]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*s\s+(.*)$", line)
        if m:
            rows.append((float(m.group(1)), m.group(2).strip()))
    return rows


def ocr_in(ocr: list[tuple[float, str]], start: float, end: float) -> str:
    bits = [txt for t, txt in ocr if start - 1e-6 <= t < end + 1e-6 and txt]
    return "\n".join(f"{t:.2f}s  {txt}" for t, txt in ocr
                     if start - 1e-6 <= t < end + 1e-6 and txt) or "（无）"


def asr_in(asr: list[dict], start: float, end: float) -> str:
    bits = []
    for seg in asr:
        if seg["end"] <= start or seg["start"] >= end:
            continue
        bits.append(f'{seg["start"]:.2f}-{seg["end"]:.2f}  {seg["text"]}')
    return "\n".join(bits) or "（无）"


def window_starts(duration: float, cuts: list[float]) -> list[float]:
    starts: list[float] = []
    t = 0.0
    while t < duration - 0.05:
        starts.append(round(t, 2))
        t += HOP
    for cut in cuts:
        if cut <= 0 or cut >= duration:
            continue
        if any(abs(s - cut) <= CUT_NEAR for s in starts):
            continue
        starts.append(round(cut, 2))
    return sorted(set(starts))


def cell_times(start: float, end: float) -> list[float]:
    times = []
    t = start
    while t < end - 1e-6 and len(times) < CELLS:
        times.append(round(t, 2))
        t += STEP
    if not times:
        times = [round(start, 2)]
    return times


def ensure_frames(src: Path, work: str, duration: float, times: list[float]) -> list[Path]:
    frame_dir = scratch_dir(work, "frames")
    frames = []
    for t in times:
        dest = frame_dir / frame_name(t)
        if dest.exists() and dest.stat().st_size > 1000:
            frames.append(dest)
            continue
        if grab(src, t, dest):
            frames.append(dest)
        else:
            print(f"[MISS] {t:.2f}s")
    return frames


def make_strip(cells: list[Path], dest: Path) -> Path | None:
    from PIL import Image

    images = []
    for p in cells:
        im = Image.open(p).convert("RGB")
        if im.height != CELL_H:
            w = max(1, round(im.width * CELL_H / im.height))
            im = im.resize((w, CELL_H), Image.Resampling.LANCZOS)
        images.append(im)
    if not images:
        return None
    canvas = Image.new("RGB", (sum(im.width for im in images), CELL_H))
    x = 0
    for im in images:
        canvas.paste(im, (x, 0))
        x += im.width
        im.close()
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest, quality=85)
    return dest


def extract_audio(src: Path, wav: Path) -> None:
    wav.parent.mkdir(parents=True, exist_ok=True)
    print("[AUDIO] ffmpeg 抽 16k mono", flush=True)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(wav)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0 or not wav.exists():
        raise SystemExit(f"抽音频失败：{(r.stderr or '')[-400:]}")


def transcribe(wav: Path, out: Path, device: str) -> list[dict]:
    from faster_whisper import WhisperModel

    compute = "int8" if device == "cpu" else "float16"
    print(f"[ASR] faster-whisper small · {device}", flush=True)
    model = WhisperModel("small", device=device, compute_type=compute)
    segs, _info = model.transcribe(str(wav), language="zh", word_timestamps=False)
    rows = []
    for s in segs:
        text = (s.text or "").strip()
        if not text:
            continue
        rows.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
        print(f"  {s.start:7.2f}-{s.end:6.2f}  {text}")
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def parse_report(text: str) -> dict:
    # 说话人字段丢掉，不当答案。
    lines = [ln for ln in text.splitlines() if not SPEAKER_LINE.search(ln)]
    seen: dict[str, str] = {}
    people = {"adults": None, "children": None, "note": ""}
    mouths: list[dict] = []
    texts: list[dict] = []
    action = ""
    unsure: list[str] = []
    section = ""
    for raw in lines:
        line = raw.rstrip()
        if re.match(r"^\s*看图\s*[:：]?", line):
            section = "看图"
            continue
        if re.match(r"^\s*口型\s*[:：]?", line):
            section = "口型"
            continue
        if re.match(r"^\s*画面字\s*[:：]?", line):
            section = "画面字"
            continue
        m = re.match(r"^\s*人数\s*[:：]\s*(.+)$", line)
        if m:
            section = ""
            body = m.group(1).strip()
            p = PEOPLE.search(body)
            if p:
                people["adults"] = int(p.group(1))
                people["children"] = int(p.group(2))
            people["note"] = body
            continue
        m = re.match(r"^\s*动作\s*[:：]\s*(.+)$", line)
        if m:
            section = ""
            action = m.group(1).strip()
            continue
        m = re.match(r"^\s*拿不准\s*[:：]\s*(.+)$", line)
        if m:
            section = ""
            body = m.group(1).strip()
            if body and body != "无":
                unsure = [x.strip() for x in re.split(r"[,，、]", body) if x.strip()]
            continue
        if section == "看图":
            s = SEEN.match(line)
            if s:
                seen[s.group(1)] = s.group(2).strip()
            continue
        if section == "口型":
            s = MOUTH.match(line)
            if s:
                mouths.append({"who": s.group(1).strip(), "mouth": s.group(2),
                               "note": s.group(3).strip()})
            continue
        if section == "画面字":
            s = TEXT.match(line)
            if s:
                kind, rest = s.group(1), s.group(2).strip()
                if kind != "无" and rest:
                    texts.append({"kind": kind, "text": rest})
                elif kind == "无":
                    texts.append({"kind": "无", "text": rest})
    return {
        "seen": seen,
        "adults": people["adults"],
        "children": people["children"],
        "people_note": people["note"],
        "mouths": mouths,
        "text_seen": texts,
        "action": action,
        "agent_uncertain": unsure,
        "parsed": bool(seen or people["note"] or action),
    }


def uniq(xs: list[str]) -> list[str]:
    out, seen = [], set()
    for x in xs:
        if x and x not in seen:
            out.append(x)
            seen.add(x)
    return out


def norm_han(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s)


OCR_FOLD = str.maketrans({
    "會": "曾", "経": "经", "後": "后", "來": "来", "閒": "间",
    "麼": "么", "雲": "云", "點": "点", "這": "这", "還": "还",
    "對": "对", "時": "时", "裏": "里", "裡": "里",
})


def fold_han(s: str) -> str:
    return norm_han(s).translate(OCR_FOLD)


def grams2(s: str) -> set[str]:
    s = fold_han(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def is_grid_start(t: float) -> bool:
    n = round(t / HOP)
    return abs(t - n * HOP) <= 0.06


def window_has_cut(win: dict, cuts: list[float]) -> bool:
    a, b = win["start"], win["end"]
    return any(a + 0.15 < c < b - 0.15 for c in cuts)


def ocr_mismatch(ocr_txt: str, texts: list[dict], seen: dict | None = None) -> bool:
    ocr_n = fold_han(ocr_txt.replace("（无）", ""))
    seen_n = fold_han("".join(t.get("text", "") for t in texts
                              if t.get("kind") != "无"))
    if not seen_n and seen:
        seen_n = fold_han("".join(seen.values()))
    if not ocr_n:
        return False
    if not seen_n:
        return True
    if ocr_n in seen_n or seen_n in ocr_n:
        return False
    go, gs = grams2(ocr_n), grams2(seen_n)
    if not go or not gs:
        return False
    inter = go & gs
    return len(inter) < 3 and len(inter) / min(len(go), len(gs)) < 0.25


def flag_window(win: dict, ocr_txt: str, prev: dict | None,
                cuts: list[float]) -> tuple[list[str], list[str]]:
    """flags = 必须开图；notes = 附录（看不清、画外音提示）。"""
    hard, notes = [], []
    notes.extend(win.get("agent_uncertain") or [])
    if win.get("error"):
        return uniq(hard + ["跑失败"]), uniq(notes)
    if not win.get("parsed"):
        hard.append("格式不对")
    mouths = win.get("mouths") or []
    open_m = any(m.get("mouth") == "开" for m in mouths)
    closed_all = bool(mouths) and all(m.get("mouth") in ("闭", "出画") for m in mouths)
    ocr_has = bool(fold_han(ocr_txt.replace("（无）", "")))
    kinds = {t.get("kind") for t in win.get("text_seen") or []}
    has_dialogue = "对白" in kinds
    if open_m and not ocr_has:
        notes.append("嘴动无字幕")
    if closed_all and has_dialogue:
        notes.append("有对白嘴未动")
    if ocr_mismatch(ocr_txt, win.get("text_seen") or [], win.get("seen") or {}):
        if win["end"] - win["start"] >= 1.0:
            hard.append("OCR与画面字对不上")
        else:
            notes.append("OCR与画面字对不上")
    if (prev and is_grid_start(win["start"])
            and win.get("adults") is not None and prev.get("adults") is not None
            and not window_has_cut(win, cuts) and not window_has_cut(prev, cuts)):
        a = (win["adults"] or 0) + (win["children"] or 0)
        b = (prev["adults"] or 0) + (prev["children"] or 0)
        if abs(a - b) >= 2:
            notes.append("人数相对上窗跳了")
    return uniq(hard), uniq(notes)


def apply_flags(rows: list[dict], ocr: list[tuple[float, str]],
                cuts: list[float]) -> None:
    prev_hop = None
    for w in rows:
        ocr_txt = ocr_in(ocr, w["start"], w["end"])
        w["flags"], w["notes"] = flag_window(w, ocr_txt, prev_hop, cuts)
        if is_grid_start(w["start"]):
            prev_hop = w


async def judge(client, model, fallback, win: dict, sem) -> dict:
    from cursor_sdk import SDKImage, UserMessage

    strip = Path(win["strip"])
    cell_lines = "\n".join(f"  {t:.2f}: 你看见什么" for t in win["cells"])
    text = PROMPT.format(
        n=len(win["cells"]),
        start=win["start"],
        end=win["end"],
        ocr=win["ocr_prompt"],
        asr=win["asr_prompt"],
        cell_lines=cell_lines,
    )
    msg = UserMessage(text=text, images=[SDKImage.from_file(str(strip))])
    async with sem:
        result, used, err = await prompt_with_retry(
            client, msg, model, fallback, str(strip.parent)
        )
    row = dict(win)
    if err:
        row["error"] = err
        row["parsed"] = False
        return row
    tok = tokens_of(result)
    row["tok_in"] = tok[0] if tok else None
    row["tok_out"] = tok[1] if tok else None
    row["ms"] = getattr(result, "duration_ms", None)
    row["fell_back"] = fallback is not None and used is fallback
    body = result.result or ""
    row["raw"] = body
    row.update(parse_report(body))
    return row


def build_ticks(windows: list[dict], duration: float, ocr, asr,
                fill_tail: bool) -> list[dict]:
    ticks: dict[int, dict] = {}
    ordered = sorted(windows, key=lambda w: w["start"])
    for i, w in enumerate(ordered):
        last = fill_tail and i == len(ordered) - 1
        core_end = duration if last else min(w["start"] + HOP, w["end"], duration)
        t = w["start"]
        while t < core_end - 1e-6:
            key = int(round(t * 100))
            ticks[key] = {
                "t": round(t, 2),
                "window": round(w["start"], 2),
                "adults": w.get("adults"),
                "children": w.get("children"),
                "mouths": w.get("mouths") or [],
                "ocr": next((txt for ot, txt in ocr if abs(ot - t) < 0.26), ""),
                "asr": " ".join(
                    s["text"] for s in asr
                    if s["start"] - 1e-6 <= t < s["end"] + STEP
                ),
                "action": w.get("action") or "",
                "uncertain": w.get("flags") or [],
            }
            t = round(t + STEP, 2)
    return [ticks[k] for k in sorted(ticks)]


def write_md(path: Path, work: str, tag: str, windows: list[dict], ticks: list[dict],
             t_in: int, t_out: int, secs: float) -> None:
    must = [w for w in windows if w.get("flags")]
    lines = [
        f"# {work} 原片时间线（source_agent）",
        "",
        f"{tag} · {len(windows)} 窗 · {len(ticks)} ticks · plan 模式",
        f"token 入 {t_in} 出 {t_out} · 累计 {secs:.0f}s",
        "",
        f"**必须开图 {len(must)} 窗**（OCR 对不上 / 人数跳 / 跑失败 / 格式）。",
        "附录的看不清、「有对白嘴未动」不必开图——后者当画外音提示。",
        "",
        "## 必须开图",
        "",
    ]
    if not must:
        lines += ["无。", ""]
    else:
        for w in must:
            lines.append(f"### {w['start']:.2f}–{w['end']:.2f}")
            lines.append(f"- **原因** {'；'.join(w['flags'])}")
            if w.get("strip"):
                lines.append(f"- **横条** `{w['strip']}`")
            lines.append(
                f"- **人数** 成人 {w.get('adults')} 孩子 {w.get('children')}"
            )
            lines.append(f"- **动作** {w.get('action', '')}")
            lines.append("")
    lines += ["## 各窗", ""]
    for w in windows:
        head = f"### {w['start']:.2f}–{w['end']:.2f}"
        if w.get("fell_back"):
            head += "  ·  主模型被拒，走了兜底"
        if w.get("tok_in") is not None:
            head += f"  ·  入 {w['tok_in']} 出 {w['tok_out']} / {(w.get('ms') or 0)/1000:.0f}s"
        lines.append(head)
        if w.get("error"):
            lines += ["", f"跑失败：{w['error']}", ""]
            continue
        if w.get("flags"):
            lines.append(f"- **必须开图** {'；'.join(w['flags'])}")
        notes = w.get("notes") or []
        if notes:
            lines.append(f"- **提示** {'；'.join(notes)}")
        lines.append(
            f"- **人数** 成人 {w.get('adults')} 孩子 {w.get('children')} — {w.get('people_note','')}"
        )
        for m in w.get("mouths") or []:
            lines.append(f"- **口型** {m.get('who')} `{m.get('mouth')}` {m.get('note','')}")
        for t in w.get("text_seen") or []:
            lines.append(f"- **画面字** `{t.get('kind')}` {t.get('text')}")
        lines.append(f"- **动作** {w.get('action','')}")
        if w.get("seen"):
            lines.append("- 它看见的：" + " ／ ".join(
                f"{k} {v}" for k, v in w["seen"].items()))
        if not w.get("parsed"):
            lines += ["", "```", (w.get("raw") or "")[:1500], "```"]
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


async def run(args) -> int:
    from cursor_sdk import AsyncClient

    src = args.video.expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"找不到源片：{src}")

    work = args.work
    root = scratch_dir(work)
    source_dir = scratch_dir(work, "source")
    strips_dir = scratch_dir(work, "source/strips")

    meta = probe(src)
    duration = float(meta["format"]["duration"])
    (root / "probe.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[PROBE] {duration:.2f}s")

    cuts_path = root / "scene.txt"
    if not args.no_scene:
        cuts = scene_cuts(src, 0.25)
        cuts_path.write_text("\n".join(f"{c:.3f}" for c in cuts) + "\n", encoding="utf-8")
        print(f"[SCENE] {len(cuts)} 处硬切")
    else:
        cuts = load_cuts(cuts_path)

    if args.windows:
        spans = parse_windows(args.windows)
    else:
        spans = [(s, min(s + WINDOW, duration)) for s in window_starts(duration, cuts)]

    needed = []
    for a, b in spans:
        needed.extend(cell_times(a, b))
    needed = sorted(set(round(t, 2) for t in needed))
    # 满量时把 0.5 秒网格都抽齐，OCR/ticks 才对得上。
    if not args.windows:
        t = 0.0
        grid = []
        while t < duration:
            grid.append(round(t, 2))
            t += STEP
        needed = sorted(set(needed) | set(grid))

    print(f"[FRAMES] {len(needed)} 张 @ {STEP}s", flush=True)
    frames = ensure_frames(src, work, duration, needed)
    print(f"[FRAMES] 齐 {len(frames)}", flush=True)
    frame_map = {p.stem: p for p in frames}

    ocr_path = root / "ocr.txt"
    if args.skip_ocr and ocr_path.exists():
        print(f"[OCR] 复用 {ocr_path}", flush=True)
        ocr = load_ocr(ocr_path)
    else:
        print("[OCR]", flush=True)
        ocr_frames = [frame_map[frame_name(t).replace(".jpg", "")]
                      for t in needed if frame_name(t).replace(".jpg", "") in frame_map]
        lines, _detail = run_ocr(ocr_frames, False)
        ocr_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ocr = load_ocr(ocr_path)
    print(f"[OCR] {len(ocr)} 行", flush=True)

    wav = source_dir / "audio.wav"
    asr_path = source_dir / "asr.json"
    if args.skip_asr:
        asr = json.loads(asr_path.read_text(encoding="utf-8")) if asr_path.exists() else []
    else:
        extract_audio(src, wav)
        asr = transcribe(wav, asr_path, args.asr_device)

    windows = []
    for start, end in spans:
        cells = [t for t in cell_times(start, end)
                 if frame_name(t).replace(".jpg", "") in frame_map]
        cell_paths = [frame_map[frame_name(t).replace(".jpg", "")] for t in cells]
        dest = strips_dir / f"w{cs(start)}-{cs(end)}.jpg"
        if dest.exists() and dest.stat().st_size > 1000:
            strip = dest
        else:
            strip = make_strip(cell_paths, dest)
        if strip is None:
            print(f"[STRIP FAIL] {start:.2f}-{end:.2f}")
            continue
        windows.append({
            "start": start,
            "end": end,
            "cells": cells,
            "strip": str(strip),
            "ocr_prompt": ocr_in(ocr, start, end),
            "asr_prompt": asr_in(asr, start, end),
        })

    tag = run_tag(args.model, args.param)
    model = model_selection(args.model, args.param)
    fallback = args.fallback or None
    print(f"{len(windows)} 窗 · 并发 {args.concurrency} · {tag}"
          + (f" · 兜底 {fallback}" if fallback else ""), flush=True)
    print("[SDK] 拉 bridge…", flush=True)

    sem = asyncio.Semaphore(args.concurrency)
    async with await AsyncClient.launch_bridge(workspace=str(Path.cwd())) as client:
        print("[SDK] bridge 起来了", flush=True)
        tasks = [judge(client, model, fallback, w, sem) for w in windows]
        rows = []
        done = 0
        for coro in asyncio.as_completed(tasks):
            row = await coro
            rows.append(row)
            done += 1
            mark = "ERR" if row.get("error") else (
                "疑" if row.get("agent_uncertain") else "ok"
            )
            print(f"  [{done}/{len(windows)} {mark}] {row['start']:.2f}-{row['end']:.2f}"
                  + (f" {row.get('error','')[:80]}" if row.get("error") else "")
                  + ("  (兜底)" if row.get("fell_back") else ""), flush=True)

    rows.sort(key=lambda w: w["start"])
    apply_flags(rows, ocr, cuts)

    ticks = build_ticks(rows, duration, ocr, asr, fill_tail=not bool(args.windows))
    t_in = sum(r.get("tok_in") or 0 for r in rows)
    t_out = sum(r.get("tok_out") or 0 for r in rows)
    secs = sum((r.get("ms") or 0) for r in rows) / 1000
    partial = "-部分" if args.windows else ""
    payload = {
        "work": work,
        "video": str(src),
        "duration": duration,
        "step": STEP,
        "window": WINDOW,
        "hop": HOP,
        "model": tag,
        "windows": [{k: v for k, v in w.items() if k not in ("ocr_prompt", "asr_prompt")}
                    for w in rows],
        "ticks": ticks,
        "flags": [{"start": w["start"], "end": w["end"], "flags": w["flags"]}
                  for w in rows if w.get("flags")],
        "notes": [{"start": w["start"], "end": w["end"], "notes": w["notes"]}
                  for w in rows if w.get("notes")],
    }
    # strip 路径写成相对 scratch，raw 太长只在 md 里留。
    for w in payload["windows"]:
        w.pop("raw", None)
        sp = w.get("strip", "")
        try:
            w["strip"] = str(Path(sp).relative_to(root))
        except ValueError:
            pass

    js = source_dir / f"timeline{partial}.json"
    md = source_dir / f"timeline{partial}.md"
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_md(md, work, tag, rows, ticks, t_in, t_out, secs)
    nflag = sum(1 for w in rows if w.get("flags"))
    print(f"\n时间线 -> {js}")
    print(f"报告   -> {md}")
    print(f"token 入 {t_in} 出 {t_out} · 累计 {secs:.0f}s")
    print(f"必须开图 {nflag} 窗 / {len(rows)} 窗，跑失败 "
          f"{sum(1 for w in rows if w.get('error'))} 窗。")
    print("只开「必须开图」横条。禁止通读 frames/。")
    return 0


def report_only(args) -> int:
    work = args.work
    root = scratch_dir(work)
    source_dir = scratch_dir(work, "source")
    js = source_dir / "timeline.json"
    if not js.exists():
        raise SystemExit(f"没有 {js}，先跑满量")
    payload = json.loads(js.read_text(encoding="utf-8"))
    ocr = load_ocr(root / "ocr.txt")
    asr_path = source_dir / "asr.json"
    asr = json.loads(asr_path.read_text(encoding="utf-8")) if asr_path.exists() else []
    cuts = load_cuts(root / "scene.txt")
    rows = payload["windows"]
    apply_flags(rows, ocr, cuts)
    duration = float(payload["duration"])
    ticks = build_ticks(rows, duration, ocr, asr, fill_tail=True)
    payload["ticks"] = ticks
    payload["flags"] = [{"start": w["start"], "end": w["end"], "flags": w["flags"]}
                        for w in rows if w.get("flags")]
    payload["notes"] = [{"start": w["start"], "end": w["end"], "notes": w["notes"]}
                        for w in rows if w.get("notes")]
    t_in = sum(r.get("tok_in") or 0 for r in rows)
    t_out = sum(r.get("tok_out") or 0 for r in rows)
    secs = sum((r.get("ms") or 0) for r in rows) / 1000
    tag = payload.get("model") or ""
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md = source_dir / "timeline.md"
    write_md(md, work, tag, rows, ticks, t_in, t_out, secs)
    nflag = sum(1 for w in rows if w.get("flags"))
    print(f"时间线 -> {js}")
    print(f"报告   -> {md}")
    print(f"必须开图 {nflag} 窗 / {len(rows)} 窗")
    for w in rows:
        if w.get("flags"):
            print(f"  {w['start']:.2f}-{w['end']:.2f}  {'; '.join(w['flags'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read source video into a timeline. Reports only.")
    p.add_argument("video", type=Path, nargs="?")
    p.add_argument("--work", required=True, help='例如 "人间隙/04-懦弱"')
    p.add_argument("--windows", default="",
                   help="只跑这些窗，逗号分隔 起-止，例如 0.00-3.00,6.50-9.50")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--model", default="gemini-3.7-flash")
    p.add_argument("--param", nargs="*", default=["effort=low"],
                   help="档位，默认 effort=low")
    p.add_argument("--fallback", default="composer-2.5",
                   help="主模型被内容安全拒掉时换它；--fallback \"\" 关掉")
    p.add_argument("--asr-device", default="cpu", choices=("cpu", "cuda"))
    p.add_argument("--skip-asr", action="store_true", help="复用已有 asr.json")
    p.add_argument("--skip-ocr", action="store_true", help="复用已有 ocr.txt")
    p.add_argument("--report-only", action="store_true",
                   help="用已有 timeline.json 重算 flags 和 md，不跑 SDK")
    p.add_argument("--no-scene", action="store_true")
    args = p.parse_args(argv)
    if args.param is None:
        args.param = []
    if args.report_only:
        return report_only(args)
    if not args.video:
        raise SystemExit("需要原片路径，或改用 --report-only")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
