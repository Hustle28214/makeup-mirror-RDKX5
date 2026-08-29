# make-up-mirror (RDK X5)

基于 **地平线 RDK X5** 的智能化妆镜：USB 摄像头 → RDK X5 → HDMI 触控屏。云台跟随人脸，全屏实时预览，并叠加**化妆分析**或**头皮屑检测**结果。开机自启动，接上 HDMI 与摄像头即用。

## 功能

- **化妆镜模式 (`MM_MODE=makeup`)**：分区匀度（额头 / 双颊 / 下巴）、左右对称度（LAB 色差）、痘痘/红斑候选、T 区油光、黑眼圈、光照左右均衡、滚动时间线与总体评分。
- **头皮屑检测模式 (`MM_MODE=dandruff`，默认)**：针对黑色头发优化的 HSV + 白 top-hat 流水线，输出头皮屑数量与位置。
- **人脸跟踪云台**：PCA9685 + 两颗连续旋转舵机（yaw + tilt），Haar 正脸 / 侧脸级联，画面偏差驱动速度控制 `pulse = stop + gain × err`，人脸居中即停。
- **Kiosk 前端**：单页 HTML/CSS/JS + Chromium `--kiosk` 全屏，MJPEG 推流 + JSON 检测端点。

## 硬件（详见 [BOM.csv](BOM.csv)）

- 主控：RDK X5（Ubuntu ARM64）
- 舵机：MG90S 数字舵机 × 2（连续旋转型，非位置型）
- 舵机驱动：PCA9685（16 路 PWM，I²C）
- 音频：WM8960（可选，麦克风 + 喇叭）
- 摄像头：USB UVC 免驱，支持 1080p MJPG
- 显示：HDMI 触控屏
- 结构：折叠式化妆镜
- 电源：12V DC 适配器 + DC 12V→5V 分电模块

I²C 接线：PCA9685 挂在 **`/dev/i2c-5`**（40-pin 的 Pin 3/5），地址 `0x40`；yaw = CH0，tilt = CH1。

## 目录结构

```
backend/          # HTTP + MJPEG 服务 (app.py)、Camera、DandruffDetector、MakeupDetector
frontend/         # index.html (头皮屑) / makeup.html (化妆镜) + JS/CSS
face_track/       # 独立的 PCA9685 云台跟脸脚本 face_track.py + 标定/自检工具
scripts/          # 部署脚本 (install.sh / kiosk.sh / deploy_rdk.py / 各类 _rdk_*.py 诊断)
systemd/          # makeup-mirror-backend.service + makeup-mirror-kiosk.service
BOM.csv           # 硬件物料清单
```

## 部署到 RDK X5

```bash
# 克隆或 scp 到板子，建议路径 /home/sunrise/make-up-mirror
cd ~/make-up-mirror
bash scripts/install.sh
sudo reboot
```

启动后 HDMI 应自动进入全屏检测界面。

## 手动运行（调试）

**后端服务：**

```bash
cd ~/make-up-mirror/backend
python3 app.py                               # 默认头皮屑模式 + /dev/video0
MM_MODE=makeup python3 app.py                # 切换化妆镜模式
MM_CAM=/dev/video2 python3 app.py            # 指定摄像头
xdg-open http://127.0.0.1:8080/
```

常用环境变量：`MM_MODE` (`dandruff`/`makeup`)、`MM_CAM`、`MM_CAM_W`/`MM_CAM_H`/`MM_CAM_FPS`、`MM_TARGET_FPS`、`MM_DETECT_EVERY`、`MM_JPEG_QUALITY`。RDK X5 上 systemd 单元默认降到 1280×720 15fps 以适配 3 GB 内存。

**云台跟脸：**

启动前必须**手动完成物理归零**（软件启动时会把两轴 θ 自动置零，作为后续限位/积分的原点）：

1. **摄像头镜头朝向**：将镜头**摆到与自己视线平行**（正对预期使用者）。
2. **舵机 1 (yaw, CH0)**：让摇臂**垂直于镜子边**，即云台正朝前。
3. **舵机 2 (tilt, CH1)**：让镜头光轴**与视线平行**（水平方向对齐使用者眼睛高度）。

到位后即可启动 —— 期间**不要再手动扭动机构**，否则内部 θ 与实际不符，限位会失灵。

```bash
cd ~/make-up-mirror/face_track
python3 face_track.py --web-port 8081        # 摄像头 0 + 舵机 + MJPEG 调试页
python3 face_track.py --no-servo --show      # 仅检测，本地窗口预览
python3 servo_calibrate.py                   # 交互 jog/us/release 测舵机
```

关键参数（详见 `face_track.py` 顶部）：

- **速度控制**：`--stop-us 1560`（连续旋转停机脉宽）、`--yaw-gain 0.9` / `--tilt-gain 0.7`、`--yaw-deadband-px 70` / `--tilt-deadband-px 130`、`--yaw-min-speed-us 60` / `--tilt-min-speed-us 90`、`--max-speed-us 200`。
- **方向翻转**：`--yaw-invert` / `--no-yaw-invert`、`--tilt-invert` / `--no-tilt-invert`。
- **软件角度限位**（基于开环 θ 积分，超界自动钳到 stop）：`--yaw-limit-left-deg 90 --yaw-limit-right-deg 90 --tilt-limit-up-deg 90 --tilt-limit-down-deg 30`。
- **速度模型系数**（2026-08-30 负载状态标定）：yaw `ω=1.86·Δus−62`（min |Δus|=60）；tilt `ω=1.76·Δus−64`（min |Δus|=90）。可用 `--yaw-speed-slope / --yaw-speed-intercept / --tilt-speed-slope / --tilt-speed-intercept` 覆盖。

## HTTP 接口

- `GET /`                — 前端 (依 `MM_MODE` 返回 `index.html` 或 `makeup.html`)
- `GET /stream.mjpg`     — 带叠加的 MJPEG 视频流
- `GET /detections.json` — 最新一帧检测结果 JSON

## 排查

```bash
v4l2-ctl --list-devices                             # 找 USB 摄像头节点
v4l2-ctl -d /dev/video0 --list-formats-ext          # 确认 1080p MJPG
journalctl -u makeup-mirror-backend -f
journalctl -u makeup-mirror-kiosk -f
xrandr --output HDMI-1 --mode 1920x1080             # HDMI 分辨率修正
i2cdetect -y -r 5                                   # 应看到 0x40 (PCA9685)
```

## 检测参数调优（头皮屑模式）

阈值集中在 [backend/detector.py](backend/detector.py) 顶部：

- `HAIR_V_MAX` / `HAIR_S_MAX`：黑发范围，环境偏亮时上调 `HAIR_V_MAX`
- `FLAKE_V_MIN` / `FLAKE_TOPHAT_MIN`：屑的亮度阈值
- `MIN_AREA` / `MAX_AREA`：斑点面积（按 960 宽度内部处理）
- `MIN_CIRCULARITY`：过滤长条状高光/发丝

## 后续可选升级

- 用 RDK BPU 上的轻量语义分割（`hobot_dnn`）替换 CV 流水线，降低误检。
- ROI：只在头发中央区域检测，规避额头/背景高光。
- 双路摄像头（正/侧）：`app.py` 起两个 `Camera`，暴露 `/stream_a.mjpg`/`/stream_b.mjpg`，前端加切换按钮。
- 化妆镜模式接入人脸关键点（MediaPipe / dlib），补齐几何对称度与口红/眼线越界检测。
