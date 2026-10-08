# -*- coding: utf-8 -*-
"""
数字蜂巢 · CellHive 跨端通用运行时资源与存储寻址助手 (Core Runtime)
职责：
1. 统一寻址 Web 静态资源目录（自动识别源码态、PyInstaller 冻结态、飞牛套件态）；
2. 统一获取应用配置目录与数据持久化目录（自动识别飞牛 TRIM_PKGETC/TRIM_PKGVAR、Windows LocalAppData 及本地开发态）。
"""

import os
import sys

def get_web_dir() -> str:
    """获取 Web 静态资产目录 (包含 index.html)"""
    # 1. PyInstaller 打包运行态
    if getattr(sys, "frozen", False):
        base_meipass = getattr(sys, "_MEIPASS", "")
        candidate = os.path.join(base_meipass, "web")
        if os.path.exists(candidate):
            return candidate

    # 2. 飞牛 fnOS 套件运行态
    trim_dest = os.environ.get("TRIM_APPDEST")
    if trim_dest:
        candidate = os.path.join(trim_dest, "core", "web")
        if os.path.exists(candidate):
            return candidate

    # 3. 源码开发态：寻址 core/web
    current_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(current_dir, "web")
    if os.path.exists(candidate):
        return candidate

    # 4. 容错寻址：若当前位于 platforms/ 内部，寻址 ../../core/web
    project_root = os.path.dirname(current_dir)
    fallback = os.path.join(project_root, "core", "web")
    if os.path.exists(fallback):
        return fallback

    return candidate


def get_data_dir() -> str:
    """获取数据持久化目录 (存放历史短信、日志等)"""
    # 1. 飞牛 fnOS 环境变量
    trim_var = os.environ.get("TRIM_PKGVAR")
    if trim_var:
        os.makedirs(trim_var, exist_ok=True)
        return trim_var

    # 2. Windows LocalAppData
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            path = os.path.join(local_app_data, "CellHive", "data")
            os.makedirs(path, exist_ok=True)
            return path

    # 3. 默认本地开发态
    current_dir = os.path.dirname(os.path.abspath(__file__))
    fallback = os.path.join(os.path.dirname(current_dir), ".runtime")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def get_config_dir() -> str:
    """获取配置文件目录 (存放 gateway_config.json)"""
    # 1. 飞牛 fnOS 环境变量
    trim_etc = os.environ.get("TRIM_PKGETC")
    if trim_etc:
        os.makedirs(trim_etc, exist_ok=True)
        return trim_etc

    # 2. 默认返回与 data_dir 同级或当前工程根目录
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    return os.path.join(project_root, "config") if os.path.exists(os.path.join(project_root, "config")) else current_dir


def directory(kind: str) -> str:
    """获取指定类型的运行时目录 (兼容历史接口)"""
    data = get_data_dir()
    if kind == "dataDir":
        return data
    elif kind == "logDir":
        p = os.path.join(data, "logs")
        os.makedirs(p, exist_ok=True)
        return p
    elif kind == "cacheDir":
        p = os.path.join(data, "cache")
        os.makedirs(p, exist_ok=True)
        return p
    return data

