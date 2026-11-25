# -*- coding: utf-8 -*-
"""构建 Windows 便携版：整合运行库、驱动说明、资源文件和启动器。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path


SRC = Path(__file__).resolve().parent
DIST = SRC / "dist" / "汽水音乐自动化"
PYTHON = DIST / "python" / "python.exe"
ADB = DIST / "platform-tools" / "adb.exe"
APP_VERSION = "1.4.0"
ZIP = SRC / "dist" / f"qishui-music-automation-v{APP_VERSION}-portable.zip"
SUPPORT_SRC = SRC / "private" / "support"
SUPPORT_DIST = DIST / "support"
SUPPORT_SIGNING_KEY = SRC / "private" / "support-signing.key"
SUPPORT_PUBLIC_KEY_B64 = "flBIRAA+UlJLYdPnvHVeVZZ+EKcWVgS8NBrCrL44NVg="
GOOGLE_USB_URL = "https://dl.google.com/android/repository/usb_driver_r13-windows.zip"
GOOGLE_USB_CACHE = SRC / "packaging" / "cache" / "usb_driver_r13-windows.zip"
VCRT = ["msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll"]

README = """汽水音乐自动脚本·便携版使用说明
================================================

【支持的手机】
  · Android 7.0 以上：USB 调试 + ADB
  · iPhone iOS 27.0 及以上：USB 免签直连，不装证书、不越狱
  · iPhone iOS 17.0–26.x：控制台内按向导一键安装 WebDriverAgent（WDA）
  · iPhone iOS 16 及以下：暂不支持，建议先升级系统
  · 电脑：Windows 10 / 11 64 位

【包内已经集成，不用另装】
  Python 3.11、adb / platform-tools、OpenCV、ONNX Runtime、
  RapidOCR 中文模型、pymobiledevice3（iPhone CoreDevice）、
  cryptography、VC++ 运行库、驱动助手、启动器和界面资源。
  iPhone 17–26 的 WDA 签名与运行组件会在首次配置时联网下载并校验。

【第一次使用】
  1. 把整个 zip 解压到硬盘，不要直接在压缩包里运行。
  2. 双击「一键启动.bat」。
  3. 首次启动会检查系统、Python 运行库、adb 和 iPhone 驱动。
  4. 按向导选择 Android 或 iPhone，查看对应的连接步骤。
  5. 以后可双击「连接向导.bat」重新打开环境与驱动检查。
  6. 浏览器会自动打开控制台。

【Android 连接】
  1. 设置 → 关于手机 → 连续点版本号，开启开发者选项。
  2. 开发者选项 → USB 调试。
     小米 / 红米还要打开「USB 调试（安全设置）」。
  3. 数据线连电脑，手机点「允许 USB 调试」。
  4. 控制台顶部选 Android → 读取当前设备 → 开始。

【iPhone 连接】
  1. Windows 需要 Apple 手机驱动（Apple Mobile Device Service）。
     首次连接 iPhone 时，启动器会提示自动安装；
     也可以双击 drivers\\iPhone\\安装iPhone驱动.bat，
     或自行安装 Apple 官方 iTunes / Apple Devices。
  2. iPhone 解锁 → 数据线连接 → 点「信任此电脑」并输入锁屏密码。
  3. 设置 → 隐私与安全性 → 开发者模式 → 打开并重启手机。
  4. 控制台顶部选 iPhone → 读取 iPhone → 开始。
  5. 首次点开始会自动准备 Apple 开发者组件，需要联网，可能需要几分钟。
     不需要证书、不需要签名、不需要越狱；iOS 27.0 及以上走免签直连。
     iOS 17.0–26.x 会自动弹出兼容向导：
       ① 打开 Apple ID 登录入口，在 Impactor 窗口完成登录；
       ② 点「安装 / 续签 WDA」；
       ③ 点「启动 WDA」，显示已就绪后返回开始任务。
     免费 Apple ID 签名通常约 7 天有效，到期后在向导中一键续签。
     Apple ID 密码只在 Impactor 窗口输入，本程序不读取、不保存密码。
     iOS 16 及以下未纳入当前兼容范围。
  6. 汽水音乐需要已经安装并登录（Bundle: com.soda.music）。

