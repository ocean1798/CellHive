local M = {}
local config = require "config"
local model = require "model"
local serial_comm = require "serial_comm"
local current_config = nil
local function is_enabled(val)
    return val == true or val == 1 or val == "1" or val == "true"
end

local function masked_config_value(value)
    if type(value) ~= "table" then return value end
    local result = {}
    for key, item in pairs(value) do
        if tostring(key):lower():find("url", 1, true)
            or tostring(key):lower():find("endpoint", 1, true)
            or tostring(key):lower():find("key", 1, true)
            or tostring(key):lower():find("secret", 1, true)
            or tostring(key):lower():find("token", 1, true)
            or tostring(key):lower():find("password", 1, true)
            or tostring(key):lower():find("credential", 1, true) then
            result[key] = (item and tostring(item) ~= "") and "••••" or ""
        elseif type(item) == "table" then
            result[key] = masked_config_value(item)
        else
            result[key] = item
        end
    end
    return result
end
function M.load_config()
    if fskv then
        local stored = fskv.get("notify_cfg")
        if stored and type(stored) == "string" and #stored > 0 then
            local succ, obj = pcall(json.decode, stored)
            if succ and type(obj) == "table" then
                current_config = obj
                return current_config
            end
        end
    end
    current_config = config.notify or {}
    return current_config
end
function M.save_config(cfg_table)
    if type(cfg_table) ~= "table" then return false end
    if not fskv then return false end
    local encoded, value = pcall(json.encode, cfg_table)
    if not encoded or not value then return false end
    local written, saved = pcall(fskv.set, "notify_cfg", value)
    if not written or not saved then return false end
    current_config = cfg_table
    return true
end
function M.get_config()
    return current_config or M.load_config()
end
function M.get_masked_config()
    return masked_config_value(M.get_config() or {})
