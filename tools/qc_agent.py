r"""让 agent 读横条替你做分片级自检的初筛。

一条 clip 一个 run，把 qc_frames.py --dir 出的三帧横条连同该 clip 的中文脚本一起发过去，
按 finish-video 的四项出「现象清单」。**只筛不判**：跑在 plan 模式下，agent 改不了文件，
也被要求不给改法。被标出来的那几条，主 agent 自己开图再动手（Read 失败则 see_image.py）。不是问用户。

    .\.venv\Scripts\python.exe tools\qc_agent.py --work "人间隙/04-懦弱" --limit 3

同步 API 在 Windows 上有 select() 管道的 bug（cursor-sdk 1.0.28），这里走异步，顺带
拿到并发。需要 CURSOR_API_KEY。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cursor_sdk_util import (  # noqa: E402
    model_selection,
    prompt_with_retry,
    run_tag,
    tokens_of,
)
from scratch import scratch_dir  # noqa: E402

SCRIPT_ROOT = Path(r"D:\develop\video-script")
STRIP_RE = re.compile(r"^h3-t2v-turbo-(?P<seg>.+?)-(?P<clip>clip-\d+)$")
ENGLISH_BLOCK = re.compile(r"```text\s*\n.*?```", re.S)
VERDICT = re.compile(
    r"^\s*(人数|口型|内容|画面)\s*[:：]\s*(疑-点名|疑-拿不准|疑|过|无对白)\s*[—\-–]*\s*(.*)$"
)
SEEN = re.compile(r"^\s*(左|中|右)\s*[:：]\s*(.+)$")

CRITERIA = """\
**1 人数**：入画人数与该 clip 的「人数锁 / Identity lock」一致。画外音的人不占人数；
远处虚焦、看不清脸的背景人形不要算进锁。

**2 口型**：双向判断，两头都要看。
  - 该说话的人，嘴要在动；
  - 被写成只听不说的人，嘴要闭着——把台词演到错的那张嘴上是要报的；
  - 说话人的嘴**不在画面里**（被裁出画、背对镜头、面罩遮住）也要报：那等于有语音没有嘴。

**3 内容**：演的是脚本那件事——对着「表演节拍」逐拍看。
  脚本要求的**事件、物件、位置**没出现，照报：闪电没闪、该硬切没切、脚踩的位置
  跟脚本写的不是一个地方、指定的道具不在。这些跟采样疏密无关。
  只有一种情况可以判过：你的疑点仅仅是「动作幅度大小看不清」，那属于三帧采样太稀。

**4 画面**：报能点名的崩坏。常见的有：多指少指多肢、穿模（物体插进身体）、画面里生成了
真汉字或乱码字符（招牌、徽章、肩章、贴纸）、道具在三帧间跳变或长度改变、明显的融化、
背景出现不该有的异常物体或整齐重复排列的东西——**以及任何其他你能具体点名的异常，
不限于这几类**。
**但运动模糊、压缩发糊、手部柔焦、暗部噪点不算崩坏，不要因此报疑。**"""

PROMPT = """\
你在替一条 AI 生成的短剧分片做质检初筛。

**这张图是同一条 clip 的三帧横向拼接**，从左到右分别是这一拍的**起、中、末**。
不是三个镜头，也不是三个人。整条时长 {seconds}，所以左≈0秒、中≈中点、右≈结尾。

按下面四项逐项判断：

{criteria}

## 该 clip 的脚本（中文部分）

{script}

## 怎么回

**分两步，顺序不要颠倒。**

**第一步，先只描述你在三帧里实际看见了什么**——人在哪、手脚在哪、嘴张没张、有什么东西。
这一步**不要参照上面的脚本**，不要用脚本里的词，不要写「符合」「一致」这类判断。
就当脚本不存在，只当一个描述画面的人。

**第二步，再拿你自己刚写的描述去对脚本**，逐项判。
如果描述和脚本对不上，**以你看见的为准**——脚本是要求，不是答案。

**只报现象，不要给改法。** 你的输出会由人复核，所以宁可多报。三档怎么选：

- `过` —— 对得上
- `疑-点名` —— 能具体说出哪里不对（位置、数目、有没有出现）
- `疑-拿不准` —— 觉得不对但说不清，或者画面看不清没法判

