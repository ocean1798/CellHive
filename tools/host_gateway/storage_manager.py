# -*- coding: utf-8 -*-
"""
Air780 智能通信网关 - 多卡分舱与墓碑存储引擎 (AIR-27)
以 ICCID 作为物理分舱主键，原生 msg_id 作为墓碑防复活强确定性索引，
兼容维护全局活跃视图 gateway_history.json。
"""

import os
import re
import json
import time
import hashlib
import threading
from typing import Dict, List, Any, Optional

try:
    from send2trash import send2trash
except ImportError:
    def send2trash(path):
        # 回退机制：若无 send2trash，安全保留原文件或标记
        pass

# 常见国内运营商 ICCID 前缀推导字典 (ITU-T E.118 / 8986)
OPERATOR_PREFIXES = [
    ("898600", "中国移动"),
    ("898602", "中国移动"),
    ("898604", "中国移动"),
    ("898607", "中国移动"),
    ("898608", "中国移动"),
    ("898601", "中国联通"),
    ("898606", "中国联通"),
    ("898609", "中国联通"),
    ("898603", "中国电信"),
    ("898605", "中国电信"),
    ("898611", "中国电信"),
    ("898612", "中国广电"),
]

# 运营商号段规则 (ITU-T / 中国工信部)
UNICOM_PREFIXES = ("130", "131", "132", "145", "155", "156", "166", "175", "176", "185", "186", "196")
MOBILE_PREFIXES = ("134", "135", "136", "137", "138", "139", "147", "150", "151", "152", "157", "158", "159", "172", "178", "182", "183", "184", "187", "188", "195", "197", "198")
TELECOM_PREFIXES = ("133", "149", "153", "173", "174", "177", "180", "181", "189", "190", "191", "193", "199")
BROADNET_PREFIXES = ("192",)

# 官方特服号 (蜂窝短信中心专有路由)
OFFICIAL_SERVICES = {
    "10010": "中国联通",
    "10086": "中国移动",
    "10000": "中国电信",
    "10099": "中国广电",
}

def derive_operator_and_badge(iccid: Optional[str] = None,
                              my_phone: Optional[str] = None,
                              sender: Optional[str] = None,
                              content: Optional[str] = None,
                              model: Optional[str] = None,
                              slot: Optional[str] = None) -> Dict[str, str]:
    """
    根据 ICCID、本机号码、发件人及正文签名四级推导运营商与大白话徽章 (AIR-31)
    四级推导优先级：
    1. ICCID 标准前缀识别
    2. 本机手机号段推导
    3. 官方特服号 (10010/10086/10000/10099) 发件人识别
    4. 正文首部签名强正则匹配 (^【中国联通】等) 兜底 (防伪基站跨网误判)
    """
    operator = "未知运营商"
    clean_iccid = str(iccid or "").strip()

    # 1. ICCID 优先
    for prefix, name in OPERATOR_PREFIXES:
        if clean_iccid.startswith(prefix):
            operator = name
            break

    # 2. 本机号段二级推导
    clean_phone = re.sub(r"\D", "", str(my_phone or ""))
    phone_pfx3 = ""
    if clean_phone.startswith("86") and len(clean_phone) >= 5:
        phone_pfx3 = clean_phone[2:5]
    elif len(clean_phone) >= 3:
        phone_pfx3 = clean_phone[:3]

    if operator == "未知运营商" and phone_pfx3:
        if phone_pfx3 in UNICOM_PREFIXES:
            operator = "中国联通"
        elif phone_pfx3 in MOBILE_PREFIXES:
            operator = "中国移动"
        elif phone_pfx3 in TELECOM_PREFIXES:
            operator = "中国电信"
        elif phone_pfx3 in BROADNET_PREFIXES:
            operator = "中国广电"

    # 3. 官方特服号发件人三级推导
    clean_sender = str(sender or "").strip()
    if operator == "未知运营商" and clean_sender in OFFICIAL_SERVICES:
        operator = OFFICIAL_SERVICES[clean_sender]

    # 4. 短信首部官方签名四级兜底 (严格匹配首部，防营销伪装短信)
    clean_content = str(content or "").strip()
    if operator == "未知运营商" and clean_content:
        m = re.match(r"^【(中国联通|中国移动|中国电信|中国广电)】", clean_content)
        if m:
            operator = m.group(1)

    # 提取本机尾号
    badge_tail = ""
    if len(clean_phone) >= 4:
        badge_tail = clean_phone[-4:]
    elif len(clean_iccid) >= 4 and clean_iccid != "sim_unknown":
        badge_tail = clean_iccid[-4:]

    short_op = operator.replace("中国", "") if operator != "未知运营商" else "蜂窝"

    # 生成紧凑融合风格展示徽章 (方案 C: Air780EPV · 联通 2515)
    raw_model = str(model or "").replace("合宙", "").strip()
    if not raw_model:
        raw_model = "Air780"

    if badge_tail and short_op != "蜂窝":
        display_badge = f"{raw_model} · {short_op} {badge_tail}"
    elif short_op != "蜂窝":
        display_badge = f"{raw_model} · {short_op}"
    elif badge_tail:
        display_badge = f"{raw_model} · {badge_tail}"
    else:
        display_badge = f"{raw_model} · 蜂窝网络"

    m_slot = re.match(r"^slot_(\d+)$", str(slot or ""))
    slot_name = f"卡 {m_slot.group(1)}" if m_slot else (slot or "模组")
    slot_label = f"{slot_name} · {operator}"

    return {
        "operator": operator,
        "short_operator": short_op,
        "badge_text": f"📱 {short_op} · {badge_tail}" if badge_tail else f"📱 {short_op}",
        "display_badge": display_badge,
        "slot_display": display_badge,
        "slot_label": slot_label,
        "tail": badge_tail,
        "iccid": clean_iccid
    }

