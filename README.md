# 小罗看见 · RDK X5 镜头妆效反馈镜

“小罗看见”是一个面向新手跟练化妆的双视角反馈原型：手机 App 播放教程，RDK X5 采集镜前摄像头画面并通过 HDMI 显示镜头视角，用户可以及时判断眼影、腮红等妆效是否被相机“吃掉”。

本仓库只保留当前可复用的核心链路：RDK X5 硬件服务、HDMI Kiosk、可选云台跟脸，以及 iOS SwiftUI App。第三方模型代理、API Key、旧版实验后端、路演/PRD 素材和本机 Xcode 状态文件未纳入。

## 当前状态

- 已验证：iOS App 可解析 RDK X5 的 MJPEG 连续 JPEG 帧并实时显示；可切换手机前置摄像头；可定格当前画面。
- 已验证：语音暂停视频使用 Mac 辅助功能配置完成，但本仓库不把它包装成 RDK X5 已完成能力。
- 已保留：RDK X5 的 USB 摄像头采集、化妆检测叠加、`/stream.mjpg` 推流、`/detections.json` 检测结果、HDMI Kiosk 和 PCA9685 云台跟脸代码。
- 未声称：摄像头/显示屏的最终结构、实时传输延迟、模型 API 联调、自动妆容判断和硬件稳定性已完成。

## 目录

```text
backend/                 RDK X5 摄像头、化妆检测和 HTTP 服务
frontend/                HDMI Kiosk 页面
face_track/              PCA9685 云台跟脸及标定工具
systemd/                 RDK X5 后端和 Kiosk 服务单元
scripts/install.sh       板端安装脚本
scripts/kiosk.sh         Chromium/Firefox Kiosk 启动脚本
scripts/deploy_rdk.py    局域网部署辅助脚本
BOM.csv                  硬件物料清单
ios/                     SwiftUI App 与 Xcode 工程
```

## RDK X5 运行

硬件要求：RDK X5、USB UVC 摄像头、HDMI 显示器；云台跟脸另需 PCA9685 和舵机。默认摄像头为 `/dev/video0`，后端监听 `8080`。

```bash
cd ~/makeup-mirror-RDKX5
bash scripts/install.sh
sudo systemctl start makeup-mirror-backend
sudo systemctl start makeup-mirror-kiosk
```

浏览器检查：

```text
http://<RDK-X5-IP>:8080/
http://<RDK-X5-IP>:8080/stream.mjpg
http://<RDK-X5-IP>:8080/detections.json
```

常用调试变量：`MM_CAM`、`MM_CAM_W`、`MM_CAM_H`、`MM_CAM_FPS`、`MM_TARGET_FPS`、`MM_DETECT_EVERY`、`MM_JPEG_QUALITY`。化妆检测参数见 `backend/makeup_detector.py`。

云台跟脸：

```bash
cd face_track
python3 face_track.py --web-port 8081
python3 servo_calibrate.py
```

PCA9685 默认使用 `/dev/i2c-5`、地址 `0x40`，CH0 为 yaw，CH1 为 tilt；具体接线和脉宽参数以实物标定为准。

## iOS App

用 Xcode 打开 `ios/KanjianApp.xcodeproj`。运行前将 `ios/KanjianApp/KanjianViewModel.swift` 中的 `cameraServerURL` 改成 RDK X5 在当前局域网的地址，例如：

```swift
@Published var cameraServerURL = "http://192.168.1.20:8080"
```

手机和 RDK X5 必须在同一网络。App 的核心流程是：连接板端 MJPEG → 实时预览 → 定格 → 本地演示建议/效果图。模型 API 代理未随仓库上传；如需重新接入，应通过独立后端完成，并将密钥放在服务端环境变量，绝不写进 Swift 或 Git。

## 安全与提交边界

- 不提交 `.env`、API Key、Token、密码、运行时图片和 Xcode 用户状态。
- RDK X5 的本地 HTTP 服务当前没有鉴权，不应直接暴露到公网。
- 摄像头画面默认只用于实时显示和主动定格；是否保存、如何提示用户，仍需在产品验证中确定。

## 说明

硬件基础代码来自 [Hustle28214/makeup-mirror-RDKX5](https://github.com/Hustle28214/makeup-mirror-RDKX5)。本次整理将其化妆镜主链路与本地 iOS App 合并，并删除与当前目标无关的头皮屑模式、Windows 运行脚本、诊断临时脚本、第三方模型代理和本机缓存。
