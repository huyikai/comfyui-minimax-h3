"""Download MiniMax H3 weights with resumable curl (avoids stuck HF Xet)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "ComfyUI" / "models"
PRIMARY = "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main"
MIRROR = "https://hf-mirror.com/Comfy-Org/MiniMax-H3/resolve/main"
TURBO_PRIMARY = "https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora/resolve/main"
TURBO_MIRROR = "https://hf-mirror.com/larryvrh/MiniMax-H3-Turbo-Lora/resolve/main"
CURL = "curl.exe" if sys.platform == "win32" else "curl"
# Smallest first so a broken link fails quickly.
FILES = [
    "vae/minimax_h3_audio_vae_fp32.safetensors",
    "vae/minimax_h3_video_vae_fp16.safetensors",
    "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
]
REF2VA = "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors"
TURBO_LORA = "loras/minimax_h3_turbo_v4_step600_ema.safetensors"
TURBO_FILE = "minimax_h3_turbo_v4_step600_ema.safetensors"


def curl_get(url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        CURL,
        "-L",
        "--retry",
        "20",
        "--retry-all-errors",
        "--retry-delay",
        "2",
        "-C",
        "-",
        "--fail",
        "--progress-bar",
        "-o",
        str(dest),
        url,
    ]
    print(f"GET {url}", flush=True)
    return subprocess.call(cmd)


def download(rel: str, urls: list[str] | None = None) -> None:
    dest = ROOT / rel
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"skip existing {rel} ({dest.stat().st_size / 1e9:.2f} GB)", flush=True)
        return
    candidates = urls or [f"{PRIMARY}/{rel}", f"{MIRROR}/{rel}"]
    for url in candidates:
        code = curl_get(url, dest)
        if code == 0 and dest.exists() and dest.stat().st_size > 1_000_000:
            print(f"done {rel} ({dest.stat().st_size / 1e9:.2f} GB)", flush=True)
            return
        print(f"curl exit {code} from {url}", flush=True)
    raise SystemExit(f"failed to download {rel}")


def main() -> None:
    files = list(FILES)
    if "--ref2va" in sys.argv:
        files.append(REF2VA)
    for rel in files:
        download(rel)
    if "--turbo" in sys.argv:
        download(
            TURBO_LORA,
            [
                f"{TURBO_PRIMARY}/{TURBO_FILE}",
                f"{TURBO_MIRROR}/{TURBO_FILE}",
            ],
        )
    print("all requested weights ready", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