class CardStorageCompartment:
    """单个 SIM 卡（基于 ICCID）的物理独立分舱"""
    def __init__(self, card_dir: str, iccid: str):
        self.card_dir = card_dir
        self.iccid = iccid
        self.messages_path = os.path.join(card_dir, "messages.json")
        self.calls_path = os.path.join(card_dir, "calls.json")
        self.tombstones_path = os.path.join(card_dir, "tombstones.json")
        self.lock = threading.RLock()
        os.makedirs(self.card_dir, exist_ok=True)
        self._load_tombstones()

    def _load_tombstones(self):
        self.tombstone_ids = set()
        self.tombstone_fingerprints = set()
        if os.path.isfile(self.tombstones_path):
            try:
                with open(self.tombstones_path, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                    if isinstance(entries, list):
                        for e in entries:
                            if isinstance(e, dict):
                                if "msg_id" in e:
                                    self.tombstone_ids.add(str(e["msg_id"]))
                                sender = self._normalize_phone(e.get("sender", ""))
                                content = str(e.get("content", "")).strip()
                                t_sec = e.get("time_sec") or self._normalize_time_sec(e.get("time"))
                                if sender or content:
                                    self.tombstone_fingerprints.add((sender, content, t_sec))
                            elif isinstance(e, str):
                                self.tombstone_ids.add(e)
            except Exception:
                pass

    def _normalize_time_sec(self, raw_time: Any) -> int:
        if not raw_time:
            return 0
        try:
            if isinstance(raw_time, (int, float)):
                v = float(raw_time)
                return int(v) if v < 1e11 else int(v / 1000)
            if isinstance(raw_time, str):
                s = raw_time.strip()
                if s.isdigit():
                    v = float(s)
                    return int(v) if v < 1e11 else int(v / 1000)
                if len(s) >= 19 and "-" in s and ":" in s:
                    return int(time.mktime(time.strptime(s[:19], "%Y-%m-%d %H:%M:%S")))
        except Exception:
            pass
        return 0

    def _normalize_phone(self, phone: str) -> str:
        s = str(phone or "").strip()
        if s.startswith("+86"):
            s = s[3:]
        return s

    def calculate_fallback_id(self, sender: str, content: str, raw_time: Any) -> str:
        clean_sender = self._normalize_phone(sender)
        clean_content = str(content or "").strip()
        t_sec = self._normalize_time_sec(raw_time)
        s = f"{self.iccid}:{clean_sender}:{clean_content}:{t_sec}"
        return f"legacy_{hashlib.md5(s.encode('utf-8')).hexdigest()[:16]}"

    def is_deleted(self, msg_id: str, sender: str = "", content: str = "", raw_time: Any = "") -> bool:
        if msg_id and str(msg_id) in self.tombstone_ids:
            return True
        fb_id = self.calculate_fallback_id(sender, content, raw_time)
        if fb_id in self.tombstone_ids:
            return True
        clean_sender = self._normalize_phone(sender)
        clean_content = str(content or "").strip()
        if clean_content:
            t_sec = self._normalize_time_sec(raw_time)
            for (ts_sender, ts_content, ts_time) in self.tombstone_fingerprints:
                if ts_sender == clean_sender and ts_content == clean_content:
                    if ts_time == 0 or t_sec == 0 or abs(ts_time - t_sec) <= 3600:
                        return True
        return False

    def add_tombstone(self, msg_id: str, sender: str = "", content: str = "", raw_time: Any = "") -> bool:
        fb_id = self.calculate_fallback_id(sender, content, raw_time)
        clean_sender = self._normalize_phone(sender)
        clean_content = str(content or "").strip()
        t_sec = self._normalize_time_sec(raw_time)

        with self.lock:
            if msg_id:
                self.tombstone_ids.add(str(msg_id))
            self.tombstone_ids.add(fb_id)
            if clean_sender or clean_content:
                self.tombstone_fingerprints.add((clean_sender, clean_content, t_sec))

            entries = []
            if os.path.isfile(self.tombstones_path):
                try:
                    with open(self.tombstones_path, "r", encoding="utf-8") as f:
                        entries = json.load(f)
                except Exception:
                    entries = []

            entries.append({
                "msg_id": str(msg_id or fb_id),
                "fallback_id": fb_id,
                "iccid": self.iccid,
                "sender": clean_sender,
                "content": clean_content,
                "time_sec": t_sec,
                "deleted_at": int(time.time())
            })
            temp_path = self.tombstones_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.tombstones_path)

            # 同时从 messages.json 物理剔除
            msgs = self.load_messages()
            new_msgs = [
                m for m in msgs 
                if not self.is_deleted(
                    m.get("id"), 
                    m.get("phone") or m.get("sender", ""), 
                    m.get("content", ""), 
                    m.get("time") or m.get("timestamp", "")
                )
            ]
            self.save_messages(new_msgs)
            return True

    def load_messages(self) -> List[Dict[str, Any]]:
        with self.lock:
            if not os.path.isfile(self.messages_path):
                return []
            try:
                with open(self.messages_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
            except Exception:
                return []

    def save_messages(self, messages: List[Dict[str, Any]]):
        with self.lock:
            temp_path = self.messages_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.messages_path)

    def append_message(self, msg: Dict[str, Any]) -> bool:
        """追加单条短信"""
        msg_id = msg.get("id")
        if self.is_deleted(msg_id, msg.get("phone", ""), msg.get("content", ""), msg.get("time", "")):
            return False
        with self.lock:
            msgs = self.load_messages()
            # 查重
            for existing in msgs:
                if msg_id and existing.get("id") == msg_id:
                    return False
            msgs.insert(0, msg)
            if len(msgs) > 10000:
                msgs.pop()
            self.save_messages(msgs)
            return True

    def clear_local(self) -> bool:
        """清空当前分舱的本地记录，文件移入系统回收站"""
        with self.lock:
            for p in (self.messages_path, self.calls_path):
                if os.path.isfile(p):
                    try:
                        send2trash(p)
                    except Exception:
                        try:
                            os.remove(p)
                        except Exception:
                            pass
            return True


