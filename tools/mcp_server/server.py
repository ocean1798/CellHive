# -*- coding: utf-8 -*-
"""
Air780EPV 智能随身通信网关 - 工业级 Model Context Protocol (MCP) 服务入口
基于 Anthropic 官方 FastMCP 协议栈规范构建。
具备：
1. 完整实现 Tools + Resources + Prompts 三大原语规范；
2. 双重暴露原则：主动操作与只读资源分流，同时保留大模型主动调起能力；
3. 智能时序守候：支持 wait_for_otp，具备 3 分钟新鲜度与条件变量事件唤醒，彻底根除 4G 空中时延误判；
4. 标准错误传递：所有异常统一交由协议层标记 isError: true。
"""

import sys
import json
import time
from typing import Optional
from mcp.server.fastmcp import FastMCP
from gateway_driver import GatewayDriver

# 初始化 FastMCP 服务与后台驱动
mcp = FastMCP("Air780EPV-Cellular-Gateway")
driver = GatewayDriver()
driver.start()

# ==================== 1. MCP Tools (主动调用工具) ====================

@mcp.tool()
def cellular_list_dongles() -> str:
    """
    主动探测并列出当前多卡槽通信集群（Multi-Dongle Cluster）中所有接入的 4G 模组与卡槽信息。
    支持 1~N 块模组热插拔识别，输出各卡槽的 slot_id、硬件型号、芯片架构(BSP)、绑定的手机号、COM 端口、在线状态、4G 信号(CSQ)以及硬件能力矩阵(如 VoLTE/FOTA 等)。
    """
    try:
        driver.start()
        dongles = driver.list_dongles()
    except Exception as e:
        raise RuntimeError(f"获取集群卡槽列表失败: {e}")

    # 规范卡槽元数据透传，消除 AI 盲人摸象
    slots_meta = []
    for d in dongles:
        slot_id = d.get("slot", "")
        model = d.get("model") or d.get("bsp") or "Air780"
        phone = d.get("phone") or "未知号码"
        caps = d.get("capabilities", {})
        slots_meta.append({
            "slot": slot_id,
            "model": model,
            "phone": phone,
            "port": d.get("port"),
            "online": d.get("online", False),
            "signal_csq": d.get("csq"),
            "capabilities": {
                "volte_call": caps.get("volte", False),
                "fota_upgrade": caps.get("fota") == "supported",
                "sms": True
            }
        })

    result = {
        "cluster_total": len(dongles),
        "slots": slots_meta
    }
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def cellular_wait_for_otp(timeout_seconds: int = 20, freshness_seconds: int = 180, slot: Optional[str] = None) -> str:
    """
    智能守候并提取短信动态验证码（OTP）。解决 4G 基站 3~8 秒空中传输时延的核心时序工具。
    工作机制：
    1. 优先检查过去 freshness_seconds（默认 3 分钟）内是否已收到未消费的有效验证码（支持按 slot 过滤）；
    2. 若无，在事件队列上挂起守候（最大等待 timeout_seconds 秒），全集群短信到站瞬间毫秒级精准唤醒；
    3. 自动通过工业级 3 层防误报引擎剔除发件人尾号与客服电话，返回提取的验证码纯文本与来源卡槽。
    
    参数:
      timeout_seconds: 最大守候秒数（默认 20 秒）
      freshness_seconds: 有效验证码新鲜度窗口（默认 180 秒 / 3分钟）
      slot: 可选卡槽过滤（如 'slot_1', 'slot_2'；缺省为监听集群任意卡槽）
    """
    try:
        driver.start()
        # 前置检查权限与可用性：若 MCP 处于关闭隔离态，立即 Fast-fail 拦截，杜绝盲等 20 秒
        if not driver.is_mcp_action_allowed():
            raise PermissionError("❌ 物理调用被拒绝：上位机管理员已在 Web 控制台中关闭 AI 智能体通信服务 (MCP) 开关。请联系管理员在控制台【系统设置 - AI 智能体通信服务】中开启授权。")
    except PermissionError as pe:
        raise RuntimeError(str(pe))
    except Exception as e:
        if "上位机网关中枢未运行" in str(e):
            raise RuntimeError(str(e))

    try:
        otp_info = driver.wait_for_otp(timeout_seconds=timeout_seconds, freshness_seconds=freshness_seconds, slot=slot)
    except PermissionError as pe:
        raise RuntimeError(str(pe))
    except Exception as e:
        raise RuntimeError(f"验证码守候失败: {e}")

    ts = otp_info.get("ts", otp_info.get("time", 0))
    time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts else "刚刚"
    age_sec = int(time.time() - ts) if ts else 0

    result = {
        "status": "SUCCESS",
        "otp_code": otp_info.get("code"),
        "from_slot": otp_info.get("slot", slot or "slot_1"),
        "receiver_phone": otp_info.get("receiver") or "",
        "sender": otp_info.get("from"),
        "received_at": time_str,
        "age_seconds_ago": age_sec,
        "raw_content": otp_info.get("content")
    }
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def cellular_get_status(slot: Optional[str] = None) -> str:
    """
    主动查阅 4G 智能网关的硬件运行看板与蜂窝网络状态（支持指定卡槽）。
    包含：当前卡槽归属、模组型号、4G 信号强度 (CSQ / RSRP)、核心温度、供电电压、脱机黑匣子短信存量、4G随身上网状态、连续开机运行时间。

    参数:
      slot: 目标卡槽编号（如 'slot_1', 'slot_2'；缺省自动路由至主卡槽或当前活跃模组）
    """
    try:
        driver.start()
        stat = driver.get_status(slot=slot)
    except Exception as e:
        raise RuntimeError(f"获取网关状态失败: {e}")

    rndis_txt = "🟢 已开启 (USB 虚拟网卡在线)" if stat.get("rndis") else "⚪ 已关闭 "
    data_txt = "🟢 已开启 (移动数据)" if stat.get("cellular_data") else "⚪ 已关闭 (移动数据)"
    uptime_sec = stat.get("uptime_seconds", 0)
    m, s = divmod(uptime_sec, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    uptime_str = f"{d}天 {h}时 {m}分 {s}秒" if d > 0 else f"{h}时 {m}分 {s}秒"

    result = {
        "slot": slot or stat.get("slot", "slot_1"),
        "hardware": {
            "model": stat.get("bsp") or stat.get("model", "Air780 Series"),
            "firmware_version": f"v{stat.get('version', '1.2.1')}",
            "temperature_celsius": stat.get("temp"),
            "voltage_vbat": f"{stat.get('vbat')} V",
            "lua_memory_used_kb": f"{stat.get('lua_mem_kb')} KB / 128 KB",
            "capabilities": stat.get("capabilities", {})
        },
        "cellular_network": {
            "network_ready": stat.get("net_ready", False),
            "csq_signal": stat.get("csq"),
            "rsrp_dbm": f"{stat.get('rsrp')} dBm"
        },
        "system_status": {
            "pc_rndis_sharing": rndis_txt,
            "board_cellular_data": data_txt,
            "blackbox_sms_count": stat.get("blackbox_count", 0),
            "continuous_uptime": uptime_str,
            "daily_reboot_policy": stat.get("daily_reboot_desc", "未开启")
        }
    }
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def cellular_dial_phone(phone: str, slot: Optional[str] = None, timeout_seconds: int = 15, hangup_on_answer: bool = True) -> str:
    """
    驱动 4G VoLTE 蜂窝语音向目标电话号码发起呼叫振铃（AIR-30）。
    可用于突发紧急告警（服务器宕机、UPS断电、安防报警）时物理打响主人手机，支持超时自动挂断。
    
    参数:
      phone: 目标电话号码（如 '13800000000'）
      slot: 可选出站卡槽（如 'slot_2'；缺省自动优选具备 VoLTE 语音协议栈且插卡的模组）
      timeout_seconds: 最大振铃等待秒数（默认 15 秒，超时自动挂断）
      hangup_on_answer: 对方接听后是否立即挂断（默认 True）
    """
    if not phone:
        raise ValueError("目标电话号码不能为空")

    try:
        driver.start()
        res = driver.dial_phone(phone, slot=slot, timeout_seconds=timeout_seconds, hangup_on_answer=hangup_on_answer)
    except Exception as e:
        raise RuntimeError(f"拨号失败: {e}")

    if not res.get("ok"):
        err = res.get("error") or res.get("msg") or "未知错误"
        return f"❌ 电话呼叫失败: {err} (卡槽: {res.get('slot') or slot})"

    used_slot = res.get("slot") or slot or "slot_2"
    watchdog_desc = f"{timeout_seconds}秒后自动挂断"
    return f"📞 已向 {phone} 发起 VoLTE 电话呼叫！[出站卡槽: {used_slot}] 状态: 对方手机正在振铃，{watchdog_desc}。"

@mcp.tool()
def cellular_hangup_phone(slot: Optional[str] = None) -> str:
    """
    主动挂断当前正在进行中的电话呼叫（AIR-30）。
    
    参数:
      slot: 可选卡槽编号（缺省针对当前活跃卡槽）
    """
    try:
        driver.start()
        res = driver.hangup_phone(slot=slot)
    except Exception as e:
        raise RuntimeError(f"挂断通话失败: {e}")

    used_slot = res.get("slot") or slot or "slot_2"
    return f"📴 已向卡槽 [{used_slot}] 下发挂断指令。"

@mcp.tool()
def cellular_send_sms(phone: str, content: str, slot: Optional[str] = None, strategy: Optional[str] = "operator_affinity") -> str:
    """
    驱动 4G 蜂窝射频向目标手机号主动代发一条短信（支持指定出站卡槽与智能路由分流 AIR-22）。
    
    参数:
      phone: 目标手机号码（如 '+8613800000000' 或 '10010'）
      content: 短信文本正文
      slot: 可选出站卡槽编号（如 'slot_1', 'slot_2'；缺省自动根据策略优选健康卡槽）
      strategy: 可选出站分流策略（'operator_affinity' 同网优先[默认], 'round_robin' 轮询, 'signal_best' 强信优先）
    """
    if not phone or not content:
        raise ValueError("手机号和短信正文不能为空")

    try:
        driver.start()
        res = driver.send_sms(phone, content, slot=slot, strategy=strategy or "operator_affinity")
    except Exception as e:
        raise RuntimeError(f"短信下发失败: {e}")

    used_slot = res.get("slot") or slot
    target_slot_desc = f" [出站卡槽: {used_slot}]" if used_slot else ""
    strat_desc = f" [策略: {res.get('routed_strategy', strategy)}]" if not slot else ""
    msg = res.get("msg")
    if msg == "QUEUED_TO_BASE_STATION":
        return f"✅ 短信已成功提交至 4G 基站发送队列！{target_slot_desc}{strat_desc} 目标: {phone}，正文: '{content}'"
    return f"ℹ️ 蜂窝基带反馈{target_slot_desc}{strat_desc}: {res}"

@mcp.tool()
def cellular_get_history(limit: int = 20, keyword: Optional[str] = None, slot: Optional[str] = None) -> str:
    """
    主动查阅模组板载 128KB LittleFS 脱机黑匣子中的短信历史记录（断电记忆不丢，支持按卡槽查看）。
    
    参数:
      limit: 获取最近短信的最大条数（默认 20 条）
      keyword: 可选关键词过滤（按发件人号码或正文内容筛选）
      slot: 可选卡槽编号（如 'slot_1', 'slot_2'；缺省查看主卡槽）
    """
    try:
        driver.start()
        hist = driver.get_history(limit=limit, slot=slot)
    except Exception as e:
        raise RuntimeError(f"读取脱机黑匣子失败: {e}")

    items = hist.get("items", [])
    if keyword:
        kw = keyword.lower()
        items = [it for it in items if kw in it.get("sender", "").lower() or kw in it.get("content", "").lower()]

    formatted = []
    for it in items:
        ts = it.get("time", 0)
        t_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts else ""
        formatted.append({
            "sender": it.get("sender"),
            "time": t_str,
            "otp": it.get("otp"),
            "content": it.get("content")
        })

    result = {
        "slot": slot or "slot_1",
        "total_archived": hist.get("total", len(items)),
        "returned_count": len(formatted),
        "filter_keyword": keyword or "none",
        "messages": formatted
    }
    return json.dumps(result, ensure_ascii=False, indent=2)

@mcp.tool()
def cellular_clear_sms_history(slot: Optional[str] = None) -> str:
    """
    清空板载 LittleFS 脱机黑匣子中的全部短信存档（支持指定卡槽）。
    
    参数:
      slot: 可选卡槽编号（如 'slot_1', 'slot_2'；缺省清空主卡槽）
    """
    try:
        driver.start()
        driver.clear_history(slot=slot)
    except Exception as e:
        raise RuntimeError(f"清空黑匣子失败: {e}")
    target_desc = f"【{slot}】" if slot else ""
    return f"✅ 板载 LittleFS 脱机黑匣子短信存档{target_desc}已彻底清空！"

@mcp.tool()
def cellular_toggle_rndis(enable: bool, slot: Optional[str] = None) -> str:
    """
    受控开启或关闭 4G 随身上网（USB 虚拟网卡 RNDIS）。
    出厂默认关闭，开启后宿主机可直接通过 4G 随身上网。
    
    参数:
      enable: True 为开启 4G 随身上网；False 为关闭 4G 随身上网
      slot: 可选卡槽编号（如 'slot_1', 'slot_2'；缺省针对当前活跃卡槽）
    """
    try:
        driver.start()
        driver.set_rndis(enable, slot=slot)
    except Exception as e:
        raise RuntimeError(f"随身上网切换失败: {e}")

    action_txt = "开启 4G 随身上网" if enable else "关闭 4G 随身上网"
    target_desc = f"【{slot}】" if slot else ""
    return f"✅ 指令已执行: 正在对{target_desc}模组{action_txt}... 模组已启动 USB 协议栈热复位，预计 5 秒内完成重连。"

@mcp.tool()
def cellular_toggle_board_data(enable: bool, slot: Optional[str] = None) -> str:
    """
    受控开启或关闭模组自身的 4G 移动数据通信。
    出厂默认关闭，由上位机推送通知；若脱机使用需要模组自身发 HTTP 推送，可按需开启。
    
    参数:
      enable: True 为开启移动数据；False 为关闭移动数据
      slot: 可选卡槽编号（如 'slot_1', 'slot_2'；缺省针对当前活跃卡槽）
    """
    try:
        driver.start()
        driver.set_cellular_data(enable, slot=slot)
    except Exception as e:
        raise RuntimeError(f"板载蜂窝数据切换失败: {e}")

    action_txt = "开启移动数据" if enable else "关闭移动数据"
    target_desc = f"【{slot}】" if slot else ""
    return f"✅ 指令已执行: 已对{target_desc}{action_txt}。"

@mcp.tool()
def cellular_reboot_gateway(reason: str = "mcp_agent_action", slot: Optional[str] = None) -> str:
    """
    向网关下发软复位指令，安全重启硬件模组（支持指定卡槽）。
    
    参数:
      reason: 重启原因说明
      slot: 可选卡槽编号（如 'slot_1', 'slot_2'；缺省重启当前活跃卡槽）
    """
    try:
        driver.start()
        driver.reboot(reason=reason, slot=slot)
    except Exception as e:
        raise RuntimeError(f"重启指令下发失败: {e}")
    target_desc = f"【{slot}】" if slot else ""
    return f"✅ 已成功向{target_desc}网关下发重启指令 (原因: {reason})，模组将在 1 秒内安全复位并重新驻网。"

# ==================== 2. MCP Resources (只读上下文资源) ====================

@mcp.resource("cellular://dongles")
def resource_dongles_cluster() -> str:
    """提供当前多模组通信集群所有卡槽的实时看板数据列表 JSON。"""
    driver.start()
    return json.dumps(driver.list_dongles(), ensure_ascii=False, indent=2)

@mcp.resource("cellular://gateway/status")
def resource_gateway_status() -> str:
    """提供网关当前硬件温度、电压、4G 信号与网络状态的实时全景 JSON。"""
    driver.start()
    return json.dumps(driver.get_status(), ensure_ascii=False, indent=2)

@mcp.resource("cellular://sms/latest")
def resource_latest_sms() -> str:
    """提供最新一条收到的短信详情。"""
    driver.start()
    try:
        hist = driver.get_history(limit=1)
    except PermissionError:
        return "⚠️ AI 智能体通信服务已在控制台中关闭，无法访问短信黑匣子资源。"
    except Exception as e:
        return f"读取短信失败: {e}"
    items = hist.get("items", [])
    if not items:
        return "暂无收到任何短信记录"
    latest = items[0]
    return f"发件人: {latest.get('sender')}\n时间: {latest.get('time')}\n验证码: {latest.get('otp') or '无'}\n正文: {latest.get('content')}"

@mcp.resource("cellular://sms/history")
def resource_sms_history() -> str:
    """提供模组板载脱机黑匣子短信存档的只读数据流。"""
    driver.start()
    try:
        hist = driver.get_history(limit=50)
        return json.dumps(hist, ensure_ascii=False, indent=2)
    except PermissionError:
        return json.dumps({"ok": False, "error": "⚠️ AI 智能体通信服务已在控制台中关闭，无法访问短信黑匣子资源。"}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)

# ==================== 3. MCP Prompts (工作流模版) ====================

@mcp.prompt("carrier_query")
def prompt_carrier_query(carrier: str = "中国联通") -> str:
    """
    运营商短信业务办理与查询模版。
    """
    return (
        f"我想查询当前手机卡的业务信息。运营商为：{carrier}。\n"
        "常用指令参考：\n"
        "- 中国联通：发送 '101' 到 '10010' 查话费；发送 '201' 到 '10010' 查流量；发送 'CXLL' 到 '10010' 查套餐余量。\n"
        "- 中国移动：发送 '101' 到 '10086' 查话费；发送 'CXLL' 到 '10086' 查流量。\n"
        "- 中国电信：发送 '101' 到 '10001' 查话费；发送 '108' 到 '10001' 查流量。\n"
        "请根据需要，调用 cellular_send_sms 工具代我发送查询短信，并在 10 秒后通过 cellular_wait_for_otp 或查阅最新短信获取反馈。"
    )

@mcp.prompt("otp_verification")
def prompt_otp_verification() -> str:
    """
    验证码自动监听与提取工作流提示词。
    """
    return (
        "用户正在等待一条登录或验证短信。请按照以下规范处理：\n"
        "1. 立即调用 cellular_wait_for_otp(timeout_seconds=25, freshness_seconds=180) 守候短信；\n"
        "2. 成功获取后，突出展示提取出的验证码（如大字纯文本形式），并简要说明发件人和接收时间；\n"
        "3. 若超时未收到，提醒用户检查手机号是否正确，并询问是否重新发起守候。"
    )

if __name__ == "__main__":
    # 启动 FastMCP 服务 (通过 stdio 交换 JSON-RPC 2.0)
    mcp.run()
