# 规范契约：系统设置抽屉两列布局与视觉规范 (AIR-58)

## 1. 抽屉物理尺寸与响应式契约
- **桌面端视口（宽度 > 640px）**：
  - 抽屉宽度：固定或最大宽度 `640px`（原为 420px 偏窄单列），为两列排布提供充裕横向空间；
  - 栅格排布：运行策略采用 `display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.5rem;`；
- **移动端视口（宽度 <= 640px）**：
  - 抽屉宽度：`100%` 满宽；
  - 栅格排布：自动回退为单列 `grid-template-columns: 1fr;`，底行通栏自动重置为 `grid-column: span 1 !important;`，100% 保证手机端不被撑破横向视口。

## 2. 交互与布局契约
### 2.1 运行策略与偏好（两列网格）
- **左列**：
  1. 开机自动启动 (静默守护)
  2. 收到验证码自动复制
  3. 桌面屏幕右下角通知
- **右列**：
  4. 防偷窥模式 (隐藏短信内容)
  5. 验证码到达提示音
  6. 设备离线自动保存短信
- **底行通栏**：
  7. 每日凌晨 04:00 自动重启维护

### 2.2 推送渠道表单契约（直观可见，拒绝过度隐藏）
- 保留 5 大渠道（飞书、钉钉、企微、Bark、Webhook）直接可见的配置形态；
- 移除大号占满整行的 `<button>清除已存地址</button>`，废除旧版 JS 中 `setupNotifyClearControls()` 动态追加逻辑；
- 改为输入框右侧内嵌的精致小胶囊（`[清空]`）：
  - 当对应字段已有值或后端已存时展示，点击后清空输入框并记入 `state.notifyClearFields[channel]`；
  - 若点击清空地址（`url`），联动将该渠道开关置为 false，防止后端 400 校验阻断。

### 2.3 冗余拔除契约
- 彻底移除“软件手动启动时弹出主窗口”开关；
- 彻底移除“最近通知结果”机械 log 卡片容器及 `#notifyResultsList` DOM。

## 3. 视觉色值 Token 规范（局部变量隔离）
- **抽屉主体与局部变量覆盖**：
  ```css
  .settings-drawer {
    --bg-card: rgba(255, 255, 255, 0.025);
    --border-color: rgba(255, 255, 255, 0.06);
    background: rgba(11, 15, 25, 0.96) !important;
    backdrop-filter: blur(24px);
    -webkit-backdrop-filter: blur(24px);
    border-left: 1px solid rgba(255, 255, 255, 0.08);
  }
  ```
  通过在 `.settings-drawer` 下局部重定义 `--bg-card`，无需破坏主界面样式，即可让抽屉内所有卡片（通知渠道、系统策略、局域网直达、数据目录维护、关于铭牌）全部告别 `#1f2937` 灰白发亮感，呈现通透高雅的晶体质感。
- **输入框容器**：`background: rgba(0, 0, 0, 0.3); border: 1px solid rgba(255, 255, 255, 0.1); color: #f8fafc;`
