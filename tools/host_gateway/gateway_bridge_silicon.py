#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780EPV 网关 -> 《硅基涌现》数据桥接服务 (Gateway to Silicon Emergence Bridge)
职责：
1. 复用高可靠网关底层驱动 (GatewayDriver)，保持与 Hub (127.0.0.1:17800) 的通信；
2. 采集网关硬件指标（CSQ、温度、供电、脱机黑匣子存量与随身上网状态）；
3. 全双工长连接监听短信广播 (sms_rx) 与提码引擎；
4. 将最新全景快照格式化写入《硅基涌现》可消费的本地 snapshot 缓存文件，供插件 Surface 渲染。
"""

import os
import sys
import time
import json
from pathlib import Path

# 确保 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TOOLS_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = TOOLS_DIR.parent.parent.resolve()
MCP_SERVER_DIR = TOOLS_DIR.parent / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))

from gateway_driver import GatewayDriver

RUNTIME_DIR = PROJECT_ROOT / ".runtime"
SNAPSHOT_FILE = RUNTIME_DIR / "silicon_gateway_snapshot.json"

class SiliconGatewayBridge:
    def __init__(self):
        self.driver = GatewayDriver()
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    def generate_snapshot(self) -> dict:
        """从驱动层拉取并组装符合《硅基涌现》插件 Surface 消费的数据快照"""
        raw_status = self.driver.get_status()
        hist = self.driver.get_history(limit=1)
        latest_item = None
        if hist.get("items"):
            latest_item = hist["items"][0]

        hw_model = raw_status.get("bsp", "Air780EPV")
        temp = raw_status.get("temp", "--")
        vbat = raw_status.get("vbat", "--")
        csq = raw_status.get("csq", 0)
        rsrp = raw_status.get("rsrp", 0)
        rndis = raw_status.get("rndis", False)
        cell_data = raw_status.get("cellular_data", False)
        uptime = raw_status.get("uptime_seconds", 0)
        blackbox_cnt = raw_status.get("blackbox_count", 0)

        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        seconds = uptime % 60
        uptime_str = f"{hours}时 {minutes}分 {seconds}秒"

        rndis_str = "🟢 已开启 (USB网卡在线)" if rndis else "🔴 已关闭"
        data_str = "🟢 已开启 (移动数据)" if cell_data else "⚪ 已关闭 (移动数据)"

        latest_sms_dict = None
        if latest_item:
            ts = latest_item.get("time", time.time())
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            otp_val = latest_item.get("otp", "")
            if not otp_val:
                otp_val = self.driver._extract_otp_fallback(latest_item.get("content", ""), latest_item.get("sender", ""))
            latest_sms_dict = {
                "sender": latest_item.get("sender", "未知号码"),
                "content": latest_item.get("content", ""),
                "otp": otp_val,
                "time": time_str
            }

        snapshot = {
            "hardware": {
                "model": hw_model,
                "temperature_celsius": str(temp),
                "voltage_vbat": f"{vbat} V",
                "lua_memory_used_kb": f"{raw_status.get('lua_mem_kb', '--')} KB / 128 KB"
            },
            "cellular_network": {
                "network_ready": raw_status.get("net_ready", True),
                "csq_signal": csq,
                "rsrp_dbm": f"{rsrp} dBm"
            },
            "system_status": {
                "rndis_internet_sharing": rndis_str,
                "cellular_data_mode": data_str,
                "blackbox_sms_count": blackbox_cnt,
                "continuous_uptime": uptime_str
            },
            "latest_sms": latest_sms_dict,
            "last_updated": int(time.time())
        }
        return snapshot

    def save_snapshot(self, snapshot: dict):
        try:
            temp_file = SNAPSHOT_FILE.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            temp_file.replace(SNAPSHOT_FILE)
            print(f"💾 快照已同步写入: {SNAPSHOT_FILE} (更新于 {time.strftime('%H:%M:%S')})")
        except Exception as e:
            print(f"⚠️ 快照写入失败: {e}")

    def run_once(self) -> bool:
        self.driver.start()
        print("🔄 正在从 Air780EPV 通信中枢采集网关全景数据...")
        snapshot = self.generate_snapshot()
        self.save_snapshot(snapshot)
        print("📊 采集完成！当前快照内容:")
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return True

    def run_daemon(self):
        self.driver.start()
        print("🎧 硅基涌现网关桥接服务启动 (长连接常驻守护)...")
        last_save = 0
        while True:
            try:
                now = time.time()
                # 每 10 秒主动轮询刷新一次硬件看板与最新短信
                if now - last_save >= 10:
                    snapshot = self.generate_snapshot()
                    self.save_snapshot(snapshot)
                    last_save = now
                time.sleep(1)
            except KeyboardInterrupt:
                print("\n🛑 桥接服务已退出")
                break
            except Exception as e:
                print(f"⚠️ 桥接异常: {e}")
                time.sleep(2)

def main():
    bridge = SiliconGatewayBridge()
    if "--once" in sys.argv:
        bridge.run_once()
    else:
        bridge.run_daemon()

if __name__ == "__main__":
    main()
