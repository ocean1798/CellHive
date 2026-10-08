#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数字蜂巢 · CellHive (Air780 系列智能随身通信网关) - Windows 桌面独立原生客户端
整合通信中枢 Hub (17800)、Web 接口与 SSE 控制台 (17801)、Windows 原生 WebView2 单实例窗口与系统托盘。
支持单实例防多开、DWM 沉浸式暗黑标题栏、关机注销安全放行、关闭缩入托盘与绿色低功耗休眠联动。
"""

import os
import sys
import time
import socket
import json
import threading
import argparse
import shutil
import subprocess
import ctypes
import gateway_runtime as runtime

# 1. Windows Per-Monitor V2 高 DPI 感知声明 (必须在任何 GUI 库导入前执行)
def _enable_high_dpi_awareness():
    if sys.platform == "win32":
        try:
            # 优先启用 Windows 10 1703+ DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (-4)
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                # 降级尝试 Windows 8.1+ PROCESS_PER_MONITOR_DPI_AWARE (2)
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass

_enable_high_dpi_awareness()

# 2. 治理 WebView2 磁盘缓存配额 (硬顶死 32MB，禁用冗余着色器磁盘持久化，防止膨胀)
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
    "--disk-cache-size=33554432 --disable-gpu-shader-disk-cache"
)

# 显式构建验证入口 (PyInstaller 打包校验)
if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--verify-web":
    from gateway_package_check import verify_web
    verify_web(sys.argv[2])
    raise SystemExit(0)

# 确保在 Windows 无黑框 windowed 模式下标准流安全
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

def get_bundle_dir() -> str:
    """获取只读静态资源目录 (PyInstaller 解压根目录)"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

def get_config_dir() -> str:
    """获取可写配置持久化目录 (始终位于 exe 或主脚本同级)"""
    return runtime.directory("dataDir")

def get_webview_storage_path() -> str:
    """获取 WebView2 用户数据目录 (强制锁定在 LocalAppData，严禁进入网络漫游 Roaming)"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "CellHive", "webview_data")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path

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

def find_app_window_hwnd() -> int:
    """通过原生标题或进程 PID 稳健寻找数字蜂巢窗口句柄 (支持穿透隐藏状态)"""
    if sys.platform != "win32":
        return 0
    try:
        user32 = ctypes.windll.user32
        # 1. 优先按窗口标题精确查找 (FindWindowW 可查找当前进程的隐藏窗体)
        hwnd = user32.FindWindowW(None, "数字蜂巢 · CellHive")
        if hwnd:
            return hwnd

        # 2. 备选方案：枚举当前进程拥有的所有顶级窗口
        import win32gui, win32process
        cur_pid = os.getpid()
        found = []

        def _enum_proc(h, _):
            _, pid = win32process.GetWindowThreadProcessId(h)
            if pid == cur_pid:
                title = win32gui.GetWindowText(h)
                cls = win32gui.GetClassName(h)
                if "CellHive" in title or "WindowsForms" in cls:
                    found.append(h)

        win32gui.EnumWindows(_enum_proc, None)
        if found:
            return found[0]
    except Exception as e:
        _log_debug(f"find_app_window_hwnd failed: {e}")
    return 0

def force_bring_to_foreground(hwnd: int) -> bool:
    """穿透 Windows 防骚扰焦点锁 (LockSetForegroundWindow)，强力置顶激活窗口"""
    if not hwnd or sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        user32.ShowWindowAsync(hwnd, SW_RESTORE)

        fore_hwnd = user32.GetForegroundWindow()
        if fore_hwnd != hwnd and fore_hwnd != 0:
            fore_tid = user32.GetWindowThreadProcessId(fore_hwnd, None)
            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            if fore_tid != cur_tid and fore_tid != 0:
                user32.AttachThreadInput(cur_tid, fore_tid, True)
                user32.SetForegroundWindow(hwnd)
                user32.AttachThreadInput(cur_tid, fore_tid, False)
            else:
                user32.SetForegroundWindow(hwnd)
        else:
            user32.SetForegroundWindow(hwnd)
        return True
    except Exception as e:
        _log_debug(f"force_bring_to_foreground error: {e}")
        return False

def apply_immersive_dark_mode(hwnd: int):
    """为 Windows 10/11 窗口标题栏赋予原生沉浸式暗黑风格"""
    if sys.platform == "win32" and hwnd:
        try:
            # Win11 & Win10 20H1+ 属性为 20，Win10 1809~1909 为 19
            for attr in (20, 19):
                v = ctypes.c_int(1)
                res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)
                )
                if res == 0:
                    break
        except Exception as e:
            _log_debug(f"apply_immersive_dark_mode error: {e}")

def check_webview_available() -> bool:
    """深度探测系统是否具备真实可用的 Microsoft Edge WebView2 运行时"""
    try:
        import webview
        from webview.platforms.winforms import _is_chromium
        return bool(_is_chromium)
    except Exception as e:
        _log_debug(f"check_webview_available false: {e}")
        return False

def launch_fallback_browser(url: str):
    """当系统缺失 WebView2 时平滑回退至外部浏览器"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                subprocess.Popen([p, f"--app={url}", "--window-size=1280,820", "--app-id=Air780EPVGateway"])
                return
            except Exception:
                pass
    import webbrowser
    webbrowser.open(url)

