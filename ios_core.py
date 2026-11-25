# -*- coding: utf-8 -*-
"""iPhone USB backend for iOS 17+.

The backend keeps one userspace RemoteServiceDiscovery tunnel and one set of
CoreDevice services alive in a dedicated asyncio thread.  Screen captures,
touches and app control therefore do not pay the tunnel setup cost on every
action.

Basic device information is read through lockdownd over usbmux and works before
Developer Mode is enabled.  Screen capture and touch need Developer Mode and a
mounted Developer Disk Image; the first preparation is handled automatically.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Optional


class IOSCoreError(RuntimeError):
    """An actionable iPhone backend error."""

    def __init__(self, message: str, code: str = "ios_core"):
        super().__init__(message)
        self.code = code


def _png_size(data: bytes) -> tuple[int, int]:
    if not data or len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    try:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    except Exception:
        return 0, 0


def _plain(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _process_pid(token: dict[str, Any]) -> int:
    for key in ("processIdentifier", "pid", "identifier"):
        value = token.get(key)
        if isinstance(value, dict):
            value = value.get("_0")
        try:
            pid = int(value)
            if pid > 0:
                return pid
        except (TypeError, ValueError):
            pass
    return 0


def _token_contains(token: dict[str, Any], bundle_id: str) -> bool:
    if not bundle_id:
        return False
    needle = bundle_id.lower()
    for key in ("bundleIdentifier", "bundleId", "executable", "path", "name"):
        value = token.get(key)
        if isinstance(value, dict):
            value = value.get("_0") or value.get("path") or value.get("relative")
        if needle in _plain(value).lower():
            return True
    return needle in repr(token).lower()


def _token_executable(token: dict[str, Any]) -> str:
    for key in ("executableURL", "executable", "path"):
        value = token.get(key)
        if isinstance(value, dict):
            value = value.get("relative") or value.get("path") or value.get("_0")
        path = _plain(value)
        if path.startswith("file://"):
            path = path[7:]
        if path:
            return path
    return ""


class _IOSCore:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._state_lock = threading.Lock()

        self._op_lock: Optional[asyncio.Lock] = None
        self._stack: Optional[contextlib.AsyncExitStack] = None
        self._tunnel = None
        self._rsd = None
        self._device_info_svc = None
        self._screen_svc = None
        self._app_svc = None
        self._touch_cm = None
        self._hid_svc = None
        self._touch_service_id = 257

        self._udid = ""
        self._probe_cache: dict[str, Any] = {}
        self._probe_ts = 0.0
        self._ready = False
        self._last_error = ""
        self._active_bundle = ""
        self._apps: dict[str, dict[str, Any]] = {}
        self._apps_ts = 0.0
        self._screen_size = (0, 0)
        self._display_size = (0, 0)
        self._display_info: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Background event loop
    # ------------------------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return self._loop
        with self._state_lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()

            def runner() -> None:
                asyncio.set_event_loop(loop)
                self._loop_ready.set()
                loop.run_forever()

            thread = threading.Thread(target=runner, name="ios-core-backend", daemon=True)
            self._loop = loop
            self._thread = thread
            self._loop_ready.clear()
            thread.start()
            self._loop_ready.wait(3.0)
            return loop

    def _call(self, coro, timeout: float = 30.0):
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise IOSCoreError("iPhone 操作超时，请解锁手机后重试", "timeout") from None
        except IOSCoreError:
            raise
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            self._last_error = message
            raise IOSCoreError(message) from exc

    async def _locked(self, coro, timeout: float = 30.0):
        if self._op_lock is None:
            self._op_lock = asyncio.Lock()
        async with self._op_lock:
            return await asyncio.wait_for(coro, timeout=timeout)

    # ------------------------------------------------------------------
    # Lockdown / device information
    # ------------------------------------------------------------------
    async def _probe_async(self, udid: str = "") -> dict[str, Any]:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.usbmux import select_devices_by_connection_type

        devices = await select_devices_by_connection_type("USB")
        if not devices:
            self._udid = ""
            self._reset_async()
            return {}
        chosen = None
        if udid:
            for device in devices:
                if device.matches_udid(udid):
                    chosen = device
                    break
        if chosen is None:
            chosen = devices[0]
        selected_udid = chosen.serial

        lockdown = None
        developer_mode: Optional[bool] = None
        try:
            lockdown = await asyncio.wait_for(
                create_using_usbmux(serial=selected_udid, autopair=False),
                timeout=12.0,
            )
            values = dict(lockdown.all_values or {})
            try:
                developer_mode = bool(await lockdown.get_developer_mode_status())
            except Exception:
                developer_mode = None
        finally:
            if lockdown is not None:
                with contextlib.suppress(Exception):
                    await lockdown.close()

        if selected_udid != self._udid:
            await self._reset_and_wait()
            self._udid = selected_udid

        info = {
            "udid": selected_udid,
            "name": _plain(values.get("DeviceName")),
            "model": _plain(values.get("ProductType")),
            "ios": _plain(values.get("ProductVersion")),
            "build": _plain(values.get("BuildVersion")),
            "serial_number": _plain(values.get("SerialNumber")),
            "device_class": _plain(values.get("DeviceClass")),
            "hardware_model": _plain(values.get("HardwareModel")),
            "developer_mode": developer_mode,
            "developer_mode_known": developer_mode is not None,
            "ready": bool(self._ready and selected_udid == self._udid),
            "screen_width": self._screen_size[0],
            "screen_height": self._screen_size[1],
            "display_width": self._display_size[0],
            "display_height": self._display_size[1],
        }
        info["display"] = dict(self._display_info)
        return info

    def probe(self, udid: str = "", force: bool = False) -> dict[str, Any]:
        now = time.time()
        if (
            not force
            and self._probe_cache
            and now - self._probe_ts < 3.0
            and (not udid or self._probe_cache.get("udid") == udid)
        ):
            return dict(self._probe_cache)
        data = self._call(self._probe_async(udid), timeout=20.0)
        self._probe_cache = dict(data or {})
        self._probe_ts = now
        return dict(self._probe_cache)

    # ------------------------------------------------------------------
    # Developer image / persistent CoreDevice services
    # ------------------------------------------------------------------
    def _run_pmd3_cli(self, args: list[str], timeout: float = 420.0) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        creationflags = 0x08000000 if os.name == "nt" else 0
        return subprocess.run(
            [sys.executable, "-m", "pymobiledevice3", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=creationflags,
        )

    def _mount_developer_image(self, udid: str) -> None:
        args = ["mounter", "auto-mount"]
        if udid:
            args += ["--udid", udid]
        try:
            result = self._run_pmd3_cli(args)
        except subprocess.TimeoutExpired:
            raise IOSCoreError(
                "iOS 开发者镜像准备超时。请确认 iPhone 已解锁、已信任电脑并已开启开发者模式，然后重试",
                "ddi_timeout",
            ) from None
        output = "\n".join(x for x in (result.stdout, result.stderr) if x).strip()
        if result.returncode != 0:
            lower = output.lower()
            if "developer mode" in lower or "未启用开发者模式" in output:
                raise IOSCoreError(
                    "请先在 iPhone 打开「设置 → 隐私与安全性 → 开发者模式」，开启后重启手机",
                    "developer_mode",
                )
            if "pair" in lower or "trust" in lower or "配对" in output:
                raise IOSCoreError(
                    "iPhone 尚未信任此电脑。请解锁手机，点「信任」并输入锁屏密码",
                    "pairing",
                )
            tail = output[-900:] if output else "未知错误"
            raise IOSCoreError("iPhone 开发者组件准备失败：\n" + tail, "ddi")

    def _reveal_developer_mode(self, udid: str) -> None:
        args = ["amfi", "reveal-developer-mode"]
        if udid:
            args += ["--udid", udid]
        with contextlib.suppress(Exception):
            self._run_pmd3_cli(args, timeout=30.0)

    async def _ensure_ready_async(self, udid: str = "") -> None:
        if self._ready and self._rsd is not None and (not udid or self._udid == udid):
            return
        await self._reset_and_wait()
        probe = await self._probe_async(udid)
        if not probe:
            raise IOSCoreError("未检测到通过 USB 连接的 iPhone，请检查数据线", "not_connected")
        target = _plain(probe.get("udid"))
        self._udid = target

        if probe.get("developer_mode") is False:
            await asyncio.to_thread(self._reveal_developer_mode, target)
            raise IOSCoreError(
                "请先在 iPhone 打开「设置 → 隐私与安全性 → 开发者模式」，开启后重启手机"
                "（设置入口已自动显示）",
                "developer_mode",
            )

        await asyncio.to_thread(self._mount_developer_image, target)

        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
        from pymobiledevice3.remote.core_device.app_service import AppServiceService
        from pymobiledevice3.remote.core_device.device_info import DeviceInfoService, select_display
        from pymobiledevice3.remote.core_device.hid_service import touch_session
        from pymobiledevice3.remote.core_device.screen_capture_service import ScreenCaptureService

        stack = contextlib.AsyncExitStack()
        try:
            tunnel = UserspaceRsdTunnel(serial=target)
            rsd = await tunnel.aopen()
            stack.push_async_callback(tunnel.aclose)

            device_info_svc = DeviceInfoService(rsd)
            await stack.enter_async_context(device_info_svc)
            screen_svc = ScreenCaptureService(rsd)
            await stack.enter_async_context(screen_svc)
            app_svc = AppServiceService(rsd)
            await stack.enter_async_context(app_svc)

            raw_display = await device_info_svc.get_display_info()
            display = select_display(raw_display)
            self._display_info = dict(display or {})
            mode = (display or {}).get("currentMode") or {}
            size = mode.get("size") or ()
            if len(size) >= 2:
                self._display_size = (int(round(size[0])), int(round(size[1])))

            touch_cm = touch_session(rsd)
            hid_svc = await touch_cm.__aenter__()
            stack.push_async_callback(touch_cm.__aexit__, None, None, None)
            touch_service_id = await hid_svc.active_touchscreen_service_id()

            self._stack = stack
            self._tunnel = tunnel
            self._rsd = rsd
            self._device_info_svc = device_info_svc
            self._screen_svc = screen_svc
            self._app_svc = app_svc
            self._touch_cm = touch_cm
            self._hid_svc = hid_svc
            self._touch_service_id = int(touch_service_id)
            self._ready = True
            self._last_error = ""
            self._probe_ts = 0.0
        except BaseException:
            await stack.aclose()
            raise

    def ensure_ready(self, udid: str = "", timeout: float = 500.0) -> None:
        self._call(self._ensure_ready_async(udid), timeout=timeout)

    def _reset_async(self) -> None:
        stack = self._stack
        self._stack = None
        self._tunnel = None
        self._rsd = None
        self._device_info_svc = None
        self._screen_svc = None
        self._app_svc = None
        self._touch_cm = None
        self._hid_svc = None
        self._ready = False
        self._apps = {}
        self._apps_ts = 0.0
        self._screen_size = (0, 0)
        if stack is not None:
            with contextlib.suppress(Exception):
                asyncio.create_task(stack.aclose())

    def reset(self) -> None:
        with contextlib.suppress(Exception):
            self._call(self._reset_and_wait(), timeout=15.0)
        self._probe_ts = 0.0
        self._probe_cache = {}

    async def _reset_and_wait(self) -> None:
        stack = self._stack
        self._stack = None
        self._tunnel = None
        self._rsd = None
        self._device_info_svc = None
        self._screen_svc = None
        self._app_svc = None
        self._touch_cm = None
        self._hid_svc = None
        self._ready = False
        self._apps = {}
        self._apps_ts = 0.0
        self._screen_size = (0, 0)
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    # ------------------------------------------------------------------
    # Screen / input
    # ------------------------------------------------------------------
    async def _screenshot_async(self) -> bytes:
        if self._screen_svc is None:
            raise IOSCoreError("iPhone 画面通道尚未连接", "not_ready")
        response = await self._screen_svc.capture_screenshot()
        data = (response or {}).get("image")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise IOSCoreError("iPhone 截图返回为空", "screenshot")
        data = bytes(data)
        size = _png_size(data)
        if size[0] and size[1]:
            self._screen_size = size
        return data

    def screenshot_png(self) -> Optional[bytes]:
        try:
            return self._call(self._locked(self._screenshot_async()), timeout=25.0)
        except Exception as exc:
            self._last_error = str(exc)
            if self._ready:
                self.reset()
            return None

    async def _tap_async(self, x: int, y: int) -> None:
        from pymobiledevice3.remote.core_device.hid_service import (
            TOUCHSCREEN_STATE_CONTACT,
            TOUCHSCREEN_STATE_RELEASE,
        )

        if self._hid_svc is None:
            raise IOSCoreError("iPhone 触摸通道尚未连接", "not_ready")
        width, height = self._screen_size
        if width <= 0 or height <= 0:
            await self._screenshot_async()
            width, height = self._screen_size
        if width <= 0 or height <= 0:
            raise IOSCoreError("读不到 iPhone 屏幕尺寸", "screen_size")
        hx = max(0, min(65535, round(int(x) * 65535 / float(width))))
        hy = max(0, min(65535, round(int(y) * 65535 / float(height))))
        await self._hid_svc.send_touchscreen(
            TOUCHSCREEN_STATE_CONTACT, hx, hy, service_id=self._touch_service_id
        )
        await asyncio.sleep(0.05)
        await self._hid_svc.send_touchscreen(
            TOUCHSCREEN_STATE_RELEASE, hx, hy, service_id=self._touch_service_id
        )

    def tap(self, x: int, y: int) -> bool:
        try:
            self._call(self._locked(self._tap_async(x, y)), timeout=12.0)
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    async def _swipe_async(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        if self._hid_svc is None:
            raise IOSCoreError("iPhone 触摸通道尚未连接", "not_ready")
        width, height = self._screen_size
        if width <= 0 or height <= 0:
            await self._screenshot_async()
            width, height = self._screen_size
        if width <= 0 or height <= 0:
            raise IOSCoreError("读不到 iPhone 屏幕尺寸", "screen_size")
        scale_x = 65535.0 / width
        scale_y = 65535.0 / height
        path = (
            int(round(x1 * scale_x)),
            int(round(y1 * scale_y)),
            int(round(x2 * scale_x)),
            int(round(y2 * scale_y)),
        )
        await self._hid_svc.multi_drag(
            [path],
            steps=14,
            duration=max(0.12, float(duration_ms) / 1000.0),
            service_id=self._touch_service_id,
        )

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> bool:
        try:
            self._call(
                self._locked(self._swipe_async(x1, y1, x2, y2, duration_ms)),
                timeout=15.0,
            )
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    # ------------------------------------------------------------------
    # App lifecycle
    # ------------------------------------------------------------------
    async def _apps_async(self, refresh: bool = False) -> dict[str, dict[str, Any]]:
        if self._app_svc is None:
            raise IOSCoreError("iPhone 应用通道尚未连接", "not_ready")
        now = time.monotonic()
        if not refresh and self._apps and now - self._apps_ts < 60.0:
            return dict(self._apps)
        apps = await self._app_svc.list_apps(include_container_paths=True)
        records: dict[str, dict[str, Any]] = {}
        for app in apps or []:
            bundle_id = _plain(app.get("bundleIdentifier"))
            path = _plain(app.get("path")).rstrip("/")
            if bundle_id and path:
                records[bundle_id] = {
                    "bundle": bundle_id,
                    "name": _plain(app.get("name")),
                    "path": path,
                    "version": _plain(app.get("version")),
                    "build": _plain(app.get("bundleVersion")),
                }
        self._apps = records
        self._apps_ts = now
        return dict(records)

    async def _resolve_bundle_async(self, bundle_id: str) -> str:
        requested = _plain(bundle_id) or "com.soda.music"
        apps = await self._apps_async()
        if requested in apps:
            return requested

        aliases = {
            "com.luna.music": "com.soda.music",
            "com.soda.music": "com.luna.music",
        }
        alias = aliases.get(requested)
        if alias and alias in apps:
            return alias

        needle = requested.lower()
        for app_id, app in apps.items():
            text = " ".join(str(value) for value in app.values()).lower()
            if needle and needle in text:
                return app_id
        for app_id, app in apps.items():
            text = (app.get("name", "") + " " + app.get("path", "")).lower()
            if any(word in text for word in ("汽水音乐", "汽水", "soda music", "schubert")):
                return app_id
        return requested

    async def _matching_processes_async(
        self, bundle_id: str, processes: Optional[list[dict[str, Any]]] = None
    ) -> list[dict[str, Any]]:
        resolved = await self._resolve_bundle_async(bundle_id)
        apps = await self._apps_async()
        target_path = _plain((apps.get(resolved) or {}).get("path")).rstrip("/")
        if processes is None:
            processes = await self._processes_async()
        hits: list[dict[str, Any]] = []
        for token in processes:
            executable = _token_executable(token)
            if target_path and (
                executable == target_path or executable.startswith(target_path + "/")
            ):
                hits.append(token)
                continue
            if _token_contains(token, resolved):
                hits.append(token)
        return hits

    async def _processes_async(self) -> list[dict[str, Any]]:
        if self._app_svc is None:
            raise IOSCoreError("iPhone 应用通道尚未连接", "not_ready")
        return list(await self._app_svc.list_processes())

    def processes(self) -> list[dict[str, Any]]:
        try:
            return self._call(self._locked(self._processes_async()), timeout=15.0)
        except Exception:
            return []

    def app_running(self, bundle_id: str) -> bool:
        try:
            return self._call(
                self._locked(self._app_running_async(bundle_id)),
                timeout=20.0,
            )
        except Exception:
            return False

    async def _app_running_async(self, bundle_id: str) -> bool:
        return bool(await self._matching_processes_async(bundle_id))

    async def _launch_async(self, bundle_id: str) -> None:
        if self._app_svc is None:
            raise IOSCoreError("iPhone 应用通道尚未连接", "not_ready")
        resolved = await self._resolve_bundle_async(bundle_id)
        apps = await self._apps_async()
        if resolved not in apps:
            raise IOSCoreError(
                "iPhone 上没有找到汽水音乐。请先从 App Store 安装并登录后再试",
                "app_not_installed",
            )
        try:
            await self._app_svc.launch_application(resolved, kill_existing=False)
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            raise IOSCoreError(
                "汽水音乐启动失败：%s。请确认 App 已安装、手机已解锁，然后重试" % message,
                "app_launch",
            ) from exc
        self._active_bundle = resolved

    def launch_app(self, bundle_id: str) -> bool:
        try:
            self._call(self._locked(self._launch_async(bundle_id), timeout=30.0), timeout=35.0)
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    async def _terminate_async(self, bundle_id: str) -> None:
        tokens = await self._processes_async()
        hit = await self._matching_processes_async(bundle_id, tokens)
        if not hit:
            self._active_bundle = ""
            return
        for token in hit:
            pid = _process_pid(token)
            if pid:
                with contextlib.suppress(Exception):
                    await self._app_svc.send_signal_to_process(pid, 9)
        self._active_bundle = ""

    def terminate_app(self, bundle_id: str) -> bool:
        try:
            self._call(self._locked(self._terminate_async(bundle_id), timeout=25.0), timeout=30.0)
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    def installed_apps(self) -> list[dict[str, Any]]:
        try:
            apps = self._call(
                self._locked(self._apps_async(refresh=True)),
                timeout=30.0,
            )
            return list(apps.values())
        except Exception:
            return []

    def resolve_app_bundle(self, bundle_id: str) -> str:
        try:
            return self._call(
                self._locked(self._resolve_bundle_async(bundle_id)),
                timeout=30.0,
            )
        except Exception:
            return _plain(bundle_id)

    def current_bundle(self) -> str:
        if self._active_bundle and self.app_running(self._active_bundle):
            return self._active_bundle
        return ""

    @property
    def is_ready(self) -> bool:
        return bool(self._ready and self._rsd is not None)

    @property
    def screen_size(self) -> tuple[int, int]:
        return self._screen_size

    @property
    def display_size(self) -> tuple[int, int]:
        return self._display_size

    @property
    def display_info(self) -> dict[str, Any]:
        return dict(self._display_info)

    @property
    def last_error(self) -> str:
        return self._last_error


_BACKEND: Optional[_IOSCore] = None
_BACKEND_LOCK = threading.Lock()


def backend() -> _IOSCore:
    global _BACKEND
    if _BACKEND is None:
        with _BACKEND_LOCK:
            if _BACKEND is None:
                _BACKEND = _IOSCore()
    return _BACKEND

