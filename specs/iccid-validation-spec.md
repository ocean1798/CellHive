# 规范契约：SIM卡ICCID校验与未插卡状态对账规范 (AIR-56)

## 1. 国际电信标准 ICCID 准入规范 (ITU-T E.118)

### 1.1 结构与特征
依据 ITU-T E.118 标准，SIM 卡集成电路卡标识（ICCID）的结构严格如下：
- **前 2 位（MII 主行业标识）**：固定为 `89`，代表电信行业（Telecommunications）；
- **第 3~4/5 位（CC 国家代码）**：中国固定为 `86`；
- **第 5~6/7 位（II 运营商识别码）**：
  - 中国移动：`898600`, `898602`, `898604`, `898607`, `898608`
  - 中国联通：`898601`, `898606`, `898609`
  - 中国电信：`898603`, `898605`, `898611`
  - 中国广电：`898612`, `898615`
  - 国际/境外卡：以 `89` 开头，后接其他国家代码（如 `891...`, `8944...` 等）
- **总长度**：标准为 19 位或 20 位纯十进制数字。

### 1.2 准入校验规则 (Truth Filter)
```python
def is_valid_iccid(val: Any) -> bool:
    """
    校验是否为符合 ITU-T E.118 国际电信标准的有效 ICCID：
    - 必须以 89 开头；
    - 总长度为 19 或 20 位纯数字（兼容 3GPP BCD 填充的末尾 F/f 字符）。
    """
    if not val:
        return False
    s = str(val).strip().rstrip("Ff")
    return bool(re.match(r"^89\d{17,18}$", s))
```
**一票否决门禁**：任何不以 `89` 开头、包含非数字字符、或去 F 后长度不在 19~20 位区间的字符串（包括系统时间戳、内部进程号、日志文本行），在上位机任何接收端（串口解析、伴生口探测、接口入参、前端缓存）均**直接判定为非法脏数据，一律禁止存入 `iccid` 字段**。

---

## 2. 伴生端口探测与安全防线规范

### 2.1 端口属性识别
- 模组的同物理 USB 拓扑伴生口（通常为 `x.2`）在 LuatOS 固件下往往为 **Trace/Debug 日志口**，而非纯 Modem AT 口；
- 严禁假定伴生口一定响应标准 AT 指令；
- 严禁对从伴生口读取到的任意日志文本行使用宽泛正则 `re.sub(r"\D", "", line)` 提取纯数字当作通信标识。

### 2.2 响应行安全解析守则
1. **优先匹配 LuatOS REPL 定界符**：
   - 指令：`print("FP_START", mobile and mobile.imei and mobile.imei(), mobile and mobile.iccid and mobile.iccid(), "FP_END")`
   - 提取定界符 `FP_START` 与 `FP_END` 之间的明确分段；如果为 `"nil"` 则确认为空。
2. **AT 模式兼容必须具备白名单特征与前缀门禁**：
   - 包含系统日志特征（以 `I/`、`W/`、`E/`、`D/` 开头，或包含 `CMD received:` 等字样）的行直接跳过；
   - 严格采用白名单提取：针对 `AT+ICCID`，仅解析前缀为 `+ICCID:`、`+CCID:`、`+QCCID:` 或以 `89` 开头的独立行；
   - 针对 `AT+CGSN`（IMEI），仅解析前缀为 `+CGSN:` 或 15 位数字的标准 IMEI 独立行；
   - 提取出的数字必须通过 `is_valid_iccid()` 门禁检验。
3. **单次探测与热插拔断线自愈生命周期**：
   - 卡槽建立 `_companion_probed: bool` 标志；
   - 在串口初次建立连接时触发一次探测；探测完成后置为 `True`；
   - 在设备未插卡或读不出 ICCID 的日常轮询中，**严禁每 2 秒无限重复骚扰伴生端口**；
   - 当设备发生物理断开（`is_connected = False` 或串口异常断开）时，必须将 `self._companion_probed = False` 及时重置，保证下次热插拔上线后能够自愈重测。

---

## 3. 未插卡状态对账规范

