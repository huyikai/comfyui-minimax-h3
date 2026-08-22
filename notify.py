#!/usr/bin/env python3
"""Progress and completion mail for the video-script runs.

Sending happens on a worker thread so SMTP latency — a 20 MB attachment takes
half a minute — never stalls rendering. Credentials and recipient live in
logs/smtp.json, logs/notify_email.txt and logs/ntfy_topic.txt.
"""

from __future__ import annotations

import json
import queue
import smtplib
import threading
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
SMTP_FILE = LOG_DIR / "smtp.json"
EMAIL_FILE = LOG_DIR / "notify_email.txt"
TOPIC_FILE = LOG_DIR / "ntfy_topic.txt"
MAIL_LOG = LOG_DIR / "notify_mail.log"

# QQ/Foxmail rejects personal mail much above 50 MB.
MAX_ATTACH_BYTES = 45 * 1024 * 1024
SMTP_TIMEOUT_SEC = 180
DRAIN_TIMEOUT_SEC = 420


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def mail_log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with MAIL_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {msg}\n")


def fmt_bytes(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1000:
        return f"{n / 1000:.0f} KB"
    return f"{n} B"


def fmt_minutes(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分" if secs < 15 else f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def fmt_remain_short(seconds: float) -> str:
    minutes = max(0, int(round(seconds / 60)))
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}小时{minutes}分"
    return f"{minutes}分"


def clip_label(clip_id: str) -> str:
    parts = clip_id.replace("\\", "/").split("/")
    stem = parts[-1].replace(".md", "") if parts else clip_id
    segment = parts[-2] if len(parts) >= 2 else ""
    return f"{segment} {stem}".strip()


def send_email(title: str, body: str, attachments: list[Path] | None = None) -> str:
    if not SMTP_FILE.exists():
        return "no-smtp-config"
    cfg = json.loads(SMTP_FILE.read_text(encoding="utf-8"))
    sender = cfg.get("user") or _read(EMAIL_FILE)
    recipient = _read(EMAIL_FILE) or sender
    if not sender or not cfg.get("password"):
        return "no-smtp"

    attached: list[str] = []
    skipped: list[str] = []
    keep: list[Path] = []
    for path in attachments or []:
        if not path.exists():
            skipped.append(f"{path.name}（文件不存在）")
            continue
        size = path.stat().st_size
        if size > MAX_ATTACH_BYTES:
            skipped.append(f"{path.name}（{fmt_bytes(size)}，超过 {fmt_bytes(MAX_ATTACH_BYTES)} 上限）")
            continue
        attached.append(f"{path.name} {fmt_bytes(size)}")
        keep.append(path)

    notes: list[str] = []
    if attached:
        notes += ["", "附件：" + "；".join(attached)]
    if skipped:
        notes += ["", "未附上：" + "；".join(skipped)]

    msg = MIMEMultipart()
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr((cfg.get("from_name") or "minmaxH3", sender))
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(body + "\n".join(notes), "plain", "utf-8"))
    for path in keep:
        part = MIMEApplication(path.read_bytes(), _subtype="mp4", Name=path.name)
        part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", path.name))
        msg.attach(part)

    try:
        with smtplib.SMTP_SSL(
            cfg.get("host") or "smtp.qq.com",
            int(cfg.get("port") or 465),
            timeout=SMTP_TIMEOUT_SEC,
        ) as smtp:
            smtp.login(sender, cfg["password"])
            smtp.sendmail(sender, [recipient], msg.as_string())
    except Exception as e:
        return f"smtp-failed: {e}"
    return "smtp-ok" + (f" attached={attached}" if attached else "")


def send_ntfy(title: str, body: str) -> str:
    topic = _read(TOPIC_FILE)
    if not topic:
        return "no-topic"
    request = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=f"{title}\n{body}".encode("utf-8"),
        method="POST",
        headers={
            "Title": title.encode("latin-1", "replace").decode("latin-1"),
            "Tags": "movie_camera",
            "Priority": "default",
        },
    )
    try:
        urllib.request.urlopen(request, timeout=20).read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"ntfy-failed: {e}"
    return "ntfy-ok"


