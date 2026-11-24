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
import subprocess
import sys
import time
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
# 关闭类目标：广告结束页右上角的「领取成功 ×」胶囊，点它才会关掉广告、弹出奖励弹窗
CLOSE_TARGETS = ["领取成功", "退出不看了"]
# 「xx秒后 / xx s 后进入直播间 [取消]」：文字需同时含倒计时和「进入直播间」，
# 避免把普通横幅「点击进入直播间」也当成自动跳转

# 跳过目标（仅当广告本身允许跳过时才会出现）
SKIP_TARGETS = ["跳过广告", "跳过", "关闭广告"]
ENABLE_SKIP = True          # 提前点“跳过”有可能拿不到奖励，视情况关掉

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
    "退出不看了": {"x": (0.00, 1.00), "y": (0.80, 1.00)},
    "跳过广告": {"x": (0.55, 1.00), "y": (0.00, 0.20)},
    "跳过":     {"x": (0.55, 1.00), "y": (0.00, 0.20)},
    "关闭广告": {"x": (0.55, 1.00), "y": (0.00, 0.20)},
}
CENTER_TARGETS = {"领取奖励", "继续观看"}
# ===========================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(SCRIPT_DIR, "debug_clicks")
PROFILE_PATH = os.path.join(SCRIPT_DIR, "device_profile.json")
PROFILE = {}


def load_profile():
    """读取 device_profile.json：自定义机型名、分辨率、OCR 宽度、adb 路径、设备序列号。"""
    global PROFILE
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            PROFILE = json.load(f) or {}
    except Exception:
        PROFILE = {}
    return PROFILE


