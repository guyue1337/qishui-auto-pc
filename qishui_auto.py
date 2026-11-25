# -*- coding: utf-8 -*-
"""
汽水音乐自动看广告（纯 UI 自动化）

通道：
  主通道 —— adb exec-out screencap + RapidOCR（视频广告是持续渲染的 SurfaceView，
            uiautomator 等不到 idle，实测必然失败，所以 OCR 才是主通道）
  可选   —— adb shell uiautomator dump（部分设备/静态页可用，启动时自动探测，
            连续失败会自动关闭）
  附加   —— input keyevent 164 静音（退出恢复）

声明：只点击界面上真实存在的按钮，不修改 App、不伪造广告观看进度、不加速播放。
      广告该播多久还是多久，本脚本只负责替你点“领取奖励 / 继续观看 / 跳过”。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import base64
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

# Windows 控制台默认 GBK，强制 UTF-8 避免中文 print 报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ================== 配置区 ==================
# 领奖目标（按优先级，先点靠前的）
REWARD_TARGETS = ["领取奖励", "继续观看"]
# 关闭类目标：先退出会挡住流程的互动弹窗，再关闭广告结束页右上角的「领取成功」胶囊
CLOSE_TARGETS = ["坚持退出", "退出不看了", "领取成功"]
# 「xx秒后 / xx s 后进入直播间 [取消]」：文字需同时含倒计时和「进入直播间」，
# 避免把普通横幅「点击进入直播间」也当成自动跳转

# 跳过目标（仅当广告本身允许跳过时才会出现）
SKIP_TARGETS = ["跳过广告", "跳过", "关闭广告"]
ENABLE_SKIP = True          # 提前点“跳过”有可能拿不到奖励，视情况关掉
AD_FAST_MODE = True         # 广告极速：优先识别跳过，播放期间只扫必要区域
AD_FAST_FALLBACK_EVERY = 4  # 极速模式下每几轮补扫一次底部/免费听区域，防止卡住

USE_UIAUTOMATOR = False     # True / False / "auto"。默认关：它看不到弹窗等覆盖层，会抢占 OCR
OCR_SCORE = 0.5             # OCR 置信度下限
FUZZY = True                # 允许轻微识别误差
POLL_INTERVAL = 0.1         # 每轮间隔（截图+OCR 本身约 0.8~1.5s）
OCR_FALLBACK_INTERVAL = 2.5 # 保留：uiautomator 兜底时的最小间隔
CLICK_COOLDOWN = 0.4        # 点击后冷却；同坐标重复点击由 do_click 的 3s 去重兜底
MUTE_ON_START = True        # 开始运行就静音手机，停止后恢复原音量
OCR_MAX_WIDTH = 720         # OCR 缩放宽度（太小会漏「取消」等小字，别低于 600）
SAVE_CLICK_DEBUG = True     # 命中时保存标注截图
DUMP_PATH = "/sdcard/qishui_dump.xml"
UIA_TIMEOUT = 4             # uiautomator 单次超时；失败会浪费这么久，所以探测后就别频繁试
UIA_MAX_FAIL = 3            # 连续失败几次后自动关闭 uiautomator

# 各目标允许点击的区域（相对屏幕宽高 0~1）
TARGET_ROI = {
    "领取奖励": {"x": (0.20, 0.80), "y": (0.30, 0.78)},
    "继续观看": {"x": (0.20, 0.80), "y": (0.30, 0.78)},
    "领取成功": {"x": (0.55, 1.00), "y": (0.00, 0.20)},
    "坚持退出": {"x": (0.18, 0.82), "y": (0.42, 0.82)},
    "退出不看了": {"x": (0.00, 1.00), "y": (0.80, 1.00)},
    "跳过广告": {"x": (0.55, 1.00), "y": (0.00, 0.20)},
    "跳过":     {"x": (0.55, 1.00), "y": (0.00, 0.20)},
    "关闭广告": {"x": (0.55, 1.00), "y": (0.00, 0.20)},
}
CENTER_TARGETS = {"领取奖励", "继续观看"}
# 首页「免费听N天」按钮固定在底部右侧，局部识别比整屏 OCR 快很多
FREE_LISTEN_ROI = (0.55, 0.73, 0.97, 0.90)
# 自动流程只识别按钮附近的小区域。iOS 全屏 OCR 约 5-7 秒，小区域通常 0.2-1.5 秒。
FAST_TOP_ROI = (0.62, 0.02, 1.00, 0.18)
FAST_REWARD_ROI = (0.10, 0.38, 0.90, 0.66)
FAST_BOTTOM_ROI = (0.00, 0.80, 1.00, 0.99)
# ===========================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(SCRIPT_DIR, "debug_clicks")
DEFAULT_PROFILE_PATH = os.path.join(SCRIPT_DIR, "device_profile.json")
LOCAL_PROFILE_PATH = os.path.join(SCRIPT_DIR, "device_profile.local.json")
# 本机私密配置优先，仓库里的 device_profile.json 只保留空白模板。
PROFILE_PATH = LOCAL_PROFILE_PATH if os.path.exists(LOCAL_PROFILE_PATH) else DEFAULT_PROFILE_PATH
PROFILE = {}
PLATFORM = "android"
IOS_WDA_URL = "http://127.0.0.1:8100"
IOS_BUNDLE_ID = "com.soda.music"
IOS_LAST_ERROR = ""
IOS_CORE_LAST_ERROR = ""
LAST_FREE_DAYS = None


def _apply_profile_settings():
    """把 device_profile.json 里的平台配置同步到模块全局。"""
    global PLATFORM, IOS_WDA_URL, IOS_BUNDLE_ID
    p = PROFILE or {}
    platform = str(p.get("platform") or "android").strip().lower()
    PLATFORM = platform if platform in ("android", "ios") else "android"
    IOS_WDA_URL = (p.get("ios_wda") or "http://127.0.0.1:8100").strip().rstrip("/")
    IOS_BUNDLE_ID = (p.get("ios_bundle") or "com.soda.music").strip()


def configure_platform(platform=None, wda=None, bundle=None):
    """页面上临时切换平台时调用；不写文件，点保存后才持久化。"""
    global PLATFORM, IOS_WDA_URL, IOS_BUNDLE_ID, IOS_LAST_ERROR, IOS_CORE_LAST_ERROR, screen_cache
    platform = str(platform or "").strip().lower()
    if platform in ("android", "ios"):
        PLATFORM = platform
    if wda is not None:
        IOS_WDA_URL = str(wda).strip().rstrip("/") or "http://127.0.0.1:8100"
    if bundle is not None:
        IOS_BUNDLE_ID = str(bundle).strip() or "com.soda.music"
    IOS_LAST_ERROR = ""
    IOS_CORE_LAST_ERROR = ""
    screen_cache = None
    _device_cache.update(v=None, ts=0.0)
    _ios_device_cache.update(v=None, ts=0.0)
    _wda_status_cache.update(v=None, ts=0.0)
    _wda_session.update(id=None, ts=0.0)
    _wda_window_cache.update(v=None, ts=0.0)
    _wda_app_cache.update(v="", ts=0.0)
    PROFILE["platform"] = PLATFORM
    PROFILE["ios_wda"] = IOS_WDA_URL
    PROFILE["ios_bundle"] = IOS_BUNDLE_ID


def _read_profile_file():
    """读取 device_profile.json 原始内容，不做平台解析。"""
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _profile_platform(data, platform=None):
    p = str(platform or (data or {}).get("platform") or "android").strip().lower()
    return p if p in ("android", "ios") else "android"


def read_profile(platform=None):
    """只读参数；platform 可指定读取 Android 或 iOS 的独立配置。"""
    raw = _read_profile_file()
    want = _profile_platform(raw, platform)
    sections = raw.get("profiles")
    if isinstance(sections, dict):
        sec = sections.get(want)
        if isinstance(sec, dict) and sec:
            out = dict(sec)
            out["platform"] = want
            return out
    active = _profile_platform(raw)
    out = {k: v for k, v in raw.items() if k != "profiles"}
    out["platform"] = want
    if want != active:
        out["name"] = ""
        out["width"] = 0
        out["height"] = 0
        if want == "ios":
            out["serial"] = ""
    return out


def load_profile():
    """读取并应用 device_profile.json：机型、平台、WDA、adb 路径等。"""
    global PROFILE
    PROFILE = read_profile()
    _apply_profile_settings()
    return PROFILE


def save_profile(data):
    global PROFILE
    incoming = dict(data or {})
    want = _profile_platform(incoming)
    incoming["platform"] = want
    raw = _read_profile_file()
    sections = raw.get("profiles")
    if not isinstance(sections, dict):
        sections = {}
        old_platform = _profile_platform(raw)
        if raw.get("name") or raw.get("width"):
            sections[old_platform] = {
                k: v for k, v in raw.items() if k != "profiles"
            }
    sections[want] = incoming
    raw.update(incoming)
    raw["profiles"] = sections
    PROFILE = incoming
    _apply_profile_settings()
    configure_platform(PLATFORM, IOS_WDA_URL, IOS_BUNDLE_ID)
    try:
        with open(PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _profile_serial():
    s = (PROFILE.get("serial") or "").strip()
    return s

click_seq = 0
ocr_engine = None
last_ocr_ts = 0.0
screen_cache = None
muted = False
saved_volume = None
uia_ok = None
uia_fails = 0
last_click = {"target": None, "x": -1, "y": -1, "ts": 0.0}


# ------------------------- adb 基础 -------------------------
_NOWIN = {}
if os.name == "nt":
    _NOWIN["creationflags"] = 0x08000000  # CREATE_NO_WINDOW，避免每次调用闪黑框


def adb_path():
    """按优先级找 adb，换电脑/换手机都不会缺驱动：
    1) 控制台里手填的路径（device_profile.json 的 adb 字段）
    2) 本目录自带的 platform-tools\\adb.exe
    3) 系统 PATH 里的 adb
    4) 常见的 Android SDK / 模拟器安装位置
    """
    exe = "adb.exe" if os.name == "nt" else "adb"

    p = (PROFILE.get("adb") or "").strip().strip('"')
    if p:
        p = os.path.expandvars(os.path.expanduser(p))
        if os.path.isdir(p):
            p = os.path.join(p, exe)
        if os.path.exists(p):
            return p

    for cand in (
        os.path.join(SCRIPT_DIR, "platform-tools", exe),
        os.path.join(SCRIPT_DIR, "platform-tools", "platform-tools", exe),
    ):
        if os.path.exists(cand):
            return cand

    found = shutil.which("adb")
    if found:
        return found

    home = os.path.expanduser("~")
    cands = []
    if os.name == "nt":
        cands += [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", exe),
            os.path.join(home, "AppData", "Local", "Android", "Sdk", "platform-tools", exe),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Nox", "bin", "adb.exe"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "BlueStacks_nxt", "HD-Adb.exe"),
            r"C:\platform-tools\adb.exe",
            r"D:\platform-tools\adb.exe",
            r"C:\Android\platform-tools\adb.exe",
            r"D:\Android\platform-tools\adb.exe",
        ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return "adb"


def _raw_adb(args, binary=False, timeout=20):
    """不带 -s 的底层调用（用于自己枚举设备）"""
    try:
        r = subprocess.run([adb_path()] + args, capture_output=True, timeout=timeout, **_NOWIN)
        if r.returncode != 0:
            return None
        return r.stdout if binary else r.stdout.decode("utf-8", "ignore")
    except Exception:
        return None


_auto_serial = {"v": None, "ts": 0.0}
_device_cache = {"v": None, "ts": 0.0}


def list_devices():
    """解析 adb devices -l，返回所有设备（含 unauthorized / offline 状态）。"""
    out = _raw_adb(["devices", "-l"]) or ""
    items = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        item = {"serial": parts[0], "state": parts[1], "raw": line}
        for token in parts[2:]:
            if ":" in token:
                k, v = token.split(":", 1)
                item[k] = v
        items.append(item)
    return items


def _any_serial():
    """没填序列号时，自动挑第一台在线设备；多台设备也能用"""
    now = time.time()
    if _auto_serial["v"] and now - _auto_serial["ts"] < 30:
        return _auto_serial["v"]
    for dev in list_devices():
        if dev.get("state") == "device":
            _auto_serial["v"] = dev["serial"]
            _auto_serial["ts"] = now
            return dev["serial"]
    _auto_serial["v"] = None
    return None


def _getprop(props, key):
    m = re.search(r"^\[" + re.escape(key) + r"\]:\s*\[(.*?)\]\s*$", props or "", re.M)
    return m.group(1).strip() if m else ""


def _prop_any(props, *keys):
    for key in keys:
        value = _getprop(props, key)
        if value:
            return value
    return ""


def _get_android_device_info(serial=None, force=False):
    """一次性读取设备状态、型号、品牌、Android 版本、分辨率、DPI。

    读取失败时也返回结构化结果和 error 文案，GUI 可以直接显示原因。
    """
    now = time.time()
    if not force and _device_cache["v"] and now - _device_cache["ts"] < 1.5:
        return dict(_device_cache["v"])

    devices = list_devices()
    wanted = (serial or _profile_serial() or "").strip()
    online = [d for d in devices if d.get("state") == "device"]
    selected = None
    if wanted:
        selected = next((d for d in devices if d.get("serial") == wanted), None)
    if selected is None and online:
        selected = online[0]
    if selected is None and devices:
        selected = devices[0]

    info = {
        "connected": False,
        "state": "no_device",
        "serial": "",
        "model": "",
        "brand": "",
        "manufacturer": "",
        "name": "",
        "android": "",
        "sdk": "",
        "build": "",
        "device": "",
        "width": 0,
        "height": 0,
        "physical_width": 0,
        "physical_height": 0,
        "density": 0,
        "physical_density": 0,
        "error": "",
    }

    if selected is None:
        info["error"] = "未检测到设备：请检查 USB 连接与「USB 调试」授权"
        _device_cache.update(v=info, ts=now)
        return dict(info)

    info["serial"] = selected.get("serial", "")
    info["state"] = selected.get("state", "unknown")
    if info["state"] == "unauthorized":
        info["error"] = "设备未授权：请在手机上点「允许 USB 调试」后重试"
    elif info["state"] == "offline":
        info["error"] = "设备离线：请重新插拔数据线后重试"
    elif info["state"] != "device":
        info["error"] = "设备状态异常：%s" % info["state"]
    if info["error"]:
        _device_cache.update(v=info, ts=now)
        return dict(info)

    sn = info["serial"]
    props = adb(["shell", "getprop"], serial=sn, timeout=10) or ""
    info["model"] = (_prop_any(props, "ro.product.model", "ro.product.marketname",
                               "ro.product.odm.model", "ro.product.vendor.model",
                               "ro.product.system.model")
                     or selected.get("model", ""))
    info["brand"] = _prop_any(props, "ro.product.brand", "ro.product.vendor.brand",
                              "ro.product.manufacturer")
    info["manufacturer"] = _prop_any(props, "ro.product.manufacturer",
                                     "ro.product.vendor.manufacturer")
    info["android"] = _prop_any(props, "ro.build.version.release",
                                "ro.system.build.version.release",
                                "ro.vendor.build.version.release")
    info["sdk"] = _prop_any(props, "ro.build.version.sdk",
                            "ro.system.build.version.sdk")
    info["build"] = _prop_any(props, "ro.build.display.id", "ro.build.id")
    info["device"] = _prop_any(props, "ro.product.device", "ro.product.vendor.device")
    if not info["serial"]:
        info["serial"] = (adb(["get-serialno"], serial=sn, timeout=5) or "").strip()

    size_out = adb(["shell", "wm", "size"], serial=sn) or ""
    ms = re.findall(r"(\d{3,5})\s*x\s*(\d{3,5})", size_out)
    if ms:
        info["physical_width"], info["physical_height"] = int(ms[0][0]), int(ms[0][1])
        info["width"], info["height"] = int(ms[-1][0]), int(ms[-1][1])

    density_out = adb(["shell", "wm", "density"], serial=sn) or ""
    dm = re.findall(r"density:\s*(\d+)", density_out, re.I)
    if dm:
        info["physical_density"] = int(dm[0])
        info["density"] = int(dm[-1])

    if not info["width"] or not info["height"]:
        for cmd in (["shell", "dumpsys", "display"],
                    ["shell", "dumpsys", "window", "displays"]):
            out = adb(cmd, serial=sn, timeout=10) or ""
            m = re.search(r"(?:cur|init|real|override)[= ]\s*(\d{3,5})x(\d{3,5})",
                          out, re.I)
            if not m:
                m = re.search(r"\b(\d{3,5})\s*x\s*(\d{3,5})\b", out)
            if m:
                info["width"], info["height"] = int(m.group(1)), int(m.group(2))
                if not info["physical_width"]:
                    info["physical_width"], info["physical_height"] = info["width"], info["height"]
                break

    if not info["width"] or not info["height"]:
        png = adb(["exec-out", "screencap", "-p"], binary=True, serial=sn, timeout=10)
        pw, ph = _png_size(png)
        if pw and ph:
            info["width"], info["height"] = pw, ph
            info["physical_width"], info["physical_height"] = pw, ph

    if not info["density"]:
        d = _prop_any(props, "ro.sf.lcd_density", "ro.vendor.display.lcd_density")
        if d.isdigit():
            info["density"] = int(d)
    if not info["density"] and info["physical_density"]:
        info["density"] = info["physical_density"]

    brand = info["brand"]
    model = info["model"]
    if not model or model.lower() in ("android", "unknown", "sdk"):
        model = info["device"]
    if brand and model and brand.lower() not in model.lower():
        info["name"] = "%s %s" % (brand, model)
    else:
        info["name"] = model or brand or info["serial"]
    if not info["physical_width"]:
        info["physical_width"], info["physical_height"] = info["width"], info["height"]
    if not info["physical_density"]:
        info["physical_density"] = info["density"]
    info["connected"] = bool(info["width"] and info["height"])
    if not info["connected"]:
        info["error"] = "设备已连接，但读取不到屏幕尺寸：请解锁手机后重试"
    _device_cache.update(v=info, ts=now)
    return dict(info)


# ------------------------- iOS / WebDriverAgent -------------------------
_ios_device_cache = {"v": None, "ts": 0.0}
_wda_status_cache = {"v": None, "ts": 0.0}
_wda_session = {"id": None, "ts": 0.0}
_wda_window_cache = {"v": None, "ts": 0.0}
_wda_app_cache = {"v": "", "ts": 0.0}


def _wda_request(path, method="GET", payload=None, timeout=4):
    global IOS_LAST_ERROR
    url = IOS_WDA_URL + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", "ignore"))
    except Exception as e:
        IOS_LAST_ERROR = "%s %s: %s" % (method, path, e)
        return None


def _wda_value(raw):
    if isinstance(raw, dict):
        value = raw.get("value")
        if isinstance(value, dict):
            return value
    return {}


def _wda_error(raw):
    value = _wda_value(raw)
    err = value.get("error")
    if err and str(err).lower() not in ("", "success", "none"):
        return str(err)
    return ""


def _wda_status(force=False):
    now = time.time()
    ttl = 2.0 if _wda_status_cache["v"] is not None else 5.0
    if not force and now - _wda_status_cache["ts"] < ttl:
        return _wda_status_cache["v"]
    raw = _wda_request("/status", timeout=3)
    _wda_status_cache.update(v=raw, ts=now)
    return raw


def _ios_core_backend():
    import ios_core
    return ios_core.backend()


def ios_core_ready():
    try:
        return bool(_ios_core_backend().is_ready)
    except Exception:
        return False


def ios_prepare(serial=None):
    """准备 iOS 27+ 的免签 USB 直连；低版本回退 WDA。返回 (ok, error, code)。"""
    global IOS_CORE_LAST_ERROR, IOS_LAST_ERROR, screen_cache
    try:
        core = _ios_core_backend()
        core.ensure_ready(str(serial or _profile_serial() or ""))
        sw, sh = core.screen_size
        if sw > 0 and sh > 0:
            screen_cache = (int(sw), int(sh))
        IOS_CORE_LAST_ERROR = ""
        IOS_LAST_ERROR = ""
        return True, "", ""
    except Exception as e:
        IOS_CORE_LAST_ERROR = str(e)
        return False, str(e), str(getattr(e, "code", "") or "ios_core")


def _ios_wda_available(force=True):
    raw = _wda_status(force=force)
    value = _wda_value(raw)
    state = str(value.get("state") or "").lower()
    return bool(value) and (state in ("", "success", "ready") or bool(value.get("ready")))


def ios_available():
    return ios_core_ready() or _ios_wda_available()


def _wda_session_id(force=False):
    now = time.time()
    if not force and _wda_session["id"] and now - _wda_session["ts"] < 600:
        return _wda_session["id"]
    sid = ""
    raw = _wda_status()
    if isinstance(raw, dict):
        sid = raw.get("sessionId") or _wda_value(raw).get("sessionId") or ""
    if not sid:
        for payload in ({"capabilities": {"alwaysMatch": {}}}, {"desiredCapabilities": {}}):
            r = _wda_request("/session", "POST", payload, timeout=6)
            if isinstance(r, dict):
                sid = r.get("sessionId") or _wda_value(r).get("sessionId") or ""
            if sid:
                break
    _wda_session.update(id=sid or None, ts=now)
    return sid


def _wda_paths(path):
    sid = _wda_session_id()
    out = []
    if sid:
        out.append("/session/%s%s" % (sid, path))
    out.append(path)
    return out


def _wda_try(paths, method="GET", payload=None, timeout=4):
    for path in paths:
        raw = _wda_request(path, method, payload, timeout)
        if raw is not None and not _wda_error(raw):
            return raw
    return None


def _wda_device_raw():
    raw = _wda_try(_wda_paths("/wda/device/info"), timeout=4)
    return _wda_value(raw)


def _wda_window_size(force=False):
    now = time.time()
    if not force and _wda_window_cache["v"] and now - _wda_window_cache["ts"] < 30:
        return _wda_window_cache["v"]
    raw = _wda_try(_wda_paths("/window/size"), timeout=4)
    value = _wda_value(raw)
    try:
        w = int(float(value.get("width") or value.get("screenWidth") or 0))
        h = int(float(value.get("height") or value.get("screenHeight") or 0))
    except Exception:
        w = h = 0
    if w > 0 and h > 0:
        _wda_window_cache.update(v=(w, h), ts=now)
        return w, h
    return _wda_window_cache["v"]


def _wda_screenshot_png(timeout=8):
    raw = _wda_try(_wda_paths("/screenshot"), timeout=timeout)
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    if isinstance(value, dict):
        value = value.get("screenshot") or value.get("data")
    if isinstance(value, str) and value:
        try:
            return base64.b64decode(value)
        except Exception:
            return None
    return None


def _png_size(data):
    if not data or len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    try:
        return (int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"))
    except Exception:
        return 0, 0


_IOS_MODEL_NAMES = {
    "iPhone8,1": "iPhone 6s",
    "iPhone8,2": "iPhone 6s Plus",
    "iPhone8,4": "iPhone SE (1st)",
    "iPhone9,1": "iPhone 7",
    "iPhone9,2": "iPhone 7 Plus",
    "iPhone9,3": "iPhone 7",
    "iPhone9,4": "iPhone 7 Plus",
    "iPhone10,1": "iPhone 8",
    "iPhone10,2": "iPhone 8 Plus",
    "iPhone10,3": "iPhone X",
    "iPhone10,4": "iPhone 8",
    "iPhone10,5": "iPhone 8 Plus",
    "iPhone10,6": "iPhone X",
    "iPhone11,2": "iPhone XS",
    "iPhone11,4": "iPhone XS Max",
    "iPhone11,6": "iPhone XS Max",
    "iPhone11,8": "iPhone XR",
    "iPhone12,1": "iPhone 11",
    "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd)",
    "iPhone13,1": "iPhone 12 mini",
    "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max",
    "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,4": "iPhone 13 mini",
    "iPhone14,5": "iPhone 13",
    "iPhone14,6": "iPhone SE (3rd)",
    "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus",
    "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus",
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus",
    "iPhone17,5": "iPhone 16e",
    "iPhone18,1": "iPhone 17 Pro",
    "iPhone18,2": "iPhone 17 Pro Max",
    "iPhone18,3": "iPhone 17",
    "iPhone18,4": "iPhone Air",
}

# 仅用于“开发者模式尚未开启、CoreDevice 还读不到显示器信息”时先把物理分辨率
# 显示出来。开启开发者模式后会以设备真实回报的 currentMode.size 为准。
_IOS_MODEL_SCREENS = {
    "iPhone12,1": (828, 1792, 2),
    "iPhone12,3": (1125, 2436, 3),
    "iPhone12,5": (1242, 2688, 3),
    "iPhone12,8": (750, 1334, 2),
    "iPhone13,1": (1080, 2340, 3),
    "iPhone13,2": (1170, 2532, 3),
    "iPhone13,3": (1170, 2532, 3),
    "iPhone13,4": (1284, 2778, 3),
    "iPhone14,2": (1170, 2532, 3),
    "iPhone14,3": (1284, 2778, 3),
    "iPhone14,4": (1080, 2340, 3),
    "iPhone14,5": (1170, 2532, 3),
    "iPhone14,6": (750, 1334, 2),
    "iPhone14,7": (1170, 2532, 3),
    "iPhone14,8": (1284, 2778, 3),
    "iPhone15,2": (1179, 2556, 3),
    "iPhone15,3": (1290, 2796, 3),
    "iPhone15,4": (1179, 2556, 3),
    "iPhone15,5": (1290, 2796, 3),
    "iPhone16,1": (1179, 2556, 3),
    "iPhone16,2": (1290, 2796, 3),
    "iPhone17,1": (1206, 2622, 3),
    "iPhone17,2": (1320, 2868, 3),
    "iPhone17,3": (1179, 2556, 3),
    "iPhone17,4": (1290, 2796, 3),
    "iPhone17,5": (1170, 2532, 3),
    "iPhone18,1": (1206, 2622, 3),
    "iPhone18,2": (1320, 2868, 3),
    "iPhone18,3": (1206, 2622, 3),
    "iPhone18,4": (1260, 2736, 3),
}


def _ios_marketing_name(model, name=""):
    raw = str(model or "").strip()
    m = re.search(r"(iPhone\d+,\d+|iPad\d+,\d+|iPod\d+,\d+)", raw)
    if m:
        return _IOS_MODEL_NAMES.get(m.group(1), m.group(1))
    if raw and raw.lower() not in ("iphone", "ipad", "apple", "ios"):
        return raw
    clean_name = str(name or "").strip()
    if clean_name and clean_name.lower() not in ("iphone", "ipad", "apple", "ios"):
        return clean_name
    if raw.lower().startswith("ipad"):
        return "iPad"
    return "iPhone"


def _wda_scalar(path):
    """读取 WDA 只返回字符串的接口，如 /wda/device/name。"""
    raw = _wda_try(_wda_paths(path), timeout=2)
    if not isinstance(raw, dict):
        return ""
    value = raw.get("value")
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, dict):
        for key in ("value", "name", "model", "udid", "version"):
            if value.get(key):
                return str(value[key]).strip()
    return ""


def get_ios_device_info(force=False):
    global screen_cache, IOS_CORE_LAST_ERROR
    now = time.time()
    if not force and _ios_device_cache["v"] and now - _ios_device_cache["ts"] < 4.0:
        return dict(_ios_device_cache["v"])
    info = {
        "connected": False,
        "ready": False,
        "state": "no_device",
        "platform": "ios",
        "driver": "",
        "serial": "",
        "model": "",
        "device_model": "",
        "brand": "Apple",
        "manufacturer": "Apple",
        "name": "",
        "device_name": "",
        "android": "",
        "ios": "",
        "sdk": "",
        "build": "",
        "wda_version": "",
        "width": 0,
        "height": 0,
        "physical_width": 0,
        "physical_height": 0,
        "points_width": 0,
        "points_height": 0,
        "scale": 0,
        "density": 0,
        "wda": IOS_WDA_URL,
        "bundle": IOS_BUNDLE_ID,
        "developer_mode": None,
        "needs_wda": False,
        "error": "",
        "hint": "",
    }

    core_probe = {}
    core = None
    try:
        core = _ios_core_backend()
        core_probe = core.probe(_profile_serial(), force=force)
    except Exception as e:
        IOS_CORE_LAST_ERROR = str(e)

    version = str(core_probe.get("ios") or "")
    try:
        version_parts = [int(part) for part in version.split(".")[:2]]
    except Exception:
        version_parts = []
    major = version_parts[0] if version_parts else 0
    minor = version_parts[1] if len(version_parts) > 1 else 0
    core_ready = bool(core is not None and core.is_ready)
    wda_ready = _ios_wda_available(force=False)
    prefer_wda = bool(0 < major < 27 and wda_ready)
    if core_probe and not prefer_wda and (major >= 17 or core_ready or not wda_ready):
        model = str(core_probe.get("model") or "")
        physical_w, physical_h, scale = _IOS_MODEL_SCREENS.get(model, (0, 0, 0))
        dw, dh = core.display_size
        sw, sh = core.screen_size
        if dw and dh:
            physical_w, physical_h = dw, dh
        if sw and sh:
            physical_w, physical_h = sw, sh
            info["width"], info["height"] = sw, sh
        info["name"] = _ios_marketing_name(model, core_probe.get("name"))
        info["device_name"] = str(core_probe.get("name") or "")
        info["model"] = model
        info["device_model"] = model
        info["serial"] = str(core_probe.get("udid") or "")
        info["ios"] = version
        info["build"] = str(core_probe.get("build") or "")
        info["physical_width"], info["physical_height"] = physical_w, physical_h
        if not info["width"]:
            info["width"], info["height"] = physical_w, physical_h
        if scale:
            info["scale"] = int(scale)
            info["points_width"] = int(round(physical_w / scale)) if physical_w else 0
            info["points_height"] = int(round(physical_h / scale)) if physical_h else 0
            info["density"] = int(round(scale * 160))
        info["developer_mode"] = core_probe.get("developer_mode")
        info["connected"] = True
        info["ready"] = core_ready
        info["state"] = "device" if core_ready else "usb"
        if core_ready:
            info["driver"] = "CoreDevice USB"
        elif 0 < major < 27:
            info["driver"] = "USB · 需 WDA"
            info["needs_wda"] = True
        else:
            info["driver"] = "USB"
        if core_ready and info["width"] and info["height"]:
            screen_cache = (int(info["width"]), int(info["height"]))
        if core_probe.get("developer_mode") is False:
            info["error"] = "iPhone 已连接；请开启「开发者模式」后重启手机"
            info["hint"] = ("设置 → 隐私与安全性 → 开发者模式。开启并重启后，"
                            "无需安装证书即可直接控制 iPhone。")
        elif major and major < 17:
            info["error"] = "iOS 16 及以下未纳入当前兼容范围"
            info["hint"] = "当前版本支持 iOS 17.0 及以上，请先升级 iPhone 系统。"
        elif 0 < major < 27 and not core_ready:
            info["error"] = f"iOS {version} 不支持免签自动点击"
            info["hint"] = (
                "Apple 从 iOS 27.0 才开放免签触控通道。"
                "请点开控制台右侧「旧版 iPhone 兼容通道」，"
                "按向导自动安装并启动 WDA。"
            )
        elif not core_ready:
            info["hint"] = "USB 已连接。点「开始」时会自动准备免签直连，首次可能需要几分钟。"
        _ios_device_cache.update(v=info, ts=now)
        return dict(info)

    raw = _wda_status(force=force)
    if not raw:
        if core_probe:
            info["connected"] = True
            info["state"] = "usb"
            info["driver"] = "USB"
            info["serial"] = str(core_probe.get("udid") or "")
            info["model"] = str(core_probe.get("model") or "")
            info["ios"] = version
            info["build"] = str(core_probe.get("build") or "")
            info["name"] = _ios_marketing_name(info["model"], core_probe.get("name"))
            info["device_name"] = str(core_probe.get("name") or "")
            info["error"] = "当前 iPhone 系统版本需要使用旧版 WDA 兼容模式"
            info["hint"] = "点开右侧「旧版 iPhone 兼容通道」，按向导一键配置 WDA。"
        else:
            info["error"] = ("未检测到 iPhone。请用 USB 数据线连接，解锁手机并点「信任此电脑」。"
                             + ((" " + IOS_CORE_LAST_ERROR) if IOS_CORE_LAST_ERROR else ""))
        _ios_device_cache.update(v=info, ts=now)
        return dict(info)

    value = _wda_value(raw)
    dev = _wda_device_raw()
    os_info = dev.get("os") if isinstance(dev.get("os"), dict) else {}
    status_os = value.get("os") if isinstance(value.get("os"), dict) else {}
    build_info = value.get("build") if isinstance(value.get("build"), dict) else {}
    win_w, win_h = _wda_window_size(force=force)
    png_w, png_h = _png_size(_wda_screenshot_png())

    name = dev.get("name") or value.get("deviceName") or value.get("device") or ""
    model = dev.get("model") or value.get("model") or value.get("deviceModel") or ""
    if not model or model.lower() in ("iphone", "ipad"):
        model = _wda_scalar("/wda/device/model") or model or value.get("device") or ""
    if not name or name.lower() in ("iphone", "ipad", "apple"):
        name = _wda_scalar("/wda/device/name") or name or value.get("device") or "iPhone"
    version = (os_info.get("version") or status_os.get("version")
               or value.get("productVersion") or dev.get("version")
               or _wda_scalar("/wda/device/version") or "")
    udid = (value.get("udid") or dev.get("udid") or value.get("deviceUDID")
            or _wda_scalar("/wda/device/udid") or "")

    scale = 0.0
    if png_w and win_w:
        scale = png_w / float(win_w)
    elif png_w and not win_w:
        screen_raw = _wda_try(_wda_paths("/wda/screen"), timeout=3)
        screen_value = _wda_value(screen_raw)
        try:
            scale = float(screen_value.get("scale") or 0)
        except Exception:
            scale = 0.0
        if scale > 0:
            win_w, win_h = int(round(png_w / scale)), int(round(png_h / scale))

    info["name"] = _ios_marketing_name(model, name)
    info["model"] = model
    info["device_model"] = model
    info["ios"] = version
    info["serial"] = udid
    info["wda_version"] = str(build_info.get("version") or value.get("version") or "")
    info["build"] = str(build_info.get("productVersion") or build_info.get("id") or "")
    info["physical_width"], info["physical_height"] = png_w, png_h
    info["points_width"], info["points_height"] = win_w, win_h
    info["width"], info["height"] = (png_w, png_h) if png_w and png_h else (win_w, win_h)
    if not info["physical_width"]:
        info["physical_width"], info["physical_height"] = info["width"], info["height"]
    if scale > 0:
        info["scale"] = round(scale, 2)
        info["density"] = int(round(scale * 160))
    info["connected"] = bool(win_w and win_h)
    info["ready"] = info["connected"]
    info["state"] = "device" if info["connected"] else "wda"
    info["driver"] = "WebDriverAgent"
    if not info["connected"]:
        info["error"] = "WDA 已连接，但读不到窗口尺寸（请确认 /window/size 可用）"
    _ios_device_cache.update(v=info, ts=now)
    return dict(info)


def get_device_info(serial=None, force=False):
    if PLATFORM == "ios":
        return get_ios_device_info(force=force)
    return _get_android_device_info(serial=serial, force=force)


def adb(args, binary=False, timeout=20, serial=None):
    """执行 adb 命令，失败/超时返回 None"""
    try:
        cmd = [adb_path()]
        sn = "" if (args and args[0] == "devices") else (serial or _profile_serial() or _any_serial())
        if sn:
            cmd += ["-s", sn]
        r = subprocess.run(cmd + args, capture_output=True, timeout=timeout, **_NOWIN)
        if r.returncode != 0:
            return None
        return r.stdout if binary else r.stdout.decode("utf-8", "ignore")
    except Exception:
        return None


def screen_size():
    """返回逻辑分辨率 (宽, 高)。
    注意：wm size 可能同时有 Physical 和 Override，screencap / input tap 用的是
    Override（逻辑分辨率），所以取最后一个匹配值。"""
    global screen_cache
    if screen_cache:
        return screen_cache
    if PLATFORM == "ios":
        try:
            sw, sh = _ios_core_backend().screen_size
            if sw > 0 and sh > 0:
                screen_cache = (int(sw), int(sh))
                return screen_cache
        except Exception:
            pass
        img = ios_screenshot()
        if img is not None:
            return screen_cache
        wh = _wda_window_size(force=True)
        if wh:
            screen_cache = wh
        else:
            screen_cache = (int(PROFILE.get("width") or 0) or 1170,
                            int(PROFILE.get("height") or 0) or 2532)
        return screen_cache
    out = adb(["shell", "wm", "size"]) or ""
    ms = re.findall(r"(\d+)x(\d+)", out)
    if ms:
        screen_cache = (int(ms[-1][0]), int(ms[-1][1]))
    else:
        try:
            _pw = int(PROFILE.get("width") or 0)
            _ph = int(PROFILE.get("height") or 0)
        except Exception:
            _pw, _ph = 0, 0
        screen_cache = (_pw or 1080, _ph or 2400)
    return screen_cache


def set_screen_size(w, h):
    global screen_cache
    if w and h:
        screen_cache = (int(w), int(h))


def ios_screenshot():
    png = None
    try:
        core = _ios_core_backend()
        if core.is_ready:
            png = core.screenshot_png()
    except Exception:
        png = None
    if not png:
        png = _wda_screenshot_png()
    if not png:
        return None
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None
    if img is not None:
        h, w = img.shape[:2]
        set_screen_size(w, h)
    return img


def _ios_point_scale():
    win = _wda_window_size()
    img = screen_cache
    if not win or not img:
        return 1.0, 1.0
    return (img[0] / float(win[0]), img[1] / float(win[1]))


def ios_tap(x, y):
    try:
        core = _ios_core_backend()
        if core.is_ready:
            return core.tap(x, y)
    except Exception:
        pass
    sx, sy = _ios_point_scale()
    payload = {"x": int(round(x / sx)), "y": int(round(y / sy))}
    raw = _wda_try(_wda_paths("/wda/tap/0"), "POST", payload, timeout=5)
    if raw is None:
        raw = _wda_try(_wda_paths("/wda/tap"), "POST", payload, timeout=5)
    return raw is not None


def ios_swipe(x1, y1, x2, y2, duration_ms=300):
    try:
        core = _ios_core_backend()
        if core.is_ready:
            return core.swipe(x1, y1, x2, y2, duration_ms)
    except Exception:
        pass
    sx, sy = _ios_point_scale()
    payload = {
        "fromX": int(round(x1 / sx)), "fromY": int(round(y1 / sy)),
        "toX": int(round(x2 / sx)), "toY": int(round(y2 / sy)),
        "duration": max(0.05, float(duration_ms) / 1000.0),
    }
    raw = _wda_try(_wda_paths("/wda/dragfromtoforduration"), "POST", payload, timeout=8)
    return raw is not None


def ios_back():
    w, h = screen_size()
    return ios_swipe(2, h // 2, int(w * 0.65), h // 2, 260)


def ios_current_app():
    try:
        core = _ios_core_backend()
        if core.is_ready:
            return core.current_bundle()
    except Exception:
        pass
    now = time.time()
    if _wda_app_cache["v"] and now - _wda_app_cache["ts"] < 1.5:
        return _wda_app_cache["v"]
    raw = _wda_try(_wda_paths("/wda/activeAppInfo"), timeout=2)
    value = _wda_value(raw)
    bundle = value.get("bundleId") or value.get("bundleID") or value.get("name") or ""
    _wda_app_cache.update(v=bundle, ts=now)
    return bundle


def ios_terminate_app(bundle=None):
    bundle = bundle or IOS_BUNDLE_ID
    try:
        core = _ios_core_backend()
        if core.is_ready:
            _wda_app_cache["ts"] = 0.0
            return core.terminate_app(bundle)
    except Exception:
        pass
    raw = _wda_try(_wda_paths("/wda/apps/terminate"), "POST",
                   {"bundleId": bundle}, timeout=6)
    _wda_app_cache["ts"] = 0.0
    return raw is not None


def ios_launch_app(bundle=None):
    bundle = bundle or IOS_BUNDLE_ID
    try:
        core = _ios_core_backend()
        if core.is_ready:
            _wda_app_cache["ts"] = 0.0
            return core.launch_app(bundle)
    except Exception:
        pass
    payload = {"bundleId": bundle, "arguments": [], "environment": {}}
    raw = _wda_try(_wda_paths("/wda/apps/launch"), "POST", payload, timeout=12)
    _wda_app_cache["ts"] = 0.0
    return raw is not None


def ios_app_state(bundle=None):
    bundle = bundle or IOS_BUNDLE_ID
    try:
        core = _ios_core_backend()
        if core.is_ready:
            return 4 if core.app_running(bundle) else 1
    except Exception:
        pass
    raw = _wda_try(_wda_paths("/wda/apps/state"), "POST",
                   {"bundleId": bundle}, timeout=4)
    value = _wda_value(raw)
    try:
        return int(value.get("state"))
    except Exception:
        return None


def tap_device(x, y):
    if PLATFORM == "ios":
        return ios_tap(x, y)
    adb(["shell", "input", "tap", str(x), str(y)])
    return True


def back_device():
    if PLATFORM == "ios":
        return ios_back()
    adb(["shell", "input", "keyevent", "4"])
    return True


def swipe_device(x1, y1, x2, y2, duration_ms=300):
    if PLATFORM == "ios":
        return ios_swipe(x1, y1, x2, y2, duration_ms)
    adb(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)])
    return True


# ------------------------- 文字匹配 -------------------------
def normalize_text(s):
    if not s:
        return ""
    s = s.strip().replace(" ", "").replace("\u3000", "")
    s = re.sub(r"[×xX✕✖·•\|丨\[\]【】()（）\d:：，,。.]+$", "", s)
    s = re.sub(r"^[×xX✕✖·•\|丨]+", "", s)
    s = s.replace("领収", "领取").replace("领职", "领取").replace("领敢", "领取")
    s = s.replace("奨", "奖").replace("奬", "奖").replace("勵", "励")
    return s


def text_match(recognized, target):
    a = normalize_text(recognized)
    b = normalize_text(target)
    if not a:
        return False
    if a == b or b in a:
        return True
    # 识别结果比目标短时，只允许 3 字以上的子串（避免“广告”误配“关闭广告”）
    if len(a) >= 3 and a in b:
        return True
    if not FUZZY:
        return False
    hit = sum(1 for ch in b if ch in a)
    return hit >= max(2, len(b) - 1)


def in_roi(cx, cy, sw, sh, target):
    roi = TARGET_ROI.get(target, {"x": (0.0, 1.0), "y": (0.0, 1.0)})
    (x0, x1), (y0, y1) = roi["x"], roi["y"]
    return x0 * sw <= cx <= x1 * sw and y0 * sh <= cy <= y1 * sh


# ------------------------- 通道一：OCR（主） -------------------------
def ocr_available():
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401
        return True
    except Exception:
        return False


def get_ocr():
    global ocr_engine
    if ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        ocr_engine = RapidOCR(det_limit_side_len=512, det_limit_type="max")
    return ocr_engine


def screenshot():
    if PLATFORM == "ios":
        return ios_screenshot()
    import cv2
    import numpy as np
    data = adb(["exec-out", "screencap", "-p"], binary=True, timeout=10)
    if not data:
        return None
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is not None:
        h, w = img.shape[:2]
        set_screen_size(w, h)  # 以真实截图为准，避免 wm size 分辨率不一致
    return img


FREE_DAYS_RE = re.compile(r"免费听\s*(\d{1,3})\s*天")
REWARD_DAYS_RE = re.compile(r"第\s*(\d{1,3})\s*[日天]")


def sync_days(max_back=3):
    """尽力读真实免费听天数：当前页读不到就往回退到首页读，读完再点「免费听」回到广告。"""
    n = read_free_days()
    if n is not None:
        return n
    for _ in range(max_back):
        back_device()
        time.sleep(0.9)
        FOCUS_CACHE["ts"] = 0
        n = read_free_days()
        if n is not None:
            return n
    return None


def _parse_free_days_nodes(nodes):
    """只认首页「免费听N天」，不要把奖励弹窗里的「第N天」当成总天数。"""
    for row in nodes or []:
        raw = str(row[0] or "")
        compact = raw.replace(" ", "").replace("\u3000", "").replace("\n", "")
        m = FREE_DAYS_RE.search(compact)
        if not m:
            continue
        try:
            value = int(m.group(1))
        except Exception:
            continue
        if 0 <= value <= 999:
            return value
    return None


def parse_free_days(nodes):
    """从 ocr_nodes 的结果里解析真实免费听天数，解析不到返回 None。"""
    return _parse_free_days_nodes(nodes)


def parse_reward_days(nodes):
    """从「恭喜获得 · 第N日畅听」弹窗读取当前累计天数。"""
    for row in nodes or []:
        raw = str(row[0] or "")
        compact = raw.replace(" ", "").replace("\u3000", "").replace("\n", "")
        if "畅听" not in compact and "免费听" not in compact:
            continue
        m = REWARD_DAYS_RE.search(compact)
        if not m:
            continue
        try:
            value = int(m.group(1))
        except Exception:
            continue
        if 0 <= value <= 999:
            return value
    return None


def read_free_days(fast=False, screen=None):
    """读首页「免费听N天」；fast=True 时只识别右下角区域。"""
    if screen is None:
        screen = screenshot()
    if screen is None:
        return None
    try:
        n = _parse_free_days_nodes(_ocr_region(screen, FREE_LISTEN_ROI))
        if n is not None or fast:
            return n
        return _parse_free_days_nodes(ocr_nodes(screen))
    except Exception:
        return None


def ocr_nodes(screen):
    """返回 [(label, cx, cy), ...]，坐标为原图像素"""
    import cv2
    import numpy as np
    h, w = screen.shape[:2]
    scale = 1.0
    img = screen
    if w > OCR_MAX_WIDTH:
        scale = OCR_MAX_WIDTH / float(w)
        img = cv2.resize(screen, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    result, _ = get_ocr()(img, use_cls=False)
    if not result:
        return []
    inv = 1.0 / scale
    items = []
    for row in result:
        if not row or len(row) < 3:
            continue
        box, text, score = row[0], row[1], float(row[2])
        if score < OCR_SCORE:
            continue
        pts = np.array(box, dtype=np.float32)
        cx = float(pts[:, 0].mean()) * inv
        cy = float(pts[:, 1].mean()) * inv
        x1 = int(round(float(pts[:, 0].min()) * inv))
        x2 = int(round(float(pts[:, 0].max()) * inv))
        y1 = int(round(float(pts[:, 1].min()) * inv))
        y2 = int(round(float(pts[:, 1].max()) * inv))
        items.append((str(text), int(round(cx)), int(round(cy)), x1, y1, x2, y2))
    return items


def _ocr_region(screen, roi):
    """只在指定相对区域内做 OCR，并把坐标映射回原图。"""
    if screen is None:
        return []
    h, w = screen.shape[:2]
    x1 = max(0, min(w - 1, int(w * roi[0])))
    y1 = max(0, min(h - 1, int(h * roi[1])))
    x2 = max(x1 + 1, min(w, int(w * roi[2])))
    y2 = max(y1 + 1, min(h, int(h * roi[3])))
    crop = screen[y1:y2, x1:x2]
    if crop.size == 0:
        return []
    out = []
    for row in ocr_nodes(crop):
        out.append((row[0], row[1] + x1, row[2] + y1,
                    row[3] + x1, row[4] + y1, row[5] + x1, row[6] + y1))
    return out


# ------------------------- 通道二：uiautomator（可选） -------------------------
def dump_nodes(timeout=20):
    """返回 [(label, cx, cy), ...]；失败返回 None"""
    if PLATFORM == "ios":
        return None
    adb(["shell", "rm", "-f", DUMP_PATH], timeout=timeout)
    adb(["shell", "uiautomator", "dump", "--compressed", DUMP_PATH], timeout=timeout)
    xml = adb(["shell", "cat", DUMP_PATH], timeout=timeout)
    if not xml or "<hierarchy" not in xml:
        return None
    xml = xml[xml.find("<hierarchy"):]
    end = xml.rfind(">")
    if end > 0:
        xml = xml[: end + 1]
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None

    nodes = []
    for n in root.iter("node"):
        label = (n.get("text") or "").strip() or (n.get("content-desc") or "").strip()
        if not label:
            continue
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.get("bounds") or "")
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        nodes.append((label, (x1 + x2) // 2, (y1 + y2) // 2, x1, y1, x2, y2))
    return nodes


def uia_available():
    """启动探测一次；失败则本次会话关闭。"""
    global uia_ok
    if uia_ok is not None:
        return uia_ok
    if PLATFORM == "ios":
        uia_ok = False
        return False
    if USE_UIAUTOMATOR is False:
        uia_ok = False
    elif USE_UIAUTOMATOR is True:
        uia_ok = True
    else:
        print("· 探测 uiautomator …")
        t = time.time()
        uia_ok = dump_nodes(timeout=UIA_TIMEOUT) is not None
        print(f"  {'✅ 可用' if uia_ok else '❌ 不可用（等不到 idle），改用 OCR'}"
              f"（{time.time() - t:.1f}s）")
    return uia_ok


# ------------------------- 点击 -------------------------
def save_debug(screen, x, y, target, label):
    if not SAVE_CLICK_DEBUG or screen is None:
        return
    try:
        import cv2
        os.makedirs(DEBUG_DIR, exist_ok=True)
        vis = screen.copy()
        cv2.drawMarker(vis, (x, y), (0, 0, 255), cv2.MARKER_CROSS, 60, 3)
        cv2.circle(vis, (x, y), 28, (0, 0, 255), 3)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", target)
        cv2.imwrite(os.path.join(DEBUG_DIR, f"{ts}_{click_seq:04d}_{safe}_{x}_{y}.png"), vis)
    except Exception:
        pass


def do_click(x, y, target, label, source, screen=None):
    """点击并抑制 3 秒内同一坐标的重复点击"""
    global click_seq
    now = time.time()
    if (target == last_click["target"]
            and abs(x - last_click["x"]) < 20
            and abs(y - last_click["y"]) < 20
            and now - last_click["ts"] < 3.0):
        return False

    click_seq += 1
    last_click.update(target=target, x=x, y=y, ts=now)
    print(f"  >>> 点击 #{click_seq} [{datetime.now():%H:%M:%S}] "
          f"{target} <- 「{label}」 @({x},{y}) via {source}")
    tap_device(x, y)
    save_debug(screen, x, y, target, label)
    return True


# 「xx秒后 / xx s 后进入直播间 [取消]」：数字 + 秒/s/S + 后，且同段含「进入直播间」
LIVE_ENTER_RE = re.compile(r"\d+\s*[秒sSｓＳ]\s*后")


def try_cancel_live_enter(nodes, source):
    """检测到「4s后进入直播间」这类倒计时时，点「取消」拦下来，避免被自动带进直播间。"""
    if not nodes:
        return False

    def is_countdown(lbl):
        return bool(LIVE_ENTER_RE.search(lbl)) and ("进入直播间" in lbl)

    if not any(is_countdown(n[0]) for n in nodes):
        return False

    # 优先找独立的「取消」节点
    for n in nodes:
        if normalize_text(n[0]) == "取消":
            print(f"  >>> 拦截「进入直播间」，点取消 <- 「{n[0]}」 @({n[1]},{n[2]})")
            do_click(n[1], n[2], "取消", n[0], source)
            return True

    # 兜底：OCR 把「倒计时 + 取消」并成一段时，点该段右侧（取消在右边）
    for n in nodes:
        lbl = normalize_text(n[0])
        if "取消" in lbl and len(lbl) <= 12:
            x1, x2 = n[3], n[5]
            tx = int(x2 - (x2 - x1) * 0.12)
            print(f"  >>> 拦截「进入直播间」，点取消(合并段右侧) <- 「{n[0]}」 @({tx},{n[2]})")
            do_click(tx, n[2], "取消", n[0], source)
            return True

    # 兜底：实测 OCR 会把「取消」两个字整段吞掉（只剩「4s后进入直播间」），
    # 这时点不到按钮，用返回键把倒计时关掉，否则 4 秒后就被拖进直播间。
    global last_back_ts
    if time.time() - last_back_ts > 2.0:
        last_back_ts = time.time()
        print("  >>> 拦截「进入直播间」：识别不到「取消」，改用返回键")
        back_device()
        return True

    return False


def match_and_click(nodes, source, screen=None, targets=None):
    """在节点里找目标并点击。命中返回目标字符串，否则 None"""
    if not nodes:
        return None
    # 「xx秒后进入直播间 [取消]」优先拦下，别被带进直播间
    if try_cancel_live_enter(nodes, source):
        return "取消"
    sw, sh = screen_size()
    # 领奖优先，其次“领取成功 / 退出不看了”（关广告），最后才跳过
    if targets is None:
        targets = REWARD_TARGETS + CLOSE_TARGETS + (SKIP_TARGETS if ENABLE_SKIP else [])

    for target in targets:
        cands = []
        for node in nodes:
            label, cx, cy = node[0], node[1], node[2]
            if not text_match(label, target):
                continue
            if not in_roi(cx, cy, sw, sh, target):
                continue
            cands.append(node)
        if not cands:
            continue

        if target in CENTER_TARGETS:
            midx, midy = sw * 0.5, sh * 0.5
            cands.sort(key=lambda c: (c[1] - midx) ** 2 + (c[2] - midy) ** 2)
        else:
            cands.sort(key=lambda c: (-c[1], c[2]))  # 偏右上

        label, cx, cy = cands[0][0], cands[0][1], cands[0][2]
        # 「领取成功 ×」是同一个胶囊按钮，整个都能点，直接点中心最稳
        do_click(cx, cy, target, label, source, screen)
        return target

    return None


def _merge_ocr_nodes(*groups):
    merged = []
    seen = set()
    for group in groups:
        for node in group or []:
            label, cx, cy = node[0], int(node[1]), int(node[2])
            key = (label, round(cx / 24), round(cy / 24))
            if key in seen:
                continue
            seen.add(key)
            merged.append(node)
    return merged


def fast_scan_and_click(screen, source="ocr", idle=0):
    """只扫描按钮附近区域并尝试点击，返回 (命中目标, 本轮OCR节点)。"""
    global LAST_FREE_DAYS
    if screen is None:
        return None, []

    # 极速模式：先取得顶部与中央两块结果，再按“领奖 > 关闭 > 跳过”决策。
    # 广告播放期间不扫描免费听/底部区域，每轮 OCR 从 5 次降到 2 次。
    top_nodes = _ocr_region(screen, FAST_TOP_ROI)
    reward_nodes = _ocr_region(screen, FAST_REWARD_ROI)

    # 奖励弹窗的「领取奖励」优先级高于广告结束页右上角的「领取成功 ×」。
    # 后者只是一条关闭胶囊，先点它会反复关掉顶部提示，中央弹窗永远不动。
    hit = match_and_click(reward_nodes, source, screen, targets=REWARD_TARGETS)
    if hit:
        return hit, _merge_ocr_nodes(top_nodes, reward_nodes)

    hit = match_and_click(top_nodes, source, screen, targets=CLOSE_TARGETS)
    if hit:
        return hit, _merge_ocr_nodes(top_nodes, reward_nodes)

    if ENABLE_SKIP:
        hit = match_and_click(top_nodes, source, screen, targets=SKIP_TARGETS)
        if hit:
            return hit, _merge_ocr_nodes(top_nodes, reward_nodes)

    # 极速模式下按间隔补扫边缘按钮；非极速模式保持原来的完整扫描。
    scan_edges = (not AD_FAST_MODE) or (
        idle >= AD_FAST_FALLBACK_EVERY
        and idle % AD_FAST_FALLBACK_EVERY == 0
    )
    if not scan_edges:
        return None, _merge_ocr_nodes(top_nodes, reward_nodes)

    free_nodes = _ocr_region(screen, FREE_LISTEN_ROI)
    days = _parse_free_days_nodes(free_nodes)
    if days is not None:
        LAST_FREE_DAYS = days
    for node in free_nodes:
        if "免费听" in node[0]:
            do_click(node[1], node[2], "免费听", node[0], source, screen)
            return "免费听", _merge_ocr_nodes(top_nodes, free_nodes)

    combined = _merge_ocr_nodes(top_nodes, reward_nodes)
    hit = match_and_click(combined, source, screen)
    if hit:
        return hit, _merge_ocr_nodes(combined, free_nodes)

    bottom_nodes = _ocr_region(screen, FAST_BOTTOM_ROI)
    combined = _merge_ocr_nodes(combined, free_nodes, bottom_nodes)
    hit = match_and_click(combined, source, screen)
    return hit, combined


# ------------------------- 静音 -------------------------
def get_music_volume():
    """返回媒体音量索引；失败 None"""
    if PLATFORM == "ios":
        return None
    out = adb(["shell", "cmd", "media_session", "volume", "--stream", "3", "--get"]) or ""
    m = re.search(r"volume is (\d+)", out)
    return int(m.group(1)) if m else None


AUDIO_STATE = os.path.join(SCRIPT_DIR, "audio_state.json")
last_nonzero_volume = 8


def _load_audio_state():
    global last_nonzero_volume
    try:
        import json
        with open(AUDIO_STATE, encoding="utf-8") as f:
            v = int(json.load(f).get("last_nonzero", 8))
        if 0 < v <= 15:
            last_nonzero_volume = v
    except Exception:
        pass


def _save_audio_state():
    try:
        import json
        with open(AUDIO_STATE, "w", encoding="utf-8") as f:
            json.dump({"last_nonzero": last_nonzero_volume}, f)
    except Exception:
        pass


_load_audio_state()


def toggle_mute():
    # KEYCODE_VOLUME_MUTE：切换系统的静音开关（不是把索引设成 0）
    if PLATFORM == "ios":
        return False
    adb(["shell", "input", "keyevent", "164"])
    return True


def set_music_volume(idx):
    """直接把媒体音量索引设成 idx，并回读确认。成功返回 True。"""
    if PLATFORM == "ios":
        return False
    try:
        adb(["shell", "cmd", "media_session", "volume", "--stream", "3", "--set", str(int(idx))])
    except Exception:
        return False
    time.sleep(0.15)
    return get_music_volume() == int(idx)


def mute_on_start():
    global muted, saved_volume, last_nonzero_volume
    if not MUTE_ON_START:
        return
    if PLATFORM == "ios":
        print("ℹ️  iOS 暂不支持自动静音，请手动把手机音量调低")
        return
    v = get_music_volume()
    if v is None:
        print("⚠️  读不到媒体音量，仍强制设为 0")
        set_music_volume(0)
        muted = True
        return
    if v > 0:
        last_nonzero_volume = v
        _save_audio_state()
        saved_volume = v
    else:
        saved_volume = 0            # 启动时本来就静音：停止后恢复到上次非零音量
    if set_music_volume(0):
        muted = True
        print(f"🔇 已静音（原音量 {saved_volume or last_nonzero_volume}，停止后恢复）")
        return
    toggle_mute()
    time.sleep(0.3)
    muted = get_music_volume() == 0
    print("🔇 已静音（keyevent 164）" if muted else "⚠️  静音未生效，继续运行")


def unmute():
    global muted, saved_volume
    if PLATFORM == "ios":
        return
    cur = get_music_volume()
    target = saved_volume if (saved_volume or 0) > 0 else last_nonzero_volume
    if cur is None:
        pass
    elif cur == target:
        print(f"🔊 音量无需调整（{cur}）")
    elif set_music_volume(target):
        print(f"🔊 已恢复音量 {target}（原 {cur}）")
    else:
        toggle_mute()
        print("🔊 已恢复音量（keyevent 164）")
    muted = False
    saved_volume = None


# ------------------------- 直播间自救 -------------------------
# 被带进直播间后，直播间里没有任何可领奖按钮，脚本会一直空转（实测挂了 23 分钟）。
FOCUS_CACHE = {"ts": 0.0, "v": ""}
IOS_LIVE_CACHE = {"ts": 0.0, "v": False}
last_back_ts = 0.0


def current_focus():
    """当前前台 Window/Activity，缓存 1.5s"""
    now = time.time()
    if now - FOCUS_CACHE["ts"] < 1.5:
        return FOCUS_CACHE["v"]
    if PLATFORM == "ios":
        v = ios_current_app()
    else:
        out = adb(["shell", "dumpsys", "window"], timeout=10) or ""
        m = re.search(r"mCurrentFocus=Window\{[^}]*?([\w.]+/[\w.$]+)", out)
        v = m.group(1) if m else ""
    FOCUS_CACHE.update(ts=now, v=v)
    return v


LIVE_HINTS = ("更多直播", "说点什么", "在线观众", "直播间人数", "点亮", "送礼物", "直播中")


def in_live_room():
    """前台 Activity 判断直播间。Honor 上是 LunaDefaultLivePlayerActivity，
    其它机型可能是 LiveRoomActivity / 插件里的 live 页面，统一按关键字匹配。"""
    if PLATFORM == "ios":
        now = time.time()
        if now - IOS_LIVE_CACHE["ts"] < 3.0:
            return IOS_LIVE_CACHE["v"]
        hit = _live_text_visible()
        IOS_LIVE_CACHE.update(ts=now, v=hit)
        return hit
    f = (current_focus() or "").lower()
    if not f:
        return False
    if "liveplayer" in f or "liveroom" in f or "livelist" in f:
        return True
    if ".live." in f and "activity" in f:
        return True
    return False


def _live_text_visible():
    """焦点读不到时用画面文字兜底：同时出现两个直播间专属文案就算在直播间"""
    scr = screenshot()
    if scr is None:
        return False
    hit = 0
    for n in ocr_nodes(scr):
        t = n[0]
        if any(h in t for h in LIVE_HINTS):
            hit += 1
            if hit >= 2:
                return True
    return False


def _back():
    back_device()
    FOCUS_CACHE["ts"] = 0.0
    time.sleep(0.8)


def _tap_live_close():
    """直播间右上角一般有 × / 关闭，先点它"""
    scr = screenshot()
    if scr is None:
        return False
    _w, h = screen_size()
    for n in ocr_nodes(scr):
        t = n[0].replace(" ", "")
        if t in ("关闭", "×", "✕", "退出", "退出直播间") and n[2] < h * 0.22:
            do_click(n[1], n[2], "直播间关闭", n[0], "ocr", scr)
            time.sleep(1.0)
            return True
    return False


def _wait_ad_transition(timeout=3.5):
    """点完入口后轮询 Activity，离开首页就提前返回，省掉固定等待。"""
    end = time.time() + max(0.4, timeout)
    while time.time() < end:
        FOCUS_CACHE["ts"] = 0.0
        focus = current_focus()
        if focus and "MainActivity" not in focus:
            time.sleep(0.35)
            return True
        time.sleep(0.18)
    return False


def _try_free_listen(screen):
    """优先只识别右下角「免费听」，命中就点进去并记录真实天数。"""
    global LAST_FREE_DAYS
    if screen is None:
        return False
    nodes = _ocr_region(screen, FREE_LISTEN_ROI)
    days = _parse_free_days_nodes(nodes)
    if days is not None:
        LAST_FREE_DAYS = days
    for n in nodes:
        if "免费听" in n[0]:
            do_click(n[1], n[2], "免费听", n[0], "ocr", screen)
            _wait_ad_transition()
            return True
    return False


def _tap_free_listen():
    """在首页找「免费听N天」按钮点进去，重新进入广告流程；
    有「再看1个」奖励弹窗时优先点它（模态弹窗会挡住底下的按钮）。"""
    global LAST_FREE_DAYS
    LAST_FREE_DAYS = None
    for _ in range(6):
        scr = screenshot()
        if scr is None:
            time.sleep(0.8)
            continue
        if _try_free_listen(scr):
            return True
        nodes = ocr_nodes(scr)
        days = _parse_free_days_nodes(nodes)
        if days is not None:
            LAST_FREE_DAYS = days
        for n in nodes:
            if "再看" in n[0]:
                do_click(n[1], n[2], "再看", n[0], "ocr", scr)
                _wait_ad_transition(timeout=3.0)
                return True
        for n in nodes:
            if "免费听" in n[0]:
                do_click(n[1], n[2], "免费听", n[0], "ocr", scr)
                _wait_ad_transition()
                return True
        time.sleep(1.2)
    return False


def enter_next_ad():
    """回到首页时接上下一条广告。
    首页常挂着奖励弹窗（「恭喜获得第N天免费时长畅听」），弹窗是模态的，
    直接点底下的「免费听」没用（实测会一直空转），必须按优先级：
      1) 弹窗上的「再看1个」= 继续看下一条广告（不花钱，最安全）
      2) 「免费听N天」= 从推荐页入口进
      3) 都没有就按返回键把弹窗关掉再试
    """
    global LAST_FREE_DAYS
    LAST_FREE_DAYS = None
    for attempt in range(4):
        scr = screenshot()
        if scr is None:
            time.sleep(0.8)
            continue
        if _try_free_listen(scr):
            return True
        nodes = ocr_nodes(scr)
        days = _parse_free_days_nodes(nodes)
        if days is not None:
            LAST_FREE_DAYS = days
        for n in nodes:
            if "再看" in n[0]:
                do_click(n[1], n[2], "再看", n[0], "ocr", scr)
                _wait_ad_transition(timeout=3.0)
                return True
        for n in nodes:
            if "免费听" in n[0]:
                do_click(n[1], n[2], "免费听", n[0], "ocr", scr)
                _wait_ad_transition()
                return True
        back_device()
        time.sleep(0.9)
    return False


def restart_app():
    """强制重启 App 并回到广告流程。
    关键：force-stop 后要确认进程真的没了，否则 monkey 只是把原来的
    直播间任务重新拉到前台，看起来就像「重启了还在直播间」。"""
    if PLATFORM == "ios":
        ios_terminate_app()
        time.sleep(1.6)
        ios_launch_app()
        FOCUS_CACHE["ts"] = 0.0
        IOS_LIVE_CACHE["ts"] = 0.0
        time.sleep(7.0)
        if in_live_room():
            ios_terminate_app()
            time.sleep(1.6)
            ios_launch_app()
            FOCUS_CACHE["ts"] = 0.0
            IOS_LIVE_CACHE["ts"] = 0.0
            time.sleep(7.0)
        return _tap_free_listen()
    for _ in range(2):
        adb(["shell", "am", "force-stop", "com.luna.music"])
        time.sleep(1.6)
        pid = (adb(["shell", "pidof", "com.luna.music"]) or "").strip()
        if not pid:
            break
        adb(["shell", "am", "force-stop", "--user", "0", "com.luna.music"])
        time.sleep(1.6)

    adb(["shell", "monkey", "-p", "com.luna.music", "-c",
         "android.intent.category.LAUNCHER", "1"])
    FOCUS_CACHE["ts"] = 0.0
    time.sleep(7.0)

    # 有些广告会在启动后 4 秒自动跳直播间，再杀一次
    if in_live_room():
        adb(["shell", "am", "force-stop", "com.luna.music"])
        time.sleep(1.6)
        adb(["shell", "monkey", "-p", "com.luna.music", "-c",
             "android.intent.category.LAUNCHER", "1"])
        FOCUS_CACHE["ts"] = 0.0
        time.sleep(7.0)

    return _tap_free_listen()


def escape_live_room():
    """退出直播间，四层兜底：
    ① 返回键 ×3 ② 右上角「关闭 / ×」 ③ 左边缘侧滑 ④ 强杀重启 App。
    实测这个 App 的直播间经常把返回键吞掉，所以不能只按两次就放弃。"""
    for _ in range(3):
        _back()
        if not in_live_room():
            print("  ↩️  已退出直播间，继续监控")
            return True

    if _tap_live_close():
        FOCUS_CACHE["ts"] = 0.0
        time.sleep(1.0)
        if not in_live_room():
            print("  ↩️  点关闭按钮退出直播间")
            return True

    try:
        w, h = screen_size()
        swipe_device(2, h // 2, int(w * 0.6), h // 2, 220)
        FOCUS_CACHE["ts"] = 0.0
        time.sleep(1.0)
        if not in_live_room():
            print("  ↩️  侧滑退出直播间")
            return True
    except Exception:
        pass

    if _live_text_visible():
        print("  ⚠️  画面仍是直播间，直接强杀 App")

    print("  ⚠️  退不出直播间，强制重启 App")
    ok = restart_app()
    print("  ✅ 已重启并重新进入广告流程" if ok else "  ⚠️  重新进入广告失败，下一轮继续重试")
    return ok


# ------------------------- 主循环 -------------------------
def main():
    global last_ocr_ts, uia_fails

    load_profile()
    if PLATFORM == "ios":
        if not ios_core_ready() and not _ios_wda_available():
            ok, err, _code = ios_prepare()
            if not ok and not _ios_wda_available():
                print("❌ " + (err or "iPhone 未连接"))
                return
    use_ocr = ocr_available()
    use_uia = uia_available()
    sw, sh = screen_size()

    print("🚀 汽水音乐自动看广告脚本启动")
    if PLATFORM == "ios":
        driver = "CoreDevice USB" if ios_core_ready() else "WebDriverAgent"
        print(f"   平台: iOS / {driver}")
    else:
        print("   平台: Android / ADB")
    print(f"   逻辑分辨率: {sw}x{sh}")
    print(f"   主通道: {'OCR' if use_ocr else '（OCR 不可用）'}   "
          f"uiautomator: {'开' if use_uia else '关'}")
    print(f"   领奖目标: {REWARD_TARGETS}")
    print(f"   跳过目标: {SKIP_TARGETS if ENABLE_SKIP else '已禁用'}")
    print(f"   广告极速: {'开' if AD_FAST_MODE else '关'}")
    print("   停止请按 Ctrl + C")
    print("-" * 50)

    if not use_ocr and not use_uia:
        print("❌ 没有可用通道：请 pip install -r requirements.txt")
        return

    mute_on_start()
    idle = 0
    feeder_idle = 0
    try:
        while True:
            # 直播间检测放最前面：被拖进直播间后这里永远等不到目标按钮
            if in_live_room():
                escape_live_room()
                continue

            # 停在首页（不在广告里）说明流程断了，重新点「免费听」进去
            if "MainActivity" in current_focus():
                feeder_idle += 1
                if feeder_idle >= 3:
                    print("· 检测到停在首页，重新点「免费听」进入广告")
                    _tap_free_listen()
                    feeder_idle = 0
                continue
            feeder_idle = 0

            clicked = False

            # 主通道：OCR。奖励弹窗等覆盖层 uiautomator 抓不到，必须靠 OCR
            if use_ocr:
                screen = screenshot()
                if screen is not None:
                    target, _nodes = fast_scan_and_click(screen, "ocr", idle=idle)
                    if target:
                        clicked = True

            # 兜底：OCR 没找到时才用 uiautomator
            if not clicked and use_uia:
                nodes = dump_nodes(timeout=UIA_TIMEOUT)
                if nodes is None:
                    uia_fails += 1
                    if uia_fails >= UIA_MAX_FAIL:
                        print(f"⚠️  uiautomator 连续失败 {uia_fails} 次，本次会话关闭")
                        use_uia = False
                else:
                    uia_fails = 0
                    if match_and_click(nodes, "uia"):
                        clicked = True

            if clicked:
                idle = 0
                time.sleep(CLICK_COOLDOWN)
                continue

            idle += 1
            if idle % 10 == 0:
                print(f"· 等待中…（{idle} 轮，未发现可点按钮）")
            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        unmute()


if __name__ == "__main__":
    main()
