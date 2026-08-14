# CodexMeter（智码仪）

[English](README.md) | **简体中文**

在 M5StickS3 上显示本机 Codex 用量的低功耗蓝牙仪表盘。

电脑端从本地 Codex 会话日志读取额度和 token 计数，每 60 秒通过 BLE 短暂连接设备、发送一次数据并立即断开。设备不需要连接 Wi-Fi，也不会接触对话正文或 Codex 认证信息。

## 功能

- 显示 Codex 当前额度窗口的**剩余百分比**和重置倒计时
- 汇总今日、近 7 天以及当前会话的 token 数量
- 展示今日 input、cached input、output 和 reasoning token 明细
- 通过 BLE 无线同步，默认每分钟一次
- 四方向自动旋转，竖屏和横屏分别使用独立布局
- 电池供电 30 秒无操作后降低亮度，移动或按键后恢复
- 检测到 USB/外部 5V 供电时保持最高亮度
- macOS LaunchAgent 开机登录后自动运行，并防止重复实例

剩余额度采用以下颜色：

| 剩余额度 | 颜色 |
| --- | --- |
| 60–100% | 绿色 |
| 30–59% | 黄色 |
| 0–29% | 红色 |
| 无百分比数据 | 灰色空圆和 `--%` |

数据超过 3 分钟未更新时，仅将顶部状态点标为黄色，不会把额度圆形误标为红色。

## 工作原理

```text
~/.codex/sessions/**/*.jsonl
~/.codex/archived_sessions/*.jsonl
               │
               ▼
     bridge/ble_sender.py
      读取并汇总 token_count
               │
        BLE，每 60 秒短连接
               │
               ▼
        M5StickS3 / CodexMeter
```

百分比来自 Codex 日志中的 `rate_limits.primary.used_percent`，屏幕显示的是 `100 - used_percent`。设备只接受总体额度 `limit_id=codex`，避免把模型专属额度误当成账户总体额度。

token 数量来自本机 `token_count` 事件。token 与额度百分比不是同一种计费单位，因此 CodexMeter 不会根据 token 数量虚构一个“token 总额度”。今日统计按电脑的本地时区从零点开始，近 7 天为今天加前 6 个自然日。

## 硬件和软件要求

