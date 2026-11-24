# -*- coding: utf-8 -*-
"""把源码同步进便携目录并打成 zip。"""
import os
import shutil
import zipfile

SRC = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(SRC, "dist", "汽水音乐自动化")
ZIP = os.path.join(SRC, "dist", "汽水音乐自动化-便携版.zip")

README = """汽水音乐自动化（本地部署）使用说明
================================================

【能做什么】
  自动做完汽水音乐「看广告领免费听」的整个流程：
  点领取奖励 → 等倒计时 → 变成「领取成功 ×」→ 点它 → 下一条，循环。
  纯 UI 自动化，不修改 App、不伪造进度。坐标按屏幕比例算，换手机不用改代码。

【运行环境】
  · Windows 10 / 11 64 位
  · 安卓手机 Android 7.0 以上（实测 Android 12 / 荣耀）
  · 不用装 Python、不用装 adb、不用装 .NET
    便携版已内置 Python 3.11 + adb 37.0.3 + OCR 模型 + VC 运行库，解压即用
  · 电脑能识别手机即可（Win10/11 一般插上自动装驱动；老系统装一次厂商 USB 驱动）

【准备】
  1. 安卓手机装好「汽水音乐」并登录。
  2. 打开「开发者选项 → USB 调试」：
     · 小米 / 红米：还要打开「USB 调试（安全设置）」，否则脚本点不动屏幕
     · 华为 / 荣耀：开发者选项里选「仅充电时允许调试」
     · vivo / OPPO / 一加：开发者选项 →「USB 调试」
  3. 数据线连电脑（要能传数据的线，纯充电线不行），
     手机弹「允许 USB 调试吗」→ 点允许（建议勾「始终允许」）。
  4. 手机保持亮屏，别锁屏。

【运行】
  双击「双击启动.bat」→ 浏览器自动打开控制台
  → 点「读取当前手机」→ 点「保存参数」→ 点「开始」。

【换手机 / 换电脑】
  换手机：插上新手机 →「读取当前手机」→「保存参数」，坐标全按比例算，不用改代码。
  换电脑：整个文件夹拷过去，装好手机驱动就能用（配置都跟着文件夹走）。
  多台手机同时插：在右侧「序列号」填要控制的那台（adb devices 里能看到）。

【分辨率】
  支持任意分辨率（1080×2388 / 1080×2400 / 1440×3200 都行），按屏幕百分比定位。
  万一识别不准，把「OCR宽」调到 640 或 800 再试。

【注意】
  · device_profile.json / vip_state.json / audio_state.json 是配置和计数，别删。
  · 打开「广告期间静音」后，开始会把媒体音量调到 0，停止自动恢复。
  · 被拖进直播间或 App 卡死时，程序会自动强杀重启 App 继续刷。
  · 全部在本机运行，不联网上传任何数据。

【排错】
  · 提示没找到 adb.exe：确认 platform-tools 文件夹在，或在右侧「ADB」填 adb.exe 完整路径。
  · 端口被占用：程序会自动换一个端口，看黑窗口里打印的地址。
  · 找不到设备：换数据线 / USB 口；手机上「撤销 USB 调试授权」再重新插。
  · 小米点不动：一定是没开「USB 调试（安全设置）」。

作者 QQ：2021106570
"""

BAT = """@echo off
chcp 65001 >nul
title 汽水音乐自动化本地部署
cd /d "%~dp0"
set "PATH=%~dp0platform-tools;%~dp0python;%~dp0python\\Scripts;%PATH%"
if not exist "%~dp0python\\python.exe" (
  echo [错误] 找不到内置 python，请确认完整解压后再运行。
  pause
  exit /b 1
)
echo 正在启动，浏览器会自动打开控制台...
echo 关闭这个窗口即退出。
echo.
"%~dp0python\\python.exe" qishui_gui.py
echo.
echo 程序已退出（上面是错误信息）。
pause
"""

VCRT = ["msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll"]


def main():
    # 1) 同步源码
    for name in ("qishui_auto.py", "qishui_gui.py", "requirements.txt"):
        shutil.copy2(os.path.join(SRC, name), os.path.join(DIST, name))
        print("copy", name)

    # 2) 补 VC 运行库（onnxruntime 依赖 msvcp140，缺了别人电脑上会 import 失败）
    pydir = os.path.join(DIST, "python")
    sys32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32")
    for dll in VCRT:
        s = os.path.join(sys32, dll)
        d = os.path.join(pydir, dll)
        if os.path.exists(s) and not os.path.exists(d):
            shutil.copy2(s, d)
            print("dll ", dll)

    # 3) 说明与启动器
    with open(os.path.join(DIST, "使用说明.txt"), "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(README)
    with open(os.path.join(DIST, "双击启动.bat"), "w", encoding="gbk", newline="\r\n") as f:
        f.write(BAT)

    # 4) 出厂默认配置（不带头手机信息）
    with open(os.path.join(DIST, "device_profile.json"), "w", encoding="utf-8") as f:
        f.write('{\n  "name": "",\n  "width": 0,\n  "height": 0,\n  "ocr_width": 720,\n  "adb": "",\n  "serial": ""\n}\n')

    # 5) 清运行时垃圾（含 site-packages 里的 __pycache__，能省 30MB）
    for junk in ("vip_state.json", "audio_state.json"):
        p = os.path.join(DIST, junk)
        if os.path.exists(p):
            os.remove(p)
    for junkdir in ("debug_clicks", "__pycache__"):
        p = os.path.join(DIST, junkdir)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    for dirpath, dirnames, filenames in os.walk(DIST):
        if "__pycache__" in dirnames:
            shutil.rmtree(os.path.join(dirpath, "__pycache__"), ignore_errors=True)
            dirnames.remove("__pycache__")
        for fn in filenames:
            if fn.endswith(".pyc"):
                try:
                    os.remove(os.path.join(dirpath, fn))
                except Exception:
                    pass

    # 6) 打包
    if os.path.exists(ZIP):
        os.remove(ZIP)
    root = os.path.dirname(DIST)
    base = os.path.basename(DIST)
    n = 0
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for dirpath, _dirnames, filenames in os.walk(DIST):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                rel = os.path.relpath(fp, root)
                z.write(fp, rel)
                n += 1
    size = os.path.getsize(ZIP) / 1024 / 1024
    print(f"ZIP OK  {n} files  {size:.1f} MB  -> {ZIP}")


if __name__ == "__main__":
    main()
