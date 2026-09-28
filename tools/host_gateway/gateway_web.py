import os
import sys
import time
import json
import socket
import ipaddress
import threading
import queue
import argparse
import base64
import hashlib
import re
import uuid
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn


def _public_gateway_config(config):
    """Keep notification credentials server-side while reporting configured state."""
    public = {}
    for section, value in config.items():
        if not isinstance(value, dict):
            public[section] = value
            continue
        item = dict(value)
        if section in ("feishu", "dingtalk", "wecom", "bark", "webhook"):
            for key in ("url", "secret"):
                if key in item:
                    item[key + "_configured"] = bool(item.pop(key))
        public[section] = item
    return public

# 引入 luadb_packer 打包引擎
HOST_GATEWAY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "host_gateway"))
if HOST_GATEWAY_DIR not in sys.path:
    sys.path.insert(0, HOST_GATEWAY_DIR)

try:
    import luadb_packer
except ImportError:
    luadb_packer = None

try:
    import firmware_flasher
except ImportError:
    firmware_flasher = None

from urllib.parse import urlparse, parse_qs

# 端口与地址配置
DEFAULT_WEB_HOST = "0.0.0.0"
DEFAULT_WEB_PORT = 17801
DEFAULT_HUB_HOST = "127.0.0.1"
DEFAULT_HUB_PORT = 17800

def _event_unix_seconds(value):
    """Use only an explicit Unix second/millisecond value, never a naive time string."""
    try:
        if isinstance(value, bool) or value is None:
            return None
        seconds = float(value)
        if 1e11 <= seconds < 1e14:
            seconds /= 1000
        return seconds if 1e9 <= seconds < 1e11 else None
    except (TypeError, ValueError):
        return None

class _SafeStream:
    def write(self, msg): pass
    def flush(self): pass

if sys.stdout is None:
    sys.stdout = _SafeStream()
if sys.stderr is None:
    sys.stderr = _SafeStream()


def get_bundle_dir() -> str:
    """获取静态资源解压/打包根目录：PyInstaller 模式下读取 _MEIPASS"""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

def get_config_dir() -> str:
    """获取配置持久化目录：PyInstaller 模式下写入 exe 所在目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

BUNDLE_DIR = get_bundle_dir()
DATA_DIR = get_config_dir()

WEB_DIR = os.path.join(BUNDLE_DIR, "web")
INDEX_HTML_PATH = os.path.join(WEB_DIR, "index.html")
GATEWAY_CONFIG_PATH = os.path.join(DATA_DIR, "gateway_config.json")

try:
    import gateway_runtime as runtime
    APP_VERSION = getattr(runtime, "BUSINESS_VERSION", "1.3.0")
except ImportError:
    runtime = None
    APP_VERSION = "1.3.0"

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_APP_NAME = "CellHiveGateway"

def get_log_dir() -> str:
    if runtime and hasattr(runtime, "directory"):
        try:
            return runtime.directory("logDir")
        except Exception:
            pass
    env_log = os.environ.get("GATEWAY_LOG_DIR")
    if env_log:
        return os.path.abspath(env_log)
    return DATA_DIR

def get_autostart_status() -> bool:
    """实时查询注册表 HKCU Run 项，以此作为开机自启唯一真理源 (SSOT)"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, RUN_APP_NAME)
            return bool(val)
    except (FileNotFoundError, OSError):
        return False

