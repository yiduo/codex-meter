#pragma once

// 电脑端会按这个广播名称寻找 StickS3。
#define BLE_DEVICE_NAME "CodexMeter"

// 可选共享密钥；若设置，bridge/ble_sender.py 必须传入相同值。
#define BLE_SHARED_KEY ""

// 超过这个时间没有收到新数据时，屏幕显示 STALE（默认 3 分钟）。
#define BLE_STALE_AFTER_MS 180000UL

// 无操作 30 秒后降低亮度；按键、新数据或检测到移动时恢复最高亮度。
#define SCREEN_TIMEOUT_MS 30000UL
#define SCREEN_BRIGHTNESS 255
#define SCREEN_DIM_BRIGHTNESS 10
#define MOTION_THRESHOLD_G 0.12f

// BMI270 低频检测移动和四方向；方向稳定后才旋转，避免轻微晃动抖屏。
#define IMU_SAMPLE_INTERVAL_MS 100UL
#define ORIENTATION_TRIGGER_G 0.20f
#define ORIENTATION_STABLE_MS 150UL
// StickS3 的 X+ 指向机身底部；正立时静止重力读数沿 X-。
#define ORIENTATION_UPRIGHT_X_SIGN -1.0f

// 供电来源轮询间隔；USB/外部 5V 供电时屏幕保持最高亮度。
#define POWER_CHECK_INTERVAL_MS 1000UL
#define EXTERNAL_POWER_MIN_MV 4000

// 同一总体额度周期内已用百分比只能上升；超过该容差的回退数据会被拒绝。
#define LIMIT_PERCENT_DROP_TOLERANCE 0.5f
