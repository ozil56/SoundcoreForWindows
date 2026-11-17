# Windows 工具开发说明

v2.5 将托盘整合为七个入口，具体使用方式见仓库首页和包内使用说明。

## 构建与测试

使用 Windows x64、Python 3.13（含 Tk），在本目录执行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py" -v
.\.venv\Scripts\python.exe build_release.py
```

源码可用 pythonw.exe 启动，发布 EXE 为 console=False。构建按显式清单生成便携目录、ZIP、SHA256，拒绝个人配置、音频、日志和模型，不覆盖同版本包。

## 实现边界

- `soundcore_microphone.py`：BLE 生命周期、WASAPI、单实例和本地命令；`tray_app.py`：七项菜单。
- `workflow_panels.py`：连接与自动转写概览；`device_manager.py`：设备文件操作；`self_service_panel.py`：当前一批本地音频。
- `settings_panel.py`：集中设置；`dictionary_panel.py` / `vocabulary.py`：通用与项目词库。
- `auto_sync.py`：校验后下载删除、持久转写队列、Ollama 后处理队列。
- `transfer_batch.py` / `wifi_transfer.py` / `windows_wifi.py`：共用临时热点、WSS 快传、Windows WLAN 切换、蓝牙回退和恢复；`wifi_fast_transfer` 默认 true。
- `asr_worker.py`：Qwen3-ASR 分段与自适应恢复；`asr_process.py`：冻结程序与外部 Python 的 DLL / 环境隔离。
- `ollama_summary.py`：使用 Ollama `/api/chat` 结构化输出。逐段完整润色、层级归并要点、记录检查点，不覆盖原文。

短于设置阈值（默认 120 秒）或有效内容不足 80 字时跳过 Ollama。原始转写先完成；纪要状态单独保存为 pending/running/complete/skipped/failed。失败后退避重试，进度保留。模型串行使用显存，ASR 队列优先。

ASR 默认单模型每批最多两段，每段最长 60 秒、输出上限 4096 tokens，可设置为每批 1/2 段、15–60 秒、512–8192 tokens。同一录音按段长分组，保存原始索引并按时间发布。逐行检查 EOS、重复和空白输出；批 OOM 或尾批单段 OOM 均进入单段有界细分。只释放本进程未用显存。批内中断可重算本批，已提交批次保留。参数和策略均进入断点签名；不兼容断点先归档，不再迁移旧的连续前缀断点。

纪要按全文润色、原文待办提取、去重、独立复核、主要内容的顺序生成。引用连续原文，复核不接收候选的旧具体工作和分类。数字单位、结构和 ID 校验失败时有限重试，数值仍不通过时保留引用并给出待核实说明，结构错误明确失败。主要内容按换行分条，分号保留，以客观介绍开头并校验混入的执行安排。缓存规则为 paragraphs-v3 / actions-office-v5 / review-office-v4 / main-detail-office-v8；概要绑定复核待办哈希，旧版相同自动上下文的正文缓存可复用。上下文长度独立设置，0 时沿用 8192/16384。

使用自动计数的设备清单读取不下载、不删除，遵守录音抢占。未连接时只能显示上次读取或未知，预计运行时间取决于设置、设备空闲和电脑运行状态。

v2.5.4 热词策略为 `full-vocabulary-spelling-pinyin-near-v2`：每段首轮提示为空，使用首轮文字检索全部已选通用/专用词库，非空候选才在同一模型、同一音频上补识别。按文字命中、完整同音、4–10音节纯汉字词的一次音节增删替换评分；同分按长度及稳定摘要排序。每段最多70条、1100字符，调用方不能放大上限；不填固定前缀、不对小词库全量提示、不做词条替换。导入使用NFKC并去BOM/空行；已存词库不自动改写。结果包含首轮文字、所选候选、阶段耗时及短段异常重试去提示记录。历史reference不再参与识别。断点采用 `office-batch2-v6` 并包含热词策略，首轮与补识别全部成功后才提交本批。

## 本地控制

```powershell
.\SoundcoreMicrophone.exe --tray --data-dir "D:\SoundcoreData"
.\SoundcoreMicrophone.exe --command sync-now --data-dir "D:\SoundcoreData"
.\SoundcoreMicrophone.exe --command local-now --data-dir "D:\SoundcoreData"
```

`devices`/`show`、`files`、`sync`、`transcribe`、`vocabulary`、`settings` 打开六个面板；`quit` 退出。`disconnect` 暂断 30 秒、`reconnect` 立即重连。旧内部 pause/resume 命令保留用于设备管理，但不在菜单公开。

测试使用模拟设备和真实 Tk 控件，不删除设备实际录音。涵盖下载完整性、跨天调度、优先队列、词库版本、文件保留、输出上限细分、短录音跳过和完整纪要分段。

Ollama API 依据：[Chat API](https://docs.ollama.com/api/chat)、[Structured outputs](https://docs.ollama.com/capabilities/structured-outputs)。

## 本地转写环境（可选）

麦克风与手动录音管理不需要安装模型。自动转写使用独立环境，与托盘程序的打包依赖隔离：

```powershell
py -3.13 install_local_asr.py --data-dir "D:\SoundcoreData"
```

需要 Windows x64 Python 3.13；安装器下载 PyTorch CUDA 12.8、Qwen3-ASR-1.7B-hf 权重及依赖，合计需要数 GB 下载和更多解压空间。无 NVIDIA GPU 可加 `--cpu`，速度需要在目标机器验证。首次安装联网下载公开模型；转写进程设置离线模式，不上传音频。

完成后右键托盘 → **设置**，配置运行程序、模型目录并保存；日常自动功能按需启用。只转写外部音频时可直接用 **自助转写**，无需开启日常自动功能。模型权重不放进基础 ZIP。模型固定在 `bcd2b5b7f32b480ab5790554cfa8347f246a14f3`，遵循其 Apache-2.0 许可；依赖与模型的许可仍独立适用。

每日时间段使用本机时间，默认 00:00–06:00。20:00–08:00 表示当天晚上到次日上午；结束时间不包含在运行区间，开始/结束相同时为全天。不会自动唤醒或启动关机的电脑，跨过整个窗口的任务等下次窗口。

下载仅在停止录音至少 15 秒后进行。默认通过 BLE 打开录音豆临时热点并切换到 WSS，同一批录音只建一次热点；结束/取消后重订阅原接收器的 BLE 通知、刷新加密会话，恢复原 WLAN 并删除临时网络配置。低电量、无无线网卡或快传失败时回退蓝牙；设置中可关闭 Wi-Fi 快传。设备物理录音开始、关闭自动下载或到达时间段结束时取消未完成传输，下次从文件开头重试；已完成音频不重复下载。自动删除默认关闭，启用后在蓝牙恢复后重新核对本地 SHA256 再删除，并以重新读取设备列表确认结果；删除失败时下次复用已保存文件重试。

音频旁 `.wav.json` 是完成凭证，队列会导入预设目录内已有的完成下载。文本按设备录音开始时间（本机时区）归档为 `转写文本/YYYY/MM/DD/HH-mm-ss_设备标识_文件编号.txt`，无有效设备时间时显式标注音频文件修改时间。每份文本有分段元数据 `.txt.json`；同名已有文本不覆盖。改变文本根目录仅影响之后的输出，不移动旧文件或重新转写已完成任务。

`sync-state.json` 保存队列；`transcription-cache/` 保存已完成分段。程序退出或时间段结束后，下次从已完成分段继续；中断时正在识别的那一段会重新识别。队列为空 60 秒后释放模型进程和显存。取消自动转写不会删除已下载音频。原始文本保持识别原文；Ollama 润色另存纪要，按已保存的分组进度续做，并在分组边界遵守时间段和 ASR 优先级。

## 反馈

提交问题时附 Windows 版本、蓝牙适配器型号、主固件版本、复现步骤与错误文字即可。设备标识、录音、账号资料和解密密钥不需要提交。报告其他型号或固件的兼容性时，请区分扫描、连接、音频、下载和删除分别是否验证。
