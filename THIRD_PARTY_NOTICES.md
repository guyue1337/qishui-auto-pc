# 第三方组件与许可证说明

本项目在实现 iPhone 设备读取、旧版 iOS 兼容、签名安装与自动化控制时，会使用或按需下载以下第三方组件。各组件的版权与许可证归其原作者所有。

## WebDriverAgent

- 项目：Appium WebDriverAgent
- 版本：`16.15.3`
- 许可证：BSD 3-Clause
- 来源：<https://github.com/appium/WebDriverAgent>
- 用途：为 iOS 17.0–26.x 提供 WebDriverAgent 运行包，仅在用户主动配置旧版 iPhone 兼容模式时下载。
- 便携包中的 `assets/ios/WDA.ipa` 来自上述固定版本，已移除仅用于调试的 `.dSYM` 目录；完整许可证见 `assets/ios/WEBDRIVERAGENT-LICENSE.txt`。

## Impactor

- 项目：Impactor
- 版本：`2.7.3`
- 许可证：MIT
- 来源：<https://github.com/claration/impactor>
- 用途：提供 Apple ID 登录窗口与签名安装入口。账号密码由 Impactor 在本机处理，本项目不读取或保存密码。

## plumesign / plume_core / apple-codesign

- 项目：Impactor 的签名组件及其 Apple 签名依赖
- 许可证：MIT / Mozilla Public License 2.0（以各上游组件声明为准）
- 来源：<https://github.com/claration/impactor>
- 用途：对 WebDriverAgent 运行包进行个人免费签名并安装到用户自己的设备。

## pymobiledevice3

- 版本：`11.63.1`
- 许可证：GNU General Public License v3.0
- 来源：<https://github.com/doronz88/pymobiledevice3>
- 用途：读取 iPhone 设备信息、建立开发者服务通道、端口转发和启动测试运行器。

## 其他运行库

便携包还包含 Python、OpenCV、NumPy、ONNX Runtime、RapidOCR、cryptography 等运行库。其版权与许可证以各自发行包内附带的许可证文件为准。

## 说明

- 上述第三方工具仅用于用户个人设备上的学习、研究与非商业自动化。
- 本项目不托管、不修改 Apple 账号系统，也不提供绕过 Apple 账号或应用授权的能力。
- 第三方组件的下载地址与 SHA-256 校验值固定在源码中，下载后会在本机校验。
- 如果某个上游组件更新了许可证，请以其官方仓库中的最新许可证文本为准。