- M5StickS3
- 一根支持数据传输的 USB-C 线
- macOS 电脑（常驻启动配置使用 LaunchAgent）
- Python 3.10 或更高版本
- [PlatformIO Core](https://docs.platformio.org/en/latest/core/installation/index.html)
- 已在本机使用过 Codex，且 `~/.codex` 下存在 `token_count` 事件

## 快速开始

### 1. 获取项目并安装电脑端依赖

```bash
git clone https://github.com/yiduo/codex-meter.git
cd codex-meter
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. 编译并刷入 M5StickS3

连接设备后运行：

```bash
pio run
pio run --target upload
```

可选：打开串口监视器。

```bash
pio device monitor
```

如果设备没有进入下载模式，按住侧面的 Reset 约 2 秒，看到绿色 LED 闪烁后松开，再执行上传命令。多个串口设备同时连接时，可先运行 `pio device list`，再在 `platformio.ini` 中临时指定 `upload_port`。

### 3. 验证本地数据

先确认电脑端能够读到 Codex 数据：

```bash
.venv/bin/python bridge/server.py --once
```

返回 JSON 中：

- `valid: true` 表示找到了总体 Codex 百分比
- `used_percent` 是已用百分比，设备会换算为剩余百分比
- `today_tokens` 是今天所有会话的增量汇总
- `updated_at` 是本次快照的 Unix 时间戳

### 4. 通过蓝牙同步

让设备保持开机，然后运行：

```bash
.venv/bin/python bridge/ble_sender.py --once
```

看到 `同步成功` 后，启动每分钟同步的常驻前台进程：

```bash
.venv/bin/python bridge/ble_sender.py --interval 60
```

macOS 首次运行时可能询问蓝牙权限，请允许当前终端或 Python 使用蓝牙。CoreBluetooth 使用系统分配的 UUID，而不是固定 MAC 地址，因此发送端会按广播名称 `CodexMeter` 搜索设备，并在连接后通过 GATT 特征确认。

## macOS 开机自动运行

仓库中的 [`launchd/io.github.codexmeter.ble.plist`](launchd/io.github.codexmeter.ble.plist) 是模板。先确保当前目录就是项目根目录，然后生成包含实际绝对路径的 LaunchAgent：

```bash
mkdir -p "$HOME/Library/LaunchAgents" logs
PROJECT_DIR="$(pwd)"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
  launchd/io.github.codexmeter.ble.plist \
  > "$HOME/Library/LaunchAgents/io.github.codexmeter.ble.plist"
```

加载并立即启动：

```bash
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/io.github.codexmeter.ble.plist"
launchctl kickstart -k "gui/$(id -u)/io.github.codexmeter.ble"
```

如果已经加载过该服务，修改配置后使用：

```bash
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/io.github.codexmeter.ble.plist"
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/io.github.codexmeter.ble.plist"
```

检查运行状态和日志：

```bash
launchctl print "gui/$(id -u)/io.github.codexmeter.ble"
tail -f logs/ble_sender.log
tail -f logs/ble_sender.error.log
```

LaunchAgent 使用 `RunAtLoad` 和 `KeepAlive`，登录 macOS 后会自动启动，异常退出时会重新拉起。发送端同时使用文件锁，避免手工启动和 LaunchAgent 产生两个同步进程。

## 设备操作

- A 键：恢复最高亮度；亮屏时切换总览、token 明细和蓝牙状态页
- B 键：恢复最高亮度；亮屏时重新开始 BLE 广播
- 移动设备：恢复最高亮度并根据重力方向旋转界面
- 收到新数据：恢复最高亮度

两种竖屏方向采用 135×240 完整布局；两种横屏方向采用 240×135 紧凑布局。横屏总览为左侧主指标、右侧数据卡，明细页和蓝牙页使用 2×2 卡片。

## 配置

固件设置位于 [`include/config.h`](include/config.h)：

```cpp
#define BLE_DEVICE_NAME "CodexMeter"
#define BLE_SHARED_KEY ""
#define BLE_STALE_AFTER_MS 180000UL
#define SCREEN_TIMEOUT_MS 30000UL
#define SCREEN_BRIGHTNESS 255
#define SCREEN_DIM_BRIGHTNESS 10
#define MOTION_THRESHOLD_G 0.12f
#define ORIENTATION_TRIGGER_G 0.20f
#define ORIENTATION_STABLE_MS 150UL
```

如需设置简单的 BLE 共享密钥，在固件中填写 `BLE_SHARED_KEY`，重新刷机，并在电脑端提供相同值：

```bash
.venv/bin/python bridge/ble_sender.py \
  --interval 60 \
  --shared-key "与 config.h 相同"
```

也可以通过环境变量 `CODEX_BLE_KEY` 提供密钥。共享密钥只用于拒绝意外写入，不等同于完整的加密和身份认证；请勿在固件中放置重要密码。

常用发送端参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--device` | `CodexMeter` | BLE 广播名称 |
| `--interval` | `60` | 两次同步之间的秒数，最低 5 秒 |
| `--timeout` | `15` | 扫描和连接超时 |
| `--codex-home` | `~/.codex` | Codex 数据目录 |
| `--once` | 关闭 | 成功同步一次后退出 |

## BLE 协议

- Service：`7d6a1000-8f3b-4b6d-9f6a-4d3558438d01`
- Data（Write）：`7d6a1001-8f3b-4b6d-9f6a-4d3558438d01`
- Status（Read）：`7d6a1002-8f3b-4b6d-9f6a-4d3558438d01`
- 负载：分片传输、以换行结束的紧凑 JSON
- 确认：Status 返回最近成功应用的 `updated_at`
- 防回退：同一额度周期内拒绝异常降低的已用百分比；`resets_at` 改变后接受官方重置值

## 可选的本地 HTTP 接口

`bridge/server.py` 主要用于调试数据源，也可以启动一个本地 JSON 接口：

```bash
.venv/bin/python bridge/server.py --host 127.0.0.1 --port 8787
curl http://127.0.0.1:8787/api/usage
```

如需局域网访问，可以改为 `--host 0.0.0.0`，并通过 `--api-key` 或环境变量 `CODEX_DASHBOARD_KEY` 设置请求头密钥：

```bash
curl -H 'X-Dashboard-Key: your-key' http://127.0.0.1:8787/api/usage
```

BLE 同步不依赖这个 HTTP 服务。

## 故障排查

### 屏幕一直显示 `WAITING` 或 `--%`

1. 运行 `bridge/server.py --once`，确认本机有 `token_count` 和总体额度数据。
2. 运行 `bridge/ble_sender.py --once`，查看是扫描、连接还是设备确认失败。
3. 确认设备广播名与 `--device`、`BLE_DEVICE_NAME` 一致。
4. 检查 macOS“系统设置 → 隐私与安全性 → 蓝牙”中的终端/Python权限。
5. 按 B 键重新开始广播；仍无效时重启设备。

### 偶尔出现“未找到蓝牙设备”

设备可能正在重启、刚断开连接或处于一次扫描间隔中。常驻发送端会在下一周期自动重试；只要日志随后出现 `同步成功`，无需重启服务。

### 服务看起来停止了

```bash
launchctl print "gui/$(id -u)/io.github.codexmeter.ble"
tail -n 50 logs/ble_sender.log
tail -n 50 logs/ble_sender.error.log
```

状态为 `running` 即表示服务仍在运行。日志中的单次扫描失败并不代表服务退出。

### 横竖屏方向不正确或反应慢

设备需要检测到明确且稳定的重力主轴才会旋转，以避免平放或轻微抖动时频繁翻转。握住设备并轻晃一下，然后在目标方向保持约 0.15 秒。传感器阈值和稳定时间可在 `include/config.h` 中调整。

### 插电后屏幕仍变暗

确认使用的 USB 线确实提供 5V，并检查串口是否能识别设备。固件会综合 PMIC 电源来源、VBUS 电压、充电状态和 USB 状态判断外部供电。

## 开发与验证

运行电脑端单元测试和固件编译：

```bash
python3 -m unittest discover -s tests -v
pio run
```

项目结构：

```text
bridge/      Codex 日志解析、本地调试接口和 BLE 发送端
include/     固件配置
launchd/     macOS LaunchAgent 模板
src/         M5StickS3 固件
tests/       Python 单元测试
```

## 隐私说明

电脑端只读取 `token_count` 事件中的计数和额度字段，不会通过 BLE 发送提示词、回复正文、文件内容或认证令牌。默认 BLE 负载仅在电脑与设备之间传输；可选 HTTP 服务默认建议绑定 `127.0.0.1`。
