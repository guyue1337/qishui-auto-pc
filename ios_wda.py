# -*- coding: utf-8 -*-
"""iOS 17-26.x WebDriverAgent helper.

The public helpers downloaded here keep Apple account credentials in their own
local store.  This module never accepts, prints, or persists an Apple password.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


BASE_DIR = Path(__file__).resolve().parent
LOCAL_ROOT = Path(
    os.environ.get("LOCALAPPDATA")
    or os.environ.get("APPDATA")
    or Path.home()
) / "QishuiAutomation" / "wda"
TOOLS_DIR = LOCAL_ROOT / "tools"
CACHE_DIR = LOCAL_ROOT / "cache"
WORK_DIR = LOCAL_ROOT / "work"
LOG_DIR = LOCAL_ROOT / "logs"
STATE_FILE = LOCAL_ROOT / "state.json"
RUNNER_LOG = LOG_DIR / "wda_runner.log"

WDA_PORT = 8100
WDA_URL = f"http://127.0.0.1:{WDA_PORT}"
WDA_RUNNER_PREFIX = "com.facebook.WebDriverAgentRunner.xctrunner"
WDA_ZIP_NAME = "WebDriverAgentRunner-Runner-v16.15.3.zip"

TOOL_SPECS = {
    "plumesign": {
        "filename": "plumesign-windows-x86_64.exe",
        "url": "https://github.com/claration/impactor/releases/download/v2.7.3/"
               "plumesign-windows-x86_64.exe",
        "sha256": "94664749FBBCEB9A16BF89583F473DAFB9B35C8D058A9D8665DC963B3453C6FC",
        "size": 10_759_168,
    },
    "impactor": {
        "filename": "Impactor-windows-x86_64-portable.exe",
        "url": "https://github.com/claration/impactor/releases/download/v2.7.3/"
               "Impactor-windows-x86_64-portable.exe",
        "sha256": "EBF939F8C8E74B4CD693D2F9871DB4B8304F12396181FDD17404A127427B9923",
        "size": 18_259_968,
    },
    "wda": {
        "filename": WDA_ZIP_NAME,
        "url": "https://github.com/appium/WebDriverAgent/releases/download/v16.15.3/"
               "WebDriverAgentRunner-Runner.zip",
        "sha256": "2176793538D902F6B1E61291E9997F68D306541F222EA715D256A7C6CE9D4FC5",
        "size": 1_145_983,
    },
}

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _now_iso(ts: Optional[float] = None) -> str:
    try:
        return datetime.fromtimestamp(float(ts or time.time()), timezone.utc).isoformat()
    except Exception:
        return ""


def _major_version(value: Any) -> int:
    try:
        return int(str(value or "").split(".", 1)[0])
    except Exception:
        return 0


def _mask_email(email: str) -> str:
    email = str(email or "")
    if "@" not in email:
        return ""
    name, domain = email.split("@", 1)
    head = name[:2] if len(name) > 2 else name[:1]
    return f"{head}***@{domain}"


def _mask_udid(value: str) -> str:
    value = str(value or "")
    return ("***" + value[-6:]) if len(value) > 6 else "***"


def _redact(text: str) -> str:
    text = re.sub(r"\b[0-9A-Fa-f]{24,40}\b", "[设备标识已隐藏]", str(text or ""))
    text = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "[账号已隐藏]",
        text,
    )
    return text


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _python_exe() -> str:
    candidates = [
        BASE_DIR / "python" / "python.exe",
        BASE_DIR / "dist" / "汽水音乐自动化" / "python" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("RUST_LOG", "info")
    return env


def _verify_file(path: Path, spec: dict[str, Any]) -> bool:
    try:
        return path.is_file() and _sha256(path) == spec["sha256"]
    except Exception:
        return False


def _download_file(url: str, dest: Path, log: Callable[[str], None]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "QishuiAutomation/1.3"})
        with urllib.request.urlopen(request, timeout=45) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            last_report = 0
            with tmp.open("wb") as f:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total and done - last_report >= max(total // 5, 1):
                        last_report = done
                        log(f"下载进度 {min(100, int(done * 100 / total))}%")
        tmp.replace(dest)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class WdaManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._busy = False
        self._action = ""
        self._step = "待配置"
        self._progress: Optional[int] = None
        self._last_error = ""
        self._message = "iOS 17-26.x 需要 WDA；iOS 27+ 可直接免签连接。"
        self._logs: list[tuple[str, str, str]] = []
        self._log_sink: Optional[Callable[[str, str], None]] = None
        self._cache: dict[str, Any] = {}
        self._cache_ts = 0.0
        self._cache_udid = ""
        self._installed_cache: dict[str, Any] = {"udid": "", "ts": 0.0, "bundle": ""}
        self._http_cache: dict[str, Any] = {"ts": 0.0, "ready": False}
        self._runner_proc: Optional[subprocess.Popen[str]] = None
        self._forward_proc: Optional[subprocess.Popen[str]] = None
        self._children: list[subprocess.Popen[str]] = []
        for directory in (LOCAL_ROOT, TOOLS_DIR, CACHE_DIR, WORK_DIR, LOG_DIR):
            directory.mkdir(parents=True, exist_ok=True)
        atexit.register(self.shutdown)

    # ------------------------------------------------------------------
    # logging
    # ------------------------------------------------------------------
    def set_log_sink(self, sink: Optional[Callable[[str, str], None]]) -> None:
        self._log_sink = sink

    def log(self, text: str, level: str = "info") -> None:
        message = _redact(str(text).rstrip())
        if not message:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._logs.append((stamp, level, message))
            if len(self._logs) > 500:
                del self._logs[: len(self._logs) - 500]
        sink = self._log_sink
        if sink:
            try:
                sink(message, level)
            except Exception:
                pass

    def snapshot_logs(self, since: int = 0) -> tuple[int, list[tuple[str, str, str]]]:
        with self._lock:
            return len(self._logs), self._logs[since:]

    def _set_step(self, step: str, progress: Optional[int] = None) -> None:
        with self._lock:
            self._step = step
            self._progress = progress
        self.log(step, "info")

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------
    def tool_path(self, name: str) -> Path:
        return TOOLS_DIR / TOOL_SPECS[name]["filename"]

    def tools_ready(self, include_gui: bool = False) -> bool:
        names = ["plumesign", "wda"] + (["impactor"] if include_gui else [])
        return all(self.tool_path(name).is_file() for name in names)

    def _seed_candidates(self, filename: str) -> list[Path]:
        return [
            BASE_DIR / "packaging" / "cache" / filename,
            BASE_DIR.parent / "packaging" / "cache" / filename,
        ]

    def ensure_tool(
        self,
        name: str,
        log: Optional[Callable[[str], None]] = None,
    ) -> Path:
        spec = TOOL_SPECS[name]
        target = TOOLS_DIR / spec["filename"]
        callback = log or self.log
        if _verify_file(target, spec):
            return target

        for seed in self._seed_candidates(spec["filename"]):
            if seed.is_file() and _verify_file(seed, spec):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(seed, target)
                if _verify_file(target, spec):
                    callback(f"{spec['filename']} 已校验")
                    return target

        callback(f"正在下载 {spec['filename']}…")
        _download_file(spec["url"], target, callback)
        if not _verify_file(target, spec):
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(f"{spec['filename']} 校验失败，已删除，请检查网络后重试")
        callback(f"{spec['filename']} 下载并校验完成")
        return target

    # ------------------------------------------------------------------
    # account / installed app
    # ------------------------------------------------------------------
    def account_info(self) -> dict[str, Any]:
        path = Path(os.environ.get("APPDATA") or "") / "PlumeImpactor" / "accounts.json"
        if not path.is_file():
            return {"logged_in": False, "masked": ""}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            emails = list((data.get("accounts") or {}).keys())
            logged_in = bool(emails)
            return {
                "logged_in": logged_in,
                "masked": _mask_email(emails[0]) if emails else "",
            }
        except Exception:
            return {"logged_in": False, "masked": ""}

    def _query_installed_bundle(self, udid: str) -> str:
        import asyncio

        async def query() -> str:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.installation_proxy import InstallationProxyService

            lockdown = await create_using_usbmux(
                serial=udid,
                connection_type="USB",
                pair_timeout=20,
            )
            try:
                async with InstallationProxyService(lockdown=lockdown) as service:
                    apps = await service.get_apps(application_type="User")
            finally:
                try:
                    await lockdown.close()
                except Exception:
                    pass
            matches = [
                str(bundle) for bundle in (apps or {})
                if str(bundle).startswith(WDA_RUNNER_PREFIX)
            ]
            if not matches:
                return ""
            matches.sort(key=lambda item: (item != WDA_RUNNER_PREFIX, len(item)))
            return matches[0]

        async def bounded() -> str:
            return await asyncio.wait_for(query(), timeout=24)

        return asyncio.run(bounded())

    def _installed_bundle(self, udid: str, force: bool = False) -> str:
        if not udid:
            return ""
        now = time.time()
        with self._lock:
            cache = dict(self._installed_cache)
        if (
            not force
            and cache.get("udid") == udid
            and now - float(cache.get("ts") or 0.0) < 12
        ):
            return str(cache.get("bundle") or "")
        bundle = ""
        try:
            bundle = self._query_installed_bundle(udid)
        except Exception:
            bundle = str(cache.get("bundle") or "") if cache.get("udid") == udid else ""
        with self._lock:
            self._installed_cache = {"udid": udid, "ts": now, "bundle": bundle}
        return bundle

    # ------------------------------------------------------------------
    # HTTP / state
    # ------------------------------------------------------------------
    def _http_ready(self, force: bool = False) -> bool:
        now = time.time()
        with self._lock:
            if not force and now - float(self._http_cache.get("ts") or 0.0) < 1.2:
                return bool(self._http_cache.get("ready"))
        ready = False
        try:
            request = urllib.request.Request(f"{WDA_URL}/status", headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=1.2) as response:
                data = json.loads(response.read().decode("utf-8", "ignore") or "{}")
            value = data.get("value") if isinstance(data, dict) else None
            if isinstance(value, dict):
                state = str(value.get("state") or "").lower()
                ready = state in ("", "success", "ready") or bool(value.get("ready"))
        except Exception:
            ready = False
        with self._lock:
            self._http_cache = {"ts": now, "ready": ready}
        return ready

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self, data: dict[str, Any]) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)

    def _status_from_device(self, device: dict[str, Any], deep: bool) -> dict[str, Any]:
        device = device or {}
        udid = str(device.get("serial") or "")
        ios = str(device.get("ios") or "")
        major = _major_version(ios)
        needs_wda = bool(device.get("needs_wda")) or (0 < major < 27)
        wda_required = 0 < major < 27
        supported = 17 <= major <= 26
        if not major and needs_wda:
            supported = True

        account = self.account_info()
        state = self._load_state()
        state_matches = bool(udid and str(state.get("device_udid") or "") == udid)
        if deep or (udid and udid != self._cache_udid):
            bundle = self._installed_bundle(udid, force=deep)
        else:
            bundle = str(self._installed_cache.get("bundle") or "")
            if str(self._installed_cache.get("udid") or "") != udid:
                bundle = ""
        if not bundle and state_matches and state.get("bundle_id"):
            bundle = str(state.get("bundle_id") or "")
        ready = self._http_ready(force=deep) if wda_required else bool(device.get("ready"))

        expires_at = float(state.get("expires_at") or 0.0) if state_matches else 0.0
        days_left = None
        needs_resign = False
        if expires_at:
            seconds = expires_at - time.time()
            days_left = max(0, int((seconds + 86399) // 86400))
            needs_resign = seconds <= 0

        if major >= 27:
            mode = "coredevice"
            state_name = "coredevice" if device.get("ready") else "device"
            message = "这台 iPhone 可使用 iOS 27+ 免签直连，不需要安装 WDA。"
        elif major and major < 17:
            mode = "unsupported"
            state_name = "unsupported"
            message = "iOS 16 及以下未纳入当前兼容范围。"
        elif needs_wda:
            mode = "wda"
            if ready:
                state_name, message = "ready", "WDA 已运行，可以开始任务。"
            elif needs_resign:
                state_name, message = "expired", "WDA 签名已过期，请重新安装/续签。"
            elif bundle:
                state_name, message = "stopped", "WDA 已安装，点击「启动 WDA」即可。"
            elif not account["logged_in"]:
                state_name, message = "no_account", "请先打开 Impactor 登录 Apple ID。"
            else:
                state_name, message = "not_installed", "尚未安装 WDA，请点击一键安装。"
        else:
            mode = "unknown"
            state_name = "unknown"
            message = "等待读取 iPhone 后再判断连接方式。"

        if self._last_error and state_name not in ("ready",):
            message = self._last_error
        return {
            "mode": mode,
            "needs_wda": needs_wda,
            "wda_required": wda_required,
            "supported": supported,
            "ios": ios,
            "udid_masked": _mask_udid(udid),
            "tools_ready": self.tools_ready(),
            "account_logged_in": bool(account["logged_in"]),
            "account_masked": account["masked"],
            "installed": bool(bundle),
            "bundle_id": bundle,
            "running": bool(ready),
            "ready": bool(ready),
            "busy": self._busy,
            "action": self._action,
            "step": self._step,
            "progress": self._progress,
            "state": state_name,
            "message": message,
            "error": self._last_error,
            "signed_at": (_now_iso(state.get("signed_at"))
                          if state_matches and state.get("signed_at") else ""),
            "expires_at": _now_iso(expires_at) if expires_at else "",
            "days_left": days_left,
            "needs_resign": needs_resign,
        }

    def status(
        self,
        device: Optional[dict[str, Any]] = None,
        deep: bool = False,
    ) -> dict[str, Any]:
        device = device or {}
        now = time.time()
        udid = str(device.get("serial") or "")
        with self._lock:
            cached = dict(self._cache)
        if (
            not deep
            and not self._busy
            and cached
            and cached.get("_cache_udid") == udid
            and now - self._cache_ts < 4.0
        ):
            cached.pop("_cache_udid", None)
            return cached
        result = self._status_from_device(device, deep=deep)
        with self._lock:
            self._cache = dict(result)
            self._cache_ts = now
            self._cache_udid = udid
            self._cache["_cache_udid"] = udid
        return result

    def peek(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._cache)
        result.pop("_cache_udid", None)
        return result

    # ------------------------------------------------------------------
    # process helpers
    # ------------------------------------------------------------------
    def _spawn(
        self,
        cmd: list[str],
        prefix: str,
        cwd: Optional[Path] = None,
    ) -> subprocess.Popen[str]:
        self.log(f"启动：{prefix}", "dim")
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd or BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=_subprocess_env(),
            creationflags=_CREATE_NO_WINDOW,
        )
        with self._lock:
            self._children.append(proc)

        def pump() -> None:
            try:
                if proc.stdout:
                    for line in proc.stdout:
                        line = line.rstrip()
                        if line:
                            self.log(f"{prefix} {line}", "dim")
            except Exception:
                pass
            finally:
                try:
                    proc.stdout and proc.stdout.close()
                except Exception:
                    pass

        threading.Thread(target=pump, daemon=True).start()
        return proc

    def _run_stream(self, cmd: list[str], prefix: str, timeout: float) -> None:
        proc = self._spawn(cmd, prefix)
        deadline = time.time() + timeout
        while proc.poll() is None:
            if time.time() > deadline:
                self._terminate(proc)
                raise RuntimeError(f"{prefix} 超时（{int(timeout)} 秒）")
            time.sleep(0.2)
        if proc.returncode != 0:
            raise RuntimeError(f"{prefix} 失败，退出码 {proc.returncode}")

    @staticmethod
    def _terminate(proc: Optional[subprocess.Popen[str]]) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _stop_children(self) -> None:
        with self._lock:
            children = list(self._children)
            self._children.clear()
            self._runner_proc = None
            self._forward_proc = None
        for proc in children:
            self._terminate(proc)

    def shutdown(self) -> None:
        self._stop_children()

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _require_device(self, device: dict[str, Any]) -> tuple[str, str]:
        udid = str((device or {}).get("serial") or "")
        ios = str((device or {}).get("ios") or "")
        if not udid:
            raise RuntimeError("未读取到 iPhone，请先连接、解锁并信任电脑")
        major = _major_version(ios)
        if major and major >= 27:
            raise RuntimeError("这台 iPhone 使用 iOS 27+ 免签直连，不需要 WDA")
        if major and major < 17:
            raise RuntimeError("当前版本仅支持 iOS 17.0 及以上")
        if (device or {}).get("developer_mode") is False:
            raise RuntimeError("请先开启 iPhone 的开发者模式并重启")
        return udid, ios

    def _read_bundle_id(self, app_dir: Path) -> str:
        info_path = app_dir / "Info.plist"
        with info_path.open("rb") as f:
            info = plistlib.load(f)
        return str(info.get("CFBundleIdentifier") or "")

    def _read_expiry(self, app_dir: Path) -> float:
        profile = app_dir / "embedded.mobileprovision"
        try:
            raw = profile.read_bytes()
            start = raw.find(b"<?xml")
            end = raw.find(b"</plist>", start)
            if start >= 0 and end >= 0:
                data = plistlib.loads(raw[start:end + len(b"</plist>")])
                expires = data.get("ExpirationDate")
                if isinstance(expires, datetime):
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    return expires.timestamp()
        except Exception:
            pass
        return time.time() + 7 * 86400

    def _prepare_app(self) -> Path:
        wda_zip = self.ensure_tool("wda")
        stage = WORK_DIR / "wda-stage"
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(wda_zip) as archive:
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                if not name.startswith("WebDriverAgentRunner-Runner.app/"):
                    continue
                parts = Path(name).parts
                if any(part in ("", ".", "..") for part in parts):
                    continue
                target = stage / Path(name)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
        app_dir = stage / "WebDriverAgentRunner-Runner.app"
        if not (app_dir / "Info.plist").is_file():
            raise RuntimeError("WDA 压缩包解压失败")
        return app_dir

    def _install_sync(self, device: dict[str, Any]) -> None:
        udid, _ios = self._require_device(device)
        self._set_step("检查 WDA 工具", 5)
        plumesign = self.ensure_tool("plumesign")
        self._set_step("准备 WDA 安装包", 20)
        app_dir = self._prepare_app()
        account = self.account_info()
        if not account["logged_in"]:
            raise RuntimeError("尚未登录 Apple ID，请先点「打开 Apple ID 登录入口」")
        self._stop_children()
        self._set_step("正在使用免费 Apple ID 签名并安装，首次可能较慢", 35)
        self._run_stream(
            [
                str(plumesign),
                "sign",
                "--package", str(app_dir),
                "--apple-id",
                "--register-and-install",
                "--udid", udid,
            ],
            "签名",
            timeout=900,
        )
        self._set_step("确认安装结果", 82)
        bundle = self._read_bundle_id(app_dir)
        installed = self._installed_bundle(udid, force=True)
        if installed:
            bundle = installed
        if not bundle:
            raise RuntimeError("签名完成，但设备上没有查到 WDA，请解锁手机后重试")
        expires_at = self._read_expiry(app_dir)
        self._save_state({
            "bundle_id": bundle,
            "device_udid": udid,
            "signed_at": time.time(),
            "expires_at": expires_at,
        })
        with self._lock:
            self._cache_ts = 0.0
        self._set_step("WDA 安装完成", 100)
        self.log(
            f"WDA 已安装；免费签名预计 {max(0, int((expires_at - time.time()) / 86400))} 天后到期",
            "ok",
        )

    def _start_sync(self, device: dict[str, Any]) -> None:
        udid, _ios = self._require_device(device)
        self._set_step("检查 WDA 安装状态", 5)
        bundle = self._installed_bundle(udid, force=True)
        if not bundle:
            raise RuntimeError("尚未安装 WDA，请先点击「安装 / 续签 WDA」")
        state = self._load_state()
        expires_at = float(state.get("expires_at") or 0.0)
        if expires_at and expires_at <= time.time():
            raise RuntimeError("WDA 签名已过期，请重新点击「安装 / 续签 WDA」")
        if self._http_ready(force=True):
            self._set_step("WDA 已在运行", 100)
            return
        if not self._port_available(WDA_PORT):
            raise RuntimeError(f"本机端口 {WDA_PORT} 被占用，请关闭旧的 WDA/转发工具后重试")
        self._stop_children()
        python = _python_exe()
        self._set_step("挂载 iOS 开发者组件", 12)
        self._run_stream(
            [
                python,
                "-m", "pymobiledevice3",
                "mounter", "auto-mount",
                "--udid", udid,
                "--userspace",
            ],
            "开发者组件",
            timeout=720,
        )
        self._set_step("建立 8100 端口转发", 38)
        forward = self._spawn(
            [
                python,
                "-m", "pymobiledevice3",
                "usbmux", "forward", str(WDA_PORT), str(WDA_PORT),
                "--serial", udid,
            ],
            "端口转发",
        )
        with self._lock:
            self._forward_proc = forward
        time.sleep(0.8)
        if forward.poll() is not None:
            raise RuntimeError("8100 端口转发启动失败")
        self._set_step("启动 WDA 测试运行器", 50)
        runner = self._spawn(
            [
                python,
                "-u", str(BASE_DIR / "wda_runner.py"),
                "--udid", udid,
                "--bundle-id", bundle,
                "--log", str(RUNNER_LOG),
            ],
            "WDA运行器",
        )
        with self._lock:
            self._runner_proc = runner
        deadline = time.time() + 150
        while time.time() < deadline:
            if self._http_ready(force=True):
                self._set_step("WDA 已启动", 100)
                self.log("WDA 已就绪，现在可以开始任务", "ok")
                return
            if runner.poll() is not None:
                tail = self._log_tail(RUNNER_LOG, 6)
                detail = ("\n" + tail) if tail else ""
                if re.search(r"provision|expired|not signed|code ?sign", tail, re.I):
                    raise RuntimeError("WDA 启动失败，签名可能已过期，请重新安装/续签" + detail)
                raise RuntimeError("WDA 运行器提前退出，请查看 WDA 日志" + detail)
            if int(time.time()) % 5 == 0:
                self._step = "等待 WDA 就绪"
            time.sleep(0.8)
        self._stop_children()
        raise RuntimeError("WDA 启动超时，请检查 iPhone 是否解锁、开发者模式是否开启")

    @staticmethod
    def _log_tail(path: Path, lines: int) -> str:
        try:
            data = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(data[-lines:])
        except Exception:
            return ""

    @staticmethod
    def _port_available(port: int) -> bool:
        try:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

    def ensure_running(
        self,
        device: dict[str, Any],
        log: Optional[Callable[[str], None]] = None,
    ) -> tuple[bool, str]:
        with self._lock:
            if self._busy:
                return False, "WDA 正在执行其他操作，请稍等"
            self._busy = True
            self._action = "start"
            self._last_error = ""
        try:
            self._start_sync(device)
            if log:
                log("WDA 已就绪")
            return True, ""
        except Exception as exc:
            self._last_error = str(exc)
            self.log(str(exc), "error")
            if log:
                log(str(exc))
            return False, str(exc)
        finally:
            with self._lock:
                self._busy = False
                self._action = ""
                self._cache_ts = 0.0

    def _login_sync(self) -> None:
        self._set_step("准备 Impactor 登录窗口", 5)
        impactor = self.ensure_tool("impactor")
        self._set_step("正在打开 Impactor", 80)
        if os.name == "nt":
            os.startfile(str(impactor))  # type: ignore[attr-defined]
        else:
            subprocess.Popen([str(impactor)], cwd=str(impactor.parent))
        self._set_step("Impactor 已打开", 100)
        self.log(
            "请在 Impactor 窗口输入 Apple ID 完成登录；本程序不会读取密码。"
            "登录后回到这里点「检查状态」。",
            "ok",
        )

    def _logout_sync(self) -> None:
        self._set_step("清除本机 Apple ID 登录", 10)
        plumesign = self.ensure_tool("plumesign")
        self._run_stream(
            [str(plumesign), "account", "logout"],
            "账号",
            timeout=120,
        )
        self._set_step("已清除登录信息", 100)
        with self._lock:
            self._cache_ts = 0.0

    def _action_worker(
        self,
        action: str,
        device: dict[str, Any],
    ) -> None:
        try:
            if action == "login":
                self._login_sync()
            elif action == "install":
                self._install_sync(device)
            elif action == "start":
                self._start_sync(device)
            elif action == "logout":
                self._logout_sync()
            else:
                raise RuntimeError("未知操作")
        except Exception as exc:
            self._last_error = str(exc)
            self.log(str(exc), "error")
        finally:
            with self._lock:
                self._busy = False
                self._action = ""
                self._cache_ts = 0.0

    def action(self, action: str, device: dict[str, Any]) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        if action == "check":
            return {"ok": True, "status": self.status(device=device, deep=True)}
        if action == "stop":
            self._stop_children()
            self._last_error = ""
            self._step = "WDA 已停止"
            self._progress = None
            with self._lock:
                self._cache_ts = 0.0
            return {"ok": True, "status": self.status(device=device, deep=False)}
        if action not in ("login", "install", "start", "logout"):
            return {"ok": False, "error": "未知操作"}
        with self._lock:
            if self._busy:
                return {"ok": False, "error": "已有 WDA 操作正在进行"}
            self._busy = True
            self._action = action
            self._last_error = ""
            self._progress = 0
            self._step = "准备中"
        threading.Thread(
            target=self._action_worker,
            args=(action, dict(device or {})),
            daemon=True,
        ).start()
        return {"ok": True, "accepted": True, "status": self.status(device=device, deep=False)}


_manager: Optional[WdaManager] = None
_manager_lock = threading.Lock()


def get_manager() -> WdaManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = WdaManager()
        return _manager
