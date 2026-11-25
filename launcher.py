# -*- coding: utf-8 -*-
"""汽水音乐自动化 便携版启动器。

双击「一键启动.bat」后先做环境自检，再启动网页控制台。
不联网上传任何数据；只有在用户确认安装 iPhone 驱动时才会调用 winget。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path


BASE = Path(__file__).resolve().parent
PYTHON = BASE / "python" / "python.exe"
ADB = BASE / "platform-tools" / "adb.exe"
APP = BASE / "qishui_gui.py"
PROFILE = BASE / "device_profile.json"
APPLE_SERVICE = "Apple Mobile Device Service"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "QishuiMusicAutomation"
FIRST_RUN_MARKER = STATE_DIR / "first_run_v1.json"
DEFAULT_APP_VERSION = "1.4.0"

DEFAULT_PROFILE = {
    "platform": "android",
    "name": "",
    "width": 0,
    "height": 0,
    "ocr_width": 720,
    "adb": "",
    "serial": "",
    "ios_wda": "http://127.0.0.1:8100",
    "ios_bundle": "com.soda.music",
    "profiles": {
        "android": {
            "platform": "android",
            "name": "",
            "width": 0,
            "height": 0,
            "ocr_width": 720,
            "adb": "",
            "serial": "",
            "ios_wda": "http://127.0.0.1:8100",
            "ios_bundle": "com.soda.music",
        },
        "ios": {
            "platform": "ios",
            "name": "",
            "width": 0,
            "height": 0,
            "ocr_width": 720,
            "adb": "",
            "serial": "",
            "ios_wda": "http://127.0.0.1:8100",
            "ios_bundle": "com.soda.music",
        },
    },
}


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _pause(message: str = "") -> None:
    if message:
        print(message)
    try:
        input("按回车退出...")
    except (EOFError, KeyboardInterrupt):
        pass


def _run(args: list[str], timeout: float = 30.0, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(BASE),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        check=check,
    )


def _powershell(script: str, timeout: float = 20.0) -> str:
    exe = shutil.which("powershell") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    try:
        result = _run([exe, "-NoProfile", "-NonInteractive", "-Command", script], timeout=timeout)
    except Exception:
        return ""
    return (result.stdout or "").strip()


def _ensure_profile() -> None:
    if PROFILE.exists():
        try:
            with PROFILE.open("r", encoding="utf-8") as f:
                json.load(f)
            return
        except Exception:
            broken = PROFILE.with_suffix(".broken.json")
            try:
                shutil.copy2(PROFILE, broken)
            except OSError:
                pass
            print(f"[提示] device_profile.json 格式异常，已备份为 {broken.name} 并重建默认配置")
    with PROFILE.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(DEFAULT_PROFILE, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _check_files() -> list[str]:
    missing = []
    for path in (
        PYTHON,
        ADB,
        APP,
        BASE / "qishui_auto.py",
        BASE / "ios_core.py",
        BASE / "ios_wda.py",
        BASE / "wda_runner.py",
    ):
        if not path.exists():
            missing.append(str(path.relative_to(BASE)))
    return missing


def _first_run_needed() -> bool:
    return not FIRST_RUN_MARKER.exists()


def _app_version() -> str:
    try:
        data = json.loads((BASE / "version.json").read_text(encoding="utf-8"))
        version = str(data.get("version") or "").strip()
        if version:
            return version
    except (OSError, ValueError, TypeError):
        pass
    return DEFAULT_APP_VERSION


def _mark_first_run_done() -> None:
    try:
        FIRST_RUN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "version": _app_version(),
        }
        FIRST_RUN_MARKER.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _windows_status() -> tuple[bool, str]:
    if os.name != "nt":
        return False, f"{platform.system()} {platform.release()}"
    version = sys.getwindowsversion()
    label = "Windows 11" if version.build >= 22000 else f"Windows {version.major}.{version.minor}"
    arch = "64 位" if sys.maxsize > 2**32 else "32 位"
    return version.major >= 10 and sys.maxsize > 2**32, f"{label}（{arch}）"


def _show_android_setup() -> None:
    print("""