严格按下面的格式回，两段都要，每行一句话，不要加别的段落：

看图:
  左: 你看见什么
  中: 你看见什么
  右: 你看见什么
人数: 过|疑-点名|疑-拿不准 — 现象
口型: 过|疑-点名|疑-拿不准|无对白 — 现象
内容: 过|疑-点名|疑-拿不准 — 现象
画面: 过|疑-点名|疑-拿不准 — 现象
"""


def clip_script(work: str, seg: str, clip: str) -> str | None:
    """取 clip 的中文部分。英文 prompt 块去掉：agent 要判的是画面对不对得上中文节拍。"""
    p = SCRIPT_ROOT / Path(work) / seg / f"{clip}.md"
    if not p.exists():
        return None
    return ENGLISH_BLOCK.sub("[英文 prompt 略]", p.read_text(encoding="utf-8")).strip()


def duration_of(script: str) -> str:
    m = re.search(r"时长：`([^`]+)`", script)
    return m.group(1) if m else "未标注"


def parse_report(text: str) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    seen: dict[str, str] = {}
    for line in text.splitlines():
        m = VERDICT.match(line)
        if m:
            out[m.group(1)] = (m.group(2), m.group(3).strip())
            continue
        s = SEEN.match(line)
        if s and s.group(1) not in seen:
            seen[s.group(1)] = s.group(2).strip()
    return out, seen


def named(verdicts: dict[str, tuple[str, str]]) -> list[str]:
    return [k for k, (s, _) in verdicts.items() if s == "疑-点名"]


def unsure(verdicts: dict[str, tuple[str, str]]) -> list[str]:
    return [k for k, (s, _) in verdicts.items() if s in ("疑-拿不准", "疑")]


async def judge(client, model, strip: Path, work: str, sem, fallback=None) -> dict:
    from cursor_sdk import SDKImage, UserMessage

    m = STRIP_RE.match(strip.stem)
    if not m:
        return {"strip": strip.name, "error": "文件名认不出段和 clip"}
    seg, clip = m.group("seg"), m.group("clip")
    script = clip_script(work, seg, clip)
    if script is None:
        return {"seg": seg, "clip": clip, "error": "找不到对应的 clip 脚本"}

    text = PROMPT.format(seconds=duration_of(script), criteria=CRITERIA, script=script)
    msg = UserMessage(text=text, images=[SDKImage.from_file(str(strip))])

    async with sem:
        result, used, err = await prompt_with_retry(
            client, msg, model, fallback, str(strip.parent)
        )
    if err:
        return {"seg": seg, "clip": clip, "error": err}
    tok = tokens_of(result)
    meta = {"tok_in": tok[0] if tok else None, "tok_out": tok[1] if tok else None,
            "ms": getattr(result, "duration_ms", None)}
    body = result.result or ""
    verdicts, seen = parse_report(body)
    return {"seg": seg, "clip": clip, "raw": body, "verdicts": verdicts, "seen": seen,
            "fell_back": fallback is not None and used is fallback, **meta}


async def run(args) -> int:
    from cursor_sdk import AsyncClient

    strips_dir = args.dir or scratch_dir(args.work, "qc/clips")
    strips = sorted(Path(strips_dir).glob("*.jpg"))
    if args.clips:
        strips = [s for s in strips if any(c in s.stem for c in args.clips)]
    if args.limit:
        strips = strips[: args.limit]
    if not strips:
        print(f"{strips_dir} 里没有横条，先跑 qc_frames.py --dir")
        return 1

    tag = run_tag(args.model, args.param)
    model = model_selection(args.model, args.param)
    fallback = args.fallback or None
    print(f"{len(strips)} 条 · 并发 {args.concurrency} · {tag}"
          + (f" · 兜底 {fallback}" if fallback else ""))
    sem = asyncio.Semaphore(args.concurrency)
    async with await AsyncClient.launch_bridge(workspace=str(Path.cwd())) as client:
        tasks = [judge(client, model, s, args.work, sem, fallback) for s in strips]
        rows = []
        for coro in asyncio.as_completed(tasks):
            row = await coro
            rows.append(row)
            label = row.get("clip") or row.get("strip")
            if row.get("error"):
                print(f"  [ERR ] {row.get('seg','')} {label}: {row['error']}")
            else:
                hard, soft = named(row["verdicts"]), unsure(row["verdicts"])
                mark = "点名" if hard else ("拿不准" if soft else " ok ")
                bits = "/".join(hard) + ("｜" + "/".join(soft) if hard and soft else "")
                print(f"  [{mark}] {row['seg']} {label}"
                      + (f" -> {bits or '/'.join(soft)}" if hard or soft else "")
                      + ("  (兜底模型)" if row.get("fell_back") else ""))

    # 点名的排前面：先看能说出哪里不对的，软疑点有余力再看。
    rows.sort(key=lambda r: (not named(r.get("verdicts") or {}),
                             not unsure(r.get("verdicts") or {}),
                             r.get("seg", ""), r.get("clip", "")))
    # 局部跑（--clips / --limit）单独落一份，别把满量那份报告冲掉。
    partial = "-部分" if (args.clips or args.limit) else ""
    out = scratch_dir(args.work, "qc") / f"agent-report-{tag}{partial}.md"
    t_in = sum(r.get("tok_in") or 0 for r in rows)
    t_out = sum(r.get("tok_out") or 0 for r in rows)
    secs = sum((r.get("ms") or 0) for r in rows) / 1000
    hard_rows = [r for r in rows if named(r.get("verdicts") or {})]
    soft_rows = [r for r in rows if not named(r.get("verdicts") or {})
                 and unsure(r.get("verdicts") or {})]
    errs = [r for r in rows if r.get("error")]
    bad = [r for r in rows if not r.get("error") and not r.get("verdicts")]
    lines = [f"# {args.work} 分片自检初筛（agent）", "",
             f"{tag} · {len(rows)} 条 · plan 模式（只读）",
             f"点名 {len(hard_rows)} 条 · 拿不准 {len(soft_rows)} 条 · "
             f"失败 {len(errs)} 条 · 格式不对 {len(bad)} 条",
             f"token 入 {t_in} 出 {t_out}（每条均入 {t_in // max(len(rows), 1)}）"
             f" · 累计 {secs:.0f}s", "",
             "点名的排在前面。**主 agent 自己开图再动手，不要问用户。**", ""]
    for r in rows:
        head = f"## {r.get('seg','?')} {r.get('clip') or r.get('strip')}"
        if r.get("fell_back"):
            head += f"  ·  主模型被拒，走了兜底 {fallback}"
        if r.get("tok_in") is not None:
            head += (f"  ·  入 {r['tok_in']} 出 {r['tok_out']}"
                     f" / {(r.get('ms') or 0) / 1000:.0f}s")
        lines.append(head)
        if r.get("error"):
            lines += ["", f"跑失败：{r['error']}", ""]
            continue
        for k in ("人数", "口型", "内容", "画面"):
            s, why = r["verdicts"].get(k, ("?", "没按格式回"))
            lines.append(f"- **{k}** `{s}` {why}")
        if r.get("seen"):
            lines.append("- 它看见的：" + " ／ ".join(
                f"{k} {v}" for k, v in r["seen"].items()))
        if not r["verdicts"]:
            lines += ["", "```", r.get("raw", "")[:1500], "```"]
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n报告 -> {out}")
    print(f"token 入 {t_in} 出 {t_out}（每条均入 {t_in // max(len(rows), 1)}）· 累计 {secs:.0f}s")
    print(f"点名 {len(hard_rows)} 条，拿不准 {len(soft_rows)} 条，"
          f"格式不对 {len(bad)} 条，跑失败 {len(errs)} 条。")
    print("**点名的那些主 agent 自己开图再动手（Read 失败则 tools/see_image.py --strip）。**")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Agent-assisted per-clip QC triage. Reports only.")
    p.add_argument("--work", required=True, help='例如 "人间隙/04-懦弱"')
    p.add_argument("--dir", type=Path, help="横条目录，默认 .scratch/<作品>/qc/clips")
    p.add_argument("--clips", nargs="*", help="只筛这几条，按文件名子串匹配")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 条，先小规模验证用")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--model", default="gemini-3.7-flash")
    p.add_argument("--param", nargs="*", default=[],
                   help="档位，例如 --param effort=low thinking=false；不给用默认变体")
    p.add_argument("--fallback", default="composer-2.5",
                   help="主模型被内容安全拒掉时换它再试一次；--fallback '' 关掉")
    args = p.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
