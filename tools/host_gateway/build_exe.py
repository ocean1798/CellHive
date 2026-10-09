#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780EPV 智能随身通信网关 - Windows 独立 exe 打包构建脚本
使用 PyInstaller 将网关 Hub、Web 服务端、前端看板与系统托盘打包为单文件绿色免安装 exe。
"""

import os
import sys
import shutil
import subprocess
import argparse

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
ENTRY_SCRIPT = os.path.join(CURRENT_DIR, "gateway_app.py")
WEB_DIR = os.path.join(CURRENT_DIR, "web")
ICON_PATH = os.path.join(CURRENT_DIR, "app.ico")
DIST_DIR = os.path.join(CURRENT_DIR, "dist")
BUILD_DIR = os.path.join(CURRENT_DIR, "build")

LUA_SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "deploy", "smart-gateway-780epv")
FLASHER_DIR = os.path.join(PROJECT_ROOT, "tools", "flasher")
DUMMY_BIN = os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "dummy.bin")

def build(output_dir=None, build_info=None, fota_bundle=None):
    # 0. 在创建输出目录与启动 PyInstaller 之前，必须对用户指定的随附包进行前置完整性校验
    fota_bundle_valid = None
    if fota_bundle:
        bundle_abs = os.path.abspath(fota_bundle)
        if not os.path.isdir(bundle_abs):
            print(f"[-] 错误: 指定的随附包目录不存在或非目录: {bundle_abs}")
            sys.exit(1)

        # 随附包目录项白名单严审：仅允许 manifest.json, script.bin, script_ota.bin 三个正规文件
        # 严禁任何额外文件、目录或符号链接，杜绝非包内容随目录打入 EXE
        expected_items = {"manifest.json", "script.bin", "script_ota.bin"}
        try:
            actual_items = set(os.listdir(bundle_abs))
            if actual_items != expected_items:
                extra = actual_items - expected_items
                missing = expected_items - actual_items
                reasons = []
                if extra:
                    reasons.append(f"包含非法额外项 {sorted(list(extra))}")
                if missing:
                    reasons.append(f"缺少必要文件 {sorted(list(missing))}")
                print(f"[-] 错误: 随附包目录项不合法: {'; '.join(reasons)}")
                sys.exit(1)

            for item_name in actual_items:
                item_path = os.path.join(bundle_abs, item_name)
                if os.path.islink(item_path):
                    print(f"[-] 错误: 随附包禁止包含符号链接/快捷方式: {item_name}")
                    sys.exit(1)
                if os.path.isdir(item_path):
                    print(f"[-] 错误: 随附包禁止包含子目录: {item_name}")
                    sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            print(f"[-] 错误: 扫描随附包目录失败: {e}")
            sys.exit(1)

        try:
            import luadb_packer
            pkg_data = luadb_packer.load_release_package(bundle_abs)
            manifest = pkg_data.get("manifest", {})
            pkg_ver = manifest.get("version")
            pkg_id = manifest.get("package_id")
            print(f"[+] 随附更新包前置校验通过: v{pkg_ver} (package_id: {pkg_id[:16] if pkg_id else 'none'}...)")
            fota_bundle_valid = bundle_abs
        except Exception as e:
            print(f"[-] 错误: 指定随附包完整性校验未通过: {e}")
            sys.exit(1)

    dist_dir = os.path.join(output_dir, "dist") if output_dir else DIST_DIR
    build_dir = os.path.join(output_dir, "work") if output_dir else BUILD_DIR
    if output_dir:
        os.makedirs(output_dir, exist_ok=False)
    print("=" * 60)
    print("  开始构建 Air780EPV-Gateway Windows 独立分发版 (.exe)")
    print("=" * 60)

    # 1. 检查必要文件
    if not os.path.exists(ENTRY_SCRIPT):
        print(f"[-] 错误: 未找到入口脚本: {ENTRY_SCRIPT}")
        sys.exit(1)
    if not os.path.exists(WEB_DIR):
        print(f"[-] 错误: 未找到 Web 目录: {WEB_DIR}")
        sys.exit(1)

    # 2. 构建 PyInstaller 参数
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", "Air780EPV-Gateway",
        "--distpath", dist_dir,
        "--workpath", build_dir,
        "--specpath", output_dir or CURRENT_DIR,
        f"--add-data={WEB_DIR};web",
        f"--add-data={ICON_PATH};.",
        f"--add-data={LUA_SCRIPTS_DIR};lua_scripts",
        f"--add-data={FLASHER_DIR};flasher",
        f"--add-data={DUMMY_BIN};.",
        "--hidden-import=pystray._win32",
        "--hidden-import=serial.tools.list_ports",
        "--hidden-import=PIL.ImageDraw",
        "--hidden-import=PIL.Image",
        "--hidden-import=clr",
        "--hidden-import=pythonnet",
        "--hidden-import=clr_loader",
        "--hidden-import=bottle",
        "--hidden-import=webview",
        "--hidden-import=webview.platforms.winforms",
        "--collect-all=webview",
    ]
    if build_info:
        cmd.append(f"--add-data={build_info};.")

    # 统一装入受验随附包逻辑资源位置 (AIR-38 / fota_bundle)
    if fota_bundle_valid:
        cmd.append(f"--add-data={fota_bundle_valid};fota_bundle")
        print(f"[*] 随附受验包已配置打包: {fota_bundle_valid} -> fota_bundle")
    else:
        print("[*] 未提供随附包，生成免随附包安装镜像 (运行时报告 no_package)")

    if os.path.exists(ICON_PATH):
        cmd.append(f"--icon={ICON_PATH}")

    cmd.append(ENTRY_SCRIPT)

    print("[*] 执行打包指令:")
    print(" ".join(cmd))
    print("-" * 60)

    res = subprocess.run(cmd, cwd=CURRENT_DIR)
    if res.returncode != 0:
        print(f"[-] 打包失败，退出码: {res.returncode}")
        sys.exit(res.returncode)

    # 4. 产物校验、体积门禁与双轨兼容分发 (CellHive.exe 与 Air780EPV-Gateway.exe)
    primary_exe = os.path.join(dist_dir, "CellHive.exe")
    legacy_exe = os.path.join(dist_dir, "Air780EPV-Gateway.exe")

    # 如果输出了 Air780EPV-Gateway.exe 且主产物不存在，同步复制
    if os.path.exists(legacy_exe) and not os.path.exists(primary_exe):
        shutil.copy2(legacy_exe, primary_exe)
    elif os.path.exists(primary_exe) and not os.path.exists(legacy_exe):
        shutil.copy2(primary_exe, legacy_exe)

    target_exe = primary_exe if os.path.exists(primary_exe) else legacy_exe

    if os.path.exists(target_exe):
        # 确保双轨产物同时存在且字节一致
        if not os.path.exists(primary_exe) or not os.path.exists(legacy_exe):
            if os.path.exists(primary_exe):
                shutil.copy2(primary_exe, legacy_exe)
            else:
                shutil.copy2(legacy_exe, primary_exe)

        for exe_p in (primary_exe, legacy_exe):
            size_mb = os.path.getsize(exe_p) / (1024 * 1024)
            assert size_mb <= 55.0, f"[-] 门禁阻断：Windows 单文件 EXE ({os.path.basename(exe_p)}) 体积超出 55MB 上限门禁！当前: {size_mb:.2f} MB > 55.00 MB"

        primary_size_mb = os.path.getsize(primary_exe) / (1024 * 1024)
        print("=" * 60)
        print(f"[+] 打包成功！双轨可执行文件已生成:")
        print(f"   主程序: {primary_exe} ({primary_size_mb:.2f} MB)")
        print(f"   兼容别名: {legacy_exe} ({os.path.getsize(legacy_exe) / (1024 * 1024):.2f} MB)")
        print(f"   说明: 单文件绿色免安装，双击即可在后台常驻并托盘运行。")
        print("=" * 60)
    else:
        print("[-] 未在预期目录找到输出文件")
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir")
    parser.add_argument("--build-info")
    parser.add_argument("--fota-bundle", help="显式受验随附成品包目录")
    args = parser.parse_args()
    build(os.path.abspath(args.output_dir) if args.output_dir else None, args.build_info, args.fota_bundle)