【iOS 17.0–26.x 手动签名备选】
  如果 Impactor 登录不稳定，可以使用随包 assets\\ios\\WDA.ipa：
  1. 从 https://sideloadly.io/ 安装 Sideloadly。
  2. iPhone 解锁并连接 USB，点「信任此电脑」。
  3. 把 assets\\ios\\WDA.ipa 拖入 Sideloadly。
  4. 输入一个可正常登录的免费 Apple ID、密码和双重认证验证码，点 Start。
  5. 设置 → 通用 → VPN 与设备管理 → 信任该开发者。
  6. 设置 → 隐私与安全性 → 开发者模式 → 开启并重启。
  7. 回控制台 → iPhone → WDA 配置 → 检查状态 → 启动 WDA。
  免费签名通常约 7 天有效，到期用同一个 WDA.ipa 在 Sideloadly 重新签名。
  详细图文：assets\\ios\\iOS自签安装教程.webp

【界面参数】
  OCR宽：截图缩放到这个宽度再识别。720 通用；
         识别不准可调 800，电脑/手机卡顿可调 640。
  广告极速：默认开启，优先识别跳过按钮并减少无效扫描；
          遇到个别广告识别不到按钮时可临时关闭，回退完整扫描。
  WDA地址：默认 http://127.0.0.1:8100，由兼容向导自动配置。
  Bundle：iOS 应用标识，汽水音乐为 com.soda.music。

【常见问题】
  · 启动器提示运行库缺失：重新解压完整 zip，不要只复制 bat。
  · 安卓插着但读不到：换数据线 / USB 口；手机上重新允许调试；
    设备管理器有黄色感叹号时安装手机厂商 USB 驱动。
  · iPhone 读不到：确认 Apple 驱动服务已安装、手机已信任电脑、
    开发者模式已开启。
  · 提示 iOS 17.0–26.x 需要 WDA：这是 Apple CoreDevice 的系统限制。
    在控制台打开「旧版 iPhone 兼容通道」，按向导登录、安装、启动。
  · WDA 提示签名过期：免费签名通常约 7 天，重新点「安装 / 续签 WDA」。
  · Apple ID 输入：只在打开的 Impactor 登录窗口内输入，程序不接触密码。
  · 端口被占用：程序会自动换端口，看控制台窗口打印的地址。
  · 首次 iPhone 准备很慢：Apple 开发者组件需要下载，之后会快很多。
  · 天数显示 --：广告页没有「第N日免费听」时读不到，开始运行后会自动补。

【数据与隐私】
  全部在本机运行，不上传日志、截图或账号信息。
  配置文件：device_profile.json
  运行状态：vip_state.json / audio_state.json（首次运行自动生成）

【使用许可】
  仅限个人学习、研究与非商业使用。
  转载或二次分发必须保留作者标识、项目名称和原仓库链接。
  完整条款见包内 LICENSE 文件。

作者：guyue1337
GitHub：https://github.com/guyue1337
"""

BAT_MAIN = """@echo off
chcp 65001 >nul
setlocal
title Qishui Music Automation
cd /d "%~dp0"
set "PATH=%~dp0;%~dp0python;%~dp0python\\Scripts;%~dp0platform-tools;%PATH%"
if not exist "%~dp0python\\python.exe" (
  echo [ERROR] Bundled Python not found. Please extract the whole zip again.
  pause
  exit /b 1
)
"%~dp0python\\python.exe" "%~dp0launcher.py"
if errorlevel 1 pause
"""

BAT_CHECK = """@echo off
chcp 65001 >nul
setlocal
title Qishui Environment Check
cd /d "%~dp0"
set "PATH=%~dp0;%~dp0python;%~dp0python\\Scripts;%~dp0platform-tools;%PATH%"
if not exist "%~dp0python\\python.exe" (
  echo [ERROR] Bundled Python not found. Please extract the whole zip again.
  pause
  exit /b 1
)
"%~dp0python\\python.exe" "%~dp0launcher.py" --check
pause
"""

SETUP_BAT = """@echo off
chcp 65001 >nul
setlocal
title Qishui First Run Setup
cd /d "%~dp0"
set "PATH=%~dp0;%~dp0python;%~dp0python\\Scripts;%~dp0platform-tools;%PATH%"
if not exist "%~dp0python\\python.exe" (
  echo [ERROR] Bundled Python not found. Please extract the whole zip again.
  pause
  exit /b 1
)
"%~dp0python\\python.exe" "%~dp0launcher.py" --setup
"""

IPHONE_DRIVER_BAT = """@echo off
chcp 65001 >nul
setlocal
title Install iPhone USB Driver
cd /d "%~dp0..\\.."
if not exist "python\\python.exe" (
  echo [ERROR] Bundled Python not found.
  pause
  exit /b 1
)
"python\\python.exe" "launcher.py" --install-iphone-driver
pause
"""

ANDROID_DRIVER_BAT = """@echo off
chcp 65001 >nul
setlocal
title Android USB Driver Help
cd /d "%~dp0..\\.."
if not exist "python\\python.exe" (
  echo [ERROR] Bundled Python not found.
  pause
  exit /b 1
)
"python\\python.exe" "launcher.py" --android-driver-help
pause
"""

DRIVERS_README = """驱动说明
================================================

