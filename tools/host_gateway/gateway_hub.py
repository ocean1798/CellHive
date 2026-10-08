# -*- coding: utf-8 -*-
"""
Air780 系列智能通信网关 - 本地多设备共享中枢 (Multi-Dongle Cluster & Dynamic Session Pool)
功能：
1. 动态探测并管理 1~N 块移芯/合宙 4G 模组（Air780EPV / Air780EC / Air780E / Air700E 等）的用户通信口 (x.6/VUART_0)；
2. 为每个物理设备开辟独立的 DongleSession 链路与心跳重连机制，分配动态卡槽 (slot_1 ~ slot_n)，支持即插即用热插拔；
3. 绑定 127.0.0.1:17800 端口并实现 Windows Mutex 单例独占保护；
4. 将多板卡上报的 NDJSON 事件（状态、短信、验证码、来电）注入设备卡槽元数据，防串台广播给所有在线客户端；
5. 收到板端短信后借用电脑宽带代推全渠道通知，代推成功后定向向原卡板回写 Push ACK 确认；
6. 汇聚各客户端（Web 控制台、FastMCP）下发的指令，支持定向卡槽路由与缺省主卡向下兼容。
"""

import sys
import os
import re
import time
import uuid
import json
import copy
import socket
import select
import serial
import serial.tools.list_ports
import threading
import hashlib
import hmac
import base64
import urllib.request
import urllib.parse
import urllib.error
from typing import List, Dict, Any, Optional, Tuple

from cluster_health import ClusterHealthMonitor, HealthState
from cluster_router import ClusterRouter, RouteStrategy, detect_sim_carrier, detect_phone_carrier, clean_phone_number

CARRIER_NAME_MAP = {
    "cucc": ("中国联通", "联通"),
    "cmcc": ("中国移动", "移动"),
    "ctcc": ("中国电信", "电信"),
    "cbn":  ("中国广电", "广电"),
}

HUB_HOST = "127.0.0.1"
HUB_PORT = 17800
SERIAL_BAUD = 115200
SERIAL_PORT: Optional[str] = None  # 兼容旧版，集群架构下默认由会话池自动探测

class _SafeStream:
    def write(self, msg): pass
    def flush(self): pass

if sys.stdout is None:
    sys.stdout = _SafeStream()
if sys.stderr is None:
    sys.stderr = _SafeStream()

try:
    from runtime import get_web_dir, get_data_dir, get_config_dir
except ImportError:
    try:
        from core.runtime import get_web_dir, get_data_dir, get_config_dir
    except ImportError:
        def get_web_dir() -> str:
            if getattr(sys, "frozen", False):
                return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
            return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
        def get_data_dir() -> str:
            return os.path.dirname(os.path.abspath(__file__))
        def get_config_dir() -> str:
            return os.path.dirname(os.path.abspath(__file__))

DATA_DIR = get_data_dir()
CONFIG_DIR = get_config_dir()
GATEWAY_CONFIG_PATH = os.path.join(CONFIG_DIR, "gateway_config.json")

def _redact_log_text(msg: Any) -> str:
    """脱敏日志中的手机号、验证码与密钥"""
    text = str(msg)
    text = re.sub(r"https?://[^\s\"'<>]+", "<endpoint>", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\d)(?:\+?86[\s-]?)?1\d{10}(?!\d)", "<phone>", text)
    text = re.sub(r"(?i)(验证码|otp|pin|code)(\s*[:：=]\s*)\d{4,8}", r"\1\2<redacted>", text)
    text = re.sub(r"(?i)([?&](?:token|secret|sign|signature|password|key)=)[^&#\s]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)((?:\"|')?(?:secret|token|password|credential|webhook_secret)(?:\"|')?\s*[:=]\s*(?:\"|'))[^\"']*(\")", r"\1<redacted>\2", text)
    return text

def log(msg: str):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{now}] [Hub] {_redact_log_text(msg)}\n"
    if sys.stderr is not None:
        try:
            sys.stderr.write(formatted)
            sys.stderr.flush()
        except Exception:
            pass
    try:
        log_p = os.path.join(DATA_DIR, "hub_debug.log")
        with open(log_p, "a", encoding="utf-8") as f:
            f.write(formatted)
    except Exception:
        pass

def set_windows_clipboard(text: str) -> bool:
    """免依赖使用 ctypes 调用 Windows API 将文本原子写入系统剪贴板（含重试防抖）"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE

        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL

        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.05)
        if not opened:
            return False

        user32.EmptyClipboard()
        data = text.encode("utf-16le") + b"\x00\x00"
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_mem:
            user32.CloseClipboard()
            return False

        p_mem = kernel32.GlobalLock(h_mem)
        if not p_mem:
            kernel32.GlobalFree(h_mem)
            user32.CloseClipboard()
            return False

        ctypes.memmove(p_mem, data, len(data))
        kernel32.GlobalUnlock(h_mem)
        user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        user32.CloseClipboard()
        return True
    except Exception as e:
        log(f"写入 Windows 剪贴板异常: {e}")
        try:
            import ctypes
            ctypes.windll.user32.CloseClipboard()
        except Exception:
            pass
        return False

def get_windows_clipboard() -> Optional[str]:
    """免依赖使用 ctypes 调用 Windows API 从系统剪贴板读取文本"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        CF_UNICODETEXT = 13
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE

        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.05)
        if not opened:
            return None

        h_mem = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_mem:
            user32.CloseClipboard()
            return None

        p_mem = kernel32.GlobalLock(h_mem)
        if not p_mem:
            user32.CloseClipboard()
            return None

        text = ctypes.wstring_at(p_mem)
        kernel32.GlobalUnlock(h_mem)
        user32.CloseClipboard()
        return text
    except Exception as e:
        try:
            import ctypes
            ctypes.windll.user32.CloseClipboard()
        except Exception:
            pass
        return None


def _escape_powershell_str(val: str) -> str:
    if not val:
        return ""
    # 转义单引号为双单引号，剔除反引号、换行与控制空字符，强制截断至 100 字符以内
    cleaned = str(val).replace("'", "''").replace("`", "").replace("\x00", "").replace("\r", " ").replace("\n", " ").strip()
    if len(cleaned) > 100:
        cleaned = cleaned[:100]
        # 防护：若截断恰好劈开了一对双单引号导致末尾为单引号，修剪末尾确保单引号绝对成对闭合
        quote_count = cleaned.count("'")
        if quote_count % 2 != 0:
            cleaned = cleaned.rstrip("'")
    return cleaned


_notification_sinks = []

def register_notification_sink(fn):
    """注册跨平台通知观察者 (Windows Toast / fnOS 系统消息等)"""
    if fn not in _notification_sinks:
        _notification_sinks.append(fn)

def dispatch_notification(title: str, body: str, privacy: bool = False):
    """向所有注册的观察者分发通知"""
    for sink in _notification_sinks:
        try:
            sink(title, body, privacy=privacy)
        except TypeError:
            try:
                sink(title, body)
            except Exception:
                pass
        except Exception:
            pass

def show_windows_toast(title: str, message: str, privacy: bool = False):
    if sys.platform != "win32":
        return
    if privacy:
        title = "📩 数字蜂巢收到新短信"
        message = "收到一条新短信（已开启防偷窥保护，点击进入控制台查看）"
    safe_title = _escape_powershell_str(title)
    safe_message = _escape_powershell_str(message)
    ps_cmd = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$toastXml = [xml]$template.GetXml()
