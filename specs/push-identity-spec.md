# 规范契约：多卡多渠道消息推送业务卡身份与模板规范 (AIR-54)

## 一、业务卡身份解析器契约 (Identity Resolution Contract)

上位机系统需为每个卡槽（Slot）提取统一的、全自动的大白话业务卡身份元数据：

```json
{
  "slot_id": "slot_3",
  "slot_cn": "卡槽 3",
  "port": "COM10",
  "carrier": "中国联通",
  "carrier_short": "联通",
  "phone_masked": "132****3879",
  "phone_tail": "3879",
  "model": "Air780E",
  "imei": "861551056135833",
  "display_tag": "【中国联通 3879】",
  "device_desc": "[卡槽 3 · COM10] Air780E"
}
```

### 1. 字段自动推导规则（零脑补 · 纯客观真实数据）
1. **槽位中文名 (Slot CN)**：
   - 将 `slot_1` / `slot_2` / `slot_3` 规范转换为人话 `卡槽 1` / `卡槽 2` / `卡槽 3`。
2. **运营商推导优先级 (Carrier)**：
   - **优先级 1（ICCID 查表推导）**：
     提取 `session.meta.get("iccid")` 或 `data.get("iccid")`，调用已有的 `detect_sim_carrier(iccid)`。若命中返回代码，通过映射字典转换为标准中文：
     `"cucc"` ➔ `("中国联通", "联通")`、`"cmcc"` ➔ `("中国移动", "移动")`、`"ctcc"` ➔ `("中国电信", "电信")`、`"cbn"` ➔ `("中国广电", "广电")`；
   - **优先级 2（手机号段与客服号推导）**：
     若 ICCID 为空或未识别，直接复用 `cluster_router.detect_phone_carrier(phone)` 模块函数推导运营商；
   - **优先级 3（短信内容头部签名推导）**：
     若前两级均未命中且传入了短信内容，根据短信内容头部签名特征（如 `【中国联通】`、`【中国移动】`、`【中国电信】`、`【中国广电】`）精确推导；
   - **优先级 4（兜底）**：若仍无法推导，运营商置为空字符串。
3. **本机号码安全清洗与尾号提取 (Phone)**：
   - 提取数字串 `digits = re.sub(r"\D", "", raw_phone)`；
   - 若含国家码 `86` 且长度为 13 位，裁剪前缀保留 11 位；
   - 长度防呆门禁：
     - 若 `len(digits) >= 11`：`phone_masked = f"{digits[:3]}****{digits[-4:]}"`，`phone_tail = digits[-4:]`；
     - 若 `7 <= len(digits) < 11`：`phone_masked = f"{digits[:2]}****{digits[-2:]}"`，`phone_tail = digits[-4:]`；
     - 若 `len(digits) < 7`：`phone_masked = digits`，`phone_tail = ""`。
4. **端口防呆 (Port)**：
   - `port_suffix = f" · {port}" if port else ""`；
   - 组合设备描述：`device_desc = f"[{slot_cn}{port_suffix}] {model}"`。

---

## 二、4 级优雅降级阶梯 (Graceful Degradation Ladder)

外层标题标签 `display_tag` 严格遵循以下 4 级全自动降级阶梯，杜绝任何空洞占位符：

| 级别 | 硬件具备条件 | 标题标签输出示例 (display_tag) | 说明 |
| :--- | :--- | :--- | :--- |
| **L1 (双全 · 最优)** | 具有运营商 + 具有手机号 | `【中国联通 3879】` | 最常见场景，一眼识别是哪张卡 |
| **L2 (流量卡/未写号)** | 具有运营商，无手机号 | `【中国移动 (卡槽 1)】` | 保证运营商清晰，辅以卡槽定位 |
| **L3 (仅读取到手机号)** | 无运营商，具有手机号 | `【手机卡 3879】` | 保证尾号对账 |
| **L4 (完全未知/故障)** | 无运营商，无手机号 | `【卡槽 2 · Air780EPM】` | 保证硬件槽位兜底排查 |

---

## 三、各推送渠道模板契约 (Push Channel Templates)

### 核心排布原则：各司其职，彻底消灭正文重复行！
- **外层标题**：负责交代“哪张卡收到了什么”；
- **内层正文**：直接呈现发件人、时间与短信内容，**绝对不再重复塞入卡槽名称**；
- **底栏灰字**：负责硬件与端口溯源排查。

---

### 1. 飞书 (Feishu Interactive Card 2.0)
- **普通新短信标题**：`📩 {display_tag} 收到新短信`
  - 示例：`📩 【中国联通 3879】 收到新短信`
- **验证码新短信标题**：`🔑 {display_tag} 收到短信验证码`
  - 示例：`🔑 【中国联通 3879】 收到短信验证码`
- **卡片正文（直接看发件人与时间，杜绝重复卡槽行）**：
  `**发件人：** `{sender}`\n**接收时间：** {time_str}`
- **验证码高亮（若有）**：
  `**提取验证码：**\n```text\n{code}\n```\n---`
- **短信正文**：
  `**短信正文：**\n{content}`
- **底栏灰字（排查溯源）**：
  `<font color='grey'>来源设备: {device_desc} (上位机推送)</font>`

### 2. 企业微信 (WeCom Markdown) & 钉钉 (DingTalk Markdown)
- **标题**：
  - 普通短信：`### 📩 {display_tag} 收到新短信`
  - 验证码短信：`### 🔑 {display_tag} 收到短信验证码`
- **内容（清爽直接）**：
  ```markdown
  > **发件人**: {sender}
  > **接收时间**: {time_str}
  > **提取验证码**: <font color="warning">{code}</font>  (若有)
  > **短信正文**: {content}

  > **来源设备**: {device_desc} (上位机推送)
  ```

### 3. Bark (iOS 通知推送)
- **Title (通知标题)**：
  - 普通短信：`📩 {display_tag}`（例：`📩 【中国联通 3879】`）
  - 验证码短信：`🔑 {display_tag}`
- **Body (通知正文)**：
  ```text
  发件人: {sender}
  内容: {content}
  🔑 提取验证码: 【{code}】 (若有)

  设备: {device_desc} (上位机推送)
  ```

### 4. 电话拦截与呼叫事件
- **普通拦截标题**：`📞 {display_tag} 拦截到呼入电话`
- **空中升级暗号标题**：`⚡ {display_tag} 识别暗号呼叫：激活空中更新`
- **正文**：展示呼入号码、处理动作与接收时间。

### 5. 通用 Webhook
在保持既有字段 `event`, `slot`, `port`, `model`, `imei`, `device_desc`, `timestamp`, `data` 不变的前提下，顶层增量扩充标准业务字段：
- `carrier`: 运营商全称（如 `"中国联通"`）
- `phone`: 本机号码脱敏字符串（如 `"132****3879"`）
- `phone_tail`: 4位尾号（如 `"3879"`）
- `display_tag`: 人话标题标签（如 `"【中国联通 3879】"`）