一、Android
  adb 已经内置在 platform-tools，不需要单独安装 adb。
  Windows 10 / 11 通常会在插线后自动安装 ADB 接口驱动。

  如果设备管理器出现黄色感叹号，或者控制台一直读不到设备：
    · 荣耀 / 华为：安装官方 HiSuite
    · 小米 / Redmi：安装小米手机助手
    · OPPO / 一加 / realme：安装对应官方手机助手
    · vivo / iQOO：安装官方手机助手
    · Google / Pixel / Nexus：可使用 Android\\google_usb 内的 Google USB 驱动
  安装后重新插线，并在手机上点「允许 USB 调试」。

二、iPhone
  iOS 27.0 及以上不需要证书、也不需要 WebDriverAgent。
  iOS 17.0–26.x 在控制台内按「旧版 iPhone 兼容通道」向导安装 WDA。
  免费 Apple ID 签名通常约 7 天，到期后在同一个向导内续签。
  如果内置登录入口不稳定，可用 assets\\ios\\WDA.ipa + Sideloadly 手动签名；
  图文步骤见 assets\\ios\\iOS自签安装教程.webp。
  iOS 16 及以下暂不支持。
  电脑必须安装 Apple Mobile Device Service，它随 iTunes 或 Apple Devices 安装。

  推荐操作：
    1. 双击 iPhone\\安装iPhone驱动.bat
    2. 让启动器自动调用 winget 安装 Apple 官方 iTunes
    3. 安装完成后重新插线，手机点「信任此电脑」
    4. iPhone 设置 → 隐私与安全性 → 开发者模式 → 打开并重启

  如果自动安装失败，请从 Apple 官网下载 iTunes 或 Apple Devices：
    https://support.apple.com/downloads/itunes

三、首次使用 iPhone 时
  Apple 开发者组件需要联网下载，可能等待几分钟。
  后续启动会复用缓存，速度会快很多。
