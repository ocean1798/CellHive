PROJECT = "Air780EPV_Gateway"
VERSION = "1.2.9"
local BUILD_ID = "hardware-gateway-r1-20260915"
_G.GATEWAY_VERSION = VERSION
log.setLevel(2) -- INFO 级别
log.style(0) -- 纯文本风格输出，避免合宙上位机私有二进制帧干扰通信
_G.sys     = require "sys"
_G.sysplus = require "sysplus"
_G.config  = require "config"
_G.led     = require "led"
local model           = require "model"
local serial_comm     = require "serial_comm"
local sms_service     = require "sms_service"
local call_service    = require "call_service"
local storage_service = require "storage_service"
local notify_service  = require "notify_service"
local reboot_service  = require "reboot_service"
local fota_service    = require "fota_service"
if wdt then
    wdt.init(1000 * 10)
    sys.timerLoopStart(wdt.feed, 1000 * 3)
    log.info("sys", "Hardware watchdog armed")
end
if errDump then errDump.config(false) end
pm.force(pm.NONE)                   -- 保持常态活跃，不进入深度休眠
pm.power(pm.GPS, false)
pm.power(pm.GPS_ANT, false)
pm.power(pm.CAMERA, false)
if fskv then
    fskv.init()
end
local function is_rndis_persisted()
    if fskv then
        return fskv.get("rndis_enable") == true
    end
    return false
end
local function set_rndis_persisted(enable)
    if fskv then
        fskv.set("rndis_enable", enable == true)
    end
end
local function is_cellular_data_persisted()
    if fskv then
        return fskv.get("cellular_data_enable") == true
    end
    return false
end
local function set_cellular_data_persisted(enable)
    if fskv then
        fskv.set("cellular_data_enable", enable == true)
    end
end
local rndis_initial = is_rndis_persisted()
if mobile and mobile.CONF_USB_ETHERNET then
    mobile.config(mobile.CONF_USB_ETHERNET, rndis_initial and 3 or 0)
    log.info("main", "RNDIS USB mode initialized to:", rndis_initial and 3 or 0)
end
mobile.config(mobile.CONF_STATICCONFIG, 1)  -- 静态网络优化
mobile.config(mobile.CONF_QUALITYFIRST, 2)  -- 信号质量优先
mobile.ipv6(config and config.network and config.network.IPv6 == 1)
mobile.setAuto(1000 * 10, 1000 * 30, 5)     -- SIM脱落恢复与周期搜网
local data_initial = is_cellular_data_persisted()
local gateway_state = {
    net_ready = false,           -- 蜂窝信号/信令驻网就绪 (SMS/Call OK)
    ip_ready = false,            -- 蜂窝 IP 数据承载激活
    dis_count = 0,
    rndis_active = rndis_initial,
    data_active = data_initial,
    boot_notified = false
}
local temp_cellular_override = false
_G.set_temp_cellular_data = function(enable, reason)
    temp_cellular_override = (enable == true)
    log.info("main", "Temp cellular data override set to:", temp_cellular_override, "reason:", reason or "unspecified")
end
_G.is_cellular_data_enabled = function()
    return gateway_state.data_active == true or temp_cellular_override == true
end
serial_comm.init()
sms_service.init()
call_service.init()
storage_service.init()
notify_service.init()
reboot_service.init()
fota_service.init()
local function format_phone_number(raw_num)
    if not raw_num or #raw_num == 0 then
        return "未知"
    end
    local num11 = raw_num:match("%+86(%d+)")
    if num11 and #num11 == 11 then
        return num11 .. " +86"
    end
    num11 = raw_num:match("^(%d%d%d%d%d%d%d%d%d%d%d)$")
    if num11 then
        return num11 .. " +86"
    end
    return raw_num
end
local function build_status_report()
    local raw_num = mobile and mobile.number and mobile.number() or nil
    local num_str = format_phone_number(raw_num)
    local rndis_str = gateway_state.rndis_active == true and "🟢 已打开" or gateway_state.rndis_active == false and "⚪ 已关闭" or "未知"
    local data_str = gateway_state.data_active == true and "🟢 已打开" or gateway_state.data_active == false and "⚪ 已关闭" or "未知"
    local csq = mobile and mobile.csq and mobile.csq() or "未知"
    local rsrp = mobile and mobile.rsrp and mobile.rsrp() or "未知"
    local temp = model.temp() or "未知"
    local vbat = model.vbat() or "未知"
    local info_lines = {
        string.format("设备型号：%s", model.bsp() or "未知"),
        string.format("唯一IMEI：%s", model.imei() or "未知"),
        string.format("号码：%s", num_str),
        string.format("信号：CSQ %s (RSRP %s dBm)", csq, rsrp),
        string.format("版本：%s (v%s)", model.os(), VERSION),
        string.format("温度：%s ℃", temp),
        string.format("电压：%s V", vbat),
        string.format("USB共享：%s", rndis_str),
        string.format("蜂窝网络：%s", data_str)
    }
    return table.concat(info_lines, "\r\n")
