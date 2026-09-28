#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780EPV 智能随身通信网关 - Windows 桌面独立应用主程序
整合物理中枢 Hub (17800)、Web 控制台 (17801) 与系统托盘 (System Tray)，
支持开箱即用、无黑框静默后台常驻、短信验证码毫秒级存入剪贴板。
"""

import os
import sys
import time
import socket
import json
import threading
import webbrowser
import argparse
import shutil
import subprocess
import gateway_runtime as runtime

# Explicit build verification exits before desktop mutex, browser, tray or Hub startup.
if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--verify-web":
    from gateway_package_check import verify_web
    verify_web(sys.argv[2])
    raise SystemExit(0)

# 确保在 Windows 下标准输出为 UTF-8，且在 windowed 模式下有安全回退流
class _SafeStream:
    def write(self, *args, **kwargs): pass
    def flush(self): pass
    def isatty(self): return False

if sys.stdout is None:
    sys.stdout = _SafeStream()
if sys.stderr is None:
    sys.stderr = _SafeStream()

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 路径自适应
def get_bundle_dir() -> str:
    """获取只读静态资源目录 (PyInstaller 解压根目录)"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

def get_config_dir() -> str:
    """获取可写配置持久化目录 (始终位于 exe 或主脚本同级)"""
    return runtime.directory("dataDir")

# 引入中枢核心与 Web 服务器
from gateway_hub import GatewayHub, HUB_HOST, HUB_PORT, SERIAL_PORT, SERIAL_BAUD, show_windows_toast
from gateway_web import WebServer, DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, get_autostart_status, set_autostart_status

_app_mutex = None