"""

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


def log(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> None:
    print(f"[ERROR] {message}", flush=True)
    raise SystemExit(1)


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="\n") as f:
        f.write(text)


def copy_tree(src: Path, dst: Path, ignore=None) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=ignore)
    log(f"copy  {src.name}/ -> {dst.relative_to(DIST)}/")


def check_runtime() -> None:
    if not PYTHON.exists():
        fail(f"找不到内置 Python：{PYTHON}\n请先把 python 运行时放进 dist 目录。")
    if not ADB.exists():
        fail(f"找不到内置 adb：{ADB}\n请先把 platform-tools 放进 dist 目录。")
    code = (
        "import cv2, numpy, onnxruntime, rapidocr_onnxruntime, "
        "pymobiledevice3, cryptography; print('ok')"
    )
    log("check  正在加载 OCR / iOS 依赖（首次可能较慢）…")
    try:
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        fail("内置 Python 运行库加载超过 180 秒，请检查磁盘占用或杀毒软件拦截。")
    if result.returncode != 0 or "ok" not in result.stdout:
        fail("内置 Python 缺少运行库：\n" + (result.stderr or result.stdout)[-1600:])
    log("check  Python / OCR / iOS 依赖 OK")


def copy_sources() -> None:
    for name in ("qishui_gui.py", "qishui_auto.py", "ios_core.py",
                 "ios_wda.py", "wda_runner.py", "launcher.py",
                 "requirements.txt", "LICENSE", "CHANGELOG.md",
                 "THIRD_PARTY_NOTICES.md"):
        src = SRC / name
        if not src.exists():
            fail(f"缺少源码：{name}")
        shutil.copy2(src, DIST / name)
        log(f"copy  {name}")


def copy_assets() -> None:
    src = SRC / "assets"
    if not src.is_dir() or not (src / "avatar.webp").exists():
        fail("缺少 assets/avatar.webp")
    copy_tree(src, DIST / "assets", ignore=shutil.ignore_patterns("ios-signing-guide.webp"))


def _support_manifest_body(files: dict[str, str], version: int) -> bytes:
    return json.dumps({
        "version": int(version),
        "files": files,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def copy_support() -> None:
    if SUPPORT_DIST.exists():
        shutil.rmtree(SUPPORT_DIST)
    if not SUPPORT_SRC.is_dir():
        return

    plaintext = [
        path.name for path in SUPPORT_SRC.iterdir()
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    ]
    if plaintext:
        fail("私有支持资源中发现明文图片，已阻止打包：" + "，".join(plaintext))

    encrypted = sorted(SUPPORT_SRC.glob("*.enc"))
    key_path = SUPPORT_SRC / ".key"
    if not encrypted:
        return
    if not key_path.exists():
        fail("私有支持资源缺少密钥文件")
    if not SUPPORT_SIGNING_KEY.exists():
        fail("缺少私有支持模块签名密钥")

    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        cipher = Fernet(key_path.read_bytes().strip())
        for path in encrypted:
            payload = cipher.decrypt(path.read_bytes())
            _, sep, image = payload.partition(b"\n")
            valid = (
                image.startswith(b"\x89PNG\r\n\x1a\n")
                or image.startswith(b"\xff\xd8\xff")
                or (len(image) > 12 and image[:4] == b"RIFF" and image[8:12] == b"WEBP")
            )
            if not sep or not valid:
                fail(f"私有支持资源校验失败：{path.name}")

        private_raw = base64.urlsafe_b64decode(SUPPORT_SIGNING_KEY.read_bytes().strip())
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_raw)
        public_raw = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        if base64.b64encode(public_raw).decode("ascii") != SUPPORT_PUBLIC_KEY_B64:
            fail("支持模块签名密钥与程序内置公钥不匹配")
    except SystemExit:
        raise
    except Exception as exc:
        fail(f"私有支持资源验证失败：{exc}")

    SUPPORT_DIST.mkdir(parents=True)
    files: dict[str, str] = {}
    for path in [key_path, *encrypted]:
        target = SUPPORT_DIST / path.name
        shutil.copy2(path, target)
        files[path.name] = hashlib.sha256(target.read_bytes()).hexdigest()

    version = int(datetime.now().timestamp())
    signature = private_key.sign(_support_manifest_body(files, version))
    manifest = {
        "version": version,
        "files": files,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    write_text(
        SUPPORT_DIST / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    log(f"check  support module signed ({len(files)} files)")


def add_vc_runtime() -> None:
    sys32 = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32"
    pydir = DIST / "python"
    for dll in VCRT:
        src = sys32 / dll
        dst = pydir / dll
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            log(f"dll   {dll}")


def prepare_drivers() -> None:
    root = DIST / "drivers"
    if root.exists():
        shutil.rmtree(root)
    (root / "iPhone").mkdir(parents=True)
    (root / "Android").mkdir(parents=True)
    write_text(root / "iPhone" / "安装iPhone驱动.bat", IPHONE_DRIVER_BAT)
    write_text(root / "Android" / "安卓USB驱动说明.bat", ANDROID_DRIVER_BAT)
    write_text(root / "驱动说明.txt", DRIVERS_README, encoding="utf-8-sig")
    log("make  drivers/")

    try:
        GOOGLE_USB_CACHE.parent.mkdir(parents=True, exist_ok=True)
        if not GOOGLE_USB_CACHE.exists():
            log("download Google USB driver ...")
            urllib.request.urlretrieve(GOOGLE_USB_URL, GOOGLE_USB_CACHE)
        target = root / "Android" / "google_usb"
        target.mkdir(parents=True)
        with zipfile.ZipFile(GOOGLE_USB_CACHE) as z:
            for info in z.infolist():
                if not info.filename.startswith("usb_driver/") or info.is_dir():
                    continue
                rel = info.filename[len("usb_driver/"):]
                if not rel:
                    continue
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
        log("make  drivers/Android/google_usb/")
    except Exception as exc:
        log(f"warn  Google USB driver 未下载：{exc}")


def prepare_config_and_docs() -> None:
    with (DIST / "device_profile.json").open("w", encoding="utf-8", newline="\n") as f:
        json.dump(DEFAULT_PROFILE, f, ensure_ascii=False, indent=2)
        f.write("\n")
    write_text(DIST / "一键启动.bat", BAT_MAIN)
    write_text(DIST / "双击启动.bat", BAT_MAIN)
    write_text(DIST / "环境自检.bat", BAT_CHECK)
    write_text(DIST / "使用说明.txt", README, encoding="utf-8-sig")
    log("make  one-click launchers / docs / default profile")
    write_text(DIST / "连接向导.bat", SETUP_BAT)


def package_versions() -> None:
    code = r"""
