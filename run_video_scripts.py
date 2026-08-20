#!/usr/bin/env python3
"""Queue MiniMax H3 Turbo t2v jobs from video-script clip markdown files."""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import numpy as np

from notify import Notifier

ROOT = Path(__file__).resolve().parent
COMFY = ROOT / "ComfyUI"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
START_PS1 = ROOT / "start.ps1"
SCRIPT_ROOT = Path(r"D:\develop\video-script")
BASE_URL = "http://127.0.0.1:8188"
LOG_DIR = ROOT / "logs"
PROGRESS_PATH = LOG_DIR / "video_script_progress.json"
RUN_LOG = LOG_DIR / "video_script_run.log"
# Optional {"<filter>": "<path to the reference cut>"} for the loudness / width
# comparison printed after each concat.
REFERENCE_FILE = LOG_DIR / "reference.json"

MEGAPIXELS = 0.9
ASPECT = "16:9 (Widescreen)"
STEPS = 4
DURATION = 5.0
JOB_TIMEOUT_SEC = 45 * 60
POLL_SEC = 8
COMFY_BOOT_SEC = 180
OUTPUT_MIN_BYTES = 80_000
# Clip-to-clip level swings reach 25 LUFS raw, so every clip is normalised to
# one target before it is written or concatenated.
# Clips start speaking at sample 0, so the head ramp only exists to avoid a
# discontinuity click; anything longer eats the first syllable.
AUDIO_FADE_IN_SEC = 0.005
AUDIO_FADE_OUT_SEC = 0.02
AUDIO_TARGET_LUFS = -12.0
AUDIO_TARGET_TP = -1.5
AUDIO_TARGET_LRA = 11.0
AUDIO_SAMPLE_RATE = 32000
AUDIO_BITRATE = "192k"
# H3 renders the voice with ~4% decorrelation between L and R, which reads as a
# doubled/smeared voice. The reference cut is effectively mono, so collapse it.
AUDIO_DOWNMIX_MONO = True
AUDIO_SILENCE_DB = -60.0
# Every clip ends with ~30ms of digital silence. Butt-joining them punches an
# audible hole in the room tone at each cut, so the gap gets bridged instead.
AUDIO_BRIDGE_PRE_SEC = 0.012
AUDIO_BRIDGE_POST_SEC = 0.018
AUDIO_BRIDGE_MAX_GAP_SEC = 0.080
# Loudness normalisation aligns speech but leaves the room tone 26 dB apart
# across clips, which is what the cuts sound like. Pull the floors together
# without touching speech.
AUDIO_AMBIENCE_MAX_CUT_DB = 9.0
AUDIO_AMBIENCE_MAX_LIFT_DB = 4.0
AUDIO_PEAK_CEILING = 0.84
OUTPUT_DIR: Path | None = None
CLIP_FILTER = ""
CLIP_EXCLUDE = ""
KEEP_RAW = False
NOTIFIER = Notifier(enabled=False)
# 越界原片字幕样式不跟愿违一套，按约定不生成 ASS。
SUBS_SKIP_MARKERS = ("01-越界",)
ASS_PLAY_RES = (1280, 720)


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_json(method: str, path: str, body=None, timeout: int = 30):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else {}


def comfy_up() -> bool:
    try:
        http_json("GET", "/system_stats", timeout=5)
        return True
    except Exception:
        return False


def wait_comfy(timeout: int = COMFY_BOOT_SEC) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if comfy_up():
            return True
        time.sleep(2)
    return False


def comfy_pids() -> list[int]:
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'main.py' "
        "-and $_.CommandLine -match 'ComfyUI|preview-method|sage-attention' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    pids = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def stop_comfy() -> None:
    pids = comfy_pids()
    if not pids:
        return
    log(f"[RESTART] stopping ComfyUI pids={pids}")
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {','.join(map(str, pids))} -Force -ErrorAction SilentlyContinue"],
        capture_output=True,
        text=True,
    )
    deadline = time.time() + 30
    while time.time() < deadline and comfy_pids():
        time.sleep(1)
    time.sleep(2)