class StorageManager:
    """全局存储中枢管理器"""
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.cards_dir = os.path.join(data_dir, "cards")
        self.legacy_history_path = os.path.join(data_dir, "gateway_history.json")
        self.lock = threading.RLock()
        self.compartments: Dict[str, CardStorageCompartment] = {}
        if os.path.isfile(self.legacy_history_path):
            self._migrate_legacy_if_needed()

    def get_compartment(self, iccid: Optional[str]) -> CardStorageCompartment:
        clean_iccid = str(iccid or "sim_unknown").strip()
        if not clean_iccid:
            clean_iccid = "sim_unknown"
        with self.lock:
            if clean_iccid not in self.compartments:
                os.makedirs(self.cards_dir, exist_ok=True)
                card_path = os.path.join(self.cards_dir, clean_iccid)
                self.compartments[clean_iccid] = CardStorageCompartment(card_path, clean_iccid)
            return self.compartments[clean_iccid]

    def _migrate_legacy_if_needed(self):
        """将旧版平铺 gateway_history.json 迁移到首张识别卡或 sim_unknown"""
        if not os.path.isfile(self.legacy_history_path):
            return
        try:
            with open(self.legacy_history_path, "r", encoding="utf-8") as f:
                history = json.load(f)
            sms_list = history.get("sms", [])
            calls_list = history.get("calls", [])
            if not sms_list and not calls_list:
                return

            # 推导迁移归宿
            target_iccid = "sim_unknown"
            for s in sms_list:
                if s.get("iccid"):
                    target_iccid = s["iccid"]
                    break

            comp = self.get_compartment(target_iccid)
            if not os.path.isfile(comp.messages_path) and sms_list:
                comp.save_messages(sms_list)
        except Exception:
            pass

    def sync_projection_view(self, active_iccid: Optional[str], recent_sms: List[Dict[str, Any]], recent_calls: List[Dict[str, Any]]):
        """双写兼容投影至 gateway_history.json，支撑原有受管测试基线"""
        try:
            history = {"sms": list(recent_sms), "calls": list(recent_calls)}
            temp = self.legacy_history_path + ".pending"
            with open(temp, "w", encoding="utf-8") as output:
                json.dump(history, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.legacy_history_path)
        except Exception:
            pass

    def get_aggregated_messages(self, limit: int = 40, cursor: Optional[str] = None,
                                slot_filter: Optional[str] = None,
                                keyword: Optional[str] = None,
                                order: str = "desc",
                                slots_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        全集群多卡槽复合游标聚合分页 (AIR-22 / 权威存储重构)
        - 单页默认 40 条 (支持 1~100)
        - 支持关键词服务端全文检索 (keyword)
        - 采用复合稳定游标 (sort_ts, msg_id)，彻底杜绝同秒多信丢失
        """
        all_msgs: List[Dict[str, Any]] = []
        limit = max(1, min(limit, 100))

        # 构建 ICCID 到 slot 的反查表
        iccid_to_slot: Dict[str, str] = {}
        slot_to_iccid: Dict[str, str] = {}
        if slots_meta:
            for s_id, meta in slots_meta.items():
                iccid = str(meta.get("iccid") or "").strip()
                if iccid:
                    iccid_to_slot[iccid] = s_id
                    slot_to_iccid[s_id] = iccid

        # 若指定了 slot_filter 且能定位到特定 iccid，可做单卡定向检索优化
        target_iccids = None
        if slot_filter and slot_filter != "all":
            if slot_filter in slot_to_iccid:
                target_iccids = [slot_to_iccid[slot_filter]]

        kw_clean = str(keyword).strip().lower() if keyword else None

        # 遍历卡槽分舱
        with self.lock:
            if not os.path.isdir(self.cards_dir):
                return {"items": [], "has_more": False, "next_cursor": None, "total": 0, "count": 0}

            card_folders = target_iccids if target_iccids is not None else os.listdir(self.cards_dir)
            for card_name in card_folders:
                c_dir = os.path.join(self.cards_dir, card_name)
                if not os.path.isdir(c_dir):
                    continue
                comp = self.get_compartment(card_name)
                raw_msgs = comp.load_messages()
                for m in raw_msgs:
                    m_id = str(m.get("id") or "")
                    sender = str(m.get("phone") or m.get("from") or m.get("sender") or "")
                    content = str(m.get("content") or "")
                    raw_time = m.get("time") or m.get("received_at") or ""

                    # 墓碑过滤
                    if comp.is_deleted(m_id, sender, content, raw_time):
                        continue

                    # 补齐卡槽归属
                    msg_slot = m.get("slot") or iccid_to_slot.get(card_name) or "slot_1"
                    if slot_filter and slot_filter != "all" and msg_slot != slot_filter:
                        continue

                    # 关键词全文检索过滤 (发件人/内容)
                    if kw_clean:
                        if kw_clean not in sender.lower() and kw_clean not in content.lower():
                            continue

                    # 时间戳归一化
                    ts = m.get("timestamp")
                    if ts is None:
                        # 尝试从 time 字段解析
                        try:
                            if isinstance(raw_time, (int, float)):
                                ts = float(raw_time)
                            elif isinstance(raw_time, str) and len(raw_time) >= 19:
                                ts = time.mktime(time.strptime(raw_time[:19], "%Y-%m-%d %H:%M:%S"))
                            else:
                                ts = float(m.get("id") or 0.0)
                        except Exception:
                            ts = 0.0

                    m_copy = dict(m)
                    m_copy["slot"] = msg_slot
                    m_copy["iccid"] = card_name
                    m_copy["sort_ts"] = float(ts)
                    # A parsed legacy text/id may sort records, but must not become display time.
                    source_time = m.get("timestamp")
                    if source_time is None and (isinstance(raw_time, (int, float)) or
                                                (isinstance(raw_time, str) and raw_time.strip().isdigit())):
                        source_time = raw_time
                    try:
                        display_ts = float(source_time)
                        if 1e11 <= display_ts < 1e14:
                            display_ts /= 1000
                        if not 1e9 <= display_ts < 1e11:
                            raise ValueError("untrusted timestamp")
                        m_copy["timestamp"] = display_ts
                    except (TypeError, ValueError):
                        m_copy.pop("timestamp", None)
                    m_copy["id"] = m_id or f"{msg_slot}_{int(float(ts)*1000)}"

                    # 关联卡槽当前活跃元数据 (真实本机号码与型号，杜绝将发件人误当本机号码)
                    slot_info = (slots_meta or {}).get(msg_slot, {})
                    slot_phone = slot_info.get("phone") or slot_info.get("number")
                    slot_model = slot_info.get("model") or slot_info.get("bsp")
                    slot_iccid = slot_info.get("iccid")
                    eff_iccid = card_name if (card_name and card_name != "sim_unknown") else (slot_iccid or "")

                    # 智能计算运营商与去工程化紧凑徽章 (AIR-31)
                    badge_info = derive_operator_and_badge(
                        iccid=eff_iccid,
                        my_phone=slot_phone,
                        sender=sender,
                        content=content,
                        model=slot_model,
                        slot=msg_slot
                    )
                    m_copy["operator"] = badge_info["operator"]
                    m_copy["short_operator"] = badge_info["short_operator"]
                    m_copy["display_badge"] = badge_info["display_badge"]
                    m_copy["slot_display"] = badge_info["display_badge"]
                    m_copy["slot_badge"] = f"[{badge_info['display_badge']}]"
                    m_copy["slot_label"] = badge_info["slot_label"]

                    all_msgs.append(m_copy)

        # 排序：默认时间戳逆序（最新在前），同秒则按 id 逆序
        is_asc = (order == "asc")
        all_msgs.sort(key=lambda x: (x.get("sort_ts", 0.0), str(x.get("id", ""))), reverse=(not is_asc))

        total_filtered_cnt = len(all_msgs)

        # 复合游标过滤
        if cursor:
            cursor_str = str(cursor).strip()
            cursor_ts = None
            cursor_id = ""
            if "_" in cursor_str:
                parts = cursor_str.split("_", 1)
                try:
                    cursor_ts = float(parts[0])
                    cursor_id = parts[1]
                except Exception:
                    pass
            if cursor_ts is None:
                try:
                    cursor_ts = float(cursor_str)
                except Exception:
                    cursor_ts = None

            if cursor_ts is not None:
                if not is_asc:
                    # 逆序分页：取更旧的数据
                    if cursor_id:
                        all_msgs = [
                            x for x in all_msgs
                            if (x.get("sort_ts", 0.0) < cursor_ts) or
                               (x.get("sort_ts", 0.0) == cursor_ts and str(x.get("id", "")) < cursor_id)
                        ]
                    else:
                        all_msgs = [x for x in all_msgs if x.get("sort_ts", 0.0) < cursor_ts]
                else:
                    # 正序分页：取更新的数据
                    if cursor_id:
                        all_msgs = [
                            x for x in all_msgs
                            if (x.get("sort_ts", 0.0) > cursor_ts) or
                               (x.get("sort_ts", 0.0) == cursor_ts and str(x.get("id", "")) > cursor_id)
                        ]
                    else:
                        all_msgs = [x for x in all_msgs if x.get("sort_ts", 0.0) > cursor_ts]

        items = all_msgs[:limit]
        has_more = len(all_msgs) > limit
        next_cursor = None
        if has_more and items:
            last_item = items[-1]
            next_cursor = f"{last_item.get('sort_ts', 0.0)}_{last_item.get('id', '')}"

        return {
            "items": items,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "total": total_filtered_cnt,
            "count": len(items)
        }
