#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780EPV 智能随身通信网关 - PC 上位机控制端 (P0~P4 原型)
基于 USB CDC 虚拟串口 (COM8 / VUART_0) 与板端进行双向 NDJSON 交互
支持断线自愈重连、短信代发、脱机黑匣子查看与清空、随身上网受控启停、来电拦截监控与心跳看板
"""

import sys
import os
import time
import json
import socket
import threading
import subprocess

# 确保 Windows 终端支持 ANSI 颜色与 UTF-8 编码
if os.name == "nt":
    os.system("")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")

HUB_HOST = "127.0.0.1"
HUB_PORT = 17800

class SmartGatewayClient:
    def __init__(self, host=HUB_HOST, port=HUB_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.sock_lock = threading.Lock()
        self.running = False
        self.rx_thread = None
        self.connected = False

    def _auto_spawn_hub(self):
        hub_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway_hub.py")
        if not os.path.exists(hub_path):
            return
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
        except Exception:
            pass

    def connect(self):
        for attempt in range(1, 4):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.settimeout(2.0)
                s.connect((self.host, self.port))
                s.settimeout(None)
                with self.sock_lock:
                    self.sock = s
                self.connected = True
                print(f"\033[32m[+] 成功接入本地网关共享中枢: {self.host}:{self.port}\033[0m")
                return True
            except (ConnectionRefusedError, OSError):
                if attempt == 1:
                    print("\033[33m[*] 共享中枢未运行，正在后台透明自拉起 gateway_hub...\033[0m")
                    self._auto_spawn_hub()
                time.sleep(0.5)

        self.connected = False
        return False

    def start(self):
        self.running = True
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
        self.connected = False
        print("[*] 网关客户端已断开")

    def send_cmd(self, cmd_name, params=None):
        req_id = f"cli_{int(time.time()*1000)}"
        packet = {
            "type": "cmd",
            "id": req_id,
            "cmd": cmd_name,
            "params": params or {}
        }
        return self.send_raw_json(packet), req_id

    def send_raw_json(self, data_dict):
        with self.sock_lock:
            if not self.connected or not self.sock:
                print("\033[31m[-] 网关中枢未连接，无法发送指令\033[0m")
                return False
            try:
                line = json.dumps(data_dict, ensure_ascii=False) + "\n"
                self.sock.sendall(line.encode("utf-8"))
                return True
            except Exception as e:
                print(f"\033[31m[-] 写入失败: {e}\033[0m")
                return False

    def _rx_loop(self):
        """后台接收线程，具备断线自愈重连特性"""
        buffer = ""
        while self.running:
            if not self.connected:
                # 尝试重连
                if self.connect():
                    buffer = ""
                else:
                    time.sleep(1.5)
                    continue

            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    print("\033[33m[!] 与中枢连接断开，正在自动自愈重连...\033[0m")
                    self.connected = False
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
                    if not line:
                        continue
                    self._handle_frame(line)

            except Exception as e:
                if not self.running:
                    break
                print(f"\033[33m[!] 接收异常 ({e})，正在重新连接...\033[0m")
                self.connected = False
                with self.sock_lock:
                    if self.sock:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                        self.sock = None
                time.sleep(1.0)

    def _handle_frame(self, raw_line):
        try:
            obj = json.loads(raw_line)
        except Exception:
            # 可能是板端普通调试日志
            print(f"\033[90m[RAW] {raw_line}\033[0m")
            return

        frame_type = obj.get("type")

        if frame_type == "event":
            event = obj.get("event")
            data = obj.get("data", {})

            if event == "gateway_ready":
                print(f"\n\033[32;1m================ 网关已就绪上线 ================\033[0m")
                print(f"  设备型号: {data.get('bsp')} | 本机号码: {data.get('number')}")
                print(f"  信号质量: CSQ {data.get('csq')} | 芯片温度: {data.get('temp')} ℃ | 电压: {data.get('vbat')} V")
                print(f"  随身上网: {'已启用' if data.get('rndis') else '默认关闭'}")
                print(f"\033[32;1m===============================================\033[0m\nGateway> ", end="", flush=True)

            elif event == "sms_rx":
                sender = data.get("from")
                content = data.get("content")
                code = data.get("code")
                print(f"\n\033[35;1m📩 [收到新短信] 来自: {sender}\033[0m")
                print(f"  正文: {content}")
                if code:
                    print(f"  \033[33;1m🔑 提取验证码: 【{code}】\033[0m")
                print("Gateway> ", end="", flush=True)

            elif event == "call_rx":
                sender = data.get("from")
                action = data.get("action")
                print(f"\n\033[31;1m📞 [来电拦截] 号码: {sender} -> 动作: {action} (已自动拒接)\033[0m\nGateway> ", end="", flush=True)

            elif event == "status":
                csq = data.get("csq")
                rsrp = data.get("rsrp")
                temp = data.get("temp")
                vbat = data.get("vbat")
                lua_mem = data.get("lua_mem_kb")
                rndis = "开" if data.get("rndis") else "关 (安全)"
                # 单行轻量刷新
                # print(f"\033[36m[心跳] CSQ: {csq} ({rsrp}dBm) | 芯片温度: {temp}℃ | 电压: {vbat}V | 堆内存: {lua_mem}KB | 随身上网: {rndis}\033[0m")

            elif event == "gateway_rebooting":
                reason = data.get("reason", "unknown")
                delay = data.get("delay_ms", 1000)
                print(f"\n\033[33;1m⚠️  [系统提示] 网关正在重启 (原因: {reason})... 正在等待自动重连...\033[0m\nGateway> ", end="", flush=True)

            elif event == "sms_tx_report":
                success = data.get("success")
                tag = "\033[32m成功\033[0m" if success else "\033[31m失败\033[0m"
                print(f"\n[*] 短信投递报告: 基站反馈 {tag}\nGateway> ", end="", flush=True)

        elif frame_type == "res":
            req_id = obj.get("id")
            code = obj.get("code")
            msg = obj.get("msg")
            data = obj.get("data", {})

            if msg == "STATUS_OK":
                rndis_desc = "\033[32;1m🟢 4G随身上网已开启\033[0m" if data.get('rndis') else "\033[90m⚪ 默认关闭 \033[0m"
                data_desc = "\033[32;1m🟢 已开启 (允许板端发HTTP)\033[0m" if data.get('cellular_data') else "\033[33;1m⚪ 已关闭 (移动数据)\033[0m"
                uptime_sec = data.get('uptime_seconds', 0)
                m, s = divmod(uptime_sec, 60)
                h, m = divmod(m, 60)
                d, h = divmod(h, 24)
                uptime_str = f"{d}天 {h}时 {m}分 {s}秒" if d > 0 else f"{h}时 {m}分 {s}秒"
                daily_h = data.get('daily_reboot_hour', -1)
                rb_desc = f"每天 {daily_h:02d}:00" if (daily_h is not None and daily_h >= 0) else "未开启 (默认关闭)"

                print(f"\n\033[32;1m---------- 智能通信网关运行状态 ----------\033[0m")
                print(f"  硬件模组: {data.get('bsp')} | 蜂窝驻网: {data.get('net_ready')}")
                print(f"  蜂窝信号: CSQ {data.get('csq')} (RSRP: {data.get('rsrp')} dBm)")
                print(f"  工作温度: {data.get('temp')} ℃ | 供电电压: {data.get('vbat')} V")
                print(f"  脱机黑匣子: 已持久化 {data.get('blackbox_count', 0)} 条短信")
                print(f"  PC共享上网: {rndis_desc}")
                print(f"  板载蜂窝数据: {data_desc}")
                print(f"  连续运行: {uptime_str} | 自动重启: {rb_desc}")
                print(f"  常驻堆内存: {data.get('lua_mem_kb')} KB / 128 KB (健康极低消耗)")
                print(f"\033[32;1m-----------------------------------------\033[0m\nGateway> ", end="", flush=True)

            elif msg == "UPTIME_OK":
                uptime_sec = data.get('uptime_seconds', 0)
                m, s = divmod(uptime_sec, 60)
                h, m = divmod(m, 60)
                d, h = divmod(h, 24)
                uptime_str = f"{d}天 {h}时 {m}分 {s}秒" if d > 0 else f"{h}时 {m}分 {s}秒"
                daily_h = data.get('daily_reboot_hour', -1)
                cur_time = data.get('current_time', '')
                next_desc = data.get('next_reboot_desc', '未开启')
                next_sec = data.get('next_reboot_seconds', -1)
                cd_str = f"，距下次重启还剩 {next_sec // 3600}时 {(next_sec % 3600)//60}分" if next_sec > 0 else ""

                if daily_h is not None and daily_h >= 0:
                    rb_txt = f"每天 {daily_h:02d}:00 (预计 {next_desc}{cd_str})"
                else:
                    rb_txt = "未开启 (默认关闭)"

                print(f"\n\033[36;1m[系统运行时间] 连续运行: {uptime_str} | 基站时钟: {cur_time} | 自动重启: {rb_txt}\033[0m\nGateway> ", end="", flush=True)

            elif msg == "REBOOT_POLICY_UPDATED":
                hour = data.get('daily_reboot_hour', -1)
                if hour is not None and hour >= 0:
                    desc = data.get('next_reboot_desc', f"每天 {hour:02d}:00")
                    print(f"\n\033[32m[+] 自动重启已设置: 每天 {hour:02d}:00 自动重启 (下次: {desc})\033[0m\nGateway> ", end="", flush=True)
                else:
                    print(f"\n\033[32m[+] 自动重启已关闭 (默认随身防打扰模式)\033[0m\nGateway> ", end="", flush=True)

            elif msg == "REBOOT_COMMENCED":
                print("\n\033[33m[!] 设备正在重启，请稍候自动重连...\033[0m\nGateway> ", end="", flush=True)

            elif msg == "HISTORY_OK":
                items = data.get("items", [])
                total = data.get("total", 0)
                print(f"\n\033[34;1m========== 板载脱机黑匣子短信存档 (共 {total} 条，展示最新 {len(items)} 条) ==========\033[0m")
                if not items:
                    print("  [暂无归档短信]")
                else:
                    for i, it in enumerate(items, 1):
                        ts_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(it.get('time', 0)))
                        otp_part = f" | 验证码: 【\033[33;1m{it.get('otp')}\033[0m\033[34;1m】" if it.get('otp') else ""
                        print(f"  [{i}] [{ts_str}] 来自: {it.get('sender')}{otp_part}")
                        print(f"      内容: {it.get('content')}")
                print(f"\033[34;1m=======================================================================\033[0m\nGateway> ", end="", flush=True)

            elif msg == "HISTORY_CLEARED":
                print("\n\033[32m[+] 板载脱机黑匣子已彻底清空\033[0m\nGateway> ", end="", flush=True)

            elif msg == "RNDIS_SWITCHING":
                state_str = "开启随身上网" if data.get("enable") else "关闭随身上网"
                print(f"\n\033[33;1m[!] 指令已确认: 正在{state_str}... 板卡将执行 USB 热复位，正在守候自动重连...\033[0m\nGateway> ", end="", flush=True)

            elif msg == "CELLULAR_DATA_UPDATED":
                st = data.get("cellular_data")
                st_str = "\033[32;1m开启板载数据 (走4G发HTTP)\033[0m" if st else "\033[33;1m关闭移动数据\033[0m"
                print(f"\n\033[32;1m[+] 板载数据通信切换成功: {st_str}\033[0m\nGateway> ", end="", flush=True)

            elif msg == "QUEUED_TO_BASE_STATION":
                print(f"\n\033[32m[+] 短信已提交至 4G 基站发送队列，等待运营商网络回执...\033[0m\nGateway> ", end="", flush=True)

            else:
                print(f"[*] 指令回执 [{req_id}]: code={code}, msg={msg}, data={data}\nGateway> ", end="", flush=True)


def print_menu():
    print("""