def set_autostart_status(enable: bool) -> bool:
    """根据布尔值增删注册表 HKCU 启动项，自动适配 Exe 与源码环境"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                if getattr(sys, "frozen", False):
                    cmd = f'"{os.path.abspath(sys.executable)}" --no-browser'
                else:
                    python_exe = sys.executable
                    pythonw = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
                    if os.path.exists(pythonw):
                        python_exe = pythonw
                    script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "gateway_app.py"))
                    cmd = f'"{python_exe}" "{script_path}" --no-browser'
                winreg.SetValueEx(key, RUN_APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, RUN_APP_NAME)
                except FileNotFoundError:
                    pass
            return True
    except Exception as e:
        _log(f"设置自启动注册表异常: {e}")
        return False

# 虚拟与代理适配器特征黑名单关键字 (大小写无关) (AIR-63)
VIRTUAL_IFACE_KEYWORDS = (
    "meta", "mihomo", "clash", "sing-box", "v2ray", "wsl", "vethernet",
    "hyper-v", "vmnet", "vmware", "vbox", "virtual", "virbr", "docker",
    "tap", "tun", "rndis", "tailscale", "zerotier", "wireguard"
)

# 保留与虚假 IP 拒绝网段 (AIR-63)
BOGUS_OR_RESERVED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),      # 本机环回
    ipaddress.ip_network("169.254.0.0/16"),   # 链路本地 APIPA
    ipaddress.ip_network("198.18.0.0/15"),    # 基准测试 / 代理 Fake-IP
    ipaddress.ip_network("100.64.0.0/10"),    # 运营商级 CGNAT / Tailscale 虚拟网
    ipaddress.ip_network("0.0.0.0/8"),        # 本网络
    ipaddress.ip_network("224.0.0.0/4"),      # 组播
)

_RFC1918_172 = ipaddress.ip_network("172.16.0.0/12")

def _is_virtual_iface_name(name: str) -> bool:
    """判定适配器名称是否包含虚拟/代理关键字 (AIR-63)"""
    if not name:
        return False
    low = str(name).lower()
    return any(k in low for k in VIRTUAL_IFACE_KEYWORDS)

def _is_valid_lan_ipv4(ip_str: str) -> bool:
    """严格判定是否为合规的私有物理局域网 IPv4 地址 (AIR-63)"""
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip.version != 4:
            return False
        # 排除环回、链路本地、未指定、组播与保留广播
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            return False
        # 排除测试保留与 Fake-IP 网段
        for bogus_net in BOGUS_OR_RESERVED_NETWORKS:
            if ip in bogus_net:
                return False
        # 必须是标准私有网段 (RFC 1918)
        return ip.is_private
    except (ValueError, TypeError):
        return False

def _ip_priority_score(ip_str: str) -> int:
    """
    私有局域网 IP 优先级评分：分值越高越优先 (AIR-63)
    192.168.x.x (家庭/路由最常见) -> 300
    10.x.x.x (企业私网)            -> 200
    172.16~31.x.x (标准私网)      -> 100
    其他合规私网                  -> 50
    """
    if ip_str.startswith("192.168."):
        return 300
    if ip_str.startswith("10."):
        return 200
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip in _RFC1918_172:
            return 100
    except Exception:
        pass
    return 50

def get_local_lan_ip() -> str:
    """
    智能探测本机真实物理局域网 IP (AIR-63):
    优先过滤 Clash/Mihomo/TUN/VMware/WSL 等虚拟网卡，确保返回手机与外部设备可真实访问的局域网地址。
    """
    # 策略 1：使用 psutil 详尽枚举真实物理网卡与活动状态
    try:
        import psutil
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        candidates: List[Tuple[int, str]] = []

        for iface_name, addr_list in addrs.items():
            # 过滤名称含虚拟关键字的网卡
            if _is_virtual_iface_name(iface_name):
                continue
            # 过滤未连接或禁用的网卡 (isup == False)
            stat = stats.get(iface_name)
            if stat and not stat.isup:
                continue

            for a in addr_list:
                if a.family == socket.AF_INET:
                    ip_candidate = a.address
                    if _is_valid_lan_ipv4(ip_candidate):
                        score = _ip_priority_score(ip_candidate)
                        candidates.append((score, ip_candidate))

        if candidates:
            # 按评分从高到低排序，返回最高分 IP
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
    except Exception:
        pass

    # 策略 2：通过主机名枚举解析全部绑定 IP 并过滤评分
    try:
        hostname = socket.gethostname()
        _, _, ip_list = socket.gethostbyname_ex(hostname)
        host_candidates: List[Tuple[int, str]] = []
        for ip_cand in ip_list:
            if _is_valid_lan_ipv4(ip_cand):
                score = _ip_priority_score(ip_cand)
                host_candidates.append((score, ip_cand))
        if host_candidates:
            host_candidates.sort(key=lambda x: x[0], reverse=True)
            return host_candidates[0][1]
    except Exception:
        pass

    # 策略 3：传统无连接 UDP 路由寻路保底（若返回黑名单网段则弃用）
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        if ip and _is_valid_lan_ipv4(ip):
            return ip
    except Exception:
        pass
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass

    return "127.0.0.1"

def get_log_size_info() -> Dict[str, Any]:
    log_file = os.path.join(get_log_dir(), "gateway_app.log")
    if os.path.exists(log_file):
        try:
            size_bytes = os.path.getsize(log_file)
            if size_bytes < 1024:
                human = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                human = f"{size_bytes / 1024:.1f} KB"
            else:
                human = f"{size_bytes / (1024 * 1024):.2f} MB"
            return {"bytes": size_bytes, "human": human, "path": log_file}
        except Exception:
            pass
    return {"bytes": 0, "human": "0 KB", "path": log_file}

def clear_runtime_log() -> bool:
    log_file = os.path.join(get_log_dir(), "gateway_app.log")
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            f.truncate(0)
        return True
    except Exception as e:
        _log(f"清理日志失败: {e}")
        return False

def handle_open_folder(target: str) -> Tuple[bool, str]:
    if sys.platform != "win32" or not hasattr(os, "startfile"):
        return False, "当前操作系统不支持快捷打开文件夹"
    target_map = {
        "data": DATA_DIR,
        "logs": get_log_dir()
    }
    dest = target_map.get(target)
    if not dest:
        return False, "非法目录目标 (仅允许 data 或 logs)"
    try:
        os.makedirs(dest, exist_ok=True)
        os.startfile(dest)
        return True, "OK"
    except Exception as e:
        return False, f"打开文件夹异常: {e}"

def parse_semver(ver_str: Any) -> tuple:
    """提取 SemVer 版本号数字元组 (major, minor, patch)。
    严格三段数字边界匹配，彻底杜绝字典序倒挂与 1.2.9x 等非法版本混入。
    解析失败返回 (0, 0, 0)。
    """
    if not ver_str or not isinstance(ver_str, str):
        return (0, 0, 0)
    s = ver_str.strip()
    # 严格匹配三段式数字版本号，三段之间必须有点，且第三段必须在合法边界 ($ 或 - 或 + 或 _) 结束，
    # 禁止 1.2.9x 等非标后缀直接粘连在数字后
    m = re.search(r'(?:^|[vV]|[\-_])(\d+)\.(\d+)\.(\d+)(?:$|[\-_+].*)', s)
    if not m:
        return (0, 0, 0)
    try:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return (0, 0, 0)


def extract_trusted_device_id(slot_info: Optional[Dict[str, Any]]) -> Optional[str]:
    """提取可信的设备物理稳定身份。
    只接受新鲜且合规的 14-17 位纯数字 IMEI (统一添加 imei: 前缀)；
    严禁从 slot、COM 口、unknown 或空串回退！
    """
    if not slot_info or not isinstance(slot_info, dict):
        return None
    raw_imei = slot_info.get("imei")
    if not raw_imei or not isinstance(raw_imei, str):
        return None
    cleaned = raw_imei.strip()
    if re.fullmatch(r"^\d{14,17}$", cleaned):
        return f"imei:{cleaned}"
    return None


def get_effective_fota_bundle_dir(bundle_dir: Optional[str] = None) -> Optional[str]:
    """获取随附发布包实际资源目录 (纯函数参数优先，无环境变量或全局覆盖捷径)"""
    if bundle_dir:
        return bundle_dir if os.path.exists(bundle_dir) else None
    if luadb_packer and hasattr(luadb_packer, "get_fota_bundle_dir"):
        return luadb_packer.get_fota_bundle_dir()
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        return os.path.join(meipass, "fota_bundle") if meipass else None
    return os.path.join(HOST_GATEWAY_DIR, "fota_bundle")


def evaluate_upgrade_gate(slot_info: Optional[Dict[str, Any]], bundle_dir: Optional[str] = None) -> Dict[str, Any]:
    """
    统一评估固件更新门禁状态与随附包兼容性 (AIR-38 / v4 规范定向加固)。
    适配判定严格 fail closed：
    1. 随附包清单完整校验；
    2. 设备稳定身份必须为新鲜且可信的 IMEI；
    3. 型号必须精确匹配目标 models（禁止双向子串/前缀匹配）；
    4. 芯片架构必须明确且等于目标 chip（禁止 BSP 猜测）；
    5. 底层内核版本必须明确且在目标清单内（禁止缺字段跳过）；
    6. serial_ota 能力描述必须符合 interface-contract-v1 rev2 规范与必要字段；
    7. 数字版本比对（1.2.9x 等非法版本解析失败保留未知）；
    8. 随附包较新时因 v3 执行链未经验收始终返回 host_not_ready 且 can_install=False。
    """
    effective_dir = bundle_dir or get_effective_fota_bundle_dir()
    pkg_status, pkg_data, pkg_err = "no_package", None, "未检测到上位机随附更新包"
    if luadb_packer and hasattr(luadb_packer, "load_bundled_release_package"):
        pkg_status, pkg_data, pkg_err = luadb_packer.load_bundled_release_package(effective_dir)
    elif effective_dir and os.path.exists(effective_dir):
        if luadb_packer and hasattr(luadb_packer, "load_release_package"):
            try:
                pkg_data = luadb_packer.load_release_package(effective_dir)
                pkg_status, pkg_err = "ok", None
            except Exception as e:
                pkg_status, pkg_err = "invalid_package", str(e)
        else:
            pkg_status, pkg_err = "error", "打包器组件不可用"
    else:
        pkg_status, pkg_err = "no_package", "未检测到上位机随附更新包"

    if pkg_status == "no_package":
        return {
            "check_state": "no_package",
            "reason": "未检测到上位机随附更新包",
            "can_install": False,
            "package_version": None,
            "package_id": None,
            "changelog": "",
            "size_kb": None,
            "target_manifest": None,
        }
    if pkg_status == "invalid_package":
        return {
            "check_state": "invalid_package",
            "reason": f"随附更新包校验失败: {pkg_err}",
            "can_install": False,
            "package_version": None,
            "package_id": None,
            "changelog": "",
            "size_kb": None,
            "target_manifest": None,
        }
    if pkg_status != "ok" or not pkg_data:
        return {
            "check_state": "error",
            "reason": pkg_err or "读取随附更新包发生异常",
            "can_install": False,
            "package_version": None,
            "package_id": None,
            "changelog": "",
            "size_kb": None,
            "target_manifest": None,
        }

    manifest = pkg_data.get("manifest", {})
    pkg_version = manifest.get("version")
    pkg_id = manifest.get("package_id")
    pkg_changelog = manifest.get("changelog", "")
    pkg_size_bytes = (manifest.get("sota", {}) or {}).get("size") or (manifest.get("script", {}) or {}).get("size") or 0
    pkg_size_kb = round(pkg_size_bytes / 1024, 1) if pkg_size_bytes else 0.0
    pkg_target = manifest.get("target", {})
    pkg_tuple = parse_semver(pkg_version)

    # 1. 设备在线核验 (严格只接受 online is True，缺失/None/False 均为 unknown_device)
    if not slot_info or not isinstance(slot_info, dict) or slot_info.get("online") is not True:
        return {
            "check_state": "unknown_device",
            "reason": "设备离线或在线状态异常 (online 状态非 True)",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }

    # 2. 设备稳定物理身份核验（只用可信 IMEI，缺失或格式错误判定为 unknown_device）
    device_id = extract_trusted_device_id(slot_info)
    if not device_id:
        return {
            "check_state": "unknown_device",
            "reason": "未能获取设备可信稳定身份 (缺少有效 14-17 位纯数字 IMEI)",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }

    # 3. 固件版本解析与有效性（无法解析则未知，1.2.9x 不得误判）
    current_ver = str(slot_info.get("version") or "").strip()
    cur_tuple = parse_semver(current_ver)
    if not current_ver or cur_tuple == (0, 0, 0):
        return {
            "check_state": "unknown_device",
            "reason": f"设备固件版本格式非法或无法识别: {current_ver or '空'}",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }

    # 4. 状态优先级：优先数字版本比对 (equal / device_newer 立即阻断，不降级且不可安装)
    if cur_tuple == pkg_tuple:
        return {
            "check_state": "equal",
            "reason": "当前设备版本与随附包一致",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }

    if cur_tuple > pkg_tuple:
        return {
            "check_state": "device_newer",
            "reason": "当前设备版本较新，随附包较旧，不降级",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }

    # 5. 随附包版本较新 (pkg_tuple > cur_tuple)：此时方行硬件/架构/能力严密适配判定
    # 设备型号精确匹配 (严格相等，禁止双向子串/模糊包含)
    device_model = slot_info.get("model")
    if not device_model or not isinstance(device_model, str):
        return {
            "check_state": "unknown_device",
            "reason": "未能获取设备型号信息",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }
    target_models = pkg_target.get("models", [])
    if device_model not in target_models:
        return {
            "check_state": "incompatible",
            "reason": f"随附更新包不适用于此设备型号 (设备: {device_model}, 目标支持: {target_models})",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }

    # 芯片架构精确匹配 (必须明确 Hub 上报 chip，禁止 BSP 猜测)
    device_chip = slot_info.get("chip")
    target_chip = str(pkg_target.get("chip") or "").lower()
    if not device_chip or not isinstance(device_chip, str) or str(device_chip).strip().lower() != target_chip:
        return {
            "check_state": "incompatible",
            "reason": f"设备芯片架构不匹配或未声明 (设备: {device_chip}, 目标要求: {target_chip})",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }

    # 底层内核版本精确匹配 (必须明确上报 core_version 且在目标列表内，禁止缺字段跳过)
    device_core = slot_info.get("core_version")
    target_cores = pkg_target.get("core_versions", [])
    if not device_core or not isinstance(device_core, str) or device_core not in target_cores:
        return {
            "check_state": "incompatible",
            "reason": f"设备底层内核版本不匹配或未声明 (设备: {device_core}, 目标支持: {target_cores})",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }

    # 串口 SOTA 安全更新能力描述校验 (interface-contract-v1 rev2)
    sota_cap = slot_info.get("serial_ota")
    if not sota_cap or not isinstance(sota_cap, dict):
        return {
            "check_state": "unsupported",
            "reason": "设备未上报 serial_ota 安全更新能力描述",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }
    if sota_cap.get("revision") != 2:
        return {
            "check_state": "unsupported",
            "reason": f"设备 serial_ota 能力版本不符合安全规范 (当前: {sota_cap.get('revision')}, 要求: 2)",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }
    timeouts = sota_cap.get("timeouts")
    req_timeouts = {"start_ms", "chunk_ms", "init_ms", "write_ms", "reply_margin_ms", "reboot_ms"}
    if not isinstance(timeouts, dict) or not req_timeouts.issubset(set(timeouts.keys())):
        return {
            "check_state": "unsupported",
            "reason": "设备 serial_ota 能力超时参数缺失",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }
    for k in sorted(list(req_timeouts)):
        val = timeouts.get(k)
        if type(val) is not int or val <= 0:
            return {
                "check_state": "unsupported",
                "reason": f"设备 serial_ota 能力超时参数 {k} 非合法正整数 (当前值: {val!r})",
                "can_install": False,
                "package_version": pkg_version,
                "package_id": pkg_id,
                "changelog": pkg_changelog,
                "size_kb": pkg_size_kb,
                "target_manifest": manifest,
            }
    max_pkg_bytes = sota_cap.get("max_package_bytes")
    if type(max_pkg_bytes) is not int or max_pkg_bytes <= 0:
        return {
            "check_state": "unsupported",
            "reason": f"设备 serial_ota 能力 max_package_bytes 非合法正整数 (当前值: {max_pkg_bytes!r})",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }
    if pkg_size_bytes > max_pkg_bytes:
        return {
            "check_state": "unsupported",
            "reason": f"随附包大小超出设备支持上限 ({pkg_size_bytes} > {max_pkg_bytes})",
            "can_install": False,
            "package_version": pkg_version,
            "package_id": pkg_id,
            "changelog": pkg_changelog,
            "size_kb": pkg_size_kb,
            "target_manifest": manifest,
        }

    # 维护占用判定 (busy)
    with flashing_lock:
        if flashing_state.get("is_flashing"):
            return {
                "check_state": "busy",
                "reason": f"已有卡槽 [{flashing_state.get('slot')}] 正在执行维护或烧录任务，请稍候",
                "can_install": False,
                "package_version": pkg_version,
                "package_id": pkg_id,
                "changelog": pkg_changelog,
                "size_kb": pkg_size_kb,
                "target_manifest": manifest,
            }

    # 6. v3 安全升级执行链未经验收前始终阻断，绝不 available，can_install 恒为 False
    return {
        "check_state": "host_not_ready",
        "reason": "上位机安全更新功能尚未就绪",
        "can_install": False,
        "package_version": pkg_version,
        "package_id": pkg_id,
        "changelog": pkg_changelog,
        "size_kb": pkg_size_kb,
        "target_manifest": manifest,
    }

# 引入 Hub 的渠道测试函数与分舱存储引擎
from gateway_hub import test_channel_push
from storage_manager import StorageManager


flashing_lock = threading.Lock()
flashing_state = {
    "is_flashing": False,
    "slot": None,
    "percent": 0,
    "status": "idle",
    "stage": "idle",
    "error": None,
    "start_time": 0
}

def update_flashing_progress(percent: int, status: str, stage: str = "flashing", error: str = None, slot: str = None):
    with flashing_lock:
        flashing_state["percent"] = percent
        flashing_state["status"] = status
        flashing_state["stage"] = stage
        flashing_state["error"] = error
        if slot:
            flashing_state["slot"] = slot
        if percent >= 100 or error:
            flashing_state["is_flashing"] = False

def _log(msg: str):
    if sys.stdout is not None:
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}] [Web] {msg}", flush=True)
        except Exception:
            pass


# =========================================================================
# 核心类：HubBackendClient (与 17800 中枢通信的多卡槽客户端)
# =========================================================================

class HubBackendClient:
    """与本地 127.0.0.1:17800 多模组中枢通信的双向 TCP 客户端"""

    def __init__(self, host=DEFAULT_HUB_HOST, port=DEFAULT_HUB_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.sock_lock = threading.Lock()
        self.running = False
        self.rx_thread = None

        # RPC 等待字典：{ req_id: {"event": threading.Event(), "response": None} }
        self.pending_requests = {}
        self.pending_lock = threading.Lock()

        # 多卡槽集群状态
        self.slots: List[Dict[str, Any]] = []
        self.active_slot: str = "slot_1"
        self.storage_mgr = StorageManager(DATA_DIR)
        self.synced_slots: Set[str] = set() # 记录已完成脱机同步收割的卡槽
        self.latest_status: Dict[str, Any] = {}
        self.latest_status_by_slot: Dict[str, Dict[str, Any]] = {}
        self.recent_sms_events: List[Dict[str, Any]] = []
        self.recent_sms_by_slot: Dict[str, List[Dict[str, Any]]] = {}
        self.recent_calls: List[Dict[str, Any]] = []
        self.recent_calls_by_slot: Dict[str, List[Dict[str, Any]]] = {}
        self.call_status_by_slot: Dict[str, Dict[str, Any]] = {}
        self.is_hardware_connected = False
        self.cache_lock = threading.Lock()

        # SSE 广播客户端队列列表
        self.sse_listeners = []
        self.sse_lock = threading.Lock()

        # 方案 D: 读取本地内部免检 Session Token
        self.internal_session_token = ""
        self._load_session_token()

        # 活跃度上报与防抖 (AIR-64)
        self._last_touch_time: float = 0.0

    def touch_activity(self, active_sse_count: Optional[int] = None, force: bool = False):
        """向底层通信中枢同步活跃状态，带 3.0s 本地防抖 (AIR-64)"""
        now = time.time()
        if not force and active_sse_count is None and (now - getattr(self, "_last_touch_time", 0.0) < 3.0):
            return
        self._last_touch_time = now
        params = {}
        if active_sse_count is not None:
            params["active_sse_count"] = int(active_sse_count)

        def _bg_touch():
            try:
                self.execute_cmd("touch_activity", params=params, timeout=1.0)
            except Exception:
                pass
        threading.Thread(target=_bg_touch, daemon=True).start()

    def _load_session_token(self):
        """读取 Hub 生成在本地数据目录的内部免检令牌"""
        try:
            token_path = os.path.join(DATA_DIR, ".hub_session_token")
            if os.path.exists(token_path):
                with open(token_path, "r", encoding="utf-8") as f:
                    self.internal_session_token = f.read().strip()
        except Exception:
            pass

    def start(self):
        self.running = True
        self._ensure_connected()
        if not self.rx_thread or not self.rx_thread.is_alive():
            self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self.rx_thread.start()

    def stop(self):
        self.running = False
        with self.sock_lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

    def _auto_spawn_hub(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        hub_path = os.path.join(base_dir, "gateway_hub.py")
        if not os.path.exists(hub_path):
            return
        _log(f"正在后台自拉起 gateway_hub.py 中枢: {hub_path}")
        try:
            creation_flags = 0
            if os.name == "nt":
                creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            subprocess.Popen(
                [sys.executable, hub_path],
                creationflags=creation_flags,
                close_fds=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            _log(f"自拉起 Hub 失败: {e}")

    def _ensure_connected(self) -> bool:
        with self.sock_lock:
            if self.sock:
                return True
            for attempt in range(1, 4):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    s.settimeout(2.0)
                    s.connect((self.host, self.port))
                    s.settimeout(None)
                    self.sock = s
                    _log(f"已成功连接底层多设备通信中枢: {self.host}:{self.port}")
                    # 建连成功后立即刷新读取一次 Session Token（防止 Hub 独立重启后换了新令牌）
                    self._load_session_token()
                    # 请求获取卡槽全量列表
                    time.sleep(0.1)
                    req_line = json.dumps({"type": "cmd", "cmd": "get_slots", "id": "init_slots", "token": self.internal_session_token, "source": "web"}) + "\n"
                    self.sock.sendall(req_line.encode("utf-8"))
                    return True
                except (ConnectionRefusedError, OSError):
                    if attempt == 1:
                        self._auto_spawn_hub()
                    time.sleep(0.5)
            return False

    def _trigger_offline_sync(self, slot_id: str, iccid: str):
        """触发模组脱机黑匣子短信异步拉取与 2PC 清理闭环 (审查 P1 修正)"""
        clean_iccid = str(iccid or "").strip()
        if not slot_id or not clean_iccid or clean_iccid == "sim_unknown":
            return
        with self.cache_lock:
            if slot_id in self.synced_slots:
                return
            self.synced_slots.add(slot_id)

        threading.Thread(target=self._run_offline_sync, args=(slot_id, clean_iccid), daemon=True).start()

    def _run_offline_sync(self, slot_id: str, iccid: str):
        _log(f"[{slot_id}] 检测到模组上线就绪，启动脱机黑匣子自动同步 (ICCID: {iccid})...")
        time.sleep(1.0)  # 避开开机/插卡初始通信高频期
        cursor = None
        all_fetched = []
        max_batches = 10  # 板端最多 100 条，每批 15 条，最多 7~8 批即可收割完毕
        batch_count = 0

        while batch_count < max_batches:
            batch_count += 1
            cmd_payload = {"limit": 15}
            if cursor:
                cmd_payload["cursor"] = cursor
            resp = self.execute_cmd("get_history", params=cmd_payload, slot=slot_id, timeout=4.0)
            if not resp.get("ok"):
                break
            raw_data = resp.get("data", {})
            raw_items = raw_data.get("items") or raw_data.get("list") or []
            if not raw_items:
                break
            all_fetched.extend(raw_items)
            has_more = raw_data.get("has_more")
            next_cur = raw_data.get("next_cursor")
            has_more = bool(next_cur and str(next_cur) != "0")
            if not has_more:
                break
            cursor = next_cur

        if all_fetched:
            _log(f"[{slot_id}] 成功拉取脱机短信 {len(all_fetched)} 条，增量写入本地 ICCID 分舱权威存储...")
            comp = self.storage_mgr.get_compartment(iccid)
            duplicate_rows = {}
            for it in all_fetched:
                sender = it.get("from") or it.get("sender") or it.get("phone") or "未知号码"
                content = it.get("content", "")
                raw_time = it.get("time") or it.get("ts") or ""
                numeric_raw_time = raw_time if (isinstance(raw_time, (int, float)) or
                                                (isinstance(raw_time, str) and raw_time.strip().isdigit())) else None
                ts = _event_unix_seconds(it.get("timestamp"))
                if ts is None:
                    ts = _event_unix_seconds(numeric_raw_time)
                time_str = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
                            if ts is not None else str(raw_time))

                row_bytes = json.dumps(it, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                row_digest = hashlib.sha256(row_bytes).hexdigest()
                duplicate_rows[row_digest] = duplicate_rows.get(row_digest, 0) + 1
                msg_id = it.get("id") or f"board_{row_digest}_{duplicate_rows[row_digest]}"
                if comp.is_deleted(msg_id, sender, content, raw_time):
                    continue

                msg_obj = {
                    "id": msg_id,
                    "slot": slot_id,
                    "iccid": iccid,
                    "phone": sender,
                    "sender": sender,
                    "content": content,
                    "otp": it.get("code") or it.get("otp") or "",
                    "time": time_str
                }
                if ts is not None:
                    msg_obj["timestamp"] = ts
                comp.append_message(msg_obj)

            # 2PC 确认清理：上位机落盘成功后，下发 clear_history 让板端 LittleFS 恢复 0 占用
            _log(f"[{slot_id}] 本地权威落盘成功，下发 clear_history 清空板端脱机暂存...")
            self.execute_cmd("clear_history", slot=slot_id, timeout=3.0)
            self.broadcast_sse("sms_received", {"slot": slot_id, "sync": True})

    def execute_cmd(self, cmd_name: str, params: dict = None, slot: Optional[str] = None, timeout: float = 8.0, wait_terminal: bool = False) -> dict:
        """向 Hub 下发指令并同步等待响应，支持定向指定目标卡槽 slot"""
        if not self._ensure_connected():
            return {"ok": False, "error": "无法连接底层通信中枢"}

        req_id = f"web_{uuid.uuid4().hex}"
        evt = threading.Event()
        req_entry = {"event": evt, "response": None, "wait_terminal": wait_terminal, "cmd": cmd_name}

        with self.pending_lock:
            self.pending_requests[req_id] = req_entry

        # 若未指定 slot，仅针对单板控制命令回退到 active_slot；对于 send_sms 等支持集群智能调度的命令保持 None
        if slot:
            target_slot = slot
        elif cmd_name in ("send_sms", "get_cluster_overview", "get_cluster_health", "get_slots", "list_dongles"):
            target_slot = None
        else:
            target_slot = self.active_slot or "slot_1"

        packet = {
            "type": "cmd",
            "id": req_id,
            "cmd": cmd_name,
            "params": params or {},
            "source": "web",
            "token": getattr(self, "internal_session_token", "")
        }
        if not packet["token"]:
            self._load_session_token()
            packet["token"] = getattr(self, "internal_session_token", "")
        if target_slot:
            packet["slot"] = target_slot

        try:
            line = json.dumps(packet, ensure_ascii=False) + "\n"
            with self.sock_lock:
                if not self.sock:
                    return {"ok": False, "error": "底层通信已断开"}
                self.sock.sendall(line.encode("utf-8"))
        except Exception as e:
            with self.pending_lock:
                self.pending_requests.pop(req_id, None)
            if cmd_name == "send_sms":
                return {"ok": False, "code": "unknown", "id": req_id,
                        "error": "短信请求写入中断，可能已被设备收到；结果未知，请勿重复发送"}
            return {"ok": False, "error": f"指令写入套接字失败: {e}"}

        # 同步等待响应
        if evt.wait(timeout=timeout):
            resp = req_entry.get("response", {})
            return resp
        else:
            with self.pending_lock:
                entry = self.pending_requests.pop(req_id, None)
            if entry and entry.get("queued"):
                return {
                    "ok": False,
                    "code": -408,
                    "msg": "UNKNOWN",
                    "error": f"短信已进入发送队列，但在 {timeout}s 内未收到基站终态回执",
                    "data": {"reason": "modem_result_timeout"}
                }
            if cmd_name == "send_sms":
                return {"ok": False, "code": "unknown", "id": req_id,
                        "error": "短信请求已提交但未收到设备回执，结果未知；请勿重复发送"}
            return {"ok": False, "error": f"等待设备响应超时 ({timeout}s)"}

    def _rx_loop(self):
        buffer = ""
        while self.running:
            if not self._ensure_connected():
                time.sleep(1.0)
                continue

            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    _log("中枢套接字断开，准备重连...")
                    with self.sock_lock:
                        if self.sock:
                            try:
                                self.sock.close()
                            except Exception:
                                pass
                            self.sock = None
                    time.sleep(1.0)
                    continue

                buffer += chunk.decode("utf-8", errors="ignore")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._dispatch_frame(line)

            except Exception as e:
                if not self.running:
                    break
                _log(f"接收线程异常: {e}")
                with self.sock_lock:
                    if self.sock:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                        self.sock = None
                time.sleep(1.0)

    def _update_status_cache(self, event_data: dict, slot: str = ""):
        """更新对应卡槽的状态缓存"""
        if not isinstance(event_data, dict):
            return

        target_slot = slot or event_data.get("slot") or self.active_slot or "slot_1"

        with self.cache_lock:
            if target_slot not in self.latest_status_by_slot:
                self.latest_status_by_slot[target_slot] = {}
            target_cache = self.latest_status_by_slot[target_slot]
            slot_info = next((s for s in self.slots if s.get("slot") == target_slot), {})
            # 计数只属于当前设备/SIM。缺少身份字段不代表换卡。
            identity_changed = any(
                event_data.get(key) and (target_cache.get(key) or slot_info.get(key))
                and event_data[key] != (target_cache.get(key) or slot_info.get(key))
                for key in ("imei", "iccid")
            )
            offline = event_data.get("online") is False
            if identity_changed or offline:
                target_cache.pop("sms_count", None)

            if "rndis" in event_data:
                target_cache["rndis"] = bool(event_data["rndis"])
                target_cache["rndis_enable"] = bool(event_data["rndis"])
            elif "rndis_enable" in event_data:
                target_cache["rndis"] = bool(event_data["rndis_enable"])
                target_cache["rndis_enable"] = bool(event_data["rndis_enable"])

            if "cellular_data" in event_data:
                target_cache["cellular_data"] = bool(event_data["cellular_data"])
                target_cache["cellular_data_enable"] = bool(event_data["cellular_data"])
            elif "cellular_data_enable" in event_data:
                target_cache["cellular_data"] = bool(event_data["cellular_data_enable"])
                target_cache["cellular_data_enable"] = bool(event_data["cellular_data_enable"])

            for k in ("model", "bsp", "imei", "iccid", "csq", "rsrp", "temp", "vbat", "uptime", "lua_mem_kb", "version", "capabilities", "port", "online"):
                if k in event_data and (k not in ("imei", "iccid") or event_data[k]):
                    target_cache[k] = event_data[k]
            # 增量缺字段保留同设备最近值；显式 0 是有效设备值。
            if not offline:
                if "blackbox_count" in event_data:
                    target_cache["sms_count"] = event_data["blackbox_count"]
                elif "sms_count" in event_data:
                    target_cache["sms_count"] = event_data["sms_count"]
            if "current_version" in event_data:
                target_cache["version"] = event_data["current_version"]
            if "uptime_seconds" in event_data:
                target_cache["uptime"] = event_data["uptime_seconds"]

            if not target_cache.get("port"):
                slot_info = next((s for s in self.slots if s.get("slot") == target_slot), None)
                if slot_info and slot_info.get("port"):
                    target_cache["port"] = slot_info["port"]

            phone_val = event_data.get("phone") or event_data.get("number")
            if phone_val and str(phone_val).strip():
                target_cache["phone"] = str(phone_val).strip()
                target_cache["number"] = str(phone_val).strip()
            elif not target_cache.get("phone"):
                slot_info = next((s for s in self.slots if s.get("slot") == target_slot), None)
                if slot_info and slot_info.get("phone"):
                    target_cache["phone"] = slot_info["phone"]
                    target_cache["number"] = slot_info["phone"]

            target_cache["slot"] = target_slot

            # 如果当前活跃卡槽与 target_slot 一致，同步更新缺省缓存
            if target_slot == self.active_slot:
                self.latest_status.pop("sms_count", None)
                self.latest_status.update(target_cache)

    def _update_slots_cache(self, slots_data: list):
        """Hub 卡槽摘要中的设备数也同步到后续 SSE 使用的状态缓存。"""
        for slot_info in slots_data:
            if slot_info.get("slot"):
                self._update_status_cache(slot_info, slot=slot_info["slot"])
        with self.cache_lock:
            self.slots = slots_data

    def _dispatch_frame(self, raw_line: str):
        try:
            data = json.loads(raw_line)
        except Exception:
            return

        frame_type = data.get("type")
        frame_slot = data.get("slot") or "slot_1"

        # 1. 响应帧
        if frame_type in ("res", "response"):
            inner_data = data.get("data")
            req_id = data.get("id") or (inner_data.get("id") if isinstance(inner_data, dict) else None)
            if req_id:
                with self.pending_lock:
                    entry = self.pending_requests.get(req_id)
                    if entry:
                        msg = data.get("msg") or (inner_data.get("msg", "") if isinstance(inner_data, dict) else "")
                        code = data.get("code") if "code" in data else (inner_data.get("code", 0) if isinstance(inner_data, dict) else 0)
                        if entry.get("wait_terminal") and code == 0 and msg == "QUEUED":
                            entry["queued"] = True
                            entry["intermediate"] = inner_data if isinstance(inner_data, dict) else data
                            self.broadcast_sse("sms_status", {
                                "id": req_id,
                                "slot": frame_slot,
                                "state": "QUEUED",
                                "data": inner_data if isinstance(inner_data, dict) else {}
                            })
                            return

                        self.pending_requests.pop(req_id, None)
                        res_obj = dict(data)
                        if "ok" not in res_obj:
                            res_obj["ok"] = (code == 0)
                        if isinstance(inner_data, dict):
                            for k, v in inner_data.items():
                                if k not in res_obj:
                                    res_obj[k] = v
                        entry["response"] = res_obj
                        entry["event"].set()

            # 处理 get_slots 响应
            if req_id == "init_slots" and data.get("ok"):
                slots_data = data.get("data", {}).get("slots", [])
                self._update_slots_cache(slots_data)
                with self.cache_lock:
                    if self.slots and not any(s["slot"] == self.active_slot for s in self.slots):
                        self.active_slot = self.slots[0]["slot"]
                self.broadcast_sse("cluster_update", {"slots": self.slots, "active_slot": self.active_slot})

            if data.get("ok"):
                self.is_hardware_connected = True

        # 2. 事件广播帧
        elif frame_type == "event":
            event_name = data.get("event")
            event_data = data.get("data", {})
            evt_slot = data.get("slot") or event_data.get("slot") or frame_slot

            if event_name in ("cluster_status", "dongle_connected", "dongle_disconnected"):
                # 会话池集群状态变动
                if event_name == "cluster_status":
                    self._update_slots_cache(event_data.get("slots", []))
                    # 尝试触发所有在线卡槽的脱机同步
                    for s in self.slots:
                        if s.get("online") and s.get("iccid"):
                            self._trigger_offline_sync(s.get("slot"), s.get("iccid"))
                elif event_name == "dongle_connected":
                    # 增量添加或更新
                    slot_id = event_data.get("slot")
                    self._update_status_cache(event_data, slot=slot_id)
                    existing = [s for s in self.slots if s.get("slot") == slot_id]
                    if existing:
                        existing[0].update(event_data)
                    else:
                        self.slots.append(event_data)
                    if event_data.get("iccid"):
                        self._trigger_offline_sync(slot_id, event_data.get("iccid"))
                elif event_name == "dongle_disconnected":
                    slot_id = event_data.get("slot")
                    self._update_status_cache({"online": False}, slot=slot_id)
                    for s in self.slots:
                        if s.get("slot") == slot_id:
                            s["online"] = False
                    with self.cache_lock:
                        self.synced_slots.discard(slot_id)

                with self.cache_lock:
                    if self.slots and not any(s.get("slot") == self.active_slot and s.get("online") for s in self.slots):
                        online_slots = [s for s in self.slots if s.get("online")]
                        if online_slots:
                            self.active_slot = online_slots[0]["slot"]

                self.broadcast_sse("cluster_update", {"slots": self.slots, "active_slot": self.active_slot})

            elif event_name in ("device_connected", "dongle_connected"):
                self._update_status_cache(event_data, slot=evt_slot)
                self.is_hardware_connected = True
                if event_data.get("iccid"):
                    self._trigger_offline_sync(evt_slot, event_data.get("iccid"))
                self.broadcast_sse("device_connected", {"online": True, "slot": evt_slot})

            elif event_name in ("device_disconnected", "dongle_disconnected"):
                self._update_status_cache({"online": False}, slot=evt_slot)
                with self.cache_lock:
                    self.synced_slots.discard(evt_slot)
                self.broadcast_sse("device_disconnected", {"online": False, "slot": evt_slot})

            elif event_name in ("status", "gateway_ready", "state_change"):
                self.is_hardware_connected = True
                self._update_status_cache(event_data, slot=evt_slot)
                if event_data.get("iccid"):
                    self._trigger_offline_sync(evt_slot, event_data.get("iccid"))
                with self.cache_lock:
                    status_snapshot = dict(self.latest_status_by_slot.get(evt_slot, {}))
                    s_info = next((s for s in self.slots if s.get("slot") == evt_slot), None)
                    if s_info:
                        s_info = dict(s_info)
                status_snapshot["online"] = True
                status_snapshot["slot"] = evt_slot
                # 预先合并该卡槽的静态元数据 (imei, iccid, model, version, phone, port)，杜绝缺失字段推流导致前端闪烁
                if s_info:
                    for field in ("imei", "iccid", "model", "bsp", "version", "port"):
                        if not status_snapshot.get(field) and s_info.get(field):
                            status_snapshot[field] = s_info[field]
                    if not status_snapshot.get("phone") and s_info.get("phone"):
                        status_snapshot["phone"] = s_info["phone"]
                        status_snapshot["number"] = s_info["phone"]
                self.broadcast_sse("status_update", status_snapshot)

            elif event_name in ("sms_rx", "sms_received"):
                self.is_hardware_connected = True
                raw_time = event_data.get("time") or event_data.get("ts")
                sms_timestamp = _event_unix_seconds(event_data.get("timestamp"))
                if sms_timestamp is None:
                    sms_timestamp = _event_unix_seconds(raw_time)
                if sms_timestamp is not None:
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sms_timestamp))
                elif raw_time:
                    time_str = str(raw_time)
                else:
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S")

                sender_phone = event_data.get("from") or event_data.get("phone") or "未知号码"
                with self.cache_lock:
                    s_meta = next((s for s in self.slots if s.get("slot") == evt_slot), {})
                    slot_phone = s_meta.get("phone") or s_meta.get("number")
                    slot_model = s_meta.get("model") or s_meta.get("bsp")
                    slot_iccid = s_meta.get("iccid") or self.latest_status_by_slot.get(evt_slot, {}).get("iccid") or "sim_unknown"

                from storage_manager import derive_operator_and_badge
                badge_info = derive_operator_and_badge(
                    iccid=slot_iccid,
                    my_phone=slot_phone,
                    sender=sender_phone,
                    content=event_data.get("content") or "",
                    model=slot_model,
                    slot=evt_slot
                )

                item = {
                    "slot": evt_slot,
                    "phone": sender_phone,
                    "content": event_data.get("content") or "",
                    "otp": event_data.get("code") or event_data.get("otp"),
                    "time": time_str,
                    "timestamp": sms_timestamp,
                    "operator": badge_info["operator"],
                    "display_badge": badge_info["display_badge"],
                    "slot_display": badge_info["display_badge"],
                    "slot_label": badge_info["slot_label"]
                }
                # 审查建议 P1/P2: 实时短信立即落盘至本地 ICCID 权威存储分舱
                try:
                    active_iccid = slot_iccid
                    comp = self.storage_mgr.get_compartment(active_iccid)
                    storage_item = dict(item)
                    storage_item["id"] = event_data.get("id") or event_data.get("msg_id") or f"{evt_slot}_{int(time.time()*1000)}"
                    if sms_timestamp is None:
                        storage_item.pop("timestamp", None)
                    storage_item["iccid"] = active_iccid
                    comp.append_message(storage_item)
                except Exception as e:
                    _log(f"实时短信落盘异常: {e}")

                with self.cache_lock:
                    if evt_slot not in self.recent_sms_by_slot:
                        self.recent_sms_by_slot[evt_slot] = []
                    self.recent_sms_by_slot[evt_slot].insert(0, item)
                    if len(self.recent_sms_by_slot[evt_slot]) > 100:
                        self.recent_sms_by_slot[evt_slot].pop()

                    self.recent_sms_events.insert(0, item)
                    if len(self.recent_sms_events) > 150:
                        self.recent_sms_events.pop()

                self.broadcast_sse("sms_received", item)

            elif event_name in ("call_rx", "call_incoming"):
                raw_time = event_data.get("time") or event_data.get("ts")
                if isinstance(raw_time, (int, float)) and raw_time > 1000000000:
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(raw_time))
                elif raw_time:
                    time_str = str(raw_time)
                else:
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S")

                item = {
                    "slot": evt_slot,
                    "phone": event_data.get("from") or event_data.get("phone") or "未知号码",
                    "time": time_str,
                    "action": event_data.get("action") or "rejected"
                }
                with self.cache_lock:
                    if evt_slot not in self.recent_calls_by_slot:
                        self.recent_calls_by_slot[evt_slot] = []
                    self.recent_calls_by_slot[evt_slot].insert(0, item)
                    if len(self.recent_calls_by_slot[evt_slot]) > 50:
                        self.recent_calls_by_slot[evt_slot].pop()

                    self.recent_calls.insert(0, item)
                    if len(self.recent_calls) > 100:
                        self.recent_calls.pop()

                self.broadcast_sse("call_incoming", item)

            elif event_name == "call_status":
                evt_slot = event_data.get("slot") or "slot_2"
                with self.cache_lock:
                    self.call_status_by_slot[evt_slot] = {
                        "status": event_data.get("status", "IDLE"),
                        "phone": event_data.get("phone", ""),
                        "message": event_data.get("message", ""),
                        "time": time.time()
                    }
                self.broadcast_sse("call_status", {"slot": evt_slot, "data": event_data})

            elif event_name == "fota_status":
                event_data["slot"] = evt_slot
                self.broadcast_sse("fota_status", event_data)

    def register_sse_listener(self) -> queue.Queue:
        q = queue.Queue(maxsize=128)
        with self.sse_lock:
            self.sse_listeners.append(q)
        return q

    def unregister_sse_listener(self, q: queue.Queue):
        with self.sse_lock:
            if q in self.sse_listeners:
                self.sse_listeners.remove(q)

    def perform_serial_ota(self, slot: str = "slot_1", progress_cb=None) -> dict:
        """执行多模组集群定向卡槽串口分块流式热更新 (Serial SOTA)"""
        if luadb_packer is None:
            return {"ok": False, "error": "luadb_packer 模块未加载，无法执行打包"}

        try:
            if progress_cb:
                progress_cb(5, "正在执行 Lua 静态语法预检与标准打包...", "packing")

            manifest = luadb_packer.get_version_manifest()
            target_ver = manifest.get("version", "1.2.7")

            # 自动探测芯片架构 (支持 EC718PV 与 EC618 平台)
            curr_slot_info = {}
            with self.cache_lock:
                for s in self.slots:
                    if s.get("slot") == slot:
                        curr_slot_info = s
                        break
            mod = (curr_slot_info.get("model") or curr_slot_info.get("bsp") or "")
            chip_hint = curr_slot_info.get("chip") or (curr_slot_info.get("capabilities", {}) or {}).get("chip")
            detected_chip = firmware_flasher.normalize_chip_type(mod, chip_hint)
            chip_type = "ec618" if detected_chip == "ec618" else "ec718"

            raw_luadb = luadb_packer.pack_luadb(target_version=target_ver)
            sota_bytes, meta = luadb_packer.pack_sota_package(raw_luadb, target_version=target_ver, chip_type=chip_type)

            total_len = len(sota_bytes)
            chunk_size = 2048
            chunks = [sota_bytes[i:i + chunk_size] for i in range(0, total_len, chunk_size)]
            total_chunks = len(chunks)
            sota_md5 = meta["package_md5"]

            if progress_cb:
                progress_cb(15, f"开始向卡槽 [{slot}] 启动 OTA 协商 (共 {total_chunks} 块, {total_len} 字节)...", "starting")

            start_resp = self.execute_cmd("ota_start", {
                "size": total_len,
                "md5": sota_md5,
                "total_chunks": total_chunks,
                "chunk_size": chunk_size
            }, slot=slot, timeout=8.0)

            if not start_resp.get("ok"):
                err = start_resp.get("msg") or start_resp.get("error") or "模组响应超时"
                return {"ok": False, "error": f"模组拒绝启动 OTA: {err}"}

            for idx, chunk in enumerate(chunks):
                b64_str = base64.b64encode(chunk).decode("ascii")
                pct = 15 + int((idx + 1) / total_chunks * 70)
                if progress_cb:
                    progress_cb(pct, f"正在灌流传输分块 [{idx + 1}/{total_chunks}]...", "flashing")

                chunk_resp = self.execute_cmd("ota_chunk", {
                    "index": idx,
                    "data": b64_str
                }, slot=slot, timeout=6.0)

                if not chunk_resp.get("ok"):
                    self.execute_cmd("ota_abort", {}, slot=slot, timeout=2.0)
                    return {"ok": False, "error": f"分块 [{idx + 1}/{total_chunks}] 传输失败: {chunk_resp.get('msg')}"}

            if progress_cb:
                progress_cb(88, "分块传输完成，模组正在烧录 Flash 并校验 MD5...", "burning")

            finish_resp = self.execute_cmd("ota_finish", {
                "md5": sota_md5
            }, slot=slot, timeout=20.0)

            if not finish_resp.get("ok"):
                return {"ok": False, "error": f"模组固件烧录失败: {finish_resp.get('msg') or finish_resp.get('error')}"}

            if progress_cb:
                progress_cb(92, "固件烧录完成！模组正在软重启与置换固件...", "rebooting")

            # 缓冲等待模组重启（1.5s 后触发 rtos.reboot，USB 重举约 2~4 秒）
            t_start = time.time()
            reboot_detected = False
            while time.time() - t_start < 15.0:
                time.sleep(1.0)
                status_resp = self.execute_cmd("get_status", {}, slot=slot, timeout=2.0)
                if status_resp.get("ok") and status_resp.get("data"):
                    d = status_resp["data"]
                    curr_v = d.get("version") or d.get("firmware_version")
                    if curr_v == target_ver:
                        reboot_detected = True
                        break

            if not reboot_detected:
                err_msg = f"模组升级重启超时 (15s)，未检测到新固件版本生效"
                if progress_cb:
                    progress_cb(95, err_msg, "failed")
                return {"ok": False, "error": err_msg}

            if progress_cb:
                progress_cb(100, f"模组已成功平滑升级至 v{target_ver}！", "success")
            return {"ok": True, "target_version": target_ver, "msg": f"热更新完成，模组已重启并上线 v{target_ver}"}
        except Exception as e:
            return {"ok": False, "error": f"热更新异常: {str(e)}"}

    def broadcast_sse(self, event_name: str, payload: dict):
        with self.sse_lock:
            listeners = list(self.sse_listeners)
        for q in listeners:
            try:
                q.put_nowait({"event": event_name, "data": payload})
            except queue.Full:
                pass


# =========================================================================
# Web 服务器：HTTP Handler 与线程池
# =========================================================================

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class GatewayWebHandler(BaseHTTPRequestHandler):
    server_version = "Air780ClusterWeb/2.0"

    def log_message(self, format, *args):
        """覆盖 BaseHTTPRequestHandler 的默认 stderr 输出，防止 windowed 模式下无控制台报错"""
        if sys.stderr is not None and not isinstance(sys.stderr, _SafeStream):
            try:
                sys.stderr.write("%s - - [%s] %s\n" %
                                 (self.address_string(),
                                  self.log_date_time_string(),
                                  format % args))
                sys.stderr.flush()
            except Exception:
                pass

    @property
    def backend(self) -> HubBackendClient:
        return self.server.backend

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    @staticmethod
    def _is_loopback_client(client_ip: Optional[str]) -> bool:
        """严格判定是否为本机回环流量（全面覆盖 IPv4、IPv6 及 Windows 双栈 ::ffff:127.x.x.x）"""
        if not client_ip:
            return False
        if client_ip in ("127.0.0.1", "::1", "localhost", "testclient") or client_ip.startswith("127."):
            return True
        if client_ip.startswith("::ffff:127."):
            return True
        try:
            return ipaddress.ip_address(client_ip).is_loopback
        except (ValueError, TypeError):
            return False

    def _check_client_security(self) -> bool:
        """核验客户端访问权限：内聚在 Web 服务层，守死 do_GET 与 do_POST 双入口"""
        client_ip = self.client_address[0] if (self.client_address and len(self.client_address) > 0) else None
        if not client_ip:
            self._send_forbidden_lan_response()
            return False

        # 1. 本机流量永远拥有最高特权，直接放行 (防误锁死看门狗)
        if self._is_loopback_client(client_ip):
            return True

        # 2. 外部局域网客户端：从底层 HTTP 服务器实例读取开关状态
        lan_enabled = getattr(self.server, "lan_access_enabled", False)
        if not lan_enabled:
            self._send_forbidden_lan_response()
            return False

        return True

    def _send_forbidden_lan_response(self):
        """向未授权的局域网外部客户端返回大白话 403 页面或 JSON 响应，并附带 CORS 标头"""
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            data = {
                "ok": False,
                "error": "forbidden",
                "message": "🔒 局域网跨设备访问已在控制台关闭。如需在手机上查看验证码，请在电脑本机控制台打开【系统设置】开启「允许同局域网跨设备访问」。"
            }
            self._send_json_resp(403, data)
            return

        html_content = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>局域网跨设备访问已关闭 - CellHive 数字蜂巢</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: #0b0f19;
      color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }
    .card {
      background: #1e293b;
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 12px;
      padding: 2rem;
      max-width: 480px;
      width: 100%;
      text-align: center;
      box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5), 0 8px 10px -6px rgba(0, 0, 0, 0.5);
    }
    .icon {
      font-size: 3rem;
      margin-bottom: 1rem;
      line-height: 1;
    }
    h1 {
      font-size: 1.25rem;
      font-weight: 700;
      color: #f8fafc;
      margin-bottom: 0.75rem;
    }
    p {
      font-size: 0.875rem;
      color: #94a3b8;
      line-height: 1.6;
      margin-bottom: 1.25rem;
    }
    .badge {
      display: inline-block;
      background: rgba(239, 68, 68, 0.15);
      color: #f87171;
      border: 1px solid rgba(239, 68, 68, 0.3);
      padding: 0.25rem 0.75rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      margin-bottom: 1rem;
    }
    .tips {
      background: rgba(15, 23, 42, 0.6);
      border-radius: 8px;
      padding: 0.85rem;
      font-size: 0.8rem;
      color: #cbd5e1;
      text-align: left;
      line-height: 1.5;
      border: 1px dashed rgba(255, 255, 255, 0.1);
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">🔒</div>
    <div class="badge">403 访问受限 · 安全隔离</div>
    <h1>局域网跨设备访问已在控制台关闭</h1>
    <p>为保护您的短信与验证码高密隐私，避免同一 Wi-Fi 下的其他人偷窥，网关默认仅允许电脑本机访问。</p>
    <div class="tips">
      <strong>💡 如何在手机上开启：</strong><br>
      请在插着网关的电脑上打开控制台，点击右上角【系统设置】，在「4. 局域网访问与跨设备协作」中开启<strong>「允许同局域网跨设备访问」</strong>开关并点击保存即可。
    </div>
  </div>
</body>
</html>"""
        body = html_content.encode("utf-8")
        self.send_response(403)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json_resp(self, status_code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if not self._check_client_security():
            return
        if hasattr(self.backend, "touch_activity"):
            self.backend.touch_activity()
        try:
            self._handle_get()
        except Exception as e:
            import traceback
            trace_str = traceback.format_exc()
            _log(f"HTTP GET Error: {e}\n{trace_str}")
            try:
                self._send_json_resp(500, {"ok": False, "error": str(e), "trace": trace_str})
            except Exception:
                pass

    def _handle_get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        target_slot = query.get("slot", [None])[0] or self.backend.active_slot or "slot_1"

        # 1. 网页静态页面
        if path in ("/", "/index.html"):
            if os.path.exists(INDEX_HTML_PATH):
                try:
                    with open(INDEX_HTML_PATH, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self._send_cors_headers()
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                except Exception as e:
                    self._send_json_resp(500, {"ok": False, "error": f"加载 index.html 失败: {e}"})
                    return
            else:
                fallback_html = "<html><body><h1>数字蜂巢 · CellHive</h1><p>Web 资源未找到</p></body></html>".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(fallback_html)))
                self.end_headers()
                self.wfile.write(fallback_html)
                return

        # 1.1 网站图标静态响应
        if path == "/favicon.ico":
            ico_path = os.path.join(WEB_DIR, "favicon.ico")
            if not os.path.exists(ico_path):
                ico_path = os.path.join(os.path.dirname(WEB_DIR), "app.ico")
            if os.path.exists(ico_path):
                try:
                    with open(ico_path, "rb") as f:
                        ico_content = f.read()
                    self.send_response(200)
                    self._send_cors_headers()
                    self.send_header("Content-Type", "image/x-icon")
                    self.send_header("Content-Length", str(len(ico_content)))
                    self.end_headers()
                    self.wfile.write(ico_content)
                    return
                except Exception:
                    pass
            self.send_response(204)
            self.end_headers()
            return

        # 2. SSE 实时事件推送流接口
        if path == "/api/events":
            self.handle_sse_stream()
            return

        # 2.9 未分配/全新模组嗅探接口 (AIR-35)
        if path == "/api/flasher/unassigned":
            resp = self.backend.execute_cmd("get_unassigned_dongles", timeout=2.0)
            if resp.get("ok"):
                unassigned = resp.get("data", {}).get("unassigned", [])
                self._send_json_resp(200, {"ok": True, "unassigned": unassigned, "count": len(unassigned)})
            else:
                self._send_json_resp(200, {"ok": True, "unassigned": [], "count": 0})
            return

        # 3. 集群卡槽列表接口
        if path == "/api/slots":
            # 向中枢同步刷新一次最新卡槽
            resp = self.backend.execute_cmd("get_slots", timeout=2.0)
            if resp.get("ok"):
                slots_data = resp.get("data", {}).get("slots", [])
                self.backend._update_slots_cache(slots_data)
            with self.backend.cache_lock:
                slots_list = list(self.backend.slots)
                act_slot = self.backend.active_slot
            self._send_json_resp(200, {
                "ok": True,
                "slots": slots_list,
                "active_slot": act_slot,
                "count": len(slots_list)
            })
            return

        # 3.1 全集群全景驾驶舱接口 (AIR-22)
        if path == "/api/cluster/overview":
            resp = self.backend.execute_cmd("get_cluster_overview", timeout=3.0)
            if resp.get("ok"):
                self._send_json_resp(200, {"ok": True, "data": resp.get("data")})
            else:
                self._send_json_resp(500, {"ok": False, "error": resp.get("error") or "获取集群概览失败"})
            return

        # 3.2 全集群健康监控状态机接口 (AIR-22)
        if path == "/api/cluster/health":
            resp = self.backend.execute_cmd("get_cluster_health", timeout=3.0)
            if resp.get("ok"):
                self._send_json_resp(200, {"ok": True, "data": resp.get("data")})
            else:
                self._send_json_resp(500, {"ok": False, "error": resp.get("error") or "获取集群健康数据失败"})
            return

        # 3.3 全集群防 OOM 复合游标聚合收件箱 (AIR-22)
        if path == "/api/cluster/messages":
            limit_val = 15
            if "limit" in query:
                try:
                    limit_val = max(1, min(50, int(query["limit"][0])))
                except Exception:
                    pass
            cursor_val = query.get("cursor", [None])[0]
            slot_filter = query.get("slot", ["all"])[0]

            slots_meta_map = {}
            with self.backend.cache_lock:
                for s in self.backend.slots:
                    s_id = s.get("slot")
                    if s_id:
                        slots_meta_map[s_id] = s

            res = self.backend.storage_mgr.get_aggregated_messages(
                limit=limit_val,
                cursor=cursor_val,
                slot_filter=slot_filter,
                slots_meta=slots_meta_map
            )
            self._send_json_resp(200, {"ok": True, "data": res})
            return
            return

        # 4. 获取实时全局状态看板 (支持按 slot 路由)
        if path == "/api/status":
            resp = self.backend.execute_cmd("get_status", slot=target_slot, timeout=2.5)
            if resp.get("ok"):
                self.backend.is_hardware_connected = True
                raw_data = resp.get("data", {})
                rndis_val = raw_data.get("rndis") if "rndis" in raw_data else raw_data.get("rndis_enable", False)
                data_val = raw_data.get("cellular_data") if "cellular_data" in raw_data else raw_data.get("cellular_data_enable", False)
                sms_count_val = raw_data.get("blackbox_count") if "blackbox_count" in raw_data else raw_data.get("sms_count", 0)
                iccid_val = raw_data.get("iccid", "")
                if not iccid_val:
                    with self.backend.cache_lock:
                        for s in self.backend.slots:
                            if s.get("slot") == target_slot and s.get("iccid"):
                                iccid_val = s["iccid"]
                                break
                norm_status = {
                    "online": True,
                    "slot": target_slot,
                    "model": raw_data.get("bsp") or raw_data.get("model") or "Air780 Series",
                    "imei": raw_data.get("imei", ""),
                    "iccid": iccid_val,
                    "version": raw_data.get("version") or raw_data.get("current_version") or "1.2.0",
                    "csq": raw_data.get("csq", 0),
                    "rsrp": raw_data.get("rsrp", 0),
                    "temp": raw_data.get("temp", 0),
                    "vbat": raw_data.get("vbat", 0),
                    "rndis": bool(rndis_val),
                    "rndis_enable": bool(rndis_val),
                    "cellular_data": bool(data_val),
                    "cellular_data_enable": bool(data_val),
                    "sms_count": sms_count_val,
                    "uptime": raw_data.get("uptime_seconds") if "uptime_seconds" in raw_data else raw_data.get("uptime", 0),
                    "lua_mem_kb": raw_data.get("lua_mem_kb", 0),
                    "capabilities": raw_data.get("capabilities", {}),
                    "raw": raw_data
                }
                self.backend._update_status_cache(norm_status, slot=target_slot)
                self._send_json_resp(200, {"ok": True, "online": True, "slot": target_slot, "data": norm_status})
            else:
                err_msg = resp.get("error") or resp.get("msg") or "模组未响应，物理设备已拔出"
                self.backend._update_status_cache({"online": False}, slot=target_slot)
                with self.backend.cache_lock:
                    cached = dict(self.backend.latest_status_by_slot.get(target_slot, {}))
                cached["online"] = False
                cached["slot"] = target_slot
                self._send_json_resp(200, {"ok": False, "online": False, "slot": target_slot, "error": err_msg, "data": cached})
            return

        # 5. 获取短信历史记录 (本地权威存储 + 服务端全文检索 + 复合游标懒加载)
        if path == "/api/history":
            limit_val = 40
            if "limit" in query:
                try:
                    limit_val = max(1, min(100, int(query["limit"][0])))
                except Exception:
                    pass
            cursor_val = query.get("cursor", [None])[0]
            kw = query.get("keyword", [None])[0]
            order = query.get("order", ["desc"])[0].lower()
            requested_slot = query.get("slot", [None])[0]

            slots_meta_map = {}
            with self.backend.cache_lock:
                for s in self.backend.slots:
                    s_id = s.get("slot")
                    if s_id:
                        slots_meta_map[s_id] = s

            res = self.backend.storage_mgr.get_aggregated_messages(
                limit=limit_val,
                cursor=cursor_val,
                slot_filter=requested_slot,
                keyword=kw,
                order=order,
                slots_meta=slots_meta_map
            )

            self._send_json_resp(200, {
                "ok": True,
                "slot": requested_slot or "all",
                "items": res["items"],
                "list": res["items"],
                "total": res["total"],
                "count": res["count"],
                "next_cursor": res["next_cursor"],
                "has_more": res["has_more"],
                "data": res
            })
            return

        # 6. 获取来电拦截记录 (支持按 slot 路由)
        if path == "/api/calls":
            with self.backend.cache_lock:
                calls = list(self.backend.recent_calls_by_slot.get(target_slot, []))
            self._send_json_resp(200, {"ok": True, "slot": target_slot, "items": calls, "count": len(calls)})
            return

        # 6.1 获取通话状态 (AIR-30)
        if path == "/api/call/status":
            with self.backend.cache_lock:
                status_info = getattr(self.backend, "call_status_by_slot", {}).get(target_slot, {"status": "IDLE"})
            self._send_json_resp(200, {"ok": True, "slot": target_slot, "data": status_info})
            return

        # 7. 获取网关通用配置 (含通知与 MCP 开关)
        if path == "/api/notify/results":
            result = self.backend.execute_cmd("get_notify_results", timeout=3.0)
            if result.get("ok"):
                self._send_json_resp(200, {"ok": True, "data": result.get("data") or {}})
            else:
                self._send_json_resp(503, {"ok": False, "error": "后台通知记录暂不可用"})
            return

        if path in ("/api/config", "/api/config/notify"):
            if os.path.exists(GATEWAY_CONFIG_PATH):
                try:
                    with open(GATEWAY_CONFIG_PATH, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    public_cfg = _public_gateway_config(cfg)
                    self._send_json_resp(200, {"ok": True, "config": public_cfg, "data": public_cfg})
                    return
                except Exception as e:
                    self._send_json_resp(500, {"ok": False, "error": f"读取配置失败: {e}"})
                    return
            else:
                self._send_json_resp(200, {"ok": True, "config": {}, "data": {}})
            return

        # 7.1 获取系统运行环境、偏好与关于信息 (AIR-57)
        if path == "/api/system/settings":
            cfg_system = {}
            if os.path.exists(GATEWAY_CONFIG_PATH):
                try:
                    with open(GATEWAY_CONFIG_PATH, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    cfg_system = cfg.get("system", {}) if isinstance(cfg, dict) else {}
                except Exception:
                    pass

            lan_ip = get_local_lan_ip()
            log_info = get_log_size_info()
            autostart_live = get_autostart_status()

            web_port = DEFAULT_WEB_PORT
            try:
                web_port = self.server.server_port or DEFAULT_WEB_PORT
            except Exception:
                pass

            # 探测 tools/mcp_server/server.py 绝对路径
            # 候选1: 源码或 exe 目录同级的 tools/mcp_server/server.py
            # 候选2: 从 dist 向上查找工程根目录下的 tools/mcp_server/server.py
            exe_or_file_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
            candidate_paths = [
                os.path.abspath(os.path.join(exe_or_file_dir, "..", "..", "..", "tools", "mcp_server", "server.py")),
                os.path.abspath(os.path.join(exe_or_file_dir, "..", "..", "tools", "mcp_server", "server.py")),
                os.path.abspath(os.path.join(exe_or_file_dir, "..", "mcp_server", "server.py")),
                os.path.abspath(os.path.join(exe_or_file_dir, "tools", "mcp_server", "server.py"))
            ]
            mcp_py = ""
            for cp in candidate_paths:
                if os.path.isfile(cp):
                    mcp_py = cp
                    break
            if not mcp_py:
                mcp_py = candidate_paths[0]

            daily_reboot_hour_val = 4
            try:
                raw_h = cfg_system.get("daily_reboot_hour", 4)
                h = int(raw_h) if raw_h is not None else 4
                daily_reboot_hour_val = h if 0 <= h <= 23 else 4
            except (ValueError, TypeError):
                daily_reboot_hour_val = 4

            client_ip = self.client_address[0] if (self.client_address and len(self.client_address) > 0) else None
            is_client_local = self._is_loopback_client(client_ip)
            lan_access_enabled = bool(getattr(self.server, "lan_access_enabled", False))

            settings_data = {
                "version": APP_VERSION,
                "build_type": "standalone_exe" if getattr(sys, "frozen", False) else "source",
                "platform": "Windows" if sys.platform == "win32" else sys.platform,
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "autostart": autostart_live,
                "auto_copy_otp": bool(cfg_system.get("auto_copy_otp", True)),
                "desktop_notification": bool(cfg_system.get("desktop_notification", True)),
                "privacy_mode": bool(cfg_system.get("privacy_mode", False)),
                "play_sound": bool(cfg_system.get("play_sound", True)),
                "store_on_board": bool(cfg_system.get("store_on_board", False)),
                "daily_reboot": bool(cfg_system.get("daily_reboot", False)),
                "daily_reboot_hour": daily_reboot_hour_val,
                "lan_access": lan_access_enabled,
                "lan_access_enabled": lan_access_enabled,
                "is_current_client_local": is_client_local,
                "mcp_server_path": mcp_py.replace("\\", "/"),
                "lan_ip": lan_ip,
                "lan_url": f"http://{lan_ip}:{web_port}",
                "is_local_only": lan_ip == "127.0.0.1",
                "data_dir": DATA_DIR,
                "log_dir": get_log_dir(),
                "log_size_bytes": log_info["bytes"],
                "log_size_human": log_info["human"]
            }
            self._send_json_resp(200, {"ok": True, "data": settings_data})
            return

        # 8. 获取固件版本与待更元数据信息 (AIR-38 / v4 门禁规范)
        if path == "/api/control/upgrade_info":
            # 每次强制向 Hub 请求新鲜 get_slots，失败返回安全的 unknown_device，不复用旧缓存
            resp = self.backend.execute_cmd("get_slots", timeout=1.5)
            if not resp or not isinstance(resp, dict) or not resp.get("ok"):
                self._send_json_resp(200, {
                    "ok": True,
                    "check_state": "unknown_device",
                    "reason": "向网关核心请求新鲜设备状态失败",
                    "source": "bundled",
                    "slot": target_slot,
                    "device_id": None,
                    "model": "未知",
                    "current_version": None,
                    "package_version": None,
                    "package_id": None,
                    "changelog": "",
                    "size_kb": None,
                    "can_install": False,
                    "target_version": "",
                    "has_update": False,
                    "sota_supported": False,
                    "tip": "向网关核心请求新鲜设备状态失败",
                    "upgrade_method": "向网关核心请求新鲜设备状态失败"
                })
                return

            slots_data = resp.get("data", {}).get("slots", [])
            curr_slot_info = None
            for s in slots_data:
                if isinstance(s, dict) and s.get("slot") == target_slot:
                    curr_slot_info = s
                    break

            gate_res = evaluate_upgrade_gate(curr_slot_info)
            device_id = extract_trusted_device_id(curr_slot_info)
            current_ver = curr_slot_info.get("version") if curr_slot_info else None
            model = curr_slot_info.get("model") if curr_slot_info else "未知"

            self._send_json_resp(200, {
                "ok": True,
                "check_state": gate_res["check_state"],
                "reason": gate_res["reason"],
                "source": "bundled",
                "slot": target_slot,
                "device_id": device_id,
                "model": model or "未知",
                "current_version": current_ver,
                "package_version": gate_res["package_version"],
                "package_id": gate_res["package_id"],
                "changelog": gate_res["changelog"],
                "size_kb": gate_res["size_kb"],
                "can_install": gate_res["can_install"],
                # 兼容旧前端字段
                "target_version": gate_res["package_version"] or "",
                "has_update": (gate_res["check_state"] == "available" and gate_res["can_install"]),
                "sota_supported": (gate_res["check_state"] not in ("unsupported", "incompatible")),
                "tip": gate_res["reason"],
                "upgrade_method": gate_res["reason"],
            })
            return

        self._send_json_resp(404, {"ok": False, "error": "接口不存在"})

    @staticmethod
    def _format_error_message(resp: dict) -> str:
        if not isinstance(resp, dict):
            return "未知错误"
        code = resp.get("code")
        msg = str(resp.get("msg", "")).strip()
        data_sec = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        reason = data_sec.get("reason", "")

        if code == -409 or msg == "SMS_RESULT_UNKNOWN" or reason == "previous_modem_result_pending":
            return "上一条短信发送结果未决，设备已保护发送；请先核对原尝试，勿直接重试或重置"
        if code == -429 or msg == "QUEUE_FULL":
            return "短信发送队列已满，请等待前序短信处理完成"
        if code == -101 or msg == "PARAM_ERR":
            return "短信参数错误：手机号码或短信内容不能为空"
        if code == -102 or msg == "SEND_FAILED":
            return "模组底层射频发射失败"
        if code == -408 or msg == "UNKNOWN" or reason == "modem_result_timeout":
            return "等待模组发送结果超时，结果未知"
        if code == -1 or msg == "SENT_FAILED":
            return "基站发送失败"

        return resp.get("error") or msg or reason or f"请求失败 (错误码: {code})"

    def do_POST(self):
        if not self._check_client_security():
            return
        if hasattr(self.backend, "touch_activity"):
            self.backend.touch_activity()
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        content_len = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_len > 0:
            raw_body = self.rfile.read(content_len)
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except Exception:
                pass

        qs_slot = query.get("slot", [None])[0]
        target_slot = qs_slot or body.get("slot") or self.backend.active_slot or "slot_1"

        # 1. 切换前端当前活跃卡槽
        if path == "/api/slots/switch":
            new_slot = body.get("slot")
            if not new_slot:
                self._send_json_resp(400, {"ok": False, "error": "必须提供目标 slot"})
                return
            with self.backend.cache_lock:
                self.backend.active_slot = new_slot
            _log(f"用户已切换当前活跃卡槽 -> 【{new_slot}】")
            self.backend.broadcast_sse("cluster_update", {"slots": self.backend.slots, "active_slot": new_slot})
            self._send_json_resp(200, {"ok": True, "active_slot": new_slot})
            return

        # 2. 发送短信 (支持定向卡槽与集群智能路由分流 AIR-22)
        if path == "/api/sms/send":
            phone = (body.get("phone") or "").strip()
            content = (body.get("content") or "").strip()
            strategy = (body.get("strategy") or "operator_affinity").strip()
            dry_run = bool(body.get("dry_run", False))
            if not phone or not content:
                self._send_json_resp(400, {"ok": False, "error": "手机号与正文不能为空"})
                return

            # 区分 direct 与 auto 契约：显式传 slot 则为 direct；未传或为 auto 则为 None
            specified_slot = body.get("slot")
            if specified_slot in ("", "auto", None):
                target_slot = None
            else:
                target_slot = str(specified_slot).strip()

            wait_term = body.get("wait_terminal", False)
            resp = self.backend.execute_cmd(
                "send_sms",
                params={"phone": phone, "content": content, "strategy": strategy, "slot": target_slot, "dry_run": dry_run},
                slot=target_slot,
                timeout=12.0,
                wait_terminal=wait_term and not dry_run
            )
            if resp.get("ok"):
                used_slot = resp.get("slot") or target_slot or "slot_1"
                self._send_json_resp(200, {
                    "ok": True,
                    "slot": used_slot,
                    "status": "routed" if dry_run else ("submitted" if resp.get("msg") in ("SENT_OK", "SENT") else "queued"),
                    "request_id": resp.get("id"),
                    "strategy": resp.get("routed_strategy") or strategy,
                    "fallback": resp.get("fallback_used", False),
                    "panic_mode": resp.get("panic_mode", False),
                    "msg": "SUBMITTED" if resp.get("msg") in ("SENT_OK", "SENT") else resp.get("msg", "UNKNOWN"),
                    "data": {"accepted": True} if resp.get("msg") in ("SENT_OK", "SENT") else resp.get("data")
                })
            else:
                err_msg = self._format_error_message(resp)
                self._send_json_resp(500, {"ok": False, "slot": target_slot, "error": err_msg, "code": resp.get("code")})
            return

        # 2.1 重置短信队列与发送保护状态
        if path == "/api/sms/reset":
            resp = self.backend.execute_cmd("reset_sms", params={}, slot=target_slot, timeout=5.0)
            if resp.get("ok"):
                self._send_json_resp(200, {"ok": True, "slot": target_slot, "msg": "短信发送状态已重置", "data": resp.get("data")})
            else:
                err_msg = self._format_error_message(resp)
                self._send_json_resp(500, {"ok": False, "slot": target_slot, "error": err_msg, "code": resp.get("code")})
            return

        # 3. 控制 RNDIS 开关 (支持定向卡槽)
        if path == "/api/control/rndis":
            enable = bool(body.get("enable", False))
            resp = self.backend.execute_cmd("set_rndis", params={"enable": enable}, slot=target_slot, timeout=6.0)
            if resp.get("ok"):
                with self.backend.cache_lock:
                    if target_slot not in self.backend.latest_status_by_slot:
                        self.backend.latest_status_by_slot[target_slot] = {}
                    self.backend.latest_status_by_slot[target_slot]["rndis"] = enable
                    self.backend.latest_status_by_slot[target_slot]["rndis_enable"] = enable
                self._send_json_resp(200, {"ok": True, "slot": target_slot, "rndis": enable, "msg": "RNDIS 配置已更新"})
            else:
                self._send_json_resp(200, {"ok": False, "slot": target_slot, "error": resp.get("error") or "RNDIS 切换失败"})
            return

        # 4. 控制板载蜂窝数据开关 (支持定向卡槽)
        if path == "/api/control/data":
            enable = bool(body.get("enable", False))
            resp = self.backend.execute_cmd("set_cellular_data", params={"enable": enable}, slot=target_slot, timeout=6.0)
            if resp.get("ok"):
                with self.backend.cache_lock:
                    if target_slot not in self.backend.latest_status_by_slot:
                        self.backend.latest_status_by_slot[target_slot] = {}
                    self.backend.latest_status_by_slot[target_slot]["cellular_data"] = enable
                    self.backend.latest_status_by_slot[target_slot]["cellular_data_enable"] = enable
                self._send_json_resp(200, {"ok": True, "slot": target_slot, "cellular_data": enable, "msg": "蜂窝数据配置已更新"})
            else:
                self._send_json_resp(200, {"ok": False, "slot": target_slot, "error": resp.get("error") or "蜂窝数据切换失败"})
            return

        # 5. 软重启模组 (支持定向卡槽)
        if path == "/api/control/reboot":
            reason = body.get("reason", "web_console_action")
            resp = self.backend.execute_cmd("reboot", params={"reason": reason}, slot=target_slot, timeout=3.0)
            self._send_json_resp(200, {"ok": True, "slot": target_slot, "msg": "重启指令已成功下发至网关"})
            return

        # 5.1 发起 VoLTE 电话拨号呼叫 (AIR-30)
        if path == "/api/call/dial":
            phone = (body.get("phone") or body.get("number") or "").strip()
            timeout_sec = int(body.get("timeout") or body.get("timeout_seconds") or 15)
            hangup_on_ans = bool(body.get("hangup_on_answer", True))

            if not phone:
                self._send_json_resp(400, {"ok": False, "error": "目标手机号不能为空"})
                return

            resp = self.backend.execute_cmd(
                "call_dial",
                params={"phone": phone, "timeout": timeout_sec, "hangup_on_answer": hangup_on_ans, "slot": target_slot},
                slot=target_slot,
                timeout=8.0
            )
            if resp.get("ok"):
                used_slot = resp.get("slot") or target_slot or "slot_2"
                self._send_json_resp(200, {
                    "ok": True,
                    "slot": used_slot,
                    "status": "DIALING",
                    "timeout": timeout_sec,
                    "msg": f"正在向 {phone} 发起 VoLTE 呼叫，{timeout_sec}秒后自动挂断",
                    "data": resp.get("data")
                })
            else:
                err_msg = resp.get("error") or self._format_error_message(resp)
                self._send_json_resp(400 if ("HARDWARE_UNSUPPORTED" in str(resp) or "NO_VOLTE_SLOT" in str(resp)) else 500, {
                    "ok": False,
                    "slot": target_slot,
                    "error": err_msg,
                    "code": resp.get("code")
                })
            return

        # 5.2 手动挂断当前呼叫 (AIR-30)
        if path == "/api/call/hangup":
            resp = self.backend.execute_cmd("call_hangup", slot=target_slot, timeout=4.0)
            self._send_json_resp(200, {
                "ok": resp.get("ok", False),
                "slot": target_slot,
                "msg": "已执行挂断指令" if resp.get("ok") else (resp.get("error") or "挂断失败")
            })
            return

        # 6. 触发空中 FOTA 更新 (支持定向卡槽)
        if path == "/api/control/fota":
            resp = self.backend.execute_cmd("trigger_fota", slot=target_slot, timeout=6.0)
            if resp.get("ok"):
                self._send_json_resp(200, {"ok": True, "slot": target_slot, "msg": "已触发板卡 FOTA 固件检测"})
            else:
                self._send_json_resp(200, {"ok": False, "slot": target_slot, "error": resp.get("error") or "FOTA 触发失败"})
            return

        # 6.1 删除单条短信 (支持墓碑持久化)
        if path == "/api/control/delete_sms":
            msg_id = body.get("id")
            sender = body.get("sender") or body.get("phone") or ""
            content = body.get("content") or ""
            sms_time = body.get("time") or ""
            active_iccid = body.get("iccid")
            if not active_iccid:
                with self.backend.cache_lock:
                    s_meta = next((s for s in self.backend.slots if s.get("slot") == target_slot), {})
                    active_iccid = s_meta.get("iccid")
            comp = self.backend.storage_mgr.get_compartment(active_iccid)
            succ = comp.add_tombstone(msg_id, sender, content, sms_time)
            self._send_json_resp(200, {
                "ok": True,
                "slot": target_slot,
                "msg": "已从本地归档移除并生成墓碑记录" if succ else "已记录删除墓碑"
            })
            return

        # 10. 串口分块平滑热更 (AIR-38 / v4 门禁安全链)
        if path == "/api/control/upgrade_script":
            data = body or {}
            action_slot = data.get("slot") or target_slot
            expected_device_id = data.get("expected_device_id")
            req_package_id = data.get("package_id")

            # 1. 每次必须向 Hub 请求新鲜 get_slots，读取失败直接拒绝，不复用旧缓存
            resp = self.backend.execute_cmd("get_slots", timeout=1.5)
            if not resp or not isinstance(resp, dict) or not resp.get("ok"):
                self._send_json_resp(400, {
                    "ok": False,
                    "check_state": "unknown_device",
                    "error": "向网关核心请求新鲜设备状态失败，请确认设备是否在线",
                    "reason": "向网关核心请求新鲜设备状态失败",
                    "slot": action_slot
                })
                return

            slots_data = resp.get("data", {}).get("slots", [])
            curr_slot_info = None
            for s in slots_data:
                if isinstance(s, dict) and s.get("slot") == action_slot:
                    curr_slot_info = s
                    break

            if not curr_slot_info or curr_slot_info.get("online") is not True:
                self._send_json_resp(400, {
                    "ok": False,
                    "check_state": "unknown_device",
                    "error": "设备离线或在线状态异常 (online 状态非 True)",
                    "reason": "设备离线或在线状态异常",
                    "slot": action_slot
                })
                return

            # 2. 核验新鲜且可信的设备稳定物理身份 (IMEI)
            actual_device_id = extract_trusted_device_id(curr_slot_info)
            if not actual_device_id:
                self._send_json_resp(400, {
                    "ok": False,
                    "check_state": "unknown_device",
                    "error": "设备稳定物理身份缺失 (缺少有效 IMEI)，安装已阻断",
                    "reason": "设备稳定物理身份缺失",
                    "slot": action_slot
                })
                return

            if not expected_device_id or str(expected_device_id).strip() != actual_device_id:
                self._send_json_resp(400, {
                    "ok": False,
                    "check_state": "unknown_device",
                    "error": f"设备身份已发生变化或不匹配 (预期: {expected_device_id}, 实际: {actual_device_id})，安装已阻断",
                    "reason": "设备身份已发生变化 (换设备)",
                    "slot": action_slot
                })
                return

            # 3. 检查任务忙态 (busy)
            with flashing_lock:
                if flashing_state.get("is_flashing"):
                    self._send_json_resp(423, {
                        "ok": False,
                        "check_state": "busy",
                        "error": f"已有卡槽 [{flashing_state.get('slot')}] 正在烧录升级中，请稍候...",
                        "reason": f"已有卡槽 [{flashing_state.get('slot')}] 正在烧录升级中，请稍候...",
                        "slot": action_slot
                    })
                    return

            # 4. 门禁全量校验 (重新校验包、版本、兼容性与安全能力)
            gate_res = evaluate_upgrade_gate(curr_slot_info)

            # 核验 package_id 防换包
            if not req_package_id:
                self._send_json_resp(400, {
                    "ok": False,
                    "check_state": gate_res["check_state"],
                    "error": "请求缺少 package_id 参数，请重新检查更新",
                    "reason": "请求缺少 package_id 参数",
                    "slot": action_slot
                })
                return

            if not gate_res["package_id"] or gate_res["package_id"] != req_package_id:
                self._send_json_resp(400, {
                    "ok": False,
                    "check_state": gate_res["check_state"] if gate_res["check_state"] in ("no_package", "invalid_package") else "invalid_package",
                    "error": f"更新包身份不匹配 (预期: {gate_res.get('package_id')}, 请求: {req_package_id})，安装已阻断",
                    "reason": "更新包身份不匹配 (换包)",
                    "slot": action_slot
                })
                return

            # 5. 任何非 available 状态在服务端一律安全拒绝
            if gate_res["check_state"] != "available" or not gate_res["can_install"]:
                status_code = 423 if gate_res["check_state"] == "busy" else 400
                self._send_json_resp(status_code, {
                    "ok": False,
                    "check_state": gate_res["check_state"],
                    "error": gate_res["reason"],
                    "reason": gate_res["reason"],
                    "slot": action_slot
                })
                return

            # 6. 核心安全底线：v3 安全升级执行链未经验收，不得调用旧执行器或现场打包逻辑！
            # 绝对切断向 _ota_worker / perform_serial_ota 的调用，设备副作用严格为零！
            self._send_json_resp(400, {
                "ok": False,
                "check_state": "host_not_ready",
                "error": "上位机安全更新功能尚未就绪 (v3 安全执行链未经验收)",
                "reason": "上位机安全更新功能尚未就绪",
                "slot": action_slot
            })
            return

        # 10.1 硬件底层线刷与全新模块烧录 (FlashToolCLI · AIR-35 通用多芯片引擎)
        if path == "/api/control/flash":
            data = body or {}
            action_slot = data.get("slot") or target_slot
            req_port = data.get("port")
            req_chip = data.get("chip_type") or data.get("chip") or data.get("recommend_chip")
            req_mode = data.get("mode") or "script"  # 'script' 或 'full'
            req_model = data.get("model") or data.get("hardware_model")
            is_unassigned = bool(data.get("is_unassigned") or action_slot in (None, "", "new_device"))

            curr_slot_info = {}
            if action_slot and action_slot != "new_device":
                with self.backend.cache_lock:
                    for s in self.backend.slots:
                        if s.get("slot") == action_slot:
                            curr_slot_info = s
                            break
            else:
                action_slot = None

            target_port = req_port or curr_slot_info.get("port")
            model_bsp = req_model or curr_slot_info.get("model") or curr_slot_info.get("bsp") or ""

            # 归一化芯片类型 (AIR-44: 原生支持 EC718PV, EC718PM 与 EC618 三大芯片架构)
            import firmware_flasher
            chip_type = firmware_flasher.normalize_chip_type(model_bsp, req_chip)

            def _cli_flash_worker(slot_id, port, chip, mode, model, unassigned_flag):
                self.backend.broadcast_sse("cli_flash_progress", {
                    "slot": slot_id or "new_device",
                    "percent": 5,
                    "message": f"正在准备向目标设备 ({port or 'Bootloader自动探测'}) 下发烧录任务 ({chip.upper()} · {'全量' if mode=='full' else '脚本'})...",
                    "stage": "starting"
                })
                # 1. 若为已知卡槽或未分配裸板，挂起串口以防冲突并检查加锁结果
                pause_res = None
                if slot_id:
                    pause_res = self.backend.execute_cmd("pause_for_flash", {"mode": mode}, slot=slot_id, timeout=3.0)
                else:
                    pause_res = self.backend.execute_cmd("pause_for_flash", {"is_unassigned": True, "port": port, "chip": chip, "mode": mode}, timeout=3.0)

                if pause_res and not pause_res.get("ok"):
                    err_msg = pause_res.get("error") or pause_res.get("msg") or "目标模组正忙或串口独占锁定失败"
                    self.backend.broadcast_sse("cli_flash_progress", {
                        "slot": slot_id or "new_device",
                        "percent": 0,
                        "message": f"硬件线刷中断: {err_msg}",
                        "stage": "error"
                    })
                    return
                time.sleep(0.3)

                def _cb(pct, msg):
                    self.backend.broadcast_sse("cli_flash_progress", {
                        "slot": slot_id or "new_device",
                        "percent": pct,
                        "message": msg,
                        "stage": "flashing"
                    })

                try:
                    res = firmware_flasher.flash_hardware_cli(
                        current_vuart_port=port,
                        hardware_model=model,
                        chip_type=chip,
                        mode=mode,
                        progress_cb=_cb
                    )
                except Exception as e:
                    res = {"ok": False, "msg": f"调用烧录引擎异常: {e}"}

                # 2. 恢复串口轮询
                if slot_id:
                    self.backend.execute_cmd("resume_after_flash", {}, slot=slot_id, timeout=3.0)
                else:
                    self.backend.execute_cmd("resume_after_flash", {"is_unassigned": True, "port": port}, timeout=3.0)

                if res.get("ok"):
                    self.backend.broadcast_sse("cli_flash_progress", {
                        "slot": slot_id or "new_device",
                        "percent": 100,
                        "message": f"线刷完成！{chip.upper()} 模组已平滑重启生效",
                        "stage": "success",
                        "port": res.get("port"),
                        "chip": chip,
                        "mode": mode
                    })
                else:
                    self.backend.broadcast_sse("cli_flash_progress", {
                        "slot": slot_id or "new_device",
                        "percent": 0,
                        "message": f"硬件线刷中断: {res.get('msg')}",
                        "stage": "error"
                    })

            threading.Thread(
                target=_cli_flash_worker,
                args=(action_slot, target_port, chip_type, req_mode, model_bsp, is_unassigned),
                daemon=True
            ).start()

            self._send_json_resp(200, {
                "ok": True,
                "slot": action_slot or "new_device",
                "chip": chip_type,
                "mode": req_mode,
                "port": target_port,
                "msg": f"已启动 {chip_type.upper()} ({'全量系统刷入' if req_mode=='full' else '极速应用脚本更新'}) 任务",
                "status": "started"
            })
            return

        # 7. 清空板载历史记录 (支持定向卡槽)
        if path == "/api/control/clear_history":
            resp = self.backend.execute_cmd("clear_history", slot=target_slot, timeout=6.0)
            if resp.get("ok"):
                with self.backend.cache_lock:
                    self.backend.recent_sms_by_slot[target_slot] = []
                self._send_json_resp(200, {"ok": True, "slot": target_slot, "msg": "板载短信黑匣子已清空"})
            else:
                self._send_json_resp(200, {"ok": False, "slot": target_slot, "error": resp.get("error") or "清空黑匣子失败"})
            return

        # 8. 保存网关通用配置 (含通知设置与 MCP 开关)
        if path in ("/api/config", "/api/config/notify"):
            try:
                cur_cfg = {}
                if os.path.exists(GATEWAY_CONFIG_PATH):
                    with open(GATEWAY_CONFIG_PATH, "r", encoding="utf-8") as f:
                        cur_cfg = json.load(f)
                    if not isinstance(cur_cfg, dict):
                        raise ValueError("invalid config root")
                clear_fields = body.get("clear") or {}
                for k, v in body.items():
                    if k == "clear":
                        continue
                    if isinstance(v, dict):
                        section = cur_cfg.get(k)
                        if not isinstance(section, dict):
                            section = {}
                        for field, value in v.items():
                            if k in ("feishu", "dingtalk", "wecom", "bark", "webhook") and field in ("url", "secret") and value == "":
                                continue
                            section[field] = value
                        cur_cfg[k] = section
                    else:
                        cur_cfg[k] = v
                if isinstance(clear_fields, dict):
                    for channel, fields in clear_fields.items():
                        if channel in ("feishu", "dingtalk", "wecom", "bark", "webhook") and isinstance(fields, list):
                            for field in fields:
                                replacement = (body.get(channel) or {}).get(field)
                                if field in ("url", "secret") and not replacement:
                                    cur_cfg.get(channel, {}).pop(field, None)
                for channel in ("feishu", "dingtalk", "wecom", "bark", "webhook"):
                    channel_cfg = cur_cfg.get(channel) or {}
                    if channel_cfg.get("enable") and not channel_cfg.get("url"):
                        self._send_json_resp(400, {"ok": False, "error": f"{channel} 已启用但未配置地址"})
                        return

                next_path = GATEWAY_CONFIG_PATH + ".next"
                with open(next_path, "w", encoding="utf-8") as f:
                    json.dump(cur_cfg, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(next_path, GATEWAY_CONFIG_PATH)

                reload_result = self.backend.execute_cmd("reload_notify_config", timeout=3.0)
                active = bool(reload_result.get("ok"))
                board_sync = {}
                board_sync_known = False
                if active:
                    slots_resp = self.backend.execute_cmd("get_slots", timeout=3.0)
                    board_sync_known = bool(slots_resp.get("ok"))
                    slots = (slots_resp.get("data") or {}).get("slots") or []
                    board_cfg = {name: cur_cfg.get(name) or {} for name in
                                 ("feishu", "dingtalk", "wecom", "bark", "webhook")}
                    for slot_info in slots:
                        slot_id = slot_info.get("slot")
                        if not slot_id or not slot_info.get("online"):
                            continue
                        board_resp = self.backend.execute_cmd(
                            "set_notify_config", params=board_cfg, slot=slot_id, timeout=6.0)
                        board_data = board_resp.get("data") or {}
                        board_sync[slot_id] = bool(
                            board_resp.get("ok") and board_resp.get("msg") == "NOTIFY_CONFIG_PERSISTED"
                            and isinstance(board_data, dict) and board_data.get("synced"))
                self._send_json_resp(200, {"ok": True, "saved": True, "active": active,
                                           "board_sync": board_sync,
                                           "board_sync_known": board_sync_known,
                                           "msg": "配置已保存并加载" if active else "配置已保存，后台加载未确认"})
            except Exception as e:
                self._send_json_resp(500, {"ok": False, "error": f"保存配置失败: {e}"})
            return

        # 9. 测试单渠道推送
        if path == "/api/config/notify/test":
            channel = body.get("channel")
            if channel not in ("feishu", "dingtalk", "wecom", "bark", "webhook"):
                self._send_json_resp(400, {"ok": False, "error": "缺少测试渠道或参数"})
                return
            reload_result = self.backend.execute_cmd("reload_notify_config", timeout=3.0)
            if not reload_result.get("ok"):
                self._send_json_resp(503, {"ok": False, "error": "后台配置尚未加载，未发送测试消息"})
                return
            if not os.path.isfile(GATEWAY_CONFIG_PATH):
                self._send_json_resp(400, {"ok": False, "error": "请先保存通知渠道配置"})
                return
            try:
                with open(GATEWAY_CONFIG_PATH, "r", encoding="utf-8") as f:
                    saved_cfg = json.load(f)
                if not isinstance(saved_cfg, dict):
                    raise ValueError("invalid config root")
            except (OSError, ValueError):
                self._send_json_resp(503, {"ok": False, "error": "已保存配置无法读取，未发送测试消息"})
                return
            channel_cfg = saved_cfg.get(channel) or {}
            if not channel_cfg.get("enable") or not channel_cfg.get("url"):
                self._send_json_resp(400, {"ok": False, "error": "请先保存并启用该通知渠道"})
                return

            target_slot = body.get("slot")
            dev_desc = None
            if target_slot and hasattr(self.backend, "hub") and self.backend.hub:
                session = self.backend.hub.session_pool.get_session(target_slot)
                if session:
                    m = session.meta.get("model") or "Air780"
                    im = session.meta.get("imei") or ""
                    dev_desc = f"[{session.slot_id.upper()}] {m} · IMEI: {im}" if im else f"[{session.slot_id.upper()}] {m}"
            res = test_channel_push(channel, channel_cfg, device_desc=dev_desc)
            self._send_json_resp(200, res)
            return

        # 10. 系统偏好增量更新 (Patch Update，防重新洗牌) (AIR-57 / AIR-59 / AIR-62)
        if path == "/api/system/settings":
            BOOL_SYSTEM_KEYS = {
                "autostart", "auto_copy_otp", 
                "desktop_notification", "privacy_mode", "play_sound",
                "store_on_board", "daily_reboot"
            }
            if "autostart" in body:
                set_autostart_status(bool(body["autostart"]))

            cur_cfg = {}
            if os.path.exists(GATEWAY_CONFIG_PATH):
                try:
                    with open(GATEWAY_CONFIG_PATH, "r", encoding="utf-8") as f:
                        cur_cfg = json.load(f)
                    if not isinstance(cur_cfg, dict):
                        cur_cfg = {}
                except Exception:
                    cur_cfg = {}

            sec_system = cur_cfg.setdefault("system", {})
            for k in BOOL_SYSTEM_KEYS:
                if k in body:
                    sec_system[k] = bool(body[k])

            # 局域网访问权限控制 (AIR-62：权限安全红线，仅限本机客户端修改，外部非本机请求忽略篡改)
            client_ip = self.client_address[0] if (self.client_address and len(self.client_address) > 0) else None
            is_client_local = self._is_loopback_client(client_ip)
            if "lan_access" in body and is_client_local:
                new_lan_val = bool(body["lan_access"])
                sec_system["lan_access"] = new_lan_val
                # 同步更新底层 HTTP 服务器内存原子缓存
                self.server.lan_access_enabled = new_lan_val
                _log(f"局域网跨设备访问开关已由本机更新为: {'已启用' if new_lan_val else '已关闭'}")
            elif "lan_access" in body and not is_client_local:
                _log(f"外部客户端 {client_ip} 试图更改局域网访问权限，已被安全防护策略忽略")

            # 整型整点安全门禁与范围限制 (0~23)
            if "daily_reboot_hour" in body:
                try:
                    h = int(body["daily_reboot_hour"])
                    sec_system["daily_reboot_hour"] = h if 0 <= h <= 23 else 4
                except (ValueError, TypeError):
                    sec_system["daily_reboot_hour"] = 4

            try:
                next_path = GATEWAY_CONFIG_PATH + ".next"
                with open(next_path, "w", encoding="utf-8") as f:
                    json.dump(cur_cfg, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(next_path, GATEWAY_CONFIG_PATH)

                self.backend.execute_cmd("reload_notify_config", timeout=2.0)

                # 向在线板卡物理同步定时重启策略 (关为 -1，开为 0~23)
                target_reboot_hour = int(sec_system.get("daily_reboot_hour", 4)) if sec_system.get("daily_reboot", False) else -1
                slots_resp = self.backend.execute_cmd("get_slots", timeout=2.0)
                if slots_resp and isinstance(slots_resp, dict) and slots_resp.get("ok"):
                    slots = (slots_resp.get("data") or {}).get("slots") or []
                    for s in slots:
                        sid = s.get("slot")
                        if sid and s.get("online"):
                            try:
                                self.backend.execute_cmd("set_reboot_policy", params={"hour": target_reboot_hour}, slot=sid, timeout=3.0)
                            except Exception:
                                pass

                self._send_json_resp(200, {"ok": True, "msg": "系统偏好已保存并同步", "data": sec_system})
            except Exception as e:
                self._send_json_resp(500, {"ok": False, "error": f"保存配置失败: {e}"})
            return

        # 11. 目录直达打开安全门禁 (AIR-57)
        if path == "/api/system/open_folder":
            target = str(body.get("target") or "").strip()
            ok, msg = handle_open_folder(target)
            if ok:
                self._send_json_resp(200, {"ok": True, "msg": msg})
            else:
                self._send_json_resp(400, {"ok": False, "error": msg})
            return

        # 12. 运行日志清空 (AIR-57)
        if path == "/api/system/clear_logs":
            if clear_runtime_log():
                self._send_json_resp(200, {"ok": True, "log_size_human": "0 KB", "log_size_bytes": 0})
            else:
                self._send_json_resp(500, {"ok": False, "error": "清理日志失败"})
            return

        self._send_json_resp(404, {"ok": False, "error": "接口不存在"})

    def handle_sse_stream(self):
        """处理 SSE 持续事件推送流 (含局域网开关关闭瞬间的主动热逐出看门狗)"""
        client_ip = self.client_address[0] if (self.client_address and len(self.client_address) > 0) else None
        is_local = self._is_loopback_client(client_ip)

        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = self.backend.register_sse_listener()
        if hasattr(self.backend, "touch_activity"):
            self.backend.touch_activity(active_sse_count=len(self.backend.sse_listeners), force=True)
        _log(f"前端已建立 SSE 事件流连接 (当前队列数: {len(self.backend.sse_listeners)})")

        # 初始向刚连接的前端发送当前卡槽列表
        init_slots_event = {
            "event": "cluster_update",
            "data": {
                "slots": self.backend.slots,
                "active_slot": self.backend.active_slot
            }
        }
        init_payload = f"event: cluster_update\ndata: {json.dumps(init_slots_event['data'], ensure_ascii=False)}\n\n".encode("utf-8")
        try:
            self.wfile.write(init_payload)
            self.wfile.flush()
        except Exception:
            self.backend.unregister_sse_listener(q)
            if hasattr(self.backend, "touch_activity"):
                self.backend.touch_activity(active_sse_count=len(self.backend.sse_listeners), force=True)
            return

        try:
            last_periodic_touch = time.time()
            while True:
                now = time.time()
                if now - last_periodic_touch >= 15.0:
                    last_periodic_touch = now
                    if hasattr(self.backend, "touch_activity"):
                        self.backend.touch_activity(active_sse_count=len(self.backend.sse_listeners), force=True)

                # 存量连接安全看门狗：非本机客户端在局域网开关关闭时立即主动掐断断连
                if not is_local:
                    lan_enabled = getattr(self.server, "lan_access_enabled", False)
                    if not lan_enabled:
                        _log(f"局域网访问已在控制台关闭，主动掐断外部客户端 {client_ip} 的 SSE 实时流连接")
                        try:
                            self.wfile.write(b"event: close\ndata: {\"reason\":\"lan_access_disabled\"}\n\n")
                            self.wfile.flush()
                        except Exception:
                            pass
                        break

                try:
                    item = q.get(timeout=2.0)
                    # 写入真实 payload 之前再次核验局域网权限，防止正在排队的短信泄露
                    if not is_local and not getattr(self.server, "lan_access_enabled", False):
                        _log(f"局域网访问已关闭，阻断向外部客户端 {client_ip} 发送待推送事件")
                        break
                    evt_name = item.get("event", "message")
                    evt_data = json.dumps(item.get("data", {}), ensure_ascii=False)
                    payload = f"event: {evt_name}\ndata: {evt_data}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                except queue.Empty:
                    # 发送保活心跳包
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            _log(f"SSE 推送异常: {e}")
        finally:
            self.backend.unregister_sse_listener(q)
            if hasattr(self.backend, "touch_activity"):
                self.backend.touch_activity(active_sse_count=len(self.backend.sse_listeners), force=True)
            _log(f"前端已断开 SSE 事件流连接 ({client_ip})")


# =========================================================================
# WebServer 容器封装
# =========================================================================

class WebServer:
    def __init__(self, host: str = DEFAULT_WEB_HOST, port: int = DEFAULT_WEB_PORT, hub_host: str = DEFAULT_HUB_HOST, hub_port: int = DEFAULT_HUB_PORT):
        self.host = host
        self.port = port
        self.backend = HubBackendClient(host=hub_host, port=hub_port)
        self.httpd = None

        # 从磁盘配置文件加载局域网访问初始开关（出厂默认为 false，安全隔离）
        self.lan_access_enabled = False
        if os.path.exists(GATEWAY_CONFIG_PATH):
            try:
                with open(GATEWAY_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if isinstance(cfg, dict):
                    self.lan_access_enabled = bool(cfg.get("system", {}).get("lan_access", False))
            except Exception:
                self.lan_access_enabled = False

    def start(self):
        self.backend.start()
        self.httpd = ThreadedHTTPServer((self.host, self.port), GatewayWebHandler)
        self.httpd.backend = self.backend
        # 将布尔缓存原子挂载到底层 HTTP 实例，供 GatewayWebHandler 毫秒级免 I/O 检查
        self.httpd.lan_access_enabled = self.lan_access_enabled
        _log(f"Web 控制台已启动，访问地址: http://127.0.0.1:{self.port} (局域网跨设备访问: {'已启用' if self.lan_access_enabled else '已关闭·仅本机可用'})")
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            _log("正在关闭 Web 控制台...")
        finally:
            if self.httpd:
                self.httpd.shutdown()
            self.backend.stop()
            _log("Web 控制台已安全关闭")

    def stop(self):
        """外部受控停止 Web 监听与后端 TCP 客户端"""
        try:
            if self.httpd:
                self.httpd.shutdown()
        except Exception:
            pass
        try:
            if self.backend:
                self.backend.stop()
        except Exception:
            pass
        _log("Web 控制台已安全关闭")

def main():
    parser = argparse.ArgumentParser(description="Air780 Series Smart Cellular Gateway Web Console")
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help=f"HTTP 监听端口 (默认 {DEFAULT_WEB_PORT})")
    parser.add_argument("--host", type=str, default=DEFAULT_WEB_HOST, help=f"HTTP 监听地址 (默认 {DEFAULT_WEB_HOST})")
    parser.add_argument("--hub-host", type=str, default=DEFAULT_HUB_HOST, help="底层 Hub 主机地址")
    parser.add_argument("--hub-port", type=int, default=DEFAULT_HUB_PORT, help="底层 Hub 监听端口")
    args = parser.parse_args()

    server = WebServer(host=args.host, port=args.port, hub_host=args.hub_host, hub_port=args.hub_port)
    server.start()

if __name__ == "__main__":
    main()
