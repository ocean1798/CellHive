#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780 系列智能通信网关 - LuaDB 脚本打包器与静态语法校验器
负责将 deploy/smart-gateway-780epv/ 源码安全编译并组装为符合合宙与移芯底层规范的：
1. 标准 LuaDB 脚本镜像 (script.bin，用于底层 FlashTool 物理烧录于 0x00324000)
2. 标准 SOTA 容器升级包 (script_ota.bin，用于应用层串口/HTTP 注入流式热更)

具备三层安全防线：
- 静态语法预检防线：扫描所有 .lua 文件词法/语法，阻断坏包下发防止板端 Reboot Loop 死锁；
- 根头与尾部校验防线：24 字节 Tag 03 根头与 16 字节 .airm2m_all_crc#.bin MD5 尾部闭环；
- 容器结构防线：SOTA 封装校验 92 字节 CoreUpgrade 头部 (0xeac37218) 与 CRC32。
"""

import os
import sys
import re
import struct
import binascii
import hashlib
import tempfile
import subprocess
import json
from typing import Tuple, List, Dict, Optional, Any
from typing import Tuple, List, Dict, Optional

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))

def get_bundle_resource_dir(sub_name: str, fallback_path: str) -> str:
    """在 PyInstaller 打包环境下只能使用 _MEIPASS 相应路径，缺失不得回退开发目录；非 frozen 环境使用工程路径"""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if not meipass:
            raise RuntimeError("PyInstaller 打包环境 _MEIPASS 缺失或不可用")
        return os.path.join(meipass, sub_name)
    return fallback_path

DEFAULT_SCRIPTS_DIR = get_bundle_resource_dir("lua_scripts", os.path.join(PROJECT_ROOT, "deploy", "smart-gateway-780epv"))
LUAC_EXE = get_bundle_resource_dir(os.path.join("tools", "luac_536.exe"), os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "tools", "luac_536.exe"))
SOC_TOOLS_EXE = get_bundle_resource_dir(os.path.join("tools", "soc_tools.exe"), os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "tools", "soc_tools.exe"))
DUMMY_BIN = get_bundle_resource_dir("dummy.bin", os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "dummy.bin"))

CORE_SCRIPTS = [
    "main.lua",
    "config.lua",
    "model.lua",
    "led.lua",
    "serial_comm.lua",
    "sms_service.lua",
    "call_service.lua",
    "notify_service.lua",
    "storage_service.lua",
    "fota_service.lua",
    "reboot_service.lua"
]

FOTA_MAGIC = 0xeac37218
FLASH_SCRIPT_ADDR = 0x00324000

VERSION_MANIFEST_PATH = os.path.join(PROJECT_ROOT, "tools", "fota", "version.json")

def get_version_manifest() -> Dict[str, Any]:
    """从工程 tools/fota/version.json 读取版本事实源元数据"""
    if not os.path.exists(VERSION_MANIFEST_PATH):
        raise FileNotFoundError(f"版本清单文件不存在: {VERSION_MANIFEST_PATH}")
    try:
        with open(VERSION_MANIFEST_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except json.JSONDecodeError as e:
        raise ValueError(f"版本清单 JSON 解析失败: {e}")
    if not isinstance(data, dict):
        raise ValueError("版本清单格式错误: 根节点必须为字典")
    version = data.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"^[0-9]+\.[0-9]+\.[0-9]+$", version):
        raise ValueError(f"版本清单 version 无效: {version}")
    return data


def validate_release_target(target: Dict[str, Any]) -> Dict[str, Any]:
    """校验发布目标描述字典的完整性与格式规范"""
    req_fields = {"chip", "models", "core_versions", "script_offset", "script_capacity", "lua_version", "lua_bitw", "soc_sha256", "format_sha256"}
    if not isinstance(target, dict) or set(target.keys()) != req_fields:
        raise ValueError("target 必须为字典且字段必须齐全无多余")
    chip = target["chip"]
    if chip not in ("ec618", "ec718pv"):
        raise ValueError(f"chip 必须为 ec618 或 ec718pv: {chip}")
    clean_models, clean_cores = [], []
    for f_name, src_list, dst_list in (("models", target["models"], clean_models), ("core_versions", target["core_versions"], clean_cores)):
        if not isinstance(src_list, list) or not src_list:
            raise ValueError(f"{f_name} 必须为非空列表")
        for item in src_list:
            if not isinstance(item, str) or not item:
                raise ValueError(f"{f_name} 条目必须为非空字符串")
            if f_name == "core_versions" and item.lower() in ("unknown", "v0001"):
                raise ValueError(f"core_versions 禁止占位版本: {item}")
            if item not in dst_list:
                dst_list.append(item)
    offset, cap = target["script_offset"], target["script_capacity"]
    if type(offset) is not int or type(cap) is not int:
        raise ValueError("script_offset 与 script_capacity 必须为真int非bool")
    if offset <= 0 or cap <= 0 or (offset + cap) > (2 ** 32) or offset % 4096 != 0 or cap % 4096 != 0:
        raise ValueError("script_offset 或 capacity 范围越界或未4096对齐")
    lua_ver, lua_bitw = target["lua_version"], target["lua_bitw"]
    if lua_ver != "5.3" or type(lua_ver) is not str or type(lua_bitw) is not int or lua_bitw not in (32, 64):
        raise ValueError("lua_version 必须为'5.3'且 lua_bitw 必须为 32 或 64")
    soc_sha, fmt_sha = target["soc_sha256"], target["format_sha256"]
    for k, s in (("soc_sha256", soc_sha), ("format_sha256", fmt_sha)):
        if not isinstance(s, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", s):
            raise ValueError(f"{k} 必须为 64 位十六进制字符串")
    return {
        "chip": chip, "models": clean_models, "core_versions": clean_cores,
        "script_offset": offset, "script_capacity": cap,
        "lua_version": lua_ver, "lua_bitw": lua_bitw,
        "soc_sha256": soc_sha.lower(), "format_sha256": fmt_sha.lower(),
    }


def compute_build_identity(scripts_dir: str, version: str, target: Dict[str, Any]) -> Dict[str, Any]:
    """计算源码与构建环境的确定性身份标识哈希"""
    target = validate_release_target(target)
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError(f"version 必须为语义化版本号字符串: {version}")

    real_scripts_dir = os.path.realpath(scripts_dir)
    if not os.path.isdir(real_scripts_dir):
        raise ValueError(f"scripts_dir 不存在或不是目录: {scripts_dir}")

    source_inputs = {}
    for fn in sorted(CORE_SCRIPTS):
        fp = os.path.join(scripts_dir, fn)
        real_fp = os.path.realpath(fp)
        try:
            if os.path.commonpath([real_scripts_dir, real_fp]) != real_scripts_dir or real_fp == real_scripts_dir:
                raise ValueError(f"文件路径逃出 scripts_dir: {fn}")
        except ValueError:
            raise ValueError(f"文件路径逃出 scripts_dir: {fn}")

        if not os.path.isfile(real_fp):
            raise FileNotFoundError(f"核心脚本文件不存在: {fn}")
        if os.path.getsize(real_fp) == 0:
            raise ValueError(f"核心脚本文件为空: {fn}")

        with open(real_fp, "rb") as f:
            content = f.read()
        if len(content) == 0:
            raise ValueError(f"核心脚本文件为空: {fn}")
        source_inputs[fn] = hashlib.sha256(content).hexdigest()

    source_json = json.dumps(source_inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    source_sha256 = hashlib.sha256(source_json).hexdigest()

    tools = {}
    tool_items = [
        ("luac", LUAC_EXE),
        ("soc_tools", SOC_TOOLS_EXE),
        ("dummy", DUMMY_BIN),
    ]
    for key, path in tool_items:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"工具文件不存在: {path}")
        if key != "dummy" and os.path.getsize(path) == 0:
            raise ValueError(f"工具文件为空: {path}")
        with open(path, "rb") as f:
            tool_bytes = f.read()
        if key != "dummy" and len(tool_bytes) == 0:
            raise ValueError(f"工具文件为空: {path}")
        tools[key] = hashlib.sha256(tool_bytes).hexdigest()

    build_payload = {
        "version": version,
        "source_sha256": source_sha256,
        "tools": tools,
        "target": target,
    }
    build_json = json.dumps(build_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    build_id = hashlib.sha256(build_json).hexdigest()

    return {
        "source_sha256": source_sha256,
        "build_id": build_id,
        "source_inputs": source_inputs,
        "tools": tools,
    }

class LuaSyntaxError(Exception):
    """Lua 语法检查未通过异常"""
    pass

def _decode_process_output(res: subprocess.CompletedProcess) -> str:
    """将进程输出按 UTF-8 (backslashreplace) 显式解码为字符串并 strip，禁抛解码异常；空值返回空字符串"""
    raw = res.stderr or res.stdout
    if not raw:
        return ""
    try:
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="backslashreplace").strip()
        return str(raw).strip()
    except Exception:
        return ""

def validate_lua_syntax(scripts_dir: str = DEFAULT_SCRIPTS_DIR) -> Tuple[bool, List[str]]:
    """
    静态扫描脚本目录下的核心 Lua 文件，调用 luac -p 执行精确语法检查。
    禁止无工具时的正则/括号数兜底；缺工具直接返回 False 与明确原因。
    """
    if not os.path.exists(LUAC_EXE):
        return (False, [f"未找到 Lua 编译器 (luac): {LUAC_EXE}"])

    errors = []
    for fn in CORE_SCRIPTS:
        fp = os.path.join(scripts_dir, fn)
        if not os.path.exists(fp):
            errors.append(f"核心脚本缺失: {fn}")
            continue

        try:
            res = subprocess.run(
                [LUAC_EXE, "-p", fp],
                shell=False,
                capture_output=True,
                timeout=15
            )
            if res.returncode != 0:
                err_msg = _decode_process_output(res)
                errors.append(f"{fn} 语法错误: {err_msg}")
        except subprocess.TimeoutExpired:
            errors.append(f"{fn} 语法检查超时 (15秒)")
        except Exception as e:
            errors.append(f"{fn} 语法检查执行异常: {e}")

    return (len(errors) == 0, errors)

def strip_lua_code(code_str: str) -> str:
    """剔除多余行注释与多余空行以压缩源码体积，节约板端 RAM"""
    lines = []
    for line in code_str.splitlines():
        trimmed = line.strip()
        # 忽略纯单行注释行（保留非注释或内嵌代码）
        if trimmed.startswith("--") and not trimmed.startswith("--[["):
            continue
        if trimmed:
            lines.append(line)
    return "\n".join(lines)

def pack_luadb(
    scripts_dir: str = DEFAULT_SCRIPTS_DIR,
    target_version: Optional[str] = None,
    magic: Optional[int] = None,
    base_addr: Optional[int] = None,
    build_id: Optional[str] = None,
    target: Optional[Dict[str, Any]] = None,
    **kwargs
) -> bytes:
    """
    将 scripts_dir 下的核心脚本打包为标准的合宙 LuaDB 二进制镜像。
    包含 24 字节根头、文件条目及尾部 .airm2m_all_crc#.bin MD5 校验码。
    magic 与 base_addr 为底层线刷工具调用预留（标准的 LuaDB 具有固定 TLV 根头）。
    """
    if target is not None:
        target = validate_release_target(target)

    if not target_version:
        target_version = get_version_manifest()["version"]
    if not isinstance(target_version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", target_version):
        raise ValueError(f"target_version 必须为语义化三段数字版本号: {target_version}")

    if build_id is not None:
        if not isinstance(build_id, str) or not re.fullmatch(r"[0-9a-f]{64}", build_id):
            raise ValueError(f"build_id 必须为小写 64 位十六进制 SHA-256: {build_id}")

    if not os.path.exists(LUAC_EXE):
        raise FileNotFoundError(f"未找到 Lua 编译器 (luac): {LUAC_EXE}")

    valid, errors = validate_lua_syntax(scripts_dir)
    if not valid:
        raise LuaSyntaxError(f"打包前语法校验未通过:\n" + "\n".join(errors))

    file_entries: List[Tuple[str, bytes]] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for fn in CORE_SCRIPTS:
            src_fp = os.path.join(scripts_dir, fn)
            if not os.path.exists(src_fp):
                raise FileNotFoundError(f"核心脚本缺失: {src_fp}")
            with open(src_fp, "r", encoding="utf-8") as fp:
                src_code = fp.read()

            # 仅在 main.lua 编译副本中替换整行 VERSION 与可选 local BUILD_ID，其余文件不做替换
            if fn == "main.lua":
                src_code, n_ver = re.subn(
                    r'(?m)^VERSION\s*=\s*"[^"\r\n]*".*$',
                    f'VERSION = "{target_version}"',
                    src_code
                )
                if n_ver != 1:
                    raise ValueError(f"main.lua 中 VERSION 声明替换失败 (匹配次数: {n_ver} != 1)")
                if build_id is not None:
                    src_code, n_bid = re.subn(
                        r'(?m)^local\s+BUILD_ID\s*=\s*"[^"\r\n]*".*$',
                        f'local BUILD_ID = "{build_id}"',
                        src_code
                    )
                    if n_bid != 1:
                        raise ValueError(f"main.lua 中 local BUILD_ID 声明替换失败 (匹配次数: {n_bid} != 1)")

            out_lua_tmp = os.path.join(tmpdir, fn)
            with open(out_lua_tmp, "w", encoding="utf-8") as tfp:
                tfp.write(src_code)

            out_luac = os.path.join(tmpdir, fn.replace(".lua", ".luac"))
            cmd = [LUAC_EXE, "-s", "-o", out_luac, out_lua_tmp]
            try:
                res = subprocess.run(cmd, shell=False, capture_output=True, timeout=15)
                if res.returncode != 0 or not os.path.exists(out_luac):
                    err_msg = _decode_process_output(res)
                    raise RuntimeError(f"编译 {fn} 失败: {err_msg}")
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"编译 {fn} 超时 (15秒)")

            with open(out_luac, "rb") as c_fp:
                compiled_bytes = c_fp.read()
            if not compiled_bytes or len(compiled_bytes) < 33:
                raise ValueError(f"编译产物为空或长度过短: {fn} (长度: {len(compiled_bytes)} < 33)")
            if not compiled_bytes.startswith(b"\x1bLua\x53\x00\x19\x93\x0d\x0a\x1a\x0a"):
                raise ValueError(f"编译产物 Lua 头签名/版本/格式校验失败: {fn}")
            if target is not None:
                expected_width = target["lua_bitw"] // 8
                if compiled_bytes[15] != expected_width or compiled_bytes[16] != expected_width:
                    raise ValueError(
                        f"编译产物位宽与 target.lua_bitw 不一致: "
                        f"header[15]={compiled_bytes[15]}, header[16]={compiled_bytes[16]}, expected={expected_width} ({fn})"
                    )
            file_entries.append((fn.replace(".lua", ".luac"), compiled_bytes))
    buf = bytearray()

    # 1. 根头部 (24 字节)
    # Magic(6B): 01 04 5a a5 5a a5
    # Tag 02 (版本=2, len=2): 02 00
    # Tag 03 (头部长度=24, len=4): 18 00 00 00  (注意十六进制 0x18 = 24)
    # Tag 04 (总文件数=len+1, len=2)
    # Tag fe (累加校验和, len=2)
    root_hdr = bytearray(b"\x01\x04\x5a\xa5\x5a\xa5\x02\x02\x02\x00\x03\x04\x18\x00\x00\x00\x04\x02")
    file_count = len(file_entries) + 1  # 包含尾部校验文件
    root_hdr.extend(struct.pack("<H", file_count))
    root_hdr.extend(b"\xfe\x02")
    root_hdr.extend(struct.pack("<H", sum(root_hdr) & 0xFFFF))
    if len(root_hdr) != 24:
        raise ValueError(f"根头长度异常: {len(root_hdr)}")
    buf.extend(root_hdr)

    # 2. 依次写入各文件记录
    for name, content in file_entries:
        name_bytes = name.encode("ascii")
        f_hdr = bytearray(b"\x01\x04\x5a\xa5\x5a\xa5")
        f_hdr.append(2)
        f_hdr.append(len(name_bytes))
        f_hdr.extend(name_bytes)
        f_hdr.extend(b"\x03\x04")
        f_hdr.extend(struct.pack("<I", len(content)))
        f_hdr.extend(b"\xfe\x02")
        f_hdr.extend(struct.pack("<H", sum(f_hdr) & 0xFFFF))
        buf.extend(f_hdr)
        buf.extend(content)

    # 3. 写入尾部 .airm2m_all_crc#.bin MD5 签名文件 (16 字节)
    crc_name = b".airm2m_all_crc#.bin"
    crc_hdr = bytearray(b"\x01\x04\x5a\xa5\x5a\xa5")
    crc_hdr.append(2)
    crc_hdr.append(len(crc_name))
    crc_hdr.extend(crc_name)
    crc_hdr.extend(b"\x03\x04")
    crc_hdr.extend(struct.pack("<I", 16))
    crc_hdr.extend(b"\xfe\x02")
    crc_hdr.extend(struct.pack("<H", sum(crc_hdr) & 0xFFFF))
    buf.extend(crc_hdr)

    # 计算此前所有数据的 MD5 摘要并追加
    md5_hash = hashlib.md5(buf).digest()
    buf.extend(md5_hash)

    # 4. 末尾填充至标准扇区对齐 (容量上限防护)
    capacity = target["script_capacity"] if target is not None else 65536
    total_len = len(buf)
    if total_len > capacity:
        raise ValueError(f"LuaDB 体积超标: {total_len} 字节 > {capacity} 字节 ({capacity // 1024}KB 上限)!")
    buf.extend(b"\x00" * (capacity - total_len))

    return bytes(buf)

def pack_sota_package(raw_luadb_bytes: bytes, target_version: Optional[str] = None, chip_type: str = "ec718", target: Optional[Dict[str, Any]] = None, work_dir: Optional[str] = None) -> Tuple[bytes, Dict]:
    """
    将标准 LuaDB 镜像封装为符合移芯 EC718/EC618 规范的 SOTA 容器升级包。
    - EC718/EC718P/EC718PV: Magic 0xeac37218, 基地址 0x00324000
    - EC618/Air780E/Air780EG: Magic 0xeaf18c16, 基地址 0x0024D000
    包含 92 字节 CoreUpgrade 头部与 CRC32 校验。
    """
    if target is not None:
        target = validate_release_target(target)
        if not isinstance(raw_luadb_bytes, bytes) or len(raw_luadb_bytes) == 0:
            raise ValueError("raw_luadb_bytes 必须为非空 bytes")
        if len(raw_luadb_bytes) != target["script_capacity"]:
            raise ValueError(f"raw_luadb_bytes 长度与 script_capacity 不符: {len(raw_luadb_bytes)} != {target['script_capacity']}")
        chip_type = target["chip"]

    if not target_version:
        target_version = get_version_manifest()["version"]
    if not isinstance(target_version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", target_version):
        raise ValueError(f"target_version 必须为语义化三段数字版本号: {target_version}")

    if not os.path.exists(SOC_TOOLS_EXE):
        raise FileNotFoundError(f"未找到 soc_tools.exe 工具链: {SOC_TOOLS_EXE}")

    if target is not None:
        is_ec618 = (chip_type == "ec618")
        script_addr = f"{target['script_offset']:X}"
    else:
        chip_lower = chip_type.strip().lower() if isinstance(chip_type, str) else ""
        if chip_lower in ("ec618", "air780e", "air780eg"):
            is_ec618 = True
        elif chip_lower in ("ec718", "ec718p", "ec718pv", "air780ep", "air780epv"):
            is_ec618 = False
        else:
            raise ValueError(f"未知或不支持的芯片型号: '{chip_type}' (仅支持 EC618/Air780E/Air780EG 与 EC718/EC718P/EC718PV/Air780EP/Air780EPV)")
        script_addr = "24D000" if is_ec618 else "324000"

    magic_hex = "eaf18c16" if is_ec618 else "eac37218"
    magic_num = 0xeaf18c16 if is_ec618 else FOTA_MAGIC

    if not os.path.exists(DUMMY_BIN):
        raise FileNotFoundError(f"未找到 dummy.bin 占位文件: {DUMMY_BIN}")

    with tempfile.TemporaryDirectory(dir=work_dir) as tmpdir:
        tmp_script_bin = os.path.join(tmpdir, "script.bin")
        tmp_script_zip = os.path.join(tmpdir, "script_fota.zip")
        tmp_output_sota = os.path.join(tmpdir, "output.sota")

        with open(tmp_script_bin, "wb") as f:
            f.write(raw_luadb_bytes)

        # 压缩体并内嵌目标写入基地址
        cmd_zip = [SOC_TOOLS_EXE, "zip_file", magic_hex, script_addr, tmp_script_bin, tmp_script_zip, "40000", "1"]
        try:
            res_zip = subprocess.run(cmd_zip, shell=False, capture_output=True, timeout=30)
            if res_zip.returncode != 0 or not os.path.exists(tmp_script_zip):
                err_msg = _decode_process_output(res_zip)
                raise RuntimeError(f"soc_tools zip_file 失败: {err_msg}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("soc_tools zip_file 超时 (30秒)")

        # 装配 92 字节 CoreUpgrade 头部
        cmd_ota = [SOC_TOOLS_EXE, "make_ota_file", magic_hex, "0", "0", "0", "0", "0", tmp_script_zip, DUMMY_BIN, tmp_output_sota]
        try:
            res_ota = subprocess.run(cmd_ota, shell=False, capture_output=True, timeout=30)
            if res_ota.returncode != 0 or not os.path.exists(tmp_output_sota):
                err_msg = _decode_process_output(res_ota)
                raise RuntimeError(f"soc_tools make_ota_file 失败: {err_msg}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("soc_tools make_ota_file 超时 (30秒)")

        with open(tmp_output_sota, "rb") as f:
            ota_package = f.read()

        # 校验生成的 SOTA 结构
        if len(ota_package) < 96:
            raise ValueError(f"生成的 SOTA 升级包长度异常: {len(ota_package)} < 96")
        magic, header_crc32 = struct.unpack("<II", ota_package[:8])
        if magic != magic_num:
            raise ValueError(f"SOTA 头部 Magic 异常: 0x{magic:08x} != 0x{magic_num:08x}")
        calc_crc32 = (~binascii.crc32(ota_package[8:92])) & 0xffffffff
        if header_crc32 != calc_crc32:
            raise ValueError(f"SOTA 头部 CRC32 异常: 0x{header_crc32:08x} != 0x{calc_crc32:08x}")

        meta = {
            "header_magic": magic_num,
            "header_crc32": header_crc32,
            "chip_type": target["chip"] if target is not None else ("ec618" if is_ec618 else "ec718"),
            "common_len": len(ota_package) - 92,
            "total_len": len(ota_package),
            "package_md5": hashlib.md5(ota_package).hexdigest(),
            "version": target_version
        }
        if target is not None:
            meta["script_offset"] = target["script_offset"]
            meta["script_capacity"] = target["script_capacity"]
        return ota_package, meta


def sha256_bytes(data: bytes) -> str:
    """计算二进制数据的 SHA-256 十六进制摘要"""
    return hashlib.sha256(data).hexdigest()


def md5_bytes(data: bytes) -> str:
    """计算二进制数据的 MD5 十六进制摘要"""
    return hashlib.md5(data).hexdigest()


def canonical_json_dumps(data: Any) -> str:
    """按规范确定性序列化 JSON 字符串 (sort_keys=True, separators=(',', ':'), ensure_ascii=False)"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_json_sha256(data: Any) -> str:
    """计算规范确定性 JSON 的 SHA-256 十六进制摘要"""
    return sha256_bytes(canonical_json_dumps(data).encode("utf-8"))