\033[36;1m======================================================================
   Air780EPV 智能随身通信网关控制中枢 (Smart Gateway CLI)
======================================================================\033[0m
\033[33;1m常用指令 [支持输入数字编号直接回车执行]:\033[0m
  \033[32;1m[1]\033[0m status                   : 实时硬件状态看板 (信号/温度/电压/黑匣子/运行时间)
  \033[32;1m[2]\033[0m history [数量]           : 查阅板载 128KB LittleFS 脱机黑匣子 (直接回车默认 20 条)
  \033[32;1m[3]\033[0m rndis on                 : 一键开启 4G 随身上网 (激活虚拟网卡)
  \033[32;1m[4]\033[0m rndis off                : 一键关闭 4G 随身上网
  \033[32;1m[5]\033[0m data on                  : 开启板载 4G 蜂窝数据 (允许板端发HTTP)
  \033[32;1m[6]\033[0m data off                 : 关闭移动数据
  \033[32;1m[7]\033[0m uptime                   : 查看连续运行时间与下次自动重启倒计时
  \033[32;1m[8]\033[0m send <号码> <内容>       : 驱动 4G 射频代发短信 (如输入 8 弹出引导)
  \033[32;1m[9]\033[0m reboot                   : 立即重启设备
  \033[32;1m[p]\033[0m reboot_policy [时间]     : 设置每天自动重启时间点 (如输入 p 弹出引导，默认关闭)
  \033[32;1m[c]\033[0m clear_history            : 清空板载黑匣子短信记录
  \033[32;1m[0]\033[0m clear                    : 控制台清屏
  \033[32;1m[q]\033[0m exit                     : 退出控制台
\033[36;1m======================================================================\033[0m""")


def interactive_cli():
    print_menu()
    client = SmartGatewayClient()
    client.start()

    time.sleep(0.5)

    try:
        while True:
            cmd_line = input("Gateway> ").strip()
            if not cmd_line:
                continue

            parts = cmd_line.split(maxsplit=2)
            action = parts[0].lower()

            if action in ("q", "exit", "quit"):
                break
            elif action in ("help", "h", "?"):
                print_menu()

            elif action in ("0", "clear"):
                os.system("cls" if os.name == "nt" else "clear")

            elif action in ("1", "status"):
                client.send_cmd("get_status")

            elif action in ("2", "history"):
                limit = 20
                if len(parts) > 1 and parts[1].isdigit():
                    limit = int(parts[1])
                client.send_cmd("get_history", {"limit": limit})

            elif action == "3" or (action == "rndis" and len(parts) >= 2 and parts[1].lower() == "on"):
                client.send_cmd("set_rndis", {"enable": True})

            elif action == "4" or (action == "rndis" and len(parts) >= 2 and parts[1].lower() == "off"):
                client.send_cmd("set_rndis", {"enable": False})

            elif action == "5" or (action == "data" and len(parts) >= 2 and parts[1].lower() == "on"):
                client.send_cmd("set_cellular_data", {"enable": True})

            elif action == "6" or (action == "data" and len(parts) >= 2 and parts[1].lower() == "off"):
                client.send_cmd("set_cellular_data", {"enable": False})

            elif action in ("7", "uptime"):
                client.send_cmd("get_uptime")

            elif action in ("8", "send"):
                to_num, text = None, None
                if len(parts) >= 3:
                    to_num = parts[1]
                    text = parts[2]
                else:
                    try:
                        to_num = input("  >> 请输入目标手机号: ").strip()
                        if not to_num:
                            print("  [!] 已取消发送")
                            continue
                        text = input("  >> 请输入短信内容: ").strip()
                        if not text:
                            print("  [!] 已取消发送")
                            continue
                    except KeyboardInterrupt:
                        print("\n  [!] 已取消输入")
                        continue

                print(f"[*] 正在向 {to_num} 发送短信: '{text}'...")
                client.send_cmd("send_sms", {"phone": to_num, "text": text})

            elif action in ("9", "reboot", "restart"):
                print("[*] 正在向网关发送重启指令...")
                client.send_cmd("reboot", {"reason": "user_cli_reboot"})

            elif action in ("p", "reboot_policy", "autoreboot", "auto_reboot"):
                val = None
                if len(parts) >= 2:
                    val = parts[1].strip()
                else:
                    try:
                        print("  [设置] 每天固定几点自动重启设备 (默认关闭)")
                        val = input("  >> 请输入每天重启的小时 (0~23，例如 3 表示每天凌晨3:00；输入 off 或 -1 关闭): ").strip()
                    except KeyboardInterrupt:
                        print("\n  [!] 已取消输入")
                        continue

                if not val:
                    print("  [!] 输入为空，已取消")
                    continue

                hour = -1
                if val.lower() in ("off", "-1", "none", "close", "n", "false"):
                    hour = -1
                elif val.isdigit() and 0 <= int(val) <= 23:
                    hour = int(val)
                else:
                    print(f"  [!] 输入无效: '{val}'。请输入 0~23 的整点小时数，或输入 off 关闭")
                    continue

                client.send_cmd("set_reboot_policy", {"hour": hour})

            elif action in ("c", "clear_history"):
                client.send_cmd("clear_history")

            else:
                print(f"未知指令: '{cmd_line}', 输入数字 1~9 或 help 查看命令帮助")
    except KeyboardInterrupt:
        pass
    finally:
        client.stop()

if __name__ == "__main__":
    interactive_cli()
