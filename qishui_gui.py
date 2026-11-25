# -*- coding: utf-8 -*-
"""
汽水音乐自动化本地部署 — 可视化控制台
======================================
复用 qishui_auto.py 的全部逻辑（截图 / OCR / 点击优先级 / ROI 防误点），
外面套一层本地网页界面：浏览器打开就能开始、停止、看实时画面、看日志、记免费听天数。

启动：双击 控制台.bat，或 python qishui_gui.py
只用标准库 + 已装好的 cv2/numpy，不引入任何新依赖。
"""
import base64
import atexit
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np

try:
    from cryptography.fernet import Fernet
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except Exception:
    Fernet = None
    InvalidSignature = Exception
    Ed25519PublicKey = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import qishui_auto as q  # noqa: E402
import ios_wda  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PORT = int(os.environ.get("QISHUI_PORT") or 8788)
PREVIEW_WIDTH = 720
JPEG_QUALITY = 76
MAX_LOG = 400
VIP_FILE = os.path.join(BASE_DIR, "vip_state.json")
SUPPORT_DIR = os.path.join(BASE_DIR, "support")
SUPPORT_MANIFEST = os.path.join(SUPPORT_DIR, "manifest.json")
SUPPORT_KINDS = {"wechat": "微信", "alipay": "支付宝"}
SUPPORT_EXTS = (".png", ".jpg", ".webp")
SUPPORT_MAX_BYTES = 4 * 1024 * 1024
SUPPORT_ENC_EXT = ".enc"
SUPPORT_KEY_NAME = ".key"
SUPPORT_PUBLIC_KEY_B64 = "flBIRAA+UlJLYdPnvHVeVZZ+EKcWVgS8NBrCrL44NVg="
AVATAR_PATH = os.path.join(BASE_DIR, "assets", "avatar.webp")
_support_cipher_cache = {"v": None, "mtime": 0.0}