def start_comfy() -> None:
    log("[RESTART] starting start.ps1")
    subprocess.Popen(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(START_PS1)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def restart_comfy() -> None:
    stop_comfy()
    start_comfy()
    if not wait_comfy():
        raise RuntimeError("ComfyUI did not come back after restart")
    log("[RESTART] ComfyUI is up")


def extract_prompt(text: str) -> str:
    m = re.search(r"##\s*英文 H3 Prompt[\s\S]*?```text\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    m = re.search(r"```text\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip()
    raise ValueError("no fenced text prompt found")


def extract_duration(text: str) -> float:
    m = re.search(r"时长：\s*`?\s*([0-9]+(?:\.[0-9]+)?)\s*s", text)
    if not m:
        m = re.search(r"H3 填\s*([0-9]+(?:\.[0-9]+)?)", text)
    if not m:
        raise ValueError("no duration found in clip header")
    value = float(m.group(1))
    if value < 4.0 or value > 15.0:
        raise ValueError(f"duration {value} is outside H3 range 4.00–15.00")
    return value


def load_clips() -> list[dict]:
    clips = []
    for path in sorted(SCRIPT_ROOT.rglob("clip-*.md")):
        text = path.read_text(encoding="utf-8")
        prompt = extract_prompt(text)
        duration = extract_duration(text)
        rel = path.relative_to(SCRIPT_ROOT).as_posix()
        if CLIP_FILTER and CLIP_FILTER.replace("\\", "/") not in rel:
            continue
        if CLIP_EXCLUDE and CLIP_EXCLUDE.replace("\\", "/") in rel:
            continue
        clips.append(
            {
                "id": rel,
                "path": path,
                "prompt": prompt,
                "duration": duration,
                "stem": path.stem,
                "segment": path.parent.name,
                "text": text,
            }
        )
    if not clips:
        hint = f" matching {CLIP_FILTER!r}" if CLIP_FILTER else ""
        raise RuntimeError(f"no clip-*.md under {SCRIPT_ROOT}{hint}")
    return clips


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        try:
            data = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"done": {}, "runs": {}}
        data.setdefault("done", {})
        data.setdefault("runs", {})
        return data
    return {"done": {}, "runs": {}}


def save_progress(progress: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_PATH.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def reusable_output(clip: dict, progress: dict) -> Path | None:
    """The clip's existing mp4, when it was rendered with the current settings."""
    entry = (progress.get("done") or {}).get(clip["id"])
    if not entry:
        return None
    if float(entry.get("megapixels") or 0) != float(MEGAPIXELS):
        return None
    if int(entry.get("steps") or 0) != int(STEPS):
        return None
    dest = clip_output_path(clip)
    if str(entry.get("output") or "") != str(dest):
        return None
    if not dest.exists() or dest.stat().st_size < OUTPUT_MIN_BYTES:
        return None
    return dest


def run_key(filter_text: str) -> str:
    return filter_text.replace("\\", "/") or "*"


def remember_run(progress: dict, args) -> None:
    progress.setdefault("runs", {})[run_key(CLIP_FILTER)] = {
        "filter": CLIP_FILTER,
        "exclude": CLIP_EXCLUDE,
        "output_dir": str(OUTPUT_DIR) if OUTPUT_DIR else "",
        "megapixels": MEGAPIXELS,
        "steps": STEPS,
        "concat_out": str(args.concat_out) if args.concat_out else "",
        "at": datetime.now().isoformat(timespec="seconds"),
    }


def clip_output_path(clip: dict) -> Path:
    name = f"h3-t2v-turbo-{clip['segment']}-{clip['stem']}.mp4"
    if OUTPUT_DIR is not None:
        return OUTPUT_DIR / name
    return clip["path"].with_name(name)


def template_from_history() -> dict:
    hist = http_json("GET", "/history?max_items=30", timeout=60)
    for item in reversed(list(hist.values())):
        prompt = item.get("prompt")
        pdata = prompt[2] if isinstance(prompt, list) else prompt
        if not isinstance(pdata, dict):
            continue
        if pdata.get("131", {}).get("class_type") != "MiniMaxH3ImageToVideo":
            continue
        if pdata.get("134", {}).get("class_type") != "MiniMaxH3TurboLoRA":
            continue
        return pdata
    raise RuntimeError("no MiniMax H3 Turbo t2v prompt found in ComfyUI history")


def build_prompt(template: dict, clip: dict) -> dict:
    prompt = deepcopy(template)
    prefix = f"video/scripts/{clip['segment']}_{clip['stem']}"
    prompt["92"]["inputs"]["filename_prefix"] = prefix
    prompt["92"]["inputs"]["format"] = "auto"
    prompt["92"]["inputs"]["codec"] = "auto"
    prompt["115"]["inputs"]["aspect_ratio"] = ASPECT
    prompt["115"]["inputs"]["megapixels"] = MEGAPIXELS
    prompt["115"]["inputs"]["multiple"] = 32
    prompt["131"]["inputs"]["prompt"] = clip["prompt"]
    prompt["133"]["inputs"]["value"] = float(clip["duration"])
    prompt["136"]["inputs"]["value"] = STEPS
    prompt["129"]["inputs"]["noise_seed"] = random.randint(0, 2**53 - 1)
    return prompt


def queue_prompt(prompt: dict, client_id: str) -> str:
    body = {"prompt": prompt, "client_id": client_id}
    try:
        result = http_json("POST", "/prompt", body, timeout=60)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {detail[:2000]}") from e
    if result.get("error") or result.get("node_errors"):
        raise RuntimeError(json.dumps(result, ensure_ascii=False)[:2000])
    pid = result.get("prompt_id")
    if not pid:
        raise RuntimeError(f"no prompt_id in {result}")
    return pid


def history_item(prompt_id: str):
    try:
        hist = http_json("GET", f"/history/{prompt_id}", timeout=30)
    except Exception as e:
        raise ConnectionError(str(e)) from e
    return hist.get(prompt_id)


def output_files(item: dict) -> list[Path]:
    files = []
    for node_out in (item.get("outputs") or {}).values():
        for im in node_out.get("images") or []:
            name = im.get("filename")
            if not name:
                continue
            sub = im.get("subfolder") or ""
            files.append(COMFY / "output" / sub / name if sub else COMFY / "output" / name)
    return files


def wait_job(prompt_id: str) -> dict:
    deadline = time.time() + JOB_TIMEOUT_SEC
    while time.time() < deadline:
        if not comfy_up():
            raise ConnectionError("ComfyUI is down")
        item = history_item(prompt_id)
        if item:
            status = item.get("status") or {}
            if status.get("completed") or status.get("status_str") in ("success", "error"):
                if status.get("status_str") == "error" or status.get("completed") is False:
                    msgs = status.get("messages") or []
                    raise RuntimeError(f"execution error: {msgs[-1:]}")
                files = output_files(item)
                if not files:
                    raise RuntimeError("completed but no output files")
                missing = [p for p in files if not p.exists() or p.stat().st_size < OUTPUT_MIN_BYTES]
                if missing:
                    raise RuntimeError(f"output missing/too small: {missing}")
                return item
        time.sleep(POLL_SEC)
    raise TimeoutError(f"job {prompt_id} timed out after {JOB_TIMEOUT_SEC}s")


def measure_loudness(src: Path) -> dict | None:
    """EBU R128 pass 1. Returns None when the track is silent or unreadable."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(src),
        "-map",
        "a:0",
        "-af",
        f"loudnorm=I={AUDIO_TARGET_LUFS}:TP={AUDIO_TARGET_TP}:LRA={AUDIO_TARGET_LRA}:print_format=json",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    err = result.stderr.decode("utf-8", "replace")
    blocks = re.findall(r'\{[^{}]*"input_i"[\s\S]*?\}', err)
    if not blocks:
        return None
    try:
        stats = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    try:
        if float(stats["input_i"]) < -70.0:
            return None
    except (KeyError, ValueError):
        return None
    return stats


def audio_filter_chain(src: Path, duration: float | None = None, fades: bool = True) -> str:
    """loudnorm to a fixed target, then de-click fades at both edges."""
    parts: list[str] = []
    stats = measure_loudness(src)
    if stats:
        parts.append(
            "loudnorm="
            f"I={AUDIO_TARGET_LUFS}:TP={AUDIO_TARGET_TP}:LRA={AUDIO_TARGET_LRA}"
            f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
            f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
            f":offset={stats['target_offset']}:linear=true:print_format=summary"
        )
    if AUDIO_DOWNMIX_MONO:
        # Dual mono rather than a mono stream: R128 sums channel powers, so a
        # true mono track would measure 3 LU quieter than the target.
        parts.append("pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1")
    parts.append(f"aresample={AUDIO_SAMPLE_RATE}")
    if duration and duration > 0:
        # Pad-then-trim pins the track to the video length so concat offsets
        # (and the ASS timeline built from them) cannot drift. Trim by sample
        # count: AAC decode starts at a non-zero PTS, which makes a
        # timestamp-based trim cut ~50ms short.
        parts.append("asetpts=N/SR/TB")
        parts.append("apad")
        parts.append(f"atrim=end_sample={round(duration * AUDIO_SAMPLE_RATE)}")
        parts.append("asetpts=N/SR/TB")
    if fades:
        if AUDIO_FADE_IN_SEC > 0:
            parts.append(f"afade=t=in:st=0:d={AUDIO_FADE_IN_SEC:.3f}")
        if duration and duration > AUDIO_FADE_OUT_SEC > 0:
            parts.append(f"afade=t=out:st={duration - AUDIO_FADE_OUT_SEC:.3f}:d={AUDIO_FADE_OUT_SEC:.3f}")
    return ",".join(parts)


def normalize_clip_audio(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".audio-tmp" + dest.suffix)
    chain = audio_filter_chain(src, probe_duration(src))
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-af",
        chain,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not tmp.exists() or tmp.stat().st_size < OUTPUT_MIN_BYTES:
        err = result.stderr.decode("utf-8", "replace")[-1500:]
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg audio normalise failed: {err}")
    tmp.replace(dest)


def copy_result(clip: dict, src: Path) -> Path:
    dest = clip_output_path(clip)
    normalize_clip_audio(src, dest)
    return dest


def chinese_section(text: str) -> str:
    m = re.search(r"##\s*中文对照[\s\S]*", text)
    return m.group(0) if m else text


def skip_subs(clip: dict) -> str | None:
    rel = clip["id"].replace("\\", "/")
    for marker in SUBS_SKIP_MARKERS:
        if marker in rel:
            return marker
    return None


def overview_path(clip: dict) -> Path:
    return clip["path"].parent / "00-overview.md"


def extract_title(overview_text: str) -> str:
    m = re.search(r"解释性标题[^|\n]*\|\s*`([^`]+)`", overview_text)
    if m:
        return m.group(1).strip()
    m = re.search(r"解释性标题[^|\n]*\|\s*([^|\n]+)\|", overview_text)
    if m:
        return m.group(1).strip().strip("`")
    return ""


def clip_era(clip: dict, overview_text: str) -> str:
    """过去 / 现在 / empty. 黄字标题只出现在「现在」这一拍。"""
    for m in re.finditer(r"`(clip-\d+\.md)`[：:]\s*(过去|现在)", overview_text):
        if m.group(1) == clip["path"].name:
            return m.group(2)
    return ""


def extract_dialogue_events(text: str) -> tuple[list[dict], list[str]]:
    missing: list[str] = []
    section = chinese_section(text)
    beat_re = re.compile(
        r"-\s*\*\*节拍\s*\d+\s*（\s*([0-9]+(?:\.[0-9]+)?)\s*[–\-]\s*([0-9]+(?:\.[0-9]+)?)"
    )
    dialogue_re = re.compile(r"\*\*对白(?!修正)[^*]*\*\*[ \t]*`([^`\n]+)`?")
    beats = [(m.start(), float(m.group(1)), float(m.group(2))) for m in beat_re.finditer(section)]
    bare = dialogue_re.findall(section)
    if not beats:
        if bare:
            missing.append(
                "在「中文对照 / 表演节拍」为每句对白补起止秒数，"
                "例如：节拍 2（0.80–2.00），下一行 **对白 (S1)：** 用反引号包原文"
            )
        return [], missing

    events: list[dict] = []
    for i, (pos, t0, t1) in enumerate(beats):
        chunk_end = beats[i + 1][0] if i + 1 < len(beats) else len(section)
        lines = [t.strip() for t in dialogue_re.findall(section[pos:chunk_end]) if t.strip()]
        if not lines:
            continue
        if t1 <= t0:
            missing.append(f"节拍时间无效：{t0}–{t1}")
            continue
        step = (t1 - t0) / len(lines)
        for j, line in enumerate(lines):
            events.append(
                {
                    "text": line,
                    "start": t0 + j * step,
                    "end": t0 + (j + 1) * step,
                    "style": "Dialogue",
                }
            )
    if not events and bare:
        missing.append("对白没写进带时间的表演节拍里，请把 `**对白**` 放进对应 `**节拍 N（开始–结束）**` 下面")
    return events, missing


def detect_speech_ranges(video: Path) -> list[tuple[float, float]]:
    """Voice-band energy islands, not broadband silence (stage noise / reverb fool that)."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(video),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "-v",
            "error",
            "-",
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return []
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if pcm.size < 1600:
        return []
    sr = 16000
    hop = int(0.02 * sr)
    win = int(0.04 * sr)
    freqs = np.fft.rfftfreq(win, 1 / sr)
    band = (freqs >= 180) & (freqs <= 3800)
    window = np.hanning(win)
    energies = []
    for i in range(0, len(pcm) - win, hop):
        spec = np.fft.rfft(pcm[i : i + win] * window)
        energies.append(float(np.mean(np.abs(spec[band]) ** 2)))
    if not energies:
        return []
    v = np.array(energies)
    peak = float(np.max(v))
    if peak <= 1e-12:
        return []
    p25 = float(np.percentile(v, 25))
    p70 = float(np.percentile(v, 70))
    # A loud peak must not hide later, quieter syllables.
    thresh_high = max(peak * 0.04, p70 * 1.15, p25 * 8.0)
    high = v >= thresh_high
    active = np.zeros(len(v), dtype=bool)
    hang = max(1, int(0.16 / 0.02))
    hang_left = 0
    for i, is_high in enumerate(high):
        if is_high:
            hang_left = hang
            active[i] = True
        elif hang_left > 0:
            hang_left -= 1
            active[i] = True
        else:
            active[i] = False
    # fill holes shorter than 180ms (intra-word gaps)
    hole = max(1, int(0.18 / 0.02))
    filled = active.copy()
    n = len(filled)
    i = 0
    while i < n:
        if filled[i]:
            i += 1
            continue
        j = i
        while j < n and not filled[j]:
            j += 1
        if i > 0 and j < n and (j - i) <= hole:
            filled[i:j] = True
        i = j
    spans: list[tuple[float, float]] = []
    i = 0
    while i < n:
        if not filled[i]:
            i += 1
            continue
        j = i
        while j < n and filled[j]:
            j += 1
        start = i * 0.02
        end = min(len(pcm) / sr, j * 0.02 + 0.02)
        if end - start >= 0.15 and int(np.sum(high[i:j])) >= 4:
            spans.append((start, end))
        i = j
    return spans


def _merge_speech_gaps(spans: list[tuple[float, float]], max_gap: float) -> list[tuple[float, float]]:
    if not spans:
        return []
    out = [spans[0]]
    for start, end in spans[1:]:
        prev_s, prev_e = out[-1]
        if start - prev_e <= max_gap:
            out[-1] = (prev_s, end)
        else:
            out.append((start, end))
    return out


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _assign_extra_spans(dialogues: list[dict], spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """When there are more voice islands than lines, glue islands onto the nearest line."""
    groups: list[list[tuple[float, float]]] = [[] for _ in dialogues]
    beats = [(d["start"], d["end"]) for d in dialogues]
    for sp in spans:
        best = max(
            range(len(beats)),
            key=lambda i: (
                _overlap(sp, beats[i]),
                -abs((sp[0] + sp[1]) / 2 - (beats[i][0] + beats[i][1]) / 2),
            ),
        )
        groups[best].append(sp)
    times: list[tuple[float, float]] = []
    for i, grp in enumerate(groups):
        if grp:
            times.append((grp[0][0], grp[-1][1]))
        else:
            times.append(beats[i])
    return times


def align_dialogue_to_speech(dialogues: list[dict], speech: list[tuple[float, float]], duration: float) -> list[dict]:
    """Map script lines onto actual speech islands in the generated audio."""
    n = len(dialogues)
    if n == 0:
        return []
    spans = [(s, e) for s, e in speech if e - s >= 0.16]
    spans = _merge_speech_gaps(spans, 0.22)
    beat_window = (dialogues[0]["start"], dialogues[-1]["end"])
    # Opening click / breath: drop only if too short to be a real line.
    if (
        len(spans) >= 2
        and spans[0][0] <= 0.08
        and (spans[0][1] - spans[0][0]) < 0.40
        and spans[1][0] - spans[0][1] > 0.35
        and len(spans) - 1 >= n
    ):
        spans = spans[1:]
    # Crying / noise before the scripted line: do not stretch a later utterance backward.
    while (
        n == 1
        and len(spans) >= 2
        and spans[0][1] < beat_window[0] - 0.20
        and spans[1][0] - spans[0][1] > 0.45
    ):
        spans = spans[1:]

    def padded(start: float, end: float) -> tuple[float, float]:
        return (max(0.0, start - 0.04), min(duration, end + 0.06))

    def apply_times(times: list[tuple[float, float]]) -> list[dict]:
        fixed: list[tuple[float, float]] = []
        for i, (start, end) in enumerate(times):
            if i + 1 < len(times) and end > times[i + 1][0]:
                end = times[i + 1][0]
            start, end = padded(start, end)
            if end - start < 0.18:
                end = min(duration, start + 0.35)
            fixed.append((start, end))
        for i in range(len(fixed) - 1):
            start, end = fixed[i]
            nxt = fixed[i + 1][0]
            if end > nxt:
                fixed[i] = (start, nxt)
        out = []
        for d, (start, end) in zip(dialogues, fixed):
            out.append({**d, "start": start, "end": end})
        return out

    if len(spans) == 1 and (spans[0][1] - spans[0][0]) > 0.72 * duration:
        return dialogues
    speech_cover = sum(e - s for s, e in spans)
    # Too little detected voice vs. number of lines → keep script beats
    # rather than flashing several subtitles inside one short blip.
    if n > 1 and speech_cover < max(0.55, 0.38 * n):
        return dialogues
    if n == 1:
        if not spans:
            return dialogues
        # One subtitle line should last for the whole utterance, including
        # mid-sentence pauses (H3 often splits one sentence into bursts).
        return apply_times([(spans[0][0], spans[-1][1])])
    if len(spans) > n:
        return apply_times(_assign_extra_spans(dialogues, spans))
    if len(spans) == n:
        return apply_times(spans)
    if not spans:
        return dialogues
    union_s, union_e = spans[0][0], spans[-1][1]
    beat0, beat1 = beat_window
    beat_span = max(0.01, beat1 - beat0)
    mapped = []
    for d in dialogues:
        start = union_s + (d["start"] - beat0) / beat_span * (union_e - union_s)
        end = union_s + (d["end"] - beat0) / beat_span * (union_e - union_s)
        mapped.append((start, end))
    return apply_times(mapped)


def probe_duration(video: Path) -> float | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 3600 * 100)
    m, cs = divmod(cs, 60 * 100)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def escape_ass(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def render_ass(events: list[dict], video_duration: float | None) -> str:
    play_x, play_y = ASS_PLAY_RES
    limit = video_duration if video_duration and video_duration > 0 else None
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_x}",
        f"PlayResY: {play_y}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # 对白：底部居中白字黑边，相对 1280×720 原片约 7.8% 底边距
        "Style: Dialogue,Microsoft YaHei,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,1,2.4,0,2,40,40,56,1",
        # 解释性标题：黄金字、更靠上，相对原片约 29% 底边距
        "Style: Title,Microsoft YaHei,56,&H0000D7FF,&H000000FF,&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,1,3.2,0,2,40,40,211,1",
        # 片尾字卡：画面正中白字
        "Style: EndCard,Microsoft YaHei,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,1,2.4,0,5,40,40,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for ev in events:
        start = ev["start"]
        end = ev["end"]
        if limit is not None:
            if start >= limit:
                continue
            end = min(end, limit)
        if end <= start:
            continue
        layer = 0
        if ev["style"] == "Title":
            layer = 1
        elif ev["style"] == "EndCard":
            layer = 2
        lines.append(
            f"Dialogue: {layer},{ass_timestamp(start)},{ass_timestamp(end)},"
            f"{ev['style']},,0,0,0,,{escape_ass(ev['text'])}"
        )
    lines.append("")
    return "\n".join(lines)


def write_clip_ass(clip: dict, video: Path) -> Path | None:
    skipped = skip_subs(clip)
    if skipped:
        log(f"[SUBS] skip {clip['id']} ({skipped} 不按原片出字幕)")
        return None

    duration = probe_duration(video)
    events, missing = clip_timeline_events(clip, duration, video)
    for item in missing:
        log(f"[SUBS-NEED] {clip['id']}: {item}")
    if not events:
        log(f"[SUBS] skip {clip['id']} (没有可写的对白或标题)")
        return None
    ass_path = video.with_suffix(".ass")
    ass_path.write_text(render_ass(events, duration), encoding="utf-8")
    log(f"[SUBS] {clip['id']} -> {ass_path} events={len(events)}")
    return ass_path


def run_clip(template: dict, clip: dict, info: dict) -> Path:
    client_id = str(uuid.uuid4())
    prompt = build_prompt(template, clip)
    settings = f"{MEGAPIXELS} MP · {STEPS} steps · {clip['duration']:.2f}s · {ASPECT}"
    info["settings"] = settings
    info["prompt_chars"] = len(clip["prompt"])
    log(f"[QUEUE] {clip['id']} steps={STEPS} mp={MEGAPIXELS} duration={clip['duration']:.2f}s {ASPECT} prompt_chars={len(clip['prompt'])}")
    prompt_id = queue_prompt(prompt, client_id)
    info["prompt_id"] = prompt_id
    log(f"[WAIT] {clip['id']} prompt_id={prompt_id}")
    item = wait_job(prompt_id)
    src = output_files(item)[0]
    dest = copy_result(clip, src)
    log(
        f"[OK] {clip['id']} -> {dest} ({dest.stat().st_size} bytes) "
        f"src={src.name} loudnorm={AUDIO_TARGET_LUFS:.0f}LUFS"
    )
    try:
        write_clip_ass(clip, dest)
    except Exception as e:
        log(f"[SUBS-FAIL] {clip['id']}: {e}")
    return dest


def run_clip_with_retries(template_holder: list, clip: dict, info: dict) -> Path:
    consecutive_fail = 0
    attempt = 0
    started = time.time()
    while True:
        attempt += 1
        info["attempts"] = attempt
        try:
            if not comfy_up():
                restart_comfy()
                template_holder[0] = template_from_history()
            dest = run_clip(template_holder[0], clip, info)
            info["seconds"] = time.time() - started
            return dest
        except Exception as e:
            consecutive_fail += 1
            log(f"[FAIL] {clip['id']} attempt={attempt} consecutive={consecutive_fail}: {e}")
            NOTIFIER.clip_failed(clip["id"], attempt, consecutive_fail, str(e))
            if consecutive_fail == 1:
                log(f"[RETRY] {clip['id']} retrying without restart")
                time.sleep(5)
                continue
            log(f"[RETRY] {clip['id']} restarting ComfyUI then retrying")
            try:
                restart_comfy()
                template_holder[0] = template_from_history()
            except Exception as re:
                log(f"[FAIL] restart failed: {re}")
                time.sleep(10)
            consecutive_fail = 0
            time.sleep(3)


def extract_end_card(text: str) -> str:
    m = re.search(r"片尾字卡[\s\S]*?>\s*(.+)", text)
    return m.group(1).strip() if m else ""


def clip_timeline_events(clip: dict, video_duration: float | None, video: Path | None = None) -> tuple[list[dict], list[str]]:
    text = clip.get("text") or clip["path"].read_text(encoding="utf-8")
    events, missing = extract_dialogue_events(text)
    ov_path = overview_path(clip)
    ov_text = ov_path.read_text(encoding="utf-8") if ov_path.exists() else ""
    era = clip_era(clip, ov_text) if ov_text else ""
    title = extract_title(ov_text) if ov_text else ""
    limit = video_duration if video_duration and video_duration > 0 else float(clip["duration"])
    if not ov_path.exists():
        missing.append(f"同目录补 `{ov_path.name}`，并写「屏幕字」解释性标题和「分段文件」过去/现在")
    else:
        if not era:
            missing.append("在段总表「分段文件」标明本 clip 是过去还是现在")
        if era == "现在" and not title:
            missing.append("在段总表「屏幕字」补解释性标题原文")
    dialogues = [ev for ev in events if ev.get("style", "Dialogue") == "Dialogue"]
    if video is not None and video.exists() and dialogues:
        try:
            speech = detect_speech_ranges(video)
            dialogues = align_dialogue_to_speech(dialogues, speech, limit)
        except Exception as e:
            log(f"[SUBS] speech-align fallback {clip['id']}: {e}")
    out: list[dict] = []
    for ev in dialogues:
        start = ev["start"]
        end = ev["end"]
        if start >= limit:
            continue
        end = min(end, limit)
        if end <= start:
            continue
        out.append({**ev, "start": start, "end": end, "style": "Dialogue"})
    if era == "现在" and title:
        out.append({"text": title, "start": 0.0, "end": limit, "style": "Title"})
    return out, missing


def shift_events(events: list[dict], offset: float) -> list[dict]:
    shifted = []
    for ev in events:
        shifted.append({**ev, "start": ev["start"] + offset, "end": ev["end"] + offset})
    return shifted


def run_ffmpeg(cmd: list[str], label: str) -> None:
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace")[-2000]
        raise RuntimeError(f"{label} failed: {err}")


def decode_clip_audio(video: Path, duration: float) -> np.ndarray:
    """Normalised mono PCM, trimmed to exactly the video length, no fades."""
    chain = audio_filter_chain(video, duration, fades=False)
    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(video),
            "-af",
            chain,
            "-ac",
            "1",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-f",
            "s16le",
            "-v",
            "error",
            "-",
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace")[-1000:]
        raise RuntimeError(f"decode audio failed for {video.name}: {err}")
    return np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _frame_db(samples: np.ndarray, frame: int) -> np.ndarray:
    usable = len(samples) // frame * frame
    if usable == 0:
        return np.array([AUDIO_SILENCE_DB], dtype=np.float32)
    rms = np.sqrt((samples[:usable].reshape(-1, frame) ** 2).mean(axis=1))
    return 20 * np.log10(rms + 1e-9)


def ambience_floor(samples: np.ndarray) -> float:
    frame = int(AUDIO_SAMPLE_RATE * 0.010)
    levels = _frame_db(samples, frame)
    levels = levels[levels > AUDIO_SILENCE_DB]
    return float(np.percentile(levels, 20)) if levels.size else AUDIO_SILENCE_DB


def match_ambience(samples: np.ndarray, floor: float, target: float) -> tuple[np.ndarray, float]:
    """Move only the between-words material toward the group's median floor."""
    delta = float(np.clip(target - floor, -AUDIO_AMBIENCE_MAX_CUT_DB, AUDIO_AMBIENCE_MAX_LIFT_DB))
    if abs(delta) < 1.0:
        return samples, 0.0
    frame = int(AUDIO_SAMPLE_RATE * 0.010)
    levels = _frame_db(samples, frame)
    low, high = floor + 4.0, floor + 18.0
    weight = np.clip((high - levels) / (high - low), 0.0, 1.0)
    gain_db = delta * weight
    # A ~120ms moving average keeps the gain too slow to pump on speech onsets.
    window = int(0.12 * AUDIO_SAMPLE_RATE / frame) | 1
    gain_db = np.convolve(gain_db, np.ones(window) / window, mode="same")
    gain = np.repeat(10 ** (gain_db / 20), frame).astype(np.float32)
    if gain.size < samples.size:
        gain = np.concatenate([gain, np.full(samples.size - gain.size, gain[-1] if gain.size else 1.0)])
    return samples * gain[: samples.size], delta


def silent_tail_len(samples: np.ndarray) -> int:
    frame = int(AUDIO_SAMPLE_RATE * 0.001)
    levels = _frame_db(samples, frame)
    count = 0
    while count < levels.size and levels[levels.size - 1 - count] < AUDIO_SILENCE_DB:
        count += 1
    return min(count * frame, int(AUDIO_SAMPLE_RATE * AUDIO_BRIDGE_MAX_GAP_SEC))


def silent_head_len(samples: np.ndarray) -> int:
    """Some clips open on silence; the bridge has to stay up that long too."""
    frame = int(AUDIO_SAMPLE_RATE * 0.001)
    levels = _frame_db(samples, frame)
    count = 0
    while count < levels.size and levels[count] < AUDIO_SILENCE_DB:
        count += 1
    return min(count * frame, int(AUDIO_SAMPLE_RATE * AUDIO_BRIDGE_MAX_GAP_SEC))


def room_tone(samples: np.ndarray, want: int, at_end: bool) -> np.ndarray:
    """Quietest non-silent stretch near one edge, tiled to `want` samples."""
    span = int(AUDIO_SAMPLE_RATE * 0.8)
    region = samples[-span:] if at_end else samples[:span]
    window = max(want, int(AUDIO_SAMPLE_RATE * 0.020))
    if region.size < window * 2:
        region = samples
    floor_amp = 10 ** (AUDIO_SILENCE_DB / 20)
    step = max(1, window // 4)
    best: np.ndarray | None = None
    best_rms = None
    for start in range(0, max(1, region.size - window), step):
        chunk = region[start : start + window]
        rms = float(np.sqrt((chunk ** 2).mean()))
        if rms < floor_amp:
            continue
        if best_rms is None or rms < best_rms:
            best, best_rms = chunk, rms
    if best is None:
        return np.zeros(want, dtype=np.float32)
    reps = int(np.ceil(want / best.size)) + 1
    return np.tile(best, reps)[:want].astype(np.float32)


def build_concat_audio(
    videos: list[Path],
    durations: list[float],
    tmp: Path,
    tail_silence: float = 0.0,
) -> Path:
    """One normalised, continuous audio track for the whole cut.

    Two things make raw butt-joins audible: every clip ends with ~30ms of
    digital silence, and loudness normalisation leaves the room tone up to
    26 dB apart between clips. So the floors are pulled together first, then
    each cut is bridged with room tone under an equal-power crossfade.
    """
    tracks = [decode_clip_audio(video, duration) for video, duration in zip(videos, durations)]
    floors = [ambience_floor(track) for track in tracks]
    target = float(np.median(floors))
    matched: list[np.ndarray] = []
    for video, track, floor in zip(videos, tracks, floors):
        adjusted, delta = match_ambience(track, floor, target)
        if delta:
            log(f"[AUDIO] {video.name} 底噪 {floor:.1f} dB {delta:+.1f} dB")
        matched.append(adjusted)

    lengths = [track.size for track in matched]
    total = sum(lengths) + int(round(tail_silence * AUDIO_SAMPLE_RATE))
    out = np.zeros(total, dtype=np.float32)
    head = max(1, int(AUDIO_SAMPLE_RATE * AUDIO_FADE_IN_SEC))
    pos = 0
    for track in matched:
        seg = track.copy()
        ramp = min(head, seg.size)
        seg[:ramp] *= np.sin(np.linspace(0, np.pi / 2, ramp))
        out[pos : pos + seg.size] += seg
        pos += seg.size

    pre = int(AUDIO_SAMPLE_RATE * AUDIO_BRIDGE_PRE_SEC)
    post = int(AUDIO_SAMPLE_RATE * AUDIO_BRIDGE_POST_SEC)
    pos = 0
    bridged = 0
    for i in range(len(matched) - 1):
        pos += lengths[i]
        gap = silent_tail_len(matched[i]) or int(AUDIO_SAMPLE_RATE * 0.008)
        lead = silent_head_len(matched[i + 1])
        start, stop = pos - gap - pre, pos + lead + post
        if start < 0 or stop > out.size:
            continue
        span = stop - start
        ramp = np.linspace(0, np.pi / 2, span)
        fill = room_tone(matched[i], span, True) * np.cos(ramp)
        fill += room_tone(matched[i + 1], span, False) * np.sin(ramp)
        # Only fill the hole: taper the bridge back out where real audio lives,
        # and taper the real audio in the other direction so power stays flat.
        envelope = np.ones(span, dtype=np.float32)
        envelope[:pre] = np.sin(np.linspace(0, np.pi / 2, pre))
        envelope[span - post :] = np.cos(np.linspace(0, np.pi / 2, post))
        out[start:stop] *= np.concatenate(
            [
                np.cos(np.linspace(0, np.pi / 2, pre)),
                np.zeros(span - pre - post, dtype=np.float32),
                np.sin(np.linspace(0, np.pi / 2, post)),
            ]
        )
        out[start:stop] += fill * envelope
        bridged += 1

    tail = min(int(AUDIO_SAMPLE_RATE * AUDIO_FADE_OUT_SEC), sum(lengths))
    end = sum(lengths)
    out[end - tail : end] *= np.cos(np.linspace(0, np.pi / 2, tail))

    peak = float(np.abs(out).max())
    if peak > AUDIO_PEAK_CEILING:
        out *= AUDIO_PEAK_CEILING / peak
    log(f"[AUDIO] 接缝桥接 {bridged}/{max(0, len(matched) - 1)} 处 峰值={20 * np.log10(max(peak, 1e-9)):.2f} dBFS")

    pcm = (np.clip(out, -1.0, 1.0) * 32767.0).astype("<i2")
    stereo = np.repeat(pcm[:, None], 2, axis=1).tobytes()
    out_path = tmp / "joined.wav"
    with wave.open(str(out_path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(AUDIO_SAMPLE_RATE)
        handle.writeframes(stereo)
    return out_path


def decode_pcm(src: Path, channels: int = 1) -> np.ndarray:
    """Raw samples from any media file, mono or interleaved stereo."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(src),
            "-ac",
            str(channels),
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-f",
            "s16le",
            "-v",
            "error",
            "-",
        ],
        capture_output=True,
    )
    data = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        return data[: data.size // channels * channels].reshape(-1, channels)
    return data


def stereo_spread(samples: np.ndarray) -> tuple[float, float]:
    """(side/mid in dB, L/R correlation). Decorrelated voice reads as doubled."""
    left, right = samples[:, 0], samples[:, 1]
    if np.abs(left - right).max() < 1e-6:
        return -120.0, 1.0
    mid = (left + right) / 2
    side = (left - right) / 2
    mid_rms = float(np.sqrt((mid ** 2).mean()))
    side_rms = float(np.sqrt((side ** 2).mean()))
    ratio = 20 * np.log10(side_rms / (mid_rms + 1e-12) + 1e-12)
    return float(ratio), float(np.corrcoef(left, right)[0, 1])


def seam_metrics(mono: np.ndarray, offsets: list[float]) -> tuple[list[float], list[float]]:
    """Per-join dropout length in ms and background level step in dB.

    The dropout only counts as a seam defect when the material on *both* sides
    is louder than the dip; a clip that genuinely starts on silence would
    otherwise be reported as a hole.
    """
    hop = int(AUDIO_SAMPLE_RATE * 0.005)
    edge = int(0.040 * AUDIO_SAMPLE_RATE)
    flank = int(0.150 * AUDIO_SAMPLE_RATE)
    window = int(0.6 * AUDIO_SAMPLE_RATE)
    gaps: list[float] = []
    steps: list[float] = []

    def level(chunk: np.ndarray) -> np.ndarray:
        usable = chunk.size // hop * hop
        if usable == 0:
            return np.array([], dtype=np.float32)
        frames = chunk[:usable].reshape(-1, hop)
        return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)

    for offset in offsets:
        cut = int(offset * AUDIO_SAMPLE_RATE)
        if cut - window < 0 or cut + window > mono.size:
            continue
        before = level(mono[cut - window : cut - edge])
        after = level(mono[cut + edge : cut + window])
        if not before.size or not after.size:
            continue
        steps.append(abs(float(np.percentile(after, 25)) - float(np.percentile(before, 25))))
        near = level(mono[cut - edge : cut + edge])
        pre = level(mono[cut - edge - flank : cut - edge])
        post = level(mono[cut + edge : cut + edge + flank])
        if not near.size or not pre.size or not post.size:
            continue
        # A low percentile approximates the room tone; the median would be
        # dragged up by speech and turn every pause between lines into a "hole".
        reference = min(float(np.percentile(pre, 20)), float(np.percentile(post, 20)))
        gaps.append(float((near < reference - 15).sum()) * 5.0)
    return gaps, steps


def reference_video(explicit: Path | None) -> Path | None:
    if explicit:
        return explicit if explicit.exists() else None
    if not REFERENCE_FILE.exists():
        return None
    try:
        table = json.loads(REFERENCE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    key = run_key(CLIP_FILTER)
    path = table.get(key) or table.get(CLIP_FILTER) or table.get("default")
    return Path(path) if path and Path(path).exists() else None


def audio_report(out_mp4: Path, offsets: list[float], ref: Path | None) -> tuple[list[str], list[str]]:
    """Objective checks on the finished cut. Returns (lines, warnings)."""
    lines: list[str] = []
    warnings: list[str] = []

    stats = measure_loudness(out_mp4)
    if stats:
        integrated = float(stats["input_i"])
        true_peak = float(stats["input_tp"])
        lines.append(f"响度 {integrated:.2f} LUFS · 真峰值 {true_peak:.2f} dBTP · LRA {stats['input_lra']}")
        if true_peak > -0.5:
            warnings.append(f"真峰值 {true_peak:.2f} dBTP 偏高，可能削波")
        if abs(integrated - AUDIO_TARGET_LUFS) > 3.0:
            warnings.append(f"整体响度 {integrated:.2f} LUFS 偏离目标 {AUDIO_TARGET_LUFS:.0f} 超过 3 dB")

    stereo = decode_pcm(out_mp4, 2)
    if stereo.size:
        spread, corr = stereo_spread(stereo)
        lines.append(f"立体声 side/mid {spread:.1f} dB · L/R 相关 {corr:+.4f}")
        if corr < 0.99:
            warnings.append(f"左右声道相关度 {corr:.4f} 偏低，人声可能发散")

    mono = stereo[:, 0] if stereo.ndim == 2 and stereo.size else decode_pcm(out_mp4)
    if mono.size and offsets:
        gaps, steps = seam_metrics(mono, offsets)
        if gaps:
            bad = sum(1 for g in gaps if g >= 15)
            lines.append(
                f"接缝 {len(gaps)} 处：空洞中位 {np.median(gaps):.0f}ms 最大 {max(gaps):.0f}ms；"
                f"底噪台阶中位 {np.median(steps):.1f}dB 最大 {max(steps):.1f}dB"
            )
            if bad:
                warnings.append(f"{bad} 处接缝仍有 15ms 以上的静音空洞")

    video_len = probe_duration(out_mp4)
    audio_len = mono.size / AUDIO_SAMPLE_RATE if mono.size else 0.0
    if video_len:
        drift = (audio_len - video_len) * 1000
        lines.append(f"音画时长 视频 {video_len:.3f}s · 音频 {audio_len:.3f}s · 差 {drift:+.0f}ms")
        if abs(drift) > 60:
            warnings.append(f"音画时长相差 {drift:+.0f}ms，可能不同步")

    if ref:
        ref_stats = measure_loudness(ref)
        ref_stereo = decode_pcm(ref, 2)
        bits = [f"参考片 {ref.name}"]
        if ref_stats and stats:
            bits.append(
                f"响度差 {float(stats['input_i']) - float(ref_stats['input_i']):+.2f} LU"
            )
        if ref_stereo.size and stereo.size:
            ref_spread, _ = stereo_spread(ref_stereo)
            bits.append(f"side/mid 差 {spread - ref_spread:+.1f} dB")
        lines.append(" · ".join(bits))

    return lines, warnings


def subtitle_report(clips: list[dict], videos: list[Path], needs: list[str]) -> tuple[list[str], list[str]]:
    """Speech the burnt-in subtitles never cover, plus unresolved SUBS-NEED notes."""
    lines: list[str] = []
    warnings: list[str] = []
    uncovered_total = 0.0
    for clip, video in zip(clips, videos):
        duration = probe_duration(video)
        events, _ = clip_timeline_events(clip, duration, video)
        spans = [(e["start"], e["end"]) for e in events if e.get("style") == "Dialogue"]
        try:
            speech = detect_speech_ranges(video)
        except Exception:
            continue
        holes = []
        for start, end in speech:
            covered = sum(
                max(0.0, min(end, b) - max(start, a))
                for a, b in spans
            )
            # Clips routinely open with a sigh or a breath that the alignment
            # deliberately drops, so only flag stretches long enough to be a line.
            if (end - start) - covered > 0.4:
                holes.append((start, end))
        if holes:
            uncovered_total += sum(e - s for s, e in holes)
            spots = "，".join(f"{s:.2f}-{e:.2f}s" for s, e in holes[:3])
            warnings.append(f"{clip['segment']}/{clip['stem']} 有语音无字幕：{spots}")
    lines.append(f"字幕未覆盖语音合计 {uncovered_total:.2f}s")
    if needs:
        lines.append(f"脚本缺对白/时间码 {len(needs)} 处")
        warnings.extend(f"脚本待补：{item}" for item in needs[:5])
    return lines, warnings


def prune_raw_outputs(keep_per_clip: int = 1) -> None:
    """ComfyUI keeps every render; only the newest per clip is worth holding."""
    raw_dir = COMFY / "output" / "video" / "scripts"
    if not raw_dir.exists():
        return
    groups: dict[str, list[Path]] = {}
    for path in raw_dir.glob("*.mp4"):
        stem = re.sub(r"_\d+_?$", "", path.stem)
        groups.setdefault(stem, []).append(path)
    removed = 0
    freed = 0
    for paths in groups.values():
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for path in paths[keep_per_clip:]:
            freed += path.stat().st_size
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        log(f"[CLEAN] ComfyUI 原始输出删除 {removed} 个，释放 {freed / 1_000_000:.1f} MB")


def concat_finished_clips(clips: list[dict], out_mp4: Path, reference: Path | None = None) -> Path:
    if not clips:
        raise RuntimeError("no clips to concat")
    videos: list[Path] = []
    durations: list[float] = []
    all_events: list[dict] = []
    kept: list[dict] = []
    subs_needed: list[str] = []
    offset = 0.0
    missing_videos: list[str] = []
    for clip in clips:
        video = clip_output_path(clip)
        if not video.exists() or video.stat().st_size < OUTPUT_MIN_BYTES:
            missing_videos.append(clip["id"])
            continue
        try:
            stats = measure_loudness(video)
            # Loose tolerance: loudnorm lands within ~1 dB, and re-running the
            # concat should not re-encode every clip again.
            if stats and abs(float(stats["input_i"]) - AUDIO_TARGET_LUFS) > 1.5:
                normalize_clip_audio(video, video)
                log(f"[AUDIO] {video.name} {stats['input_i']} -> {AUDIO_TARGET_LUFS:.1f} LUFS")
        except Exception as e:
            log(f"[AUDIO-FAIL] {clip['id']}: {e}")
        videos.append(video)
        kept.append(clip)
        duration = probe_duration(video) or float(clip["duration"])
        durations.append(duration)
        events, missing = clip_timeline_events(clip, duration, video)
        for item in missing:
            log(f"[SUBS-NEED] {clip['id']}: {item}")
            subs_needed.append(f"{clip['segment']}/{clip['stem']} {item}")
        try:
            write_clip_ass(clip, video)
        except Exception as e:
            log(f"[SUBS-FAIL] {clip['id']}: {e}")
        all_events.extend(shift_events(events, offset))
        log(f"[CONCAT] +{duration:.3f}s offset={offset:.3f}s {video.name} events={len(events)}")
        offset += duration

    if missing_videos:
        raise RuntimeError(f"missing rendered mp4s: {missing_videos}")
    if not videos:
        raise RuntimeError("no rendered mp4s found")

    work_ov = clips[0]["path"].parent.parent / "00-overview.md"
    end_card = extract_end_card(work_ov.read_text(encoding="utf-8")) if work_ov.exists() else ""
    end_card_sec = 1.2
    if end_card:
        all_events.append(
            {
                "text": end_card,
                "start": offset,
                "end": offset + end_card_sec,
                "style": "EndCard",
            }
        )

    tmp = LOG_DIR / "concat_work"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    list_path = tmp / "concat.txt"
    lines = []
    for video in videos:
        lines.append(f"file '{video.resolve().as_posix()}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    joined = tmp / "joined_v.mp4"
    run_ffmpeg(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-an", "-c", "copy", str(joined)],
        "concat video",
    )

    if end_card:
        end_mp4 = tmp / "endcard.mp4"
        run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s=864x480:d={end_card_sec:.2f}:r=24",
                "-t",
                f"{end_card_sec:.2f}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(end_mp4),
            ],
            "end card",
        )
        with_end = tmp / "joined_end.mp4"
        list2 = tmp / "concat2.txt"
        list2.write_text(
            f"file '{joined.resolve().as_posix()}'\nfile '{end_mp4.resolve().as_posix()}'\n",
            encoding="utf-8",
        )
        run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list2),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(with_end),
            ],
            "concat end card",
        )
        joined = with_end
        offset += end_card_sec

    audio = build_concat_audio(videos, durations, tmp, end_card_sec if end_card else 0.0)

    ass_path = out_mp4.with_suffix(".ass")
    ass_path.write_text(render_ass(all_events, offset), encoding="utf-8")
    ass_for_ffmpeg = (tmp / "burn.ass").resolve()
    shutil.copy2(ass_path, ass_for_ffmpeg)
    # libass on Windows needs escaped path: drive colon and backslashes.
    ass_filter = ass_for_ffmpeg.as_posix().replace(":", r"\:")
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(joined),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            f"ass='{ass_filter}'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            AUDIO_BITRATE,
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-shortest",
            "-movflags",
            "+faststart",
            str(out_mp4),
        ],
        "burn subtitles",
    )
    log(f"[CONCAT] wrote {out_mp4} ({out_mp4.stat().st_size} bytes) ass={ass_path} clips={len(videos)} duration={offset:.2f}s")

    seam_offsets = [sum(durations[: i + 1]) for i in range(len(durations) - 1)]
    lines: list[str] = []
    warnings: list[str] = []
    for check, args in (
        (audio_report, (out_mp4, seam_offsets, reference_video(reference))),
        (subtitle_report, (kept, videos, subs_needed)),
    ):
        try:
            got_lines, got_warnings = check(*args)
            lines += got_lines
            warnings += got_warnings
        except Exception as e:
            lines.append(f"{check.__name__} 未能完成：{e}")
    for line in lines:
        log(f"[QC] {line}")
    for item in warnings:
        log(f"[QC-WARN] {item}")

    shutil.rmtree(tmp, ignore_errors=True)
    if not KEEP_RAW:
        prune_raw_outputs()

    NOTIFIER.concat_done(out_mp4, ass_path, len(videos), offset, lines, warnings)
    return out_mp4


def parse_args(argv: list[str] | None = None):
    p = argparse.ArgumentParser(description="Queue MiniMax H3 Turbo t2v jobs from video-script clips.")
    p.add_argument("--filter", default="", help="Only clips whose relative path contains this string.")
    p.add_argument("--exclude", default="", help="Skip clips whose relative path contains this string.")
    p.add_argument("--megapixels", type=float, default=MEGAPIXELS)
    p.add_argument("--steps", type=int, default=STEPS)
    p.add_argument("--output-dir", type=Path, default=None, help="Copy finished mp4s here instead of each clip folder.")
    p.add_argument(
        "--concat-only",
        action="store_true",
        help="Skip generation; concat already-rendered mp4s, merge ASS timelines, burn subtitles.",
    )
    p.add_argument(
        "--concat-out",
        type=Path,
        default=None,
        help="Final concatenated mp4 path. Default: Downloads/<filter-name>-成片.mp4",
    )
    p.add_argument(
        "--fresh",
        action="store_true",
        help="Re-render every clip instead of reusing matching mp4s from a previous run.",
    )
    p.add_argument(
        "--reference",
        type=Path,
        default=None,
        help="Reference cut to compare loudness and stereo width against. Default: logs/reference.json.",
    )
    p.add_argument(
        "--keep-raw",
        action="store_true",
        help="Keep every ComfyUI render instead of pruning to the newest per clip.",
    )
    p.add_argument(
        "--no-notify",
        action="store_true",
        help="Skip progress/completion mail even when logs/smtp.json exists.",
    )
    p.add_argument(
        "--notify-label",
        default="",
        help="Subject prefix for the mails. Default: the last part of --filter.",
    )
    return p.parse_args(argv)


def default_concat_out(filter_text: str) -> Path:
    downloads = Path.home() / "Downloads"
    slug = filter_text.replace("/", "-").replace("\\", "-").strip("-") or "成片"
    return downloads / f"{slug}-成片.mp4"


def notify_label(filter_text: str, override: str) -> str:
    if override:
        return override
    parts = [p for p in filter_text.replace("\\", "/").split("/") if p]
    return parts[-1] if parts else "任务"


def main(argv: list[str] | None = None) -> int:
    global MEGAPIXELS, STEPS, OUTPUT_DIR, CLIP_FILTER, CLIP_EXCLUDE, KEEP_RAW, NOTIFIER
    args = parse_args(argv)
    MEGAPIXELS = args.megapixels
    STEPS = args.steps
    CLIP_FILTER = args.filter
    CLIP_EXCLUDE = args.exclude
    KEEP_RAW = args.keep_raw

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    progress = load_progress()
    last = (progress.get("runs") or {}).get(run_key(CLIP_FILTER)) or {}
    output_dir = args.output_dir
    if output_dir is None and last.get("output_dir"):
        output_dir = Path(last["output_dir"])
        log(f"[RESUME] 沿用上次的输出目录 {output_dir}")
    OUTPUT_DIR = output_dir.expanduser().resolve() if output_dir else None
    if OUTPUT_DIR is not None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    NOTIFIER = Notifier(
        label=notify_label(CLIP_FILTER, args.notify_label),
        enabled=not args.no_notify,
    )
    clips = load_clips()
    concat_out = args.concat_out or (Path(last["concat_out"]) if last.get("concat_out") else None)
    out_mp4 = (concat_out or default_concat_out(CLIP_FILTER)).expanduser().resolve()
    dest_dir = str(OUTPUT_DIR) if OUTPUT_DIR else "clip folders"
    try:
        if args.concat_only:
            if args.output_dir:
                remember_run(progress, args)
                save_progress(progress)
            NOTIFIER.total = len(clips)
            NOTIFIER.out_mp4 = out_mp4
            NOTIFIER.started_at = datetime.now()
            log(f"[CONCAT] {len(clips)} clips -> {out_mp4}")
            concat_finished_clips(clips, out_mp4, args.reference)
            return 0

        remember_run(progress, args)
        if args.fresh:
            progress["done"] = {}
        save_progress(progress)
        reusable = {} if args.fresh else {
            c["id"]: p for c in clips if (p := reusable_output(c, progress)) is not None
        }
        todo = len(clips) - len(reusable)
        log(
            f"[START] {len(clips)} clips from {SCRIPT_ROOT} "
            f"filter={CLIP_FILTER!r} exclude={CLIP_EXCLUDE!r} mp={MEGAPIXELS} steps={STEPS} out={dest_dir} "
            f"reuse={len(reusable)} todo={todo}"
        )
        NOTIFIER.run_started(
            len(clips), out_mp4, dest_dir, f"{MEGAPIXELS} MP · {STEPS} steps · {ASPECT}", len(reusable)
        )

        template_holder: list = []
        for i, clip in enumerate(clips, 1):
            if clip["id"] in reusable:
                log(f"[SKIP] {i}/{len(clips)} {clip['id']} 已有成片，跳过")
                continue
            log(f"[CLIP] {i}/{len(clips)} {clip['id']}")
            if not template_holder:
                if not comfy_up():
                    restart_comfy()
                template_holder = [template_from_history()]
            info: dict = {"next_label": clips[i]["id"] if i < len(clips) else ""}
            dest = run_clip_with_retries(template_holder, clip, info)
            progress.setdefault("done", {})[clip["id"]] = {
                "output": str(dest),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "megapixels": MEGAPIXELS,
                "steps": STEPS,
                "duration": clip["duration"],
                "aspect": ASPECT,
            }
            save_progress(progress)
            NOTIFIER.clip_done(i, clip["id"], dest, info)

        missing = [c["id"] for c in clips if c["id"] not in progress.get("done", {})]
        if missing:
            log(f"[INCOMPLETE] still missing: {missing}")
            NOTIFIER.run_incomplete(missing)
            return 1
        log("[DONE] all clips processed")
        log(f"[CONCAT] {len(clips)} clips -> {out_mp4}")
        concat_finished_clips(clips, out_mp4, args.reference)
        return 0
    except BaseException as e:
        if not isinstance(e, (SystemExit, KeyboardInterrupt)):
            NOTIFIER.run_crashed(e)
        raise
    finally:
        # The last mail carries the finished mp4, so give it time to upload.
        NOTIFIER.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("[STOP] interrupted")
        raise SystemExit(130)