┌─ 安卓手机连接步骤 ──────────────────────────────┐
│ 1. 设置 → 关于手机 → 连续点击「版本号」7 次     │
│ 2. 设置 → 系统/更多设置 → 开发者选项            │
│ 3. 打开「USB 调试」，部分品牌还要开「USB 安装」 │
│ 4. 用数据线连接电脑，USB 用途选择「传输文件」   │
│ 5. 手机弹出「允许 USB 调试」时点允许            │
│ 6. 回到控制台，选择 Android → 读取 Android 手机 │
└────────────────────────────────────────────────┘
如果电脑设备管理器出现黄色感叹号：
  · 荣耀 / 华为：安装官方 HiSuite
  · 小米 / Redmi：安装小米手机助手
  · OPPO / 一加 / realme、vivo / iQOO：安装对应官方助手
  · Google / Pixel：使用 drivers\\Android\\google_usb
""")


def _show_iphone_setup() -> None:
    status = _apple_service_status().upper()
    if status == "STOPPED":
        print("[驱动] Apple Mobile Device Service 已安装但未运行，正在尝试启动...")
        if _try_start_apple_service():
            print("[驱动] Apple 服务已启动。")
        else:
            print("[驱动] 启动失败，请右键以管理员身份运行 drivers\\iPhone\\安装iPhone驱动.bat。")
    elif status != "RUNNING":
        print("[驱动] 未检测到 Apple Mobile Device Service，iPhone 将无法被电脑识别。")
        print("       可以现在安装 Apple 官方驱动组件，也可以稍后运行")
        print("       drivers\\iPhone\\安装iPhone驱动.bat")
        try:
            choice = input("        现在安装？[1 安装 / 2 稍后，默认 2]: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = "2"
        if choice == "1":
            _install_apple_driver()
            _pause("安装流程结束，按回车继续...")

    print("""
┌─ iPhone 连接步骤 ──────────────────────────────┐
│ 1. 安装 Apple 官方设备驱动（iTunes / Apple Devices）│
│ 2. 用 USB 数据线连接电脑                         │
│ 3. 手机解锁后点「信任此电脑」，输入锁屏密码       │
│ 4. 设置 → 隐私与安全性 → 开发者模式 → 打开       │
│ 5. 重启 iPhone，再回到控制台选择 iPhone          │
└────────────────────────────────────────────────┘
兼容范围：
  · iOS 27.0 及以上：USB 免签直连，不需要证书
  · iOS 17.0–26.x：控制台内按向导一键安装并启动 WDA
  · iOS 16 及以下：暂不支持，建议先升级系统
