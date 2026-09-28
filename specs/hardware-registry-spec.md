# 规格定义：展示透传与烧录按需芯片探测契约 (AIR-47)

## 1. 日常展示链路数据契约 (Daily Display Contract)

### 1.1 板端上报规范
- 板端 `model.lua` 继续保持如下原生逻辑：
  ```lua
  function model.bsp()
      cached_bsp = (hmeta and hmeta.model and hmeta.model()) or (rtos and rtos.bsp and rtos.bsp()) or "unknown"
      return cached_bsp
  end
  ```
- 板端在 `gateway_ready` 与状态包中上报字段：
  - `model`: 板端自省商业型号（如 `"Air780EPM"`、`"Air780E"`、`"Air780EPV"`）
  - `bsp`: 底层 BSP 标识
  - `capabilities.chip`: 原生物理芯片（如 `"EC718PM"`、`"EC618"`、`"EC718PV"` 或 `"unknown"`）

### 1.2 上位机 Hub 接收规范 (Zero Guesswork)
- `gateway_hub.py` 的 `DongleSession.handle_packet` 中：
  ```python
  # 彻底废除 if "780E" in bsp 等猜词逻辑，原样信任：
  self.meta["model"] = str(data.get("model") or data.get("bsp") or self.meta.get("model") or "Air780 Series").strip()
  self.meta["bsp"] = str(data.get("bsp") or self.meta.get("bsp") or "").strip()
  ```
- 卡槽摘要输出：
  `get_summary()` 返回的 `model` 直接等于 `self.meta["model"]`。

### 1.3 前端 Web 渲染规范
- 卡片头部大标题：直接渲染 `slot.model`；
- 运营商徽标与号码展示：直接使用 `slot.model`；
- 功能按钮权限：呼叫按钮严格由 `slot.capabilities.volte === true` 决定，完全不凭型号名猜测。

---

## 2. 烧录按需现场芯片探测契约 (Flasher JIT Detection Contract)

### 2.1 触发时机
- **场景 A**：用户在正常卡槽卡片上点击【⚡ 固件线刷】打开 `modalFlashtool`；
- **场景 B**：用户在 Web 顶部未分配横幅点击【🚀 开箱装机向导】；
- **场景 C**：上位机运行 `firmware_flasher.py` CLI 命令行烧录。

### 2.2 现场芯片推导确定性规则 (Deterministic JIT Resolution)
进入烧录流程时，系统按以下严格的“三级梯队”获取真实芯片架构：

```
[进入烧录上下文 (slot_id 或 target_port)]
           │
           ▼
[第一梯队：读取现场物理芯片字段 slot.capabilities.chip]
• 若 chip 为有效代号 (大小写无关比较):
    "EC718PM" ➔ 立即锁定 "ec718pm"
    "EC718PV" / "EC718P" ➔ 立即锁定 "ec718pv"
    "EC618"   ➔ 立即锁定 "ec618"
• 命中后：直接进入装配，跳过后续步骤。
           │ (仅当第一梯队为 "unknown" 或全空时向下回退)
           ▼
[第二梯队：全词精准匹配兜底字典 (EXACT MATCH)]
• 输入端前置清洗：去除 "合宙" 前缀、去除首尾空格、转换为大写
• 仅对清洗后 model / bsp 做大写精准全等对比 (大写字典查表，0 模糊遍历):
    "AIR780EPM": "ec718pm"
    "AIR780EPV": "ec718pv"
    "AIR780EP":  "ec718pv"
    "AIR780E":   "ec618"
    "AIR780EC":  "ec618"
    "AIR780EG":  "ec618"
    "AIR700E":   "ec618"
• 命中后：直接返回对应 chip_family。
           │ (仅当全新未刷机、无卡槽信息的裸端口时向下回退)
           ▼
[第三梯队：全新出厂 AT 模组伴生口嗅探]
• 串口发送 "ATI\r\n"，读取原厂响应并提取关键词：
    包含 "780EPM" ➔ "ec718pm"
    包含 "780EP"  ➔ "ec718pv"
    包含 "780E" / "618" ➔ "ec618"
• 兜底：若均为未知，默认选 "ec718pv" 并给出警报。
```

### 2.3 终端可观测性契约
在 `web/index.html` 打开烧录弹窗时，控制台必须实时打印真实探测结果：
```
[SYSTEM] 移芯通用物理线刷底座就绪 (FlashToolCLI)
[物理芯片探测] 目标卡槽 slot_3 · 现场探测芯片: EC718PM · 自动装配底包: LuatOS-SoC_V2050_Air780EPM_103.soc
[安全检查] 脚本基地址锁定 0x279000 · 完好保护 RF 校准表 (保留 0x3f2000)
```

---

## 3. 存储与历史数据解耦契约

在 `storage_manager.py:derive_operator_and_badge` 中：
- 彻底删除 `raw_model = "Air780E" if slot == "slot_2" else "Air780EPV"`；
- 改为直接使用调用端传入的真实 `model`；若全空，使用中立的 `"Air780"` 作为兜底文本。