def build_release_package(
    output_dir: str,
    scripts_dir: str,
    release: Dict[str, Any],
    target: Dict[str, Any]
) -> Dict[str, Any]:
    """
    构建固定发布的脚本镜像发布包。
    校验 release 元数据与 target 规范，并执行源码/环境身份双重计算防线。
    内存产物生成完毕后独占写入 output_dir，最后以 exclusive 写入 manifest.json。
    交由 load_release_package 执行只读核包后返回。
    """
    if not isinstance(release, dict):
        raise ValueError("release 必须为字典")

    version = release.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"^[0-9]+\.[0-9]+\.[0-9]+$", version):
        raise ValueError(f"release.version 必须为严格三段数字版本号: {version}")

    changelog = release.get("changelog", "")
    if changelog is None:
        changelog = ""
    if not isinstance(changelog, str):
        raise ValueError("release.changelog 必须为字符串")

    validated_target = validate_release_target(target)

    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ValueError("output_dir 必须为非空路径字符串")
    if os.path.exists(output_dir):
        raise FileExistsError(f"output_dir 已存在，禁止覆盖: {output_dir}")

    if not isinstance(scripts_dir, str) or not os.path.isdir(scripts_dir):
        raise ValueError(f"scripts_dir 不存在或不是目录: {scripts_dir}")

    identity_before = compute_build_identity(scripts_dir, version, validated_target)

    raw_luadb = pack_luadb(
        scripts_dir=scripts_dir,
        target_version=version,
        build_id=identity_before["build_id"],
        target=validated_target,
    )

    raw_sota, _sota_meta = pack_sota_package(
        raw_luadb_bytes=raw_luadb,
        target_version=version,
        target=validated_target,
    )

    identity_after = compute_build_identity(scripts_dir, version, validated_target)
    if identity_after != identity_before:
        raise RuntimeError("构建前后源码或工具环境身份不一致，构建结果已废除")

    script_info = {
        "file": "script.bin",
        "size": len(raw_luadb),
        "sha256": sha256_bytes(raw_luadb),
        "md5": md5_bytes(raw_luadb),
    }
    sota_info = {
        "file": "script_ota.bin",
        "size": len(raw_sota),
        "sha256": sha256_bytes(raw_sota),
        "md5": md5_bytes(raw_sota),
    }

    manifest_payload = {
        "schema": "air780-package/v1",
        "version": version,
        "build_id": identity_before["build_id"],
        "source_sha256": identity_before["source_sha256"],
        "source_inputs": identity_before["source_inputs"],
        "tools": identity_before["tools"],
        "target": validated_target,
        "script": script_info,
        "sota": sota_info,
        "changelog": changelog,
    }
    package_id = canonical_json_sha256(manifest_payload)
    manifest = dict(manifest_payload)
    manifest["package_id"] = package_id

    os.makedirs(output_dir, exist_ok=False)

    script_path = os.path.join(output_dir, "script.bin")
    with open(script_path, "xb") as f:
        f.write(raw_luadb)

    sota_path = os.path.join(output_dir, "script_ota.bin")
    with open(sota_path, "xb") as f:
        f.write(raw_sota)

    manifest_path = os.path.join(output_dir, "manifest.json")
    manifest_content = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    with open(manifest_path, "x", encoding="utf-8") as f:
        f.write(manifest_content)

    return load_release_package(output_dir)


