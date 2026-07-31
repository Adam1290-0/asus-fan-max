# ASUS 风扇控制架构

## 硬件层级

```
用户态软件 (GHelper/atrofac/MyASUS)
    │
    ├── ATKACPI 驱动 (\\.\ATKACPI) ← 普通用户可打开
    │       │
    │       └── EC 固件（解释曲线，有硬性转速上限）
    │               │
    │               └── 风扇 PWM 控制器
    │
    └── AsusSAIO.sys 驱动 (\\.\AsusSAIO) ← 仅 SYSTEM 可打开
            │
            └── 直接 EC 寄存器写入（绕过固件曲线限制）
                    │
                    └── 风扇 PWM 控制器（物理满转）
```

## ATKACPI 接口

### 打开设备

```python
# 路径必须是 UTF-16LE: \\.\ATKACPI
path_bytes = bytes([
    0x5c,0x00, 0x5c,0x00, 0x2e,0x00, 0x5c,0x00,  # \\.\
    0x41,0x00, 0x54,0x00, 0x4b,0x00, 0x41,0x00,  # ATKA
    0x43,0x00, 0x50,0x00, 0x49,0x00, 0x00,0x00   # CPI\0
])
handle = CreateFileW(path_bytes, 0xC0000000, 0, None, 3, 0, None)
```

### 控制码

- IOCTL: `0x0022240C`（十进制 2237452）

### 输入缓冲区格式

**DSTS（读取）：** 16 字节
```
[0:4]   "DSTS" (0x44, 0x53, 0x54, 0x53)
[4:8]   参数长度 = 8 (little-endian uint32)
[8:12]  设备 ID (little-endian uint32)
[12:16] 填充 0x00
```

**DEVS（写入）：** 可变长度
```
[0:4]   "DEVS" (0x44, 0x45, 0x56, 0x53)
[4:8]   参数长度 (little-endian uint32)
[8:12]  设备 ID (little-endian uint32)
[12:]   数据（长度 = 参数长度 - 4）
```

### 设备 ID 表

| ID | 用途 | 读/写 |
|----|------|-------|
| 0x00120094 | CPU 温度 | 读 |
| 0x00120097 | GPU 温度 | 读 |
| 0x00110013 | CPU 风扇 RPM | 读 |
| 0x00110014 | GPU 风扇 RPM | 读 |
| 0x00110031 | 中间风扇 RPM | 读 |
| 0x00110022 | CPU 风扇范围 {min,max} | 写 |
| 0x00110023 | GPU 风扇范围 {min,max} | 写 |
| 0x00110024 | CPU 风扇曲线（16字节） | 写 |
| 0x00110025 | GPU 风扇曲线（16字节） | 写 |
| 0x00110034 | 风扇迟滞 | 读/写 |
| 0x00120075 | 性能模式 | 读/写 |

### RPM 解码

```python
raw = struct.unpack_from('<i', out_buf, 0)[0]  # signed int32
rpm = ((raw - 65536) & 0xFFFF) * 100
```

### 温度解码

```python
raw = struct.unpack_from('<i', out_buf, 0)[0]
temp_c = raw - 65536  # 有效范围 0-120
```

### 风扇曲线格式（16 字节）

```
[0:8]   温度点（8个，单位°C，如 30,40,50,60,70,80,90,100）
[8:16]  对应 duty（8个，单位%，如 25,40,55,75,100,100,100,100）
```

写入目标：DEVS + 设备ID 0x00110024(CPU) / 0x00110025(GPU)

### 性能模式值

- 0 = Balanced
- 1 = Turbo
- 2 = Silent
- 4 = Manual（可能解锁直接 duty 控制，但实测无效）

## AsusSAIO.sys 驱动

### 设备名

- `\\.\AsusSAIO`（主设备）
- `\\.\ASUSSAIO`（别名）
- 安全描述符：仅允许 NT AUTHORITY\SYSTEM 访问
- 管理员（High IL）打开会得到 error=5 (ACCESS_DENIED)

### 驱动位置

```
C:\Windows\System32\DriverStore\FileRepository\asussci2.inf_amd64_<hash>\ASUSSystemAnalysis\
├── AsusSAIO.sys          (内核驱动，51KB)
├── AsusWinIO64.dll       (用户态 DLL，2.4MB)
└── AsusSystemAnalysis.exe (服务主程序)
```