### 3.1 状态对账定义
当模组出现以下任一特征时，判定为当前未插卡：
1. 模组固件上报的 JSON 帧中无 `iccid` 字段，或 `iccid` 字段为 `""`、`nil`、`"未插卡"`；
2. 模组上报 `net_ready == false`、`csq == 0`，且当前内存中的 `iccid` 无法通过 `is_valid_iccid()` 检验。

### 3.2 脏数据清洗与物理拔卡清空契约
1. **随时清理非法数据**：一旦检测到当前卡槽的 `self.meta["iccid"]` 不满足 `is_valid_iccid()`，上位机必须立即将其重置为空字符串 `""`，不得保留任何历史幽灵卡号；
2. **物理拔卡/明确无卡态清空**：当接收到明确的模组状态帧（包含 `get_status` 或 `gateway_ready` 响应帧）时：
   - 若 `data.get("sim_ready") is False`，立即无条件清空 `self.meta["iccid"] = ""`；
   - 若帧内包含 `"iccid"` 键，且其值为空、`None`、`"未插卡"` 或无法通过 `is_valid_iccid()` 检验，立即清空 `self.meta["iccid"] = ""`；
   - 若当前帧无有效 ICCID，且模组未入网（`not data.get("net_ready", False)` 且 `(data.get("csq") or 0) == 0`，防御 None 穿透），上位机必须**无条件清空** `self.meta["iccid"] = ""`，彻底杜绝物理拔卡后原合法 ICCID 变成幽灵卡号驻留内存；
3. Web 控制台 API `/api/slots` 与 `/api/status` 输出的 `iccid` 字段在未插卡时必须为 `""`。

---

## 4. Web 控制台前端展示与行为规范

### 4.1 SIM 卡存在性判定（`hasSim`）与缓存防死锁机制
```javascript
function isValidIccid(iccid) {
  if (!iccid || typeof iccid !== 'string') return false;
  const s = iccid.trim().replace(/[Ff]$/, '');
  return /^89\d{17,18}$/.test(s);
}

// 严谨的三维 SIM 存在性判定：
// 1. 基于当前帧客观事实判定是否显式无卡（严禁引入 slotCache 导致拔卡自证死锁！）
const frameIccid = (data.iccid !== undefined && data.iccid !== null) ? String(data.iccid).trim() : null;
const frameHasValidIccid = isValidIccid(frameIccid);
const frameHasPhone = Boolean(data.phone && String(data.phone).trim().length >= 3);
const frameHasNetwork = Boolean(data.net_ready === true || (data.csq && parseInt(data.csq) > 0));

// 显式无卡：显式标记未插卡、SIM芯片未就绪，或者当前上报无卡号且完全无蜂窝驻网信号
const isExplicitNoSim = (data.iccid === "未插卡" || data.sim_ready === false || (frameIccid === "" && !frameHasNetwork));

// 2. 缓存失效防线：设备离线或显式无卡时，立即无条件清空 SIM 级缓存，彻底斩断历史幽灵卡号
if (!isOnline || isExplicitNoSim) {
  slotCache.iccid = "";
  slotCache.sim = "";
  slotCache.rawPhone = "";
  slotCache.formattedPhone = "";
  slotCache.carrier = "";
}

// 3. 当前有效性计算：必须在缓存清理后进行
const hasSim = !isExplicitNoSim && (frameHasValidIccid || frameHasNetwork || (isValidIccid(slotCache.iccid) && frameHasNetwork));
```

### 4.2 未插卡状态展示规范
- 当 `!hasSim` 时：
  - 运营商 Logo 区域呈现灰色 `NO_SIM` 占位图标，提示文字为“未检测到 SIM 卡”；
  - 信号条区域隐藏（`display: none`），状态徽章显示为灰色 `未插卡`；
  - 号码展示区隐藏，电话呼叫、发信、移动数据开关一律禁用（`disabled = true`）；
  - 绝对禁止在无卡时脑补“物联卡 (无本机号)”。

### 4.3 物联卡展示规范
- 仅在满足：
  1. `hasSim === true`（具有经 `isValidIccid` 验证通过的合法 `89...` 开头 ICCID）；
  2. 且 `rawNum === ""`（经查询确实无本机号）；
  3. 且运营商确实被识别或卡号真实在线；
  此时才合规呈现“物联卡 (无本机号)”。