def _validate_luadb_structure(data: bytes, target: Dict[str, Any]) -> None:
    """严格核验二进制数据是否完全符合标准 LuaDB 结构与 target 约束"""
    if len(data) < 24:
        raise ValueError("LuaDB 数据长度不足 24 字节根头")
    if data[:18] != b"\x01\x04\x5a\xa5\x5a\xa5\x02\x02\x02\x00\x03\x04\x18\x00\x00\x00\x04\x02":
        raise ValueError("LuaDB 根头固定格式校验失败")
    file_count = struct.unpack("<H", data[18:20])[0]
    if file_count != len(CORE_SCRIPTS) + 1:
        raise ValueError(f"LuaDB 记录文件总数异常: {file_count} != {len(CORE_SCRIPTS) + 1}")
    if data[20:22] != b"\xfe\x02":
        raise ValueError("LuaDB 根头校验 tag 缺失")
    if (sum(data[:22]) & 0xFFFF) != struct.unpack("<H", data[22:24])[0]:
        raise ValueError("LuaDB 根头累加校验和错误")

    offset = 24
    expected_names = [fn.replace(".lua", ".luac") for fn in CORE_SCRIPTS]
    seen_names = []
    expected_width = target["lua_bitw"] // 8

    for _ in range(len(CORE_SCRIPTS)):
        if offset + 18 > len(data):
            raise ValueError("LuaDB 条目头边界越界")
        if data[offset:offset + 6] != b"\x01\x04\x5a\xa5\x5a\xa5" or data[offset + 6] != 2:
            raise ValueError("LuaDB 条目魔数或文件名 tag 错误")
        name_len = data[offset + 7]
        hdr_len = 18 + name_len
        if offset + hdr_len > len(data):
            raise ValueError("LuaDB 条目头长度越界")
        name_bytes = data[offset + 8:offset + 8 + name_len]
        if data[offset + 8 + name_len:offset + 10 + name_len] != b"\x03\x04":
            raise ValueError("LuaDB 条目长度 tag 错误")
        content_len = struct.unpack("<I", data[offset + 10 + name_len:offset + 14 + name_len])[0]
        if data[offset + 14 + name_len:offset + 16 + name_len] != b"\xfe\x02":
            raise ValueError("LuaDB 条目校验 tag 错误")
        csum = struct.unpack("<H", data[offset + 16 + name_len:offset + 18 + name_len])[0]
        if (sum(data[offset:offset + 16 + name_len]) & 0xFFFF) != csum:
            raise ValueError("LuaDB 条目头校验和错误")
        c_start = offset + hdr_len
        c_end = c_start + content_len
        if c_end > len(data):
            raise ValueError("LuaDB 条目内容边界越界")
        content = data[c_start:c_end]
        try:
            name_str = name_bytes.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("LuaDB 条目文件名非有效 ASCII")
        if name_str in seen_names:
            raise ValueError(f"LuaDB 条目文件名重复: {name_str}")
        seen_names.append(name_str)

        if len(content) < 33:
            raise ValueError(f"LuaDB 编译产物过短: {name_str}")
        if not content.startswith(b"\x1bLua\x53\x00\x19\x93\x0d\x0a\x1a\x0a"):
            raise ValueError(f"LuaDB 编译头签名校验失败: {name_str}")
        if content[15] != expected_width or content[16] != expected_width:
            raise ValueError(f"LuaDB 脚本位宽与 target 不匹配: {name_str}")
        offset = c_end

    if set(seen_names) != set(expected_names):
        raise ValueError("LuaDB 文件条目集合与 CORE_SCRIPTS 不一致")

    if offset + 18 > len(data):
        raise ValueError("LuaDB 尾部校验头边界越界")
    if data[offset:offset + 6] != b"\x01\x04\x5a\xa5\x5a\xa5" or data[offset + 6] != 2:
        raise ValueError("LuaDB 尾部校验头魔数错误")
    crc_name_len = data[offset + 7]
    crc_hdr_len = 18 + crc_name_len
    if offset + crc_hdr_len > len(data):
        raise ValueError("LuaDB 尾部校验头越界")
    if data[offset + 8:offset + 8 + crc_name_len] != b".airm2m_all_crc#.bin":
        raise ValueError("LuaDB 尾部校验文件名必须为 .airm2m_all_crc#.bin")
    if data[offset + 8 + crc_name_len:offset + 10 + crc_name_len] != b"\x03\x04":
        raise ValueError("LuaDB 尾部校验长度 tag 错误")
    if struct.unpack("<I", data[offset + 10 + crc_name_len:offset + 14 + crc_name_len])[0] != 16:
        raise ValueError("LuaDB 尾部校验内容长度必须为 16")
    if data[offset + 14 + crc_name_len:offset + 16 + crc_name_len] != b"\xfe\x02":
        raise ValueError("LuaDB 尾部校验 tag 错误")
    crc_csum = struct.unpack("<H", data[offset + 16 + crc_name_len:offset + 18 + crc_name_len])[0]
    if (sum(data[offset:offset + 16 + crc_name_len]) & 0xFFFF) != crc_csum:
        raise ValueError("LuaDB 尾部校验头校验和错误")
    crc_start = offset + crc_hdr_len
    crc_end = crc_start + 16
    if crc_end > len(data):
        raise ValueError("LuaDB 尾部校验内容越界")
    if data[crc_start:crc_end] != hashlib.md5(data[:crc_start]).digest():
        raise ValueError("LuaDB 尾部 MD5 签名校验失败")
    if data[crc_end:] != b"\x00" * (len(data) - crc_end):
        raise ValueError("LuaDB 尾部填充非全零")


