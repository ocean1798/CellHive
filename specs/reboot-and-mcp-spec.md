# 规范契约：定时重启自定义时间与 MCP 智能体交互契约 (AIR-59)

## 1. 定时重启维护软硬件契约
### 1.1 字段与物理通信
- **配置持久化字段**：
  - `daily_reboot` (bool, default: `false`)：是否开启定时重启维护；
  - `daily_reboot_hour` (int, default: `4`, range: `0~23`)：若开启，执行维护的具体整点；若未开启，向板端同步 `-1`。
- **板端状态与真实通信**：
  - 板端 `main.lua:272` 监听 `set_reboot_policy` 串口命令，入参格式为 `{"hour": target_hour}`；
  - 上位机保存配置时，若卡槽在线，显式通过后端向板卡下发 `set_reboot_policy`，计算公式为：
    `target_hour = int(daily_reboot_hour) if daily_reboot else -1`，确保软硬件 100% 同步事实，杜绝主观脑补；
  - 上位机 `gateway_web.py` 在 `GET /api/system/settings` 中返回：
    - `"daily_reboot": bool`
    - `"daily_reboot_hour": int`
  - 前端数据回显：
    - 若 `daily_reboot === false`：开关关闭，下拉框为禁用（`disabled`）且停在已存整点或 `04:00`；
    - 若 `daily_reboot === true`：开关开启，下拉框激活可选。

### 1.2 UI 呈现与排版契约
- 位于运行策略网格底部通栏（`class="strategy-item-card strategy-item-full"`）；
- 左侧：标题“定时重启维护”，副标题“定时刷新网络连接（默认关闭，保持常年在线）”；
- 右侧操作区：
  - 紧凑整点下拉框 `<select id="cfg_daily_reboot_hour">`（包含 00:00 ~ 23:00 共 24 个整点选项）；
  - 开关 `<input type="checkbox" id="cfg_daily_reboot">`；
  - 联动：开关拨动时自动切换下拉框的 `disabled` 状态。

---

## 2. MCP 智能体通信契约
### 2.1 状态与开关契约
- **彻底废除状态回显行**：物理移除 `#mcp_badge_status` 及其父级 DOM，不再输出“⚪ 未启用 (安全隔离)”或“🟢 运行中”等车轱辘废话；
- **开关语义**：
  - 开关 ID：`#cfg_mcp_enabled`；
  - 标题：“启用外部 AI 智能体通信 (MCP)”；
  - 说明：“允许 Claude Desktop / Cursor 等 AI 助手调用蜂窝模组收发短信与提取验证码（中枢端口 17800）”。

### 2.2 客户端双模接入契约
- **后端输出规范**：
  - 在 `GET /api/system/settings` 中动态输出绝对正斜杠路径：`mcp_server_path = os.path.abspath(...).replace("\\", "/")`。
- **模式 A：Agent 自动化安装 Prompt 契约**：
  - 生成标准结构化指令：
    ```text
    请帮我在当前工程/客户端配置并启用这个本地 4G 蜂窝通信 MCP 服务：
    - 服务名: air780-cellular
    - 启动命令: python "<mcp_server_path>"
    - 标准配置 (示例):
    {
      "mcpServers": {
        "air780-cellular": {
          "command": "python",
          "args": ["<mcp_server_path>"]
        }
      }
    }
    请将其写入适合当前环境的 MCP 配置文件（如 .mcp.json、.cursor/mcp.json 或 claude_desktop_config.json）并确认服务就绪。
    ```
  - 点击“📋 复制 Agent 安装指令”，一键存入剪贴板。
- **模式 B：标准 JSON 配置契约**：
  - 呈现标准的 `mcpServers` 代码框，点击“📋 复制 JSON 配置”一键存入剪贴板。