end
local function trigger_gateway_ready()
    if gateway_state.boot_notified then return end
    gateway_state.boot_notified = true
    local report_text = build_status_report()
    local raw_num = mobile and mobile.number and mobile.number() or nil
    serial_comm.publish("gateway_ready", {
        bsp = model.bsp() or "未知",
        model = model.bsp() or "未知",
        imei = model.imei(),
        iccid = model.iccid(),
        number = raw_num,
        formatted_number = format_phone_number(raw_num),
        csq = mobile and mobile.csq and mobile.csq() or nil,
        rsrp = mobile and mobile.rsrp and mobile.rsrp() or nil,
        temp = model.temp(),
        vbat = model.vbat(),
        temp_valid = model.temp_valid(),
        vbat_valid = model.vbat_valid(),
        rndis = gateway_state.rndis_active,
        cellular_data = gateway_state.data_active,
        version = VERSION,
        build_id = BUILD_ID,
        fota_capability = fota_service.get_status().capability,
        capabilities = {
            volte = (cc ~= nil),
            chip = model.chip(),
            bsp = model.bsp(),
            fota = fota_service.get_status().capability
        },
        report_text = report_text
    })
    sys.publish("NOTIFY_PUSH", "boot", "", report_text)
    if fskv and fskv.get("fota_just_updated") then
        local updated_ver = fskv.get("fota_just_updated")
        fskv.del("fota_just_updated")
        sys.timerStart(function()
            if tostring(updated_ver) == tostring(VERSION) then
                serial_comm.publish("fota_status", { status = "verified", target_version = updated_ver, current_version = VERSION, build_id = BUILD_ID })
                sys.publish("NOTIFY_PUSH", "fota_verified", "", string.format("网关重启后已回读并确认目标版本 v%s。", tostring(updated_ver)))
            else
                serial_comm.publish("fota_status", { status = "unknown", capability = "unknown", target_version = updated_ver, current_version = VERSION, reason = "version_readback_mismatch" })
            end
        end, 3000)
    end
