# 规范契约：系统设置、自启动与关于体系规范 (AIR-57)

## 1. Windows 开机自启契约 (Registry Contract)

### 1.1 注册表键位与权限边界
- **注册表路径**：`HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`
- **键名 (Key Name)**：`CellHiveGateway`
- **唯一真理源原则 (SSOT)**：
  - 启动项的存在与否，**100% 以注册表实时查询结果为准**；
  - 配置文件中的 `autostart` 仅作镜像，当用户在 Windows 任务管理器“启动应用”中手动禁用时，API 实时以注册表查询为准，绝不脑补假状态。
- **键值 (Value)**：
  - 运行态为单文件 Exe：`"C:\Path\To\Air780EPV-Gateway.exe" --no-browser`
  - 运行态为源码解释：`"C:\Python312\pythonw.exe" "C:\Path\To\gateway_app.py" --no-browser`
- **安全与权限约束**：
  - 严格限制在 `HKEY_CURRENT_USER` (HKCU)，绝对禁止写入 `HKEY_LOCAL_MACHINE` (HKLM)；
  - 零 UAC 提权提示，零管理员权限诉求；
  - 支持自愈校准：如果用户移动了 Exe 位置，打开设置开关时自动更新为当前最新真实路径。

### 1.2 启动参数契约
- `--no-browser` 或 `--no-window`：指示应用启动时跳过 `launch_desktop_app_window()`，仅托盘图标入驻并在 Windows 右下角轻弹一次 Toast：“数字蜂巢已在后台守护”。
- 应用启动时综合判定：`open_browser = (not args.no_window) and config.get("system", {}).get("open_browser_on_start", True)`。

---

## 2. 系统设置配置 Schema 与防重新洗牌保护 (`gateway_config.json`)

系统偏好统一挂载在 `system` 节点下，**严格遵循增量合并更新 (Patch Update)，绝对禁止整根覆盖抹除既有策略**：

```json
{
  "system": {
    "store_on_board": false,
    "daily_reboot": true,
    "auto_copy_otp": true,
    "autostart": false,
    "open_browser_on_start": true,
    "desktop_notification": true,
    "privacy_mode": false,
    "play_sound": true
  }
}
```

### 2.1 既有硬件策略保护（一票否决级底线）
- `store_on_board` (bool, default `false`): 控制模组硬件是否暂存短信（板端 `sms_service.lua` 严重依赖，必须完整保留）；
- `daily_reboot` (bool, default `true`): 每日凌晨 04:00 自动维护重启看门狗（板端 `reboot_service.lua` 依赖，必须完整保留）。

### 2.2 本次新增系统偏好
- `auto_copy_otp` (bool, default `true`): 是否将提取出的验证码自动写入 Windows 剪贴板；
- `autostart` (bool, default `false`): 是否开机自启（以注册表实时查询为准）；
- `open_browser_on_start` (bool, default `true`): 非自启的手动正常启动时，是否自动弹出 Web 客户端窗口；
- `desktop_notification` (bool, default `true`): 收到新短信时是否弹出 Windows 桌面通知；
- `privacy_mode` (bool, default `false`): 隐私防窥模式。为 `true` 时，桌面 Toast 仅提示“收到新短信（已开启防窥保护）”，正文隐藏号码与内容；
- `play_sound` (bool, default `true`): 收到验证码时是否播放轻快提示音（采用 Web Audio API 原生合成双音调正弦波，零外部音频文件依赖）。

---

## 3. Web 后端 API 契约

### 3.1 获取系统运行环境与偏好
- **请求**：`GET /api/system/settings`
- **响应**：
```json
{
  "ok": true,
  "code": 0,
  "data": {
    "version": "1.3.0",
    "build_type": "standalone_exe",
    "platform": "Windows",
    "python_version": "3.12.0",
    "store_on_board": false,
    "daily_reboot": true,
    "autostart": true,
    "auto_copy_otp": true,
    "open_browser_on_start": true,
    "desktop_notification": true,
    "privacy_mode": false,
    "play_sound": true,
    "lan_ip": "192.168.1.100",
    "lan_url": "http://192.168.1.100:17801",
    "data_dir": "C:\\Users\\...\\data",
    "log_dir": "C:\\Users\\...\\log",
    "log_size_bytes": 1048576,
    "log_size_human": "1.00 MB"
  }
}
```
- **纯单机离线优雅降级**：若未连接任何局域网（`lan_ip == "127.0.0.1"`），前端展示“当前仅供本机访问”，避免展示无效的局域网链接。