def acquire_app_mutex() -> bool:
    """全局单实例运行锁；若已有实例运行，瞬间激活前台置顶已有窗口并退出第二实例"""
    global _app_mutex
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            _app_mutex = kernel32.CreateMutexW(None, True, "Air780EPV_Gateway_Desktop_App_Mutex")
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                _log_debug("Mutex exists, bringing existing window to foreground...")
                hwnd = find_app_window_hwnd()
                if hwnd:
                    force_bring_to_foreground(hwnd)
                else:
                    # 若窗口隐藏且未通过标题找到，触发本地安全唤醒通知
                    try:
                        import urllib.request
                        urllib.request.urlopen(f"http://127.0.0.1:{DEFAULT_WEB_PORT}/api/app/show", timeout=0.8)
                    except Exception:
                        pass
                return False
        except Exception as e:
            _log_debug(f"acquire_app_mutex error: {e}")
    return True

def create_tray_icon_image():
    """获取托盘图标：优先加载内置 app.ico，若无则动态绘制高对比度天线图标"""
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
        d.ellipse((2, 2, 62, 62), fill=(15, 23, 42, 255), outline=(56, 189, 248, 255), width=2)
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
        self.window = None
        self.hwnd = 0
        self.running = False
        self.is_quitting = False
        self.has_webview = False

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
        threading.Thread(target=_run_hub, daemon=True, name="HubThread").start()

        # 等待 Hub 启动
        time.sleep(0.5)

        # 2. 启动 Web 控制台 (17801 端口)
        self.web_server = WebServer(host="0.0.0.0", port=self.web_port, hub_host="127.0.0.1", hub_port=self.hub_port)
        def _run_web():
            try:
                _log_debug("Starting WebServer...")
                self.web_server.start()
            except Exception as e:
                _log_debug(f"[Web Thread Error] {e}")
        threading.Thread(target=_run_web, daemon=True, name="WebThread").start()

        # 等待 WebServer 端口连通探针 (最长 3.0s，避免首屏 ERR_CONNECTION_REFUSED)
        self._wait_for_webserver_ready(timeout=3.0)

        # 3. 挂接 Windows 系统关机与注销放行事件 (WM_QUERYENDSESSION，严禁拦截关机)
        self._setup_session_ending_listener()

        # 4. 探测系统 WebView2 深度支持
        self.has_webview = check_webview_available()
        _log_debug(f"has_webview = {self.has_webview}")

        # 5. 启动系统托盘 (必须在独立线程运行，将主线程留给 WinForms STA 消息循环)
        if self.use_tray:
            self._start_tray_thread()

        # 6. 弹出启动就绪 Toast 提示
        show_windows_toast("数字蜂巢 · CellHive", "蜂巢中枢已在后台就绪，短信验证码将毫秒级存入剪贴板。")

        # 7. 启动原生单实例窗口或运行命令行循环
        if self.has_webview:
            self._run_webview_gui()
        else:
            _log_debug("Fallback: running browser mode due to missing WebView2")
            if self.open_browser:
                launch_fallback_browser(f"http://127.0.0.1:{self.web_port}")
            self._run_headless_loop()

    def _wait_for_webserver_ready(self, timeout=3.0):
        start_t = time.time()
        while time.time() - start_t < timeout:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    if s.connect_ex(("127.0.0.1", self.web_port)) == 0:
                        _log_debug("WebServer port verified ready.")
                        return True
            except Exception:
                pass
            time.sleep(0.08)
        _log_debug("WebServer wait timeout reached, continuing...")
        return False

    def _setup_session_ending_listener(self):
        """挂接系统会话终结事件，关机/注销时坚决放行退出，绝不阻拦系统关机"""
        if sys.platform == "win32":
            try:
                import clr
                from Microsoft.Win32 import SystemEvents
                def _on_session_ending(sender, e):
                    _log_debug(f"Session ending detected: reason={getattr(e, 'Reason', 'unknown')}. Allowing shutdown.")
                    self.is_quitting = True
                    self.stop()
                SystemEvents.SessionEnding += _on_session_ending
                _log_debug("SystemEvents.SessionEnding successfully registered via CLR.")
            except Exception as e:
                _log_debug(f"SystemEvents.SessionEnding setup failed: {e}")

    def _start_tray_thread(self):
        try:
            import pystray
            icon_img = create_tray_icon_image()
            if not icon_img:
                _log_debug("Failed to create tray icon image")
                return

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
            # 使用非阻塞的 run_detached 在专用后台线程运转托盘消息泵
            self.tray_icon.run_detached()
            _log_debug("Tray icon started in detached background thread.")
        except Exception as e:
            _log_debug(f"Tray startup failed: {e}")

    def _run_webview_gui(self):
        import webview
        storage_path = get_webview_storage_path()
        _log_debug(f"Creating native window with storage_path={storage_path}")

        # 启动模式判定：开机自启或携带 --no-browser 时以 hidden 状态启动
        is_hidden_startup = not self.open_browser

        self.window = webview.create_window(
            title="数字蜂巢 · CellHive",
            url=f"http://127.0.0.1:{self.web_port}",
            width=1280,
            height=820,
            min_size=(800, 600),
            hidden=is_hidden_startup,
            background_color="#0f172a",  # 消除初次渲染的白色爆闪
            text_select=False,
            zoomable=False
        )

        self.window.events.shown += self._on_window_shown
        self.window.events.closing += self._on_window_closing

        # 在主线程独占运行 WinForms STA 消息泵
        _log_debug("Starting webview STA event loop on main thread...")
        try:
            # 禁用开发者工具 (生产环境纯净)，开启沉浸式运行
            webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
            webview.start(storage_path=storage_path)
        except Exception as e:
            _log_debug(f"webview.start encountered: {e}")
        finally:
            _log_debug("webview.start loop ended, cleaning up...")
            self.stop()
            time.sleep(0.2)
            sys.exit(0)

    def _on_window_shown(self):
        """窗口首次或恢复呈现时执行"""
        if not self.hwnd:
            self.hwnd = find_app_window_hwnd()
            if self.hwnd:
                apply_immersive_dark_mode(self.hwnd)
                _log_debug(f"Window HWND found: {self.hwnd}, dark mode applied.")

    def _on_window_closing(self):
        """窗口关闭拦截回调：托盘常驻模式下仅隐藏并挂起，防止随手误杀后台中枢"""
        _log_debug(f"_on_window_closing called: is_quitting={self.is_quitting}, use_tray={self.use_tray}")
        if self.is_quitting or not self.use_tray:
            # 明确退出或调试无托盘模式下允许真实退出
            return True
        else:
            # 托盘模式下拦截销毁，执行平滑隐藏与低功耗休眠
            if self.window:
                try:
                    self.window.hide()
                    self._suspend_webview()
                    self._show_first_minimize_toast()
                except Exception as e:
                    _log_debug(f"Error hiding window: {e}")
            return False  # 取消窗口关闭

    def _suspend_webview(self):
        """窗口缩入托盘时，挂起底层 WebView2 渲染引擎，释放 CPU 与 Working Set 内存"""
        try:
            core = getattr(self.window, "native", None)
            browser = getattr(core, "browser", None)
            wv = getattr(browser, "webview", None)
            core_wv = getattr(wv, "CoreWebView2", None)
            if core_wv and hasattr(core_wv, "TrySuspendAsync") and not getattr(core_wv, "IsSuspended", False):
                core_wv.TrySuspendAsync()
                _log_debug("CoreWebView2 suspended for green daemon mode.")
        except Exception:
            pass

    def _resume_webview(self):
        """窗口唤出时恢复渲染引擎"""
        try:
            core = getattr(self.window, "native", None)
            browser = getattr(core, "browser", None)
            wv = getattr(browser, "webview", None)
            core_wv = getattr(wv, "CoreWebView2", None)
            if core_wv and hasattr(core_wv, "Resume") and getattr(core_wv, "IsSuspended", False):
                core_wv.Resume()
                _log_debug("CoreWebView2 resumed.")
        except Exception:
            pass

    def _show_first_minimize_toast(self):
        """首次缩入托盘提示，打消用户恐慌"""
        try:
            cfg_path = os.path.join(get_config_dir(), "gateway_config.json")
            cfg = {}
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            if not isinstance(cfg, dict):
                cfg = {}
            if not cfg.get("desktop", {}).get("tray_tip_shown"):
                cfg.setdefault("desktop", {})["tray_tip_shown"] = True
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                show_windows_toast("数字蜂巢 · CellHive", "已转入后台静默常驻，短信与通信持续守护中。\n可通过任务栏托盘图标随时唤出。")
        except Exception:
            pass

    def show_window(self):
        """托盘点击或第二实例唤起时的统一幂等置顶动作（纯异步原生 Win32 消息，杜绝跨线程死锁）"""
        if not self.has_webview or not self.window:
            launch_fallback_browser(f"http://127.0.0.1:{self.web_port}")
            return

        try:
            if not self.hwnd:
                self.hwnd = find_app_window_hwnd()

            if self.hwnd:
                user32 = ctypes.windll.user32
                user32.ShowWindowAsync(self.hwnd, 9)  # SW_RESTORE = 9
                apply_immersive_dark_mode(self.hwnd)
                force_bring_to_foreground(self.hwnd)
            else:
                # 窗口尚在创建时兜底：启动后台微任务延迟 100ms 重试探测与投递，严禁在当前线程阻塞
                def _async_retry():
                    time.sleep(0.1)
                    h = find_app_window_hwnd()
                    if h:
                        self.hwnd = h
                        ctypes.windll.user32.ShowWindowAsync(h, 9)
                        apply_immersive_dark_mode(h)
                        force_bring_to_foreground(h)
                threading.Thread(target=_async_retry, daemon=True).start()
        except Exception as e:
            _log_debug(f"show_window error: {e}")

    def _run_headless_loop(self):
        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
            sys.exit(0)

    def _action_open_app(self, icon=None, item=None):
        self.show_window()

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
        """关于菜单回调：纯异步激活主界面，杜绝跨线程阻塞"""
        self.show_window()

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
        try:
            os.startfile(get_config_dir())
        except Exception:
            pass

    def _action_exit(self, icon=None, item=None):
        """托盘彻底退出：标准优雅拆卸流程，严禁使用 os._exit(0)，确保 PyInstaller 临时目录回收"""
        _log_debug("_action_exit called from tray menu")
        self.is_quitting = True

        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass

        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass

        self.stop()
        # 让渡 CPU 时间完成串口与套接字 I/O 刷盘
        time.sleep(0.3)
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
    parser.add_argument("--no-browser", "--no-window", dest="no_window", action="store_true", help="启动时不自动弹出原生应用窗口 (静默常驻托盘)")
    parser.add_argument("--no-tray", action="store_true", help="禁用系统托盘，前台控制台调试模式运行")
    args = parser.parse_args()

    # 单实例互斥检查
    if not acquire_app_mutex():
        _log_debug("Mutex not acquired (second instance), exiting main.")
        sys.exit(0)

    should_open_window = not args.no_window
    _log_debug(f"Starting GatewayDesktopApp... open_window={should_open_window}, use_tray={not args.no_tray}")

    app = GatewayDesktopApp(
        com=args.com,
        baud=args.baud,
        hub_port=args.hub_port,
        web_port=args.web_port,
        open_browser=should_open_window,
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
