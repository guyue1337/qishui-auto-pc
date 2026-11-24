# -*- coding: utf-8 -*-
"""
汽水音乐自动化本地部署 — 可视化控制台
======================================
复用 qishui_auto.py 的全部逻辑（截图 / OCR / 点击优先级 / ROI 防误点），
外面套一层本地网页界面：浏览器打开就能开始、停止、看实时画面、看日志、记免费听天数。

启动：双击 控制台.bat，或 python qishui_gui.py
只用标准库 + 已装好的 cv2/numpy，不引入任何新依赖。
"""
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import qishui_auto as q  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PORT = int(os.environ.get("QISHUI_PORT") or 8788)
PREVIEW_WIDTH = 460
JPEG_QUALITY = 72
MAX_LOG = 400
VIP_FILE = os.path.join(BASE_DIR, "vip_state.json")


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
        self.vip = {"start_days": 0, "per": 3, "cap": 30, "total": 0, "real_days": -1}
        self.real_ts = 0.0

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


def refresh_real_days(force=False):
    """OCR 首页「免费听N天」，把真实天数写进状态。节流 3 秒。"""
    now = time.time()
    if not force and now - S.real_ts < 3.0:
        return
    S.real_ts = now
    try:
        n = q.read_free_days()
    except Exception:
        n = None
    if n is None:
        return
    if n != int(S.vip.get("real_days", -1)):
        S.vip["real_days"] = int(n)
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
def detect_device():
    out = q.adb(["devices"]) or ""
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    devices = [l for l in lines[1:] if "\tdevice" in l or " device" in l]
    if not devices:
        S.device = {"connected": False, "model": "", "w": 0, "h": 0, "android": ""}
        return False
    model = (q.adb(["shell", "getprop", "ro.product.model"]) or "").strip()
    rel = (q.adb(["shell", "getprop", "ro.build.version.release"]) or "").strip()
    sw, sh = q.screen_size()
    S.device = {"connected": True, "model": model or devices[0].split()[0],
                "w": sw, "h": sh, "android": rel}
    return True


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
    if not detect_device():
        S.log("未检测到设备：请检查 USB 连接与「USB 调试」授权", "error")
        S.running = False
        return

    sw, sh = q.screen_size()
    S.log(f"设备已连接：{S.device.get('model')}  {sw}x{sh}", "ok")
    S.log(f"OCR 主通道已启用，缩放宽度 {q.OCR_MAX_WIDTH}", "info")

    if q.MUTE_ON_START:
        q.mute_on_start()

    # 启动时先把手机上的真实天数读出来（读不到会退回首页读，读完自动回广告页）
    try:
        n0 = q.sync_days()
    except Exception:
        n0 = None
    if n0 is not None:
        S.vip["real_days"] = int(n0)
        save_vip()
        S.log("📱 手机实测免费听：%d 天" % n0, "ok")
    else:
        S.log("⚠️ 没读到手机上的天数，先用本地估算", "warn")

    idle = 0
    feeder_idle = 0
    feed_tries = 0
    S.start_ts = time.time()
    try:
        while not S.stop_flag.is_set():
            # 直播间检测：被拖进直播间后永远等不到目标按钮，必须主动逃出
            if q.in_live_room():
                S.log("检测到进入直播间，正在退出…", "warn")
                q.escape_live_room()
                idle = 0
                feeder_idle = 0
                continue

            # 停在首页说明广告流程断了（常见：领完奖励弹「恭喜获得第N天」模态弹窗）
            if "MainActivity" in q.current_focus():
                refresh_real_days()
                feeder_idle += 1
                if feeder_idle >= 2:
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
                    nodes = q.ocr_nodes(screen)
                    try:
                        n_day = q.parse_free_days(nodes)
                    except Exception:
                        n_day = None
                    if n_day is not None and n_day != int(S.vip.get("real_days", -1)):
                        S.vip["real_days"] = int(n_day)
                        save_vip()
                        S.log("📱 读到了：免费听 %d 天" % n_day, "ok")
                    if q.match_and_click(nodes, "ocr", screen):
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
        return
    if not detect_device():
        S.log("未检测到设备：请检查 USB 连接与「USB 调试」授权", "error")
        return
    S.clicks = 0
    S.rounds = 0
    S.running = True
    S.stop_flag.clear()
    S.log("开始运行", "ok")
    threading.Thread(target=worker, daemon=True).start()


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
.num input,.cfgrow input{width:100%;height:30px;border-radius:10px;padding:0 8px;font-size:12.5px;
  color:#0b0d16;background:rgba(255,255,255,.52);border:1px solid rgba(255,255,255,.8);outline:none;
  box-shadow:inset 0 1px 2px rgba(60,80,140,.06)}
