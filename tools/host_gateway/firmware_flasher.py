#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780 系列智能通信网关 - 上位机多芯片通用固件线刷引擎 (Firmware Flasher Engine)
支持：
1. 全新裸板 Full Flash 模式（刷入官方 LuatOS 底层内核 + 基带 + 业务脚本，自动识别或指定芯片架构）；
2. 在线模组 Script Flash 模式（极速 1~2 秒仅重刷应用业务 script.bin）；
3. 芯片架构全支持：EC718PV（Air780EPV/Air780EP）、EC718PM（Air780EPM）与 EC618（Air780E/Air780EC/Air780EG/Air700E）；
4. 资源完全自包含（选项 A）：优先使用内置固化的 tools/flasher 资产，脱离 Luatools 临时目录与外部 7z 依赖；
5. 工业级安全参数：彻底剔除 format.json 全片抹除，EC618 引导地址锁定 0x4000，完好保全出厂 NV 与射频校准表。
"""

import os
import sys
import time
import shutil
import tempfile
import subprocess
import copy
import json
import hashlib
import configparser
from typing import Optional, Callable, Dict, Any, List
import serial
import serial.tools.list_ports

class _SafeStream:
    """包装标准输出流，防止在 Windows 无控制台进程中抛出 [Errno 22] Invalid argument"""
    def __init__(self, target):
        self.target = target
    def write(self, s):
        try:
            if self.target and hasattr(self.target, "write"):
                self.target.write(s)
        except Exception:
            pass
    def flush(self):
        try:
            if self.target and hasattr(self.target, "flush"):
                self.target.flush()
        except Exception:
            pass
    def reconfigure(self, **kwargs):
        try:
            if self.target and hasattr(self.target, "reconfigure"):
                self.target.reconfigure(**kwargs)
        except Exception:
            pass
    def isatty(self):
        try:
            return self.target.isatty() if self.target and hasattr(self.target, "isatty") else False
        except Exception:
            return False
    def __getattr__(self, name):
        if self.target and hasattr(self.target, name):
            return getattr(self.target, name)
        raise AttributeError(f"'_SafeStream' object has no attribute '{name}'")

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 全局管道保护
sys.stderr = _SafeStream(sys.stderr)
sys.stdout = _SafeStream(sys.stdout)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
def _find_project_root():
    p = os.path.dirname(os.path.abspath(__file__))
    while p and os.path.dirname(p) != p:
        if os.path.exists(os.path.join(p, "board.md")):
            return p
        p = os.path.dirname(p)
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

PROJECT_ROOT = _find_project_root()

def get_bundle_resource_dir(sub_name: str, fallback_path: str) -> str:
    """支持 PyInstaller 打包资源与开发源码双路径解析"""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        bundled = os.path.join(meipass, sub_name)
        if os.path.exists(bundled):
            return bundled
    return fallback_path

# 固化的独立 flasher 根目录
FLASHER_DIR = get_bundle_resource_dir("flasher", os.path.join(PROJECT_ROOT, "tools", "flasher"))
FLASHER_BIN_DIR = os.path.join(FLASHER_DIR, "bin")
FLASHER_TARGETS_DIR = os.path.join(FLASHER_DIR, "targets")

FLASHTOOL_CLI = os.path.join(FLASHER_BIN_DIR, "FlashToolCLI.exe")

# 向下兼容旧测试套件属性
EC_DOWNLOAD_DIR = os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "ec_download") if os.path.exists(os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "ec_download")) else FLASHER_BIN_DIR
BASE_CFG_PATH = os.path.join(FLASHER_TARGETS_DIR, "ec618", "config_ec618_usb.ini")

# 备用 Luatools 临时目录（若独立资产缺失时向下兼容）
FALLBACK_TEMP_JCT = os.path.join(tempfile.gettempdir(), "luatools_jct", "p21c43db6b88b")
FALLBACK_EC_DIR = os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "ec_download")

if not os.path.exists(FLASHTOOL_CLI):
    if os.path.exists(os.path.join(FALLBACK_TEMP_JCT, "FlashToolCLI.exe")):
        FLASHER_BIN_DIR = FALLBACK_TEMP_JCT
        FLASHTOOL_CLI = os.path.join(FLASHER_BIN_DIR, "FlashToolCLI.exe")
    elif os.path.exists(os.path.join(FALLBACK_EC_DIR, "FlashToolCLI.exe")):
        FLASHER_BIN_DIR = FALLBACK_EC_DIR
        FLASHTOOL_CLI = os.path.join(FLASHER_BIN_DIR, "FlashToolCLI.exe")

BOOT_VID = 0x17D1
BOOT_PID = 0x0001

# 芯片平台配置矩阵 (溯源自官方 .soc 与原厂 baseini)
CHIP_CONFIGS = {
    "ec718pv": {
        "name": "EC718PV",
        "models": ["Air780EPV", "Air780EP"],
        "burnaddr_bootloader": "0x3000",
        "burnaddr_system": "0x7c000",
        "burnaddr_cp": "0x18000",
        "burnaddr_script": "0x324000",
        "script_magic": 0xeac37218,
        "script_base_addr": 0x00324000,
        "has_full_flash": True,
        "rom_version": "0000000103040000",
        "rom_version_append": "0000000203000000;0000000303000000;0000000103030000;0000000203030000",
        "full_imglist": ["bootloader", "system", "cp_system", "flexfile2"],
        "target_dir_name": "ec718pv"
    },
    "ec718pm": {
        "name": "EC718PM",
        "models": ["Air780EPM"],
        "burnaddr_bootloader": "0x3000",
        "burnaddr_system": "0x82000",
        "burnaddr_cp": "0x1e000",
        "burnaddr_script": "0x279000",
        "script_magic": 0xeac37218,
        "script_base_addr": 0x00279000,
        "has_full_flash": True,
        "rom_version": "0000000103030000",
        "rom_version_append": "0000000203030000;0000000303030000",
        "full_imglist": ["bootloader", "system", "cp_system", "flexfile2"],
        "target_dir_name": "ec718pm"
    },
    "ec618": {
        "name": "EC618",
        "models": ["Air780E", "Air780EC", "Air780EG", "Air700E"],
        "burnaddr_bootloader": "0x4000",  # 严格对齐原厂 baseini:24，禁止使用 0x0 导致抹除 Flash 引导头
        "burnaddr_system": "0x24000",
        "burnaddr_cp": None,             # EC618 平台完全省略 burnaddr 以使用 cp_flash 缺省，严禁输出空串或 0x0
        "burnaddr_script": "0x24D000",
        "script_magic": 0xeaf18c16,
        "script_base_addr": 0x0024D000,
        "has_full_flash": True,
        "rom_version": "0000000302000000",
        "rom_version_append": "0000000102000000;0000000201000000;0000000302000000;0000000202000000",
        "full_imglist": ["bootloader", "system", "cp_system", "flexfile2"],
        "target_dir_name": "ec618"
    }
}

MODEL_TO_CHIP: Dict[str, str] = {
    # 完整商用产品型号
    "AIR780EPV": "ec718pv",
    "AIR780EP": "ec718pv",
    "AIR780EPM": "ec718pm",
    "AIR780E": "ec618",
    "AIR780EC": "ec618",
    "AIR780EG": "ec618",
    "AIR700E": "ec618",
    # 纯芯片型号映射（防止硬件仅上报芯片名称）
    "EC718PV": "ec718pv",
    "EC718PM": "ec718pm",
    "EC718P": "ec718pv",
    "EC718": "ec718pv",
    "EC618": "ec618",
}
VALID_CHIPS = {"ec718pv", "ec718pm", "ec618"}

def log(msg: str):
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{now}] [Flasher] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass

def normalize_chip_type(hardware_model: Optional[str], chip_hint: Optional[str] = None) -> str:
    """根据硬件型号与芯片提示归一化为标准的芯片类型 ('ec718pv', 'ec718pm' 或 'ec618')"""
    if chip_hint:
        c = str(chip_hint).strip().lower()
        if c in VALID_CHIPS:
            return c
        if "718pm" in c or "epm" in c:
            return "ec718pm"
        if "718" in c:
            return "ec718pv"
        if "618" in c:
            return "ec618"

    if not hardware_model or not isinstance(hardware_model, str) or not hardware_model.strip():
        return "ec718pv"

    # 清洗厂商前缀、去空格并大写归一化
    raw_model = hardware_model.replace("合宙", "").strip().upper()
    if raw_model in MODEL_TO_CHIP:
        return MODEL_TO_CHIP[raw_model]

    # 按长度降序遍历，确保较长的模式 (如 AIR780EPM, AIR780EPV) 优先于前缀 (如 AIR780EP, AIR780E) 命中
    for k in sorted(MODEL_TO_CHIP.keys(), key=len, reverse=True):
        if k in raw_model:
            return MODEL_TO_CHIP[k]

    return "ec718pv"

def _clean_usb_location(loc: Optional[str]) -> Optional[str]:
    """剥除 USB 端口位置中的接口后缀（冒号及之后的部分）"""
    if not loc or not isinstance(loc, str):
        return None
    s = loc.strip()
    if not s:
        return None
    if ":" in s:
        s = s.split(":", 1)[0].strip()
    return s if s else None

def _match_usb_binding(port_info: Any, identity: Dict[str, Any]) -> bool:
    """复用 USB 绑定匹配：位置去除接口后缀后完整相等，serial 完整相等"""
    bound_loc_raw = identity.get("usb_location")
    bound_serial_raw = identity.get("usb_serial")
    bound_loc = _clean_usb_location(bound_loc_raw) if isinstance(bound_loc_raw, str) else None
    bound_serial = bound_serial_raw.strip() if isinstance(bound_serial_raw, str) and bound_serial_raw.strip() else None

    if not bound_loc and not bound_serial:
        return False

    cand_loc = _clean_usb_location(getattr(port_info, "location", None))
    cand_serial_raw = getattr(port_info, "serial_number", None)
    cand_serial = cand_serial_raw.strip() if isinstance(cand_serial_raw, str) and cand_serial_raw.strip() else None

    if bound_loc:
        if cand_loc != bound_loc:
            return False
        if bound_serial and cand_serial:
            if cand_serial != bound_serial:
                return False
        return True
    else:
        if not cand_serial or cand_serial != bound_serial:
            return False
        return True

def find_bootloader_port(identity: Optional[Dict[str, Any]] = None, target_loc_prefix: Optional[str] = None) -> Optional[str]:
    """探测 ROM Bootloader 烧录端口（移芯全系平台 17D1:0001）
    支持基于已核物理身份字典匹配，或通过 USB Hub 拓扑前缀匹配，若仅有 1 个 Boot 端口则安全返回。
    """
    ports = list(serial.tools.list_ports.comports())
    boot_ports = []
    for p in ports:
        hwid = (p.hwid or "").upper()
        vid = getattr(p, "vid", None)
        pid = getattr(p, "pid", None)
        is_boot = (vid == BOOT_VID and pid == BOOT_PID) or "17D1:0001" in hwid or ("17D1" in (hex(vid or 0).upper()) and "0001" in (hex(pid or 0).upper()))
        if is_boot:
            boot_ports.append(p)

    if not boot_ports:
        return None

    # 1. 若传入了 identity 字典，优先通过物理绑定过滤
    if isinstance(identity, dict):
        target_port = identity.get("port")
        for p in boot_ports:
            if target_port and p.device == target_port:
                return p.device
            if _match_usb_binding(p, identity):
                return p.device

    # 2. 若传入了 target_loc_prefix
    if target_loc_prefix:
        for p in boot_ports:
            loc = getattr(p, "location", "") or ""
            if loc.startswith(target_loc_prefix):
                return p.device

    # 3. 兜底：若系统当前恰好只插入了 1 个 Bootloader 设备，直接返回
    if len(boot_ports) == 1:
        return boot_ports[0].device

    return None

def trigger_soft_reboot_to_boot(current_port: Optional[str] = None, identity: Optional[Dict[str, Any]] = None) -> bool:
    """
    通过底层控制端口下发 AT+ECRST=delay,799 / rtos.reboot() 与 ~SYNC~
    安全引导指定模组切入 ROM Bootloader 烧录模式
    """
    candidate_ports = []
    if current_port:
        candidate_ports.append(current_port)

    if isinstance(identity, dict):
        cp = identity.get("control_port") or identity.get("port")
        if cp and cp not in candidate_ports:
            candidate_ports.append(cp)

    target_loc_prefix = None
    ports = list(serial.tools.list_ports.comports())
    for target in list(candidate_ports):
        for p in ports:
            if p.device == target:
                loc = getattr(p, "location", "") or ""
                if ":" in loc:
                    target_loc_prefix = loc.split(":")[0]
                elif loc:
                    target_loc_prefix = loc
                break

    for p in ports:
        loc = getattr(p, "location", "") or ""
        dev = p.device
        hwid = (p.hwid or "").upper()
        if dev in candidate_ports:
            continue
        if target_loc_prefix and loc.startswith(target_loc_prefix):
            candidate_ports.append(dev)
        elif "19D1:0001" in hwid and (":X.2" in loc.upper() or loc.endswith("x.2")):
            candidate_ports.append(dev)

    for cp in candidate_ports:
        try:
            log(f"向端口 {cp} 下发复位指令引导切入 Bootloader...")
            with serial.Serial(cp, baudrate=115200, timeout=0.5) as ser:
                ser.dtr = True
                ser.rts = True
                # 1. 兼容网关运行态 JSON 命令
                ser.write(b'{"type":"cmd","cmd":"reboot","id":"flasher_rst","data":{"reason":"flasher_reboot","delay_ms":500}}\n')
                ser.flush()
                time.sleep(0.05)
                # 2. 兼容标准 AT 固件复位指令
                ser.write(b"AT+ECRST=delay,799\r\n")
                ser.flush()
                time.sleep(0.05)
                # 3. 兼容标准 LuatOS REPL 控制台与同步字符
                ser.write(b"rtos.reboot()\r\n")
                ser.flush()
                ser.write(b"~" + bytes([0, 2]) + b"~")
                ser.flush()
        except Exception:
            pass

    return True

def extract_package_images(work_dir: str, chip_type: str) -> None:
    """
    在 burnbatch 烧写前，将 luatos.binpkg 离线解压提取至沙箱 pkgimg_gen 目录
    EC718 (PV/PM) 与 EC618 调用匹配的 fcelf 工具，解压出 ap_bootloader.bin, ap.bin 与 cp-demo-flash.bin。
    """
    pkg_gen_dir = os.path.join(work_dir, "pkgimg_gen")
    os.makedirs(pkg_gen_dir, exist_ok=True)

    fcelf_exe = os.path.join(work_dir, "fcelf.exe")
    cmd = [fcelf_exe, "-E", "-input", "luatos.binpkg"]
    try:
        p = subprocess.run(cmd, cwd=work_dir, capture_output=True, encoding="utf-8", errors="replace", timeout=15.0)
        if p.returncode != 0:
            raise RuntimeError(f"{chip_type.upper()} 固件解包失败: {p.stderr or p.stdout}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{chip_type.upper()} 固件解包超时 (超过 15 秒)")

    for fname in ["ap_bootloader.bin", "ap.bin", "cp-demo-flash.bin"]:
        src = os.path.join(work_dir, fname)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(pkg_gen_dir, fname))

    # 为 ap.bin 建立别名 ap_demo-flash.bin（兼容 EC618 原厂 baseini）
    ap_path = os.path.join(pkg_gen_dir, "ap.bin")
    if os.path.exists(ap_path):
        shutil.copy2(ap_path, os.path.join(pkg_gen_dir, "ap_demo-flash.bin"))

    # 显式校验三大分卷必须存在且非空
    for req in ["ap_bootloader.bin", "ap.bin", "cp-demo-flash.bin"]:
        req_path = os.path.join(pkg_gen_dir, req)
        if not os.path.exists(req_path) or os.path.getsize(req_path) == 0:
            raise RuntimeError(f"{chip_type.upper()} 固件分卷提取不完整，缺少或空文件: {req}")

def assemble_flasher_ini(
    work_dir: str,
    chip_type: str,
    port: str,
    mode: str = "script"
) -> str:
    """
    动态装配移芯 FlashToolCLI 所需的 config_pkg_product_usb.ini 配置文件
    所有镜像与配置均置于纯 ASCII 的 work_dir 内部，彻底规避 FlashToolCLI 路径含中文异常。
    【工业级去毒化标准】：
    - 坚决剔除 format_path 与 erallum 擦除逻辑，彻底保护芯片 Flash Header 引导头与出厂射频校准表；
    - EC618 Bootloader 写入基地址严格锁定 0x4000；
    - EC618 cp_system 完全省略 burnaddr 行，使用系统缺省；
    - 分卷列表锁定为: bootloader, system, cp_system, flexfile2。
    """
    chip_cfg = CHIP_CONFIGS.get(chip_type, CHIP_CONFIGS["ec718pv"])
    target_dir = os.path.join(FLASHER_TARGETS_DIR, chip_cfg["target_dir_name"])

    # 1. 拷贝必要底包资产至 work_dir
    src_pkg = os.path.join(target_dir, "luatos.binpkg")
    dst_pkg = os.path.join(work_dir, "luatos.binpkg")
    if os.path.exists(src_pkg) and not os.path.exists(dst_pkg):
        shutil.copy2(src_pkg, dst_pkg)

    src_agentboot = os.path.join(target_dir, "agentboot_usb.bin")
    if chip_type == "ec618":
        nonbl2_path = os.path.join(FLASHER_BIN_DIR, "product_sets", "ec618_products", "common_data", "agentboot_usb", "agentboot.bin_NONBL2")
        if os.path.exists(nonbl2_path):
            src_agentboot = nonbl2_path
    elif not os.path.exists(src_agentboot):
        src_agentboot = os.path.join(FLASHER_BIN_DIR, "agentboot_usb.bin")
    dst_agentboot = os.path.join(work_dir, "agentboot_usb.bin")
    if os.path.exists(src_agentboot) and not os.path.exists(dst_agentboot):
        shutil.copy2(src_agentboot, dst_agentboot)

    # 芯片架构物理隔离：EC618 必须用 fcelf_ec618.exe，EC718 必须用 fcelf.exe
    if chip_type == "ec618":
        src_fcelf = os.path.join(FLASHER_BIN_DIR, "fcelf_ec618.exe")
    else:
        src_fcelf = os.path.join(FLASHER_BIN_DIR, "fcelf.exe")
    dst_fcelf = os.path.join(work_dir, "fcelf.exe")
    if os.path.exists(src_fcelf) and not os.path.exists(dst_fcelf):
        shutil.copy2(src_fcelf, dst_fcelf)

    ini_content = [
        "[config]",
        f"line_0_com = {port}",
        "agbaud = 921600",
        "filter_embedusb = 0",
        "filter_externcom = 1",
        "",
        "[package_info]",
        "pkgflag = 1",
        "pkg_extract_exe = .\\fcelf.exe",
        "arg_pkg_path_val = .\\luatos.binpkg",
        "pkg_bins_regen_targetdir = .\\pkgimg_gen",
        "",
        "[agentboot]",
        "tool_basedir = 0",
        "agpath = .\\agentboot_usb.bin",
        "",
        "[control]",
        "prempt_detect_time = 6",
        "msg_waittime = 2",
        "max_preamble_cnt = 8",
        "lpc_recover_en = 0",
        "pullup_qspi = 1",
        f"rom_version = {chip_cfg['rom_version']}",
        f"rom_version_append_list = {chip_cfg['rom_version_append']}",
        ""
    ]

    skip_val = 1 if mode == "script" else 0

    if mode == "full":
        # 1. bootloader 分区
        ini_content.extend([
            "[bootloader]",
            "blpath = .\\pkgimg_gen\\ap_bootloader.bin",
            f"blloadskip = {skip_val}",
            f"burnaddr = {chip_cfg['burnaddr_bootloader']}",
            ""
        ])

        # 2. system 内核分区
        sys_file = "ap_demo-flash.bin" if chip_type == "ec618" else "ap.bin"
        ini_content.extend([
            "[system]",
            f"syspath = .\\pkgimg_gen\\{sys_file}",
            f"sysloadskip = {skip_val}",
            f"burnaddr = {chip_cfg['burnaddr_system']}",
            ""
        ])

        # 3. cp_system 通信核分区 (EC618 完全省略 burnaddr 以使用缺省)
        cp_burn_line = f"burnaddr = {chip_cfg['burnaddr_cp']}\n" if chip_cfg["burnaddr_cp"] else ""
        ini_content.extend([
            "[cp_system]",
            "cp_syspath = .\\pkgimg_gen\\cp-demo-flash.bin",
            f"cp_sysloadskip = {skip_val}",
        ])
        if cp_burn_line:
            ini_content.append(cp_burn_line.strip())
        ini_content.append("")

    # 4. flexfile2 脚本业务分区
    ini_content.extend([
        "[flexfile2]",
        "filepath = .\\script.bin",
        f"burnaddr = {chip_cfg['burnaddr_script']}",
        "storage_type = ap_flash",
        ""
    ])

    ini_path = os.path.join(work_dir, "config_pkg_product_usb.ini")
    with open(ini_path, "w", encoding="utf-8") as f:
        f.write("\n".join(ini_content))
    return ini_path

def flash_hardware_cli(
    script_bin_path: Optional[str] = None,
    current_vuart_port: Optional[str] = None,
    hardware_model: Optional[str] = None,
    chip_type: Optional[str] = None,
    mode: str = "script",
    progress_cb: Optional[Callable[[int, str], None]] = None,
    watchdog_timeout: int = 40,
    identity: Optional[Dict[str, Any]] = None,
    maintenance: Optional[Dict[str, Any]] = None,
    **extra_kwargs
) -> Dict[str, Any]:
    """
    纯命令行受控烧录固件至硬件模组（通用多芯片架构驱动引擎）
    支持：
    - 'script'（应用脚本极速更新，1~2s）；
    - 'full'（全新裸板全量灌入内核+通信基带+脚本，8~12s，彻底杜绝变砖与擦除出厂校准）。
    """
    if sys.platform != "win32":
        return {"ok": False, "msg": "移芯 FlashToolCLI 烧录仅支持 Windows 环境"}

    if not os.path.exists(FLASHTOOL_CLI):
        return {"ok": False, "msg": f"未找到烧录工具 CLI 文件: {FLASHTOOL_CLI}"}

    chip = normalize_chip_type(hardware_model, chip_type)
    chip_cfg = CHIP_CONFIGS[chip]

    def emit(pct: int, txt: str, stage: str = "flashing"):
        capped = min(pct, 100)
        log(f"[{capped}%] [{chip_cfg['name']}] {txt}")
        if progress_cb:
            try:
                progress_cb(capped, txt, stage)
            except TypeError:
                try:
                    progress_cb(capped, txt)
                except Exception:
                    pass
            except Exception:
                pass

    base_temp = tempfile.gettempdir()
    if any(ord(c) > 127 for c in base_temp):
        base_temp = os.path.join(os.environ.get("SystemDrive", "C:"), "Temp")
    work_dir = os.path.join(base_temp, f"air780_flasher_{int(time.time()*1000)}_{os.getpid()}")

    try:
        os.makedirs(work_dir, exist_ok=True)
        # 纯英文沙箱绝对物理隔离：将 FlashToolCLI.exe 也自包含至 work_dir 本地调用，彻底杜绝路径中文闪退
        local_flashtool = os.path.join(work_dir, "FlashToolCLI.exe")
        shutil.copy2(FLASHTOOL_CLI, local_flashtool)
        mode_str = "全量系统刷写 (Full Flash)" if mode == "full" else "应用脚本更新 (Script Flash)"
        emit(5, f"正在准备 {chip_cfg['name']} 固件资产与打包 script.bin...")
        target_bin = os.path.join(work_dir, "script.bin")

        if script_bin_path and os.path.exists(script_bin_path):
            shutil.copy2(script_bin_path, target_bin)
        else:
            try:
                if BASE_DIR not in sys.path:
                    sys.path.insert(0, BASE_DIR)
                import luadb_packer
                raw_luadb = luadb_packer.pack_luadb(
                    magic=chip_cfg.get("script_magic"),
                    base_addr=chip_cfg.get("script_base_addr")
                )
                with open(target_bin, "wb") as f:
                    f.write(raw_luadb)
                log(f"自动打包 {chip_cfg['name']} LuaDB 完成: {len(raw_luadb)} 字节")
            except Exception as e:
                log(f"LuaDB 固件打包失败: {e}")
                return {"ok": False, "msg": f"LuaDB 固件打包失败: {e}"}

        # 2. 引导模组切入 Bootloader 模式 (人机协同 60 秒高频守候态)
        target_port = current_vuart_port or (identity.get("port") if isinstance(identity, dict) else None)
        boot_port = find_bootloader_port(identity)
        if not boot_port and target_port:
            trigger_soft_reboot_to_boot(target_port, identity)

        start_wait = time.time()
        max_wait_seconds = 60.0
        last_hint_time = -5.0  # 立即在第 0 秒发出首条操作提醒广播

        while not boot_port and (time.time() - start_wait < max_wait_seconds):
            elapsed = time.time() - start_wait
            remain = int(max_wait_seconds - elapsed)
            if elapsed - last_hint_time >= 4.0:
                last_hint_time = elapsed
                emit(
                    15,
                    f"正在守候设备进入烧录模式 (剩余 {remain} 秒)... 请在模组上【按住 BOOT 键不放，点按一下 RST 键】(或拔插一次 USB)",
                    stage="waiting_boot"
                )
            time.sleep(0.05)
            boot_port = find_bootloader_port(identity)

        if not boot_port:
            return {
                "ok": False,
                "msg": "未能在 60 秒内检测到 BootROM 烧录端口 (VID 17D1:0001)，烧录已取消。请按住板载 S1(BOOT) 按键点按 RST 键后重试",
                "phase": "uncertain"
            }

        emit(30, f"🎯 瞬间捕获到烧录端口: {boot_port}！正在装配配置文件...", stage="flashing")
        time.sleep(0.3)

        # 3. 动态组装 config_pkg_product_usb.ini 并同步关键资源至沙箱
        assemble_flasher_ini(work_dir, chip, boot_port, mode)

        # 拷贝必要辅助工具至工作目录
        for aux_tool in ["soc_tools.exe", "PrMgrCfg.json", "logging.conf", "cfg.digest"]:
            aux_src = os.path.join(FLASHER_BIN_DIR, aux_tool)
            if os.path.exists(aux_src):
                shutil.copy2(aux_src, os.path.join(work_dir, aux_tool))

        ps_src = os.path.join(FLASHER_BIN_DIR, "product_sets")
        ps_dst = os.path.join(work_dir, "product_sets")
        if os.path.exists(ps_src) and not os.path.exists(ps_dst):
            shutil.copytree(ps_src, ps_dst)

        # 4. 前置镜像解包时序 (pkg2img / fcelf -E)
        if mode == "full":
            emit(40, "正在执行内核固件解包与镜像提取 (pkg2img)...")
            extract_package_images(work_dir, chip)

        # 5. 执行 probe (EC718 系列握手，EC618 由 burnbatch 内置驱动)
        if chip in ("ec718pv", "ec718pm"):
            emit(50, "正在与芯片 Bootloader 握手 (probe)...")
            cmd_probe = [
                local_flashtool,
                "--cfgfile", "config_pkg_product_usb.ini",
                "--port", boot_port,
                "probe"
            ]
            try:
                p_probe = subprocess.run(cmd_probe, cwd=work_dir, capture_output=True, text=True, timeout=12.0)
                if p_probe.returncode != 0:
                    return {"ok": False, "msg": f"芯片握手失败 (probe failed): {p_probe.stderr or p_probe.stdout}"}
            except subprocess.TimeoutExpired:
                return {"ok": False, "msg": f"芯片握手超时 (probe timed out after 12s on {boot_port})"}
        else:
            log("[EC618] 原厂流跳过独立 probe 指令，直接由 burnbatch 内部 burnag 驱动握手与固件写入")
            emit(50, "正在连接 EC618 Bootloader...")

        # 6. 执行 burnbatch (固件安全分卷写入)
        imglist = chip_cfg["full_imglist"] if mode == "full" else ["flexfile2"]
        emit(70, f"握手成功！正在执行{mode_str}...")

        cmd_burn = [
            local_flashtool,
            "--cfgfile", "config_pkg_product_usb.ini",
            "--port", boot_port,
        ]
        if chip in ("ec718pv", "ec718pm"):
            cmd_burn.extend(["--skipconnect", "1"])

        cmd_burn.extend(["burnbatch", "--imglist"] + imglist)

        try:
            p_burn = subprocess.run(cmd_burn, cwd=work_dir, capture_output=True, text=True, timeout=float(watchdog_timeout))
            if p_burn.returncode != 0:
                return {"ok": False, "msg": f"固件写入失败 (burnbatch failed): {p_burn.stderr or p_burn.stdout}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "msg": f"固件烧写超时 (burnbatch timed out after {watchdog_timeout}s on {boot_port})"}

        # 7. 执行 sysreset (安全软复位重启)
        emit(90, "固件写入完成，正在平滑复位重启模组...")
        cmd_reset = [
            local_flashtool,
            "--cfgfile", "config_pkg_product_usb.ini",
            "--port", boot_port,
            "--skipconnect", "1",
            "sysreset"
        ]
        try:
            subprocess.run(cmd_reset, cwd=work_dir, capture_output=True, text=True, timeout=5)
        except Exception:
            pass

        emit(100, f"模组已成功平滑重启！{chip_cfg['name']} 固件烧录圆满完成。")
        return {
            "ok": True,
            "msg": f"上位机线刷成功 ({chip_cfg['name']} · {mode_str})，模组已重启生效",
            "port": boot_port,
            "chip": chip,
            "mode": mode,
            "percent": 100
        }

    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass

if __name__ == "__main__":
    print("=" * 65)
    print("🚀 上位机多芯片通用固件线刷引擎 (FlashToolCLI) 自检")
    print("=" * 65)
    boot = find_bootloader_port()
    print(f"• 当前 Bootloader 端口: {boot or '未进入 (就绪待触发)'}")
    print(f"• FlashToolCLI 就绪: {os.path.exists(FLASHTOOL_CLI)}")
    print(f"• 独立资产根目录: {FLASHER_DIR}")
    print(f"• 支持芯片矩阵: {list(CHIP_CONFIGS.keys())}")
    for k, v in CHIP_CONFIGS.items():
        td = os.path.join(FLASHER_TARGETS_DIR, v["target_dir_name"])
        print(f"  - [{k}] {v['name']}: {os.path.exists(td)} ({td})")
