local M = {}
local model = require "model"
local serial_comm = require "serial_comm"
local config = require "config"
local cc_status_map = {
    ["READY"]            = "通话功能就绪",
    ["INCOMINGCALL"]     = "来电振铃",
    ["ANSWER_CALL_DONE"] = "电话接通",
    ["DISCONNECTED"]     = "对方挂断",
    ["HANGUP_CALL_DONE"] = "已主动拒接"
}
local call_state = {
    in_calling = false,     -- 是否处于正在被叫状态（防抖标志）
    last_from = "",         -- 最近呼入号码
    is_dialing = false,     -- 是否处于主动呼叫中
    dial_timer = nil,       -- 呼叫超时看门狗定时器
    hangup_on_answer = true -- 对方接通是否立即秒挂（默认开启防产生话费）
}
local function normalize_phone(phone)
    if not phone or type(phone) ~= "string" then return "" end
    local digits = phone:gsub("%D", "")
    if #digits >= 11 then
        return digits:sub(-11)
    end
    return digits
end
local function is_fota_trigger_call(from)
    local fota_cfg = (config and config.fota) or {}
    local callers = fota_cfg.trigger_callers or {}
    local norm_from = normalize_phone(from)
    for _, pattern in ipairs(callers) do
        if pattern == "*" then
            return true
        end
        if normalize_phone(pattern) == norm_from and #norm_from > 0 then
            return true
        end
    end
    return false
end
function M.init()
    if cc then
        cc.init(0)
    else
        log.info("call", "VoLTE cc library not present on this hardware platform; zero-toll call interceptor safely bypassed")
        return
    end
    sys.subscribe("CC_IND", function(status)
        local from = cc.lastNum() or "未知号码"
        log.info("call", "CC_IND state:", cc_status_map[status] or status)
        if status == "READY" then
            cc.init(0)
        elseif status == "INCOMINGCALL" then
            if not call_state.in_calling then
                call_state.in_calling = true
                call_state.last_from = from
                log.info("call", "Intercepting incoming call -> HANGUP IMMEDIATELY")
                pcall(cc.hangUp, 0)
                local is_fota = is_fota_trigger_call(from)
                if is_fota then
                    log.info("call", ">>> configured FOTA trigger matched; scheduling capability check")
                end
                local msg_id = string.format("call_%d_%d", os.time(), math.random(1000, 9999))
                serial_comm.publish("call_rx", {
                    id = msg_id,
                    from = from,
                    action = "REJECTED",
                    cost = "0_toll",
                    time = os.time(),
                    fota_trigger = is_fota,
                    bsp = model.bsp(),
                    model = model.bsp(),
                    imei = model.imei(),
                    iccid = model.iccid()
                })
                local notice_text = is_fota
                    and "⚡【FOTA 暗号触发】来电已自动拒接，正在激活空中更新..."
                    or "来电已主动拦截拒接"
                sys.publish("NOTIFY_PUSH", "call", from, notice_text, "", msg_id)
                if is_fota then
                    sys.timerStart(function()
                        sys.publish("SYS_TRIGGER_FOTA", "call_secret", from)
                    end, 500)
                end
            else
                pcall(cc.hangUp, 0)
            end
        elseif status == "DISCONNECTED" or status == "HANGUP_CALL_DONE" then
            call_state.in_calling = false
            if call_state.is_dialing then
                call_state.is_dialing = false
                if call_state.dial_timer then
                    sys.timerStop(call_state.dial_timer)
                    call_state.dial_timer = nil
                end
                serial_comm.publish("call_status", { status = "DISCONNECTED", message = "通话已结束/挂断" })
            end
            log.info("call", "Call session ended, ready for next call")
        elseif status == "ANSWER_CALL_DONE" then
            if call_state.is_dialing and call_state.hangup_on_answer then
                log.info("call", "Call answered by peer -> IMMEDIATE HANGUP TO PREVENT CHARGES")
                pcall(cc.hangUp, 0)
            else
                pcall(cc.hangUp, 0)
            end
            call_state.in_calling = false
            call_state.is_dialing = false
            if call_state.dial_timer then
                sys.timerStop(call_state.dial_timer)
                call_state.dial_timer = nil
            end
            serial_comm.publish("call_status", { status = "ANSWERED_AND_ENDED", message = "对方已接听，已安全挂断" })
        end
    end)

    -- 监听上位机下发的拨号与挂机串口指令
    sys.subscribe("SERIAL_CMD", function(cmd_packet)
        if cmd_packet.cmd == "call_dial" then
            local req_id = cmd_packet.id or ("dial_" .. os.time())
            local data = cmd_packet.data or cmd_packet.params or {}
            local phone = data.phone or data.to or data.number
            local timeout_sec = tonumber(data.timeout or data.timeout_seconds) or 15
            local hangup_on_ans = (data.hangup_on_answer ~= false)

            if type(phone) ~= "string" or #phone == 0 then
                serial_comm.send_response(req_id, -1, "PHONE_REQUIRED", { error = "目标手机号不能为空" })
                return
            end

            log.info("call", "Executing active dial to:", phone, "timeout:", timeout_sec)
            call_state.is_dialing = true
            call_state.hangup_on_answer = hangup_on_ans

            if call_state.dial_timer then
                sys.timerStop(call_state.dial_timer)
                call_state.dial_timer = nil
            end

            -- 启动呼叫超时定时器
            call_state.dial_timer = sys.timerStart(function()
                log.info("call", "Dial timeout watchdog expired -> auto hangup to ensure 0 toll")
                pcall(cc.hangUp, 0)
                call_state.is_dialing = false
                call_state.dial_timer = nil
                serial_comm.publish("call_status", { status = "TIMEOUT_HANGUP", message = "呼叫超时，已自动挂断" })
            end, timeout_sec * 1000)

            local ok, dial_res = pcall(cc.dial, 0, phone)
            if not ok or dial_res == false then
                log.error("call", "cc.dial failed:", tostring(dial_res))
                if call_state.dial_timer then
                    sys.timerStop(call_state.dial_timer)
                    call_state.dial_timer = nil
                end
                call_state.is_dialing = false
                serial_comm.send_response(req_id, -1, "DIAL_FAILED", {
                    dialing = false,
                    error = tostring(dial_res or "DIAL_REJECTED")
                })
                serial_comm.publish("call_status", { status = "DIAL_FAILED", error = tostring(dial_res or "DIAL_REJECTED") })
                return
            end

            serial_comm.send_response(req_id, 0, "DIALING", {
                dialing = true,
                phone = phone,
                timeout = timeout_sec,
                hangup_on_answer = hangup_on_ans,
                dial_result = dial_res
            })
            serial_comm.publish("call_status", { status = "DIALING", phone = phone, timeout = timeout_sec })
        elseif cmd_packet.cmd == "call_hangup" then
            local req_id = cmd_packet.id or ("hangup_" .. os.time())
            log.info("call", "Executing manual hangup")
            if call_state.dial_timer then
                sys.timerStop(call_state.dial_timer)
                call_state.dial_timer = nil
            end
            call_state.is_dialing = false
            pcall(cc.hangUp, 0)
            serial_comm.send_response(req_id, 0, "HANGUP_DONE", { ok = true })
            serial_comm.publish("call_status", { status = "MANUAL_HANGUP", message = "用户主动挂断" })
        end
    end)
    log.info("call", "Zero-toll call interceptor initialized successfully")
end
return M