.num input:focus,.cfgrow input:focus{border-color:#7c9bff}

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
.cfgrow input{flex:1 1 auto}
.cfg2{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.cfg2 .cfgrow span{flex:0 0 30px}
.cfghint{font-size:11px;line-height:1.6;color:#5b6484}
.cfgbtns{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:2px}
.preview{position:relative;flex:1 1 auto;min-height:150px;border-radius:15px;overflow:hidden;
  background:rgba(10,10,18,.85);border:1px solid rgba(255,255,255,.55);display:grid;place-items:center;
  box-shadow:0 10px 26px rgba(60,80,150,.2)}
.preview img{width:100%;height:100%;object-fit:contain;display:block}
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
body.dark .cfghint,body.dark .hint{color:#8a92ab}
body.dark .chip{background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.08);color:#c3c9db;box-shadow:none}
body.dark .card h2,body.dark .pv h2{color:#8d95b0}
body.dark .stat{background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.07)}
body.dark .stat .v{color:#f2f4fa}
body.dark .stat .v.b{color:#8ea8ff}
body.dark .switch label{color:#cfd5e8}
body.dark .vipdays{color:#fff}
body.dark .vipmeta b{color:#dfe3f0}
body.dark .num input,body.dark .cfgrow input{background:rgba(255,255,255,.07);
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
</style>
</head>
<body class="dark">
<div class="bg"></div>

<div class="wrap">
  <aside class="brand glass">
    <div class="logo">汽</div>
    <h1>汽水音乐<br>自动化</h1>
    <ol class="steps">
      <li>手机设置里打开「开发者选项 → USB 调试」</li>
      <li>数据线连电脑，手机弹窗点「允许」（勾始终允许）</li>
      <li>手机保持亮屏，打开汽水音乐，进到看广告页</li>
      <li>点右侧「读取当前手机」→「保存参数」</li>
      <li>点「开始」，就能挂机了</li>
    </ol>
    <div class="bottom">
      <button class="themebtn" id="themebtn">切换亮色</button>
      <div class="chip"><span class="dot" id="dot"></span><span id="devtxt">正在检测设备…</span></div>
    </div>
  </aside>

  <div class="main">
    <div class="stack">
      <div class="card glass">
        <h2>控制</h2>
        <div class="btnrow">
          <button class="btn" id="startBtn">开 始</button>
          <button class="btn stop" id="stopBtn" disabled>停 止</button>
        </div>
        <div class="stats">
          <div class="stat"><div class="v" id="clicks">0</div><div class="k">点击次数</div></div>
          <div class="stat"><div class="v b" id="rounds">0</div><div class="k">看完广告</div></div>
        </div>
        <div class="switch">
          <label>广告期间静音</label>
          <div class="tog"><input type="checkbox" id="mute"><span></span></div>
        </div>
        <div class="switch">
          <label>允许点「跳过」</label>
          <div class="tog"><input type="checkbox" id="skip"><span></span></div>
        </div>
      </div>

      <div class="card glass">
        <h2>免费听天数</h2>
        <div class="viprow">
          <div class="vipdays" id="vipdays">--</div>
          <div class="vipunit">天 / <b id="vipsrc">读取中</b></div>
          <div class="vipcap">上限 <b id="vipcap">30</b> 天</div>
        </div>
        <div class="bar"><i id="vipbar"></i></div>
        <div class="vipmeta">
          <span>本次累计 <b id="viptotal">0</b> 条</span>
          <span>按条数估算 <b id="vipest">0</b> 天</span>
          <span>起始 <b id="vipstart">0</b> 天</span>
        </div>
        <div class="nums">
          <div class="num"><span>起始天数</span><input type="number" id="inStart" min="0" max="60" step="1"></div>
          <div class="num"><span>几条=1天</span><input type="number" id="inPer" min="1" max="20" step="1"></div>
          <div class="num"><span>天数上限</span><input type="number" id="inCap" min="1" max="365" step="1"></div>
        </div>
      </div>

      <div class="logbox" id="logbox"></div>
    </div>

  </div>

  <aside class="card glass pv">
    <h2>参数 · 画面</h2>
    <div class="cfg">
      <div class="cfgrow"><span>机型</span><input id="cfName" placeholder="如 HONOR TFY-AN00"></div>
      <div class="cfg2">
        <div class="cfgrow"><span>宽</span><input id="cfW" type="number" min="320" max="4000"></div>
        <div class="cfgrow"><span>高</span><input id="cfH" type="number" min="480" max="6000"></div>
      </div>
      <div class="cfg2">
        <div class="cfgrow"><span>OCR宽</span><input id="cfOcr" type="number" min="360" max="1600"></div>
        <div class="cfgrow"><span>序列号</span><input id="cfSn" placeholder="留空=唯一设备"></div>
      </div>
      <div class="cfgrow"><span>ADB</span><input id="cfAdb" placeholder="platform-tools\adb.exe"></div>
      <div class="cfghint">换手机：插 USB → 点「读取当前手机」→ 点「保存参数」，坐标全按比例算，分辨率变了也能刷。</div>
      <div class="cfgbtns">
        <button class="btn mini" id="cfRead">读取当前手机</button>
        <button class="btn mini ghost" id="cfSave">保存参数</button>
      </div>
    </div>
    <div class="preview">
      <span class="badge" id="pbadge">预览</span>
      <div class="empty" id="pempty">等待画面…<br>请确认 USB 已连接</div>
      <img id="pimg" style="display:none" alt="手机画面">
    </div>
    <div class="hint">手机亮屏 → 打开汽水音乐 → 进看广告页 → 开始运行</div>
  </aside>
</div>

<script>
let since = 0;

async function j(url, opt){ const r = await fetch(url, opt); return r.json(); }

async function refresh(){
  try{
    const s = await j('/api/status');
    document.getElementById('dot').className = 'dot ' + (s.running ? 'run' : (s.device.connected ? 'on' : ''));
    document.getElementById('devtxt').textContent = s.device.connected
      ? (s.device.model + '  ' + s.device.w + '×' + s.device.h)
      : '未连接设备';
    document.getElementById('clicks').textContent = s.clicks;
    document.getElementById('rounds').textContent = s.rounds;
    document.getElementById('startBtn').disabled = s.running;
    document.getElementById('stopBtn').disabled = !s.running;
    document.getElementById('pbadge').textContent = s.running ? '实时' : '预览';
    document.getElementById('pbadge').className = 'badge' + (s.running ? ' live' : '');
    document.getElementById('mute').checked = s.settings.mute;
    document.getElementById('skip').checked = s.settings.skip;

    const v = s.vip;
    document.getElementById('vipdays').textContent = v.days;
    document.getElementById('vipcap').textContent = v.cap;
    document.getElementById('viptotal').textContent = v.total;
    document.getElementById('vipest').textContent = v.estimate;
    document.getElementById('vipstart').textContent = v.start_days;
    document.getElementById('vipsrc').textContent = (v.real_days >= 0) ? '手机实测' : '估算';
    document.getElementById('vipbar').style.width = Math.min(100, v.days / v.cap * 100) + '%';
    if(document.activeElement !== document.getElementById('inStart'))
      document.getElementById('inStart').value = v.start_days;
    if(document.activeElement !== document.getElementById('inPer'))
      document.getElementById('inPer').value = v.per;
    if(document.activeElement !== document.getElementById('inCap'))
      document.getElementById('inCap').value = v.cap;
  }catch(e){}
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

document.getElementById('startBtn').onclick = async () => { await j('/api/start', {method:'POST'}); refresh(); };
document.getElementById('stopBtn').onclick  = async () => { await j('/api/stop',  {method:'POST'}); refresh(); };
document.getElementById('mute').onchange = e => j('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({mute:e.target.checked})});
document.getElementById('skip').onchange = e => j('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({skip:e.target.checked})});

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
/* ================= 机型参数 ================= */
async function loadProfile(){
  try{
    const p = await j('/api/profile');
    document.getElementById('cfName').value = p.name || '';
    document.getElementById('cfW').value    = p.width  || '';
    document.getElementById('cfH').value    = p.height || '';
    document.getElementById('cfOcr').value  = p.ocr_width || 720;
    document.getElementById('cfAdb').value  = p.adb || '';
    document.getElementById('cfSn').value   = p.serial || '';
  }catch(e){}
}
function profileBody(){
  return {
    name: document.getElementById('cfName').value.trim(),
    width: parseInt(document.getElementById('cfW').value)||0,
    height: parseInt(document.getElementById('cfH').value)||0,
    ocr_width: parseInt(document.getElementById('cfOcr').value)||720,
    adb: document.getElementById('cfAdb').value.trim(),
    serial: document.getElementById('cfSn').value.trim()
  };
}
document.getElementById('cfSave').onclick = async () => {
  await j('/api/profile', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(profileBody())});
  loadProfile(); refresh();
};
document.getElementById('cfRead').onclick = async () => {
  const p = await j('/api/profile?read=1');
  if(p && p.width){
    document.getElementById('cfW').value = p.width;
    document.getElementById('cfH').value = p.height;
    if(p.model && !document.getElementById('cfName').value) document.getElementById('cfName').value = p.model;
  }
  loadProfile(); refresh();
};
loadProfile();
/* ================= 机型参数结束 ================= */
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
let savedTheme = 'dark';
try{ savedTheme = localStorage.getItem('qishui_theme2') || 'dark'; }catch(e){}
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

        if path == "/api/status":
            return self._send(200, json.dumps({
                "running": S.running,
                "clicks": S.clicks,
                "rounds": S.rounds,
                "device": S.device,
                "settings": {"mute": bool(q.MUTE_ON_START),
                             "skip": bool(q.ENABLE_SKIP),
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
                w, h = q.screen_size()
                dev = S.device or {}
                return self._send(200, json.dumps({"width": w, "height": h,
                                                  "model": dev.get("model", "")},
                                                 ensure_ascii=False))
            return self._send(200, json.dumps(q.load_profile(), ensure_ascii=False))

        if path == "/api/frame":
            if S.frame:
                return self._send(200, S.frame, "image/jpeg")
            return self._send(204, b"", "image/jpeg")

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
            start_run()
            return self._send(200, json.dumps({"ok": True}))

        if path == "/api/stop":
            stop_run()
            return self._send(200, json.dumps({"ok": True}))

        if path == "/api/settings":
            if "mute" in data:
                q.MUTE_ON_START = bool(data["mute"])
                S.log(f"设置：静音 = {'开' if q.MUTE_ON_START else '关'}", "dim")
            if "skip" in data:
                q.ENABLE_SKIP = bool(data["skip"])
                S.log(f"设置：允许跳过 = {'开' if q.ENABLE_SKIP else '关'}", "dim")
            if "ocr_width" in data:
                try:
                    q.OCR_MAX_WIDTH = max(600, min(1200, int(data["ocr_width"])))
                    S.log(f"设置：OCR 宽度 = {q.OCR_MAX_WIDTH}", "dim")
                except Exception:
                    pass
            return self._send(200, json.dumps({"ok": True}))

        if path == "/api/profile":
            ok = q.save_profile(data)
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
    S.log("控制台已启动", "ok")
    if detect_device():
        S.log(f"设备：{S.device.get('model')}  {S.device.get('w')}×{S.device.get('h')}", "ok")
    else:
        S.log("未检测到设备，请插好 USB 并允许调试", "warn")
    S.log(f"免费听天数：预计 {vip_days()} 天（累计 {S.vip['total']} 条，上限 {S.vip['cap']} 天）", "info")

    threading.Thread(target=preview_worker, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
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
