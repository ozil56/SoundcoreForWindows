# Windows 工具开发说明

本目录包含独立 Windows 实现，不参与上游 Flutter 构建。使用 Python 3.13 x64（含 Tk）；在 PowerShell 中从本目录运行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe soundcore_microphone.py
```

源代码启动可用 `pythonw.exe` 避免控制台；发布的 EXE 使用 PyInstaller `console=False`。

## 测试与打包

```powershell
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py" -v
.\.venv\Scripts\python.exe build_release.py
```

18 项测试使用模拟设备、虚拟注册表及真实 Tk 控件，不访问录音豆、不改动系统自启动，也不会删除真实录音。Tk 测试窗口短暂创建后隐藏。

输出在 `release/`，包括便携目录、ZIP 和 SHA256。构建脚本从明确的程序/源码清单组装包，拒绝混入设备配置、诊断日志或音频；不会覆盖已存在的同版本发布包。依赖许可来自已安装发行包和 `licenses/`，保留在分发包中。

CI：`.github/workflows/windows-toolkit.yml`，使用 Windows runner、Python 3.13，以及固定提交版本的 GitHub Actions。CI 包不含第三方虚拟声卡驱动，麦克风用途需按主页说明安装 VB-CABLE。

## 结构

| 文件 | 职责 |
| --- | --- |
| `soundcore_stream.py` | BLE 帧、ECDH/HKDF、AES-CTR、Opus 解码、实时音频接收 |
| `soundcore_microphone.py` | 托盘、Tk 主窗口、虚拟声卡输出、自动重连与进程控制 |
| `recorder_files.py` | 设备发现与选择、文件分页、完整 WAV 下载、校验后删除 |
| `device_manager.py` | 设备选择和录音管理面板，独占使用 BLE 并恢复麦克风状态 |
| `bridge_setup.py` | 当前用户自启动和环境检查 |
| `build_release.py` | 可重复执行的构建入口与明确清单打包 |

麦克风与文件管理共享协议实现，但不会同时占用连接。文件导出必须核对请求文件 ID、密钥 magic、序号、结束通知和完整长度；只有完成后才将临时文件改名为最终 WAV。

## 本地进程控制

```powershell
.\SoundcoreMicrophone.exe --tray --data-dir "D:\SoundcoreData"
.\SoundcoreMicrophone.exe --command files --data-dir "D:\SoundcoreData"
.\SoundcoreMicrophone.exe --command pause --data-dir "D:\SoundcoreData"
.\SoundcoreMicrophone.exe --command resume --data-dir "D:\SoundcoreData"
```

支持 `show`、`devices`、`files`、`pause`、`resume`、`reconnect`、`quit`。所有命令需使用与正在运行的实例相同的 `--data-dir`。进程通过当前 Windows 会话内的命名事件通信，没有网络监听端口。

`files_cli.py` 是可选开发诊断入口，使用前先暂停托盘桥接。`--scan` 仅扫描；不带该参数时只访问数据目录中已选定的设备。它没有删除录音的命令行选项。

## 反馈

提交问题时附 Windows 版本、蓝牙适配器型号、主固件版本、复现步骤与错误文字即可。设备标识、录音、账号资料和解密密钥不需要提交。报告其他型号或固件的兼容性时，请区分扫描、连接、音频、下载和删除分别是否验证。