免费 Apple ID 的 WDA 签名通常约 7 天，到期后在控制台内续签。
首次准备 iPhone 组件需要联网，可能等待几分钟。
""")


def _first_run_wizard(imports_ok: bool, devices: list[dict[str, str]]) -> None:
    windows_ok, windows_label = _windows_status()
    apple_status = _apple_service_status().upper()
    apple_label = {
        "RUNNING": "已运行",
        "STOPPED": "已安装但未运行",
        "MISSING": "未安装（只影响 iPhone）",
    }.get(apple_status, apple_status)

    print("\n" + "=" * 62)
    print(" 首次使用 · 环境与驱动检查")
    print("=" * 62)
    print(f"  [{'OK' if windows_ok else '提示'}] 系统        {windows_label}")
    print(f"  [{'OK' if imports_ok else '异常'}] 运行库      {'完整' if imports_ok else '缺失或损坏'}")
    print(f"  [{'OK' if ADB.exists() else '异常'}] ADB         {'已内置' if ADB.exists() else '缺失'}")
    print(f"  [{'OK' if apple_status == 'RUNNING' else '提示'}] iPhone 驱动  {apple_label}")
    if devices:
        states = "，".join(f"{d['serial']} [{d['state']}]" for d in devices)
        print(f"  [OK] 安卓设备  {states}")
    elif _apple_device_present():
        print("  [OK] iPhone    已检测到 USB 连接")
    else:
        print("  [提示] 手机      暂未检测到，可先查看连接步骤")
    print("=" * 62)
    print(" 提示：Android 需要 USB 调试；iPhone 需要信任电脑和开发者模式。")
    try:
        choice = input(" 先看哪种手机的连接步骤？[1 安卓 / 2 iPhone / 3 两种 / 回车跳过]: ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = ""
    if choice == "1":
        _show_android_setup()
    elif choice == "2":
        _show_iphone_setup()
    elif choice == "3":
        _show_android_setup()
        _show_iphone_setup()
    else:
        print("已跳过首次向导。以后可双击「连接向导.bat」重新查看。")
    print("=" * 62)


def _check_imports() -> tuple[bool, str]:
    code = (
        "import cv2, numpy, onnxruntime, rapidocr_onnxruntime, "
        "pymobiledevice3, cryptography; print('ok')"
    )
    print(" 正在加载 OCR 与 iPhone 组件，首次启动可能需要 1–2 分钟，请稍候…", flush=True)
    try:
        result = _run([str(PYTHON), "-c", code], timeout=180.0)
    except subprocess.TimeoutExpired:
        return False, "组件加载超过 180 秒。请关闭杀毒软件拦截后重试，或重新解压完整便携包。"
    except Exception as exc:
        return False, str(exc)
    if result.returncode == 0 and "ok" in (result.stdout or ""):
        return True, ""
    detail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
    return False, detail[-1200:]


def _adb_devices() -> list[dict[str, str]]:
    try:
        result = _run([str(ADB), "devices", "-l"], timeout=8.0)
    except Exception:
        return []
    rows = []
    for line in (result.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] in ("device", "unauthorized", "offline", "authorizing"):
            rows.append({"serial": parts[0], "state": parts[1]})
    return rows


def _apple_service_status() -> str:
    if os.name != "nt":
        return "MISSING"
    script = (
        f"$s = Get-Service -Name '{APPLE_SERVICE}' -ErrorAction SilentlyContinue; "
        "if ($s) { $s.Status } else { 'MISSING' }"
    )
    return _powershell(script).strip() or "MISSING"


def _apple_device_present() -> bool:
    if os.name != "nt":
        return False
    script = (
        "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
        "Where-Object { $_.InstanceId -like 'USB\\VID_05AC*' } | "
        "Select-Object -First 1 -ExpandProperty FriendlyName"
    )
    return bool(_powershell(script, timeout=25.0).strip())


def _try_start_apple_service() -> bool:
    if os.name != "nt":
        return False
    try:
        result = _run(["net", "start", APPLE_SERVICE], timeout=30.0)
    except Exception:
        return False
    return result.returncode == 0 and _apple_service_status().upper() == "RUNNING"


def _install_apple_driver() -> bool:
    winget = shutil.which("winget")
    if winget:
        print("\n正在调用 winget 安装 Apple 官方驱动组件（会安装 iTunes，可能需要管理员确认）...")
        print("安装过程中请不要关闭本窗口。\n")
        try:
            result = subprocess.run(
                [
                    winget,
                    "install",
                    "--id",
                    "Apple.iTunes",
                    "-e",
                    "--source",
                    "winget",
                    "--silent",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ],
                cwd=str(BASE),
            )
        except Exception as exc:
            print(f"winget 执行失败：{exc}")
            result = None
        if result is not None and result.returncode == 0:
            if _apple_service_status().upper() in ("RUNNING", "STOPPED"):
                print("\nApple 驱动组件安装完成。")
                return True
        print("\n自动安装未完成。请从 Apple 官网手动安装 iTunes 或 Apple Devices。")
    else:
        print("\n当前系统没有 winget，将打开 Apple 官方下载页面。")

    webbrowser.open("https://support.apple.com/downloads/itunes")
    print("安装完成后重新双击「一键启动.bat」即可。")
    return False


def _android_driver_help() -> None:
    print("=" * 58)
    print(" Android USB 驱动说明")
    print("=" * 58)
    print(" adb 已内置，不需要单独安装 adb。")
    print(" Windows 10/11 通常会自动装好 ADB 接口驱动；若设备管理器出现黄色感叹号：")
    print("   1. 荣耀 / 华为：安装官方 HiSuite")
    print("   2. 小米 / Redmi：安装小米手机助手")
    print("   3. OPPO / 一加 / realme：安装对应手机助手")
    print("   4. vivo / iQOO：安装官方手机助手")
    print("   5. Google / Pixel / Nexus：可用 drivers\\Android\\google_usb 内驱动")
    print(" 装好后重新插线，手机点「允许 USB 调试」。")
    print("=" * 58)
    try:
        os.startfile("devmgmt.msc")  # type: ignore[attr-defined]
    except Exception:
        pass


def _maybe_setup_apple_driver() -> None:
    if os.name != "nt":
        return
    status = _apple_service_status().upper()
    if status == "RUNNING":
        return
    if status == "STOPPED":
        print("[驱动] Apple Mobile Device Service 未运行，正在尝试启动...")
        if _try_start_apple_service():
            print("[驱动] Apple 服务已启动")
        else:
            print("[驱动] 启动失败；如果要用 iPhone，请右键以管理员身份运行 drivers\\iPhone\\安装iPhone驱动.bat")
        return
    if not _apple_device_present():
        print("[驱动] 未检测到 iPhone。使用 iPhone 时如提示缺少 Apple 服务，再运行 drivers\\iPhone\\安装iPhone驱动.bat")
        return

    print("\n检测到 iPhone，但电脑上缺少 Apple Mobile Device Service 驱动。")
    print("  [1] 自动安装 Apple 官方驱动组件（推荐，需要联网和管理员权限）")
    print("  [2] 先启动，稍后手动安装")
    try:
        choice = input("请选择 [1/2，默认 2]: ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "2"
    if choice == "1":
        _install_apple_driver()
        _pause("安装流程结束，按回车继续启动...")
    else:
        print("已跳过。使用 iPhone 前请运行 drivers\\iPhone\\安装iPhone驱动.bat")


def _print_report(imports_ok: bool, import_detail: str, missing: list[str]) -> None:
    print("=" * 58)
    print(" 汽水音乐自动化 · 便携版环境自检")
    print("=" * 58)
    print(f"  运行目录     {BASE}")
    print(f"  Python       {sys.version.split()[0]}  ({PYTHON})")
    print(f"  运行库       {'OK' if imports_ok else '缺失'}")
    print(f"  adb          {'OK' if ADB.exists() else '缺失'}  ({ADB})")
    print(f"  iPhone 驱动  {_apple_service_status()}")
    devices = _adb_devices()
    if devices:
        print("  安卓设备     " + "，".join(f"{d['serial']} [{d['state']}]" for d in devices))
    else:
        print("  安卓设备     未检测到")
    if missing:
        print("  缺少文件     " + "，".join(missing))
    if not imports_ok and import_detail:
        print("-" * 58)
        print(import_detail)
    print("=" * 58)


def main() -> int:
    _configure_console()
    parser = argparse.ArgumentParser(description="汽水音乐自动化便携版启动器")
    parser.add_argument("--check", action="store_true", help="只做环境自检，不启动控制台")
    parser.add_argument("--setup", action="store_true", help="打开首次使用环境与连接向导")
    parser.add_argument("--install-iphone-driver", action="store_true", help="安装 Apple 官方 iPhone 驱动组件")
    parser.add_argument("--android-driver-help", action="store_true", help="显示安卓 USB 驱动说明")
    args = parser.parse_args()

    print("\n汽水音乐自动化 · Android + iPhone 双平台控制台\n")

    if args.install_iphone_driver:
        ok = _install_apple_driver()
        _pause("按回车关闭窗口...")
        return 0 if ok else 1

    if args.android_driver_help:
        _android_driver_help()
        _pause("按回车关闭窗口...")
        return 0

    missing = _check_files()
    if missing:
        _print_report(False, "", missing)
        _pause("便携包不完整，请重新解压整个压缩包后再运行。")
        return 1

    _ensure_profile()
    imports_ok, import_detail = _check_imports()
    _print_report(imports_ok, import_detail, [])
    if not imports_ok:
        _pause("运行库缺失或损坏，请重新解压完整便携包。")
        return 1

    devices = _adb_devices()
    if not devices:
        print("安卓提示：插好数据线 → 手机点「允许 USB 调试」；设备管理器有感叹号时安装厂商 USB 驱动。")
    elif all(d["state"] != "device" for d in devices):
        print("安卓提示：手机已连接但未授权，请在手机上点「允许 USB 调试」后重试。")

    if args.check:
        print("环境自检完成，可以启动。")
        return 0

    if args.setup:
        _first_run_wizard(imports_ok, devices)
        _mark_first_run_done()
        _pause("按回车关闭窗口...")
        return 0

    if _first_run_needed():
        _first_run_wizard(imports_ok, devices)
        _mark_first_run_done()
        _pause("按回车继续启动控制台...")

    _maybe_setup_apple_driver()

    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(
        [str(BASE), str(BASE / "python"), str(BASE / "platform-tools"), env.get("PATH", "")]
    )
    env.setdefault("PYTHONUTF8", "1")
    print("正在启动控制台，浏览器会自动打开...")
    try:
        result = subprocess.run([str(PYTHON), str(APP)], cwd=str(BASE), env=env)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        _pause(f"启动失败：{exc}")
        return 1
    if result.returncode:
        _pause(f"控制台异常退出（代码 {result.returncode}），请把上面的报错发给作者。")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