# ============================ 状态 ============================
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.logs = []
        self.running = False
        self.stop_flag = threading.Event()
        self.clicks = 0
        self.rounds = 0
        self.start_ts = None
        self.frame = None
        self.frame_ts = 0.0
        self.device = {"connected": False, "model": "", "w": 0, "h": 0, "android": ""}
        self.device_ts = 0.0
        self.vip = {"start_days": 0, "per": 3, "cap": 30, "total": 0, "real_days": -1}
        self.real_ts = 0.0
        self.wda_prompt = False

    def log(self, text, level="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.append((ts, level, text.rstrip("\n")))
            if len(self.logs) > MAX_LOG:
                del self.logs[: len(self.logs) - MAX_LOG]

    def snapshot_logs(self, since=0):
        with self.lock:
            return len(self.logs), self.logs[since:]


S = State()
WDA = ios_wda.get_manager()
WDA.set_log_sink(lambda text, level="info": S.log(text, level))
atexit.register(WDA.shutdown)


def load_vip():
    try:
        with open(VIP_FILE, encoding="utf-8") as f:
            d = json.load(f)
        for k in ("start_days", "per", "cap", "total", "real_days"):
            if k in d:
                S.vip[k] = int(d[k])
    except Exception:
        pass


def save_vip():
    try:
        with open(VIP_FILE, "w", encoding="utf-8") as f:
            json.dump(S.vip, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def sniff_image(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if len(raw) > 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None, None


def _support_manifest_body(manifest):
    return json.dumps({
        "version": int(manifest.get("version") or 0),
        "files": manifest.get("files") or {},
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _support_manifest():
    """仅在签名和文件摘要全部匹配时返回清单。"""
    if Ed25519PublicKey is None or not os.path.isfile(SUPPORT_MANIFEST):
        return None
    try:
        with open(SUPPORT_MANIFEST, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        signature = base64.b64decode(manifest.get("signature") or "", validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(SUPPORT_PUBLIC_KEY_B64, validate=True)
        )
        public_key.verify(signature, _support_manifest_body(manifest))
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            return None
        for name, digest in files.items():
            if not isinstance(name, str) or os.path.basename(name) != name:
                return None
            if not isinstance(digest, str) or len(digest) != 64:
                return None
            path = os.path.join(SUPPORT_DIR, name)
            with open(path, "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() != digest:
                    return None
        return manifest
    except (OSError, ValueError, TypeError, InvalidSignature):
        return None
    except Exception:
        return None


def _support_cipher(manifest):
    if Fernet is None:
        return None
    key_path = os.path.join(SUPPORT_DIR, SUPPORT_KEY_NAME)
    if SUPPORT_KEY_NAME not in (manifest.get("files") or {}):
        return None
    try:
        mtime = os.path.getmtime(key_path)
        if _support_cipher_cache["v"] is not None and _support_cipher_cache["mtime"] == mtime:
            return _support_cipher_cache["v"]
        with open(key_path, "rb") as f:
            cipher = Fernet(f.read().strip())
        _support_cipher_cache.update(v=cipher, mtime=mtime)
        return cipher
    except Exception:
        return None


def _local_support_cipher():
    if Fernet is None:
        return None
    os.makedirs(SUPPORT_DIR, exist_ok=True)
    key_path = os.path.join(SUPPORT_DIR, SUPPORT_KEY_NAME)
    try:
        if os.path.isfile(key_path):
            with open(key_path, "rb") as f:
                key = f.read().strip()
        else:
            key = Fernet.generate_key()
            with open(key_path, "wb") as f:
                f.write(key)
        return Fernet(key)
    except Exception:
        return None


def support_file(kind, manifest=None):
    manifest = manifest if manifest is not None else _support_manifest()
    if not manifest:
        return None
    name = kind + SUPPORT_ENC_EXT
    if name not in (manifest.get("files") or {}):
        return None
    return os.path.join(SUPPORT_DIR, name)


def support_meta():
    manifest = _support_manifest()
    slots = {}
    for kind in SUPPORT_KINDS:
        path = support_file(kind, manifest)
        slots[kind] = {
            "exists": bool(path),
            "version": int(manifest.get("version") or 0) if manifest else 0,
            "encrypted": bool(path),
        }
    return slots


def read_support_blob(kind):
    manifest = _support_manifest()
    path = support_file(kind, manifest)
    if not path:
        return None, None, "暂未配置支持方式"
    cipher = _support_cipher(manifest)
    if cipher is None:
        return None, None, "支持模块暂时不可用"
    try:
        with open(path, "rb") as f:
            payload = cipher.decrypt(f.read())
        mime, sep, raw = payload.partition(b"\n")
        if not sep:
            return None, None, "支持模块数据不完整"
        ext, sniffed = sniff_image(raw)
        if not ext:
            return None, None, "图片格式无法识别"
        return raw, mime.decode("ascii", "ignore") or sniffed, ""
    except Exception:
        return None, None, "支持模块校验失败"


def save_support_qr(kind, data_url):
    """本地维护工具使用：保存后需要重新签名打包。"""
    if kind not in SUPPORT_KINDS:
        return False, "未知类型", False
    m = re.match(r"^data:image/(?:png|jpe?g|webp);base64,(.+)$", data_url or "", re.S)
    if not m:
        return False, "请粘贴 PNG / JPG / WebP 图片", False
    try:
        raw = base64.b64decode(m.group(1), validate=True)
    except Exception:
        return False, "图片数据损坏，请重新复制", False
    if len(raw) > SUPPORT_MAX_BYTES:
        return False, "图片超过 4 MB，请换一张小一点的", False
    ext, mime = sniff_image(raw)
    if not ext:
        return False, "无法识别图片格式", False
    os.makedirs(SUPPORT_DIR, exist_ok=True)
    encrypted = False
    target = os.path.join(SUPPORT_DIR, kind + "." + ext)
    try:
        cipher = _local_support_cipher()
    except Exception as e:
        return False, "加密组件不可用：%s" % e, False
    if cipher is not None:
        encrypted = True
        target = os.path.join(SUPPORT_DIR, kind + SUPPORT_ENC_EXT)
        payload = mime.encode("ascii") + b"\n" + raw
        try:
            stored = cipher.encrypt(payload)
        except Exception as e:
            return False, "加密失败：%s" % e, False
    else:
        stored = raw
    tmp = target + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(stored)
        os.replace(tmp, target)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, "保存失败：%s" % e, False
    keep = os.path.basename(target)
    for old in SUPPORT_EXTS + (SUPPORT_ENC_EXT,):
        if kind + old == keep:
            continue
        try:
            os.remove(os.path.join(SUPPORT_DIR, kind + old))
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return True, "", encrypted


def clear_support_qr(kind):
    if kind not in SUPPORT_KINDS:
        return False, "未知类型"
    for ext in SUPPORT_EXTS + (SUPPORT_ENC_EXT,):
        try:
            os.remove(os.path.join(SUPPORT_DIR, kind + ext))
        except FileNotFoundError:
            pass
        except OSError as e:
            return False, f"删除失败：{e}"
    return True, ""


def vip_days():
    """优先用手机实测天数，读不到才退回本地估算。"""
    cap = max(1, int(S.vip["cap"]))
    real = int(S.vip.get("real_days", -1))
    if real >= 0:
        return min(cap, real)
    per = max(1, int(S.vip["per"]))
    start = max(0, int(S.vip["start_days"]))
    return min(cap, start + int(S.vip["total"]) // per)


def vip_estimate():
    per = max(1, int(S.vip["per"]))
    cap = max(1, int(S.vip["cap"]))
    start = max(0, int(S.vip["start_days"]))
    return min(cap, start + int(S.vip["total"]) // per)


def cap_reached():
    """手机实测天数已经够上限了就不该再刷（用户设的上限，默认 30 天）"""
    try:
        cap = int(S.vip.get("cap", 30))
        real = int(S.vip.get("real_days", -1))
    except Exception:
        return False
    return cap > 0 and real >= 0 and real >= cap


def refresh_real_days(force=False):
    """OCR 首页「免费听N天」，把真实天数写进状态。"""
    now = time.time()
    if not force and now - S.real_ts < 12.0:
        return
    S.real_ts = now
    try:
        n = q.read_free_days(fast=True)
    except Exception:
        n = None
    apply_real_days(n)


def apply_real_days(n):
    if n is None:
        return
    try:
        n = int(n)
    except Exception:
        return
    if n < 0:
        return
    if n != int(S.vip.get("real_days", -1)):
        S.vip["real_days"] = n
        save_vip()
        S.log("📱 手机实测免费听：%d 天" % n, "ok")


# ============================ stdout 劫持 ============================
class Tee:
    def __init__(self, original):
        self.original = original

    def write(self, s):
        try:
            self.original.write(s)
        except Exception:
            pass
        if s and s.strip():
            S.log(s, "dim")
        return len(s)

    def flush(self):
        try:
            self.original.flush()
        except Exception:
            pass

    def __getattr__(self, item):
        return getattr(self.original, item)


# ============================ 设备 ============================
def apply_device_info(info):
    """把 q.get_device_info() 的结果同步到全局状态，前端轮询直接可见。"""
    info = info or {}
    S.device = {
        "connected": bool(info.get("connected")),
        "ready": bool(info.get("ready", info.get("connected"))),
        "state": info.get("state") or "no_device",
        "platform": info.get("platform") or q.PLATFORM,
        "driver": info.get("driver") or "",
        "model": info.get("model") or "",
        "name": info.get("name") or info.get("model") or "",
        "device_name": info.get("device_name") or "",
        "brand": info.get("brand") or "",
        "serial": info.get("serial") or "",
        "w": int(info.get("width") or 0),
        "h": int(info.get("height") or 0),
        "physical_w": int(info.get("physical_width") or 0),
        "physical_h": int(info.get("physical_height") or 0),
        "points_w": int(info.get("points_width") or 0),
        "points_h": int(info.get("points_height") or 0),
        "density": int(info.get("density") or 0),
        "android": info.get("android") or "",
        "ios": info.get("ios") or "",
        "build": info.get("build") or "",
        "developer_mode": info.get("developer_mode"),
        "hint": info.get("hint") or "",
        "wda": info.get("wda") or q.IOS_WDA_URL,
        "bundle": info.get("bundle") or q.IOS_BUNDLE_ID,
        "needs_wda": bool(info.get("needs_wda")),
        "wda_version": info.get("wda_version") or "",
        "error": info.get("error") or "",
    }
    S.device_ts = time.time()
    if S.device["connected"]:
        q.set_screen_size(S.device["w"], S.device["h"])
    return S.device["connected"]


def detect_device(force=False):
    return apply_device_info(q.get_device_info(force=force))


# ============================ 画面 ============================
def encode_frame(img):
    h, w = img.shape[:2]
    if w > PREVIEW_WIDTH:
        s = PREVIEW_WIDTH / float(w)
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    return buf.tobytes() if ok else None


def preview_worker():
    """空闲时尽可能连续抓帧（受限于 adb screencap 本身约 1~1.5s/张）"""
    while True:
        try:
            if not S.running and S.device.get("connected"):
                if time.time() - S.frame_ts > 0.15:
                    img = q.screenshot()
                    if img is not None:
                        jpg = encode_frame(img)
                        if jpg:
                            S.frame = jpg
                            S.frame_ts = time.time()
        except Exception:
            pass
        time.sleep(0.05)


def ocr_prewarm_worker():
    """空闲时提前加载 OCR，避免第一次点开始还要等模型初始化。"""
    time.sleep(1.2)
    try:
        if not q.ocr_available():
            return
        q.get_ocr()
        import numpy as np
        q.ocr_nodes(np.zeros((64, 64, 3), dtype=np.uint8))
        S.log("OCR 引擎已预热", "dim")
    except Exception as e:
        S.log(f"OCR 预热失败：{e}", "warn")


def days_bootstrap_worker():
    """启动后校准一次首页天数，修正上次退出时留下的旧缓存。"""
    for delay in (4.0, 8.0, 15.0, 25.0, 40.0):
        time.sleep(delay)
        if S.running:
            return
        try:
            n = q.read_free_days(fast=True)
        except Exception:
            n = None
        if n is not None:
            apply_real_days(n)
            return


# ============================ 点击计数 ============================
_orig_do_click = q.do_click


def _counting_do_click(x, y, target, label, source, screen=None):
    ok = _orig_do_click(x, y, target, label, source, screen)
    if ok:
        S.clicks += 1
        if target == "领取奖励":
            S.rounds += 1
            before = vip_days()
            S.vip["total"] = int(S.vip["total"]) + 1
            save_vip()
            after = vip_days()
            if after != before:
                S.log(f"看完一条 → 估算 {after} 天（手机实测为准，上限 {S.vip['cap']} 天）", "ok")
    return ok


q.do_click = _counting_do_click


# ============================ 主循环 ============================
def worker():
    # 读取机型参数（GUI 保存的配置在这里生效）
    q.load_profile()
    try:
        if q.PROFILE.get("ocr_width"):
            q.OCR_MAX_WIDTH = int(q.PROFILE["ocr_width"])
    except Exception:
        pass
    if q.PLATFORM == "ios":
        info = q.get_ios_device_info(force=True)
        apply_device_info(info)
        try:
            major = int(str(S.device.get("ios") or "").split(".", 1)[0])
        except Exception:
            major = 0
        if (0 < major < 27) or S.device.get("needs_wda"):
            if not q.ios_available():
                S.log("iOS 17–26.x 需要 WebDriverAgent，正在准备兼容通道…", "info")
                ok, err = WDA.ensure_running(dict(S.device or {}))
                if not ok:
                    S.wda_prompt = True
                    S.log(err or "WDA 启动失败", "error")
                    S.running = False
                    return
                q.configure_platform("ios", ios_wda.WDA_URL, q.IOS_BUNDLE_ID)
                q.screen_cache = None
                q._ios_device_cache.update(v=None, ts=0.0)
            S.log("旧版 iPhone 兼容通道已就绪", "ok")
        elif not q.ios_available():
            S.log("iPhone USB 已连接，正在准备免签直连（首次可能需要几分钟）…", "info")
            ok, err, code = q.ios_prepare()
            if not ok and not q.ios_available():
                if code == "developer_mode":
                    S.log("请在 iPhone 打开「设置 → 隐私与安全性 → 开发者模式」，开启并重启后再点开始", "error")
                else:
                    S.log(err or "iPhone 直连准备失败", "error")
                S.running = False
                return
            q.screen_cache = None
            q._ios_device_cache.update(v=None, ts=0.0)
            S.log("iPhone 免签直连已就绪", "ok")
    if not detect_device():
        if q.PLATFORM == "ios":
            S.log(S.device.get("error") or "未检测到 iPhone，请检查 USB、解锁并信任电脑", "error")
        else:
            S.log("未检测到设备：请检查 USB 连接与「USB 调试」授权", "error")
        S.running = False
        return

    sw, sh = q.screen_size()
    S.log(f"设备已连接：{S.device.get('name') or S.device.get('model')}  {sw}x{sh}"
          f"  {'iOS ' + S.device.get('ios', '') if q.PLATFORM == 'ios' else 'Android ' + S.device.get('android', '')}", "ok")
    S.log(f"OCR 主通道已启用，缩放宽度 {q.OCR_MAX_WIDTH}", "info")

    if q.PLATFORM == "ios":
        if not q.ios_launch_app():
            S.log("汽水音乐启动失败：请确认 App 已安装、手机已解锁后重试", "error")
            S.running = False
            return
        time.sleep(1.8)
        S.log("汽水音乐已切换到前台", "dim")

    if q.MUTE_ON_START:
        q.mute_on_start()

    # 只有缓存显示已到上限时才复核，普通启动不再为了读天数而卡住
    if cap_reached():
        try:
            n0 = q.read_free_days(fast=False)
        except Exception:
            n0 = None
        apply_real_days(n0)
    else:
        S.log("启动优化：直接进入广告流程，天数在运行中校验", "dim")

    if cap_reached():
        S.log("🎉 免费听已到上限 %d 天，不用再刷了" % int(S.vip.get("cap", 30)), "ok")
        try:
            q.unmute()
        except Exception:
            pass
        S.running = False
        return

    idle = 0
    feeder_idle = 0
    feed_tries = 0
    live_fails = 0
    S.start_ts = time.time()
    try:
        while not S.stop_flag.is_set():
            # 任何路径下只要手机实测天数够上限就收工
            if cap_reached():
                S.log("🎉 免费听已到上限 %d 天，自动停止挂机" % int(S.vip.get("cap", 30)), "ok")
                break

            # 直播间检测：被拖进直播间后永远等不到目标按钮，必须主动逃出
            if (q.PLATFORM != "ios" or idle >= 6) and q.in_live_room():
                S.log("检测到进入直播间，正在退出…", "warn")
                ok = q.escape_live_room()
                idle = 0
                feeder_idle = 0
                if ok:
                    live_fails = 0
                else:
                    live_fails += 1
                    time.sleep(min(15, 3 * live_fails))
                continue

            # 停在首页说明广告流程断了（常见：领完奖励弹「恭喜获得第N天」模态弹窗）
            if "MainActivity" in q.current_focus():
                if (int(S.vip.get("real_days", -1)) < 0
                        or int(S.vip.get("real_days", -1)) >= int(S.vip.get("cap", 30)) - 1):
                    refresh_real_days()
                if cap_reached():
                    S.log("🎉 免费听已到上限 %d 天，自动停止挂机" % int(S.vip.get("cap", 30)), "ok")
                    S.stop_flag.set()
                    break
                feeder_idle += 1
                if feeder_idle >= 1:
                    feeder_idle = 0
                    feed_tries += 1
                    if feed_tries >= 3:
                        # 奖励弹窗点不动 = App UI 线程假死，只有强杀能救
                        S.log("首页弹窗点不动（App 假死），强杀重启 App", "warn")
                        q.restart_app()
                        feed_tries = 0
                    else:
                        S.log("停在首页，接续下一条广告", "warn")
                        q.enter_next_ad()
                        apply_real_days(q.LAST_FREE_DAYS)
                time.sleep(q.POLL_INTERVAL)
                continue
            feeder_idle = 0
            feed_tries = 0

            clicked = False
            screen = q.screenshot()
            if screen is not None:
                S.frame = encode_frame(screen)
                S.frame_ts = time.time()
                try:
                    target, nodes = q.fast_scan_and_click(screen, "ocr", idle=idle)
                    try:
                        n_day = q.parse_free_days(nodes)
                        if n_day is None:
                            n_day = q.parse_reward_days(nodes)
                    except Exception:
                        n_day = None
                    apply_real_days(n_day)
                    if cap_reached():
                        S.log("🎉 免费听已到上限 %d 天，自动停止挂机" % int(S.vip.get("cap", 30)), "ok")
                        S.stop_flag.set()
                        break
                    if target:
                        clicked = True
                except Exception as e:
                    S.log(f"识别/点击异常：{e}", "error")

            if clicked:
                idle = 0
                time.sleep(q.CLICK_COOLDOWN)
            else:
                idle += 1
                if idle % 12 == 0:
                    S.log(f"等待中…（{idle} 轮没找到可点按钮，广告可能还在播）", "dim")
                time.sleep(q.POLL_INTERVAL)
    except Exception as e:
        S.log(f"运行异常：{e}", "error")
    finally:
        try:
            q.unmute()
        except Exception:
            pass
        S.running = False
        S.stop_flag.clear()
        S.log("已停止", "warn")


def start_run():
    if S.running:
        return {"started": False, "running": True}
    if not S.device.get("connected") and not detect_device():
        if q.PLATFORM == "ios":
            S.log(S.device.get("error") or "未检测到 iPhone，请检查 USB 连接并信任电脑", "error")
        else:
            S.log("未检测到设备：请检查 USB 连接与「USB 调试」授权", "error")
        return {"started": False, "running": False}
    if q.PLATFORM == "ios" and S.device.get("needs_wda") and not q.ios_available():
        status = WDA.status(device=dict(S.device or {}), deep=True)
        if not status.get("installed") or status.get("needs_resign"):
            S.wda_prompt = True
            message = ("WDA 签名已过期，请重新安装/续签" if status.get("needs_resign")
                       else "iOS 17–26.x 需要先安装 WDA")
            S.log(message, "warn")
            return {
                "started": False,
                "running": False,
                "needs_wda_setup": True,
                "wda": status,
            }
    S.wda_prompt = False
    S.clicks = 0
    S.rounds = 0
    S.running = True
    S.stop_flag.clear()
    S.log("开始运行", "ok")
    threading.Thread(target=worker, daemon=True).start()
    return {"started": True, "running": True}


def _safe_unmute():
    for _ in range(40):
        if not S.running:
            break
        time.sleep(0.25)
    try:
        q.unmute()
    except Exception:
        pass


def stop_run():
    if not S.running:
        return
    S.stop_flag.set()
    S.log("正在停止…", "warn")
    threading.Thread(target=_safe_unmute, daemon=True).start()


# ============================ 页面 ============================
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>汽水音乐自动化本地部署</title>
<style>
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro SC","PingFang SC","Microsoft YaHei",sans-serif;
  color:#0b0d16;overflow:hidden;-webkit-font-smoothing:antialiased;
}
/* 蓝紫渐变背景 + 两团柔光 */
body::before{
  content:"";position:fixed;inset:0;z-index:-2;
  background:linear-gradient(150deg,#dfe6ff 0%,#e9e2ff 38%,#e4ecff 68%,#eee6ff 100%);
}
body::after{
  content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(620px 420px at 14% -6%, rgba(110,140,255,.42), transparent 70%),
    radial-gradient(560px 400px at 88% 4%, rgba(160,110,255,.34), transparent 72%),
    radial-gradient(680px 460px at 62% 104%, rgba(120,180,255,.28), transparent 74%);
}
.wrap{height:100%;max-width:1400px;margin:0 auto;padding:14px;
  display:grid;grid-template-columns:176px minmax(0,1fr) 336px;gap:12px}

/* 液态玻璃卡片 */
.glass{
  background:linear-gradient(160deg,rgba(255,255,255,.44),rgba(255,255,255,.24));
  border:1px solid rgba(255,255,255,.85);
  border-radius:20px;
  box-shadow:0 12px 34px rgba(70,90,160,.16), inset 0 1px 0 rgba(255,255,255,.95);
  backdrop-filter:blur(26px) saturate(180%);
  -webkit-backdrop-filter:blur(26px) saturate(180%);
}

/* ── 左列：项目名 ── */
.brand{display:flex;flex-direction:column;padding:18px 15px 15px;gap:0;min-height:0}
.brand .logo{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;
  font-size:15px;font-weight:700;color:#fff;
  background:linear-gradient(150deg,#7c9bff,#8b5cf6);
  box-shadow:0 6px 16px rgba(124,110,255,.4)}
.brand h1{margin:10px 0 0;font-size:21px;line-height:1.5;font-weight:700;color:#080a12;letter-spacing:1px}
.brand .sub{margin-top:8px;font-size:11.5px;line-height:1.75;color:#4a5068}
.chip{display:flex;align-items:center;gap:7px;font-size:11.5px;color:#2a3050;
  padding:7px 9px;border-radius:11px;background:rgba(255,255,255,.38);
  border:1px solid rgba(255,255,255,.9);box-shadow:inset 0 1px 0 #fff}
.dot{width:6px;height:6px;border-radius:50%;background:#97a2c4}
.dot.on{background:#12b76a;box-shadow:0 0 8px rgba(18,183,106,.65)}
.dot.run{background:#7c9bff;box-shadow:0 0 8px rgba(124,155,255,.75);animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.qq{margin-top:2px;font-size:11px;letter-spacing:.4px;color:#5a6180;text-align:center;user-select:all}
body.dark .qq{color:#7d86a6}
.note{margin-top:10px;padding:8px 9px;border-radius:10px;font-size:10.5px;line-height:1.6;
  color:#7a4a12;background:rgba(255,196,92,.16);border:1px solid rgba(255,180,60,.28)}
body.dark .note{color:#e0b070;background:rgba(255,180,60,.09);border-color:rgba(255,180,60,.18)}

/* ── 中列 ── */
.main{display:flex;flex-direction:column;gap:12px;min-height:0}
.stack{display:flex;flex-direction:column;gap:12px;flex:1 1 auto;min-height:0}
.card{padding:14px 15px}
.card h2,.pv h2{margin:0 0 10px;font-size:11.5px;font-weight:700;letter-spacing:.05em;color:#3d4468}

.btnrow{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.btn{height:40px;border:0;border-radius:13px;font-size:14px;font-weight:700;color:#fff;
  cursor:pointer;transition:.18s;
  background:linear-gradient(150deg,#7c9bff,#6b7dff 45%,#8b5cf6);
  box-shadow:0 8px 22px rgba(110,110,255,.32), inset 0 1px 0 rgba(255,255,255,.45)}
.btn:hover{filter:brightness(1.05)}
.btn:disabled{opacity:.42;cursor:default;filter:none;box-shadow:none}
.btn.stop{background:linear-gradient(150deg,#ff9db0,#f2536f 48%,#d92d55);
  box-shadow:0 8px 22px rgba(242,83,111,.3), inset 0 1px 0 rgba(255,255,255,.4)}
.btn.mini{height:32px;font-size:12.5px;border-radius:11px}
.btn.ghost{background:rgba(255,255,255,.42);color:#20263f;
  box-shadow:inset 0 1px 0 #fff,0 2px 8px rgba(70,90,160,.1)}

.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.stat{padding:9px 10px;border-radius:13px;background:rgba(255,255,255,.40);
  border:1px solid rgba(255,255,255,.9)}
.stat .v{font-size:20px;font-weight:700;color:#080a12;line-height:1.2;font-variant-numeric:tabular-nums}
.stat .v.b{color:#4c5cf0}
.stat .k{font-size:11px;color:#4a5068;margin-top:2px}

.switch{display:flex;align-items:center;justify-content:space-between;margin-top:8px;font-size:13px}
.switch label{color:#151a2e;font-weight:600}
.tog{position:relative;width:42px;height:24px;flex:0 0 42px}
.tog input{position:absolute;inset:0;opacity:0;margin:0;cursor:pointer;z-index:2}
.tog span{position:absolute;inset:0;border-radius:999px;background:rgba(120,130,170,.32);transition:.2s}
.tog span::after{content:"";position:absolute;top:2px;left:2px;width:20px;height:20px;
  border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(20,30,70,.28);transition:.2s}
.tog input:checked + span{background:linear-gradient(150deg,#6b8cff,#8b5cf6)}
.tog input:checked + span::after{transform:translateX(18px)}

.viprow{display:flex;align-items:baseline;gap:8px;margin-top:2px}
.vipdays{font-size:38px;font-weight:800;line-height:1;letter-spacing:-1px;
  font-variant-numeric:tabular-nums;color:#0a0d18}
.vipunit{font-size:12px;color:#4a5068}
.vipcap{margin-left:auto;font-size:11.5px;color:#4a5068}
.bar{height:6px;border-radius:99px;background:rgba(120,140,200,.22);margin:10px 0 8px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:99px;
  background:linear-gradient(90deg,#6b8cff,#8b5cf6);transition:width .4s}
.vipmeta{display:flex;gap:12px;font-size:11.5px;color:#4a5068;flex-wrap:wrap}
.vipmeta b{color:#0b0d16;font-weight:700}
.nums{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}
.num{display:flex;flex-direction:column;gap:4px}
.num span{font-size:11px;color:#4a5068}
.num input,.cfgrow input,.cfgrow select{width:100%;height:30px;border-radius:10px;padding:0 8px;font-size:12.5px;
  color:#0b0d16;background:rgba(255,255,255,.52);border:1px solid rgba(255,255,255,.8);outline:none;
  box-shadow:inset 0 1px 2px rgba(60,80,140,.06)}
.num input:focus,.cfgrow input:focus,.cfgrow select:focus{border-color:#7c9bff}

.logbox{flex:1 1 auto;min-height:110px;overflow:auto;padding:10px 12px;border-radius:15px;
  font:12px/1.85 ui-monospace,"SF Mono",Consolas,monospace;color:#20263f;
  background:rgba(255,255,255,.36);border:1px solid rgba(255,255,255,.7);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.8)}
.logbox .t{color:#7b83a0;margin-right:6px}
.ok{color:#2f49c8}.warn{color:#a06400}.error{color:#c02b4e}
.dim{color:#5b6484}.info{color:#20263f}

/* ── 右列：参数 + 画面 ── */
.pv{display:flex;flex-direction:column;gap:10px;padding:14px;min-height:0}
.cfg{display:flex;flex-direction:column;gap:7px}
.cfgrow{display:flex;align-items:center;gap:8px;font-size:12px}
.cfgrow span{flex:0 0 44px;color:#4a5068}
.cfgrow input,.cfgrow select{flex:1 1 auto}
.iosrow{display:none}
.cfg2{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.cfg2 .cfgrow span{flex:0 0 30px}
.cfghint{font-size:11px;line-height:1.6;color:#5b6484}
.devmeta{padding:7px 9px;border-radius:10px;background:rgba(124,155,255,.10);
  border:1px solid rgba(124,155,255,.18);font-size:11px;line-height:1.55;color:#3d4468;
  word-break:break-all}
.cfmsg{min-height:15px;font-size:11px;line-height:1.5;color:#5b6484}
.cfmsg.ok{color:#2f49c8}
.cfmsg.error{color:#c02b4e}
.cfgbtns{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:2px}
.preview{position:relative;flex:1 1 auto;min-height:150px;border-radius:15px;overflow:hidden;
  background:linear-gradient(155deg,#f7f8fa,#eceef2);border:1px solid rgba(255,255,255,.9);
  display:grid;place-items:center;box-shadow:0 10px 26px rgba(60,80,150,.10)}
.preview img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:block}
.badge{position:absolute;top:8px;left:8px;z-index:2;font-size:10.5px;padding:3px 7px;
  border-radius:8px;color:#fff;background:rgba(20,24,40,.55);
  border:1px solid rgba(255,255,255,.22);backdrop-filter:blur(8px)}
.empty{font-size:12px;color:#8f97b4;text-align:center;line-height:1.8}
.hint{font-size:11px;color:#5b6484;line-height:1.6}
/* ===== 暗色主题（body.dark 时生效） ===== */
body.dark{color:#e6e8ee}
body.dark::before{background:linear-gradient(150deg,#0a0b12,#0e0b18 40%,#07080f 100%)}
body.dark::after{background:
  radial-gradient(620px 420px at 14% -6%, rgba(96,124,255,.22), transparent 70%),
  radial-gradient(560px 400px at 88% 4%, rgba(150,104,255,.18), transparent 72%),
  radial-gradient(680px 460px at 62% 104%, rgba(80,140,255,.14), transparent 74%)}
body.dark .glass{background:linear-gradient(160deg,rgba(34,36,48,.48),rgba(18,19,26,.38));
  border-color:rgba(255,255,255,.10);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.10),0 14px 36px rgba(0,0,0,.5)}
body.dark .brand h1{color:#f4f6fb}
body.dark .brand .sub,body.dark .stat .k,body.dark .vipunit,body.dark .vipcap,
body.dark .vipmeta,body.dark .num span,body.dark .cfgrow span,
body.dark .cfghint,body.dark .hint,body.dark .cfmsg{color:#8a92ab}
body.dark .devmeta{background:rgba(124,155,255,.08);border-color:rgba(124,155,255,.16);color:#aeb7cf}
body.dark .cfmsg.ok{color:#8ea8ff}
body.dark .cfmsg.error{color:#ff8fa3}
body.dark .chip{background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.08);color:#c3c9db;box-shadow:none}
body.dark .card h2,body.dark .pv h2{color:#8d95b0}
body.dark .stat{background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.07)}
body.dark .stat .v{color:#f2f4fa}
body.dark .stat .v.b{color:#8ea8ff}
body.dark .switch label{color:#cfd5e8}
body.dark .vipdays{color:#fff}
body.dark .vipmeta b{color:#dfe3f0}
body.dark .num input,body.dark .cfgrow input,body.dark .cfgrow select{background:rgba(255,255,255,.07);
  border-color:rgba(255,255,255,.10);color:#eef1f8;box-shadow:none}
body.dark .logbox{background:rgba(6,7,12,.42);border-color:rgba(255,255,255,.06);color:#b9c0d0;box-shadow:none}
body.dark .logbox .t{color:#666e86}
body.dark .dim{color:#8b93a6}
body.dark .info{color:#c3c9d8}
body.dark .ok{color:#7fb2ff}
body.dark .warn{color:#ffce7a}
body.dark .error{color:#ff8fa3}
body.dark .btn.ghost{background:rgba(255,255,255,.09);color:#dde2f0;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.12)}
body.dark .bar{background:rgba(255,255,255,.10)}
body.dark .preview{border-color:rgba(255,255,255,.10);box-shadow:0 12px 30px rgba(0,0,0,.45)}
.themebtn{margin:0 0 8px;height:28px;border:1px solid rgba(255,255,255,.9);border-radius:10px;
  background:rgba(255,255,255,.62);color:#20263f;font-size:11.5px;font-weight:600;cursor:pointer;
  box-shadow:inset 0 1px 0 #fff;transition:.18s}
.themebtn:hover{filter:brightness(1.04)}
body.dark .themebtn{background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.12);color:#dde2f0;box-shadow:none}
.steps{margin:16px 0 0;padding:14px 0 0 16px;font-size:11.5px;line-height:1.7;color:#3d4468;border-top:1px solid rgba(120,140,200,.20);list-style:none;counter-reset:st}
.steps li{position:relative;margin-bottom:9px;padding-left:16px;counter-increment:st}
.steps li::before{content:counter(st);position:absolute;left:0;top:1px;width:13px;height:13px;border-radius:50%;background:rgba(124,155,255,.22);color:#4054c8;font-size:9px;font-weight:700;display:grid;place-items:center;line-height:1}
.steps li:last-child{margin-bottom:0}
.bottom{margin-top:auto;padding-top:14px;display:flex;flex-direction:column;gap:8px}
body.dark .steps{color:#98a0b8}
body.dark .steps{border-top-color:rgba(255,255,255,.08)}
body.dark .steps li::before{background:rgba(142,168,255,.20);color:#a9bdff}

/* ================= iOS 风格界面 ================= */
:root{
  --ios-blue:#007aff; --ios-green:#34c759; --ios-red:#ff3b30;
  --ios-orange:#ff9500; --ios-gray:#8e8e93; --ios-bg:#f2f2f7;
  --ios-card:#ffffff; --ios-fill:#f2f2f7; --ios-sep:rgba(60,60,67,.12);
}
body{
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display",
    "PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--ios-bg);color:#1c1c1e;overflow:hidden;
}
body::before,body::after{display:none}
.bg{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none;
  background:
    radial-gradient(42% 48% at 12% 4%, rgba(90,200,250,.20), transparent 72%),
    radial-gradient(38% 42% at 88% 8%, rgba(191,90,242,.16), transparent 72%),
    radial-gradient(44% 46% at 76% 96%, rgba(255,159,10,.14), transparent 72%),
    radial-gradient(38% 42% at 20% 92%, rgba(52,199,89,.12), transparent 72%),
    linear-gradient(150deg,#f6f8fd 0%,#f1f3fa 46%,#edf0f7 100%)}
.bg::before,.bg::after{content:"";position:absolute;border-radius:50%;
  filter:blur(60px);opacity:.40;will-change:transform}
.bg::before{width:52vw;height:52vw;left:-14vw;top:-22vw;
  background:radial-gradient(circle at 45% 45%,#5ac8fa,transparent 66%);
  animation:glassfloat 26s ease-in-out infinite alternate}
.bg::after{width:46vw;height:46vw;right:-10vw;bottom:-24vw;
  background:radial-gradient(circle at 50% 50%,#bf5af2,transparent 66%);
  animation:glassfloat 32s ease-in-out infinite alternate-reverse}
@keyframes glassfloat{from{transform:translate3d(-1.5%,-1%,0) scale(1.02)}
  to{transform:translate3d(2.5%,2%,0) scale(1.08)}}
@media (prefers-reduced-motion: reduce){.bg::before,.bg::after{animation:none}}
.wrap{height:100%;max-width:1560px;margin:0 auto;padding:14px;
  display:grid;grid-template-columns:204px minmax(0,1fr) 324px;
  grid-template-rows:auto auto minmax(0,1fr);gap:12px;position:relative;z-index:1}
.topbar{grid-column:1/-1;display:flex;align-items:center;gap:16px;
  padding:12px 16px;border-radius:22px}
.statusbar{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;
  gap:12px;padding:10px 14px;border-radius:18px}
.brand{grid-column:1;grid-row:3;padding:18px 16px 15px}
.main{grid-column:2;grid-row:3;min-height:0;
  display:grid;grid-template-columns:minmax(290px,.9fr) minmax(340px,1.1fr);gap:12px}
.pv{grid-column:3;grid-row:3;padding:12px;gap:8px;min-height:0;overflow-y:auto}
.glass{position:relative;
  background:
    radial-gradient(120% 100% at 0% 0%, rgba(255,255,255,.46), transparent 55%),
    linear-gradient(135deg, rgba(255,255,255,.72), rgba(255,255,255,.50) 52%,
      rgba(255,255,255,.64));
  border:1px solid rgba(255,255,255,.76);border-radius:24px;
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,.92),
    inset 0 -1px 0 rgba(255,255,255,.20),
    0 24px 55px rgba(30,50,90,.13),0 2px 8px rgba(30,50,90,.05);
  backdrop-filter:blur(30px) saturate(200%) brightness(1.04);
  -webkit-backdrop-filter:blur(30px) saturate(200%) brightness(1.04)}
.glass::after{content:"";position:absolute;left:16px;right:16px;top:0;height:1px;
  border-radius:1px;pointer-events:none;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.96),transparent)}
.topbrand{display:flex;align-items:center;gap:12px;min-width:220px}
.topbrand .logo{width:42px;height:42px;border-radius:13px;display:grid;place-items:center;
  font-size:18px;font-weight:700;color:#fff;
  background:linear-gradient(160deg,#0a84ff,#5e5ce6);
  box-shadow:0 8px 20px rgba(0,122,255,.28)}
.topbrand .logo.avatar{position:relative;overflow:hidden;padding:0;
  border:1.5px solid rgba(255,255,255,.88);
  background:linear-gradient(160deg,#ffb8d2,#ff8fb8);
  box-shadow:0 8px 20px rgba(255,105,160,.25)}
.topbrand .logo.avatar img{position:absolute;inset:0;display:block;
  width:100%;height:100%;object-fit:cover}
.topbrand-text h1{margin:0;font-size:22px;line-height:1.2;font-weight:700;
  letter-spacing:-.5px;color:#1c1c1e}
.topbrand-text .sub{margin-top:3px;font-size:11.5px;color:var(--ios-gray)}
.plat-switch{margin-left:auto;display:flex;gap:4px;padding:4px;
  border-radius:17px;border:1px solid rgba(255,255,255,.66);
  background:linear-gradient(135deg,rgba(255,255,255,.55),rgba(255,255,255,.26));
  box-shadow:inset 0 1px 0 rgba(255,255,255,.85),0 7px 20px rgba(30,50,90,.08);
  backdrop-filter:blur(18px) saturate(180%);
  -webkit-backdrop-filter:blur(18px) saturate(180%)}
.plat-btn{display:flex;align-items:center;gap:9px;padding:7px 14px 7px 8px;
  border:0;border-radius:13px;background:transparent;cursor:pointer;
  transition:background .18s,box-shadow .18s,transform .12s}
.plat-btn:active{transform:scale(.98)}
.plat-btn.active{background:linear-gradient(180deg,#fff,rgba(255,255,255,.90));
  box-shadow:0 2px 7px rgba(0,0,0,.14),inset 0 1px 0 #fff}
.plat-ico{width:30px;height:30px;border-radius:10px;display:grid;place-items:center;
  background:#e5e5ea;color:#3a3a3c;font-size:15px;font-weight:800;line-height:1}
.plat-ico svg{width:17px;height:17px;display:block}
.plat-btn.active .plat-ico{background:var(--ios-blue);color:#fff}
.plat-btn[data-platform="android"] .plat-ico{
  background:#e9f8ee;color:#3ddc84;
  box-shadow:inset 0 0 0 1px rgba(52,199,89,.10)}
.plat-btn[data-platform="android"] .plat-ico svg{width:19px;height:19px}
.plat-btn[data-platform="android"].active .plat-ico{
  background:#e1f7e9;color:#20bb59;
  box-shadow:inset 0 0 0 1px rgba(52,199,89,.18),0 2px 7px rgba(52,199,89,.16)}
.plat-txt{display:flex;flex-direction:column;align-items:flex-start;line-height:1.15}
.plat-txt b{font-size:13px;color:#1c1c1e}
.plat-txt small{font-size:10px;color:var(--ios-gray);margin-top:2px}
.themebtn{margin:0;height:34px;padding:0 14px;border-radius:999px;
  border:1px solid rgba(0,0,0,.08);background:rgba(255,255,255,.82);
  color:#1c1c1e;font-size:12px;font-weight:600;cursor:pointer;
  box-shadow:0 1px 2px rgba(0,0,0,.04);transition:background .16s,transform .12s}
.themebtn:active{transform:scale(.97)}
.supportbtn{display:inline-flex;align-items:center;gap:6px;height:34px;padding:0 14px;
  border-radius:999px;border:1px solid rgba(255,149,0,.28);
  background:linear-gradient(180deg,#fff8ec,#ffedd5);color:#9a5600;
  font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap;
  box-shadow:0 1px 2px rgba(255,149,0,.12);transition:filter .16s,transform .12s}
.supportbtn:hover{filter:brightness(1.02)}
.supportbtn:active{transform:scale(.97)}
.supportbtn .ico{display:grid;place-items:center}
.supportbtn .ico svg{width:15px;height:15px}
.status-main{display:flex;align-items:center;gap:10px;min-width:0}
.status-text{display:flex;flex-direction:column;min-width:0}
.status-text b{font-size:14px;color:#1c1c1e;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;max-width:70vw}
.status-text small{font-size:11px;color:var(--ios-gray);margin-top:1px}
.dot{width:8px;height:8px;flex:0 0 8px;border-radius:50%;background:#c7c7cc}
.dot.on{background:var(--ios-green);box-shadow:0 0 0 4px rgba(52,199,89,.14)}
.dot.run{background:var(--ios-blue);box-shadow:0 0 0 4px rgba(0,122,255,.14);
  animation:pulse 1.4s ease-in-out infinite}
.side-title{font-size:11px;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ios-gray)}
.steps{margin:12px 0 0;padding:12px 0 0;border-top:1px solid var(--ios-sep);
  font-size:12px;line-height:1.65;color:#3a3a3c;list-style:none;counter-reset:st}
.steps li{position:relative;margin-bottom:10px;padding-left:23px}
.steps li::before{width:16px;height:16px;background:var(--ios-blue);color:#fff;
  font-size:9px;font-weight:700;top:1px}
.note{margin-top:12px;padding:9px 10px;border-radius:12px;font-size:10.5px;
  line-height:1.6;color:#6e6e73;background:rgba(118,118,128,.10);
  border:1px solid rgba(118,118,128,.10)}
.card h2,.pv h2{margin:0 0 10px;font-size:11px;font-weight:700;
  letter-spacing:.09em;text-transform:uppercase;color:var(--ios-gray)}
.card{padding:16px}
.stat{background:var(--ios-fill);border:0;border-radius:16px;padding:11px 12px}
.stat .v{color:#1c1c1e;font-size:24px;letter-spacing:-.5px}
.stat .v.b{color:var(--ios-blue)}
.stat .k{color:var(--ios-gray);margin-top:3px}
.switch{font-size:13px}
.switch label{color:#1c1c1e;font-weight:500}
.tog span{background:#e5e5ea}
.tog input:checked + span{background:var(--ios-green)}
.btn{height:46px;border-radius:14px;border:0;color:#fff;
  background:linear-gradient(180deg,#339dff,#007aff 58%,#0068d7);
  font-size:15px;font-weight:600;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.38),0 9px 22px rgba(0,122,255,.28);
  transition:transform .12s,filter .16s}
.btn:active{transform:scale(.985)}
.btn:disabled{opacity:.42;box-shadow:none}
.btn.stop{background:linear-gradient(180deg,#ff6f67,#ff3b30 58%,#d92d25);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.35),0 9px 22px rgba(255,59,48,.24)}
.btn.ghost{background:#e5e5ea;color:#1c1c1e;box-shadow:none}
.btn.mini{height:36px;border-radius:11px;font-size:13px;box-shadow:none}
.vipdays{color:#1c1c1e;font-size:40px;letter-spacing:-1.5px}
.vipunit,.vipcap,.vipmeta,.num span{color:var(--ios-gray)}
.vipmeta b{color:#1c1c1e}
.bar{background:#e5e5ea;height:7px}
.bar i{background:linear-gradient(90deg,#0a84ff,#5e5ce6)}
.num input,.cfgrow input,.cfgrow select{background:var(--ios-fill);border:1px solid transparent;
  border-radius:11px;height:34px;color:#1c1c1e;box-shadow:none}
.num input:focus,.cfgrow input:focus,.cfgrow select:focus{
  border-color:var(--ios-blue);background:#fff}
.cfgrow span{color:var(--ios-gray)}
.cfg2 .cfgrow span{flex:0 0 40px}
.pv .cfg{gap:6px}
.pv .cfgrow{font-size:11.5px}
.pv .cfgrow span{flex-basis:38px}
.pv .cfgrow input,.pv .cfgrow select{height:31px;font-size:12px}
.pv .cfgbtns{gap:6px}
.pv .cfghint{font-size:10.5px;line-height:1.55}
.logbox{background:var(--ios-fill);border:0;border-radius:16px;color:#3a3a3c;
  box-shadow:none;font-size:11.5px;line-height:1.8}
.logbox .t{color:#a1a1a6}
.devmeta{background:var(--ios-fill);border:0;border-radius:12px;color:#3a3a3c;
  white-space:pre-line;max-height:none;overflow:visible;font-size:10.5px;line-height:1.55}
.cfghint,.hint,.cfmsg{color:var(--ios-gray)}
.cfmsg.ok{color:var(--ios-blue)}
.cfmsg.error{color:var(--ios-red)}
.preview{background:linear-gradient(155deg,#f7f8fa,#eceef2);border:0;border-radius:24px;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.82),0 10px 24px rgba(30,50,90,.07)}
.preview-card{display:flex;flex-direction:column;min-height:0;padding:14px;position:relative}
.preview-head{display:flex;align-items:center;justify-content:space-between;gap:10px}
.preview-head h2{margin:0;font-size:11px;font-weight:700;letter-spacing:.09em;
  text-transform:uppercase;color:var(--ios-gray)}
.preview-expand{height:28px;padding:0 11px;border:1px solid rgba(0,0,0,.07);
  border-radius:999px;background:rgba(255,255,255,.72);color:#3a3a3c;
  font-size:11px;font-weight:600;cursor:pointer}
.preview-expand:active{transform:scale(.97)}
.preview-card .preview{flex:1 1 auto;min-height:320px;margin-top:10px}
.preview-card .hint{margin-top:8px;text-align:center}
.preview-card.zoomed{position:fixed;inset:14px;z-index:75;padding:18px;
  background:linear-gradient(145deg,rgba(252,252,254,.98),rgba(235,237,241,.98));
  border-color:rgba(255,255,255,.92);box-shadow:0 40px 100px rgba(30,41,59,.26)}
.preview-card.zoomed .preview-head h2{color:#6e6e73}
.preview-card.zoomed .preview-expand{background:rgba(255,255,255,.76);
  border-color:rgba(60,60,67,.08);color:#1c1c1e}
.preview-card.zoomed .preview{min-height:0;margin-top:14px}
.preview-card.zoomed .hint{color:#6e6e73}
.badge{background:rgba(0,0,0,.55);border-color:rgba(255,255,255,.16);border-radius:10px}
.empty{color:#8e8e93}
.iosguide{display:none;margin-top:2px;padding:10px 12px;border-radius:12px;
  background:var(--ios-fill);font-size:11.5px;color:#3a3a3c}
.iosguide summary{font-weight:600;cursor:pointer;outline:none}
.iosguide ol{margin:8px 0 0;padding-left:18px;line-height:1.65}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;
  overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.chip{background:var(--ios-fill);border:0;box-shadow:none;color:#3a3a3c;
  border-radius:12px;padding:8px 10px}
.qq{color:var(--ios-gray)}

/* ================= 旧版 iPhone WDA ================= */
.wda-card{display:none;margin-top:9px;padding:11px 12px;border-radius:16px;
  background:linear-gradient(145deg,rgba(0,122,255,.09),rgba(255,255,255,.42));
  border:1px solid rgba(0,122,255,.14)}
.wda-card.show{display:block}
.wda-card-head{display:flex;align-items:center;gap:9px}
.wda-apple{width:31px;height:31px;flex:0 0 31px;border-radius:10px;display:grid;
  place-items:center;background:linear-gradient(160deg,#4b4b50,#1c1c1e);color:#fff;
  box-shadow:0 5px 12px rgba(20,20,25,.18)}
.wda-apple svg{width:17px;height:17px}
.wda-card-title{min-width:0;flex:1;display:flex;flex-direction:column;line-height:1.25}
.wda-card-title b{font-size:12.5px;color:#1c1c1e}
.wda-card-title small{margin-top:2px;font-size:10px;color:var(--ios-gray);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wda-chip{flex:0 0 auto;padding:4px 8px;border-radius:999px;background:rgba(118,118,128,.13);
  color:#6e6e73;font-size:9.5px;font-weight:700}
.wda-chip.ok{background:rgba(52,199,89,.15);color:#248a3d}
.wda-chip.warn{background:rgba(255,149,0,.15);color:#a56400}
.wda-chip.err{background:rgba(255,59,48,.13);color:#c0271f}
.wda-card-copy{margin:8px 0 0;font-size:10.5px;line-height:1.55;color:#5b6484}
.wda-card-btn{width:100%;margin-top:8px;height:32px;border:0;border-radius:10px;
  background:linear-gradient(180deg,#339dff,#007aff);color:#fff;font:inherit;
  font-size:11.5px;font-weight:700;cursor:pointer;box-shadow:0 5px 13px rgba(0,122,255,.20)}
.wda-card-btn:active{transform:scale(.985)}
.wda-card.coredevice{background:linear-gradient(145deg,rgba(52,199,89,.10),rgba(255,255,255,.44));
  border-color:rgba(52,199,89,.16)}
.wda-card.coredevice .wda-card-btn{display:none}
.wda-sheet{width:min(620px,100%)}
.wda-hero{display:flex;align-items:flex-start;gap:13px;padding:1px 28px 0 0}
.wda-hero .wda-apple{width:45px;height:45px;flex-basis:45px;border-radius:14px}
.wda-hero .wda-apple svg{width:24px;height:24px}
.wda-hero h3{margin:0;font-size:20px;letter-spacing:-.3px;color:#1c1c1e}
.wda-hero p{margin:5px 0 0;font-size:11.5px;line-height:1.65;color:#6e6e73}
.wda-warning{display:flex;gap:8px;margin-top:13px;padding:10px 11px;border-radius:12px;
  background:rgba(255,149,0,.11);border:1px solid rgba(255,149,0,.16);
  color:#7a4a12;font-size:10.8px;line-height:1.6}
.wda-warning b{font-weight:700}
.wda-steps{margin:12px 0 0;padding:0;list-style:none;display:grid;
  grid-template-columns:repeat(3,1fr);gap:7px;counter-reset:wda}
.wda-steps li{position:relative;padding:9px 9px 9px 28px;border-radius:11px;
  background:rgba(118,118,128,.08);font-size:10px;line-height:1.45;color:#3a3a3c;
  counter-increment:wda}
.wda-steps li::before{content:counter(wda);position:absolute;left:8px;top:9px;
  width:14px;height:14px;border-radius:50%;display:grid;place-items:center;
  background:rgba(0,122,255,.14);color:#0068d7;font-size:8px;font-weight:800}
.wda-status{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:12px}
.wda-status .item{padding:8px 10px;border-radius:11px;background:var(--ios-fill)}
.wda-status .k{font-size:9.5px;color:var(--ios-gray)}
.wda-status .v{margin-top:2px;font-size:11px;font-weight:600;color:#1c1c1e;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wda-progress{height:5px;margin-top:12px;border-radius:99px;background:rgba(118,118,128,.14);
  overflow:hidden;display:none}
.wda-progress.show{display:block}
.wda-progress i{display:block;height:100%;width:0;border-radius:99px;
  background:linear-gradient(90deg,#0a84ff,#5e5ce6);transition:width .3s}
.wda-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}
.wda-actions .btn{height:39px;font-size:12px;border-radius:11px}
.wda-actions .primary{grid-column:1/-1}
.wda-log{height:132px;margin-top:11px;padding:9px 11px;overflow:auto;border-radius:12px;
  background:rgba(0,0,0,.045);border:1px solid rgba(118,118,128,.08);
  font:10px/1.7 ui-monospace,"SF Mono",Consolas,monospace;color:#4a5068}
.wda-log .dim{color:#8e8e93}.wda-log .ok{color:#248a3d}.wda-log .error{color:#c0271f}
.wda-foot{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:10px}
.wda-foot small{font-size:9.5px;line-height:1.5;color:var(--ios-gray)}
.wda-logout{border:0;background:transparent;color:#8e8e93;font-size:10px;
  text-decoration:underline;cursor:pointer;padding:2px}
body.dark .wda-card{background:linear-gradient(145deg,rgba(10,132,255,.13),rgba(44,44,46,.60));
  border-color:rgba(10,132,255,.20)}
body.dark .wda-card.coredevice{background:linear-gradient(145deg,rgba(52,199,89,.13),rgba(44,44,46,.60));
  border-color:rgba(52,199,89,.20)}
body.dark .wda-card-title b,body.dark .wda-hero h3,body.dark .wda-status .v{color:#f2f2f7}
body.dark .wda-card-copy,body.dark .wda-hero p,body.dark .wda-foot small{color:#aeaeb2}
body.dark .wda-warning{background:rgba(255,159,10,.14);border-color:rgba(255,159,10,.20);color:#ffc46b}
body.dark .wda-steps li{background:rgba(118,118,128,.16);color:#d1d1d6}
body.dark .wda-steps li::before{background:rgba(10,132,255,.20);color:#8ec5ff}
body.dark .wda-status .item{background:#2c2c2e}
body.dark .wda-log{background:rgba(0,0,0,.22);border-color:rgba(255,255,255,.06);color:#c7c7cc}

/* ================= 支持弹窗 ================= */
.modal{position:fixed;inset:0;z-index:80;display:none;align-items:center;justify-content:center;
  padding:18px;background:rgba(12,16,28,.30);
  backdrop-filter:blur(17px) saturate(130%);-webkit-backdrop-filter:blur(17px) saturate(130%)}
.modal.show{display:flex}
.sheet{width:min(480px,100%);max-height:calc(100vh - 36px);overflow:auto;position:relative;
  background:linear-gradient(145deg,rgba(255,255,255,.93),rgba(255,255,255,.76));
  border:1px solid rgba(255,255,255,.86);border-radius:28px;padding:22px 22px 18px;
  backdrop-filter:blur(40px) saturate(200%);
  -webkit-backdrop-filter:blur(40px) saturate(200%);
  box-shadow:0 40px 100px rgba(20,30,60,.28),inset 0 1px 0 rgba(255,255,255,.96);
  animation:sheetIn .26s cubic-bezier(.22,.9,.24,1)}
@keyframes sheetIn{from{opacity:0;transform:translateY(18px) scale(.98)}
  to{opacity:1;transform:translateY(0) scale(1)}}
.sheet-close{position:absolute;top:13px;right:13px;width:30px;height:30px;border:0;border-radius:50%;
  background:rgba(118,118,128,.14);color:var(--ios-gray);font-size:15px;line-height:1;
  cursor:pointer;display:grid;place-items:center;transition:background .16s}
.sheet-close:hover{background:rgba(118,118,128,.22)}
.support-hero{text-align:center;padding:6px 10px 0}
.support-hero .appmark{width:54px;height:54px;margin:0 auto 10px;border-radius:17px;display:grid;
  place-items:center;color:#fff;font-size:24px;font-weight:700;
  background:linear-gradient(160deg,#0a84ff,#5e5ce6);
  box-shadow:0 10px 24px rgba(0,122,255,.30)}
.support-eyebrow{display:inline-flex;align-items:center;gap:6px;margin-bottom:7px;
  padding:5px 10px;border-radius:999px;background:rgba(118,118,128,.13);
  color:#6e6e73;font-size:10.5px;font-weight:600}
.support-eyebrow.run{background:rgba(52,199,89,.14);color:#248a3d}
.support-eyebrow.run::before{content:"";width:6px;height:6px;border-radius:50%;
  background:#34c759;box-shadow:0 0 0 3px rgba(52,199,89,.16)}
.support-hero h3{margin:0;font-size:21px;font-weight:700;letter-spacing:-.4px;color:#1c1c1e}
.support-hero p{margin:9px 0 0;font-size:12.5px;line-height:1.75;color:#6e6e73;
  text-wrap:pretty}
.support-hero p b{color:#1c1c1e;font-weight:700}
.support-free{display:flex;align-items:center;justify-content:center;gap:7px;
  margin-top:12px;padding:9px 12px;border-radius:12px;
  background:rgba(52,199,89,.13);color:#1f7a37;font-size:12px;font-weight:700;
  line-height:1.4}
.support-ask{display:flex;align-items:center;justify-content:center;gap:8px;
  margin-top:15px;font-size:11.5px;color:var(--ios-gray)}
.support-ask .cup-mini{width:24px;height:24px;flex:0 0 24px;border-radius:8px;display:grid;
  place-items:center;background:linear-gradient(160deg,#ffb340,#ff9500);
  box-shadow:0 4px 10px rgba(255,149,0,.22)}
.support-ask .cup-mini svg{width:14px;height:14px}
.support-continue{width:100%;margin-top:14px}
.pay-tabs{display:flex;gap:4px;padding:4px;margin:12px 0 12px;
  border-radius:14px;background:rgba(118,118,128,.12)}
.pay-tab{flex:1;height:34px;border:0;border-radius:11px;background:transparent;
  color:#3a3a3c;font-size:12.5px;font-weight:600;cursor:pointer;transition:.16s}
.pay-tab.active{background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.14);color:#1c1c1e}
.pay-tab[data-kind="wechat"].active{color:#07a854}
.pay-tab[data-kind="alipay"].active{color:#1677ff}
.qr-stage{position:relative;display:grid;place-items:center;min-height:272px;padding:14px;
  border-radius:20px;background:var(--ios-fill);border:1.5px dashed rgba(118,118,128,.30);
  transition:border-color .16s,background .16s}
.qr-stage.hasimg{border-style:solid;border-color:rgba(0,0,0,.06);background:#fff;padding:12px}
.qr-img{display:none;width:248px;height:248px;object-fit:contain;border-radius:12px;background:#fff}
.qr-empty{text-align:center;color:var(--ios-gray);font-size:11.5px;line-height:1.75}
.qr-empty .big{font-size:30px;line-height:1;margin-bottom:8px}
.qr-empty b{display:block;margin-bottom:3px;color:#3a3a3c;font-size:13px}
.qr-hint{min-height:17px;margin-top:8px;text-align:center;font-size:10.5px;
  line-height:1.6;color:var(--ios-gray)}
.qr-hint.ok{color:#248a3d}
.qr-hint.error{color:var(--ios-red)}

body.dark{background:#000;color:#f2f2f7}
body.dark .bg{background:
    radial-gradient(42% 48% at 12% 4%, rgba(10,132,255,.28), transparent 72%),
    radial-gradient(38% 42% at 88% 8%, rgba(191,90,242,.22), transparent 72%),
    radial-gradient(44% 46% at 76% 96%, rgba(255,159,10,.16), transparent 72%),
    radial-gradient(38% 42% at 20% 92%, rgba(52,199,89,.14), transparent 72%),
    linear-gradient(150deg,#050507 0%,#0b0b0f 52%,#07070a 100%)}
body.dark .glass{background:
    radial-gradient(120% 100% at 0% 0%, rgba(255,255,255,.09), transparent 55%),
    linear-gradient(135deg,rgba(46,46,50,.66),rgba(20,20,24,.50));
  border-color:rgba(255,255,255,.14);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.15),0 26px 60px rgba(0,0,0,.52)}
body.dark .glass::after{background:linear-gradient(90deg,transparent,
  rgba(255,255,255,.26),transparent)}
body.dark .topbrand-text h1,body.dark .status-text b,body.dark .stat .v,
body.dark .switch label,body.dark .vipdays,body.dark .vipmeta b,
body.dark .plat-txt b,body.dark .themebtn{color:#f2f2f7}
body.dark .topbrand-text .sub,body.dark .status-text small,body.dark .stat .k,
body.dark .vipunit,body.dark .vipcap,body.dark .vipmeta,body.dark .num span,
body.dark .cfgrow span,body.dark .cfghint,body.dark .hint,body.dark .cfmsg,
body.dark .side-title,body.dark .qq{color:var(--ios-gray)}
body.dark .plat-switch{background:linear-gradient(135deg,rgba(58,58,62,.58),rgba(28,28,32,.44));
  border-color:rgba(255,255,255,.12);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.14),0 7px 20px rgba(0,0,0,.35)}
body.dark .plat-btn.active{background:linear-gradient(180deg,#48484c,#333337);
  box-shadow:0 2px 7px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.16)}
body.dark .plat-ico{background:#3a3a3c;color:#fff}
body.dark .plat-btn[data-platform="android"] .plat-ico{
  background:rgba(52,199,89,.14);color:#3ddc84;
  box-shadow:inset 0 0 0 1px rgba(61,220,132,.10)}
body.dark .plat-btn[data-platform="android"].active .plat-ico{
  background:rgba(52,199,89,.22);color:#4ee58d;
  box-shadow:inset 0 0 0 1px rgba(61,220,132,.20),0 2px 8px rgba(52,199,89,.12)}
body.dark .themebtn{background:#2c2c2e;border-color:rgba(255,255,255,.08)}
body.dark .stat,body.dark .logbox,body.dark .devmeta,body.dark .iosguide,
body.dark .num input,body.dark .cfgrow input,body.dark .cfgrow select{background:#2c2c2e}
body.dark .num input,body.dark .cfgrow input,body.dark .cfgrow select{color:#f2f2f7}
body.dark .logbox{color:#d1d1d6}
body.dark .steps{color:#c7c7cc;border-top-color:var(--ios-sep)}
body.dark .note{background:rgba(118,118,128,.16);border-color:rgba(255,255,255,.06);color:#aeaeb2}
body.dark .card h2,body.dark .pv h2{color:var(--ios-gray)}
body.dark .bar{background:#3a3a3c}
body.dark .btn.ghost{background:#3a3a3c;color:#f2f2f7}
body.dark .progress,body.dark .chip{background:#2c2c2e;color:#d1d1d6}
body.dark .supportbtn{background:rgba(255,149,0,.14);border-color:rgba(255,159,10,.30);color:#ffb340}
body.dark .sheet{background:linear-gradient(145deg,rgba(46,46,50,.94),rgba(24,24,28,.88));
  border-color:rgba(255,255,255,.14);
  box-shadow:0 40px 100px rgba(0,0,0,.72),inset 0 1px 0 rgba(255,255,255,.15)}
body.dark .support-hero h3{color:#f2f2f7}
body.dark .support-hero p{color:#aeaeb2}
body.dark .support-hero p b{color:#f2f2f7}
body.dark .support-eyebrow{background:rgba(118,118,128,.24);color:#aeaeb2}
body.dark .support-eyebrow.run{background:rgba(52,199,89,.18);color:#7ee2a0}
body.dark .support-free{background:rgba(52,199,89,.18);color:#7ee2a0}
body.dark .pay-tabs{background:rgba(118,118,128,.24)}
body.dark .pay-tab{color:#d1d1d6}
body.dark .pay-tab.active{background:#3a3a3c;color:#fff}
body.dark .qr-stage{background:#2c2c2e;border-color:rgba(255,255,255,.14)}
body.dark .qr-stage.hasimg{background:#fff;border-color:transparent}
body.dark .qr-empty b{color:#f2f2f7}
body.dark .preview-expand{background:rgba(255,255,255,.10);
  border-color:rgba(255,255,255,.10);color:#d1d1d6}

/* ================= 三栏工作台布局 ================= */
.wrap{
  max-width:1760px;
  grid-template-columns:minmax(312px,344px) minmax(0,1fr) minmax(286px,316px);
  grid-template-areas:
    "top top top"
    "status status status"
    "rail workspace settings";
  align-items:stretch}
.topbar{grid-area:top;padding:10px 14px;border-radius:20px}
.statusbar{grid-area:status;padding:9px 13px;border-radius:18px}
.rail{grid-area:rail;display:flex;flex-direction:column;gap:10px;min-height:0;
  overflow-y:auto;overscroll-behavior:contain;padding-right:1px}
.rail::-webkit-scrollbar{width:5px}
.rail::-webkit-scrollbar-thumb{background:rgba(118,118,128,.25);border-radius:99px}
.workspace{grid-area:workspace;display:grid;grid-template-columns:minmax(340px,42%) minmax(0,1fr);
  min-height:0;padding:0;overflow:hidden}
.pv{grid-area:settings}
.rail .card{padding:12px 13px}
.rail .control-card,.rail .vip-card{flex:0 0 auto}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:10px;
  min-height:20px;margin-bottom:10px}
.panel-head h2{margin:0!important}
.panel-chip{display:inline-flex;align-items:center;gap:4px;min-height:23px;padding:0 9px;
  border-radius:999px;background:rgba(0,122,255,.10);color:var(--ios-blue);
  font-size:10.5px;font-weight:700;white-space:nowrap}
.panel-chip.soft{background:var(--ios-fill);color:var(--ios-gray);font-weight:600}
.panel-chip.soft b{color:#1c1c1e;font-weight:700}
.panel-chip.run{background:rgba(52,199,89,.13);color:#248a3d}
.control-card .btnrow{margin-top:0}
.control-card .stats{margin-top:9px}
.control-card .switch{margin-top:7px;padding:2px 1px}
.control-card .quickhelp{margin-top:10px}
.quickhelp{margin-top:10px;padding-top:9px;border-top:1px solid var(--ios-sep)}
.quickhelp summary,.config-details summary{display:flex;align-items:center;
  justify-content:space-between;gap:10px;list-style:none;cursor:pointer;
  color:#3a3a3c;font-size:11.5px;font-weight:600;outline:none;user-select:none}
.quickhelp summary::-webkit-details-marker,.config-details summary::-webkit-details-marker{display:none}
.quickhelp summary::after,.config-details summary::after{content:"";width:7px;height:7px;
  flex:0 0 7px;border-right:1.5px solid var(--ios-gray);border-bottom:1.5px solid var(--ios-gray);
  transform:rotate(45deg) translateY(-2px);transition:transform .18s}
.quickhelp[open] summary::after,.config-details[open] summary::after{
  transform:rotate(225deg) translate(-2px,-2px)}
.quickhelp .steps{margin-top:10px;padding-top:0;border-top:0}
.quickhelp .note{margin-top:8px}
.vip-card .viprow{margin-top:-1px}
.vip-card .vipdays{font-size:36px}
.vip-card .bar{margin:9px 0 7px}
.vip-card .nums{margin-top:8px}
.vip-card .num input{height:32px}
.logcard{min-width:0;min-height:0;display:flex;flex-direction:column;
  padding:0;overflow:hidden;border-left:1px solid var(--ios-sep);border-radius:0 24px 24px 0}
.loghead{height:42px;flex:0 0 42px;padding:0 16px;display:flex;align-items:center;
  justify-content:space-between;gap:12px;border-bottom:1px solid var(--ios-sep);
  color:#3a3a3c;font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase}
.log-title{display:flex;align-items:center;gap:8px}
.log-live-dot{width:7px;height:7px;border-radius:50%;background:var(--ios-green);
  box-shadow:0 0 0 4px rgba(52,199,89,.12)}
.log-meta{font-size:10px;font-weight:500;letter-spacing:.15px;text-transform:none;
  color:var(--ios-gray);white-space:nowrap}
.logcard .logbox{flex:1 1 auto;min-height:0;margin:10px 12px 12px;padding:10px 12px;
  border:1px solid rgba(118,118,128,.08);border-radius:16px;
  background:rgba(255,255,255,.30);box-shadow:none;font-size:11.5px;line-height:1.9}

/* 左栏紧凑化：保证在常见窗口高度下不需要内部拖动 */
.control-card .stats{gap:6px;margin-top:8px}
.control-card .stat{padding:8px 9px;border-radius:13px}
.control-card .stat .v{font-size:22px}
.switchgrid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:7px}
.control-card .switchgrid .switch{margin-top:0;gap:6px;font-size:11.5px}
.quickhelp{margin-top:8px;padding-top:8px}
.quickhelp .steps{margin-top:7px;font-size:10.5px;line-height:1.5}
.quickhelp .steps li{margin-bottom:6px;padding-left:18px}
.quickhelp .steps li::before{width:13px;height:13px;font-size:8px}
.quickhelp .note{margin-top:6px;padding:7px 8px;font-size:9.5px;line-height:1.5}
.vip-card .vipdays{font-size:32px}
.vip-card .bar{margin:7px 0 6px}
.vip-card .vipmeta{font-size:10.5px;gap:9px}
.vip-tip{margin-top:5px;padding:5px 7px;border-radius:9px;font-size:9px;
  line-height:1.35;color:#7a5a2e;background:rgba(255,159,10,.09);
  border:1px solid rgba(255,159,10,.14)}
body.dark .vip-tip{color:#c7a574;background:rgba(255,159,10,.07);
  border-color:rgba(255,159,10,.13)}
.vip-card .nums{gap:6px;margin-top:7px}
.vip-card .num input{height:30px}
.donate-card{padding:9px 11px}
.donate-ico{width:30px;height:30px;flex-basis:30px;border-radius:9px}
.donate-main b{font-size:12px}
.donate-main small{font-size:9.5px}

/* iOS 风格细滚动条：去掉经典上下箭头 */
.rail,.logbox,.sheet{
  scrollbar-width:thin;
  scrollbar-color:rgba(142,142,147,.38) transparent}
.rail::-webkit-scrollbar,.logbox::-webkit-scrollbar,.sheet::-webkit-scrollbar{
  width:7px;height:7px}
.rail::-webkit-scrollbar-track,.logbox::-webkit-scrollbar-track,
.sheet::-webkit-scrollbar-track{background:transparent}
.rail::-webkit-scrollbar-thumb,.logbox::-webkit-scrollbar-thumb,
.sheet::-webkit-scrollbar-thumb{
  border-radius:999px;background:rgba(142,142,147,.36);
  border:1.5px solid transparent;background-clip:padding-box}
.rail::-webkit-scrollbar-thumb:hover,.logbox::-webkit-scrollbar-thumb:hover,
.sheet::-webkit-scrollbar-thumb:hover{background:rgba(142,142,147,.58);
  background-clip:padding-box}
.rail::-webkit-scrollbar-button,.logbox::-webkit-scrollbar-button,
.sheet::-webkit-scrollbar-button{display:none;width:0;height:0}
.rail::-webkit-scrollbar-corner,.logbox::-webkit-scrollbar-corner,
.sheet::-webkit-scrollbar-corner{background:transparent}
body.dark .rail,body.dark .logbox,body.dark .sheet{
  scrollbar-color:rgba(235,235,245,.24) transparent}
body.dark .rail::-webkit-scrollbar-thumb,
body.dark .logbox::-webkit-scrollbar-thumb,
body.dark .sheet::-webkit-scrollbar-thumb{background:rgba(235,235,245,.24);
  background-clip:padding-box}
body.dark .rail::-webkit-scrollbar-thumb:hover,
body.dark .logbox::-webkit-scrollbar-thumb:hover,
body.dark .sheet::-webkit-scrollbar-thumb:hover{background:rgba(235,235,245,.36);
  background-clip:padding-box}

.preview-card{
  display:flex;flex-direction:column;min-width:0;min-height:0;padding:14px;
  overflow:hidden;border-right:1px solid var(--ios-sep);border-radius:24px 0 0 24px}
.preview-title{display:flex;align-items:center;gap:8px}
.preview-title h2{margin:0}
.preview-live-dot{width:7px;height:7px;border-radius:50%;background:#c7c7cc;
  box-shadow:0 0 0 3px rgba(199,199,204,.13);transition:.2s}
.preview-card.is-live .preview-live-dot{background:var(--ios-green);
  box-shadow:0 0 0 4px rgba(52,199,89,.14);animation:pulse 1.4s ease-in-out infinite}
.preview-card .preview{
  position:relative;flex:1 1 auto;min-height:0;margin-top:10px;
  display:grid;place-items:center;isolation:isolate;overflow:hidden;border-radius:26px;
  background:
    radial-gradient(56% 48% at 50% 102%, rgba(0,122,255,.10), transparent 72%),
    radial-gradient(42% 38% at 88% 4%, rgba(191,90,242,.08), transparent 76%),
    linear-gradient(155deg,#f8f9fb 0%,#eceef2 52%,#f4f5f7 100%);
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.92),
    inset 0 -28px 60px rgba(60,60,67,.04),0 10px 24px rgba(30,50,90,.05)}
.preview-card .preview::before{
  content:"";position:absolute;z-index:0;left:50%;bottom:-18%;width:62%;height:34%;
  transform:translateX(-50%);border-radius:50%;background:rgba(0,122,255,.10);
  filter:blur(46px);pointer-events:none}
.preview-card .preview::after{
  content:"";position:absolute;z-index:0;inset:0;pointer-events:none;opacity:.55;
  background-image:linear-gradient(rgba(60,60,67,.035) 1px,transparent 1px),
    linear-gradient(90deg,rgba(60,60,67,.035) 1px,transparent 1px);
  background-size:42px 42px;
  mask-image:linear-gradient(to bottom,transparent,#000 24%,#000 76%,transparent)}
.preview-card .preview img{
  position:absolute;z-index:1;inset:auto;left:50%;top:50%;transform:translate(-50%,-50%);
  width:auto;height:calc(100% - 30px);max-width:calc(100% - 50px);
  object-fit:contain;border-radius:27px;
  box-shadow:0 0 0 6px #f5f6f8,0 0 0 7px rgba(60,60,67,.12),
    0 24px 46px rgba(30,41,59,.18),0 5px 14px rgba(30,41,59,.10)}
.preview-card .preview .badge{z-index:3}
.preview-card .preview .empty{z-index:2;position:relative;max-width:280px;padding:18px}
.preview-card .hint{position:absolute;z-index:4;left:50%;bottom:18px;margin:0;
  transform:translateX(-50%);padding:3px 8px;border-radius:8px;white-space:nowrap;
  background:rgba(255,255,255,.72);color:#6e6e73;text-align:center;font-size:10px;
  border:1px solid rgba(60,60,67,.06);
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
.preview-card.zoomed .preview img{height:calc(100% - 24px);max-width:calc(100% - 26px)}

.config-panel{padding:14px;overflow:visible;overscroll-behavior:contain}
.config-panel::-webkit-scrollbar{width:5px}
.config-panel::-webkit-scrollbar-thumb{background:rgba(118,118,128,.25);border-radius:99px}
.config-panel .devmeta{max-height:none;margin:0 0 9px;padding:10px 11px;
  white-space:pre-line;font-size:10.5px;line-height:1.62;border-radius:13px}
.config-actions{display:grid;grid-template-columns:1.08fr .92fr;gap:7px;margin:0}
.config-actions .btn.mini{height:35px;font-size:11.5px}
.config-panel .cfmsg{min-height:17px;margin-top:5px;font-size:10.5px}
.config-panel .cfghint{margin-top:2px;font-size:10.5px;line-height:1.55}
.config-panel .cfg{margin-top:10px;padding-top:10px;border-top:1px solid var(--ios-sep)}

body.dark .panel-chip{background:rgba(10,132,255,.18);color:#8ec5ff}
body.dark .panel-chip.soft{background:#2c2c2e;color:var(--ios-gray)}
body.dark .panel-chip.soft b{color:#f2f2f7}
body.dark .panel-chip.run{background:rgba(52,199,89,.18);color:#7ee2a0}
body.dark .quickhelp summary,body.dark .config-details summary,
body.dark .loghead{color:#d1d1d6}
body.dark .logcard .logbox{background:rgba(0,0,0,.18);border-color:rgba(255,255,255,.05)}
body.dark .preview-card .preview{
  background:
    radial-gradient(56% 48% at 50% 102%, rgba(0,122,255,.20), transparent 72%),
    radial-gradient(42% 38% at 88% 4%, rgba(191,90,242,.15), transparent 76%),
    linear-gradient(155deg,#17181d 0%,#090a0e 52%,#101117 100%);
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.06),
    inset 0 -28px 60px rgba(0,0,0,.22),0 12px 30px rgba(0,0,0,.18)}
body.dark .preview-card .preview::before{background:rgba(0,122,255,.18)}
body.dark .preview-card .preview::after{
  opacity:.22;
  background-image:linear-gradient(rgba(255,255,255,.04) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,.04) 1px,transparent 1px)}
body.dark .preview-card .preview img{
  box-shadow:0 0 0 6px #1b1c21,0 0 0 7px rgba(255,255,255,.20),
    0 30px 58px rgba(0,0,0,.58),0 6px 18px rgba(0,0,0,.42)}
body.dark .preview-card .hint{background:rgba(0,0,0,.26);color:#a1a1a6;
  border-color:transparent}
body.dark .preview-card.zoomed{background:linear-gradient(145deg,rgba(26,27,32,.97),rgba(8,9,12,.98));
  border-color:rgba(255,255,255,.14);box-shadow:0 40px 100px rgba(0,0,0,.65)}
body.dark .preview-card.zoomed .preview-head h2{color:#d1d1d6}
body.dark .preview-card.zoomed .preview-expand{background:rgba(255,255,255,.14);
  border-color:rgba(255,255,255,.16);color:#fff}
body.dark .preview-card.zoomed .hint{color:#8e8e93}

.donate-card{width:100%;display:flex;flex-direction:column;justify-content:space-between;
  gap:9px;flex:1 1 auto;min-height:132px;padding:13px 14px;
  text-align:left;color:inherit;font:inherit;overflow:hidden;
  border:1px solid rgba(255,149,0,.20);
  background:
    radial-gradient(90% 70% at 100% 0%, rgba(255,184,77,.15), transparent 68%),
    linear-gradient(135deg,rgba(255,250,241,.76),rgba(255,255,255,.44))}
.donate-head{display:flex;align-items:center;gap:10px;min-width:0}
.donate-ico{width:38px;height:38px;flex:0 0 38px;border-radius:12px;display:grid;
  place-items:center;background:linear-gradient(160deg,#ffb340,#ff9500);
  box-shadow:0 6px 15px rgba(255,149,0,.25)}
.donate-ico svg{width:20px;height:20px}
.donate-heading{display:flex;flex-direction:column;min-width:0;line-height:1.25}
.donate-heading b{font-size:15px;color:#7a4300;letter-spacing:.1px}
.donate-heading small{margin-top:3px;font-size:9.5px;color:#9a8061;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.donate-info{display:flex;flex-direction:column;gap:6px;padding-top:9px;
  border-top:1px solid rgba(255,149,0,.14);min-width:0}
.donate-row{display:flex;align-items:baseline;gap:9px;font-size:10.5px;
  line-height:1.45;min-width:0}
.donate-row em{flex:0 0 4.7em;font-style:normal;color:#9b8a74}
.donate-row b{color:#3d2c1b;font-weight:600;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.donate-actions{display:flex;gap:6px;margin-top:auto;min-width:0}
.donate-action{flex:1 1 0;display:inline-flex;align-items:center;justify-content:center;
  gap:5px;height:30px;padding:0 7px;border-radius:9px;min-width:0;
  font:inherit;font-size:10.5px;font-weight:600;letter-spacing:.1px;
  text-decoration:none;white-space:nowrap;cursor:pointer;
  transition:transform .14s,filter .14s,background .14s}
.donate-action svg{width:13px;height:13px;flex:0 0 13px}
.donate-action:hover{filter:brightness(1.05)}
.donate-action:active{transform:scale(.97)}
.donate-support{color:#6b3a00;border:1px solid transparent;
  background:linear-gradient(180deg,#ffd087,#ffb440);
  box-shadow:0 4px 11px rgba(255,149,0,.24)}
.donate-github{color:#4b3824;border:1px solid rgba(120,90,50,.16);
  background:rgba(255,255,255,.62)}
body.dark .donate-card{
  background:
    radial-gradient(90% 70% at 100% 0%, rgba(255,159,10,.10), transparent 68%),
    linear-gradient(135deg,rgba(66,48,24,.56),rgba(38,32,24,.32));
  border-color:rgba(255,159,10,.22)}
body.dark .donate-heading b{color:#ffc46b}
body.dark .donate-heading small{color:#b9a88f}
body.dark .donate-info{border-top-color:rgba(255,159,10,.15)}
body.dark .donate-row em{color:#a2937c}
body.dark .donate-row b{color:#f1e2cc}
body.dark .donate-support{color:#3a2600;border-color:transparent;
  background:linear-gradient(180deg,#ffcb78,#ffab2e);
  box-shadow:0 4px 12px rgba(255,149,0,.20)}
body.dark .donate-github{color:#f1e2cc;border-color:rgba(255,255,255,.13);
  background:rgba(255,255,255,.09)}

.topbrand{min-width:0}
.topbrand-text h1{white-space:nowrap}
.status-read{height:34px;padding:0 16px;border-radius:12px;font-size:12.5px;
  white-space:nowrap;flex:0 0 auto}

@media(max-height:820px) and (min-width:1121px){
  .wrap{padding:10px;gap:9px}
  .topbar{padding:8px 12px}
  .statusbar{padding:7px 11px}
  .rail{gap:7px}
  .rail .card{padding:10px 11px}
  .panel-head{margin-bottom:7px}
  .control-card .btnrow .btn{height:40px}
  .control-card .stats{margin-top:6px;gap:5px}
  .control-card .stat{padding:7px 8px}
  .control-card .stat .v{font-size:20px}
  .control-card .switch{margin-top:3px}
  .quickhelp{margin-top:6px;padding-top:6px}
  .quickhelp summary{font-size:10.5px}
  .quickhelp .steps{font-size:9.8px;line-height:1.42}
  .quickhelp .steps li{margin-bottom:4px}
  .quickhelp .note{font-size:9px;line-height:1.4;padding:6px 7px}
  .vip-card .vipdays{font-size:28px}
  .vip-card .vipmeta{font-size:9.5px;gap:7px}
  .vip-card .num span{font-size:9.5px}
  .vip-card .num input{height:27px}
  .donate-card{padding:10px 11px;min-height:112px;gap:7px}
  .donate-ico{width:34px;height:34px;flex-basis:34px}
  .donate-heading b{font-size:14px}
  .donate-info{padding-top:7px;gap:4px}
  .donate-row{font-size:9.8px}
  .donate-action{height:27px;font-size:9.5px}
}

@media(max-height:900px) and (min-width:1121px){
  .wrap{padding:12px;gap:10px}
  .topbar{padding:8px 14px;border-radius:18px}
  .statusbar{padding:7px 13px;border-radius:16px}
  .rail{gap:8px}
  .rail .card{padding:11px 12px}
  .quickhelp .steps{font-size:10.2px;line-height:1.45}
  .quickhelp .steps li{margin-bottom:5px}
  .quickhelp .note{font-size:9.2px;line-height:1.42;padding:6px 7px}
  .vip-card .vipdays{font-size:30px}
  .vip-card .bar{margin:6px 0 5px}
  .vip-card .vipmeta{font-size:10px;gap:8px}
  .vip-card .nums{margin-top:6px}
  .vip-card .num input{height:29px}
  .donate-card{min-height:126px;padding:11px 12px;gap:7px}
  .donate-ico{width:32px;height:32px;flex-basis:32px;border-radius:10px}
  .donate-heading b{font-size:14.5px}
  .donate-actions{gap:5px}
  .donate-action{height:28px;font-size:10px}
}

@media(max-height:720px) and (min-width:1121px){
  .wrap{padding:8px;gap:7px}
  .topbar{padding:6px 10px;border-radius:18px}
  .statusbar{padding:5px 9px;border-radius:16px}
  .rail{gap:5px}
  .rail .card{padding:8px 10px}
  .panel-head{margin-bottom:5px}
  .quickhelp{margin-top:5px;padding-top:5px}
  .quickhelp .steps{font-size:9.3px;line-height:1.35}
  .quickhelp .steps li{margin-bottom:3px}
  .quickhelp .note{font-size:8.5px;line-height:1.35;padding:5px 6px}
  .vip-card .vipdays{font-size:26px}
  .vip-card .vipmeta{font-size:8.8px;gap:6px}
  .vip-card .nums{margin-top:5px}
  .donate-card{padding:8px 9px;min-height:96px;gap:5px}
  .donate-ico{width:28px;height:28px;flex-basis:28px;border-radius:8px}
  .donate-ico svg{width:15px;height:15px}
  .donate-heading b{font-size:13px}
  .donate-heading small{font-size:8.5px}
  .donate-info{padding-top:5px;gap:3px}
  .donate-row{font-size:9px}
  .donate-actions{gap:5px}
  .donate-action{height:24px;padding:0 5px;border-radius:7px;font-size:8.8px}
  .donate-action svg{width:11px;height:11px;flex-basis:11px}
}

@media(max-width:1280px){
  .wrap{grid-template-columns:minmax(286px,312px) minmax(0,1fr) minmax(270px,292px)}
  .vip-card .vipmeta{gap:8px}
  .config-actions{grid-template-columns:1fr 1fr}
}
@media(max-width:1120px){
  body{overflow:auto}
  .wrap{height:auto;min-height:100%;grid-template-columns:1fr;
    grid-template-areas:"top" "status" "workspace" "rail" "settings"}
  .topbar{flex-wrap:wrap}
  .plat-switch{margin-left:0;order:3;width:100%;justify-content:center}
  .statusbar{order:2}
  .status-text b{max-width:62vw}
  .workspace{min-height:70vh;grid-template-columns:minmax(300px,40%) minmax(0,1fr)}
  .rail{display:grid;grid-template-columns:1fr 1fr;overflow:visible;padding-right:0}
  .pv{max-height:none;overflow:visible}
}
@media(max-width:760px){
  .rail{grid-template-columns:1fr}
  .workspace{grid-template-columns:1fr;grid-template-rows:minmax(52vh,auto) 240px}
  .preview-card{border-right:0;border-bottom:1px solid var(--ios-sep);
    border-radius:24px 24px 0 0}
  .logcard{border-left:0;border-radius:0 0 24px 24px}
  .pv{overflow:visible}
}
@media(max-width:560px){
  .wrap{padding:10px;gap:10px}
  .topbar{padding:11px 12px;gap:10px;border-radius:20px}
  .topbrand{min-width:0;flex:1}
  .topbrand .logo{width:38px;height:38px;border-radius:12px}
  .topbrand-text h1{font-size:19px}
  .supportbtn{padding:0 11px}
  .themebtn{padding:0 11px}
  .plat-btn{flex:1;justify-content:center}
  .modal{padding:10px}
  .sheet{padding:20px 16px 16px;border-radius:24px}
  .qr-stage{min-height:244px}
  .qr-img{width:220px;height:220px}
}
</style>
</head>
<body>
<div class="bg"></div>

<div class="wrap">
  <header class="topbar glass">
    <div class="topbrand">
      <div class="logo avatar" title="guyue1337">
        <span>汽</span>
        <img src="/avatar.webp" alt="guyue1337 头像" onerror="this.remove()">
      </div>
      <div class="topbrand-text">
        <h1>汽水音乐自动化</h1>
        <div class="sub">Android + iPhone 双平台控制台</div>
      </div>
    </div>
    <div class="plat-switch" id="platSwitch" role="tablist" aria-label="选择设备平台">
      <button class="plat-btn active" data-platform="android" type="button">
        <span class="plat-ico"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M6.6 8.7 5.0 6.3c-.2-.3.2-.7.5-.4l1.9 2.6C8.8 7.8 10.3 7.4 12 7.4s3.2.4 4.6 1.1l1.9-2.6c.3-.3.7.1.5.4l-1.6 2.4c1.5 1.1 2.5 2.7 2.7 4.6H3.9c.2-1.9 1.2-3.5 2.7-4.6Z"/>
          <circle cx="8.8" cy="9.9" r=".95" fill="#fff"/>
          <circle cx="15.2" cy="9.9" r=".95" fill="#fff"/>
          <path d="M7 14.1h10v4.2c0 1-.8 1.8-1.8 1.8H8.8c-1 0-1.8-.8-1.8-1.8v-4.2Z"/>
          <rect x="4.6" y="14.1" width="1.7" height="5.1" rx=".85"/>
          <rect x="17.7" y="14.1" width="1.7" height="5.1" rx=".85"/>
          <rect x="8.4" y="20.1" width="1.7" height="2.2" rx=".85"/>
          <rect x="13.9" y="20.1" width="1.7" height="2.2" rx=".85"/>
        </svg></span>
        <span class="plat-txt"><b>Android</b><small>USB · ADB</small></span>
      </button>
      <button class="plat-btn" data-platform="ios" type="button">
        <span class="plat-ico"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.81-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z"/>
        </svg></span>
        <span class="plat-txt"><b>iPhone</b><small>iOS 17+ · USB</small></span>
      </button>
    </div>
    <button class="themebtn" id="themebtn">切换暗色</button>
  </header>

  <div class="statusbar glass">
    <div class="status-main">
      <span class="dot" id="dot"></span>
      <div class="status-text">
        <b id="devtxt">正在检测设备…</b>
        <small id="devsub">选择平台后读取设备</small>
      </div>
    </div>
    <button class="btn mini status-read" id="heroRead">读取当前设备</button>
  </div>

  <section class="rail">
    <div class="card glass control-card">
      <div class="panel-head">
        <h2>控制中心</h2>
        <span class="panel-chip" id="runState">待机</span>
      </div>
      <div class="btnrow">
        <button class="btn" id="startBtn">开 始</button>
        <button class="btn stop" id="stopBtn" disabled>停 止</button>
      </div>
      <div class="stats">
        <div class="stat"><div class="v" id="clicks">0</div><div class="k">点击次数</div></div>
        <div class="stat"><div class="v b" id="rounds">0</div><div class="k">看完广告</div></div>
      </div>
      <div class="switchgrid">
        <div class="switch">
          <label>广告静音</label>
          <div class="tog"><input type="checkbox" id="mute"><span></span></div>
        </div>
        <div class="switch">
          <label>允许跳过</label>
          <div class="tog"><input type="checkbox" id="skip"><span></span></div>
        </div>
        <div class="switch">
          <label title="优先识别跳过按钮，减少广告播放期间的无效扫描">广告极速</label>
          <div class="tog"><input type="checkbox" id="fastad"><span></span></div>
        </div>
      </div>
      <details class="quickhelp" open>
        <summary>连接步骤与使用说明</summary>
        <ol class="steps" id="guideSteps"></ol>
        <div class="note" id="guideNote"></div>
      </details>
    </div>

    <div class="card glass vip-card">
      <div class="panel-head">
        <h2>免费听天数</h2>
        <span class="panel-chip soft">停止上限 <b id="vipcap">30</b> 天</span>
      </div>
      <div class="viprow">
        <div class="vipdays" id="vipdays">--</div>
        <div class="vipunit">天 / <b id="vipsrc">读取中</b></div>
      </div>
      <div class="bar"><i id="vipbar"></i></div>
      <div class="vipmeta">
        <span>本次累计 <b id="viptotal">0</b> 条</span>
        <span>按条数估算 <b id="vipest">0</b> 天</span>
        <span>起始 <b id="vipstart">0</b> 天</span>
      </div>
      <div class="vip-tip" id="vipTip">首次使用先读取手机；30 天是停止上限，不是初始天数。</div>
      <div class="nums">
        <div class="num"><span>手动起始</span><input type="number" id="inStart" min="0" max="60" step="1"></div>
        <div class="num"><span>估算比例</span><input type="number" id="inPer" min="1" max="20" step="1"></div>
        <div class="num"><span>自动停止</span><input type="number" id="inCap" min="1" max="365" step="1"></div>
      </div>
    </div>

    <div class="card glass donate-card">
      <span class="donate-head">
        <span class="donate-ico"><svg viewBox="0 0 24 24" fill="none" stroke="#fff"
          stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M4 8h12v6a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5V8Z"/>
          <path d="M16 9h1.8a2.7 2.7 0 0 1 0 5.4H16"/>
          <path d="M7 3.4v1.9M11 3v2.3M15 3.4v1.9"/>
        </svg></span>
        <span class="donate-heading"><b>支持作者</b><small>支持项目持续更新</small></span>
      </span>
      <span class="donate-info">
        <span class="donate-row"><em>作者</em><b>guyue1337</b></span>
      </span>
      <span class="donate-actions">
        <button class="donate-action donate-support" id="supportBtn" type="button"
          title="支持作者（完全自愿，不影响任何功能）">
          <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
            <path d="M8 14s-5.5-3.2-5.5-7A3.4 3.4 0 0 1 8 4a3.4 3.4 0 0 1 5.5 3c0 3.8-5.5 7-5.5 7Z"/>
          </svg>支持作者
        </button>
        <a class="donate-action donate-github" href="https://github.com/guyue1337"
          target="_blank" rel="noopener noreferrer" title="打开 guyue1337 的 GitHub 主页">
          <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
            <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38
              0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53
              .63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95
              0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27
              1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95
              .29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/>
          </svg>GitHub 主页
        </a>
      </span>
    </div>

  </section>

  <section class="workspace glass">
    <section class="preview-card" id="previewCard">
      <div class="preview-head">
        <div class="preview-title">
          <span class="preview-live-dot" aria-hidden="true"></span>
          <h2>手机画面</h2>
        </div>
        <button class="preview-expand" id="previewExpand" type="button">全屏预览</button>
      </div>
      <div class="preview">
        <span class="badge" id="pbadge">预览</span>
        <div class="empty" id="pempty">等待画面…<br>Android 检查 USB 调试，iPhone 检查 USB 信任</div>
        <img id="pimg" style="display:none" alt="手机画面">
      </div>
      <div class="hint">手机亮屏 → 打开汽水音乐 → 进入广告页 → 开始运行</div>
    </section>

    <section class="logcard">
      <div class="loghead">
        <div class="log-title"><span class="log-live-dot" aria-hidden="true"></span><span>运行日志</span></div>
        <span class="log-meta">实时同步</span>
      </div>
      <div class="logbox" id="logbox"></div>
    </section>
  </section>

  <aside class="card glass pv config-panel">
    <div class="panel-head">
      <h2>设备与参数</h2>
      <span class="panel-chip" id="cfgPlatformLabel">Android</span>
    </div>
    <select id="cfPlatform" class="sr-only" aria-hidden="true">
      <option value="android">Android（ADB）</option>
      <option value="ios">iPhone（iOS 27+ 免签 / 17–26 WDA）</option>
    </select>
    <div class="devmeta" id="devmeta">未读取设备信息</div>
    <div class="cfgbtns config-actions">
      <button class="btn mini" id="cfRead">读取当前手机</button>
      <button class="btn mini ghost" id="cfSave">保存参数</button>
    </div>
    <div class="cfmsg" id="cfmsg"></div>
    <div class="cfghint" id="cfgHint">Android：插 USB → 点「读取 Android 手机」→「保存参数」。</div>

    <div class="wda-card" id="wdaCard" role="button" tabindex="0"
      aria-label="打开旧版 iPhone 兼容配置">
      <div class="wda-card-head">
        <span class="wda-apple" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="currentColor">
            <path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.81-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z"/>
          </svg>
        </span>
        <span class="wda-card-title">
          <b id="wdaCardTitle">检查 iPhone 兼容模式</b>
          <small id="wdaCardSub">连接并读取 iPhone 后自动判断</small>
        </span>
        <span class="wda-chip" id="wdaCardChip">检查中</span>
      </div>
      <p class="wda-card-copy" id="wdaCardCopy">
        iOS 27 及以上免签直连；iOS 17–26 可在这里一键安装并启动 WDA。
      </p>
      <button class="wda-card-btn" id="wdaCardBtn" type="button">打开兼容配置</button>
    </div>

    <div class="cfg">
      <div class="cfgrow"><span>机型</span><input id="cfName" placeholder="如 HONOR TFY-AN00"></div>
      <div class="cfg2">
        <div class="cfgrow"><span>宽</span><input id="cfW" type="number" min="320" max="4000"></div>
        <div class="cfgrow"><span>高</span><input id="cfH" type="number" min="480" max="6000"></div>
      </div>
      <div class="cfgrow"><span>OCR宽</span><input id="cfOcr" type="number" min="360" max="1600"></div>
      <div class="cfgrow androidrow"><span>序列号</span><input id="cfSn" placeholder="留空=唯一设备"></div>
      <div class="cfgrow androidrow"><span>ADB</span><input id="cfAdb" placeholder="platform-tools\adb.exe"></div>
      <div class="cfgrow iosrow"><span>WDA 地址</span><input id="cfWda" placeholder="自动使用 http://127.0.0.1:8100"></div>
      <div class="cfgrow iosrow"><span>Bundle</span><input id="cfBundle" placeholder="com.soda.music"></div>
    </div>
  </aside>
</div>

<div class="modal" id="supportModal" role="dialog" aria-modal="true" aria-labelledby="supportTitle">
  <div class="sheet">
    <button class="sheet-close" id="supportClose" type="button" aria-label="关闭">✕</button>
    <div class="support-hero">
      <div class="appmark">汽</div>
      <div class="support-eyebrow" id="supportEyebrow">关于本项目</div>
      <h3 id="supportTitle">关于「汽水音乐自动化」</h3>
      <p id="supportCopy">一个免费、无广告、纯本地运行的汽水音乐自动化工具。<br><b>所有功能永久免费。</b><br>如果它每天帮你省下几分钟，欢迎支持项目。</p>
      <div class="support-free" id="supportFree">✓ 支持完全自愿 · 所有功能永久免费</div>
    </div>
    <div class="support-ask">
      <span class="cup-mini"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M4 8h12v6a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5V8Z"/>
        <path d="M16 9h1.8a2.7 2.7 0 0 1 0 5.4H16"/>
        <path d="M7 3.4v1.9M11 3v2.3M15 3.4v1.9"/>
      </svg></span>
      <span>如果它帮到了你，欢迎支持项目继续更新</span>
    </div>
    <div class="pay-tabs" id="payTabs" role="tablist" aria-label="选择支持方式">
      <button class="pay-tab active" data-kind="wechat" type="button">微信</button>
      <button class="pay-tab" data-kind="alipay" type="button">支付宝</button>
    </div>
    <div class="qr-stage" id="qrStage">
      <img class="qr-img" id="qrImg" alt="支持二维码">
      <div class="qr-empty" id="qrEmpty">
        <div class="big">☕</div>
        <b>暂未配置支持方式</b>
        感谢使用，支持完全自愿<br>不影响任何功能
      </div>
    </div>
    <div class="qr-hint" id="qrHint">微信 / 支付宝扫一扫，感谢支持</div>
    <button class="btn support-continue" id="supportContinue" type="button">继续任务</button>
  </div>
</div>

<div class="modal" id="wdaModal" role="dialog" aria-modal="true" aria-labelledby="wdaTitle">
  <div class="sheet wda-sheet">
    <button class="sheet-close" id="wdaClose" type="button" aria-label="关闭">✕</button>
    <div class="wda-hero">
      <span class="wda-apple" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="currentColor">
          <path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.81-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z"/>
        </svg>
      </span>
      <div>
        <h3 id="wdaTitle">旧版 iPhone 兼容配置</h3>
        <p>适用 iOS 17.0–26.x。向导会自动准备工具、签名安装并启动 WDA。</p>
      </div>
    </div>

    <div class="wda-warning">
      <span>ⓘ</span>
      <div>免费 Apple ID 签名通常只有 <b>约 7 天有效期</b>，这不是永久免签。
        到期后回到这里点「安装 / 续签 WDA」即可；iOS 27 及以上无需此步骤。</div>
    </div>

    <ol class="wda-steps">
      <li>打开登录入口，在 Impactor 窗口登录 Apple ID</li>
      <li>回到这里点「安装 / 续签 WDA」并等待完成</li>
      <li>点「启动 WDA」，看到已就绪后返回开始任务</li>
    </ol>

    <div class="wda-status">
      <div class="item"><div class="k">iPhone 系统</div><div class="v" id="wdaVersion">--</div></div>
      <div class="item"><div class="k">Apple ID</div><div class="v" id="wdaAccount">未登录</div></div>
      <div class="item"><div class="k">WDA 安装</div><div class="v" id="wdaInstalled">未安装</div></div>
      <div class="item"><div class="k">WDA 运行</div><div class="v" id="wdaRunning">未启动</div></div>
      <div class="item"><div class="k">签名剩余</div><div class="v" id="wdaDays">--</div></div>
      <div class="item"><div class="k">当前步骤</div><div class="v" id="wdaStep">等待检查</div></div>
    </div>

    <div class="wda-progress" id="wdaProgress"><i id="wdaProgressBar"></i></div>

    <div class="wda-actions">
      <button class="btn primary" id="wdaInstall" type="button">安装 / 续签 WDA</button>
      <button class="btn ghost" id="wdaLogin" type="button">打开 Apple ID 登录入口</button>
      <button class="btn ghost" id="wdaStart" type="button">启动 WDA</button>
      <button class="btn ghost" id="wdaCheck" type="button">检查状态</button>
      <button class="btn ghost" id="wdaStop" type="button">停止 WDA</button>
    </div>

    <div class="wda-log" id="wdaLog"></div>
    <div class="wda-foot">
      <small>密码只在 Impactor 窗口输入，本程序不读取、不保存 Apple ID 密码。</small>
      <button class="wda-logout" id="wdaLogout" type="button">清除本机登录</button>
    </div>
  </div>
</div>

<script>
let since = 0;
let currentPlatform = 'android';
let statusBusy = false;
let lastRunning = null;
let manualStopPending = false;
let wdaStatus = {};
let wdaFetchBusy = false;
let wdaFetchTs = 0;
let wdaRequestBusy = false;
let wdaLogSince = 0;
let wdaLogBusy = false;
let wdaPromptShown = false;
const wdaCard = document.getElementById('wdaCard');
const wdaModal = document.getElementById('wdaModal');
const wdaLog = document.getElementById('wdaLog');

async function j(url, opt){
  let r;
  try{
    r = await fetch(url, opt);
  }catch(e){
    throw new Error('控制台连接已断开：请保持黑色启动窗口打开，再刷新本页或重新运行一键启动');
  }
  let data = null;
  try{ data = await r.json(); }catch(e){}
  if(!r.ok) throw new Error((data && data.error) || ('HTTP ' + r.status));
  return data;
}

async function refresh(){
  if(statusBusy) return;
  statusBusy = true;
  try{
    const s = await j('/api/status');
    const wasRunning = lastRunning;
    lastRunning = !!s.running;
    document.getElementById('dot').className = 'dot ' + (s.running ? 'run' : (s.device.connected ? 'on' : ''));
    const devtxt = document.getElementById('devtxt');
    const devsub = document.getElementById('devsub');
    if(s.device.connected){
      devtxt.textContent = (s.device.name || s.device.model) + '  ' + s.device.w + '×' + s.device.h;
      devsub.textContent = s.device.platform === 'ios'
        ? ('iPhone · iOS ' + (s.device.ios || '?')
           + ' · ' + (s.device.driver || (s.device.ready ? 'USB 直连' : 'USB'))
           + (s.device.ready || s.device.needs_wda ? '' : ' · 点开始自动准备'))
        : 'Android ' + (s.device.android || '') + ' · ADB';
    }else if(s.device.state === 'unauthorized'){
      devtxt.textContent = '设备未授权';
      devsub.textContent = '请在手机上点「允许 USB 调试」';
    }else if(s.device.state === 'offline'){
      devtxt.textContent = '设备离线';
      devsub.textContent = '请重新插拔数据线';
    }else{
      devtxt.textContent = '未连接设备';
      devsub.textContent = s.device.error || '连接设备后点「读取当前设备」';
    }
    devtxt.title = s.device.error || '';
    devsub.title = s.device.error || '';
    document.getElementById('clicks').textContent = s.clicks;
    document.getElementById('rounds').textContent = s.rounds;
    const startBtn = document.getElementById('startBtn');
    startBtn.disabled = !!s.running;
    startBtn.title = s.device.platform === 'ios' && s.device.needs_wda
      ? '点开始后会自动打开 iOS 17–26 兼容向导'
      : '';
    document.getElementById('stopBtn').disabled = !s.running;
    document.getElementById('pbadge').textContent = s.running ? '实时' : '预览';
    document.getElementById('pbadge').className = 'badge' + (s.running ? ' live' : '');
    const runState = document.getElementById('runState');
    runState.textContent = s.running ? '运行中' : (s.device.connected ? '已连接' : '待机');
    runState.className = 'panel-chip' + (s.running ? ' run' : ' soft');
    document.getElementById('previewCard').classList.toggle('is-live', !!s.running);
    document.getElementById('mute').checked = s.settings.mute;
    document.getElementById('skip').checked = s.settings.skip;
    document.getElementById('fastad').checked = s.settings.fast_ad;

    const v = s.vip;
    if(wasRunning === true && !s.running){
      if(manualStopPending){
        manualStopPending = false;
      }else if(v.cap > 0 && v.real_days >= v.cap){
        openSupport('limit');
      }
    }
    const hasReading = v.real_days >= 0;
    const hasFallback = hasReading || v.total > 0 || v.start_days > 0;
    document.getElementById('vipdays').textContent = hasFallback ? v.days : '--';
    document.getElementById('vipcap').textContent = v.cap;
    document.getElementById('viptotal').textContent = v.total;
    document.getElementById('vipest').textContent = v.estimate;
    document.getElementById('vipstart').textContent = v.start_days;
    document.getElementById('vipsrc').textContent = hasReading ? '手机实测'
      : (v.total > 0 ? '估算' : '待读取');
    document.getElementById('vipTip').style.display = hasReading ? 'none' : 'block';
    document.getElementById('vipbar').style.width =
      (hasFallback ? Math.min(100, v.days / v.cap * 100) : 0) + '%';
    if(document.activeElement !== document.getElementById('inStart'))
      document.getElementById('inStart').value = v.start_days;
    if(document.activeElement !== document.getElementById('inPer'))
      document.getElementById('inPer').value = v.per;
    if(document.activeElement !== document.getElementById('inCap'))
      document.getElementById('inCap').value = v.cap;
    if(s.wda) renderWdaCard(s.wda);
    if(s.wda_prompt && !wdaPromptShown){
      wdaPromptShown = true;
      openWda();
    }else if(!s.wda_prompt){
      wdaPromptShown = false;
    }
    if(currentPlatform === 'ios' && Date.now() - wdaFetchTs > 4500) refreshWda();
  }catch(e){}finally{ statusBusy = false; }
}

async function pullLog(){
  try{
    const d = await j('/api/log?since=' + since);
    if(d.items && d.items.length){
      const box = document.getElementById('logbox');
      const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
      for(const it of d.items){
        const div = document.createElement('div');
        div.className = 'ln ' + it[1];
        div.innerHTML = '<span class="t">' + it[0] + '</span><span class="m"></span>';
        div.querySelector('.m').textContent = it[2];
        box.appendChild(div);
      }
      while(box.childElementCount > 400) box.removeChild(box.firstChild);
      if(stick) box.scrollTop = box.scrollHeight;
    }
    since = d.total;
  }catch(e){}
}

let frameBusy = false;
async function pullFrame(){
  if(frameBusy) return;
  if(!document.getElementById('devtxt').textContent.includes('×')) return;
  frameBusy = true;
  const img = document.getElementById('pimg');
  const probe = new Image();
  probe.onload = () => { img.src = probe.src; img.style.display='block';
                         document.getElementById('pempty').style.display='none'; frameBusy=false; };
  probe.onerror = () => { frameBusy = false; };
  probe.src = '/api/frame?t=' + Date.now();
}

const previewCard = document.getElementById('previewCard');
const previewExpand = document.getElementById('previewExpand');
function setPreviewZoom(on){
  previewCard.classList.toggle('zoomed', !!on);
  previewExpand.textContent = on ? '退出全屏' : '全屏预览';
}
previewExpand.onclick = () => setPreviewZoom(!previewCard.classList.contains('zoomed'));

document.getElementById('startBtn').onclick = async () => {
  try{
    manualStopPending = false;
    const d = await j('/api/start', {method:'POST'});
    await refresh();
    if(d && d.needs_wda_setup){
      cfgMsg('iOS 17–26 需要先完成 WDA 配置，已打开兼容向导', 'warn');
      openWda();
      startWdaPolling();
    }else if(d && d.running){
      openSupport('start');
    }else{
      cfgMsg('任务未启动：请先连接并读取手机', 'error');
    }
  }catch(e){
    cfgMsg(e.message || '启动失败', 'error');
  }
};
document.getElementById('stopBtn').onclick = async () => {
  try{
    manualStopPending = true;
    await j('/api/stop', {method:'POST'});
    await refresh();
    openSupport('stop');
  }catch(e){
    manualStopPending = false;
    cfgMsg(e.message || '停止失败', 'error');
  }
};
document.getElementById('mute').onchange = e => j('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({mute:e.target.checked})});
document.getElementById('skip').onchange = e => j('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({skip:e.target.checked})});
document.getElementById('fastad').onchange = e => j('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({fast_ad:e.target.checked})});

function pushVip(){
  const body = {
    start_days: +document.getElementById('inStart').value || 0,
    per: Math.max(1, +document.getElementById('inPer').value || 3),
    cap: Math.max(1, +document.getElementById('inCap').value || 30),
  };
  j('/api/vip', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)}).then(refresh);
}
['inStart','inPer','inCap'].forEach(id => document.getElementById(id).onchange = pushVip);

refresh(); pullLog(); pullFrame();
setInterval(refresh, 800);
setInterval(pullLog, 400);
setInterval(pullFrame, 900);
setInterval(pollWda, 1200);
/* ================= 机型参数 ================= */
function setPlatformUI(platform){
  currentPlatform = platform === 'ios' ? 'ios' : 'android';
  const ios = currentPlatform === 'ios';
  document.getElementById('cfPlatform').value = currentPlatform;
  document.querySelectorAll('.plat-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.platform === currentPlatform);
  });
  document.querySelectorAll('.androidrow').forEach(el => el.style.display = ios ? 'none' : 'flex');
  document.querySelectorAll('.iosrow').forEach(el => el.style.display = ios ? 'flex' : 'none');
  const guideSteps = ios ? [
    'USB 连接 iPhone，解锁并信任电脑',
    '开启「设置 → 隐私与安全性 → 开发者模式」',
    '顶部选 iPhone，点「读取 iPhone」确认型号与系统',
    'iOS 27+ 直接开始；iOS 17–26 按右侧兼容卡片配置一次 WDA'
  ] : [
    '开启「开发者选项 → USB 调试」',
    'USB 连接电脑，在手机弹窗点「允许调试」',
    '顶部选 Android，点「读取 Android 手机」',
    '打开汽水音乐广告页，点「开始」'
  ];
  const guideNote = ios
    ? 'iOS 27.0+ 免签直连；iOS 17.0–26.x 首次需登录免费 Apple ID 安装 WDA，签名约 7 天后到期续签。'
    : '荣耀等机型若无授权弹窗，请重插数据线，并把 USB 用途切为「传输文件」。';
  document.getElementById('guideSteps').innerHTML = guideSteps.map(s => '<li>' + s + '</li>').join('');
  document.getElementById('guideNote').textContent = guideNote;
  if(ios){
    const wda = document.getElementById('cfWda');
    const bundle = document.getElementById('cfBundle');
    if(!wda.value) wda.value = 'http://127.0.0.1:8100';
    if(!bundle.value) bundle.value = 'com.soda.music';
    wdaCard.classList.add('show');
    refreshWda();
  }else{
    wdaCard.classList.remove('show');
  }
  document.getElementById('cfgHint').textContent = ios
    ? 'iPhone：iOS 27+ 免签直连；iOS 17–26.x 点兼容卡片，一键安装并启动 WDA。'
    : 'Android：插 USB → 点「读取 Android 手机」→「保存参数」，坐标按比例算。';
  document.getElementById('cfgPlatformLabel').textContent = ios ? 'iPhone · USB' : 'Android · ADB';
  document.getElementById('cfRead').textContent = ios ? '读取 iPhone' : '读取 Android 手机';
  document.getElementById('heroRead').textContent = ios ? '读取 iPhone' : '读取 Android 手机';
  const mute = document.getElementById('mute');
  mute.disabled = ios;
  const muteRow = mute.closest('.switch');
  if(muteRow){
    muteRow.style.opacity = ios ? '.45' : '1';
    muteRow.title = ios ? 'iOS 需要手动调节手机音量' : '';
  }
}
async function loadProfile(){
  try{
    const p = await j('/api/profile');
    document.getElementById('cfPlatform').value = p.platform || 'android';
    document.getElementById('cfName').value = p.name || '';
    document.getElementById('cfW').value    = p.width  || '';
    document.getElementById('cfH').value    = p.height || '';
    document.getElementById('cfOcr').value  = p.ocr_width || 720;
    document.getElementById('cfAdb').value  = p.adb || '';
    document.getElementById('cfSn').value   = p.serial || '';
    document.getElementById('cfWda').value  = p.ios_wda || '';
    document.getElementById('cfBundle').value = p.ios_bundle || '';
    setPlatformUI(p.platform || 'android');
    return p;
  }catch(e){ return null; }
}
function profileBody(){
  return {
    platform: document.getElementById('cfPlatform').value,
    name: document.getElementById('cfName').value.trim(),
    width: parseInt(document.getElementById('cfW').value)||0,
    height: parseInt(document.getElementById('cfH').value)||0,
    ocr_width: parseInt(document.getElementById('cfOcr').value)||720,
    adb: document.getElementById('cfAdb').value.trim(),
    serial: document.getElementById('cfSn').value.trim(),
    ios_wda: document.getElementById('cfWda').value.trim(),
    ios_bundle: document.getElementById('cfBundle').value.trim()
  };
}
function cfgMsg(text, level){
  const el = document.getElementById('cfmsg');
  el.textContent = text || '';
  el.className = 'cfmsg' + (level ? ' ' + level : '');
}
function deviceLine(p){
  if(!p || !p.connected) return (p && p.error) ? p.error : '未读取设备信息';
  const mask = s => {
    s = String(s || '');
    return s.length > 4 ? '****' + s.slice(-4) : s;
  };
  const lines = [];
  if(p.platform === 'ios' || p.ios){
    lines.push((p.name || p.model || 'iPhone') + ' · iOS ' + (p.ios || '?'));
    const second = [];
    if(p.width && p.height) second.push(p.width + '×' + p.height);
    if(p.points_width && p.points_height &&
       (p.points_width !== p.width || p.points_height !== p.height))
      second.push('逻辑 ' + p.points_width + '×' + p.points_height);
    if(p.scale) second.push(p.scale + 'x');
    if(p.build) second.push('Build ' + p.build);
    if(second.length) lines.push(second.join(' · '));
    const third = [];
    if(p.model && p.model !== p.name) third.push(p.model);
    if(p.driver) third.push(p.driver);
    if(p.developer_mode === false) third.push('开发者模式未开启');
    if(p.serial) third.push('UDID ' + mask(p.serial));
    if(third.length) lines.push(third.join(' · '));
  }else{
    lines.push((p.name || p.model || 'Android 设备') + ' · Android ' + (p.android || '?'));
    const second = [];
    if(p.width && p.height) second.push(p.width + '×' + p.height);
    if(p.sdk) second.push('SDK ' + p.sdk);
    if(p.build) second.push('Build ' + p.build);
    if(second.length) lines.push(second.join(' · '));
    const third = [];
    if(p.model && p.model !== p.name) third.push(p.model);
    if(p.density) third.push('DPI ' + p.density);
    if(p.serial) third.push('序列号 ' + mask(p.serial));
    if(third.length) lines.push(third.join(' · '));
  }
  return lines.join('\n');
}
async function readDevice(auto){
  const btn = document.getElementById('cfRead');
  const old = btn.textContent;
  if(!auto){ btn.disabled = true; btn.textContent = '读取中…'; cfgMsg('正在读取手机信息…'); }
  try{
    const qs = new URLSearchParams({
      read: '1',
      platform: document.getElementById('cfPlatform').value,
      wda: document.getElementById('cfWda').value.trim(),
      bundle: document.getElementById('cfBundle').value.trim()
    });
    const p = await j('/api/profile?' + qs.toString());
    if(!p || !p.connected) throw new Error((p && p.error) || '未检测到已授权设备');
    document.getElementById('cfName').value = p.name || p.model || '';
    document.getElementById('cfW').value = p.width || '';
    document.getElementById('cfH').value = p.height || '';
    if(p.platform === 'ios'){
      if(p.wda) document.getElementById('cfWda').value = p.wda;
      if(p.bundle) document.getElementById('cfBundle').value = p.bundle;
      if(p.serial) document.getElementById('cfSn').value = p.serial;
      setPlatformUI('ios');
    }
    const meta = document.getElementById('devmeta');
    meta.textContent = deviceLine(p);
    meta.title = meta.textContent;
    cfgMsg(auto ? '设备信息已自动刷新' : '已读取；换机或修改后请点「保存参数」', 'ok');
    refresh();
  }catch(e){
    document.getElementById('devmeta').textContent = '读取失败';
    document.getElementById('devmeta').title = '';
    cfgMsg(e.message || '读取失败', 'error');
  }finally{
    if(!auto){ btn.disabled = false; btn.textContent = old; }
  }
}
document.getElementById('cfSave').onclick = async () => {
  try{
    await j('/api/profile', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(profileBody())});
    await loadProfile(); cfgMsg('参数已保存', 'ok'); refresh();
  }catch(e){ cfgMsg(e.message || '保存失败', 'error'); }
};
document.getElementById('cfRead').onclick = () => readDevice(false);
document.getElementById('heroRead').onclick = () => readDevice(false);
async function choosePlatform(platform){
  const changed = platform !== currentPlatform;
  setPlatformUI(platform);
  if(!changed) return;
  cfgMsg(platform === 'ios' ? '正在切换到 iPhone…' : '正在切换到 Android…');
  try{
    const d = await j('/api/profile', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        select_only: true,
        platform: platform,
        ios_wda: document.getElementById('cfWda').value.trim(),
        ios_bundle: document.getElementById('cfBundle').value.trim()
      })
    });
    const p = d.profile || {};
    document.getElementById('cfName').value = p.name || '';
    document.getElementById('cfW').value = p.width || '';
    document.getElementById('cfH').value = p.height || '';
    document.getElementById('cfOcr').value = p.ocr_width || 720;
    document.getElementById('cfAdb').value = p.adb || '';
    document.getElementById('cfSn').value = p.serial || '';
    document.getElementById('cfWda').value = p.ios_wda || 'http://127.0.0.1:8100';
    document.getElementById('cfBundle').value = p.ios_bundle || 'com.soda.music';
    document.getElementById('devmeta').textContent = '未读取设备信息';
    document.getElementById('devmeta').title = '';
    cfgMsg(platform === 'ios'
      ? '已切换到 iPhone，正在读取 USB 设备…'
      : '已切换到 Android，正在读取 USB 设备…');
    await readDevice(true);
  }catch(e){ cfgMsg(e.message || '平台切换失败', 'error'); }
}
document.querySelectorAll('.plat-btn').forEach(btn => {
  btn.addEventListener('click', () => choosePlatform(btn.dataset.platform));
});
loadProfile().then(p => {
  if(p && (p.width || p.height || p.serial || (p.platform === 'ios' && p.ios_wda)))
    setTimeout(() => readDevice(true), 650);
});
/* ================= 机型参数结束 ================= */
/* ================= 旧版 iPhone WDA ================= */
const wdaStateLabels = {
  ready:['已就绪','ok'],
  expired:['需续签','err'],
  no_account:['需登录','warn'],
  not_installed:['需安装','warn'],
  stopped:['待启动',''],
  unsupported:['不支持','err'],
  coredevice:['免签直连','ok'],
  unknown:['待读取','']
};

function wdaStateView(st){
  st = st || {};
  const item = wdaStateLabels[st.state] || ['待检查',''];
  return {label:item[0], level:item[1]};
}

function renderWdaCard(st){
  if(!st || typeof st !== 'object') return;
  wdaStatus = st;
  const ios = currentPlatform === 'ios';
  wdaCard.classList.toggle('show', ios);
  if(!ios) return;

  const mode = st.mode || 'unknown';
  const view = wdaStateView(st);
  wdaCard.classList.toggle('coredevice', mode === 'coredevice');
  const title = document.getElementById('wdaCardTitle');
  const sub = document.getElementById('wdaCardSub');
  const copy = document.getElementById('wdaCardCopy');
  const chip = document.getElementById('wdaCardChip');
  const btn = document.getElementById('wdaCardBtn');

  if(mode === 'coredevice'){
    title.textContent = 'iPhone 免签直连';
    sub.textContent = 'iOS ' + (st.ios || '27+') + ' · 无需安装 WDA';
    copy.textContent = '这台 iPhone 可直接开始任务，不需要 Apple ID、证书或续签。';
    chip.textContent = '免签直连';
    chip.className = 'wda-chip ok';
    btn.textContent = '无需配置';
    return;
  }
  if(mode === 'unsupported'){
    title.textContent = 'iPhone 系统版本过低';
    sub.textContent = 'iOS ' + (st.ios || '?') + ' · 未纳入当前兼容范围';
    copy.textContent = '当前版本支持 iOS 17.0 及以上；请先升级 iPhone 系统。';
    chip.textContent = '不支持';
    chip.className = 'wda-chip err';
    btn.textContent = '查看兼容说明';
    return;
  }
  if(mode === 'wda'){
    title.textContent = '旧版 iPhone 兼容通道';
    sub.textContent = 'iOS ' + (st.ios || '17–26') + ' · 免费签名约 7 天';
    copy.textContent = st.busy
      ? (st.step || '正在处理 WDA，请稍候…')
      : (st.message || '点开后按向导安装并启动 WDA。');
    chip.textContent = st.busy ? '处理中' : view.label;
    chip.className = 'wda-chip ' + (st.busy ? 'warn' : view.level);
    btn.textContent = st.running ? '查看 WDA 状态' : '打开 WDA 配置';
    return;
  }

  title.textContent = 'iPhone 兼容模式';
  sub.textContent = st.ios ? ('iOS ' + st.ios + ' · 等待检测') : '连接并读取 iPhone 后自动判断';
  copy.textContent = st.message || 'iOS 27+ 免签直连；iOS 17–26 可在这里一键安装 WDA。';
  chip.textContent = view.label;
  chip.className = 'wda-chip ' + view.level;
  btn.textContent = '打开兼容配置';
}

function renderWdaModal(st){
  if(!st || typeof st !== 'object') return;
  const locked = !!st.busy || wdaRequestBusy;
  const mode = st.mode || 'unknown';
  const view = wdaStateView(st);
  const account = st.account_logged_in
    ? ('已登录 ' + (st.account_masked || '')) : '未登录';
  document.getElementById('wdaVersion').textContent =
    st.ios ? ('iOS ' + st.ios) : '未读取';
  document.getElementById('wdaAccount').textContent = account;
  document.getElementById('wdaInstalled').textContent =
    st.installed ? '已安装' : (mode === 'coredevice' ? '无需安装' : '未安装');
  document.getElementById('wdaRunning').textContent =
    st.running ? '已就绪' : '未启动';
  document.getElementById('wdaInstalled').title = st.bundle_id || '';
  let days = '尚未安装';
  if(mode === 'coredevice') days = '无需签名';
  else if(st.needs_resign) days = '已过期';
  else if(st.days_left !== null && st.days_left !== undefined) days = '约 ' + st.days_left + ' 天';
  else if(st.installed) days = '待检测';
  document.getElementById('wdaDays').textContent = days;
  document.getElementById('wdaDays').title = st.expires_at || '';
  document.getElementById('wdaStep').textContent =
    locked ? (st.step || '处理中…') : view.label;
  document.getElementById('wdaStep').title = st.message || st.error || '';

  const bar = document.getElementById('wdaProgress');
  const fill = document.getElementById('wdaProgressBar');
  const progress = (typeof st.progress === 'number') ? st.progress : null;
  bar.classList.toggle('show', !!st.busy || progress !== null);
  fill.style.width = Math.max(0, Math.min(100, progress === null ? 0 : progress)) + '%';

  const install = document.getElementById('wdaInstall');
  install.textContent = st.needs_resign ? '续签 WDA'
    : (st.installed ? '重新安装 / 续签 WDA' : '安装 / 续签 WDA');
  install.disabled = locked || mode === 'coredevice' || mode === 'unsupported';
  document.getElementById('wdaLogin').disabled = locked || mode === 'coredevice';
  document.getElementById('wdaCheck').disabled = locked;
  document.getElementById('wdaStart').disabled =
    locked || mode !== 'wda' || !st.installed || !!st.running || !!st.needs_resign;
  document.getElementById('wdaStop').disabled = locked || mode !== 'wda' || !st.running;
  document.getElementById('wdaLogout').disabled = locked;
}

async function refreshWda(force){
  if(wdaFetchBusy) return;
  if(!force && Date.now() - wdaFetchTs < 1600) return;
  wdaFetchBusy = true;
  wdaFetchTs = Date.now();
  try{
    const st = await j('/api/wda/status');
    renderWdaCard(st);
    renderWdaModal(st);
  }catch(e){
    if(wdaModal.classList.contains('show'))
      wdaLogLine('读取 WDA 状态失败：' + (e.message || '未知错误'), 'error');
  }finally{
    wdaFetchBusy = false;
  }
}

function wdaLogLine(text, level){
  if(!wdaLog) return;
  const stick = wdaLog.scrollHeight - wdaLog.scrollTop - wdaLog.clientHeight < 45;
  const line = document.createElement('div');
  if(level) line.className = level;
  line.textContent = '[' + new Date().toLocaleTimeString('zh-CN', {hour12:false}) + '] ' + text;
  wdaLog.appendChild(line);
  while(wdaLog.childElementCount > 240) wdaLog.removeChild(wdaLog.firstChild);
  if(stick) wdaLog.scrollTop = wdaLog.scrollHeight;
}

async function pullWdaLog(){
  if(wdaLogBusy || !wdaModal.classList.contains('show')) return;
  wdaLogBusy = true;
  try{
    const d = await j('/api/wda/log?since=' + wdaLogSince);
    if(d.items && d.items.length){
      for(const it of d.items) wdaLogLine(it[2], it[1]);
    }
    wdaLogSince = d.total;
  }catch(e){
  }finally{
    wdaLogBusy = false;
  }
}

function startWdaPolling(){
  wdaLogSince = 0;
  if(wdaLog) wdaLog.innerHTML = '';
  refreshWda(true);
  pullWdaLog();
}

function openWda(){
  if(wdaStatus.mode === 'coredevice'){
    cfgMsg('这台 iPhone 使用 iOS 27+ 免签直连，不需要安装 WDA', 'ok');
    return;
  }
  const support = document.getElementById('supportModal');
  if(support && support.classList.contains('show')) closeSupport();
  wdaModal.classList.add('show');
  document.body.style.overflow = 'hidden';
  renderWdaModal(wdaStatus);
  startWdaPolling();
}

function closeWda(){
  wdaModal.classList.remove('show');
  const support = document.getElementById('supportModal');
  if(!support || !support.classList.contains('show'))
    document.body.style.overflow = '';
}

function pollWda(){
  if(!wdaModal.classList.contains('show')) return;
  refreshWda(false);
  pullWdaLog();
}

async function wdaAction(action){
  if(wdaRequestBusy) return;
  wdaRequestBusy = true;
  renderWdaModal(wdaStatus);
  try{
    const d = await j('/api/wda/action', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action})
    });
    if(d.status){
      wdaStatus = d.status;
      renderWdaCard(d.status);
      renderWdaModal(d.status);
    }
    if(action === 'check'){
      cfgMsg('WDA 状态已刷新', 'ok');
    }else if(action === 'login'){
      cfgMsg('已打开 Impactor；登录完成后回到这里点「检查状态」', 'ok');
    }else if(action === 'stop'){
      cfgMsg('WDA 已停止', 'ok');
    }else if(action === 'logout'){
      cfgMsg('本机 Apple ID 登录信息已清除', 'ok');
    }
    setTimeout(() => refreshWda(true), 350);
  }catch(e){
    wdaLogLine(e.message || 'WDA 操作失败', 'error');
    cfgMsg(e.message || 'WDA 操作失败', 'error');
    setTimeout(() => refreshWda(true), 500);
  }finally{
    wdaRequestBusy = false;
    renderWdaModal(wdaStatus);
  }
}

wdaCard.addEventListener('click', openWda);
wdaCard.addEventListener('keydown', e => {
  if(e.key === 'Enter' || e.key === ' '){
    e.preventDefault();
    openWda();
  }
});
document.getElementById('wdaClose').onclick = closeWda;
document.getElementById('wdaInstall').onclick = () => wdaAction('install');
document.getElementById('wdaLogin').onclick = () => wdaAction('login');
document.getElementById('wdaStart').onclick = () => wdaAction('start');
document.getElementById('wdaCheck').onclick = () => wdaAction('check');
document.getElementById('wdaStop').onclick = () => wdaAction('stop');
document.getElementById('wdaLogout').onclick = () => {
  if(confirm('确定清除保存在本机的 Apple ID 登录信息？')) wdaAction('logout');
};
wdaModal.addEventListener('click', e => {
  if(e.target === wdaModal) closeWda();
});
/* ================= 旧版 iPhone WDA 结束 ================= */
/* ================= 支持 ================= */
const supportModal = document.getElementById('supportModal');
const qrStage = document.getElementById('qrStage');
const qrImg = document.getElementById('qrImg');
const qrEmpty = document.getElementById('qrEmpty');
const qrHint = document.getElementById('qrHint');
let rewardKind = 'wechat';
let rewardSlots = {wechat:{exists:false,version:0}, alipay:{exists:false,version:0}};

function setRewardHint(text, level){
  qrHint.textContent = text || '';
  qrHint.className = 'qr-hint' + (level ? ' ' + level : '');
}

function renderReward(){
  const slot = rewardSlots[rewardKind] || {exists:false, version:0};
  if(slot.exists){
    qrImg.onload = () => {
      qrImg.style.display = 'block';
      qrEmpty.style.display = 'none';
      qrStage.classList.add('hasimg');
    };
    qrImg.onerror = () => {
      qrImg.style.display = 'none';
      qrEmpty.style.display = 'block';
      qrStage.classList.remove('hasimg');
    };
    qrImg.src = '/api/support/qr?type=' + rewardKind + '&v=' + slot.version + '&t=' + Date.now();
  }else{
    qrImg.removeAttribute('src');
    qrImg.style.display = 'none';
    qrEmpty.style.display = 'block';
    qrStage.classList.remove('hasimg');
  }
}

async function loadReward(){
  try{
    const d = await j('/api/support');
    if(d && d.slots) rewardSlots = d.slots;
    renderReward();
  }catch(e){
    setRewardHint('读取支持状态失败：' + (e.message || '未知错误'), 'error');
  }
}

const supportModes = {
  start: {
    eyebrow: '任务已开始运行',
    accent: true,
    copy: '任务已经在后台开始运行，可以继续做别的事了。<br><b>支持完全自愿，所有功能都不受影响。</b><br>如果它帮到了你，欢迎支持项目继续更新。',
    action: '继续任务'
  },
  stop: {
    eyebrow: '任务已停止',
    accent: false,
    copy: '任务已经停止，广告不会再自动点击。<br><b>支持完全自愿，不影响之后继续使用。</b><br>如果它帮你省下了时间，欢迎支持项目继续更新。',
    action: '关闭'
  },
  limit: {
    eyebrow: '已达到免费听上限',
    accent: true,
    copy: '免费听天数已经达到你设置的上限，本次挂机已自动停止。<br><b>支持完全自愿，所有功能永久免费。</b><br>如果它帮你省下了时间，欢迎支持项目继续更新。',
    action: '完成'
  },
  about: {
    eyebrow: '关于本项目',
    accent: false,
    copy: '一个免费、无广告、纯本地运行的汽水音乐自动化工具。<br><b>所有功能永久免费。</b><br>如果它每天帮你省下几分钟，欢迎支持项目。',
    action: '关闭'
  }
};

function openSupport(mode){
  const cfg = supportModes[mode] || supportModes.about;
  supportModal.classList.add('show');
  document.body.style.overflow = 'hidden';
  const eyebrow = document.getElementById('supportEyebrow');
  eyebrow.textContent = cfg.eyebrow;
  eyebrow.className = 'support-eyebrow' + (cfg.accent ? ' run' : '');
  document.getElementById('supportCopy').innerHTML = cfg.copy;
  document.getElementById('supportContinue').textContent = cfg.action;
  setRewardHint('微信 / 支付宝扫一扫，感谢支持');
  loadReward();
}
function closeSupport(){
  supportModal.classList.remove('show');
  if(!wdaModal.classList.contains('show'))
    document.body.style.overflow = '';
}
document.getElementById('supportBtn').onclick = () => openSupport('about');
document.getElementById('supportContinue').onclick = closeSupport;
document.getElementById('supportClose').onclick = closeSupport;
supportModal.addEventListener('click', e => { if(e.target === supportModal) closeSupport(); });
document.addEventListener('keydown', e => {
  if(e.key !== 'Escape') return;
  if(wdaModal.classList.contains('show')) closeWda();
  else if(supportModal.classList.contains('show')) closeSupport();
  else if(previewCard.classList.contains('zoomed')) setPreviewZoom(false);
});

document.getElementById('payTabs').addEventListener('click', e => {
  const btn = e.target.closest('.pay-tab');
  if(!btn) return;
  rewardKind = btn.dataset.kind === 'alipay' ? 'alipay' : 'wechat';
  document.querySelectorAll('.pay-tab').forEach(b => {
    const on = b.dataset.kind === rewardKind;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  setRewardHint((rewardKind === 'wechat' ? '微信' : '支付宝') + '扫一扫，感谢支持');
  renderReward();
});

/* ================= 支持结束 ================= */
/* ================= 明暗主题 ================= */
function applyTheme(t){
  document.body.classList.toggle('dark', t === 'dark');
  const b = document.getElementById('themebtn');
  if (b) b.textContent = (t === 'dark') ? '切换亮色' : '切换暗色';
  try{ localStorage.setItem('qishui_theme2', t); }catch(e){}
}
document.getElementById('themebtn').onclick = () => {
  applyTheme(document.body.classList.contains('dark') ? 'light' : 'dark');
};
let savedTheme = 'light';
try{ savedTheme = localStorage.getItem('qishui_theme2') || 'light'; }catch(e){}
applyTheme(savedTheme);
/* ================= 明暗主题结束 ================= */
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/avatar.webp":
            try:
                with open(AVATAR_PATH, "rb") as f:
                    return self._send(200, f.read(), "image/webp")
            except OSError:
                return self._send(404, b"", "image/webp")

        if path == "/api/status":
            if not S.running and time.time() - S.device_ts > 2.0:
                detect_device()
            return self._send(200, json.dumps({
                "running": S.running,
                "clicks": S.clicks,
                "rounds": S.rounds,
                "device": S.device,
                "wda_prompt": bool(S.wda_prompt),
                "wda": WDA.peek() if q.PLATFORM == "ios" else None,
                "settings": {"mute": bool(q.MUTE_ON_START),
                             "skip": bool(q.ENABLE_SKIP),
                             "fast_ad": bool(q.AD_FAST_MODE),
                             "ocr_width": q.OCR_MAX_WIDTH},
                "vip": {"days": vip_days(), "total": int(S.vip["total"]),
                        "per": int(S.vip["per"]), "cap": int(S.vip["cap"]),
                        "start_days": int(S.vip["start_days"]),
                        "real_days": int(S.vip.get("real_days", -1)),
                        "estimate": vip_estimate()},
            }, ensure_ascii=False))

        if path == "/api/log":
            since = 0
            if "since=" in self.path:
                try:
                    since = int(self.path.split("since=")[1].split("&")[0])
                except Exception:
                    since = 0
            total, items = S.snapshot_logs(since)
            return self._send(200, json.dumps({"total": total, "items": items},
                                              ensure_ascii=False))

        if path == "/api/profile":
            if "read=1" in self.path:
                params = parse_qs(urlparse(self.path).query)
                platform = (params.get("platform") or [""])[0]
                wda = (params.get("wda") or [""])[0]
                bundle = (params.get("bundle") or [""])[0]
                if platform:
                    q.configure_platform(platform, wda, bundle)
                q.screen_cache = None          # 强制重新读 wm size，换手机时不会拿到旧值
                q._auto_serial["ts"] = 0.0     # 顺便重新枚举设备
                info = q.get_device_info(force=True)
                apply_device_info(info)
                return self._send(200, json.dumps(info, ensure_ascii=False))
            params = parse_qs(urlparse(self.path).query)
            platform = (params.get("platform") or [""])[0]
            return self._send(200, json.dumps(q.read_profile(platform or None),
                                              ensure_ascii=False))

        if path == "/api/frame":
            if S.frame:
                return self._send(200, S.frame, "image/jpeg")
            return self._send(204, b"", "image/jpeg")

        if path == "/api/wda/status":
            deep = "deep=1" in self.path
            status = WDA.status(device=dict(S.device or {}), deep=deep)
            return self._send(200, json.dumps(status, ensure_ascii=False))

        if path == "/api/wda/log":
            since = 0
            if "since=" in self.path:
                try:
                    since = int(self.path.split("since=")[1].split("&")[0])
                except Exception:
                    since = 0
            total, items = WDA.snapshot_logs(since)
            return self._send(200, json.dumps({"total": total, "items": items},
                                              ensure_ascii=False))

        if path == "/api/support":
            return self._send(200, json.dumps({"ok": True, "slots": support_meta()},
                                              ensure_ascii=False))

        if path == "/api/support/qr":
            params = parse_qs(urlparse(self.path).query)
            kind = (params.get("type") or [""])[0]
            if kind not in SUPPORT_KINDS:
                return self._send(404, json.dumps({"ok": False, "error": "未知类型"},
                                                  ensure_ascii=False))
            body, ctype, err = read_support_blob(kind)
            if not body:
                code = 404 if err == "暂未配置支持方式" else 500
                return self._send(code, json.dumps({"ok": False, "error": err},
                                                   ensure_ascii=False))
            return self._send(200, body, ctype or "application/octet-stream")

        return self._send(404, json.dumps({"ok": False}))

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            data = {}

        if path == "/api/start":
            result = start_run()
            return self._send(200, json.dumps(result, ensure_ascii=False))

        if path == "/api/stop":
            stop_run()
            return self._send(200, json.dumps({"ok": True}))

        if path == "/api/wda/action":
            action = str(data.get("action") or "").strip().lower()
            result = WDA.action(action, dict(S.device or {}))
            if result.get("ok"):
                S.wda_prompt = False
            code = 200 if result.get("ok") else 400
            return self._send(code, json.dumps(result, ensure_ascii=False))

        if path == "/api/settings":
            if "mute" in data:
                q.MUTE_ON_START = bool(data["mute"])
                S.log(f"设置：静音 = {'开' if q.MUTE_ON_START else '关'}", "dim")
            if "skip" in data:
                q.ENABLE_SKIP = bool(data["skip"])
                S.log(f"设置：允许跳过 = {'开' if q.ENABLE_SKIP else '关'}", "dim")
            if "fast_ad" in data:
                q.AD_FAST_MODE = bool(data["fast_ad"])
                S.log(f"设置：广告极速 = {'开' if q.AD_FAST_MODE else '关'}", "dim")
            if "ocr_width" in data:
                try:
                    q.OCR_MAX_WIDTH = max(600, min(1200, int(data["ocr_width"])))
                    S.log(f"设置：OCR 宽度 = {q.OCR_MAX_WIDTH}", "dim")
                except Exception:
                    pass
            return self._send(200, json.dumps({"ok": True}))

        if path == "/api/profile":
            if data.get("select_only"):
                platform = str(data.get("platform") or "").strip().lower()
                if platform not in ("android", "ios"):
                    return self._send(400, json.dumps({"ok": False, "error": "未知平台"},
                                                      ensure_ascii=False))
                current = q.read_profile(platform)
                current["platform"] = platform
                if platform == "ios":
                    current["ios_wda"] = str(data.get("ios_wda") or current.get("ios_wda")
                                               or q.IOS_WDA_URL).strip().rstrip("/")
                    current["ios_bundle"] = str(data.get("ios_bundle")
                                                  or current.get("ios_bundle")
                                                  or q.IOS_BUNDLE_ID).strip()
                q.save_profile(current)
                S.log("平台已切换：" + ("iPhone / USB 免签直连" if platform == "ios"
                                         else "Android / ADB"), "dim")
                return self._send(200, json.dumps({"ok": True, "profile": q.read_profile(platform)},
                                                  ensure_ascii=False))
            ok = q.save_profile(data)
            q.screen_cache = None
            try:
                if data.get("ocr_width"):
                    q.OCR_MAX_WIDTH = int(data["ocr_width"])
            except Exception:
                pass
            S.log("⚙️ 机型参数已保存：%s %sx%s，OCR宽 %s" % (
                data.get("name") or "自定义",
                data.get("width") or "?", data.get("height") or "?",
                data.get("ocr_width") or q.OCR_MAX_WIDTH))
            return self._send(200, json.dumps({"ok": ok}))

        if path == "/api/vip":
            try:
                if "start_days" in data:
                    S.vip["start_days"] = max(0, int(data["start_days"]))
                if "per" in data:
                    S.vip["per"] = max(1, int(data["per"]))
                if "cap" in data:
                    S.vip["cap"] = max(1, int(data["cap"]))
                if "total" in data:
                    S.vip["total"] = max(0, int(data["total"]))
                if "real_days" in data:
                    S.vip["real_days"] = int(data["real_days"])
                save_vip()
                S.log(f"设置：起始 {S.vip['start_days']} 天 / 每 {S.vip['per']} 条 1 天 / 上限 {S.vip['cap']} 天", "dim")
            except Exception as e:
                S.log(f"天数设置失败：{e}", "error")
            return self._send(200, json.dumps({"ok": True}))

        return self._send(404, json.dumps({"ok": False}))


def main():
    real_stdout = sys.stdout
    try:
        sys.stdout = Tee(real_stdout)
    except Exception:
        pass

    load_vip()
    q.load_profile()
    S.log("控制台已启动", "ok")

    # ---- 环境自检：换电脑/换手机时，缺什么直接写在日志里 ----
    if q.PLATFORM == "ios":
        S.log("平台：iPhone / USB 免签直连（iOS 27.0+；低版本使用 WDA）", "dim")
    else:
        try:
            ap = q.adb_path()
            if ap == "adb" or not os.path.exists(ap):
                S.log("没找到 adb.exe：请确认 platform-tools 目录完整，或在右侧「ADB」填绝对路径", "error")
            else:
                S.log(f"adb：{ap}", "dim")
            ver = q.adb(["version"]) or ""
            m = re.search(r"version\s+([\d.]+)", ver)
            if m:
                S.log(f"adb 版本：{m.group(1)}", "dim")
            else:
                S.log("adb 无法执行：检查数据线、驱动，或手机是否点了「允许调试」", "warn")
        except Exception as e:
            S.log(f"环境自检异常：{e}", "warn")

    if detect_device():
        if q.PLATFORM == "ios":
            S.log(f"设备：{S.device.get('name')}  iOS {S.device.get('ios')}"
                  f"  Build {S.device.get('build') or '?'}  {S.device.get('driver') or 'USB'}", "ok")
            if S.device.get("developer_mode") is False:
                S.log("开发者模式未开启：进入「设置 → 隐私与安全性 → 开发者模式」开启并重启", "warn")
            elif S.device.get("needs_wda"):
                S.log(S.device.get("hint") or "iOS 26.x 及以下需要 WebDriverAgent 才能自动点击", "warn")
            elif not S.device.get("ready"):
                S.log("iPhone 参数已读取。点「开始」会自动准备免签直连，首次可能需要几分钟", "info")
        else:
            S.log(f"设备：{S.device.get('model')}  {S.device.get('w')}×{S.device.get('h')}  Android {S.device.get('android')}", "ok")
    else:
        if q.PLATFORM == "ios":
            S.log(S.device.get("error") or "未检测到 iPhone：请插 USB、解锁并点「信任此电脑」", "warn")
        else:
            S.log("未检测到设备，请插好 USB 并允许调试", "warn")
    S.log(f"免费听天数：预计 {vip_days()} 天（累计 {S.vip['total']} 条，上限 {S.vip['cap']} 天）", "info")

    threading.Thread(target=preview_worker, daemon=True).start()
    threading.Thread(target=ocr_prewarm_worker, daemon=True).start()
    threading.Thread(target=days_bootstrap_worker, daemon=True).start()

    port = PORT
    for p in range(PORT, PORT + 12):
        try:
            with socket.socket() as sk:
                sk.bind(("127.0.0.1", p))
            port = p
            break
        except OSError:
            continue
    if port != PORT:
        S.log(f"端口 {PORT} 被占用，已改用 {port}", "warn")

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  控制台已就绪 -> {url}")
    print("  关闭这个窗口即退出\n")
    try:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    except Exception:
        pass

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        S.stop_flag.set()
        srv.server_close()


if __name__ == "__main__":
    main()