$toastXml.GetElementsByTagName('text')[0].AppendChild($toastXml.CreateTextNode('{safe_title}')) | Out-Null
$toastXml.GetElementsByTagName('text')[1].AppendChild($toastXml.CreateTextNode('{safe_message}')) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($toastXml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Air780Gateway').Show($toast)
"""
    def _run():
        try:
            import subprocess
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True,
                timeout=5,
                **kwargs
            )
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()

if sys.platform == "win32":
    register_notification_sink(show_windows_toast)

def find_cellular_vuart_port(ports_list: Optional[List[Any]] = None) -> Optional[str]:
    """
    查找首个有效的 4G VUART 通信端口 (严格排除 17D1 烧录端口与非 x.6 端口)。
    """
    if ports_list is None:
        try:
            ports_list = list(serial.tools.list_ports.comports())
        except Exception:
            ports_list = []
    for p in ports_list:
        hwid = (getattr(p, "hwid", "") or "").upper()
        vid_attr = getattr(p, "vid", 0) or 0
        vid = hex(vid_attr).upper() if isinstance(vid_attr, int) and vid_attr else str(vid_attr).upper()
        loc = getattr(p, "location", "") or ""
        if "17D1" in vid or "17D1" in hwid:
            continue
        if "19D1:0001" in hwid or ("19D1" in vid and ("0001" in str(getattr(p, "pid", 0)) or getattr(p, "pid", 0) == 1)):
            if loc.endswith("x.6") or ":X.6" in loc.upper() or "MI_06" in hwid or "X.6" in hwid or getattr(p, "pid", 0) == 1:
                return getattr(p, "device", None)
    return None

def scan_all_cellular_ports(ports_list: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    """
    智能扫描系统所有合宙/移芯 4G 模组的用户通信主端口 (VID:PID = 19D1:0001, x.6 / MI_06 / VUART_0)。
    严格过滤掉 x.2 (AT/控制口)、x.4 (Trace/日志口) 以及 x.0 刷机口，确保仅返回业务数据通信口。
    """
    results = []
    try:
        ports = ports_list if ports_list is not None else list(serial.tools.list_ports.comports())
        for p in ports:
            hwid = (p.hwid or "").upper()
            vid = hex(p.vid).upper() if p.vid else ""
            pid = hex(p.pid).upper() if p.pid else ""
            loc = getattr(p, "location", "") or ""

            if "19D1:0001" in hwid or ("19D1" in vid and ("0001" in pid or "1" in pid)):
                # 必须满足用户通信口判定：location 以 x.6 结尾或包含 :X.6 或 MI_06
                if (loc.endswith("x.6") or ":X.6" in loc.upper() or "MI_06" in hwid or "X.6" in hwid or
                    loc.endswith(":1.6") or loc.endswith(".6") or ":1.6" in loc or
                    getattr(p, "interface", "") in ("06", "MI_06", "VUART_0")):

                    results.append({
                        "port": p.device,
                        "desc": p.description,
                        "hwid": p.hwid,
                        "loc": loc
                    })
    except Exception as e:
        log(f"扫描串口异常: {e}")
    return results

def _notify_business_result(channel: str, http_code: int, body: bytes):
    if not 200 <= http_code < 300:
        return "rejected", f"HTTP_{http_code}"
    if channel == "webhook":
        return "http_accepted", "HTTP_2XX"
    try:
        receipt = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "unknown", "business_receipt_unavailable"
    key = "errcode" if channel in ("wecom", "dingtalk") else "code"
    expected = 200 if channel == "bark" else 0
    if isinstance(receipt, dict) and key in receipt:
        return ("accepted", "business_receipt_ok") if receipt[key] == expected else ("rejected", "business_receipt_failed")
    return "unknown", "business_receipt_unavailable"


def test_channel_push(channel: str, cfg: Dict[str, Any], device_desc: Optional[str] = None) -> Dict[str, Any]:
    """单渠道连通性测试 (Test Ping)"""
    url = (cfg.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "URL 不能为空", "cost_ms": 0}

    secret = (cfg.get("secret") or "").strip()
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    dev_label = device_desc or "Air780 集群网关"
    title = f"🔔 {dev_label} 连通性测试"
    payload_dict = {}
    target_url = url

    if channel == "feishu":
        card = {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "style": {"text_size": {"normal_v2": {"default": "normal", "pc": "normal", "mobile": "heading"}}}
            },
            "header": {
                "title": {"tag": "plain_text", "content": "🔔 连通性测试正常"},
                "template": "green"
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {"tag": "markdown", "content": f"**测试渠道：** 飞书自定义机器人\n**测试时间：** {now_str}"},
                    {"tag": "hr"},
                    {"tag": "markdown", "content": "**验证码示例：**\n```text\n886622\n```"},
                    {"tag": "hr"},
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"<font color='grey'>测试设备: {dev_label} (上位机推送)</font>"}}
                ]
            }
        }
        payload_dict = {"msg_type": "interactive", "card": card}
        if secret:
            ts = str(int(time.time()))
            sign_str = f"{ts}\n{secret}"
            hmac_code = hmac.new(sign_str.encode("utf-8"), digestmod=hashlib.sha256).digest()
            payload_dict["timestamp"] = ts
            payload_dict["sign"] = base64.b64encode(hmac_code).decode("utf-8")

    elif channel == "wecom":
        payload_dict = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"### 🔔 {dev_label} 连通性测试\n> **测试渠道**: 企业微信机器人\n> **测试时间**: {now_str}\n> **测试状态**: <font color=\"info\">连通正常</font>"
            }
        }
    elif channel == "dingtalk":
        payload_dict = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"### 🔔 {dev_label} 连通性测试\n> **测试渠道**: 钉钉机器人\n> **测试时间**: {now_str}\n> **测试状态**: 连通正常"
            }
        }
        if secret:
            ts = str(round(time.time() * 1000))
            string_to_sign = f"{ts}\n{secret}"
            hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code).decode("utf-8"))
            target_url = f"{url}&timestamp={ts}&sign={sign}" if "?" in url else f"{url}?timestamp={ts}&sign={sign}"

    elif channel == "bark":
        group = (cfg.get("group") or "Air780Gateway").strip()
        sound = (cfg.get("sound") or "minuet").strip()
        payload_dict = {
            "title": title,
            "body": f"测试时间: {now_str}\n渠道状态: 连通正常",
            "group": group,
            "sound": sound,
            "copy": "886622"
        }

    elif channel == "webhook":
        payload_dict = {
            "device": dev_label,
            "type": "test_ping",
            "time": now_str,
            "timestamp": int(time.time()),
            "message": f"{dev_label} 连通性测试正常"
        }

    try:
        data_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(target_url, data=data_bytes, headers={"Content-Type": "application/json; charset=utf-8"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=5) as resp:
            cost_ms = int((time.time() - t0) * 1000)
            state, reason = _notify_business_result(channel, resp.status, resp.read(4096))
            return {"ok": state in ("accepted", "http_accepted"), "state": state, "reason": reason,
                    "status_code": resp.status, "cost_ms": cost_ms}
    except urllib.error.HTTPError as he:
        return {"ok": False, "status_code": he.code, "error": f"HTTP {he.code}", "cost_ms": 0}
    except Exception as e:
        return {"ok": False, "state": "unknown", "error": type(e).__name__, "cost_ms": 0}


def is_valid_iccid(val: Any) -> bool:
    """校验是否为符合 ITU-T E.118 国际电信标准的有效 ICCID（89 开头，19~20 位纯数字，兼容末尾 F 填充）"""
    if not val:
        return False
    s = str(val).strip().rstrip("Ff")
    return bool(re.match(r"^89\d{17,18}$", s))


def probe_dongle_fingerprint_from_companion(loc: str, port: str) -> Dict[str, str]:
    """
    当主通信串口 (x.6 / VUART_0) 未上报 imei 或 iccid 时（如旧固件 v1.2.4），
    通过模组的同物理 USB 拓扑定位伴生 REPL / CDC 端口 (x.2) 或 AT 端口，
    下发探测指令获取真机物理指纹并固化。
    """
    if not loc:
        return {}

    target_loc_prefix = loc.split(":")[0] if ":" in loc else ""
    candidate_port = None
    try:
        for p in serial.tools.list_ports.comports():
            ploc = getattr(p, "location", "") or ""
            if target_loc_prefix and ploc.startswith(target_loc_prefix):
                # 优先匹配 x.2 (REPL / AT 伴生口)
                if ":X.2" in ploc.upper() or ploc.endswith("x.2"):
                    candidate_port = p.device
                    break
    except Exception:
        pass

    if not candidate_port:
        return {}

    result = {}
    try:
        with serial.Serial(candidate_port, baudrate=115200, timeout=1.0) as ser:
            ser.dtr = True
            ser.rts = True
            # 1. 优先通过 LuatOS REPL 模式探测真机硬件指纹
            ser.write(b'print("FP_START", mobile and mobile.imei and mobile.imei(), mobile and mobile.iccid and mobile.iccid(), "FP_END")\r\n')
            time.sleep(0.2)
            raw = ser.read(1024).decode("utf-8", errors="ignore")
            if "FP_START" in raw and "FP_END" in raw:
                parts = raw.split("FP_START")[1].split("FP_END")[0].strip().split()
                if len(parts) >= 2:
                    if parts[0] != "nil" and len(parts[0]) >= 14:
                        result["imei"] = parts[0]
                    if parts[1] != "nil" and is_valid_iccid(parts[1]):
                        result["iccid"] = parts[1].strip().rstrip("Ff")

            # 2. 若 REPL 模式未读出，兼容标准 AT 模式探测（实施前缀白名单与日志过滤）
            if not result.get("imei"):
                ser.write(b"AT+CGSN\r\n")
                time.sleep(0.1)
                at_cgsn = ser.read(256).decode("utf-8", errors="ignore")
                for line in at_cgsn.splitlines():
                    line_clean = line.strip()
                    if line_clean.startswith(("I/", "W/", "E/", "D/", "~", "CMD received")):
                        continue
                    digits = re.sub(r"\D", "", line_clean)
                    if len(digits) == 15 and (line_clean.startswith("+CGSN:") or digits.startswith(("86", "35", "01", "8689"))):
                        result["imei"] = digits
                        break

            if not result.get("iccid"):
                ser.write(b"AT+ICCID\r\n")
                time.sleep(0.1)
                at_iccid = ser.read(256).decode("utf-8", errors="ignore")
                for line in at_iccid.splitlines():
                    line_clean = line.strip()
                    if line_clean.startswith(("I/", "W/", "E/", "D/", "~", "CMD received")):
                        continue
                    if line_clean.startswith(("+ICCID:", "+CCID:", "+QCCID:", "^ICCID:")) or line_clean.startswith("89"):
                        m = re.search(r"89\d{17,18}[Ff]?", line_clean)
                        if m:
                            cand = m.group(0).rstrip("Ff")
                            if is_valid_iccid(cand):
                                result["iccid"] = cand
                                break
                        else:
                            digits = re.sub(r"\D", "", line_clean)
                            if is_valid_iccid(digits):
                                result["iccid"] = digits.rstrip("Ff")
                                break
    except Exception:
        pass

    return result


# =========================================================================
# 核心类：DongleSession (单模组硬件物理串口独立会话)
# =========================================================================

class DongleSession:
    """
    单个 4G 模组硬件的物理串口独立会话实例。
    每个 Session 独占管理一个物理 COM 端口的打开、收发、半帧缓冲与生命周期。
    """
    def __init__(self, port: str, loc: str, slot_id: str, hub: 'GatewayHub', baud: int = SERIAL_BAUD):
        self.port = port
        self.loc = loc
        self.slot_id = slot_id
        self.hub = hub
        self.baud = baud

        self.ser: Optional[serial.Serial] = None
        self.serial_lock = threading.Lock()
        self._claim_lock = threading.Lock()
        self._claim_waiters = {}
        self.is_connected = False
        self.is_flashing = False
        self.maintenance_job: Optional[Dict[str, Any]] = None
        self._serial_io_paused: bool = False
        self.running = False
        self.rx_thread: Optional[threading.Thread] = None
        self.rx_buffer = bytearray()

        # 板端元数据与状态缓存
        self.meta: Dict[str, Any] = {
            "slot": slot_id,
            "port": port,
            "loc": loc,
            "model": "Air780 Series",
            "bsp": "Unknown",
            "imei": "",
            "iccid": "",
            "version": "",
            "chip": "",
            "capabilities": {},
            "phone": "",
            "csq": 0,
            "rsrp": 0,
            "temp": "",
            "vbat": "",
            "net_ready": False,
            "online": False,
            "reported_model": "",
            "reported_chip": "",
            "reported_imei": "",
            "core_version": "",
            "boot_id": None,
            "build_id": None,
            "serial_ota": None,
            "_identity_seen_at": 0.0
        }
        self.latest_status: Dict[str, Any] = {}
        self.board_cellular_data: bool = False
        self._companion_probed: bool = False

    @property
    def capabilities(self) -> Dict[str, Any]:
        return self.meta.get("capabilities", {})

    @property
    def model(self) -> str:
        return self.meta.get("model", "")

    def start(self):
        """启动会话的后台接收与保活线程"""
        self.running = True
        self.rx_thread = threading.Thread(target=self._session_loop, daemon=True)
        self.rx_thread.start()

    def stop(self):
        """优雅关闭会话与物理串口"""
        self.running = False
        with self.serial_lock:
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
            self.is_connected = False
            self.meta["online"] = False
            self._companion_probed = False

    def send_line(self, line: str) -> bool:
        """线程安全向专属物理串口写入一行指令（自动追加 \\r\\n）"""
        clean_line = line.strip()
        if not clean_line:
            return False

        # 维护期间统一门卫拦截 (AIR-38 S04B)
        is_maintenance = bool(getattr(self, "is_flashing", False) or getattr(self, "maintenance_job", None))
        if is_maintenance:
            try:
                cmd_obj = json.loads(clean_line)
            except Exception:
                log(f"[{self.slot_id}] 维护期间拦截非 JSON 指令")
                return False

            if not isinstance(cmd_obj, dict):
                return False

            cmd_name = str(cmd_obj.get("cmd") or "").strip()
            allowed_cmds = {
                "ota_start", "ota_chunk", "ota_finish", "ota_abort",
                "get_fota_status", "get_status"
            }
            if cmd_name not in allowed_cmds:
                log(f"[{self.slot_id}] 维护期间拦截未授权指令: {cmd_name}")
                return False

            msg_job_id = cmd_obj.get("job_id")
            if not msg_job_id and isinstance(cmd_obj.get("data"), dict):
                msg_job_id = cmd_obj["data"].get("job_id")
            if not msg_job_id and isinstance(cmd_obj.get("params"), dict):
                msg_job_id = cmd_obj["params"].get("job_id")

            active_job = getattr(self, "maintenance_job", None) or {}
            active_job_id = active_job.get("job_id")
            if not active_job_id or not msg_job_id or str(msg_job_id).strip() != str(active_job_id).strip():
                log(f"[{self.slot_id}] 维护期间拦截 job_id 不匹配指令: cmd={cmd_name}, msg_job_id={msg_job_id}, active_job_id={active_job_id}")
                return False

            mode = active_job.get("mode")
            phase = active_job.get("phase")
            if mode == "script":
                if phase != "confirming" or cmd_name != "get_status":
                    log(f"[{self.slot_id}] script 维护期间非 confirming 或非 get_status 指令被拦截: cmd={cmd_name}, phase={phase}")
                    return False
            elif mode != "sota":
                log(f"[{self.slot_id}] 未知维护模式被拦截: mode={mode}")
                return False

        with self.serial_lock:
            if not self.ser or not self.ser.is_open:
                return False
            try:
                self.ser.write((clean_line + "\r\n").encode("utf-8"))
                self.ser.flush()
                return True
            except Exception as e:
                log(f"[{self.slot_id} | {self.port}] 写入串口异常: {e}")
                try:
                    if self.ser:
                        self.ser.close()
                except Exception:
                    pass
                self.ser = None
                self.is_connected = False
                self.meta["online"] = False
                return False

    def ack_push(self, msg_id: str, status: str = "ok"):
        """定向向本板回写 Push ACK 确认"""
        if not msg_id:
            return
        ack_pkt = json.dumps({"type": "cmd", "id": f"ack_{int(time.time()*1000)}", "cmd": "notify_ack", "data": {"id": msg_id, "status": status}})
        self.send_line(ack_pkt)

    def claim_push(self, msg_id: str, timeout: float = 4.0) -> str:
        """Wait for the board's response to this exact claim before any HTTP send."""
        req_id = "notify_claim_" + uuid.uuid4().hex
        pending = {"message_id": msg_id, "event": threading.Event(), "result": "unknown"}
        with self._claim_lock:
            self._claim_waiters[req_id] = pending
        packet = json.dumps({"type": "cmd", "id": req_id, "cmd": "notify_ack",
                             "data": {"id": msg_id, "status": "handled"}})
        try:
            if not self.send_line(packet):
                return "unavailable"
            pending["event"].wait(timeout)
            return pending["result"]
        finally:
            with self._claim_lock:
                self._claim_waiters.pop(req_id, None)

    def on_claim_response(self, obj: Dict[str, Any]):
        if obj.get("type") not in ("res", "response"):
            return
        with self._claim_lock:
            pending = self._claim_waiters.get(obj.get("id"))
            if not pending:
                return
            data = obj.get("data") or {}
            if not isinstance(data, dict) or data.get("id") != pending["message_id"]:
                return
            pending["result"] = "claimed" if obj.get("code") == 0 and obj.get("msg") == "NOTIFY_CLAIMED" else "expired"
            pending["event"].set()

    def _probe_companion_fingerprint_once(self, force: bool = False):
        """若固件响应未包含 imei/iccid，通过伴生端口安全探测真机指纹 (IMEI 与当前 SIM 的 ICCID)"""
        if getattr(self, "is_flashing", False) or getattr(self, "maintenance_job", None) is not None:
            return
        if not force and self.meta.get("imei") and is_valid_iccid(self.meta.get("iccid")):
            return
        now = time.time()
        last_attempt = getattr(self, "_last_companion_attempt", 0)
        if not force and (now - last_attempt < 2.0):  # 2 秒冷却防频繁
            return
        self._last_companion_attempt = now
        try:
            fps = probe_dongle_fingerprint_from_companion(self.loc, self.port)
            if fps.get("imei"):
                self.meta["imei"] = fps["imei"]
                log(f"[{self.slot_id}] 伴生端口探测成功补齐机身号 IMEI: {fps['imei']}")
                if self.loc and hasattr(self.hub, "session_pool"):
                    self.hub.session_pool.fingerprint_cache[self.loc] = fps["imei"]
            if fps.get("iccid") and is_valid_iccid(fps["iccid"]):
                self.meta["iccid"] = fps["iccid"]
                log(f"[{self.slot_id}] 伴生端口探测成功补齐卡号 ICCID: {fps['iccid']}")
        except Exception as e:
            log(f"[{self.slot_id}] 伴生端口探测异常: {e}")
        finally:
            self._companion_probed = True

    def pause_for_flash(self, job: Optional[Dict[str, Any]] = None):
        """挂起物理串口以供烧录或固件更新占用 (不再发送 AT/复位)"""
        if job is not None:
            self.maintenance_job = copy.deepcopy(job)
        elif hasattr(self.hub, "bind_update_session"):
            self.hub.bind_update_session(self)
        self.is_flashing = True
        mode = (self.maintenance_job or {}).get("mode", "script")
        if mode in ("script", "full"):
            self._serial_io_paused = True
            with self.serial_lock:
                if self.ser and self.ser.is_open:
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                self.ser = None
                self.is_connected = False
                self.meta["online"] = False
        else:
            self._serial_io_paused = False
        log(f"[{self.slot_id}] pause_for_flash (mode={mode}) 物理串口已挂起避让")

    def resume_after_flash(self):
        """烧录完成，重置挂起状态并重新打开物理串口恢复轮询"""
        self.is_flashing = False
        self._serial_io_paused = False
        self.maintenance_job = None
        if hasattr(self.hub, "bind_update_session"):
            self.hub.bind_update_session(self)
        self._ensure_serial_opened()
        log(f"[{self.slot_id}] resume_after_flash 物理串口已恢复轮询")

    def _ensure_serial_opened(self) -> bool:
        if hasattr(self.hub, "bind_update_session"):
            self.hub.bind_update_session(self)
        if getattr(self, "_serial_io_paused", False):
            return False
        with self.serial_lock:
            if self.ser and self.ser.is_open:
                try:
                    _ = self.ser.in_waiting
                    if not self.is_connected:
                        self.is_connected = True
                        self.meta["online"] = True
                        self._companion_probed = False
                        log(f"[{self.slot_id}] 物理串口连接正常: {self.port}")
                        # 模组初次连接或重新连接上线时，强制探测一次以防插拔换卡导致 ICCID 残留旧值
                        self._probe_companion_fingerprint_once(force=True)
                    return True
                except Exception:
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                    self.ser = None
                    self.is_connected = False
                    self.meta["online"] = False

            try:
                self.ser = serial.Serial(self.port, self.baud, timeout=0.5)
                self.ser.dtr = True
                self.ser.rts = True
                self.is_connected = True
                self.meta["online"] = True
                self._companion_probed = False
                log(f"[{self.slot_id}] 成功打开物理串口: {self.port} @ {self.baud}")
                self._probe_companion_fingerprint_once(force=True)
                return True
            except Exception as e:
                self.ser = None
                self.is_connected = False
                self.meta["online"] = False
                self._companion_probed = False
                return False

    def _session_loop(self):
        """会话专属的读写循环，处理 NDJSON 解包、粘包和断线重连"""
        log(f"[{self.slot_id}] 会话守护线程已启动 (目标端口: {self.port})")
        has_probed = False

        while self.running:
            if not self._ensure_serial_opened():
                has_probed = False
                time.sleep(1.0)
                continue

            if not has_probed:
                has_probed = True
                time.sleep(0.2)
                init_pkt: Dict[str, Any] = {"type": "cmd", "id": f"init_{self.slot_id}", "cmd": "get_status"}
                if self.maintenance_job and self.maintenance_job.get("job_id"):
                    j_id = self.maintenance_job["job_id"]
                    init_pkt["job_id"] = j_id
                    init_pkt["data"] = {"job_id": j_id}
                self.send_line(json.dumps(init_pkt))
                time.sleep(1.0)
                continue

            try:
                # 状态探针与自愈：维护与普通模式分流
                now = time.time()
                if self.ser and self.ser.is_open:
                    is_tx_busy = (now < getattr(self, "_sms_tx_busy_until", 0.0))
                    if not is_tx_busy:
                        if self.maintenance_job:
                            if self.maintenance_job.get("phase") == "confirming":
                                if now - getattr(self, "_last_status_poll", 0.0) >= 4.0:
                                    self._last_status_poll = now
                                    j_id = self.maintenance_job.get("job_id")
                                    poll_pkt = {
                                        "type": "cmd",
                                        "id": f"poll_{self.slot_id}",
                                        "cmd": "get_status",
                                        "job_id": j_id,
                                        "data": {"job_id": j_id}
                                    }
                                    self.send_line(json.dumps(poll_pkt))
                        else:
                            is_active = self.hub.is_in_active_mode() if (self.hub and hasattr(self.hub, "is_in_active_mode")) else True
                            heartbeat_interval = 5.0 if is_active else 30.0
                            need_poll = not getattr(self, "is_flashing", False) and (
                                (self.meta.get("bsp") in ("Unknown", "", None)) or 
                                (now - getattr(self, "_last_status_poll", 0.0) >= heartbeat_interval)
                            )
                            if need_poll and (now - getattr(self, "_last_poll_send", 0.0) >= 1.5):
                                self._last_poll_send = now
                                self._last_status_poll = now
                                poll_pkt = {"type": "cmd", "id": f"poll_{self.slot_id}", "cmd": "get_status"}
                                self.send_line(json.dumps(poll_pkt))

                line_bytes = b""
                if self.ser and self.ser.is_open:
                    line_bytes = self.ser.readline()
                else:
                    time.sleep(0.5)
                    continue

                if not line_bytes:
                    time.sleep(0.05)
                    continue

                self.rx_buffer.extend(line_bytes)
                if len(self.rx_buffer) > 64 * 1024:
                    self.rx_buffer.clear()
                    log(f"[{self.slot_id}] 串口半帧超过 64 KiB，已丢弃缓冲")
                    continue

                while b"\n" in self.rx_buffer:
                    line_bytes, _, remaining = self.rx_buffer.partition(b"\n")
                    self.rx_buffer = bytearray(remaining)
                    try:
                        line = line_bytes.rstrip(b"\r").decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(obj, dict):
                        continue

                    # 1. 为上报报文打上专属卡槽与端口元数据标签
                    obj["slot"] = self.slot_id
                    obj["port"] = self.port
                    if isinstance(obj.get("data"), dict):
                        obj["data"]["slot"] = self.slot_id
                        obj["data"]["port"] = self.port

                    # Wake a matching notification claim before any potentially
                    # slow companion-port identity probe in state refresh.
                    self.on_claim_response(obj)

                    # 2. 更新本会话内部状态
                    self._update_session_state(obj)

                    # 3. 提交给 Hub 统一流转与分发
                    self.hub.on_session_frame(self, obj, json.dumps(obj, ensure_ascii=False))

            except (serial.SerialException, OSError) as e:
                log(f"[{self.slot_id}] 物理串口发生瞬断: {e}")
                with self.serial_lock:
                    if self.ser:
                        try:
                            self.ser.close()
                        except Exception:
                            pass
                        self.ser = None
                    self.is_connected = False
                    self.meta["online"] = False
                    self._companion_probed = False
                time.sleep(1.0)
            except Exception as e:
                log(f"[{self.slot_id}] 会话未捕获异常: {e}")
                time.sleep(0.5)

    def _update_session_state(self, obj: Dict[str, Any]):
        """从板端报文中提取最新状态指标"""
        frame_type = obj.get("type")
        evt = obj.get("event")
        data = obj.get("data")
        if not isinstance(data, dict):
            return

        is_status_frame = frame_type in ("response", "res") or (frame_type == "event" and evt in ("status", "state_change", "gateway_ready"))
        if is_status_frame:
            self.latest_status.update(data)
            if "bsp" in data and data["bsp"]: self.meta["bsp"] = data["bsp"]
            if "version" in data and data["version"]: self.meta["version"] = data["version"]
            if "imei" in data and data["imei"]: self.meta["imei"] = data["imei"]

            # 随时清理不符合电信标准的非法脏卡号
            if self.meta.get("iccid") and not is_valid_iccid(self.meta.get("iccid")):
                self.meta["iccid"] = ""

            raw_iccid = data.get("iccid")
            if raw_iccid and is_valid_iccid(raw_iccid):
                self.meta["iccid"] = str(raw_iccid).strip().rstrip("Ff")
            elif data.get("sim_ready") is False:
                self.meta["iccid"] = ""
            elif "iccid" in data and (not raw_iccid or not is_valid_iccid(raw_iccid)):
                self.meta["iccid"] = ""
            elif not raw_iccid and not data.get("net_ready", False) and (data.get("csq") or 0) == 0:
                self.meta["iccid"] = ""

            if "number" in data and data["number"]: self.meta["phone"] = data["number"]
            if "phone" in data and data["phone"]: self.meta["phone"] = data["phone"]
            if "csq" in data and data["csq"] is not None:
                try: self.meta["csq"] = int(data["csq"])
                except Exception: pass
            if "rsrp" in data and data["rsrp"] is not None:
                try: self.meta["rsrp"] = int(data["rsrp"])
                except Exception: pass
            if "temp" in data and data["temp"] is not None: self.meta["temp"] = str(data["temp"])
            if "vbat" in data and data["vbat"] is not None: self.meta["vbat"] = str(data["vbat"])
            if "uptime_seconds" in data: self.meta["uptime"] = data["uptime_seconds"]
            elif "uptime" in data: self.meta["uptime"] = data["uptime"]
            if "blackbox_count" in data: self.meta["sms_count"] = data["blackbox_count"]
            elif "sms_count" in data: self.meta["sms_count"] = data["sms_count"]
            if "net_ready" in data: self.meta["net_ready"] = bool(data["net_ready"])
            if "cellular_data" in data: self.board_cellular_data = bool(data["cellular_data"])
            if "rndis" in data: self.meta["rndis"] = bool(data["rndis"])
            elif "rndis_enable" in data: self.meta["rndis"] = bool(data["rndis_enable"])
            if "capabilities" in data and isinstance(data["capabilities"], dict):
                self.meta["capabilities"] = data["capabilities"]
            if "boot_id" in data:
                self.meta["boot_id"] = data["boot_id"]
            elif "boot_id" in obj:
                self.meta["boot_id"] = obj["boot_id"]
            if "build_id" in data:
                self.meta["build_id"] = data["build_id"]
            elif "build_id" in obj:
                self.meta["build_id"] = obj["build_id"]
            if "serial_ota" in data and isinstance(data["serial_ota"], dict):
                self.meta["serial_ota"] = data["serial_ota"]
            elif "serial_ota" in obj and isinstance(obj["serial_ota"], dict):
                self.meta["serial_ota"] = obj["serial_ota"]

            # 严格设备身份更新门卫：只有 data 同时有非空且非 unknown 的 imei/model/chip/core_version 原生字符串时更新
            v_imei = data.get("imei")
            v_model = data.get("model")
            v_chip = data.get("chip")
            v_core = data.get("core_version")

            def _is_valid_identity_str(val: Any) -> bool:
                return isinstance(val, str) and bool(val.strip()) and val.strip().lower() != "unknown"

            has_valid_id = (
                _is_valid_identity_str(v_imei) and
                _is_valid_identity_str(v_model) and
                _is_valid_identity_str(v_chip) and
                _is_valid_identity_str(v_core)
            )

            if has_valid_id:
                self.meta["reported_imei"] = v_imei.strip()
                self.meta["reported_model"] = v_model.strip()
                self.meta["reported_chip"] = v_chip.strip()
                self.meta["core_version"] = v_core.strip()
                self.meta["_identity_seen_at"] = time.time()
                if hasattr(self.hub, "bind_update_session"):
                    self.hub.bind_update_session(self)
            elif any(k in data for k in ("imei", "model", "chip", "core_version")):
                self.meta["reported_imei"] = ""
                self.meta["reported_model"] = ""
                self.meta["reported_chip"] = ""
                self.meta["core_version"] = ""
                self.meta["_identity_seen_at"] = 0.0

            self.meta["online"] = True

            # 若未完成伴生口首次探测且关键信息缺失，触发伴生口探测补充（单次生命周期仅触发一次）
            if not getattr(self, "_companion_probed", False):
                if not self.meta.get("imei") or not is_valid_iccid(self.meta.get("iccid")):
                    self._probe_companion_fingerprint_once(force=False)

            # 板端自省型号与 BSP 原样透传 (Zero Guesswork)
            bsp_val = str(data.get("bsp") or "").strip()
            mod_val = str(data.get("model") or "").strip()
            if bsp_val:
                self.meta["bsp"] = bsp_val
            if mod_val:
                self.meta["model"] = mod_val
            elif bsp_val and not self.meta.get("model"):
                self.meta["model"] = bsp_val

            # 若固件响应包含 chip，记录至 meta
            chip_val = str(data.get("chip") or (data.get("capabilities", {}) or {}).get("chip", "")).strip()
            if chip_val and chip_val.lower() != "unknown":
                self.meta["chip"] = chip_val

            # 若为初始状态探测帧，主动广播最新的集群卡槽汇总
            if str(obj.get("id", "")).startswith("init_"):
                self.hub.broadcast_json({
                    "type": "event",
                    "event": "cluster_status",
                    "slot": self.slot_id,
                    "data": {"slots": self.hub.session_pool.list_all_summaries()}
                })

        elif frame_type == "event":
            evt = obj.get("event")
            if evt == "gateway_ready":
                if "bsp" in data and data["bsp"]:
                    self.meta["bsp"] = str(data["bsp"]).strip()
                if "model" in data and data["model"]:
                    self.meta["model"] = str(data["model"]).strip()
                elif self.meta.get("bsp") and not self.meta.get("model"):
                    self.meta["model"] = self.meta["bsp"]
                if "version" in data: self.meta["version"] = data["version"]
                if "imei" in data and data["imei"]: self.meta["imei"] = data["imei"]
                raw_gw_iccid = data.get("iccid")
                if raw_gw_iccid and is_valid_iccid(raw_gw_iccid):
                    self.meta["iccid"] = str(raw_gw_iccid).strip().rstrip("Ff")
                elif "iccid" in data and (not raw_gw_iccid or not is_valid_iccid(raw_gw_iccid)):
                    self.meta["iccid"] = ""
                if "capabilities" in data:
                    self.meta["capabilities"] = data["capabilities"]
                    if isinstance(data["capabilities"], dict) and data["capabilities"].get("chip"):
                        chip_cand = str(data["capabilities"]["chip"]).strip()
                        if chip_cand.lower() != "unknown":
                            self.meta["chip"] = chip_cand
            elif evt == "state_change":
                if "csq" in data: self.meta["csq"] = data["csq"]
                if "rsrp" in data: self.meta["rsrp"] = data["rsrp"]
                if "temp" in data: self.meta["temp"] = str(data["temp"])
                if "vbat" in data: self.meta["vbat"] = str(data["vbat"])
                if "cellular_data" in data: self.board_cellular_data = bool(data["cellular_data"])

    def get_summary(self) -> Dict[str, Any]:
        """获取当前会话的对外简要看板信息"""
        active_job_info = None
        if self.maintenance_job:
            active_job_info = {
                "job_id": self.maintenance_job.get("job_id"),
                "device_id": self.maintenance_job.get("device_id"),
                "mode": self.maintenance_job.get("mode"),
                "phase": self.maintenance_job.get("phase"),
                "package_id": self.maintenance_job.get("package_id"),
                "started_at": self.maintenance_job.get("started_at"),
                "updated_at": self.maintenance_job.get("updated_at"),
                "result": self.maintenance_job.get("result"),
                "error": self.maintenance_job.get("error"),
            }
        out_iccid = self.meta.get("iccid", "")
        if out_iccid and not is_valid_iccid(out_iccid):
            out_iccid = ""

        return {
            "slot": self.slot_id,
            "port": self.port,
            "loc": self.loc,
            "model": self.meta.get("model", "Air780"),
            "bsp": self.meta.get("bsp", ""),
            "chip": self.meta.get("reported_chip") or self.meta.get("chip", ""),
            "core_version": self.meta.get("core_version", ""),
            "boot_id": self.meta.get("boot_id"),
            "build_id": self.meta.get("build_id"),
            "serial_ota": self.meta.get("serial_ota"),
            "imei": self.meta.get("imei", ""),
            "iccid": out_iccid,
            "phone": self.meta.get("phone", ""),
            "version": self.meta.get("version", ""),
            "online": self.is_connected,
            "net_ready": self.meta.get("net_ready", False),
            "csq": self.meta.get("csq", 0),
            "rsrp": self.meta.get("rsrp", 0),
            "temp": self.meta.get("temp", ""),
            "vbat": self.meta.get("vbat", ""),
            "uptime": self.meta.get("uptime", 0),
            "sms_count": self.meta.get("sms_count", 0),
            "rndis": bool(self.meta.get("rndis", False)),
            "cellular_data": self.board_cellular_data,
            "capabilities": self.meta.get("capabilities", {}),
            "active_job": active_job_info
        }


# =========================================================================
# 核心类：DongleSessionPool (1~N 模组动态会话池管理器)
# =========================================================================

class DongleSessionPool:
    """
    负责 1~N 个模组的自动探测、卡槽分配 (slot_1..N)、会话增删及命令路由。
    """
    def __init__(self, hub: 'GatewayHub'):
        self.hub = hub
        self.sessions: Dict[str, DongleSession] = {}  # port -> DongleSession
        self.pool_lock = threading.RLock()
        self.running = False
        self.scanner_thread: Optional[threading.Thread] = None

        # 插拔检测与低功耗事件唤醒 (AIR-64)
        self._scan_wake_event = threading.Event()
        self._last_disconnect_time: float = 0.0

        # 卡槽历史记忆 (以 loc 或 port 为 key，确保拔插后分配同一卡槽)
        self.slot_history: Dict[str, str] = {}
        # 模组硬件指纹缓存池 (拔插/复位不丢 IMEI/ICCID)
        self.fingerprint_cache: Dict[str, Dict[str, str]] = {}
        # 未分配/出厂态模组识别列表 (AIR-35)
        self.unassigned_dongles: List[Dict[str, Any]] = []
        self._last_unassigned_broadcast: float = 0.0

    def start(self):
        self.running = True
        # 初始同步扫描一次
        self._sync_ports()
        self.scanner_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self.scanner_thread.start()

    def stop(self):
        self.running = False
        self.wake_scan()
        with self.pool_lock:
            for s in list(self.sessions.values()):
                s.stop()
            self.sessions.clear()

    def wake_scan(self):
        """外部唤醒插拔扫描循环 (AIR-64)"""
        if hasattr(self, "_scan_wake_event"):
            self._scan_wake_event.set()

    def _allocate_slot(self, port: str, loc: str) -> str:
        """为新发现的端口分配稳定卡槽 ID（拓扑亲和性，防止卡槽漂移倒错）"""
        with self.pool_lock:
            used_slots = set(s.slot_id for s in self.sessions.values())

            # 1. 物理拓扑亲和性：1-8 固定为主机卡槽 slot_1，1-13 固定为拓展 Hub 卡槽 slot_2
            if loc:
                clean_loc = loc.split(":")[0] if ":" in loc else loc
                if clean_loc.startswith("1-8") and "slot_1" not in used_slots:
                    if loc: self.slot_history[loc] = "slot_1"
                    self.slot_history[port] = "slot_1"
                    return "slot_1"
                elif clean_loc.startswith("1-13") and "slot_2" not in used_slots:
                    if loc: self.slot_history[loc] = "slot_2"
                    self.slot_history[port] = "slot_2"
                    return "slot_2"

            # 2. 优先复用历史分配记录
            hist = self.slot_history.get(loc) or self.slot_history.get(port)
            if hist and hist not in used_slots:
                return hist

            # 3. 贪心寻找最小未被使用的 slot_1, slot_2, ...
            idx = 1
            while True:
                candidate = f"slot_{idx}"
                if candidate not in used_slots:
                    if loc: self.slot_history[loc] = candidate
                    self.slot_history[port] = candidate
                    return candidate
                idx += 1

    def _sync_ports(self):
        """执行一次全量串口扫描并执行增量会话同步 (AIR-64: 单次硬件设备树扫描复用)"""
        try:
            raw_ports = list(serial.tools.list_ports.comports())
        except Exception as e:
            log(f"获取系统串口列表异常: {e}")
            raw_ports = []

        detected_list = scan_all_cellular_ports(ports_list=raw_ports)
        detected_ports = set(d["port"] for d in detected_list)

        with self.pool_lock:
            current_ports = set(self.sessions.keys())

            # 发现新接入端口
            for d in detected_list:
                p = d["port"]
                if p not in current_ports:
                    loc = d["loc"]
                    slot_id = self._allocate_slot(p, loc)
                    session = DongleSession(p, loc, slot_id, self.hub)
                    cached_imei = self.fingerprint_cache.get(loc) or self.fingerprint_cache.get(p)
                    if cached_imei and not session.meta.get("imei"):
                        session.meta["imei"] = cached_imei
                    self.sessions[p] = session
                    if hasattr(self.hub, "bind_update_session"):
                        self.hub.bind_update_session(session)
                    session.start()
                    log(f"⚡ [CLUSTER] 发现新卡板上线: {p} (位置: {loc}) -> 分配卡槽: 【{slot_id}】")
                    # 广播模组连接事件
                    self.hub.broadcast_json({
                        "type": "event",
                        "event": "dongle_connected",
                        "slot": slot_id,
                        "data": session.get_summary()
                    })
                else:
                    existing_sess = self.sessions.get(p)
                    if existing_sess and hasattr(self.hub, "bind_update_session"):
                        self.hub.bind_update_session(existing_sess)

            # 检测拔出断开端口
            for p in list(current_ports):
                if p not in detected_ports:
                    session = self.sessions.pop(p)
                    slot_id = session.slot_id
                    self._last_disconnect_time = time.time()
                    session.stop()
                    log(f"⚠️ [CLUSTER] 检测到卡板断开拔出: {p} (曾用卡槽: 【{slot_id}】)")
                    # 广播模组断开事件
                    self.hub.broadcast_json({
                        "type": "event",
                        "event": "dongle_disconnected",
                        "slot": slot_id,
                        "data": {"slot": slot_id, "port": p, "online": False}
                    })

            # AIR-35: 探测未绑定的移芯出厂态/Bootloader 端口 (全量安全嗅探，无竞争，复用单次枚举)
            try:
                self._sniff_unassigned_dongles(current_ports, ports_list=raw_ports)
            except Exception as e:
                log(f"未分配模组探测异常: {e}")

    def _is_port_in_maintenance(self, p: Any) -> bool:
        """纯枚举当前端口 USB 身份检查 store.active，占用或故障均跳过"""
        loc = (getattr(p, "location", "") or "").strip()
        ser = (getattr(p, "serial_number", "") or "").strip()
        port_id: Dict[str, Any] = {"port": getattr(p, "device", "") or ""}
        if loc:
            port_id["usb_location"] = loc
        if ser:
            port_id["usb_serial"] = ser
        if hasattr(self.hub, "update_jobs"):
            try:
                active_job = self.hub.update_jobs.active(port_id)
                if active_job is not None:
                    return True
            except Exception as e:
                log(f"[sniff] update_jobs 异常或故障，判定为占用避让 {getattr(p, 'device', '')}: {e}")
                return True

        # 检查未分配设备维护租约 (AIR-44 防并发嗅探干扰)
        if hasattr(self.hub, "_unassigned_leases") and self.hub._unassigned_leases:
            dev_name = getattr(p, "device", "") or ""
            lease_key = f"unassigned_{dev_name}"
            lease = self.hub._unassigned_leases.get(lease_key)
            if lease and (time.time() - lease.get("started_at", 0) < 120.0):
                return True

        return False

    def _sniff_unassigned_dongles(self, bound_ports: set, ports_list: Optional[List[Any]] = None):
        """
        AIR-35: 扫描系统上未绑定到 DongleSession 的移芯/合宙 4G 模组端口
        识别出厂标准 AT 固件 (19D1) 或 ROM Bootloader 态 (17D1)
        """
        unassigned = []
        all_coms = ports_list if ports_list is not None else list(serial.tools.list_ports.comports())
        
        # 1. 寻找未绑定的 Bootloader (17D1:0001)
        for p in all_coms:
            if self._is_port_in_maintenance(p):
                continue
            hwid = (p.hwid or "").upper()
            vid = p.vid
            pid = p.pid
            is_boot = (vid == 0x17D1 and pid == 0x0001) or "17D1:0001" in hwid or ("17D1" in (hex(vid or 0).upper()) and "0001" in (hex(pid or 0).upper()))
            if is_boot and p.device not in bound_ports:
                unassigned.append({
                    "port": p.device,
                    "desc": p.description or "Air780 Bootloader",
                    "mode": "bootloader",
                    "chip": "unknown",
                    "model_guess": "合宙移芯模组 (刷机模式)",
                    "recommend_flash": "full"
                })

        # 2. 寻找未绑定到会话池的 19D1 端口（如全新裸板插上，出厂仅有 AT 固件，或用户通信口 x.6 未运行智能网关固件）
        active_session_loc_prefixes = set()
        for s in self.sessions.values():
            if s.loc and ":" in s.loc:
                active_session_loc_prefixes.add(s.loc.split(":")[0])

        for p in all_coms:
            dev = p.device
            if dev in bound_ports:
                continue
            if self._is_port_in_maintenance(p):
                continue
            hwid = (p.hwid or "").upper()
            vid = hex(p.vid or 0).upper()
            pid = hex(p.pid or 0).upper()
            loc = getattr(p, "location", "") or ""
            
            # 若是已在线卡槽的伴生口 (x.2 / x.4)，跳过不作为全新模块报出
            loc_prefix = loc.split(":")[0] if (loc and ":" in loc) else ""
            if loc_prefix and loc_prefix in active_session_loc_prefixes:
                continue

            # 仅嗅探 19D1 的 AT 控制口 (x.2) 或主通信口 (x.6)
            if ("19D1:0001" in hwid or ("19D1" in vid and "0001" in pid)):
                if loc.endswith("x.2") or ":X.2" in loc.upper() or loc.endswith("x.6") or ":X.6" in loc.upper() or "MI_02" in hwid or "MI_06" in hwid:
                    # 轻量下发 ATI 测试是否为出厂 AT 态 (支持 Air780EPM, Air780EPV/EP, Air780E/EC)
                    model = "移芯模组 (出厂 AT 态)"
                    chip = "ec718pv"
                    try:
                        with serial.Serial(dev, baudrate=115200, timeout=0.3) as ser:
                            ser.write(b"ATI\r\n")
                            ser.flush()
                            time.sleep(0.1)
                            raw = ser.read(ser.in_waiting or 256).decode("utf-8", errors="ignore")
                            if "Air780EPM" in raw or "EC718PM" in raw or "780EPM" in raw:
                                model = "合宙 Air780EPM (出厂 AT 固件)"
                                chip = "ec718pm"
                            elif "Air780EP" in raw or "EC718PV" in raw or "EC718" in raw:
                                model = "合宙 Air780EPV/EP (出厂 AT 固件)"
                                chip = "ec718pv"
                            elif "Air780E" in raw or "Air780EC" in raw or "EC618" in raw:
                                model = "合宙 Air780E/EC (出厂 AT 固件)"
                                chip = "ec618"
                            elif "Air700E" in raw:
                                model = "合宙 Air700E (出厂 AT 固件)"
                                chip = "ec618"
                    except Exception:
                        pass

                    unassigned.append({
                        "port": dev,
                        "desc": p.description,
                        "mode": "factory_at",
                        "chip": chip,
                        "recommend_chip": chip,
                        "model_guess": model,
                        "recommend_flash": "full"
                    })

        with self.pool_lock:
            self.unassigned_dongles = unassigned

        # 若发现新设备且距离上次广播超过 3 秒，广播事件通知前端
        now = time.time()
        if unassigned and (now - self._last_unassigned_broadcast > 3.0):
            self._last_unassigned_broadcast = now
            self.hub.broadcast_json({
                "type": "event",
                "event": "unassigned_dongles_detected",
                "data": unassigned
            })

    def get_unassigned_dongles(self) -> List[Dict[str, Any]]:
        with self.pool_lock:
            return list(self.unassigned_dongles)

    def _scan_loop(self):
        """后台轮询扫描，感知热插拔 (AIR-64 自适应能耗退避与可中断等待)"""
        while self.running:
            try:
                self._sync_ports()
            except Exception as e:
                log(f"会话池扫描循环异常: {e}")

            is_active = self.hub.is_in_active_mode() if (self.hub and hasattr(self.hub, "is_in_active_mode")) else False
            has_recent_disconnect = (time.time() - getattr(self, "_last_disconnect_time", 0.0) < 15.0)

            # 1. 若处于活动态、或刚刚发生拔卡断开、或当前 0 设备在线等待接入：保持 3.0 秒敏捷感知
            # 2. 若处于低功耗守护态且既有卡槽健康在线：平滑退避至 15.0 秒低能耗巡检
            sleep_time = 3.0 if (is_active or has_recent_disconnect or len(self.sessions) == 0) else 15.0
            self._scan_wake_event.wait(timeout=sleep_time)
            self._scan_wake_event.clear()

    def get_session(self, target: Optional[str] = None, active_only: bool = True) -> Optional[DongleSession]:
        """
        根据 slot_id (如 'slot_1') 或 port (如 'COM8') 查找活跃会话。
        【防串台铁律】：若显式指定了 target，未找到或不在线时必须严格返回 None，绝对不可降级回退到 slot_1 或其他卡槽！
        仅当 target 为空/None 时，才允许缺省匹配：优先 slot_1，其次首个可用在线会话。
        """
        with self.pool_lock:
            if target and str(target).strip():
                target_str = str(target).strip()
                # 尝试匹配 slot
                for s in self.sessions.values():
                    if s.slot_id.lower() == target_str.lower():
                        return s if (not active_only or s.is_connected) else None
                # 尝试匹配 port
                for s in self.sessions.values():
                    if s.port.upper() == target_str.upper():
                        return s if (not active_only or s.is_connected) else None
                # 显式指定的目标不存在，直接返回 None，杜绝跨卡槽串台！
                return None

            # 缺省降级匹配（仅在未显式指定 target 时生效）：优先 slot_1
            for s in self.sessions.values():
                if s.slot_id == "slot_1" and (not active_only or s.is_connected):
                    return s
            # 否则取任意在线设备
            for s in self.sessions.values():
                if not active_only or s.is_connected:
                    return s
        return None

    def list_all_summaries(self) -> List[Dict[str, Any]]:
        with self.pool_lock:
            sorted_sessions = sorted(self.sessions.values(), key=lambda s: s.slot_id)
            return [s.get_summary() for s in sorted_sessions]


# =========================================================================
# 维护任务存储：UpdateJobStore (AIR-38 S04A)
# =========================================================================

class UpdateJobStore:
    """Hub 维护任务存储与原子状态机 (AIR-38 S04A)"""
    VALID_PHASES = {
        "preflight", "receiving", "verifying", "writing",
        "rebooting", "confirming", "succeeded", "failed", "uncertain"
    }
    TERMINAL_PHASES = {"succeeded", "failed"}

    @staticmethod
    def _reject_duplicate_keys(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise ValueError(f"Duplicate key in JSON: {k}")
            d[k] = v
        return d

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.lock = threading.RLock()
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self._fault: Optional[str] = None
        self._load()

    @staticmethod
    def _normalize_location(loc: Any) -> Optional[str]:
        if not isinstance(loc, str):
            return None
        loc = loc.strip()
        if not loc:
            return None
        if ":" in loc:
            loc = loc.split(":", 1)[0].strip()
        return loc if loc else None

    @staticmethod
    def _get_serial(ser: Any) -> Optional[str]:
        if not isinstance(ser, str):
            return None
        ser = ser.strip()
        return ser if ser else None

    def _is_usb_match(self, id1: Dict[str, Any], id2: Dict[str, Any]) -> bool:
        loc1 = self._normalize_location(id1.get("usb_location"))
        loc2 = self._normalize_location(id2.get("usb_location"))
        ser1 = self._get_serial(id1.get("usb_serial"))
        ser2 = self._get_serial(id2.get("usb_serial"))

        if loc1 is not None and loc2 is not None:
            if loc1 != loc2:
                return False
            if ser1 is not None and ser2 is not None:
                return ser1 == ser2
            return True

        if ser1 is not None and ser2 is not None:
            return ser1 == ser2

        return False

    def _is_identity_conflict(self, id1: Dict[str, Any], id2: Dict[str, Any]) -> bool:
        imei1 = id1.get("imei")
        imei2 = id2.get("imei")
        if imei1 and imei2 and imei1 == imei2:
            return True
        dev1 = id1.get("device_id")
        dev2 = id2.get("device_id")
        if dev1 and dev2 and dev1 == dev2:
            return True
        return self._is_usb_match(id1, id2)

    def _validate_stored_job(self, job: Dict[str, Any]) -> Optional[str]:
        required_fields = (
            "job_id", "device_id", "owner", "mode", "phase", "identity",
            "package_id", "expected", "started_at", "result", "error",
            "effect_started", "process_stopped", "updated_at"
        )
        for field in required_fields:
            if field not in job:
                return f"missing field '{field}'"

        if not isinstance(job["job_id"], str) or not job["job_id"].strip():
            return "invalid job_id"
        if not isinstance(job["device_id"], str) or not job["device_id"].strip():
            return "invalid device_id"
        if not isinstance(job["owner"], str):
            return "invalid owner"
        if job["mode"] not in ("sota", "script"):
            return f"invalid mode '{job['mode']}'"
        if job["phase"] not in self.VALID_PHASES:
            return f"invalid phase '{job['phase']}'"
        if not isinstance(job["package_id"], str) or len(job["package_id"]) != 64 or not re.fullmatch(r"[0-9a-f]{64}", job["package_id"]):
            return "invalid package_id"
        if not isinstance(job["started_at"], (int, float)):
            return "invalid started_at"
        if not isinstance(job["updated_at"], (int, float)):
            return "invalid updated_at"
        if job["result"] is not None and not isinstance(job["result"], str):
            return "invalid result"
        if not isinstance(job["error"], str):
            return "invalid error"
        if not isinstance(job["effect_started"], bool):
            return "invalid effect_started"
        if not isinstance(job["process_stopped"], bool):
            return "invalid process_stopped"
        if job["phase"] in ("succeeded", "failed"):
            if not job["process_stopped"]:
                return f"terminal phase '{job['phase']}' requires process_stopped to be true"
            if job["phase"] == "succeeded" and not job["effect_started"]:
                return "phase 'succeeded' requires effect_started to be true"

        expected = job["expected"]
        if not isinstance(expected, dict):
            return "expected must be dict"
        for k in ("version", "build_id", "old_boot_id"):
            if k not in expected:
                return f"expected missing '{k}'"
        if not isinstance(expected["version"], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected["version"]):
            return "invalid expected.version"
        if not isinstance(expected["build_id"], str) or len(expected["build_id"]) != 64 or not re.fullmatch(r"[0-9a-f]{64}", expected["build_id"]):
            return "invalid expected.build_id"
        if expected["old_boot_id"] is not None and not isinstance(expected["old_boot_id"], str):
            return "invalid expected.old_boot_id"

        identity = job["identity"]
        if not isinstance(identity, dict):
            return "identity must be dict"
        for k in ("imei", "device_id", "model", "chip", "core_version", "port"):
            v = identity.get(k)
            if not isinstance(v, str) or not v.strip():
                return f"identity missing/empty '{k}'"
            if k in ("model", "chip", "core_version") and v.strip().lower() == "unknown":
                return f"identity.{k} cannot be unknown"
        if job["device_id"] != identity.get("device_id"):
            return "device_id does not match identity.device_id"
        loc = identity.get("usb_location")
        ser = identity.get("usb_serial")
        has_loc = isinstance(loc, str) and bool(loc.strip())
        has_ser = isinstance(ser, str) and bool(ser.strip())
        if not (has_loc or has_ser):
            return "identity must have non-empty usb_location or usb_serial"
        if loc is not None and (not isinstance(loc, str) or not loc.strip()):
            return "invalid usb_location in identity"
        if ser is not None and (not isinstance(ser, str) or not ser.strip()):
            return "invalid usb_serial in identity"

        return None

    def _load(self):
        with self.lock:
            if not os.path.exists(self.path):
                self.jobs = {}
                return

            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception as e:
                self._fault = f"Failed to read update jobs file: {e}"
                return

            if not content.strip():
                self._fault = "Empty update jobs file"
                return

            try:
                data = json.loads(content, object_pairs_hook=self._reject_duplicate_keys)
            except Exception as e:
                self._fault = f"Bad JSON in update jobs file: {e}"
                return

            if not isinstance(data, dict) or set(data.keys()) != {"jobs"} or not isinstance(data["jobs"], dict):
                self._fault = "Invalid schema in update jobs file: expected {'jobs': dict}"
                return

            raw_jobs = data["jobs"]
            parsed_jobs = {}
            for k, job in raw_jobs.items():
                if not isinstance(k, str) or not isinstance(job, dict):
                    self._fault = f"Job record '{k}' is invalid"
                    return
                if k != job.get("job_id"):
                    self._fault = f"Job key '{k}' does not match job_id '{job.get('job_id')}'"
                    return
                err = self._validate_stored_job(job)
                if err:
                    self._fault = f"Stored job '{k}' validation failed: {err}"
                    return
                parsed_jobs[job["job_id"]] = job

            has_uncompleted = False
            for job in parsed_jobs.values():
                if job.get("phase") not in self.TERMINAL_PHASES:
                    job["phase"] = "uncertain"
                    job["result"] = "uncertain"
                    job["process_stopped"] = False
                    job["updated_at"] = time.time()
                    has_uncompleted = True

            self.jobs = parsed_jobs
            if has_uncompleted:
                try:
                    self._persist_locked()
                except Exception as e:
                    self._fault = f"Failed to persist recovered jobs: {e}"

    def _persist_locked(self):
        dir_name = os.path.dirname(self.path) or "."
        temp_path = os.path.join(dir_name, f".{os.path.basename(self.path)}.{uuid.uuid4().hex}.tmp")
        try:
            payload = {
                "jobs": self.jobs
            }
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.path)
        except Exception as e:
            self._fault = f"Persistence failure: {e}"
            raise

    def reserve(self, job_id: str, identity: Dict[str, Any], mode: str,
                package_id: str, expected: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if self._fault:
                raise RuntimeError(f"UpdateJobStore fault: {self._fault}")

            if not isinstance(job_id, str) or not job_id.strip():
                raise ValueError("job_id must be non-empty str")

            if mode not in ("sota", "script"):
                raise ValueError("mode must be 'sota' or 'script'")

            if not isinstance(package_id, str) or len(package_id) != 64 or not re.fullmatch(r"[0-9a-f]{64}", package_id):
                raise ValueError("package_id must be 64-character lowercase hex string")

            if not isinstance(expected, dict):
                raise ValueError("expected must be a dict")
            for k in ("version", "build_id", "old_boot_id"):
                if k not in expected:
                    raise ValueError(f"expected missing '{k}'")
            version = expected["version"]
            if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                raise ValueError("expected.version must be three-part ASCII numeric version (e.g. 1.0.0)")
            build_id = expected["build_id"]
            if not isinstance(build_id, str) or len(build_id) != 64 or not re.fullmatch(r"[0-9a-f]{64}", build_id):
                raise ValueError("expected.build_id must be 64-character lowercase hex string")
            old_boot_id = expected["old_boot_id"]
            if old_boot_id is not None and not isinstance(old_boot_id, str):
                raise ValueError("expected.old_boot_id must be str or None")

            if not isinstance(identity, dict):
                raise ValueError("identity must be a dict")
            for k in ("imei", "device_id", "model", "chip", "core_version", "port"):
                v = identity.get(k)
                if not isinstance(v, str) or not v.strip():
                    raise ValueError(f"identity.{k} must be non-empty str")
                if k in ("model", "chip", "core_version") and v.strip().lower() == "unknown":
                    raise ValueError(f"identity.{k} cannot be unknown")
            loc = identity.get("usb_location")
            ser = identity.get("usb_serial")
            has_loc = isinstance(loc, str) and bool(loc.strip())
            has_ser = isinstance(ser, str) and bool(ser.strip())
            if not (has_loc or has_ser):
                raise ValueError("identity must have non-empty usb_location or usb_serial")
            if loc is not None and (not isinstance(loc, str) or not loc.strip()):
                raise ValueError("identity.usb_location must be non-empty str if present")
            if ser is not None and (not isinstance(ser, str) or not ser.strip()):
                raise ValueError("identity.usb_serial must be non-empty str if present")

            if job_id in self.jobs:
                existing = self.jobs[job_id]
                if (existing.get("mode") == mode and
                    existing.get("package_id") == package_id and
                    existing.get("identity") == identity and
                    existing.get("expected") == expected):
                    return copy.deepcopy(existing)
                raise ValueError(f"Job {job_id} already exists with different parameters")

            for existing in self.jobs.values():
                if existing.get("phase") in self.TERMINAL_PHASES:
                    continue
                if self._is_identity_conflict(identity, existing.get("identity", {})):
                    raise RuntimeError(f"Device or USB binding busy with active job {existing.get('job_id')}")

            now = time.time()
            job = {
                "job_id": job_id,
                "device_id": identity["device_id"],
                "owner": "hub",
                "mode": mode,
                "phase": "preflight",
                "identity": copy.deepcopy(identity),
                "package_id": package_id,
                "expected": copy.deepcopy(expected),
                "started_at": now,
                "updated_at": now,
                "result": None,
                "error": "",
                "effect_started": False,
                "process_stopped": True,
            }

            self.jobs[job_id] = job
            self._persist_locked()
            return copy.deepcopy(job)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            if self._fault:
                raise RuntimeError(f"UpdateJobStore fault: {self._fault}")
            if not isinstance(job_id, str):
                return None
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return copy.deepcopy(job)

    def active(self, identity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.lock:
            if self._fault:
                raise RuntimeError(f"UpdateJobStore fault: {self._fault}")
            if not isinstance(identity, dict):
                raise ValueError("identity must be a dict")

            matches = []
            for job in self.jobs.values():
                if job.get("phase") in self.TERMINAL_PHASES:
                    continue
                if self._is_identity_conflict(identity, job.get("identity", {})):
                    matches.append(job)

            if len(matches) > 1:
                raise RuntimeError(f"Multiple active jobs conflict with identity: {[j['job_id'] for j in matches]}")
            if len(matches) == 1:
                return copy.deepcopy(matches[0])
            return None

    def transition(self, job_id: str, device_id: str, phase: str, *,
                   effect_started: Optional[bool] = None,
                   process_stopped: Optional[bool] = None,
                   error: str = "",
                   confirmed: bool = False) -> Dict[str, Any]:
        with self.lock:
            if self._fault:
                raise RuntimeError(f"UpdateJobStore fault: {self._fault}")

            if effect_started is not None and not isinstance(effect_started, bool):
                raise ValueError("effect_started must be None or bool")
            if process_stopped is not None and not isinstance(process_stopped, bool):
                raise ValueError("process_stopped must be None or bool")
            if not isinstance(confirmed, bool):
                raise ValueError("confirmed must be bool")

            if job_id not in self.jobs:
                raise KeyError(f"Job {job_id} not found")

            job = self.jobs[job_id]
            if job.get("device_id") != device_id:
                raise ValueError(f"device_id mismatch: job {job_id} belongs to {job.get('device_id')}, got {device_id}")

            if phase not in self.VALID_PHASES:
                raise ValueError(f"Invalid phase '{phase}'")

            if job.get("phase") in self.TERMINAL_PHASES:
                raise RuntimeError(f"Job {job_id} is already terminal ({job.get('phase')}) and cannot be changed")

            cur_effect = bool(job.get("effect_started", False))
            if effect_started is True:
                new_effect = True
            else:
                new_effect = cur_effect

            if phase in ("receiving", "writing", "rebooting"):
                new_effect = True

            cur_stopped = bool(job.get("process_stopped", True))
            if process_stopped is not None:
                new_stopped = process_stopped
            else:
                new_stopped = cur_stopped

            if phase == "succeeded":
                if new_stopped is True and confirmed is True and job.get("phase") == "confirming" and new_effect is True:
                    final_phase = "succeeded"
                else:
                    final_phase = "uncertain"
            elif phase == "failed":
                if new_stopped is True and ((not new_effect) or confirmed is True):
                    final_phase = "failed"
                else:
                    final_phase = "uncertain"
            elif phase == "uncertain":
                final_phase = "uncertain"
            else:
                final_phase = phase

            job["phase"] = final_phase
            if final_phase in self.TERMINAL_PHASES:
                job["result"] = final_phase
            elif final_phase == "uncertain":
                job["result"] = "uncertain"
            job["effect_started"] = new_effect
            job["process_stopped"] = new_stopped
            if error:
                job["error"] = str(error)
            job["updated_at"] = time.time()

            self._persist_locked()
            return copy.deepcopy(job)


# =========================================================================
# 核心类：GatewayHub (网关多路共享中枢与广播总线)
# =========================================================================

class NotifyJournal:
    """Durable ownership/results ledger; never stores message content or credentials."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.entries = {}
        self.fault = False
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    raise ValueError("invalid journal")
                self.entries = loaded
            except (OSError, ValueError):
                self.fault = True

    def _persist(self):
        next_path = self.path + ".next"
        with open(next_path, "w", encoding="utf-8") as f:
            json.dump(self.entries, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(next_path, self.path)

    def begin(self, key: str, slot: str, msg_id: str) -> str:
        with self.lock:
            if self.fault:
                return "fault"
            if key in self.entries:
                return "duplicate"
            self.entries[key] = {"slot": slot, "message_id": msg_id, "state": "seen",
                                 "channels": {}, "updated_at": time.time()}
            try:
                self._persist()
            except OSError:
                self.entries.pop(key, None)
                self.fault = True
                return "fault"
            return "new"

    def record(self, key: str, state: str = None, channel: str = None, result: str = None):
        with self.lock:
            entry = self.entries.get(key)
            if not entry or self.fault:
                return False
            if state:
                entry["state"] = state
            if channel:
                entry["channels"][channel] = result
            entry["updated_at"] = time.time()
            try:
                self._persist()
                return True
            except OSError:
                self.fault = True
                return False

    def recent(self):
        with self.lock:
            return sorted(self.entries.values(), key=lambda e: e.get("updated_at", 0), reverse=True)[:100]


class GatewayHub:
    def __init__(self, host: str = HUB_HOST, port: int = HUB_PORT, com: Optional[str] = None, baud: int = SERIAL_BAUD):
        self.host = host
        self.port = port
        self.preferred_com = com
        self.preferred_baud = baud
        self.running = False
        self.server_sock: Optional[socket.socket] = None
        self.clients: List[socket.socket] = []
        self.clients_lock = threading.Lock()
        self.serial_paused = False

        # 维护任务存储 (AIR-38 S04A)
        self.update_jobs = UpdateJobStore(os.path.join(DATA_DIR, "update_jobs.json"))
        self.notify_journal = NotifyJournal(os.path.join(DATA_DIR, "notify_results.json"))

        # 动态会话池
        self.session_pool = DongleSessionPool(self)

        # 集群健康监控与智能出站路由器 (AIR-22)
        self.health_monitor = ClusterHealthMonitor()
        self.router = ClusterRouter(self.session_pool, self.health_monitor)

        # 缓存与配置
        self.latest_otp: Optional[Dict[str, Any]] = None
        self.recent_sms: List[Dict[str, Any]] = []
        self.auto_copy_otp: bool = True
        self.mcp_config: Dict[str, Any] = {"enabled": False, "port": 17800}
        self.notify_config: Dict[str, Any] = self._load_notify_config()
        self.state_lock = threading.Lock()

        # 本地会话免检令牌 (方案 D: 内部免检信任环，仅保存在内存与本地文件中)
        self.internal_session_token = str(uuid.uuid4().hex)
        self._save_session_token()
        # 记录各客户端 socket 属性: { sock: {"source": "web"|"mcp", "authenticated": bool} }
        self.client_meta: Dict[socket.socket, Dict[str, Any]] = {}

        # 活跃度看门狗与低功耗模式支持 (AIR-64)
        self.last_active_time: float = time.time()
        self.active_sse_count: int = 0

    def touch_activity(self, active_sse_count: Optional[int] = None):
        """刷新中枢交互活跃时间，可同步更新活跃 SSE 监听数 (AIR-64)"""
        self.last_active_time = time.time()
        if active_sse_count is not None:
            self.active_sse_count = max(0, int(active_sse_count))
        if hasattr(self, "session_pool") and self.session_pool:
            self.session_pool.wake_scan()

    def is_in_active_mode(self) -> bool:
        """
        判定当前是否处于活动交互态 (AIR-64):
        若有浏览器 Web 页面打开中 (active_sse_count > 0) 或 30s 内有用户/API 交互，判定为活动态；
        否则判定为低功耗静默守护态 (Eco Daemon Mode)。
        """
        now = time.time()
        is_web_open = (getattr(self, "active_sse_count", 0) > 0)
        is_recently_active = (now - getattr(self, "last_active_time", 0.0) < 30.0)
        return is_web_open or is_recently_active

    def _save_session_token(self):
        """将内部免检令牌安全写入宿主本地数据目录"""
        try:
            token_path = os.path.join(DATA_DIR, ".hub_session_token")
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(self.internal_session_token)
        except Exception as e:
            log(f"写入 .hub_session_token 异常: {e}")

    def capture_update_identity(self, slot: str) -> Dict[str, Any]:
        """显式卡槽捕获最新准确设备身份与唯一 USB 物理拓扑 (AIR-38 S04B)"""
        if not slot or not isinstance(slot, str) or not slot.strip():
            raise ValueError("slot must be non-empty string")
        slot_str = slot.strip()
        session = self.session_pool.get_session(slot_str, active_only=True)
        if not session:
            raise RuntimeError(f"Slot '{slot_str}' has no active session or is disconnected")

        now = time.time()
        seen_at = float(session.meta.get("_identity_seen_at") or 0.0)
        if not seen_at or (now - seen_at > 15.0):
            raise RuntimeError(f"Device on slot '{slot_str}' has no complete identity frame within 15 seconds")

        reported_imei = session.meta.get("reported_imei")
        reported_model = session.meta.get("reported_model")
        reported_chip = session.meta.get("reported_chip")
        core_version = session.meta.get("core_version")

        for name, val in [("imei", reported_imei), ("model", reported_model), ("chip", reported_chip), ("core_version", core_version)]:
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"Device on slot '{slot_str}' missing or empty {name}")
            if val.strip().lower() == "unknown":
                raise ValueError(f"Device on slot '{slot_str}' {name} cannot be unknown")

        imei = reported_imei.strip()
        device_id = f"imei:{imei}"
        model = reported_model.strip()
        chip = reported_chip.strip()
        raw_core = str(core_version)

        all_coms = list(serial.tools.list_ports.comports())
        matching_ports = [p for p in all_coms if (getattr(p, "device", "") or "").upper() == session.port.upper()]
        if len(matching_ports) != 1:
            raise RuntimeError(f"Session port '{session.port}' does not uniquely match in comports (found {len(matching_ports)})")
        cur_p = matching_ports[0]

        cur_loc = (getattr(cur_p, "location", "") or "").strip()
        cur_ser = (getattr(cur_p, "serial_number", "") or "").strip()

        sess_loc = (session.loc or "").strip()
        if sess_loc and cur_loc:
            norm_sess = UpdateJobStore._normalize_location(sess_loc)
            norm_cur = UpdateJobStore._normalize_location(cur_loc)
            if norm_sess and norm_cur and norm_sess != norm_cur:
                raise RuntimeError(f"Session port '{session.port}' USB root mismatch: '{norm_cur}' != '{norm_sess}'")

        if not (cur_loc or cur_ser):
            raise RuntimeError(f"Session port '{session.port}' has neither usb_location nor usb_serial")

        cur_root = UpdateJobStore._normalize_location(cur_loc)
        ctrl_candidates = []
        for p in all_coms:
            p_dev = getattr(p, "device", "") or ""
            if not p_dev or p_dev.upper() == session.port.upper():
                continue
            p_loc = (getattr(p, "location", "") or "").strip()
            p_ser = (getattr(p, "serial_number", "") or "").strip()
            p_hwid = (getattr(p, "hwid", "") or "").upper()
            p_root = UpdateJobStore._normalize_location(p_loc)

            is_same_usb = False
            if cur_root and p_root:
                if cur_root == p_root:
                    if cur_ser and p_ser:
                        is_same_usb = (cur_ser == p_ser)
                    else:
                        is_same_usb = True
            elif cur_ser and p_ser:
                is_same_usb = (cur_ser == p_ser)

            if not is_same_usb:
                continue

            p_loc_upper = p_loc.upper()
            if p_loc_upper.endswith("X.2") or ":X.2" in p_loc_upper or p_loc_upper.endswith(".2") or "MI_02" in p_hwid:
                ctrl_candidates.append(p_dev)

        control_port = ctrl_candidates[0] if len(ctrl_candidates) == 1 else ""

        identity: Dict[str, Any] = {
            "slot": session.slot_id,
            "port": session.port,
            "imei": imei,
            "device_id": device_id,
            "model": model,
            "chip": chip,
            "core_version": raw_core,
        }
        if cur_loc:
            identity["usb_location"] = cur_loc
        if cur_ser:
            identity["usb_serial"] = cur_ser
        if control_port:
            identity["control_port"] = control_port
        else:
            identity["control_port"] = ""

        for k in ("boot_id", "build_id", "version", "serial_ota"):
            v = session.meta.get(k)
            if v is not None and v != "":
                identity[k] = v

        return identity

    def bind_update_session(self, session: DongleSession) -> Optional[Dict[str, Any]]:
        """纯枚举当前 port USB 身份与真实 reported_imei，绑定或重算更新维护态 (AIR-38 S04B)"""
        matching_ports = []
        try:
            for p in serial.tools.list_ports.comports():
                dev = getattr(p, "device", "") or ""
                if dev.upper() == session.port.upper():
                    matching_ports.append(p)
        except Exception as e:
            log(f"[{session.slot_id}] bind_update_session 枚举串口异常: {e}")
            session.maintenance_job = None
            session.is_flashing = True
            session._serial_io_paused = True
            with session.serial_lock:
                if session.ser:
                    try:
                        session.ser.close()
                    except Exception:
                        pass
                    session.ser = None
                session.is_connected = False
                session.meta["online"] = False
            return None

        if len(matching_ports) > 1:
            log(f"[{session.slot_id}] bind_update_session 发现重复同名COM口: {session.port}")
            session.maintenance_job = None
            session.is_flashing = True
            session._serial_io_paused = True
            with session.serial_lock:
                if session.ser:
                    try:
                        session.ser.close()
                    except Exception:
                        pass
                    session.ser = None
                session.is_connected = False
                session.meta["online"] = False
            return None

        if len(matching_ports) == 0:
            session.is_flashing = True
            session._serial_io_paused = True
            with session.serial_lock:
                if session.ser:
                    try:
                        session.ser.close()
                    except Exception:
                        pass
                    session.ser = None
                session.is_connected = False
                session.meta["online"] = False
            return copy.deepcopy(session.maintenance_job) if session.maintenance_job else None

        p = matching_ports[0]
        cur_loc = (getattr(p, "location", "") or "").strip()
        cur_ser = (getattr(p, "serial_number", "") or "").strip()

        query_id: Dict[str, Any] = {"port": session.port}
        if cur_loc:
            query_id["usb_location"] = cur_loc
        if cur_ser:
            query_id["usb_serial"] = cur_ser

        reported_imei = str(session.meta.get("reported_imei") or "").strip()
        if reported_imei and reported_imei.lower() != "unknown":
            query_id["imei"] = reported_imei
            query_id["device_id"] = f"imei:{reported_imei}"

        matched_job = None
        store_fault = False
        try:
            if hasattr(self, "update_jobs"):
                matched_job = self.update_jobs.active(query_id)
        except Exception as e:
            log(f"[{session.slot_id}] bind_update_session 查询 active 任务异常/故障: {e}")
            store_fault = True

        if store_fault:
            session.maintenance_job = None
            session.is_flashing = True
            session._serial_io_paused = True
            with session.serial_lock:
                if session.ser:
                    try:
                        session.ser.close()
                    except Exception:
                        pass
                    session.ser = None
                session.is_connected = False
                session.meta["online"] = False
            return None

        if matched_job is not None:
            job_identity = matched_job.get("identity") or {}
            job_imei = str(job_identity.get("imei") or "").strip()
            if not job_imei:
                job_dev = str(matched_job.get("device_id") or "").strip()
                if job_dev.startswith("imei:"):
                    job_imei = job_dev[5:].strip()

            if reported_imei and job_imei and reported_imei != job_imei:
                log(f"[{session.slot_id}] 命中任务 {matched_job.get('job_id')} 但 reported_imei '{reported_imei}' 与任务 imei '{job_imei}' 不一致，禁止 IO")
                session.maintenance_job = None
                session.is_flashing = True
                session._serial_io_paused = True
                with session.serial_lock:
                    if session.ser:
                        try:
                            session.ser.close()
                        except Exception:
                            pass
                        session.ser = None
                    session.is_connected = False
                    session.meta["online"] = False
                return None

            session.maintenance_job = copy.deepcopy(matched_job)
            session.is_flashing = True
            mode = matched_job.get("mode")
            phase = matched_job.get("phase")
            if mode == "script" and phase != "confirming":
                session._serial_io_paused = True
                with session.serial_lock:
                    if session.ser:
                        try:
                            session.ser.close()
                        except Exception:
                            pass
                        session.ser = None
                    session.is_connected = False
                    session.meta["online"] = False
            else:
                session._serial_io_paused = False

            return copy.deepcopy(matched_job)

        session.maintenance_job = None
        session.is_flashing = False
        session._serial_io_paused = False
        return None

    def _load_notify_config(self, strict: bool = False) -> Dict[str, Any]:
        """加载通知配置"""
        json_path = GATEWAY_CONFIG_PATH
        cfg = {
            "system": {"auto_copy_otp": True},
            "feishu": {"enable": 0, "url": "", "secret": ""},
            "wecom": {"enable": 0, "url": ""},
            "dingtalk": {"enable": 0, "url": "", "secret": ""},
            "bark": {"enable": 0, "url": "", "group": "Air780Gateway", "sound": "minuet"},
            "webhook": {"enable": 0, "url": "", "method": "POST"},
            "mcp": {"enabled": False, "port": 17800}
        }
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    raise ValueError("配置根节点必须是对象")
                for k, v in loaded.items():
                    if k in cfg and isinstance(v, dict):
                        cfg[k].update(v)
                    elif k == "system" and isinstance(v, dict):
                        cfg["system"].update(v)
                    elif k == "mcp" and isinstance(v, dict):
                        cfg["mcp"].update(v)
                self.auto_copy_otp = bool(cfg.get("system", {}).get("auto_copy_otp", True))
                self.mcp_config = dict(cfg.get("mcp", {"enabled": False, "port": 17800}))
                enabled_list = [ch for ch, item in cfg.items() if ch not in ("system", "mcp") and item.get("enable") in (1, True, "1") and item.get("url")]
                mcp_on = self.mcp_config.get("enabled", False)
                log(f"成功加载网关配置: 自动复制验证码={self.auto_copy_otp}, MCP物理开关={mcp_on}, 已启用渠道={enabled_list}")
                return cfg
            except Exception as e:
                log(f"解析 gateway_config.json 异常: {type(e).__name__}")
                if strict:
                    raise
        return cfg

    def reload_notify_config(self) -> Dict[str, Any]:
        with self.state_lock:
            new_config = self._load_notify_config(strict=True)
            self.notify_config = new_config
            self.auto_copy_otp = bool(self.notify_config.get("system", {}).get("auto_copy_otp", True))
            self.mcp_config = dict(self.notify_config.get("mcp", {"enabled": False, "port": 17800}))
            log(f"通知与MCP配置热重载完成 (自动复制: {self.auto_copy_otp}, MCP开启: {self.mcp_config.get('enabled', False)})")
            return dict(self.notify_config)

    def start(self):
        # 1. 尝试绑定本地 TCP 端口（单例独占保护）
        try:
            self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            elif os.name != "nt":
                self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_sock.bind((self.host, self.port))
            self.server_sock.listen(15)
        except OSError as e:
            log(f"端口 {self.host}:{self.port} 已被占用，已有 Hub 实例运行，本进程安全退出。({e})")
            sys.exit(0)

        self.running = True
        log(f"多设备网关中枢启动就绪，监听 IPC 端口: {self.host}:{self.port}")

        # 2. 启动动态串口会话池
        self.session_pool.start()

        # 3. 启动 TCP 监听主循环
        self._tcp_server_loop()

    def stop(self):
        """停止 Hub 服务及所有卡槽会话"""
        self.running = False
        try:
            if self.server_sock:
                self.server_sock.close()
        except Exception:
            pass
        with self.clients_lock:
            for c in list(self.clients):
                try:
                    c.close()
                except Exception:
                    pass
            self.clients.clear()
        if self.session_pool:
            self.session_pool.stop()

    def broadcast_text(self, text: str, is_sms_event: bool = False):
        """向所有在线客户端广播纯文本行 (需自带 \\n)；若为短信事件且 MCP 未开启，则阻断向 MCP 广播"""
        data = text.encode("utf-8")
        mcp_enabled = bool(self.mcp_config.get("enabled", False))
        with self.clients_lock:
            dead = []
            for c in self.clients:
                # 若为实时短信事件且 MCP 物理开关关闭，严禁向外部 MCP 客户端广播明文短信 (方案 D 隐私防线)
                if is_sms_event and not mcp_enabled:
                    meta = self.client_meta.get(c, {})
                    if not (meta.get("authenticated") and meta.get("source") == "web"):
                        continue
                try:
                    c.sendall(data)
                except Exception:
                    dead.append(c)
            for dc in dead:
                self.clients.remove(dc)
                self.client_meta.pop(dc, None)
                try:
                    dc.close()
                except Exception:
                    pass

    def broadcast_json(self, obj: Dict[str, Any]):
        """向所有在线客户端广播格式化 JSON"""
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        is_sms = obj.get("type") == "event" and obj.get("event") in ("sms_rx", "sms_received")
        self.broadcast_text(line, is_sms_event=is_sms)

    def on_session_frame(self, session: DongleSession, obj: Dict[str, Any], raw_line: str):
        """会话上报 NDJSON 帧的总线接收入口"""
        session.on_claim_response(obj)
        frame_type = obj.get("type")
        evt = obj.get("event")
        data = obj.get("data") or {}

        # 1. 拦截与提取验证码 (OTP)
        if frame_type == "event" and evt == "sms_rx":
            code = data.get("code")

            if code:
                with self.state_lock:
                    self.latest_otp = {
                        "code": code,
                        "slot": session.slot_id,
                        "from": data.get("from"),
                        "content": data.get("content"),
                        "time": obj.get("ts", int(time.time()))
                    }
                # 构造大白话业务卡标签 (AIR-54)
                ident = self._resolve_slot_identity(session, data)
                slot_label = ident["display_tag"]
                sys_cfg = self.notify_config.get("system", {}) if isinstance(self.notify_config, dict) else {}
                toast_enabled = bool(sys_cfg.get("desktop_notification", True))
                privacy_on = bool(sys_cfg.get("privacy_mode", False))

                if getattr(self, "auto_copy_otp", True):
                    if set_windows_clipboard(code):
                        log(f"⚡ [CLIPBOARD] 验证码 {slot_label} [{code}] 已自动存入 Windows 剪贴板")
                        if toast_enabled:
                            if privacy_on:
                                dispatch_notification("数字蜂巢 · 验证码", "⚡ 收到登录验证码 (已存入剪贴板，直接按 Ctrl+V 粘贴)", privacy=True)
                            else:
                                dispatch_notification("数字蜂巢 · 验证码", f"⚡ {slot_label} 捕获验证码：{code} (已存入剪贴板，直接按 Ctrl+V 粘贴)")
                else:
                    log(f"⚡ [OTP] 捕获验证码 {slot_label} [{code}]")
                    if toast_enabled:
                        if privacy_on:
                            dispatch_notification("数字蜂巢 · 验证码", "⚡ 收到登录验证码 (点击进入控制台查看)", privacy=True)
                        else:
                            dispatch_notification("数字蜂巢 · 验证码", f"⚡ {slot_label} 捕获验证码：{code}")
            else:
                # 纯文本非验证码普通短信弹窗
                sys_cfg = self.notify_config.get("system", {}) if isinstance(self.notify_config, dict) else {}
                if sys_cfg.get("desktop_notification", True):
                    ident = self._resolve_slot_identity(session, data)
                    sender = data.get("from") or "未知发件人"
                    content_snippet = (data.get("content") or "").replace("\n", " ")[:40]
                    privacy_on = bool(sys_cfg.get("privacy_mode", False))
                    dispatch_notification(f"📩 {ident['display_tag']} 收到新短信", f"发件人: {sender}\n{content_snippet}", privacy=privacy_on)

        # 2. 触发宿主宽带代推
        if frame_type == "event" and evt in ("sms_rx", "call_rx", "gateway_ready", "state_change"):
            self._dispatch_host_proxy_push(session, evt, data)

        # 3. 注入会话所属 slot 元数据并广播给全量客户端
        if "slot" not in obj:
            obj["slot"] = session.slot_id
        if isinstance(data, dict) and "slot" not in data:
            data["slot"] = session.slot_id

        # 4. 更新集群健康监控状态机 (AIR-22)
        try:
            if frame_type == "event":
                if evt in ("state_change", "gateway_ready"):
                    csq = data.get("csq")
                    if csq is not None:
                        self.health_monitor.update_csq(session.slot_id, int(csq))
                elif evt == "sms_sent":
                    ok = bool(data.get("ok", True))
                    if ok:
                        self.health_monitor.record_send_success(session.slot_id)
                    else:
                        err = str(data.get("error") or data.get("msg") or "SMS_SEND_FAILED")
                        self.health_monitor.record_send_failure(session.slot_id, err)
            elif frame_type == "res" and isinstance(data, dict):
                csq = data.get("csq")
                if csq is not None:
                    self.health_monitor.update_csq(session.slot_id, int(csq))
        except Exception as e:
            log(f"[{session.slot_id}] 健康状态机更新异常: {e}")

        broadcast_line = json.dumps(obj, ensure_ascii=False)
        is_sms = frame_type == "event" and evt in ("sms_rx", "sms_received")
        self.broadcast_text(broadcast_line + "\n", is_sms_event=is_sms)

    def _resolve_slot_identity(self, session: DongleSession, data: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """
        统一解析业务卡身份元数据，输出 4 级全自动优雅降级的大白话人话标签 (AIR-54)
        L1: 运营商 + 尾号 (例: 【中国联通 3879】)
        L2: 运营商 + 卡槽 (例: 【中国移动 (卡槽 1)】)
        L3: 纯手机号 + 尾号 (例: 【手机卡 3879】)
        L4: 纯卡槽 + 型号兜底 (例: 【卡槽 2 · Air780EPM】)
        """
        data = data or {}
        meta = getattr(session, "meta", {}) or {}

        # 1. 槽位中文名与端口
        raw_slot = str(getattr(session, "slot_id", "") or meta.get("slot", "") or data.get("slot", "")).lower()
        m_slot = re.search(r"slot_?(\d+)", raw_slot)
        slot_num = m_slot.group(1) if m_slot else raw_slot.replace("slot_", "").replace("slot", "")
        slot_cn = f"卡槽 {slot_num}" if slot_num else (raw_slot or "卡槽")

        port = str(getattr(session, "port", "") or meta.get("port", "") or data.get("port", "") or "").strip()
        port_suffix = f" · {port}" if port else ""

        model = str(meta.get("model") or getattr(session, "model", "") or data.get("model") or data.get("bsp") or "Air780").replace("合宙", "").strip() or "Air780"
        imei = str(meta.get("imei") or data.get("imei") or "").strip()

        # 2. 手机号码安全清洗与尾号截取
        raw_phone = meta.get("phone") or getattr(session, "phone", "") or data.get("formatted_number") or data.get("number") or ""
        clean_num = clean_phone_number(raw_phone) if raw_phone else ""
        phone_digits = re.sub(r"\D", "", clean_num or str(raw_phone))
        if phone_digits.startswith("86") and len(phone_digits) == 13:
            phone_digits = phone_digits[2:]

        phone_masked = ""
        phone_tail = ""
        if len(phone_digits) >= 11:
            phone_masked = f"{phone_digits[:3]}****{phone_digits[-4:]}"
            phone_tail = phone_digits[-4:]
        elif 7 <= len(phone_digits) < 11:
            phone_masked = f"{phone_digits[:2]}****{phone_digits[-2:]}"
            phone_tail = phone_digits[-4:]
        elif phone_digits:
            phone_masked = phone_digits
            phone_tail = ""

        # 3. 运营商推导 (三级全自动优先级 · 严格复用 cluster_router)
        carrier_full = ""
        carrier_short = ""

        # 优先级 1: ICCID 算法查表
        iccid = str(meta.get("iccid") or data.get("iccid") or "").strip()
        if iccid:
            c_code = detect_sim_carrier(iccid)
            if c_code in CARRIER_NAME_MAP:
                carrier_full, carrier_short = CARRIER_NAME_MAP[c_code]

        # 优先级 2: 手机号段推导 (复用 detect_phone_carrier)
        if not carrier_full and phone_digits:
            c_code = detect_phone_carrier(phone_digits)
            if c_code in CARRIER_NAME_MAP:
                carrier_full, carrier_short = CARRIER_NAME_MAP[c_code]

        # 优先级 3: 短信内容头部签名或客服号码特征
        if not carrier_full:
            sender = str(data.get("from") or "")
            content = str(data.get("content") or "")
            clean_s = clean_phone_number(sender)
            if clean_s == "10010" or sender in ("10010", "+8610010") or "【中国联通】" in content:
                carrier_full, carrier_short = "中国联通", "联通"
            elif clean_s == "10086" or sender in ("10086", "+8610086") or "【中国移动】" in content:
                carrier_full, carrier_short = "中国移动", "移动"
            elif clean_s == "10000" or sender in ("10000", "+8610000") or "【中国电信】" in content:
                carrier_full, carrier_short = "中国电信", "电信"
            elif clean_s == "10099" or sender in ("10099", "+8610099") or "【中国广电】" in content:
                carrier_full, carrier_short = "中国广电", "广电"

        # 4. 4 级优雅降级合成 display_tag
        if carrier_full and phone_tail:
            display_tag = f"【{carrier_full} {phone_tail}】"
        elif carrier_full:
            display_tag = f"【{carrier_full} ({slot_cn})】"
        elif phone_tail:
            display_tag = f"【手机卡 {phone_tail}】"
        else:
            display_tag = f"【{slot_cn} · {model}】"

        device_desc = f"[{slot_cn}{port_suffix}] {model}"

        return {
            "slot_id": getattr(session, "slot_id", "") or raw_slot,
            "slot_cn": slot_cn,
            "port": port,
            "model": model,
            "imei": imei,
            "carrier": carrier_full,
            "carrier_short": carrier_short,
            "phone_masked": phone_masked,
            "phone_tail": phone_tail,
            "display_tag": display_tag,
            "device_desc": device_desc,
        }

    def _dispatch_host_proxy_push(self, session: DongleSession, event_type: str, data: Dict[str, Any]):
        """借用宿主电脑本地宽带优先代推全渠道通知，并向模组回送 Push ACK 握手回执"""
        # 只要宿主在线，无论板端是否开启蜂窝数据，一律由宿主电脑本地宽带优先代推（杜绝消耗 SIM 流量）

        def _format_phone_number(raw_num: Any) -> str:
            if not raw_num: return "未知"
            m = re.search(r"(?:\+86)?(\d{11})", str(raw_num))
            return f"{m.group(1)} +86" if m else str(raw_num)

        ident = self._resolve_slot_identity(session, data)
        display_tag = ident["display_tag"]
        device_desc = ident["device_desc"]
        imei_part = f" · IMEI: {ident['imei']}" if ident["imei"] else ""
        dev_desc = f"{device_desc}{imei_part}"

        title = ""
        plain_text = ""
        md_text = ""
        extra_otp = ""
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")

        if event_type == "sms_rx":
            sender = data.get("from", "未知")
            content = data.get("content", "")
            code = data.get("code", "")
            extra_otp = code or ""
            is_otp = bool(extra_otp)
            title_icon = "🔑" if is_otp else "📩"
            title_action = "收到短信验证码" if is_otp else "收到新短信"
            title = f"{title_icon} {display_tag} {title_action}"

            otp_str = f"\r\n🔑 提取验证码: 【{code}】" if code else ""
            otp_md = f"\n> **提取验证码**: <font color=\"warning\">{code}</font>" if code else ""
            plain_text = f"发件人: {sender}\r\n接收时间: {now_str}\r\n内容: {content}{otp_str}\r\n\r\n设备: {dev_desc} (上位机推送)"
            md_text = f"### {title}\n> **发件人**: {sender}\n> **接收时间**: {now_str}\n> **短信正文**: {content}{otp_md}\n\n> **来源设备**: {dev_desc} (上位机推送)"

        elif event_type == "call_rx":
            sender = data.get("from", "未知号码")
            is_fota = bool(data.get("fota_trigger"))
            if is_fota:
                title = f"⚡ {display_tag} 识别暗号呼叫：激活空中更新"
                plain_text = f"呼入号码: {sender}\r\n接收时间: {now_str}\r\n动作: 识别暗号呼叫，已自动拒接，正在检测空中更新...\r\n\r\n设备: {dev_desc} (上位机推送)"
                md_text = f"### {title}\n> **呼入号码**: {sender}\n> **接收时间**: {now_str}\n> **处理动作**: 识别暗号呼叫，已自动拒接，正在检测空中更新...\n\n> **来源设备**: {dev_desc} (上位机推送)"
            else:
                title = f"📞 {display_tag} 拦截到呼入电话"
                plain_text = f"呼入号码: {sender}\r\n接收时间: {now_str}\r\n动作: 已自动拒接\r\n\r\n设备: {dev_desc} (上位机推送)"
                md_text = f"### {title}\n> **呼入号码**: {sender}\n> **接收时间**: {now_str}\n> **拦截动作**: 已自动拒接\n\n> **来源设备**: {dev_desc} (上位机推送)"

        elif event_type in ("gateway_ready", "state_change"):
            title = f"🚀 {display_tag} 智能网关已上线" if event_type == "gateway_ready" else f"⚙️ {display_tag} 配置状态变更"
            num = _format_phone_number(data.get("formatted_number") or data.get("number") or ident["phone_masked"])
            csq = data.get("csq") if data.get("csq") is not None else "未知"
            rsrp = data.get("rsrp") if data.get("rsrp") is not None else "未知"
            temp = data.get("temp") if data.get("temp") is not None else "未知"
            vbat = data.get("vbat") if data.get("vbat") is not None else "未知"
            ver = data.get("version") if data.get("version") is not None else "未知"
            plain_text = (
                f"设备来源：{dev_desc} (上位机推送)\r\n"
                f"端口路径：{session.port}\r\n"
                f"本机号码：{num}\r\n"
                f"信号强度：CSQ {csq} (RSRP {rsrp} dBm)\r\n"
                f"固件版本：{ver}\r\n"
                f"核心温度：{temp} ℃ | 电压：{vbat} V"
            )
            md_text = f"### {title}\n```\n{plain_text}\n```"
        else:
            return

        msg_id = data.get("id")
        channels_to_send = []

        # 1. 飞书推送
        feishu_cfg = self.notify_config.get("feishu", {})
        if feishu_cfg.get("enable") and feishu_cfg.get("url"):
            card_template = "blue"
            card_title = title
            if event_type == "sms_rx":
                card_template = "orange" if extra_otp else "blue"
                sender = data.get("from", "未知")
                content = data.get("content", "")
                body_elements = [
                    {"tag": "markdown", "content": f"**发件人：** `{sender}`\n**接收时间：** {now_str}"},
                    {"tag": "hr"}
                ]
                if extra_otp:
                    body_elements.extend([
                        {"tag": "markdown", "content": f"**提取验证码：**\n```text\n{extra_otp}\n```"},
                        {"tag": "hr"}
                    ])
                body_elements.append({"tag": "markdown", "content": f"**短信正文：**\n{content}"})
                body_elements.append({"tag": "hr"})
                body_elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"<font color='grey'>来源设备: {dev_desc} (上位机推送)</font>"
                    }
                })
            else:
                body_elements = [{"tag": "markdown", "content": md_text}]
                body_elements.append({"tag": "hr"})
                body_elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"<font color='grey'>来源设备: {dev_desc} (上位机推送)</font>"
                    }
                })

            card_payload = {
                "schema": "2.0",
                "config": {"update_multi": True, "style": {"text_size": {"normal_v2": {"default": "normal", "pc": "normal", "mobile": "heading"}}}},
                "header": {"title": {"tag": "plain_text", "content": card_title}, "template": card_template},
                "body": {"direction": "vertical", "padding": "12px 12px 12px 12px", "elements": body_elements}
            }
            pdict = {"msg_type": "interactive", "card": card_payload}
            secret = (feishu_cfg.get("secret") or "").strip()
            if secret:
                ts = str(int(time.time()))
                sign_str = f"{ts}\n{secret}"
                hmac_code = hmac.new(sign_str.encode("utf-8"), digestmod=hashlib.sha256).digest()
                pdict["timestamp"] = ts
                pdict["sign"] = base64.b64encode(hmac_code).decode("utf-8")
            channels_to_send.append(("feishu", feishu_cfg["url"], pdict))

        # 2. 企业微信
        wecom_cfg = self.notify_config.get("wecom", {})
        if wecom_cfg.get("enable") and wecom_cfg.get("url"):
            channels_to_send.append(("wecom", wecom_cfg["url"], {"msgtype": "markdown", "markdown": {"content": md_text}}))

        # 3. 钉钉
        ding_cfg = self.notify_config.get("dingtalk", {})
        if ding_cfg.get("enable") and ding_cfg.get("url"):
            durl = ding_cfg["url"]
            sec = (ding_cfg.get("secret") or "").strip()
            if sec:
                ts = str(round(time.time() * 1000))
                st = f"{ts}\n{sec}"
                hc = hmac.new(sec.encode("utf-8"), st.encode("utf-8"), digestmod=hashlib.sha256).digest()
                sgn = urllib.parse.quote_plus(base64.b64encode(hc).decode("utf-8"))
                durl = f"{durl}&timestamp={ts}&sign={sgn}" if "?" in durl else f"{durl}?timestamp={ts}&sign={sgn}"
            channels_to_send.append(("dingtalk", durl, {"msgtype": "markdown", "markdown": {"title": title, "text": md_text}}))

        # 4. Bark
        bark_cfg = self.notify_config.get("bark", {})
        if bark_cfg.get("enable") and bark_cfg.get("url"):
            bark_title = f"{title_icon} {display_tag}" if event_type == "sms_rx" else title
            bark_data = {
                "title": bark_title,
                "body": plain_text,
                "group": bark_cfg.get("group", "Air780Gateway"),
                "sound": bark_cfg.get("sound", "minuet")
            }
            if extra_otp:
                bark_data["copy"] = str(extra_otp)
                bark_data["automaticallyCopy"] = "1"
            channels_to_send.append(("bark", bark_cfg["url"], bark_data))

        # 5. 通用 Webhook
        webhook_cfg = self.notify_config.get("webhook", {})
        if webhook_cfg.get("enable") and webhook_cfg.get("url"):
            wh_body = {
                "event": event_type,
                "slot": session.slot_id,
                "port": session.port,
                "model": ident["model"],
                "imei": ident["imei"],
                "carrier": ident["carrier"],
                "phone": ident["phone_masked"],
                "phone_tail": ident["phone_tail"],
                "display_tag": ident["display_tag"],
                "device_desc": dev_desc,
                "timestamp": int(time.time()),
                "data": data,
                "source_mode": "cluster_broadband_proxy"
            }
            channels_to_send.append(("webhook", webhook_cfg["url"], wh_body))

        # 飞书验证码气泡属于同一消息的第二次投递，也写入同一结果记录。
        if feishu_cfg.get("enable") and feishu_cfg.get("url") and extra_otp:
            pure_dict = {"msg_type": "text", "content": {"text": str(extra_otp)}}
            sec2 = (feishu_cfg.get("secret") or "").strip()
            if sec2:
                ts2 = str(int(time.time()))
                s2 = f"{ts2}\n{sec2}"
                hm2 = hmac.new(s2.encode("utf-8"), digestmod=hashlib.sha256).digest()
                pure_dict["timestamp"] = ts2
                pure_dict["sign"] = base64.b64encode(hm2).decode("utf-8")
            channels_to_send.append(("feishu_otp", feishu_cfg["url"], pure_dict))

        if not msg_id:
            log(f"[{session.slot_id}] 通知没有消息 id，宿主不外发")
            return
        device_key = session.meta.get("imei") or session.loc or session.port
        journal_key = hashlib.sha256(json.dumps(
            [device_key, session.meta.get("boot_id"), msg_id], ensure_ascii=False
        ).encode("utf-8")).hexdigest()
        journal = getattr(self, "notify_journal", None)
        if not journal:
            total_channels = len(channels_to_send)
            completed_count = [0]
            success_count = [0]
            ack_sent = [False]
            ack_lock = threading.Lock()

            def _send_ack_safe(status: str):
                if not ack_sent[0] and msg_id:
                    ack_sent[0] = True
                    session.ack_push(msg_id, status)
                    log(f"[{session.slot_id}] 宽带代推已定向回执 ACK -> 【{status}】(msg_id: {msg_id})")

            def _send_channel(ch_name: str, url: str, payload_dict: Dict[str, Any]):
                payload_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
                succ = False
                try:
                    req = urllib.request.Request(url, data=payload_bytes, headers={"Content-Type": "application/json; charset=utf-8"})
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        if resp.status == 200:
                            succ = True
                            log(f"[{session.slot_id}] 宿主宽带代推 [{event_type}] 到 {ch_name} 成功 (HTTP 200)")
                except Exception as e:
                    log(f"[{session.slot_id}] 宿主宽带代推 [{event_type}] 到 {ch_name} 失败: {e}")

                with ack_lock:
                    completed_count[0] += 1
                    if succ:
                        success_count[0] += 1
                        _send_ack_safe("ok")
                    elif completed_count[0] >= total_channels and success_count[0] == 0:
                        _send_ack_safe("failed")

            for ch_name, ch_url, ch_body in channels_to_send:
                threading.Thread(target=_send_channel, args=(ch_name, ch_url, ch_body), daemon=True).start()
            return

        begin_state = journal.begin(journal_key, session.slot_id, msg_id)
        if begin_state == "fault":
            log(f"[{session.slot_id}] 通知记录不可写，宿主不认领、不外发")
            return

        def _claim_then_send():
            # Some board events precede NOTIFY_PUSH registration (cellular-data
            # changes register it two seconds later). Retry only an explicit
            # "not yet pending" response, within the board's fallback window.
            deadline = time.monotonic() + 4.5
            claim_state = "expired"
            if hasattr(session, "claim_push"):
                while time.monotonic() < deadline:
                    claim_state = session.claim_push(msg_id, timeout=min(1.0, max(0.1, deadline - time.monotonic())))
                    if claim_state != "expired":
                        break
                    time.sleep(0.2)
            else:
                claim_state = "claimed"
            if claim_state != "claimed":
                if begin_state == "new" and journal:
                    journal.record(journal_key, state="unknown")
                log(f"[{session.slot_id}] 通知认领未确认 ({claim_state})，宿主不外发")
                return
            if begin_state == "duplicate":
                log(f"[{session.slot_id}] 重复通知已认领，按历史结果不再次外发")
                return
            if journal and not journal.record(journal_key, state="claimed"):
                return
            if not channels_to_send:
                if journal:
                    journal.record(journal_key, state="skipped")
                return
            states = []
            for ch_name, ch_url, ch_body in channels_to_send:
                state = "unknown"
                try:
                    req = urllib.request.Request(
                        ch_url, data=json.dumps(ch_body, ensure_ascii=False).encode("utf-8"),
                        headers={"Content-Type": "application/json; charset=utf-8"})
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        state, _ = _notify_business_result(
                            ch_name.split("_")[0], resp.status, resp.read(4096))
                except urllib.error.HTTPError:
                    state = "rejected"
                except Exception as exc:
                    log(f"[{session.slot_id}] 渠道 {ch_name} 结果未知: {type(exc).__name__}")
                states.append(state)
                if journal and not journal.record(journal_key, channel=ch_name, result=state):
                    return
            final_state = "complete" if all(s in ("accepted", "http_accepted") for s in states) else "attention"
            if journal:
                journal.record(journal_key, state=final_state)

        threading.Thread(target=_claim_then_send, daemon=True).start()

    def get_cluster_overview(self) -> Dict[str, Any]:
        """聚合集群全景驾驶舱数据 (AIR-22)"""
        summaries = self.session_pool.list_all_summaries()
        health_summary = self.health_monitor.get_summary().get("slots", {})
        cards = []
        for s in summaries:
            s_id = s.get("slot")
            h = health_summary.get(s_id, {})
            sim_carrier = detect_sim_carrier(s.get("iccid", ""))
            cards.append({
                "slot": s_id,
                "port": s.get("port"),
                "loc": s.get("loc"),
                "model": s.get("model", "Air780"),
                "bsp": s.get("bsp", ""),
                "imei": s.get("imei", ""),
                "iccid": s.get("iccid", ""),
                "carrier": sim_carrier,
                "online": s.get("online", False),
                "csq": s.get("csq", 0),
                "temp": s.get("temp", ""),
                "vbat": s.get("vbat", ""),
                "state": h.get("state", "healthy"),
                "routable": h.get("routable", True),
                "consecutive_failures": h.get("consecutive_failures", 0),
                "total_sent": h.get("total_sent", 0),
                "total_success": h.get("total_success", 0),
                "total_failed": h.get("total_failed", 0),
                "cellular_data": s.get("cellular_data", False),
                "capabilities": s.get("capabilities", {})
            })
        return {
            "total_slots": len(cards),
            "online_slots": sum(1 for c in cards if c.get("online")),
            "healthy_slots": sum(1 for c in cards if c.get("state") == "healthy" and c.get("online")),
            "cards": cards,
            "timestamp": time.time()
        }

    def _handle_update_management(self, cmd_obj: Dict[str, Any], is_internal_master: bool) -> Optional[Dict[str, Any]]:
        """Hub 固件与脚本维护原子状态机管控接口 (AIR-38 S04C1)"""
        if not isinstance(cmd_obj, dict):
            return None
        cmd = cmd_obj.get("cmd")
        if not isinstance(cmd, str):
            return None

        mgmt_cmds = {"capture_update_identity", "get_update_job", "pause_for_flash", "update_job_result", "resume_after_flash"}
        disabled_serial = {"pause_serial", "resume_serial"}
        staged_ota = {"ota_start", "ota_chunk", "chunk", "ota_finish", "finish", "ota_abort", "abort", "get_fota_status", "ota_get_fota_status"}

        if cmd not in mgmt_cmds and cmd not in disabled_serial and cmd not in staged_ota:
            return None

        req_id = cmd_obj.get("id")

        def reply(ok: bool, code: int, msg: str, error: str = "", data: Any = None) -> Dict[str, Any]:
            res: Dict[str, Any] = {"type": "res", "ok": ok, "code": code, "msg": msg, "error": error, "data": data}
            if req_id is not None:
                res["id"] = req_id
            return res

        if not is_internal_master:
            return reply(False, 403, "FORBIDDEN", error="Internal management command requires master authorization")

        if cmd in disabled_serial:
            return reply(False, 400, "COMMAND_DISABLED", error=f"'{cmd}' is disabled; use pause_for_flash or resume_after_flash")

        if cmd in staged_ota:
            return reply(False, 503, "SERVICE_UNAVAILABLE", error=f"'{cmd}' is staged for next slice wiring and not allowed for passthrough")

        params = cmd_obj.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        def find_exact_session(target_id: Dict[str, Any]) -> Optional[Any]:
            with self.session_pool.pool_lock:
                sessions = list(self.session_pool.sessions.values())
            try:
                coms = list(serial.tools.list_ports.comports())
            except Exception:
                return None
            target_imei = str(target_id.get("imei") or "").strip()
            matched = []
            for s in sessions:
                s_ports = [p for p in coms if (getattr(p, "device", "") or "").upper() == s.port.upper()]
                if len(s_ports) != 1:
                    continue
                p = s_ports[0]
                loc = (getattr(p, "location", "") or "").strip()
                ser = (getattr(p, "serial_number", "") or "").strip()
                if not (loc or ser) or not self.update_jobs._is_usb_match(target_id, {"usb_location": loc, "usb_serial": ser}):
                    continue
                rep_imei = str(s.meta.get("reported_imei") or "").strip()
                if rep_imei and rep_imei.lower() != "unknown" and target_imei and rep_imei != target_imei:
                    continue
                matched.append(s)
            return matched[0] if len(matched) == 1 else None

        if cmd == "capture_update_identity":
            slot = str(cmd_obj.get("slot") or params.get("slot") or "").strip()
            if not slot:
                return reply(False, 400, "INVALID_PARAM", error="Explicit slot is required")
            try:
                identity = self.capture_update_identity(slot)
                return reply(True, 0, "IDENTITY_CAPTURED", data={"identity": identity, **identity})
            except Exception as e:
                return reply(False, 500, "CAPTURE_FAILED", error=str(e))

        if cmd == "get_update_job":
            job_id = str(params.get("job_id") or cmd_obj.get("job_id") or "").strip()
            if not job_id:
                return reply(False, 400, "INVALID_PARAM", error="job_id is required")
            try:
                job = self.update_jobs.get(job_id)
            except Exception as e:
                return reply(False, 500, "STORE_ERROR", error=str(e))
            if not job:
                return reply(False, 404, "JOB_NOT_FOUND", error=f"Job {job_id} not found")
            sess = find_exact_session(job.get("identity") or {})
            slot = sess.slot_id if sess else None
            job["slot"] = slot
            job["job"] = job.get("job_id")
            job["device"] = job.get("device_id")
            job["pkg"] = job.get("package_id")
            return reply(True, 0, "OK", data=job)

        if cmd == "pause_for_flash":
            slot = str(cmd_obj.get("slot") or params.get("slot") or "").strip()
            if not slot:
                return reply(False, 400, "INVALID_PARAM", error="Explicit slot is required")
            mode = str(params.get("mode") or cmd_obj.get("mode") or "").strip()
            if mode not in ("sota", "script"):
                return reply(False, 400, "INVALID_PARAM", error="mode must be 'sota' or 'script'")
            job_id = str(params.get("job_id") or cmd_obj.get("job_id") or params.get("job") or "").strip()
            pkg_id = str(params.get("package_id") or cmd_obj.get("package_id") or params.get("pkg") or "").strip()
            if not job_id or not pkg_id:
                return reply(False, 400, "INVALID_PARAM", error="job_id and package_id must be non-empty")
            if len(pkg_id) != 64 or not re.fullmatch(r"[0-9a-f]{64}", pkg_id):
                return reply(False, 400, "INVALID_PARAM", error="package_id must be 64-character lowercase hex string")
            params_id = params.get("identity")
            if not isinstance(params_id, dict):
                return reply(False, 400, "INVALID_PARAM", error="params.identity must be a dict")
            expected = params.get("expected")
            if not isinstance(expected, dict):
                return reply(False, 400, "INVALID_PARAM", error="params.expected must be a dict")
            expected = copy.deepcopy(expected)

            try:
                cap_id = self.capture_update_identity(slot)
            except Exception as e:
                active_j = None
                try:
                    s = self.session_pool.get_session(slot)
                    if s and s.maintenance_job:
                        active_j = s.maintenance_job
                except Exception:
                    pass
                return reply(False, 500, "CAPTURE_FAILED", error=f"Fresh capture failed: {e}", data={"active_job": active_j} if active_j else None)

            strict_keys = ("device_id", "imei", "model", "chip", "core_version", "port", "usb_location", "usb_serial", "control_port", "boot_id", "version")
            for k in strict_keys:
                if str(params_id.get(k) or "").strip() != str(cap_id.get(k) or "").strip():
                    return reply(False, 400, "IDENTITY_MISMATCH", error=f"identity mismatch on '{k}'")

            exp_v = expected.get("version")
            if not isinstance(exp_v, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", exp_v):
                return reply(False, 400, "INVALID_PARAM", error="expected.version must be three-part ASCII numeric")
            exp_b = expected.get("build_id")
            if not isinstance(exp_b, str) or len(exp_b) != 64 or not re.fullmatch(r"[0-9a-f]{64}", exp_b):
                return reply(False, 400, "INVALID_PARAM", error="expected.build_id must be 64-hex lowercase string")

            cap_boot = str(cap_id.get("boot_id") or "").strip()
            old_boot_str = str(expected.get("old_boot_id") or "").strip()
            if mode == "sota":
                if not old_boot_str or not cap_boot or old_boot_str != cap_boot:
                    return reply(False, 400, "INVALID_PARAM", error="sota requires matching old_boot_id from capture")
                expected["old_boot_id"] = old_boot_str
            else:
                if old_boot_str:
                    if cap_boot and old_boot_str != cap_boot:
                        return reply(False, 400, "INVALID_PARAM", error="script old_boot_id must match capture if provided")
                    expected["old_boot_id"] = old_boot_str
                else:
                    expected["old_boot_id"] = None
            expected["ota_id"] = f"ota_{job_id}"

            try:
                exist_job = self.update_jobs.get(job_id)
                if exist_job and exist_job.get("phase") != "preflight":
                    return reply(False, 409, "BUSY", error=f"Job {job_id} already active in '{exist_job.get('phase')}'", data={"active_job": exist_job})
                act_job = self.update_jobs.active(cap_id)
                if act_job and act_job.get("job_id") != job_id:
                    return reply(False, 409, "BUSY", error=f"Device busy with active job {act_job.get('job_id')}", data={"active_job": act_job})
                reserved = self.update_jobs.reserve(job_id, cap_id, mode, pkg_id, expected)
            except Exception as e:
                act = None
                try:
                    act = self.update_jobs.active(cap_id)
                except Exception:
                    pass
                if act:
                    return reply(False, 409, "BUSY", error=str(e), data={"active_job": act})
                return reply(False, 500, "STORE_ERROR", error=str(e))

            session = self.session_pool.get_session(slot, active_only=True)
            if not session:
                try:
                    self.update_jobs.transition(job_id, cap_id["device_id"], "uncertain", error="Session lost after reserve")
                except Exception:
                    pass
                return reply(False, 500, "PAUSE_FAILED", error="Session disconnected after reserve")

            with session.serial_lock:
                try:
                    re_cap = self.capture_update_identity(slot)
                    for k in strict_keys:
                        if str(re_cap.get(k) or "").strip() != str(cap_id.get(k) or "").strip():
                            raise RuntimeError(f"Identity changed under lock for '{k}'")
                    session.pause_for_flash(reserved)
                except Exception as e:
                    try:
                        self.update_jobs.transition(job_id, cap_id["device_id"], "uncertain", error=f"Pause lock error: {e}")
                    except Exception:
                        pass
                    return reply(False, 500, "PAUSE_FAILED", error=f"Pause verification failed: {e}")

            reserved["slot"] = slot
            reserved["job"] = reserved.get("job_id")
            reserved["device"] = reserved.get("device_id")
            reserved["pkg"] = reserved.get("package_id")
            return reply(True, 0, "PAUSED_FOR_FLASH", data=reserved)

        if cmd == "update_job_result":
            job_id = str(params.get("job_id") or cmd_obj.get("job_id") or "").strip()
            dev_id = str(params.get("device_id") or cmd_obj.get("device_id") or "").strip()
            pkg_id = str(params.get("package_id") or cmd_obj.get("package_id") or "").strip()
            if not job_id or not dev_id or not pkg_id:
                return reply(False, 400, "INVALID_PARAM", error="job_id, device_id, and package_id are all required")
            if "confirmed" in params or "confirmed" in cmd_obj:
                return reply(False, 400, "INVALID_PARAM", error="Parameter 'confirmed' cannot be set by client")
            req_phase = str(params.get("phase") or cmd_obj.get("phase") or "").strip().lower()
            if req_phase == "succeeded":
                return reply(False, 400, "INVALID_PARAM", error="Phase 'succeeded' cannot be reported by client")
            try:
                job = self.update_jobs.get(job_id)
            except Exception as e:
                return reply(False, 500, "STORE_ERROR", error=str(e))
            if not job:
                return reply(False, 404, "JOB_NOT_FOUND", error=f"Job {job_id} not found")
            if job.get("device_id") != dev_id or job.get("package_id") != pkg_id:
                return reply(False, 400, "PARAM_MISMATCH", error="device_id or package_id mismatch with job record")
            if job.get("phase") in UpdateJobStore.TERMINAL_PHASES:
                return reply(False, 400, "TERMINAL_STATE", error=f"Job {job_id} is already in terminal state '{job.get('phase')}'")

            err_text = str(params.get("error") or cmd_obj.get("error") or "").strip()
            mode = job.get("mode")
            try:
                if mode == "sota":
                    if req_phase != "uncertain":
                        return reply(False, 400, "INVALID_PHASE", error="SOTA jobs only allow phase 'uncertain' from client")
                    up_job = self.update_jobs.transition(job_id, dev_id, "uncertain", error=err_text)
                else:
                    if req_phase == "writing":
                        up_job = self.update_jobs.transition(job_id, dev_id, "writing", effect_started=True, process_stopped=False, error=err_text)
                    elif req_phase == "confirming":
                        p_stop = bool(params.get("process_stopped"))
                        w_done = bool(params.get("write_completed"))
                        r_done = bool(params.get("reset_completed"))
                        if p_stop and w_done and r_done:
                            up_job = self.update_jobs.transition(job_id, dev_id, "confirming", process_stopped=True, effect_started=True, error=err_text)
                            sess = find_exact_session(job.get("identity") or {})
                            if sess:
                                self.bind_update_session(sess)
                        else:
                            up_job = self.update_jobs.transition(job_id, dev_id, "uncertain", error=err_text or "Incomplete confirming flags")
                    elif req_phase == "failed":
                        p_stop = bool(params.get("process_stopped"))
                        if p_stop:
                            up_job = self.update_jobs.transition(job_id, dev_id, "failed", process_stopped=True, confirmed=True, error=err_text)
                        else:
                            up_job = self.update_jobs.transition(job_id, dev_id, "uncertain", process_stopped=False, error=err_text)
                    else:
                        up_job = self.update_jobs.transition(job_id, dev_id, "uncertain", error=err_text or f"Unsupported phase '{req_phase}'")
                return reply(True, 0, "JOB_UPDATED", data=up_job)
            except Exception as e:
                return reply(False, 500, "STORE_ERROR", error=str(e))

        if cmd == "resume_after_flash":
            job_id = str(params.get("job_id") or cmd_obj.get("job_id") or "").strip()
            dev_id = str(params.get("device_id") or cmd_obj.get("device_id") or "").strip()
            if not job_id or not dev_id:
                return reply(False, 400, "INVALID_PARAM", error="Both job_id and device_id are required")
            try:
                job = self.update_jobs.get(job_id)
            except Exception as e:
                return reply(False, 500, "STORE_ERROR", error=str(e))
            if not job:
                return reply(False, 404, "JOB_NOT_FOUND", error=f"Job {job_id} not found")
            if job.get("device_id") != dev_id:
                return reply(False, 400, "DEVICE_MISMATCH", error=f"device_id mismatch: job belongs to {job.get('device_id')}, got {dev_id}")

            sess = find_exact_session(job.get("identity") or {})
            if sess:
                self.bind_update_session(sess)
            slot = sess.slot_id if sess else None
            job["slot"] = slot
            job["job"] = job.get("job_id")
            job["device"] = job.get("device_id")
            job["pkg"] = job.get("package_id")
            return reply(True, 0, "RESUMED", data=job)

        return None

    def _handle_client_send(self, msg_str: str):
        """兼容单机/测试下发指令"""
        try:
            cmd_data = json.loads(msg_str)
            cmd = cmd_data.get("cmd")
            if cmd in ("pause_serial", "pause_for_flash"):
                self.serial_paused = True
                with self.session_pool.pool_lock:
                    for sess in self.session_pool.sessions.values():
                        sess.pause_for_flash()
            elif cmd in ("resume_serial", "resume_after_flash"):
                self.serial_paused = False
                with self.session_pool.pool_lock:
                    for sess in self.session_pool.sessions.values():
                        sess.resume_after_flash()
            else:
                self.handle_client_command(None, msg_str)
        except Exception:
            pass

    def _ensure_serial_connected(self) -> bool:
        """检查串口连接健康度，处于暂停避让态时返回 False"""
        if getattr(self, "serial_paused", False):
            return False
        with self.session_pool.pool_lock:
            return any(sess.is_connected for sess in self.session_pool.sessions.values())

    def handle_client_command(self, line: str, sock: Optional[socket.socket] = None):
        """处理来自 Web 控制台、MCP 等客户端的 JSON 指令"""
        clean_line = line.strip()
        if not clean_line or not clean_line.startswith("{") or not clean_line.endswith("}"):
            return

        try:
            cmd_obj = json.loads(clean_line)
        except Exception:
            return

        cmd_name = cmd_obj.get("cmd")
        req_id = cmd_obj.get("id")
        params = cmd_obj.get("params") or {}
        target_slot = cmd_obj.get("slot") or params.get("slot") or cmd_obj.get("target")

        # 方案 D：调用者身份鉴权与内部免检信任环
        source = str(cmd_obj.get("source") or "").lower()
        token = str(cmd_obj.get("token") or "")

        # 若携带正确内部 Session Token，赋予 Web/Internal 绝对免检信任
        is_internal_master = False
        if token and token == self.internal_session_token:
            is_internal_master = True
            if sock:
                with self.clients_lock:
                    self.client_meta[sock] = {"source": "web", "authenticated": True}
        elif sock:
            with self.clients_lock:
                meta = self.client_meta.get(sock, {})
                if meta.get("authenticated") and meta.get("source") == "web":
                    is_internal_master = True

        # Fail-closed 默认拒绝准则：非内部免检信任源，统一定性为外部 source: "mcp"
        if not is_internal_master:
            source = "mcp"
            if sock:
                with self.clients_lock:
                    self.client_meta[sock] = {"source": "mcp", "authenticated": False}

        # 任何有效客户端指令均自动续期活跃态 (AIR-64)
        self.last_active_time = time.time()

        log(f"Received client cmd: {cmd_name}, id={req_id}, target_slot={target_slot}, source={source}, is_master={is_internal_master}")

        # 方案 D 物理管控黑名单拦截：当 MCP 开关关闭且来源为外部 MCP 时，100% 物理拦截并返回自解释人话提示
        mcp_enabled = bool(self.mcp_config.get("enabled", False))
        mcp_blocked_cmds = (
            "send_sms", "call_dial", "call_hangup", "get_history",
            "set_rndis", "set_cellular_data", "reboot", "clear_history"
        )
        if not is_internal_master and not mcp_enabled and cmd_name in mcp_blocked_cmds:
            deny_resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": False,
                "code": 403,
                "msg": "MCP_ACCESS_DENIED",
                "error": "❌ 物理调用被拒绝：上位机管理员已在控制台中关闭了 AI MCP 调用权限。如需使用，请前往 Web 控制台 (http://127.0.0.1:17801) 的【系统设置】抽屉中开启「AI 智能体通信服务 (MCP)」开关。"
            }, ensure_ascii=False) + "\n"
            if sock: sock.sendall(deny_resp.encode("utf-8"))
            else: self.broadcast_text(deny_resp)
            return

        # 1. Hub 内部控制指令处理
        if cmd_name == "touch_activity":
            sse_c = params.get("active_sse_count") if isinstance(params, dict) else None
            self.touch_activity(active_sse_count=sse_c)
            resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": True,
                "active_mode": self.is_in_active_mode(),
                "active_sse_count": self.active_sse_count
            }, ensure_ascii=False) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if cmd_name in ("get_slots", "list_dongles", "get_sessions"):
            summaries = self.session_pool.list_all_summaries()
            # 注入 mcp_action_allowed 字段方便 AI 客户端获悉当前权限状态进行友好引导
            resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": True,
                "code": 0,
                "data": {
                    "slots": summaries,
                    "count": len(summaries),
                    "mcp_action_allowed": mcp_enabled
                }
            }, ensure_ascii=False) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if cmd_name == "get_notify_results":
            resp = json.dumps({"type": "res", "id": req_id, "ok": True, "code": 0,
                               "data": {"items": self.notify_journal.recent(),
                                        "journal_ok": not self.notify_journal.fault}}) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if cmd_name == "reload_notify_config":
            try:
                self.reload_notify_config()
                result = {"type": "res", "id": req_id, "cmd": "reload_notify_config",
                          "ok": True, "code": 0, "msg": "CONFIG_LOADED"}
            except (OSError, ValueError):
                result = {"type": "res", "id": req_id, "cmd": "reload_notify_config",
                          "ok": False, "code": "config_load_failed", "msg": "CONFIG_LOAD_FAILED"}
            resp = json.dumps(result) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if cmd_name == "set_notify_config":
            data = cmd_obj.get("data")
            if isinstance(data, dict):
                cur = self._load_notify_config()
                for k, v in data.items():
                    if k in cur and isinstance(v, dict): cur[k].update(v)
                    elif isinstance(v, dict): cur[k] = v
                with open(GATEWAY_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cur, f, ensure_ascii=False, indent=2)
                self.notify_config = cur
                log("Hub 已接收并更新本地通知配置，正透传至各在线板卡...")
                # 广播下发给所有在线板卡
                with self.session_pool.pool_lock:
                    for s in self.session_pool.sessions.values():
                        s.send_line(clean_line)
                return

        if cmd_name in ("ping_device", "check_hardware"):
            has_any_online = any(s.is_connected for s in self.session_pool.sessions.values())
            resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": has_any_online,
                "online": has_any_online,
                "code": 0 if has_any_online else -1,
                "msg": "PONG" if has_any_online else "HARDWARE_DISCONNECTED",
                "slots": self.session_pool.list_all_summaries()
            }) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if cmd_name == "get_cluster_health":
            resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": True,
                "code": 0,
                "data": self.health_monitor.get_summary()
            }) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if cmd_name == "get_cluster_overview":
            resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": True,
                "code": 0,
                "data": self.get_cluster_overview()
            }) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if cmd_name == "get_unassigned_dongles":
            resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": True,
                "code": 0,
                "data": {
                    "unassigned": self.session_pool.get_unassigned_dongles()
                }
            }) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        # 2. 短信发送智能路由分流 (AIR-22)
        if cmd_name == "send_sms":
            target_phone = str(cmd_obj.get("phone") or params.get("phone") or cmd_obj.get("number") or params.get("number") or "")
            strategy = str(cmd_obj.get("strategy") or params.get("strategy") or "operator_affinity")
            direct_slot = cmd_obj.get("slot") or params.get("slot")
            dry_run = bool(cmd_obj.get("dry_run") or params.get("dry_run"))

            route_res = self.router.route_outbound(target_phone, strategy=strategy, direct_slot=direct_slot)
            if not route_res.ok:
                err_resp = json.dumps({
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "code": -1,
                    "msg": "ROUTE_FAILED",
                    "error": route_res.error,
                    "slot": direct_slot
                }) + "\n"
                if sock: sock.sendall(err_resp.encode("utf-8"))
                else: self.broadcast_text(err_resp)
                return

            if dry_run:
                ok_resp = json.dumps({
                    "type": "res",
                    "id": req_id,
                    "ok": True,
                    "code": 0,
                    "slot": route_res.slot_id,
                    "status": "routed",
                    "routed_strategy": route_res.strategy_used,
                    "fallback_used": route_res.fallback_used,
                    "panic_mode": route_res.panic_mode,
                    "msg": "DRY_RUN_ROUTED_OK"
                }) + "\n"
                if sock: sock.sendall(ok_resp.encode("utf-8"))
                else: self.broadcast_text(ok_resp)
                return

            session = route_res.session
            target_content = str(cmd_obj.get("content") or params.get("content") or cmd_obj.get("text") or params.get("text") or "")
            # 标记射频与串口发送静默期（6秒内暂停后台定时探测，避免基带与总线竞争）
            session._sms_tx_busy_until = time.time() + 6.0

            # 精简下发给板端串口的数据包，同时携带顶层与 data 字典参数以兼容历史固件
            board_cmd = {
                "type": "cmd",
                "id": req_id,
                "cmd": "send_sms",
                "phone": target_phone,
                "content": target_content,
                "data": {
                    "phone": target_phone,
                    "to": target_phone,
                    "content": target_content,
                    "text": target_content
                }
            }
            clean_line = json.dumps(board_cmd, ensure_ascii=False)
            session.send_line(clean_line)
            return

        # 3. 电话呼叫与硬件能力门禁 (AIR-30)
        if cmd_name == "call_dial":
            session = None
            # 智能优选或定向选择支持 VoLTE 的卡槽
            if not target_slot or target_slot in ("auto", ""):
                for s_id, sess in self.session_pool.sessions.items():
                    if sess.is_connected and sess.capabilities.get("volte"):
                        target_slot = s_id
                        session = sess
                        break
            else:
                session = self.session_pool.get_session(target_slot)

            if not session or not session.is_connected:
                err_resp = json.dumps({
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "code": -1,
                    "msg": "NO_VOLTE_SLOT",
                    "error": "集群中无可用的 VoLTE 语音通话模组卡槽" if not target_slot else f"目标卡槽 [{target_slot}] 不在线或未连接",
                    "slot": target_slot
                }) + "\n"
                if sock: sock.sendall(err_resp.encode("utf-8"))
                else: self.broadcast_text(err_resp)
                return

            if not session.capabilities.get("volte"):
                err_resp = json.dumps({
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "code": -1,
                    "msg": "HARDWARE_UNSUPPORTED",
                    "error": f"卡槽 [{session.slot_id}] 模组 ({session.model}) 硬件缺乏 VoLTE 语音协议栈，不支持拨打电话",
                    "slot": session.slot_id
                }) + "\n"
                if sock: sock.sendall(err_resp.encode("utf-8"))
                else: self.broadcast_text(err_resp)
                return

            # 透传 call_dial 到选中的 VoLTE 硬件会话
            cmd_obj["slot"] = session.slot_id
            if "type" not in cmd_obj:
                cmd_obj["type"] = "cmd"
            clean_line = json.dumps(cmd_obj, ensure_ascii=False)
            session.send_line(clean_line)
            return

        if cmd_name == "call_hangup":
            session = None
            if not target_slot or target_slot in ("auto", ""):
                for s_id, sess in self.session_pool.sessions.items():
                    if sess.is_connected and sess.capabilities.get("volte"):
                        target_slot = s_id
                        session = sess
                        break
            else:
                session = self.session_pool.get_session(target_slot)

            if not session or not session.is_connected:
                err_resp = json.dumps({
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "code": -1,
                    "msg": "HARDWARE_DISCONNECTED",
                    "error": f"卡槽 [{target_slot}] 不在线或未连接",
                    "slot": target_slot
                }) + "\n"
                if sock: sock.sendall(err_resp.encode("utf-8"))
                else: self.broadcast_text(err_resp)
                return

            cmd_obj["slot"] = session.slot_id
            if "type" not in cmd_obj:
                cmd_obj["type"] = "cmd"
            clean_line = json.dumps(cmd_obj, ensure_ascii=False)
            session.send_line(clean_line)
            # 同时回复 ACK 确保客户端即刻获得响应
            ack_resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": True,
                "code": 0,
                "msg": "HANGUP_SENT",
                "slot": session.slot_id
            }) + "\n"
            if sock: sock.sendall(ack_resp.encode("utf-8"))
            else: self.broadcast_text(ack_resp)
            return

        # 3. 定向或缺省路由到底层硬件会话（串口让渡与恢复指令豁免活跃连接态检查）
        is_flash_manage_cmd = cmd_name in ("pause_for_flash", "resume_after_flash")
        is_unassigned = bool(cmd_obj.get("is_unassigned") or params.get("is_unassigned") or (target_slot in (None, "", "new_device")))

        # 针对全新未分配模组 (无 slot) 的专属物理维护租约与 120s TTL
        if is_flash_manage_cmd and is_unassigned:
            target_port = cmd_obj.get("port") or params.get("port") or "unassigned_boot"
            lease_key = f"unassigned_{target_port}"
            if not hasattr(self, "_unassigned_leases"):
                self._unassigned_leases = {}

            now = time.time()
            if cmd_name == "pause_for_flash":
                existing = self._unassigned_leases.get(lease_key)
                if existing and (now - existing.get("started_at", 0) < 120.0):
                    if not (cmd_obj.get("force_retry") or params.get("force_retry")):
                        err_resp = json.dumps({
                            "type": "res",
                            "id": req_id,
                            "ok": False,
                            "code": 409,
                            "msg": "BUSY",
                            "error": f"全新设备端口 {target_port} 正在烧录中 (租约剩余 {int(120 - (now - existing['started_at']))}s)"
                        }) + "\n"
                        if sock: sock.sendall(err_resp.encode("utf-8"))
                        else: self.broadcast_text(err_resp)
                        return

                job_id = params.get("job_id") or cmd_obj.get("job_id") or f"job_{int(now*1000)}"
                self._unassigned_leases[lease_key] = {
                    "job_id": job_id,
                    "started_at": now,
                    "port": target_port,
                    "phase": "writing"
                }
                resp = json.dumps({
                    "type": "res",
                    "id": req_id,
                    "ok": True,
                    "job_id": job_id,
                    "device_id": lease_key,
                    "msg": "UNASSIGNED_DEVICE_PAUSED_FOR_FLASH"
                }) + "\n"
                if sock: sock.sendall(resp.encode("utf-8"))
                else: self.broadcast_text(resp)
                return

            elif cmd_name == "resume_after_flash":
                if lease_key in self._unassigned_leases:
                    del self._unassigned_leases[lease_key]
                resp = json.dumps({
                    "type": "res",
                    "id": req_id,
                    "ok": True,
                    "msg": "UNASSIGNED_DEVICE_RESUMED_AFTER_FLASH"
                }) + "\n"
                if sock: sock.sendall(resp.encode("utf-8"))
                else: self.broadcast_text(resp)
                return

        session = self.session_pool.get_session(target_slot, active_only=not is_flash_manage_cmd)
        if not session:
            err_msg = f"目标卡槽 [{target_slot}] 不存在" if target_slot else "无可用 4G 模组会话"
            err_resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": False,
                "code": -1,
                "msg": "SLOT_NOT_FOUND",
                "error": err_msg
            }) + "\n"
            if sock: sock.sendall(err_resp.encode("utf-8"))
            else: self.broadcast_text(err_resp)
            return

        # 底层物理线刷 (FlashToolCLI) 串口独占挂起与恢复：允许在未连接/已释放态安全恢复
        if cmd_name == "pause_for_flash":
            session.pause_for_flash()
            resp = json.dumps({"type": "res", "id": req_id, "ok": True, "slot": session.slot_id, "msg": "SERIAL_PAUSED_FOR_FLASH"}) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return
        elif cmd_name == "resume_after_flash":
            session.resume_after_flash()
            resp = json.dumps({"type": "res", "id": req_id, "ok": True, "slot": session.slot_id, "msg": "SERIAL_RESUMED_AFTER_FLASH"}) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if not session.is_connected:
            err_msg = f"目标卡槽 [{target_slot}] 不在线或未插入" if target_slot else "无可用的在线 4G 模组"
            err_resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": False,
                "code": -1,
                "msg": "HARDWARE_DISCONNECTED",
                "error": err_msg
            }) + "\n"
            if sock: sock.sendall(err_resp.encode("utf-8"))
            else: self.broadcast_text(err_resp)
            return

        # SOTA 固件升级状态同步与后台探针避让
        if cmd_name in ("ota_start", "ota_finish"):
            session.is_flashing = True
            log(f"[{session.slot_id}] ⚡ SOTA 固件热更进行中，保持后台心跳探针与出站路由避让")
        elif cmd_name == "ota_abort":
            session.is_flashing = False
            log(f"[{session.slot_id}] ⚡ SOTA 固件热更中止，已恢复后台心跳探针与出站路由")

        # 透传发送给选中的硬件会话
        if "type" not in cmd_obj:
            cmd_obj["type"] = "cmd"
        clean_line = json.dumps(cmd_obj, ensure_ascii=False)
        session.send_line(clean_line)

    def _tcp_server_loop(self):
        log("TCP IPC 服务线程就绪，等待客户端连接...")
        while self.running:
            try:
                client_sock, addr = self.server_sock.accept()
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                log(f"新客户端已接入: {addr}")
                with self.clients_lock:
                    self.clients.append(client_sock)

                # 初始向新客户端推送当前会话池全景摘要
                summaries = self.session_pool.list_all_summaries()
                init_event = json.dumps({
                    "type": "event",
                    "event": "cluster_status",
                    "data": {"slots": summaries, "count": len(summaries)}
                }) + "\n"
                try:
                    client_sock.sendall(init_event.encode("utf-8"))
                except Exception:
                    pass

                # 为客户端分配专属工作线程
                threading.Thread(target=self._client_worker, args=(client_sock, addr), daemon=True).start()
            except Exception as e:
                if self.running:
                    log(f"TCP accept 异常: {e}")
                    time.sleep(0.5)

    def _client_worker(self, sock: socket.socket, addr):
        buffer = ""
        while self.running:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="ignore")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        try:
                            self.handle_client_command(line, sock)
                        except Exception as e:
                            import traceback
                            log(f"handle_client_command 异常: {e}\n{traceback.format_exc()}")
            except Exception as e:
                log(f"_client_worker 异常: {e}")
                break

        log(f"客户端已断开: {addr}")
        with self.clients_lock:
            if sock in self.clients:
                self.clients.remove(sock)
            self.client_meta.pop(sock, None)
        try:
            sock.close()
        except Exception:
            pass


# =========================================================================
# 入口与 Windows Mutex 互斥保护
# =========================================================================

_single_instance_mutex = None

def _acquire_single_instance_mutex():
    global _single_instance_mutex
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _single_instance_mutex = kernel32.CreateMutexW(None, True, "Air780_Multi_Dongle_Hub_Mutex")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            log("已有 Hub 实例正在运行 (Windows Mutex 独占)，本进程安全退出。")
            sys.exit(0)

def main():
    _acquire_single_instance_mutex()
    hub = GatewayHub()
    try:
        hub.start()
    except KeyboardInterrupt:
        log("接收到退出信号，正在关闭 Hub...")
        hub.running = False
        hub.session_pool.stop()
        if hub.server_sock:
            try:
                hub.server_sock.close()
            except Exception:
                pass
        log("Hub 已安全退出")

if __name__ == "__main__":
    main()