class Notifier:
    """Queued mail for one run. Every method is best-effort and never raises."""

    def __init__(self, label: str = "", enabled: bool = True) -> None:
        self.label = label or "任务"
        self.enabled = enabled and SMTP_FILE.exists()
        self.total = 0
        self.reused = 0
        self.out_mp4: Path | None = None
        self.output_dir: str = ""
        self.started_at: datetime | None = None
        self.done_at: list[datetime] = []
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        if self.enabled:
            self._thread = threading.Thread(target=self._worker, name="notify", daemon=True)
            self._thread.start()

    # -- plumbing ---------------------------------------------------------

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            title, body, attachments = item
            try:
                email_result = send_email(title, body, attachments)
            except Exception as e:
                email_result = f"smtp-exception: {e}"
            try:
                ntfy_result = send_ntfy(title, body)
            except Exception:
                ntfy_result = "ntfy-exception"
            mail_log(f"subject={title!r} email={email_result} ntfy={ntfy_result}")
            self._queue.task_done()

    def post(self, title: str, body: str, attachments: list[Path] | None = None) -> None:
        if not self.enabled:
            return
        self._queue.put((title, body, attachments))

    def close(self, timeout: float = DRAIN_TIMEOUT_SEC) -> None:
        if not self.enabled or self._thread is None:
            return
        self._queue.put(None)
        self._thread.join(timeout)

    # -- progress maths ---------------------------------------------------

    def _eta(self, position: int) -> tuple[str, str, str, float]:
        """Average comes from clips actually rendered; what's left comes from
        the position in the list, so reused clips don't inflate the estimate."""
        rendered = len(self.done_at)
        if not rendered or self.started_at is None:
            return "—", "—", "—", 0.0
        elapsed = (self.done_at[-1] - self.started_at).total_seconds()
        average = elapsed / rendered
        remain = average * max(self.total - position, 0)
        finish = self.done_at[-1] + timedelta(seconds=remain)
        return fmt_minutes(average), fmt_minutes(remain), finish.strftime("%H:%M"), remain

    # -- events -----------------------------------------------------------

    def run_started(
        self,
        total: int,
        out_mp4: Path,
        output_dir: str,
        settings: str,
        reused: int = 0,
        warnings: list[str] | None = None,
    ) -> None:
        self.total = total
        self.reused = reused
        self.out_mp4 = out_mp4
        self.output_dir = output_dir
        self.started_at = datetime.now()
        self.done_at = []
        body = [
            f"开始时间：{self.started_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"片段数：{total}",
            f"本次要生成：{total - reused} 条" + (f"，复用已有 {reused} 条" if reused else ""),
            f"设定：{settings}",
            "",
            f"输出目录：{output_dir}",
            f"成片将写到：{out_mp4}",
            "",
            "每完成一条会再发一封，最后一封附上成片。",
        ]
        if warnings:
            body += ["", "开跑前："] + [f"  {w}" for w in warnings]
        self.post(
            f"{self.label} 开始 {reused}/{total}" + (f"（复用 {reused} 条）" if reused else ""),
            "\n".join(body),
        )

    def clip_done(self, index: int, clip_id: str, dest: Path, info: dict) -> None:
        self.done_at.append(datetime.now())
        average, remain, finish_at, remain_sec = self._eta(index)
        label = clip_label(clip_id)
        percent = int(round(100 * index / self.total)) if self.total else 0
        size = fmt_bytes(dest.stat().st_size) if dest.exists() else "—"
        title = f"{self.label} {index}/{self.total} 完成 {label} · 约剩{fmt_remain_short(remain_sec)}"
        body = "\n".join(
            [
                f"进度：{index} / {self.total}（{percent}%）",
                f"本条：{label}",
                "状态：成功",
                f"本条耗时：{fmt_minutes(info.get('seconds') or 0)}",
                f"均速：{average}/条",
                f"预计剩余：{remain}",
                f"预计完成：{finish_at}",
                "",
                f"输出：{dest}",
                f"大小：{size}",
                f"设定：{info.get('settings') or '—'}",
                f"prompt 字数：{info.get('prompt_chars') or '—'}",
                f"prompt_id：{info.get('prompt_id') or '—'}",
                f"重试次数：{info.get('attempts', 1) - 1}",
                "",
                f"下一条：{clip_label(info['next_label']) if info.get('next_label') else '（本条是最后一条）'}",
                f"成片将写到：{self.out_mp4}",
            ]
        )
        self.post(title, body)

    def clip_failed(self, clip_id: str, attempt: int, consecutive: int, error: str) -> None:
        label = clip_label(clip_id)
        done = len(self.done_at)
        self.post(
            f"{self.label} 失败 {label} 第{attempt}次 · {done}/{self.total}",
            "\n".join(
                [
                    f"进度：已成功 {done} / {self.total}",
                    f"本条：{label}",
                    "状态：失败，脚本会自动重试",
                    f"attempt={attempt} consecutive={consecutive}",
                    f"错误：{error}",
                    f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                ]
            ),
        )

    def concat_done(
        self,
        out_mp4: Path,
        ass_path: Path,
        clips: int,
        duration: float,
        report: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        size = out_mp4.stat().st_size if out_mp4.exists() else 0
        elapsed = ""
        if self.started_at:
            elapsed = fmt_minutes((datetime.now() - self.started_at).total_seconds())
        flag = f" · {len(warnings)} 项待看" if warnings else " · 自检通过"
        body = [
            "状态：全部生成并烧好字幕",
            f"成片：{out_mp4}",
            f"大小：{fmt_bytes(size)}",
            f"ASS：{ass_path}",
            f"片段数：{clips}",
            f"时长：{duration:.2f} 秒",
            f"全程耗时：{elapsed or '—'}",
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if report:
            body += ["", "自检："] + [f"  {line}" for line in report]
        if warnings:
            body += ["", "需要留意："] + [f"  {line}" for line in warnings]
        body += ["", "成片 mp4 见附件（手机可直接点开）。"]
        self.post(
            f"{self.label} 成片完成 {clips} 条 · {duration:.0f}秒{flag}",
            "\n".join(body),
            [out_mp4],
        )

    def run_incomplete(self, missing: list[str]) -> None:
        self.post(
            f"{self.label} 未全部完成 · 已成功 {len(self.done_at)}/{self.total}",
            "\n".join(
                [
                    f"缺失 {len(missing)} 条：",
                    *[f"  {clip_label(m)}" for m in missing],
                    "",
                    f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                ]
            ),
        )

    def run_crashed(self, error: BaseException) -> None:
        self.post(
            f"{self.label} 程序异常退出 · 已成功 {len(self.done_at)}/{self.total}",
            "\n".join(
                [
                    f"错误：{error}",
                    "",
                    traceback.format_exc()[-3000:],
                    "",
                    f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                ]
            ),
        )


def send_leftover(label: str, body: str) -> None:
    """合剪成功那封不改。质检遗留由 agent 另发这一封，开头就是坏镜。"""
    n = Notifier(label=label)
    if not n.enabled:
        raise SystemExit("没有 logs/smtp.json，遗留邮件发不出")
    text = body.strip()
    if "时间：" not in text:
        text = text + "\n\n" + f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    n.post(f"{label} 遗留", text)
    n.close()


def send_env_fail(label: str, body: str) -> None:
    """环境检查失败：工作流停在这里。开头就是失败原因。"""
    n = Notifier(label=label)
    if not n.enabled:
        raise SystemExit("没有 logs/smtp.json，环境检查邮件发不出")
    text = body.strip()
    if "时间：" not in text:
        text = text + "\n\n" + f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    n.post(f"{label} 环境检查失败 · 已停止", text)
    n.close()


if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Send leftover or environment-fail mail. No mp4.")
    p.add_argument("kind", choices=["leftover", "env"])
    p.add_argument("--label", required=True, help="作品名，例如 04-懦弱")
    p.add_argument("--file", type=Path, help="正文文件；不给则读 stdin")
    args = p.parse_args()
    raw = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
    if not raw.strip():
        raise SystemExit("邮件正文是空的")
    if args.kind == "env":
        send_env_fail(args.label, raw)
    else:
        send_leftover(args.label, raw)