### 3.2 更新系统偏好
- **请求**：`POST /api/system/settings`
- **Payload**：支持部分字段增量传递（Patch Update）
- **业务行为**：
  - 仅更新传入的合法键值，保留未传入的既有键值（如 `store_on_board`, `daily_reboot`）；
  - 若 `autostart` 传入，自动调用 Windows 注册表接口增删启动项；
  - 原子更新 `gateway_config.json` 并通知 Hub 执行配置热重载。

### 3.3 系统数据与目录操作安全门禁
- **请求**：`POST /api/system/open_folder`
- **Payload**：`{"target": "data"}` 或 `{"target": "logs"}`
- **安全与白名单硬门禁**：
  - 后端写死合法 `target` 白名单硬字典：
    ```python
    TARGET_DIR_MAP = {
        "data": DATA_DIR,
        "logs": LOG_DIR
    }
    ```
  - 严禁直接接收路径字符串，非法 `target` 立即返回 `400 Bad Request`，彻底杜绝路径遍历 (Path Traversal) 与恶意 ShellExecute 执行；
  - 打开前确保目录存在：`os.makedirs(target_dir, exist_ok=True)`；
  - 平台防御门禁：非 Windows 或无 `os.startfile` 时优雅返回 400 错误。

- **请求**：`POST /api/system/clear_logs`
- **业务行为**：安全清空 `gateway_app.log`，返回最新日志大小 `0 KB`。

---

## 4. 桌面 Toast 通知安全契约 (`show_windows_toast`)

### 4.1 字符串转义防注入与换行规整
- 在将 `title` 与 `message` 插入 PowerShell 脚本前，必须进行严格的特殊字符清洗：
  - 单引号转义：`text.replace("'", "''")`（PowerShell 单引号字符串标准转义）；
  - 控制字符与换行规整：`text.replace("\r", " ").replace("\n", " ").replace("`", "")`；
  - 限制最大长度为 100 字符，杜绝超长报文引发 XML 结构解析失败。

### 4.2 隐私防窥模式脱敏
- 当 `privacy_mode == true` 时：
  - `title` 统一为：`📩 数字蜂巢收到新短信`；
  - `message` 统一为：`收到一条新短信（已开启防偷窥保护，点击进入控制台查看）`。

---

## 5. 检查更新与关于体系契约

### 5.1 检查更新 (GitHub Releases API)
- **请求方式**：前端浏览器原生异步调用：
  `https://api.github.com/repos/ocean1798/Air780EPV-Smart-Gateway/releases/latest`
- **防抖与容错机制**：
  - 严格限制 30 秒冷却防抖时间；
  - 配置 `AbortController` 6 秒超时门禁，超时或 403 限频时优雅降级提示“暂无法连接 GitHub，可手动前往 Releases 查看”；
  - 比较 SemVer 版本号（如当前 `1.3.0` vs 远端 `v1.3.1`），高亮展示【发现新版本】并提供直达下载链接。

### 5.2 托盘联动与社区直链
- 托盘菜单“ℹ️ 关于数字蜂巢”直接唤起 `http://127.0.0.1:17801/#about`；
- 前端识别 `#about` 自动展开设置抽屉并平滑滚动到底部关于铭牌；
- 官方仓库：`https://github.com/ocean1798/Air780EPV-Smart-Gateway`
- 发布页面：`https://github.com/ocean1798/Air780EPV-Smart-Gateway/releases`
- 问题反馈：`https://github.com/ocean1798/Air780EPV-Smart-Gateway/issues/new`
