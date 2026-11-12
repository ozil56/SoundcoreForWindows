# 来源与第三方组件

Windows BLE 协议实现参考 [tacshi/Soundcore](https://github.com/tacshi/Soundcore)，基于提交 `f01a19c227aac0cee9a7c851b3f6b69cf5540a24` 中的协议文档、实时流、离线文件传输与设备加密实现。上游 Flutter 源码与历史保留在本 fork 中，Windows 代码位于独立目录。

主要依赖：

| 组件 | 来源 |
| --- | --- |
| Python / Tcl / Tk | https://www.python.org/ / https://www.tcl-lang.org/ |
| Bleak / PyWinRT | https://github.com/hbldh/bleak / https://github.com/pywinrt/pywinrt |
| cryptography | https://github.com/pyca/cryptography |
| NumPy | https://numpy.org/ |
| PyOgg / Xiph 音频库 | https://github.com/Zuzu-Typ/PyOgg / https://xiph.org/ |
| sounddevice / PortAudio | https://github.com/spatialaudio/python-sounddevice / https://www.portaudio.com/ |
| pystray | https://github.com/moses-palmer/pystray |
| Pillow | https://python-pillow.org/ |
| PyInstaller | https://pyinstaller.org/ |

发布构建从相应 Python 发行包复制可用的许可证文件，并附上 Xiph、PortAudio、Tcl/Tk 的许可文本。各组件保留其原有版权与许可条款；本仓库没有替上游或第三方组件指定新许可证。

VB-CABLE 是 VB-Audio 的独立虚拟声卡产品：<https://vb-audio.com/Cable/>。请从官方渠道取得并按其许可使用。Typeless 同样由其厂商独立提供，不包含在本项目中。
