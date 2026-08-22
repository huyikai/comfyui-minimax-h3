"""开跑前的环境检查。抖音 cookie：Chrome、Cursor 内置浏览器、已有 cookies.txt 都试。

哪边有能用的就导出到 logs/cookies.txt。两边都没有：记原因、发邮件、exit 2。
这是整条链路唯一允许停下来的地方（磁盘 < 2GB / ComfyUI 起不来仍由 run_video_scripts 硬挡）。

    .\\.venv\\Scripts\\python.exe tools\\envcheck.py --url <抖音链接> --mail
    .\\.venv\\Scripts\\python.exe tools\\envcheck.py --import-cdp cookies.json --url <链接> --mail
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
COOKIES_FILE = LOG_DIR / "cookies.txt"
PROBE_URL_DEFAULT = "https://www.douyin.com/"

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE = 0xFFFFFFFFFFFFFFFF


def which_ytdlp() -> str:
    found = shutil.which("yt-dlp")
    if found:
        return found
    raise SystemExit("找不到 yt-dlp")


def _vss_copy(src: Path, dst: Path) -> int:
    """Chrome / Cursor 把 Cookies 库开成独占锁时，从卷影拷。"""
    resolved = src.resolve()
    drive = resolved.drive  # 'C:'
    if not drive:
        raise OSError(f"VSS 需要盘符: {src}")
    rel = str(resolved)[len(drive) + 1:]  # drop C:\
    dst.parent.mkdir(parents=True, exist_ok=True)
    ps = rf"""
