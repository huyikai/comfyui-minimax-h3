"""主对话 Read 图失败时的读图兜底。走 Cursor SDK，不经过 captioning 中转。

横条是三帧横拼（左=起、中=中、右=末），提示词里写死，避免被说成「同一画面重复」。

    .\\.venv\\Scripts\\python.exe tools\\see_image.py 图.jpg
    .\\.venv\\Scripts\\python.exe tools\\see_image.py --strip a.jpg b.jpg
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cursor_sdk_util import model_selection, prompt_with_retry, tokens_of  # noqa: E402

STRIP_HINT = """\
这张图是**同一条视频的三帧横向拼接**，从左到右分别是起、中、末。
不是三个镜头，也不是同画面复制三次。请分别描述左 / 中 / 右：
人有几个、各自嘴张没张、手脚在哪、有没有汉字/徽章/贴纸、有没有脚本要的物件（闪电、鞋踩的位置等）。
只写看见的，不要猜剧本。
"""

PLAIN_HINT = "描述这张图里实际看见的：人、嘴、手、道具、文字、异常。只写看见的。"


async def describe(client, path: Path, strip: bool, model, fallback: str | None) -> str:
    from cursor_sdk import SDKImage, UserMessage

    hint = STRIP_HINT if strip else PLAIN_HINT
    msg = UserMessage(text=hint, images=[SDKImage.from_file(str(path))])
    result, used, err = await prompt_with_retry(
        client, msg, model, fallback, str(path.parent),
    )
    if err:
        return f"[see_image FAIL] {path.name}: {err}"
    tok = tokens_of(result) or (0, 0)
    tag = used or "?"
    body = (result.result or "").strip()
    return f"## {path.name}  ·  {tag}  入{tok[0]} 出{tok[1]}\n{body}\n"


async def run(args) -> int:
    from cursor_sdk import AsyncClient

    missing = [p for p in args.images if not p.is_file()]
    if missing:
        print("找不到：" + ", ".join(str(p) for p in missing), file=sys.stderr)
        return 1
    model = model_selection(args.model, args.param)
    fallback = args.fallback or None
    parts = []
    async with await AsyncClient.launch_bridge(workspace=str(Path.cwd())) as client:
        for p in args.images:
            parts.append(await describe(client, p, args.strip, model, fallback))
    print("\n".join(parts))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SDK vision fallback when Read captioning is down.")
    p.add_argument("images", nargs="+", type=Path)
    p.add_argument("--strip", action="store_true", help="按三帧横条来读")
    p.add_argument("--model", default="gemini-3.7-flash")
    p.add_argument("--param", nargs="*", default=["effort=low"])
    p.add_argument("--fallback", default="composer-2.5")
    args = p.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
