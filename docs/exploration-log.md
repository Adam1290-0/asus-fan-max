# 探索路线与思考过程

记录从零解决此问题的完整思路，包括走错的弯路，避免重复探索。

## 第一阶段：确认控制链

**问题**：用户风扇转速不够，不确定哪个软件在控制。

**方法**：
1. 检查 Startup 目录 → 发现 atrofac-gui.exe 和 GHelper
2. 检查进程 → 确认两者都在运行
3. 读取配置 → atrofac 有 aggressive 曲线，GHelper 有 fan_max 设置

**结论**：atrofac 通过 ATKACPI 写 EC 曲线控制风扇，GHelper 的 fan_max 只是 UI 显示缩放。

## 第二阶段：尝试 ATKACPI 路径突破

**假设**：也许写更激进的曲线或特殊参数能让 EC 突破限速。

**尝试**：
1. 写 100% duty 曲线到 0x00110024 → CPU 仍 ~4000 RPM ❌
2. DEVS(0x00110013, 1)（向 RPM 端点写值）→ GPU 到 7400 但 CPU 仍 4000 ❌
3. 研究 GHelper 源码 → 发现 SetFanRange、PerformanceManual 等

**GHelper 源码关键发现**：
- `fan_max` 纯粹是显示用，不影响硬件
- `SetFanRange` 写 {min,max} 到 0x00110022，是 SetFanCurve 的 fallback
- 校准模式（Turbo + 100% 曲线）也只能测到 EC 允许的最大值
- GHelper 完全没有 AsusWinIO/ASHW/直接 EC 访问

**结论**：ATKACPI 路径被 EC 固件硬性限速，无法通过参数变化突破。

## 第三阶段：研究 MyASUS 路径

**假设**：MyASUS 风扇测试能到 7800 RPM，它走的是什么路径？

**发现**：
- MyASUS UI → AsusSystemDiagnosis.dll → ASHWTestWin32.dll → RPC → ASUS System Diagnosis 服务
- 服务使用 AsusWinIO64.dll → AsusSAIO.sys 驱动
- 服务当前 Stopped/Disabled

**静态分析 AsusWinIO64.dll**：
- 259 个导出函数
- 关键：InitializeWinIo, Set_FanPwmDuty, HealthyTable_SetFanTestMode
- 导入：CreateServiceW, StartServiceW（服务管理）+ DeviceIoControl（驱动通信）
- 还有 CreateProcessW, SetFirmwareEnvironmentVariableW（潜在副作用）

**结论**：真正路径是 AsusSAIO.sys 驱动直接写 EC 寄存器。

## 第四阶段：权限突破

**问题**：InitializeWinIo() 返回 0（失败）。

**排查**：
1. 管理员权限运行 → 仍返回 0 ❌
2. 尝试打开 \\.\AsusSAIO → error=5 (ACCESS_DENIED)，即使管理员 ❌
3. 检查设备安全描述符 → 只允许 SY (SYSTEM) 和 BA ( elevated admin)
4. 但 BA 也被拒绝 → 设备对象 DACL 比服务 DACL 更严格

**突破**：用计划任务以 SYSTEM 身份运行 → InitializeWinIo() = 1 ✅

**关键教训**：
- 驱动已运行（DEMAND_START + Running），不需要启动服务
- 问题纯粹是设备对象权限，不是服务状态
- 计划任务 /RU SYSTEM 是最简单的获取 SYSTEM 的方式

## 第五阶段：验证控制效果

**测试 duty=80/100**：
- 返回 1（成功），RPM 有变化
- 但 duty=100 只到 ~4500 RPM

**发现 duty 刻度是 0-255**：
- duty=150 → ~5800 RPM
- duty=200 → ~6500 RPM
- duty=255 → ~7200-7400 RPM ✅

**交叉验证的坑**：
- 用 ATKACPI 同时读 RPM → 显示只有 4000！
- 误判为"Set_FanPwmDuty 没控制真实风扇"
- 但用户亲耳听到风扇加速 → ATKACPI 在 test mode 下不更新真实 RPM
- **教训：EC 进入 test mode 后 ATKACPI 转速寄存器返回旧值，不能作为验证依据**

## 第六阶段：部署

**方案选择**：
- 方案 A：SYSTEM 守护进程自己读温度+控制（最简单）✅ 采用
- 方案 B：SYSTEM helper + named pipe IPC + atrofac 调用（复杂）
- 方案 C：集成到 atrofac Rust 代码（需修复构建工具链）

**选择 A 的原因**：
- 无需 IPC，无构建问题
- 一个 Python 文件解决
- atrofac 继续处理日常曲线，daemon 只在高温时介入
- 两者互不感知，不会冲突

## 弯路总结（避免重复）

| 弯路 | 浪费时间 | 正确做法 |
|------|----------|----------|
| 尝试各种 ATKACPI 参数突破 EC 限速 | 多轮测试 | EC 固件限速不可绕过，直接走 AsusSAIO |
| 以为 fan_max 配置影响硬件 | 研究 GHelper 配置 | 读源码确认只是 UI 缩放 |
| 管理员权限调 InitializeWinIo | 多次失败 | 直接用 SYSTEM（计划任务） |
| 用 ATKACPI 交叉验证 RPM | 误判为无效 | test mode 下 ATKACPI 不可信，用 AsusSAIO 读或听声音 |
| duty 用 0-100 百分比 | 效果不明显 | 刻度是 0-255 |
| PowerShell 多行 schtasks 语法 | 注册失败 | 用 .bat 包装或单行命令 |