end
sys.subscribe("SIM_IND", function(status, value)
    log.info("sim", "SIM_IND status:", status, "val:", value)
    if status == "RDY" then
        log.info("sim", "SIM card ready, cellular signal available...")
        gateway_state.net_ready = true
        if led and led.network then
            led.network(1)
        end
        if not gateway_state.boot_notified then
            sys.timerStart(trigger_gateway_ready, 1500)
        else
            sys.timerStart(function()
                serial_comm.publish("status", {
                    csq = mobile and mobile.csq and mobile.csq() or nil,
                    rsrp = mobile and mobile.rsrp and mobile.rsrp() or nil,
                    net_ready = true,
                    temp = model.temp(),
                    vbat = model.vbat(),
                    rndis = gateway_state.rndis_active,
                    cellular_data = gateway_state.data_active
                })
            end, 1500)
        end
    end
end)
sys.subscribe("IP_READY", function(ip, adapter)
    log.info("net", "Cellular IP_READY:", ip, "adapter:", adapter)
    gateway_state.net_ready = true
    gateway_state.ip_ready = true
    local dns_list = config and config.network and config.network.dns
    if dns_list and #dns_list > 0 then
        for i, ns in ipairs(dns_list) do
            socket.setDNS(nil, i, ns)
        end
    end
    if led and led.network then
        led.network(1)
    end
    trigger_gateway_ready()
end)
sys.subscribe("IP_LOSE", function()
    log.warn("net", "Cellular IP_LOSE detected")
    gateway_state.ip_ready = false
    gateway_state.net_ready = false
    gateway_state.dis_count = gateway_state.dis_count + 1
    if led and led.network then
        led.network(0)
    end
end)
sys.subscribe("SERIAL_CMD", function(cmd_packet)
    if not cmd_packet or type(cmd_packet) ~= "table" then return end
    if cmd_packet.cmd == "get_status" then
        local rb_stat = reboot_service.get_status()
        local registration = mobile and mobile.status and mobile.status() or nil
        local registered = nil
        if registration ~= nil then
            registered = registration == 1 or registration == 5 or registration == 6 or registration == 7
        end
        serial_comm.send_response(cmd_packet.id, 0, "STATUS_OK", {
            bsp = model.bsp(),
            model = model.bsp(),
            imei = model.imei(),
            sn = model.sn(),
            iccid = model.iccid(),
            csq = mobile and mobile.csq and mobile.csq() or nil,
            rsrp = mobile and mobile.rsrp and mobile.rsrp() or nil,
            temp = model.temp(),
            vbat = model.vbat(),
            temp_valid = model.temp_valid(),
            vbat_valid = model.vbat_valid(),
            rndis = gateway_state.rndis_active,
            cellular_data = gateway_state.data_active,
            net_ready = registered,
            registration = registration,
            ip_ready = gateway_state.ip_ready,
            version = VERSION,
            build_id = BUILD_ID,
            number = mobile and mobile.number and mobile.number() or nil,
            protocol_version = "1.1",
            capabilities = { history_cursor = true, sms_event_id = true,
                notify_claim = true, fota = fota_service.get_status().capability,
                volte = (cc ~= nil), chip = model.chip(), bsp = model.bsp() },
            model_caps = model.capabilities and model.capabilities() or nil,
            blackbox_count = storage_service.get_count(),
            uptime_seconds = rb_stat.uptime_seconds,
            daily_reboot_hour = rb_stat.daily_reboot_hour,
            daily_reboot_desc = rb_stat.next_reboot_desc,
            store_on_board = (fskv and fskv.get("store_on_board")) ~= 0 and 1 or 0,
            lua_mem_kb = math.floor(collectgarbage("count"))
        })
    elseif cmd_packet.cmd == "get_uptime" then
        local rb_stat = reboot_service.get_status()
        serial_comm.send_response(cmd_packet.id, 0, "UPTIME_OK", rb_stat)
    elseif cmd_packet.cmd == "set_storage_policy" then
        local data = cmd_packet.data or cmd_packet.params or {}
        local val = data.store_on_board
        local store_on_board = (val == 0 or val == false or val == "0") and 0 or 1
        if fskv then fskv.set("store_on_board", store_on_board) end
        log.info("main", "store_on_board policy set to:", store_on_board)
        serial_comm.send_response(cmd_packet.id, 0, "STORAGE_POLICY_UPDATED", {
            store_on_board = store_on_board
        })
    elseif cmd_packet.cmd == "set_reboot_policy" then
        local data = cmd_packet.data or cmd_packet.params or {}
        local hour_val = data.hour
        if hour_val == nil then hour_val = data.hours end
        local updated = reboot_service.set_policy(hour_val)
        serial_comm.send_response(cmd_packet.id, 0, "REBOOT_POLICY_UPDATED", updated)
    elseif cmd_packet.cmd == "reboot" then
        local data = cmd_packet.data or cmd_packet.params or {}
        serial_comm.send_response(cmd_packet.id, 0, "REBOOT_COMMENCED", {
            msg = "SoC soft reboot scheduled in 1 second..."
        })
        reboot_service.trigger_reboot(data.reason or "manual_cmd", 1000)
    elseif cmd_packet.cmd == "set_notify_config" then
        local data = cmd_packet.data or cmd_packet.params or {}
        local succ = notify_service.save_config(data)
        log.info("main", "set_notify_config executed, succ:", succ)
        serial_comm.send_response(cmd_packet.id, succ and 0 or -1, succ and "NOTIFY_CONFIG_PERSISTED" or "FAILED", {
            synced = succ,
            storage = "fskv",
            config = notify_service.get_masked_config()
        })
    elseif cmd_packet.cmd == "get_notify_config" then
        local cur_cfg = notify_service.get_masked_config()
        serial_comm.send_response(cmd_packet.id, 0, "OK", cur_cfg or {})
    elseif cmd_packet.cmd == "set_cellular_data" then
        local data = cmd_packet.data or cmd_packet.params or {}
        local enable = data.enable == true or data.enable == 1 or data.enable == "true" or data.enable == "on"
        local changed = (gateway_state.data_active ~= enable)
        set_cellular_data_persisted(enable)
        gateway_state.data_active = enable
        log.info("main", "Cellular data toggled to:", enable, "changed:", changed)
        serial_comm.send_response(cmd_packet.id, 0, "CELLULAR_DATA_UPDATED", {
            cellular_data = enable,
            msg = enable and "Cellular data ENABLED on board (HTTP requests allowed)" or "Cellular data DISABLED on board (0-traffic mode, delegated to PC broadband)"
        })
        if changed then
            local report_text = build_status_report()
            local raw_num = mobile and mobile.number and mobile.number() or nil
            serial_comm.publish("state_change", {
                bsp = model.bsp(),
                model = model.bsp(),
                imei = model.imei(),
                iccid = model.iccid(),
                number = raw_num,
                formatted_number = format_phone_number(raw_num),
                csq = mobile and mobile.csq and mobile.csq() or nil,
                rsrp = mobile and mobile.rsrp and mobile.rsrp() or nil,
                temp = model.temp(),
                vbat = model.vbat(),
                temp_valid = model.temp_valid(),
                vbat_valid = model.vbat_valid(),
                rndis = gateway_state.rndis_active,
                cellular_data = gateway_state.data_active,
                version = VERSION,
                change_type = "cellular_data",
                report_text = report_text
            })
            if enable then
                sys.timerStart(function()
                    sys.publish("NOTIFY_PUSH", "state_change", "", report_text)
                end, 2000)
            else
                sys.publish("NOTIFY_PUSH", "state_change", "", report_text)
            end
        end
    elseif cmd_packet.cmd == "set_rndis" then
        local data = cmd_packet.data or cmd_packet.params or {}
        local enable = data.enable == true or data.enable == 1 or data.enable == "true" or data.enable == "on"
        local changed = (gateway_state.rndis_active ~= enable)
        set_rndis_persisted(enable)
        gateway_state.rndis_active = enable
        if changed then
            local report_text = build_status_report()
            local raw_num = mobile and mobile.number and mobile.number() or nil
            serial_comm.publish("state_change", {
                bsp = model.bsp(),
                model = model.bsp(),
                imei = model.imei(),
                iccid = model.iccid(),
                number = raw_num,
                formatted_number = format_phone_number(raw_num),
                csq = mobile and mobile.csq and mobile.csq() or nil,
                rsrp = mobile and mobile.rsrp and mobile.rsrp() or nil,
                temp = model.temp(),
                vbat = model.vbat(),
                temp_valid = model.temp_valid(),
                vbat_valid = model.vbat_valid(),
                rndis = gateway_state.rndis_active,
                cellular_data = gateway_state.data_active,
                version = VERSION,
                change_type = "rndis",
                report_text = report_text
            })
            sys.publish("NOTIFY_PUSH", "state_change", "", report_text)
        end
        serial_comm.send_response(cmd_packet.id, 0, "RNDIS_SWITCHING", {
            enable = enable,
            reboot_required = true,
            msg = enable and "RNDIS USB adapter enabling, SoC will reboot..." or "RNDIS USB adapter disabling, SoC will reboot..."
        })
        sys.timerStart(function()
            if mobile and mobile.CONF_USB_ETHERNET then
                mobile.config(mobile.CONF_USB_ETHERNET, enable and 3 or 0)
            end
            log.info("main", "RNDIS toggled to:", enable, "rebooting SoC to reload USB profile...")
            rtos.reboot()
        end, 2000)
    end
end)
sys.timerLoopStart(function()
    if gateway_state.net_ready then
        serial_comm.publish("status", {
            csq = mobile and mobile.csq and mobile.csq() or nil,
            rsrp = mobile and mobile.rsrp and mobile.rsrp() or nil,
            temp = model.temp(),
            vbat = model.vbat(),
            temp_valid = model.temp_valid(),
            vbat_valid = model.vbat_valid(),
            rndis = gateway_state.rndis_active,
            cellular_data = gateway_state.data_active,
            lua_mem_kb = math.floor(collectgarbage("count"))
        })
    end
end, 30000)
sys.timerLoopStart(function()
    collectgarbage("collect")
end, 30000)
log.info("main", "Air780EPV Smart Gateway Bootstrapped, entering sys.run()")
-- 开机保底主动握手：开机 2 秒后主动向串口发射 gateway_ready 首帧，确保上位机无卡或搜网态均可瞬间识别
sys.timerStart(trigger_gateway_ready, 2000)
sys.run()
