# 合宙 780 蜂窝通信网关 - MCP 服务调用手册

本服务遵循 Anthropic FastMCP 协议规范，让外部 AI 助手（如 Claude Desktop、Cursor、Cline 等）可以直接调用你的 4G 模组，实现**自动收发短信、守候提取验证码、物理拨号报警、查验蜂窝网络看板**等能力。

---

## ⚠️ 使用前必读（防坑两步法）

1. **必须先启动上位机软件**  
   MCP 客户端依赖上位机中枢服务（运行在 `127.0.0.1:17800`）。**使用前请确保桌面版 `Air780EPV-Gateway.exe` 或本地 Web 控制台处于运行状态**。*(注：早期版本中的自动静默拉起已因防串口冲突而移除)*。
2. **在网页后台开启 MCP 权限开关（防 403 报错）**  
   出于硬件安全考虑，**出厂默认关闭外部 AI 的敏感写操作**。  
   请在网页后台右侧抽屉 **【系统设置】** 中，将 **【AI 智能体通信服务 (MCP)】** 开关打开。如果不打开，AI 发送短信、拨号呼叫、清空短信或重启模组时会被系统直接返回 `403 权限拒绝`。

---

## 🚀 极速接入配置

### 1. Claude Desktop 接入
打开配置文件（Windows 路径：`%APPDATA%\Claude\claude_desktop_config.json`），添加如下内容：

```json
{
  "mcpServers": {
    "air780-cellular": {
      "command": "python",
      "args": [
        "D:/path/to/Air780EPV-Smart-Gateway/tools/mcp_server/server.py"
      ]
    }
  }
}
```
> **提示**：请将上面的路径替换为你电脑上的实际绝对路径（如 `D:/.../tools/mcp_server/server.py`），路径分隔符建议使用正斜杠 `/` 或双反斜杠 `\\`，避免 JSON 转义报错。保存后完全退出并重启 Claude Desktop 即可。

### 2. Cursor 接入
在 Cursor 的 **Settings > Features > MCP Servers** 中点击 **Add New MCP Server**：
* **Name**: `air780-cellular`
* **Type**: `command`
* **Command**: `python E:/.../tools/mcp_server/server.py`（填写实际绝对路径）

---

## 🛠️ 工具与资源速查表

### 1. Tools 工具列表（AI 可直接调用的动作）

| 工具名称 | 核心参数与默认值 | 功能说明（大白话） |
| :--- | :--- | :--- |
| **`cellular_wait_for_otp`** | `timeout_seconds: 20`<br>`freshness_seconds: 180`<br>`slot: null` | **智能守候验证码**：优先提取过去 3 分钟内的新鲜验证码；如果没有，挂起等待最多 20 秒，短信到站瞬间毫秒级唤醒并返回纯数字，彻底解决空中传输时延问题。 |
| **`cellular_list_dongles`** | 无 | **查看模组集群列表**：探测并列出所有已接入的 4G 模组型号、卡槽编号、绑定手机号、信号强度及 VoLTE 语音能力。 |
| **`cellular_get_status`** | `slot: null` | **查看运行状态看板**：获取信号强度(CSQ/RSRP)、核心温度、供电电压、黑匣子存量、随身上网状态及开机运行时间。 |
| **`cellular_send_sms`** | `phone` (号码)<br>`content` (正文)<br>`slot: null`<br>`strategy: "operator_affinity"` | **发送短信**：驱动 4G 模组主动代发一条短信，支持按同运营商优先分流，监听基站发送回执。 |
| **`cellular_dial_phone`** | `phone` (号码)<br>`slot: null`<br>`timeout_seconds: 15`<br>`hangup_on_answer: true` | **拨打电话振铃**：用 4G VoLTE 语音拨打目标电话振铃告警。接听后立即挂断，超时自动挂断。*(仅带语音的模组支持)* |
| **`cellular_hangup_phone`** | `slot: null` | **主动挂断电话**：立即挂断当前正在进行中的电话呼叫。 |
| **`cellular_get_history`** | `limit: 20`<br>`keyword: null`<br>`slot: null` | **查看短信历史**：查阅模组板载 LittleFS 脱机黑匣子中的短信存档（断电不丢，支持关键词搜索）。 |
| **`cellular_clear_sms_history`**| `slot: null` | **清空短信历史**：清空板载脱机黑匣子中的全部短信存档。 |
| **`cellular_toggle_rndis`** | `enable: bool`<br>`slot: null` | **随身上网开关**：开启或关闭 USB 虚拟网卡 RNDIS 4G 上网功能（出厂默认关闭）。 |
| **`cellular_toggle_board_data`**| `enable: bool`<br>`slot: null` | **板载 4G 数据通信开关**：开启或切断模组自身的数据连接（出厂默认关闭，由上位机推送）。 |
| **`cellular_reboot_gateway`** | `reason: "mcp_agent_action"`<br>`slot: null` | **软复位重启**：向模组下发软复位指令，安全重启硬件。 |

### 2. Resources 资源列表（AI 可直接读取的数据）

* **`cellular://dongles`**：所有接入卡槽的硬件与网络全景数据 JSON。
* **`cellular://gateway/status`**：网关运行看板 JSON（含温度、电压、信号、开机时间）。
* **`cellular://sms/latest`**：最新一条收到的短信详情。
* **`cellular://sms/history`**：板载黑匣子全部历史短信数据流。

### 3. Prompts 提示词模板

* **`otp_verification`**：标准验证码动态监听与智能提取助手。
* **`carrier_query`**：运营商业务查询交互助手（自动向 10010/10086/10001 查话费或查流量）。

---

## ❓ 常见问题排查 (FAQ)

### Q1: AI 调用发短信或拨号时报错 `403 Permission Denied`？
- **原因**：Web 控制台中的 MCP 安全开关未打开；
- **解决**：在浏览器打开网关后台（`http://127.0.0.1:17801`），点击右上角打开【系统设置】抽屉，把【AI 智能体通信服务 (MCP)】开关切换为开启状态即可。

### Q2: Claude / Cursor 提示连接失败或拒绝连接？
- **原因**：网关上位机没有启动；
- **解决**：请先运行 `Air780EPV-Gateway.exe` 或启动本地控制台，确认后台已在 `127.0.0.1:17800` 正常提供服务。

### Q3: 守候验证码时 AI 为什么会停顿几秒才回复？
- **原因**：`cellular_wait_for_otp` 具备智能守候机制。如果过去 3 分钟内没有已到站的验证码，它会在后台静默守候最多 20 秒，一旦运营商空中基站将短信推送到模组，它会瞬间毫秒级提取并返回，这属于正常等待机制，无需重试。
