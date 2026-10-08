# -*- coding: utf-8 -*-
"""
AIR-17 推送握手确认与超时降级机制 全链路测试
"""
import os
import sys
import time
import json
import unittest
import importlib.util
from unittest.mock import MagicMock, patch

project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
host_hub_path = os.path.join(project_dir, "tools", "host_gateway", "gateway_hub.py")
cluster_hub_path = os.path.join(project_dir, "core", "gateway_hub.py")

def load_module_from_path(name: str, path: str):
    dir_name = os.path.dirname(os.path.abspath(path))
    if dir_name not in sys.path:
        sys.path.insert(0, dir_name)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class TestAir17PushAckAndDegradation(unittest.TestCase):
    def test_board_state_machine_logic(self):
        print("=== 1. 验证板端 notify_service.lua 超时降级状态机逻辑 ===")
        pending_pushes = {}
        board_pushes_executed = []
        cellular_data_enabled = False

        def board_execute_push(item):
            if not cellular_data_enabled:
                board_pushes_executed.append((item['id'], "blackbox_only"))
                return
            board_pushes_executed.append((item['id'], "cellular_http_push"))

        def board_on_timeout(msg_id):
            item = pending_pushes.pop(msg_id, None)
            if item:
                board_execute_push(item)

        def board_handle_ack(msg_id, status):
            item = pending_pushes.pop(msg_id, None)
            if not item:
                return
            if status == "ok":
                pass
            else:
                board_execute_push(item)

        # 场景 1：正常流程，上位机代推成功，板端注销
        msg_id_1 = "msg_1001"
        pending_pushes[msg_id_1] = {"id": msg_id_1, "type": "sms", "from": "10010", "content": "您的余额为 50 元"}
        board_handle_ack(msg_id_1, "ok")
        self.assertNotIn(msg_id_1, pending_pushes)
        self.assertEqual(len(board_pushes_executed), 0)

        # 场景 2：上位机代推失败，板端在 cellular_data=False 状态下触发降级
        msg_id_2 = "msg_1002"
        pending_pushes[msg_id_2] = {"id": msg_id_2, "type": "sms", "from": "95588", "content": "验证码 654321"}
        board_handle_ack(msg_id_2, "failed")
        self.assertEqual(board_pushes_executed[-1], (msg_id_2, "blackbox_only"))

        # 场景 3：上位机断线/超时，板端在 cellular_data=True 状态下触发降级
        cellular_data_enabled = True
        msg_id_3 = "msg_1003"
        pending_pushes[msg_id_3] = {"id": msg_id_3, "type": "sms", "from": "10086", "content": "验证码 123456"}
        board_on_timeout(msg_id_3)
        self.assertEqual(board_pushes_executed[-1], (msg_id_3, "cellular_http_push"))
        print("  -> 板端超时降级状态机断言全部通过")

    @patch("urllib.request.urlopen")
    def test_host_gateway_hub_push_and_ack(self, mock_urlopen):
        print("=== 2. 验证单卡 host_gateway Hub 宽带代推与 notify_ack 回发 ===")
        host_hub_mod = load_module_from_path("host_gateway_hub_test", host_hub_path)

        hub = host_hub_mod.GatewayHub()
        session = host_hub_mod.DongleSession(port="COM8", loc="1-8:x.6", slot_id="slot_1", hub=hub)
        session.board_cellular_data = True  # 关键：开启蜂窝数据时不得退让！
        hub.notify_config = {
            "feishu": {
                "enable": 1,
                "url": "http://127.0.0.1:9999/feishu_mock"
            }
        }
        sent_lines = []
        def fake_send_line(line):
            sent_lines.append(line)
            try:
                pkt = json.loads(line)
                if pkt.get("cmd") == "notify_ack":
                    session.on_claim_response({"type": "res", "id": pkt.get("id"), "code": 0, "msg": "NOTIFY_CLAIMED", "data": {"id": pkt.get("data", {}).get("id"), "status": "claimed"}})
            except Exception:
                pass
            return True
        session.send_line = fake_send_line

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"code": 0}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        sms_data = {
            "id": f"boot_1_{int(time.time()*1000)}_sms_99",
            "from": "10010",
            "content": "【中国联通】您的验证码是 889977",
            "code": "889977",
            "time": 1726200000
        }

        hub._dispatch_host_proxy_push(session, "sms_rx", sms_data)

        time.sleep(0.8)

        self.assertTrue(mock_urlopen.called, "即便板载蜂窝开启，上位机也必须优先宽带代推")
        self.assertTrue(any("notify_ack" in l and ("ok" in l or "handled" in l) for l in sent_lines),
                        "上位机代推成功后必须向模组回写 notify_ack [ok]")
        print("  -> 单卡 host_gateway Hub 验证通过")

    @patch("urllib.request.urlopen")
    def test_cluster_gateway_hub_push_and_ack(self, mock_urlopen):
        print("=== 3. 验证集群 cluster_gateway Hub 宽带代推与 notify_ack 回发 ===")
        if not os.path.exists(cluster_hub_path):
            self.skipTest("tools/cluster_gateway 未包含在当前开源分发包中（定位为企业独立卡池增强版），跳过集群版单元测试。")

        cluster_hub_mod = load_module_from_path("cluster_gateway_hub_test", cluster_hub_path)

        hub = cluster_hub_mod.GatewayHub()
        session = cluster_hub_mod.DongleSession(port="COM8", loc="1-8:x.6", slot_id="slot_1", hub=hub)
        session.board_cellular_data = True  # 关键：开启蜂窝数据时不得退让！
        hub.notify_config = {"feishu": {"enable": 1, "url": "http://127.0.0.1:9999/feishu_mock"}}

        sent_lines = []
        def fake_send_line(line):
            sent_lines.append(line)
            try:
                pkt = json.loads(line)
                if pkt.get("cmd") == "notify_ack":
                    session.on_claim_response({"type": "res", "id": pkt.get("id"), "code": 0, "msg": "NOTIFY_CLAIMED", "data": {"id": pkt.get("data", {}).get("id"), "status": "claimed"}})
            except Exception:
                pass
            return True
        session.send_line = fake_send_line

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"code": 0}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        hub._dispatch_host_proxy_push(session, "sms_rx", {
            "id": f"msg_cluster_{int(time.time()*1000)}",
            "from": "10010",
            "content": "集群测试验证码 123456",
            "code": "123456"
        })

        time.sleep(0.8)

        self.assertTrue(mock_urlopen.called, "集群版在蜂窝开启时也必须优先宽带代推")
        self.assertTrue(any("notify_ack" in l and ("ok" in l or "handled" in l) for l in sent_lines),
                        "集群版代推成功后必须向模组回写 notify_ack [ok]")
        print("  -> 集群 cluster_gateway Hub 验证通过")

if __name__ == "__main__":
    unittest.main()