def _log_debug(msg: str):
    try:
        log_file = os.path.join(runtime.directory("logDir"), "gateway_app.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass

def find_app_browser() -> str:
    """探测支持 --app 原生应用模式的浏览器 (优先 Edge，其次 Chrome)"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    for name in ["msedge", "chrome"]:
        p = shutil.which(name)
        if p:
            return p
    return None

def launch_desktop_app_window(url: str) -> bool:
    """唤起无地址栏、无标签页、无书签栏的独立原生 Desktop App 窗口"""
    browser_exe = find_app_browser()
    if browser_exe:
        try:
            cmd = [
                browser_exe,
                f"--app={url}",
                "--window-size=1280,820",
                "--app-id=Air780EPVGateway"
            ]
            _log_debug(f"Launching App Window: {cmd}")
            subprocess.Popen(cmd)
            return True
        except Exception as e:
            _log_debug(f"Failed to launch app window: {e}")
    # 回退机制：若无 Edge/Chrome，回退至系统默认浏览器
    webbrowser.open(url)
    return False

def acquire_app_mutex() -> bool:
    """确保全局单实例运行；若已运行则自动调起已有看板并退出新实例"""
    global _app_mutex
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            _app_mutex = kernel32.CreateMutexW(None, True, "Air780EPV_Gateway_Desktop_App_Mutex")
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                _log_debug("Mutex already exists, activating app window and exiting...")
                # 已有实例在运行，直接唤起独立的 App 客户端窗口并安全退出
                launch_desktop_app_window(f"http://127.0.0.1:{DEFAULT_WEB_PORT}")
                return False
        except Exception as e:
            _log_debug(f"Mutex error: {e}")
    return True


def create_tray_icon_image():
    """获取托盘图标：优先加载内置/同级 app.ico，若无则动态绘制高对比度天线图标"""
    ico_path = os.path.join(get_bundle_dir(), "app.ico")
    if os.path.exists(ico_path):
        try:
            from PIL import Image
            return Image.open(ico_path)
        except Exception:
            pass
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # 背景：深邃工控蓝圆角底
        d.ellipse((2, 2, 62, 62), fill=(15, 23, 42, 255), outline=(56, 189, 248, 255), width=2)
        # 4G 阶梯信号条 (青色高亮)
        color = (56, 189, 248, 255)
        d.rectangle((16, 38, 20, 48), fill=color)
        d.rectangle((24, 32, 28, 48), fill=color)
        d.rectangle((32, 25, 36, 48), fill=color)
        d.rectangle((40, 16, 44, 48), fill=color)
        return img
    except Exception:
        return None


class GatewayDesktopApp:
    def __init__(self, com=SERIAL_PORT, baud=SERIAL_BAUD, hub_port=HUB_PORT, web_port=DEFAULT_WEB_PORT, open_browser=True, use_tray=True):
        self.com = com
        self.baud = baud
        self.hub_port = hub_port
        self.web_port = web_port
        self.open_browser = open_browser
        self.use_tray = use_tray

        self.hub = None
        self.web_server = None
        self.tray_icon = None
        self.running = False

    def start(self):
        _log_debug("GatewayDesktopApp.start() called")
        self.running = True

        # 1. 启动物理通信中枢 Hub (单例持有串口与 17800 端口)
        self.hub = GatewayHub(host="127.0.0.1", port=self.hub_port, com=self.com, baud=self.baud)
        def _run_hub():
            try:
                _log_debug("Starting Hub...")
                self.hub.start()
            except Exception as e:
                _log_debug(f"[Hub Thread Error] {e}")
                if sys.stderr:
                    sys.stderr.write(f"[Hub Thread Error] {e}\n")
        hub_thread = threading.Thread(target=_run_hub, daemon=True, name="HubThread")
        hub_thread.start()

        # 等待 Hub 端口绑定
        time.sleep(0.8)

        # 2. 启动 Web 控制台 (17801 端口)
        self.web_server = WebServer(host="0.0.0.0", port=self.web_port, hub_host="127.0.0.1", hub_port=self.hub_port)
        def _run_web():
            try:
                _log_debug("Starting WebServer...")
                self.web_server.start()
            except Exception as e:
                _log_debug(f"[Web Thread Error] {e}")
                if sys.stderr:
                    sys.stderr.write(f"[Web Thread Error] {e}\n")
        web_thread = threading.Thread(target=_run_web, daemon=True, name="WebThread")
        web_thread.start()

        time.sleep(0.5)

        # 3. 自动弹出独立原生 App 客户端窗口
        if self.open_browser:
            threading.Thread(target=self._delayed_open_app_window, daemon=True).start()

        # 4. 弹出 Windows Toast 提示
        show_windows_toast("数字蜂巢 · CellHive", "蜂巢中枢已在后台守护，短信验证码将毫秒级存入剪贴板。")

        # 5. 启动系统托盘或保持命令行主循环
        if self.use_tray:
            _log_debug("Running tray...")
            self._run_tray()
        else:
            _log_debug("Running headless...")
            self._run_headless()

    def _delayed_open_app_window(self):
        time.sleep(1.2)
        launch_desktop_app_window(f"http://127.0.0.1:{self.web_port}")

    def _run_tray(self):
        try:
            import pystray
            icon_img = create_tray_icon_image()
            if not icon_img:
                raise RuntimeError("无法创建托盘图像")

            menu = pystray.Menu(
                pystray.MenuItem("📱 打开数字蜂巢控制台", self._action_open_app, default=True),
                pystray.MenuItem("开机自动启动", self._action_toggle_autostart, checked=lambda item: get_autostart_status()),
                pystray.MenuItem("⚡ 软复位模组 (重启)", self._action_reboot_board),
                pystray.MenuItem("📁 打开配置目录", self._action_open_config_dir),
                pystray.MenuItem("ℹ️ 关于数字蜂巢", self._action_about),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("❌ 退出数字蜂巢", self._action_exit)
            )

            self.tray_icon = pystray.Icon("CellHive", icon_img, "数字蜂巢 · CellHive", menu)
            self.tray_icon.run()
        except Exception as e:
            if sys.stderr:
                sys.stderr.write(f"[*] 托盘服务启动失败 ({e})，回退至前台运行模式\n")
            self._run_headless()

    def _run_headless(self):
        try:
            while self.running:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _action_open_app(self, icon=None, item=None):
        launch_desktop_app_window(f"http://127.0.0.1:{self.web_port}")

    def _action_toggle_autostart(self, icon=None, item=None):
        cur = get_autostart_status()
        new_state = not cur
        set_autostart_status(new_state)
        try:
            cfg_path = os.path.join(get_config_dir(), "gateway_config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if not isinstance(cfg, dict):
                    cfg = {}
                cfg.setdefault("system", {})["autostart"] = new_state
                next_p = cfg_path + ".next"
                with open(next_p, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(next_p, cfg_path)
        except Exception:
            pass
        msg = "已开启开机自动静默常驻" if new_state else "已关闭开机自启动"
        show_windows_toast("数字蜂巢 · CellHive", msg)

    def _action_about(self, icon=None, item=None):
        launch_desktop_app_window(f"http://127.0.0.1:{self.web_port}/#about")

    def _action_reboot_board(self, icon=None, item=None):
        def _reboot():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect(("127.0.0.1", self.hub_port))
                req = json.dumps({"type": "req", "id": f"tray_rb_{int(time.time())}", "cmd": "reboot", "data": {"reason": "tray_menu"}}) + "\n"
                s.sendall(req.encode("utf-8"))
                s.close()
                show_windows_toast("数字蜂巢 · CellHive", "已向模组发送软复位重启指令")
            except Exception as e:
                show_windows_toast("数字蜂巢 · CellHive", f"发送重启指令失败: {e}")
        threading.Thread(target=_reboot, daemon=True).start()

    def _action_open_config_dir(self, icon=None, item=None):
        cfg_dir = get_config_dir()
        try:
            os.startfile(cfg_dir)
        except Exception:
            pass

    def _action_exit(self, icon=None, item=None):
        if self.tray_icon:
            self.tray_icon.stop()
        self.stop()
        sys.exit(0)

    def stop(self):
        self.running = False
        if self.web_server:
            try:
                self.web_server.stop()
            except Exception:
                pass
        if self.hub:
            try:
                self.hub.stop()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="数字蜂巢 · CellHive - 桌面私人蜂窝通信中枢")
    parser.add_argument("--com", default=SERIAL_PORT, help=f"下位机物理串口号 (默认 {SERIAL_PORT})")
    parser.add_argument("--baud", type=int, default=SERIAL_BAUD, help=f"串口波特率 (默认 {SERIAL_BAUD})")
    parser.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT, help=f"Web 看板端口 (默认 {DEFAULT_WEB_PORT})")
    parser.add_argument("--hub-port", type=int, default=HUB_PORT, help=f"Hub 中枢内部端口 (默认 {HUB_PORT})")
    parser.add_argument("--no-browser", "--no-window", dest="no_window", action="store_true", help="启动时不自动弹出原生应用窗口")
    parser.add_argument("--no-tray", action="store_true", help="禁用系统托盘，前台控制台模式运行")
    args = parser.parse_args()

    if not acquire_app_mutex():
        _log_debug("Mutex not acquired, exiting main")
        sys.exit(0)

    # 手动启动必定打开控制台窗口；开机自启动时因带 --no-browser 则静默常驻系统托盘
    should_open_browser = not args.no_window

    _log_debug(f"Mutex acquired, creating app... (open_browser={should_open_browser})")
    app = GatewayDesktopApp(
        com=args.com,
        baud=args.baud,
        hub_port=args.hub_port,
        web_port=args.web_port,
        open_browser=should_open_browser,
        use_tray=(not args.no_tray)
    )
    app.start()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        _log_debug(f"Unhandled exception in main: {e}\n{traceback.format_exc()}")
        raise