end
local function format_message(msg_type, from, text, extra)
    local title, plain_text, md_text = "", "", ""
    local dev_label = (model and model.device_label and model.device_label()) or (model and model.bsp and model.bsp()) or "Air780"
    local dev_tail = "\r\n\r\n设备: " .. dev_label .. " （4G蜂窝直推）"
    local md_tail = "\n\n> **设备来源**: " .. dev_label .. " （4G蜂窝直推）"
    local sender = from or "未知"
    local content = text or ""
    if msg_type == "sms" then
        local has_otp = (extra and #extra > 0)
        title = has_otp and "🔑 收到短信验证码" or "📩 收到新短信"
        local otp_s = has_otp and ("\r\n\r\n验证码:\r\n" .. extra) or ""
        local otp_m = has_otp and ("\n\n**验证码：**\n```text\n" .. extra .. "\n```") or ""
        plain_text = "发件人: " .. sender .. otp_s .. "\r\n\r\n短信原文:\r\n" .. content
        md_text = "### " .. title .. "\n> **发件人**: " .. sender .. otp_m .. "\n\n**短信原文**:\n" .. content
    elseif msg_type == "call" then
        title = "📞 拦截到呼入电话"
        plain_text = "呼入号码: " .. sender .. "\r\n拦截处理: 已自动拒接"
        md_text = "### " .. title .. "\n> **呼入号码**: " .. sender .. "\n> **拦截处理**: 已自动拒接"
    elseif msg_type == "boot" then
        title = "🚀 智能通信网关已上线"
        plain_text = content
        md_text = "### " .. title .. "\n```\n" .. content .. "\n```"
    elseif msg_type == "state_change" then
        title = "⚙️ 网关配置状态变更"
        plain_text = content
        md_text = "### " .. title .. "\n```\n" .. content .. "\n```"
    elseif msg_type == "reboot" then
        title = "🔄 智能网关执行自愈重启"
        plain_text = content ~= "" and content or "定时自愈周期到达，正在复位系统..."
        md_text = "### " .. title .. "\n> **自愈说明**: " .. plain_text
    elseif msg_type == "fota" then
        title = "⚡ 固件空中热更新 (FOTA)"
        plain_text = content
        md_text = "### " .. title .. "\n> **操作来源**: " .. sender .. "\n\n" .. content
    elseif msg_type == "fota_success" then
        title = "🎉 固件空中热更新完成"
        plain_text = content
        md_text = "### " .. title .. "\n\n" .. content
    else
        title = "🔔 网关系统通知"
        plain_text = content
        md_text = "### " .. title .. "\n" .. content
    end
    return title, plain_text .. dev_tail, md_text .. md_tail
end
local function push_feishu(cfg, title, plain_text)
    return json.encode({
        msg_type = "post",
        content = { post = { zh_cn = { title = title, content = { { { tag = "text", text = plain_text } } } } } }
    })
end
local function push_wecom(cfg, title, plain_text, md_text)
    return json.encode({ msgtype = "markdown", markdown = { content = md_text } })
end
local function push_dingtalk(cfg, title, plain_text, md_text)
    return json.encode({ msgtype = "markdown", markdown = { title = title, text = md_text } })
end
local function push_bark(cfg, title, plain_text, extra)
    local bsp_name = (model and model.bsp and model.bsp()) or "Air780"
    local payload = { title = title, body = plain_text, group = cfg.group or bsp_name, sound = cfg.sound or "minuet" }
    if extra and #extra > 0 then payload.copy = extra end
    return json.encode(payload)
end
local function push_webhook(cfg, msg_type, from, text, extra)
    local dev_name = (model and model.bsp and model.bsp()) or "Air780"
    local imei_str = (model and model.imei and model.imei()) or ""
    return json.encode({
        device = dev_name,
        imei = imei_str,
        type = msg_type,
        from = from,
        content = text,
        otp = extra or "",
        timestamp = os.time(),
        source_mode = "cellular_direct"
    })
end
local function dispatch_channel(name, url, post_body, msg_id)
    if not url or #url == 0 or not post_body or #post_body == 0 then return end
    sys.taskInit(function()
        local headers = { ["Content-Type"] = "application/json; charset=utf-8" }
        local code, _, body = http.request("POST", url, headers, post_body).wait()
        local status = "UNKNOWN"
        local reason = "business_receipt_unavailable"
        if not code or code < 0 then
            reason = "transport_result_unknown"
        elseif code ~= 200 then
            status = "FAILED"
            reason = "HTTP_" .. tostring(code)
        elseif body and #body > 0 then
            local ok, result = pcall(json.decode, body)
            if ok and type(result) == "table" then
                local accepted = (name == "feishu" and result.code == 0)
                    or ((name == "wecom" or name == "dingtalk") and result.errcode == 0)
                    or (name == "bark" and result.code == 200)
                    or (name == "webhook" and (result.success == true or result.status == "ok"))
                if accepted then
                    status = "DELIVERED"
                    reason = "business_receipt_ok"
                elseif result.code ~= nil or result.errcode ~= nil or result.success == false then
                    status = "FAILED"
                    reason = "business_receipt_failed"
                end
            end
        end
        serial_comm.publish("notify_status", {
            id = msg_id,
            channel = name,
            status = status,
            reason = reason,
            http_code = code
        })
    end)
end
local pending_pushes = {}
local function execute_board_push(item)
    if not item then return end
    if not _G.is_cellular_data_enabled or not _G.is_cellular_data_enabled() then
        serial_comm.publish("notify_status", { id = item.id, channel = "board", status = "SKIPPED", reason = "cellular_data_disabled" })
        return
    end
    local notify_cfg = M.get_config()
    if not notify_cfg then return end
    local title, plain_text, md_text = format_message(item.type, item.from, item.content, item.extra)
    if notify_cfg.feishu and is_enabled(notify_cfg.feishu.enable) then
        dispatch_channel("feishu", notify_cfg.feishu.url, push_feishu(notify_cfg.feishu, title, plain_text), item.id)
    end
    if notify_cfg.wecom and is_enabled(notify_cfg.wecom.enable) then
        dispatch_channel("wecom", notify_cfg.wecom.url, push_wecom(notify_cfg.wecom, title, plain_text, md_text), item.id)
    end
    if notify_cfg.dingtalk and is_enabled(notify_cfg.dingtalk.enable) then
        dispatch_channel("dingtalk", notify_cfg.dingtalk.url, push_dingtalk(notify_cfg.dingtalk, title, plain_text, md_text), item.id)
    end
    if notify_cfg.bark and is_enabled(notify_cfg.bark.enable) then
        dispatch_channel("bark", notify_cfg.bark.url, push_bark(notify_cfg.bark, title, plain_text, item.extra), item.id)
    end
    if notify_cfg.webhook and is_enabled(notify_cfg.webhook.enable) then
        dispatch_channel("webhook", notify_cfg.webhook.url, push_webhook(notify_cfg.webhook, item.type, item.from, item.content, item.extra), item.id)
    end
end
local function on_push_timeout(msg_id)
    local item = pending_pushes[msg_id]
    if not item then return end
    pending_pushes[msg_id] = nil
    serial_comm.publish("notify_status", { id = msg_id, channel = "board", status = "FALLBACK", reason = "no_host_claim" })
    execute_board_push(item)
end
function M.handle_ack(msg_id, status)
    local item = pending_pushes[msg_id]
    if not item then return false end
    if item.timer_id then sys.timerStop(item.timer_id) end
    pending_pushes[msg_id] = nil
    if status == "fallback" then
        execute_board_push(item)
    elseif status == "ok" or status == "handled" then
        serial_comm.publish("notify_status", { id = msg_id, channel = "board", status = "ACKED", reason = "board_ack" })
    else
        serial_comm.publish("notify_status", { id = msg_id, channel = "board", status = "UNKNOWN", reason = "board_ack_unknown" })
    end
    return true
end
function M.init()
    M.load_config()
    sys.subscribe("NOTIFY_PUSH", function(msg_type, from, content, extra, msg_id)
        msg_id = msg_id or string.format("msg_%d_%d", os.time(), math.random(1000, 9999))
        local item = { id = msg_id, type = msg_type, from = from, content = content, extra = extra }
        pending_pushes[msg_id] = item
        item.timer_id = sys.timerStart(on_push_timeout, 5000, msg_id)
    end)
    sys.subscribe("SERIAL_CMD", function(cmd_packet)
        if cmd_packet.cmd == "notify_ack" then
            local data = cmd_packet.data or cmd_packet.params or {}
            local msg_id = data.id or cmd_packet.id
            local status = data.status or "ok"
            local accepted = msg_id and M.handle_ack(msg_id, status)
            serial_comm.send_response(cmd_packet.id, accepted and 0 or -409,
                accepted and (status == "handled" and "NOTIFY_CLAIMED" or "NOTIFY_ACKED") or "NOTIFY_CLAIM_EXPIRED",
                { id = msg_id, status = status })
        end
    end)
end
return M