$ErrorActionPreference = 'Stop'
$cls = [WMIClass]'root\cimv2:Win32_ShadowCopy'
$created = $cls.Create('{drive}\', 'ClientAccessible')
if ($created.ReturnValue -ne 0) {{
  throw "Win32_ShadowCopy.Create ReturnValue=$($created.ReturnValue)"
}}
$id = $created.ShadowID
$sc = Get-WmiObject Win32_ShadowCopy | Where-Object {{ $_.ID -eq $id }}
try {{
  $dev = $sc.DeviceObject
  if (-not $dev.EndsWith('\')) {{ $dev += '\' }}
  $from = $dev + '{rel}'
  $to = '{dst}'
  $ok = [System.IO.File]::Copy($from, $to, $true)
  if (-not (Test-Path -LiteralPath $to)) {{ throw "VSS copy produced no file" }}
  (Get-Item -LiteralPath $to).Length
}} finally {{
  if ($sc) {{ $sc.Delete() }}
}}
"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    if r.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        raise OSError((r.stderr or r.stdout or "vss copy failed")[-500:])
    return dst.stat().st_size


def _createfile_copy(src: Path, dst: Path) -> int:
    k32 = ctypes.windll.kernel32
    k32.SetLastError(0)
    CreateFileW = k32.CreateFileW
    CreateFileW.restype = ctypes.c_void_p
    CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    path = "\\\\?\\" + str(src.resolve())
    handle = CreateFileW(
        path, GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
    )
    err = k32.GetLastError()
    if handle in (None, 0, INVALID_HANDLE, 0xFFFFFFFF):
        raise OSError(err, f"CreateFile 打不开 {src} (winerr={err})")
    try:
        ReadFile = k32.ReadFile
        buf = ctypes.create_string_buffer(1024 * 1024)
        nread = ctypes.c_ulong(0)
        chunks = bytearray()
        while True:
            ok = ReadFile(ctypes.c_void_p(handle), buf, len(buf), ctypes.byref(nread), None)
            if not ok:
                raise OSError(k32.GetLastError(), f"ReadFile {src}")
            if nread.value == 0:
                break
            chunks.extend(buf.raw[: nread.value])
        dst.write_bytes(bytes(chunks))
        return len(chunks)
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


def _dotnet_share_copy(src: Path, dst: Path) -> int:
    """Chrome 锁库时 .NET FileShare.ReadWrite 往往比 Python open 管用。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    ps = f"""
$src = '{str(src)}'
$dst = '{str(dst)}'
$in = [System.IO.File]::Open($src, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
try {{
  $out = [System.IO.File]::Create($dst)
  try {{ $in.CopyTo($out) }} finally {{ $out.Close() }}
}} finally {{ $in.Close() }}
(Get-Item -LiteralPath $dst).Length
"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30,
    )
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout or "dotnet copy failed")[-400:])
    return dst.stat().st_size


def copy_locked(src: Path, dst: Path) -> int:
    """Chrome / Cursor 开着时 Cookies 库是独占锁。先普通读，不行再卷影。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    try:
        data = src.read_bytes()
        if data:
            dst.write_bytes(data)
            return len(data)
    except OSError as e:
        errors.append(f"read_bytes: {e}")
    for fn in (_vss_copy, _dotnet_share_copy, _createfile_copy):
        try:
            n = fn(src, dst)
            if n > 0:
                return n
            errors.append(f"{fn.__name__}: 0 bytes")
        except OSError as e:
            errors.append(f"{fn.__name__}: {e}")
    raise OSError("；".join(errors))


def cdp_to_netscape(payload) -> str:
    if isinstance(payload, str):
        payload = json.loads(payload)
    cookies = payload
    if isinstance(payload, dict):
        cookies = (
            payload.get("cookies")
            or (payload.get("result") or {}).get("cookies")
            or payload.get("value")
            or []
        )
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in cookies:
        domain = (c.get("domain") or "").strip()
        if not domain or not c.get("name"):
            continue
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = c.get("expires") if c.get("expires") is not None else c.get("expirationDate", 0)
        try:
            exp_i = int(float(exp or 0))
        except (TypeError, ValueError):
            exp_i = 0
        if exp_i < 0:
            exp_i = 0
        lines.append(
            f"{domain}\t{flag}\t{path}\t{secure}\t{exp_i}\t{c['name']}\t{c.get('value') or ''}"
        )
    return "\n".join(lines) + "\n"


def probe(url: str, extra: list[str], timeout: int = 90) -> tuple[bool, str]:
    cmd = [
        which_ytdlp(), "--no-download", "--no-warnings",
        "--print", "%(id)s", *extra, url,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "yt-dlp 超时"
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode == 0 and (r.stdout or "").strip():
        return True, (r.stdout or "").strip().splitlines()[0]
    return False, out[-1500:]


def dump_and_probe(url: str, browser_args: list[str], dest: Path) -> tuple[bool, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    ok, detail = probe(url, [*browser_args, "--cookies", str(dest)])
    return ok, detail


def chrome_user_data() -> Path | None:
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    return p if p.is_dir() else None


def chrome_profiles(user_data: Path) -> list[Path]:
    names = ["Default"] + [f"Profile {i}" for i in range(1, 8)]
    found = []
    for name in names:
        d = user_data / name
        cookies = d / "Network" / "Cookies"
        if cookies.is_file():
            found.append(d)
    return found


def try_chromium_copy(url: str, user_data: Path, profile: Path, dest: Path, label: str) -> dict:
    """把 Local State + 锁住的 Cookies 拷到临时 User Data，再让 yt-dlp 当 Chrome 档来解。"""
    local_state = user_data / "Local State"
    cookies_src = profile / "Network" / "Cookies"
    if not cookies_src.is_file():
        cookies_src = profile / "Cookies"
    if not cookies_src.is_file() or not local_state.is_file():
        return {"ok": False, "source": label, "detail": "缺 Local State 或 Cookies"}
    tmp = Path(tempfile.mkdtemp(prefix="envcheck-ud-"))
    try:
        copy_locked(local_state, tmp / "Local State")
        n = copy_locked(cookies_src, tmp / "Default" / "Network" / "Cookies")
        journal = cookies_src.parent / "Cookies-journal"
        if journal.is_file():
            try:
                copy_locked(journal, tmp / "Default" / "Network" / "Cookies-journal")
            except OSError:
                pass
        fake_profile = tmp / "Default"
        ok, detail = dump_and_probe(
            url, ["--cookies-from-browser", f"chrome:{fake_profile}"], dest,
        )
        if ok:
            return {"ok": True, "source": label, "detail": f"拷了 {n} 字节；probe={detail}"}
        return {"ok": False, "source": label, "detail": detail}
    except OSError as e:
        return {"ok": False, "source": label, "detail": str(e)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def try_native_browser(url: str, spec: str, dest: Path) -> dict:
    ok, detail = dump_and_probe(url, ["--cookies-from-browser", spec], dest)
    return {"ok": ok, "source": f"browser:{spec}", "detail": detail}


def cursor_cookie_targets() -> list[tuple[str, Path, Path]]:
    """(label, user_data_dir_with_Local_State, profile_dir)."""
    roaming = Path(os.environ.get("APPDATA", "")) / "Cursor"
    out: list[tuple[str, Path, Path]] = []
    if not roaming.is_dir():
        return out
    local_state_root = roaming
    browser = roaming / "Partitions" / "cursor-browser"
    if (browser / "Network" / "Cookies").is_file():
        out.append(("cursor-browser", local_state_root, browser))
    if (roaming / "Network" / "Cookies").is_file():
        out.append(("cursor-app", local_state_root, roaming))
    return out


def run_checks(url: str, dest: Path, import_cdp: Path | None) -> dict:
    tries: list[dict] = []

    if dest.is_file() and dest.stat().st_size > 0:
        ok, detail = probe(url, ["--cookies", str(dest)])
        row = {"ok": ok, "source": f"file:{dest}", "detail": detail}
        tries.append(row)
        if ok:
            return {"ok": True, "source": row["source"], "detail": detail, "tries": tries,
                    "cookies": str(dest)}

    if import_cdp is not None:
        dest.write_text(cdp_to_netscape(json.loads(import_cdp.read_text(encoding="utf-8"))),
                        encoding="utf-8")
        ok, detail = probe(url, ["--cookies", str(dest)])
        row = {"ok": ok, "source": f"cdp:{import_cdp.name}", "detail": detail}
        tries.append(row)
        if ok:
            return {"ok": True, "source": row["source"], "detail": detail, "tries": tries,
                    "cookies": str(dest)}

    ud = chrome_user_data()
    if ud:
        tries.append(try_native_browser(url, "chrome", dest))
        if tries[-1]["ok"]:
            return {**tries[-1], "tries": tries, "cookies": str(dest)}
        for profile in chrome_profiles(ud):
            tries.append(try_chromium_copy(
                url, ud, profile, dest, f"chrome-copy:{profile.name}",
            ))
            if tries[-1]["ok"]:
                return {**tries[-1], "tries": tries, "cookies": str(dest)}

    edge_ud = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
    if edge_ud.is_dir():
        tries.append(try_native_browser(url, "edge", dest))
        if tries[-1]["ok"]:
            return {**tries[-1], "tries": tries, "cookies": str(dest)}

    for label, user_data, profile in cursor_cookie_targets():
        tries.append(try_chromium_copy(url, user_data, profile, dest, f"cursor:{label}"))
        if tries[-1]["ok"]:
            return {**tries[-1], "tries": tries, "cookies": str(dest)}

    return {
        "ok": False,
        "source": None,
        "detail": "Chrome、Cursor 内置浏览器、cookies.txt 都拿不到能用的抖音 cookie",
        "tries": tries,
        "cookies": None,
    }


def mail_and_record(result: dict, label: str, url: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = "\n".join([
        "环境检查失败，工作流已停止。这是唯一允许中断的步骤。",
        "",
        f"链接：{url}",
        f"原因：{result.get('detail')}",
        "",
        "试过：",
        *[f"  - {t.get('source')}: {'ok' if t.get('ok') else 'fail'} {(t.get('detail') or '')[:200]}"
          for t in result.get("tries") or []],
        "",
        "下一步（人来补，agent 不再问）：登录 Chrome 或 Cursor 内置浏览器打开抖音，再重发同一条命令。",
        "",
        f"时间：{stamp}",
    ])
    sys.path.insert(0, str(ROOT))
    from notify import send_env_fail  # noqa: WPS433
    send_env_fail(label, body)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Check Douyin cookies before download.")
    p.add_argument("--url", default=PROBE_URL_DEFAULT, help="用来试 cookie 的链接")
    p.add_argument("--cookies", type=Path, default=COOKIES_FILE)
    p.add_argument("--import-cdp", type=Path, help="Network.getAllCookies 的 JSON")
    p.add_argument("--mail", action="store_true", help="失败时发环境检查邮件")
    p.add_argument("--label", default="人间隙", help="邮件标题用的号名")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    result = run_checks(args.url, args.cookies, args.import_cdp)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result["ok"]:
            print(f"[ENV] cookie ok  source={result['source']}  file={result.get('cookies')}")
        else:
            print(f"[ENV] cookie FAIL  {result['detail']}")
            for t in result.get("tries") or []:
                print(f"  - {t.get('source')}: {t.get('detail', '')[:240]}")

    if not result["ok"] and args.mail:
        try:
            mail_and_record(result, args.label, args.url)
            print("[ENV] 已发环境检查失败邮件")
        except SystemExit as e:
            print(f"[ENV] 邮件没发出：{e}")
        except Exception as e:  # noqa: BLE001
            print(f"[ENV] 邮件没发出：{type(e).__name__}: {e}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