查找活跃版本：
```powershell
sc.exe qc AsusSAIO  # 看 BINARY_PATH_NAME 指向哪个 DriverStore 路径
```

### 驱动状态

- 服务名：AsusSAIO
- 类型：内核驱动 (Type=1)
- 启动：DEMAND_START (Manual, Start=3)
- 通常状态：Running（被其他 ASUS 组件加载后保持运行）

### AsusWinIO64.dll 关键导出

| 函数 | 签名（推测） | 用途 |
|------|-------------|------|
| InitializeWinIo | `int ()` | 初始化驱动连接，SYSTEM 下返回 1 |
| ShutdownWinIo | `int ()` | 关闭连接 |
| Set_FanPwmDuty | `int (int fan_idx, int duty)` | 设置风扇 PWM，duty 0-255 |
| HealthyTable_SetFanTestMode | `int (int mode)` | 1=进入测试模式，0=退出 |
| HealthyTable_GetFanTestMode | `int ()` | 返回当前测试模式状态 |
| HealthyTable_FanCounts | `int ()` | 返回风扇数量 |
| HealthyTable_FanRPM | `int (int idx)` | 读取指定风扇 RPM |
| HealthyTable_SetFanIndex | `int (int idx)` | 选择当前操作的风扇 |
| HealthyTable_SetFanPwmDuty | `int (int duty)` | 对当前选中风扇设置 duty |
| GetDriverVersion | `int ()` | 驱动版本 |
| GetIoDllVersion | `int ()` | DLL 版本 |
| StopWinIoDriver | `int ()` | ⚠️ 停止驱动，禁止调用 |

### 调用序列（已验证）

```python
# 进入满转
InitializeWinIo()                    # → 1
HealthyTable_SetFanTestMode(1)       # → 1，进入测试模式
Set_FanPwmDuty(0, 255)              # → 1，风扇0满转
Set_FanPwmDuty(1, 255)              # → 1，风扇1满转

# 退出满转（安全，EC接管）
HealthyTable_SetFanTestMode(0)       # → 1，EC恢复自动控制
ShutdownWinIo()                      # → 1
```

### 实测 RPM 对照（duty 0-255）

| Duty | 近似 RPM | 占空比 |
|------|----------|--------|
| 80 | ~3800 | 31% |
| 100 | ~4500 | 39% |
| 150 | ~5800 | 59% |
| 200 | ~6500 | 78% |
| 255 | ~7200-7400 | 100% |

### 重要发现

1. **ATKACPI DSTS 在测试模式下不更新真实 RPM** — EC 进入测试模式后，ATKACPI 的转速寄存器返回缓存/旧值。只有 AsusSAIO 的 `HealthyTable_FanRPM` 反映真实转速。
2. **AsusSAIO 的 RPM 和 ATKACPI 的 RPM 存在缩放差异** — 两者读数不完全一致，但趋势相同。
3. **退出测试模式后 EC 平滑接管** — 风扇不会停转，而是按 EC 当前曲线逐步调速。
4. **InitializeWinIo 失败原因** — 非 SYSTEM 权限（error=5 on device open）。驱动已运行但设备对象 DACL 只允许 SY。

## 已排除的路径

| 路径 | 结果 | 原因 |
|------|------|------|
| ATKACPI DEVS 写 100% 曲线 | CPU 仍 ~4000 RPM | EC 固件硬性限速 |
| ATKACPI DEVS 写 range {255,255} | 同上 | 同一个 EC 解释器 |
| PerformanceManual 模式 (4) | 无效 | EC 不因此解锁限速 |
| GHelper 校准模式 | 只测量不突破 | 同样走 ATKACPI |
| AsusWinIO 管理员权限调用 | InitializeWinIo=0 | 设备对象拒绝非 SYSTEM |
| ASHW RPC | 不可用 | ASUSSystemDiagnosis 服务 Disabled |

## 运行 SYSTEM 代码的方法

```powershell
# 方法：计划任务（已验证可行）
schtasks /Create /TN "TaskName" /TR "script.bat" /SC ONLOGON /RU SYSTEM /RL HIGHEST /F
schtasks /Run /TN "TaskName"

# 注意：
# - 需要管理员权限创建
# - /SC ONLOGON 确保开机自启
# - /RU SYSTEM 确保 SYSTEM 身份
# - 一次性测试用 /SC ONCE + 手动 /Run
```