def load_release_package(package_dir: str) -> Dict[str, Any]:
    """
    只读解析并严格核验发布包目录完整性与真实性。
    禁止编译或重包，不依赖宿主环境或外部源码/工具路径。
    返回 {manifest, script_path, sota_path}。
    """
    if not isinstance(package_dir, str) or not package_dir.strip():
        raise ValueError("package_dir 必须为非空路径字符串")
    real_pkg_dir = os.path.realpath(package_dir)
    if not os.path.isdir(real_pkg_dir):
        raise FileNotFoundError(f"发布包目录不存在或非目录: {package_dir}")

    real_paths = {}
    for fn in ("manifest.json", "script.bin", "script_ota.bin"):
        fp = os.path.join(package_dir, fn)
        real_fp = os.path.realpath(fp)
        try:
            if os.path.commonpath([real_pkg_dir, real_fp]) != real_pkg_dir or real_fp == real_pkg_dir:
                raise ValueError(f"文件路径逃逸发布包目录: {fn}")
        except ValueError:
            raise ValueError(f"文件路径逃逸发布包目录: {fn}")
        if not os.path.isfile(real_fp):
            raise FileNotFoundError(f"发布包文件不存在: {fn}")
        if os.path.getsize(real_fp) == 0:
            raise ValueError(f"发布包文件为空: {fn}")
        real_paths[fn] = real_fp

    def _reject_dup(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        for k, v in pairs:
            if k in d:
                raise ValueError(f"manifest.json 存在重复键: {k}")
            d[k] = v
        return d

    with open(real_paths["manifest.json"], "rb") as f:
        m_bytes = f.read()
    try:
        manifest = json.loads(m_bytes.decode("utf-8"), object_pairs_hook=_reject_dup)
    except Exception as e:
        raise ValueError(f"manifest.json 解析失败: {e}")
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json 根节点必须为对象")

    req_keys = {
        "schema", "version", "build_id", "source_sha256", "source_inputs",
        "tools", "target", "script", "sota", "changelog", "package_id"
    }
    if set(manifest.keys()) != req_keys:
        raise ValueError("manifest 字段集合不符合契约")

    if manifest["schema"] != "air780-package/v1":
        raise ValueError(f"manifest schema 不匹配: {manifest['schema']}")

    version = manifest["version"]
    if not isinstance(version, str) or not re.fullmatch(r"^[0-9]+\.[0-9]+\.[0-9]+$", version):
        raise ValueError(f"manifest version 无效: {version}")

    if not isinstance(manifest["changelog"], str):
        raise ValueError("manifest changelog 必须为字符串")

    def _is_hex(s: Any, length: int) -> bool:
        return isinstance(s, str) and len(s) == length and bool(re.fullmatch(r"^[0-9a-f]+$", s))

    for k in ("source_sha256", "build_id", "package_id"):
        if not _is_hex(manifest[k], 64):
            raise ValueError(f"manifest {k} 必须为 64 位小写十六进制")

    target = manifest["target"]
    validated_target = validate_release_target(target)
    if target != validated_target:
        raise ValueError("manifest target 未严格规范化")

    source_inputs = manifest["source_inputs"]
    if not isinstance(source_inputs, dict) or set(source_inputs.keys()) != set(CORE_SCRIPTS):
        raise ValueError("source_inputs 键集合必须恰好等于 CORE_SCRIPTS")
    for fn, h in source_inputs.items():
        if not _is_hex(h, 64):
            raise ValueError(f"source_inputs[{fn}] 必须为 64 位小写十六进制 SHA-256")

    tools = manifest["tools"]
    if not isinstance(tools, dict) or set(tools.keys()) != {"luac", "soc_tools", "dummy"}:
        raise ValueError("tools 键集合必须恰好为 luac, soc_tools, dummy")
    for tk, h in tools.items():
        if not _is_hex(h, 64):
            raise ValueError(f"tools[{tk}] 必须为 64 位小写十六进制 SHA-256")

    if canonical_json_sha256(source_inputs) != manifest["source_sha256"]:
        raise ValueError("manifest source_sha256 重算不匹配")

    build_payload = {
        "version": version,
        "source_sha256": manifest["source_sha256"],
        "tools": tools,
        "target": target,
    }
    if canonical_json_sha256(build_payload) != manifest["build_id"]:
        raise ValueError("manifest build_id 重算不匹配")

    pkg_payload = {k: v for k, v in manifest.items() if k != "package_id"}
    if canonical_json_sha256(pkg_payload) != manifest["package_id"]:
        raise ValueError("manifest package_id 重算不匹配")

    script_info = manifest["script"]
    if not isinstance(script_info, dict) or set(script_info.keys()) != {"file", "size", "sha256", "md5"}:
        raise ValueError("script 字段集合不符合契约")
    if script_info["file"] != "script.bin":
        raise ValueError("script.file 必须为 script.bin")
    if type(script_info["size"]) is not int or script_info["size"] <= 0:
        raise ValueError("script.size 必须为正整数且非 bool")
    if not _is_hex(script_info["sha256"], 64) or not _is_hex(script_info["md5"], 32):
        raise ValueError("script 哈希格式错误")

    sota_info = manifest["sota"]
    if not isinstance(sota_info, dict) or set(sota_info.keys()) != {"file", "size", "sha256", "md5"}:
        raise ValueError("sota 字段集合不符合契约")
    if sota_info["file"] != "script_ota.bin":
        raise ValueError("sota.file 必须为 script_ota.bin")
    if type(sota_info["size"]) is not int or sota_info["size"] <= 0:
        raise ValueError("sota.size 必须为正整数且非 bool")
    if not _is_hex(sota_info["sha256"], 64) or not _is_hex(sota_info["md5"], 32):
        raise ValueError("sota 哈希格式错误")

    script_path = real_paths["script.bin"]
    with open(script_path, "rb") as f:
        script_bytes = f.read()
    if len(script_bytes) != script_info["size"]:
        raise ValueError("script.bin 实际大小与 manifest 不符")
    if sha256_bytes(script_bytes) != script_info["sha256"] or md5_bytes(script_bytes) != script_info["md5"]:
        raise ValueError("script.bin 哈希校验失败")
    if len(script_bytes) != target["script_capacity"]:
        raise ValueError(f"script.bin 大小不等于 target.script_capacity ({len(script_bytes)} != {target['script_capacity']})")

    sota_path = real_paths["script_ota.bin"]
    with open(sota_path, "rb") as f:
        sota_bytes = f.read()
    if len(sota_bytes) != sota_info["size"]:
        raise ValueError("script_ota.bin 实际大小与 manifest 不符")
    if sha256_bytes(sota_bytes) != sota_info["sha256"] or md5_bytes(sota_bytes) != sota_info["md5"]:
        raise ValueError("script_ota.bin 哈希校验失败")
    if len(sota_bytes) < 96:
        raise ValueError(f"script_ota.bin 长度异常: {len(sota_bytes)} < 96")

    sota_magic, sota_header_crc32 = struct.unpack("<II", sota_bytes[:8])
    expected_magic = 0xeaf18c16 if target["chip"] == "ec618" else FOTA_MAGIC
    if sota_magic != expected_magic:
        raise ValueError(f"SOTA 头部 Magic 异常: 0x{sota_magic:08x} != 0x{expected_magic:08x}")
    calc_crc32 = (~binascii.crc32(sota_bytes[8:92])) & 0xffffffff
    if sota_header_crc32 != calc_crc32:
        raise ValueError(f"SOTA 头部 CRC32 异常: 0x{sota_header_crc32:08x} != 0x{calc_crc32:08x}")

    _validate_luadb_structure(script_bytes, target)

    return {
        "manifest": manifest,
        "script_path": script_path,
        "sota_path": sota_path,
    }


def verify_release_package(package_dir: str, work_dir: str) -> Dict[str, Any]:
    """
    对已发布的固化包执行官方工具一致性验证。
    核验宿主官方工具哈希与 manifest.tools 一致，在独立 work_dir 中重现 SOTA 封装，
    比对生成字节与冻结 script_ota.bin 是否完全一致。
    只用于任何设备副作用前的预检，业务执行仍使用冻结原包。
    不重编 Lua，不修改版本，不宣称完整容器格式解码。
    返回与 load_release_package 相同的数据结构。
    """
    pkg_data = load_release_package(package_dir)
    manifest = pkg_data["manifest"]

    tool_items = [
        ("luac", LUAC_EXE),
        ("soc_tools", SOC_TOOLS_EXE),
        ("dummy", DUMMY_BIN),
    ]
    manifest_tools = manifest.get("tools", {})
    for key, path in tool_items:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"官方工具文件不存在: {path}")
        if key != "dummy" and os.path.getsize(path) == 0:
            raise ValueError(f"官方工具文件为空: {path}")
        with open(path, "rb") as f:
            content = f.read()
        if key != "dummy" and len(content) == 0:
            raise ValueError(f"官方工具文件为空: {path}")
        actual_sha = sha256_bytes(content)
        expected_sha = manifest_tools.get(key)
        if actual_sha != expected_sha:
            raise ValueError(f"官方工具 {key} SHA256 不匹配: 实际 {actual_sha} != 清单 {expected_sha}")

    if not isinstance(work_dir, str) or not work_dir.strip():
        raise ValueError("work_dir 必须为非空路径字符串")
    real_work_dir = os.path.realpath(work_dir)
    if not os.path.isdir(real_work_dir):
        raise ValueError(f"work_dir 不存在或不是目录: {work_dir}")

    with open(pkg_data["script_path"], "rb") as f:
        script_bytes = f.read()

    generated_sota, _meta = pack_sota_package(
        raw_luadb_bytes=script_bytes,
        target_version=manifest["version"],
        target=manifest["target"],
        work_dir=real_work_dir,
    )

    with open(pkg_data["sota_path"], "rb") as f:
        frozen_sota = f.read()

    if generated_sota != frozen_sota:
        raise ValueError("官方工具一致性验证失败: 重产 SOTA 字节与冻结包不一致")

    pkg_again = load_release_package(package_dir)
    if pkg_again["manifest"]["package_id"] != manifest["package_id"]:
        raise ValueError("发布包验证前后 package_id 不一致")

    return pkg_again