import json
import platform
from importlib.metadata import version

packages = ["pymobiledevice3", "rapidocr-onnxruntime", "opencv-python", "numpy", "onnxruntime", "cryptography"]
data = {}
for name in packages:
    try:
        data[name] = version(name)
    except Exception:
        data[name] = "unknown"
print(json.dumps({
    "python": platform.python_version(),
    "packages": data,
}, ensure_ascii=False))
"""
    versions = {}
    try:
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        versions = json.loads((result.stdout or "").strip() or "{}")
    except Exception:
        versions = {"python": "unknown", "packages": {}}
    manifest = {
        "name": "汽水音乐自动脚本",
        "version": APP_VERSION,
        "edition": "portable",
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": versions.get("python", "unknown"),
        "packages": versions.get("packages", {}),
    }
    write_text(DIST / "version.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    log(f"make  version.json  v{APP_VERSION}")


def clean_runtime() -> None:
    legacy_reward = DIST / "reward"
    if legacy_reward.is_dir():
        shutil.rmtree(legacy_reward, ignore_errors=True)
        log("clean legacy reward/")
    for name in (
        "vip_state.json",
        "audio_state.json",
        "device_profile.broken.json",
        "device_profile.local.json",
    ):
        path = DIST / name
        if path.exists():
            path.unlink()
    for name in ("debug_clicks", "__pycache__"):
        path = DIST / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    for path in DIST.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)
    for path in DIST.rglob("*.pyc"):
        try:
            path.unlink()
        except OSError:
            pass
    for path in DIST.glob("*.log"):
        try:
            path.unlink()
        except OSError:
            pass
    log("clean runtime junk")


def build_zip() -> None:
    if ZIP.exists():
        ZIP.unlink()
    count = 0
    log("zip   building...")
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as z:
        for path in sorted(DIST.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            arc = Path(DIST.name) / path.relative_to(DIST)
            z.write(path, arc.as_posix())
            count += 1
            if count % 1000 == 0:
                log(f"zip   {count} files ...")
    size = ZIP.stat().st_size / 1024 / 1024
    log(f"ZIP OK  {count} files  {size:.1f} MB  -> {ZIP}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-zip", action="store_true", help="只同步便携目录，不生成 zip")
    args = parser.parse_args()

    if not DIST.is_dir():
        fail(f"便携目录不存在：{DIST}")
    log(f"build {APP_VERSION} -> {DIST}")
    check_runtime()
    copy_sources()
    copy_assets()
    copy_support()
    add_vc_runtime()
    prepare_drivers()
    prepare_config_and_docs()
    package_versions()
    clean_runtime()
    if not args.no_zip:
        build_zip()
    log("done")


if __name__ == "__main__":
    main()