def save_profile(data):
    global PROFILE
    PROFILE = dict(data or {})
    try:
        with open(PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(PROFILE, f, ensure_ascii=False, indent=2)
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
def adb_path():
    """优先用本目录里的 platform-tools\\adb.exe，找不到再退回 PATH 里的 adb"""
    local = os.path.join(SCRIPT_DIR, "platform-tools",
                         "adb.exe" if os.name == "nt" else "adb")
    return local if os.path.exists(local) else "adb"


def adb(args, binary=False, timeout=20):
    """执行 adb 命令，失败/超时返回 None"""
    try:
        cmd = [adb_path()]
        _sn = _profile_serial()
        if _sn:
            cmd += ["-s", _sn]
        r = subprocess.run(cmd + args, capture_output=True, timeout=timeout)
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
        ocr_engine = RapidOCR()
    return ocr_engine


def screenshot():
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


FREE_DAYS_RE = re.compile(r"免费听\s*(\d+)\s*天")


DAY_PATTERNS = [
    re.compile(r"第\s*(\d+)\s*[日天]\s*免费听"),
    re.compile(r"免费听\s*(\d+)\s*[日天]"),
    re.compile(r"恭喜获得第\s*(\d+)\s*天"),
    re.compile(r"已领取\s*第?\s*(\d+)\s*天"),
]


def sync_days(max_back=3):
    """尽力读真实免费听天数：当前页读不到就往回退到首页读，读完再点「免费听」回到广告。"""
    n = read_free_days()
    if n is not None:
        return n
    for _ in range(max_back):
        adb(["shell", "input", "keyevent", "4"])
        time.sleep(0.9)
        FOCUS_CACHE["ts"] = 0
        n = read_free_days()
        if n is not None:
            return n
    return None


def parse_free_days(nodes):
    """从 ocr_nodes 的结果里解析真实免费听天数，解析不到返回 None。"""
    if not nodes:
        return None
    for row in nodes:
        label = (row[0] or "")
        for pat in DAY_PATTERNS:
            m = pat.search(label)
            if m:
                try:
                    return int(m.group(1))
                except Exception:
                    pass
    return None


def read_free_days():
    """从汽水音乐首页的「免费听N天」按钮读出真实天数；读不到返回 None。"""
    screen = screenshot()
    if screen is None:
        return None
    try:
        nodes = ocr_nodes(screen)
    except Exception:
        return None
    for row in nodes:
        label = row[0]
        m = FREE_DAYS_RE.search(normalize_text(label)) or FREE_DAYS_RE.search(label or "")
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return None
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


# ------------------------- 通道二：uiautomator（可选） -------------------------
def dump_nodes(timeout=20):
    """返回 [(label, cx, cy), ...]；失败返回 None"""
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
    adb(["shell", "input", "tap", str(x), str(y)])
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
        adb(["shell", "input", "keyevent", "4"])
        return True

    return False


def match_and_click(nodes, source, screen=None):
    """在节点里找目标并点击。命中返回目标字符串，否则 None"""
    if not nodes:
        return None
    # 「xx秒后进入直播间 [取消]」优先拦下，别被带进直播间
    if try_cancel_live_enter(nodes, source):
        return "取消"
    sw, sh = screen_size()
    # 领奖优先，其次“领取成功 / 退出不看了”（关广告），最后才跳过
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


# ------------------------- 静音 -------------------------
def get_music_volume():
    """返回媒体音量索引；失败 None"""
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
    adb(["shell", "input", "keyevent", "164"])


def set_music_volume(idx):
    """直接把媒体音量索引设成 idx，并回读确认。成功返回 True。"""
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
last_back_ts = 0.0


def current_focus():
    """当前前台 Window/Activity，缓存 1.5s"""
    now = time.time()
    if now - FOCUS_CACHE["ts"] < 1.5:
        return FOCUS_CACHE["v"]
    out = adb(["shell", "dumpsys", "window"], timeout=10) or ""
    m = re.search(r"mCurrentFocus=Window\{[^}]*?([\w.]+/[\w.$]+)", out)
    v = m.group(1) if m else ""
    FOCUS_CACHE.update(ts=now, v=v)
    return v


def in_live_room():
    f = current_focus()
    return ("LivePlayerActivity" in f) or (".live." in f and "Activity" in f)


def _tap_free_listen():
    """在首页找「免费听N天」按钮点进去，重新进入广告流程"""
    for _ in range(6):
        scr = screenshot()
        if scr is None:
            time.sleep(0.8)
            continue
        for n in ocr_nodes(scr):
            if "免费听" in n[0]:
                do_click(n[1], n[2], "免费听", n[0], "ocr", scr)
                time.sleep(3.5)
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
    for attempt in range(4):
        scr = screenshot()
        if scr is None:
            time.sleep(0.8)
            continue
        nodes = ocr_nodes(scr)
        for n in nodes:
            if "再看" in n[0]:
                do_click(n[1], n[2], "再看", n[0], "ocr", scr)
                time.sleep(3.0)
                return True
        for n in nodes:
            if "免费听" in n[0]:
                do_click(n[1], n[2], "免费听", n[0], "ocr", scr)
                time.sleep(3.5)
                return True
        adb(["shell", "input", "keyevent", "4"])
        time.sleep(0.9)
    return False


def restart_app():
    """强制重启 App 并回到广告流程"""
    adb(["shell", "am", "force-stop", "com.luna.music"])
    time.sleep(1.2)
    adb(["shell", "monkey", "-p", "com.luna.music", "-c",
         "android.intent.category.LAUNCHER", "1"])
    FOCUS_CACHE["ts"] = 0.0
    time.sleep(6.0)
    return _tap_free_listen()


def escape_live_room():
    """按返回键退出直播间；退不出就强制重启 App 并重新进入广告"""
    for _ in range(2):
        adb(["shell", "input", "keyevent", "4"])
        FOCUS_CACHE["ts"] = 0.0
        time.sleep(0.9)
        if not in_live_room():
            print("  ↩️  已退出直播间，继续监控")
            return True
    print("  ⚠️  返回键退不出直播间，强制重启 App 并重新进入广告")
    ok = restart_app()
    print("  ✅ 已重启并重新进入广告流程" if ok else "  ⚠️  重新进入广告失败，继续尝试")
    return ok


# ------------------------- 主循环 -------------------------
def main():
    global last_ocr_ts, uia_fails

    use_ocr = ocr_available()
    use_uia = uia_available()
    sw, sh = screen_size()

    print("🚀 汽水音乐自动看广告脚本启动")
    print(f"   逻辑分辨率: {sw}x{sh}")
    print(f"   主通道: {'OCR' if use_ocr else '（OCR 不可用）'}   "
          f"uiautomator: {'开' if use_uia else '关'}")
    print(f"   领奖目标: {REWARD_TARGETS}")
    print(f"   跳过目标: {SKIP_TARGETS if ENABLE_SKIP else '已禁用'}")
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
                if screen is not None and match_and_click(ocr_nodes(screen), "ocr", screen):
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