def get_fota_bundle_dir(custom_path: Optional[str] = None) -> Optional[str]:
    """
    获取统一 fota_bundle 逻辑资源位置。
    frozen 模式下读取 _MEIPASS/fota_bundle，缺包不得回退源码目录或硬编码版本；
    非 frozen 模式在 tools/host_gateway/fota_bundle 查找；
    若提供 custom_path 则以其为准。
    若目标目录不存在，返回 None。
    """
    if custom_path:
        return custom_path if os.path.exists(custom_path) else None
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if not meipass:
            return None
        candidate = os.path.join(meipass, "fota_bundle")
        return candidate if os.path.isdir(candidate) else None
    candidate = os.path.join(BASE_DIR, "fota_bundle")
    return candidate if os.path.isdir(candidate) else None


def load_bundled_release_package(bundle_dir: Optional[str] = None) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """
    只读加载并校验随附 release 包。
    返回 (check_status, package_data, error_reason):
    - ("no_package", None, "未检测到上位机随附更新包"): 目录缺失或为空
    - ("invalid_package", None, reason): 存在目录但校验失败
    - ("ok", package_data, None): 校验成功
    """
    pkg_dir = get_fota_bundle_dir(bundle_dir)
    if not pkg_dir or not os.path.exists(pkg_dir):
        return ("no_package", None, "未检测到上位机随附更新包")
    if not os.path.isdir(pkg_dir):
        return ("invalid_package", None, "随附包路径非有效目录")
    try:
        pkg_data = load_release_package(pkg_dir)
        return ("ok", pkg_data, None)
    except FileNotFoundError as e:
        return ("invalid_package", None, f"随附更新包文件缺失: {e}")
    except Exception as e:
        return ("invalid_package", None, f"随附更新包校验失败: {e}")


if __name__ == "__main__":
    print("⚡ 测试运行 Lua 语法预检与 LuaDB 打包器...")
    ok, errs = validate_lua_syntax()
    if not ok:
        print("[-] 语法预检失败:")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    print("[+] 语法预检 100% 通过！")

    bin_data = pack_luadb()
    print(f"[+] 成功打包标准 LuaDB (大小: {len(bin_data)} 字节, MD5: {hashlib.md5(bin_data).hexdigest()})")

    sota_data, sota_meta = pack_sota_package(bin_data)
    print(f"[+] 成功封装标准 SOTA 容器包 (大小: {sota_meta['total_len']} 字节, Magic: 0x{sota_meta['header_magic']:08x}, MD5: {sota_meta['package_md5']})")
