# 番茄小说 自动翻页 + 点催更

基于 Python + uiautomator2 控制安卓真机/模拟器上的「番茄免费小说」App：

- 自动翻页阅读（左右翻页 / 上下滚动 / 点击右缘三种模式）
- 自动跳过章末广告、关闭弹窗、点击「下一章」继续
- 自动点「催更」（同一天同一本书只点一次，状态记录在 `state.json`）
- 拟人化处理：随机翻页间隔、随机滑动轨迹、偶尔长停顿

> ⚠️ 本工具面向个人阅读便利。长时间挂机刷阅读时长/金币收益违反番茄平台用户协议，可能触发风控导致账号受限，请自行评估风险。

## 目录结构

```
fanqie-automation/
├── fanqie_reader.py      # 主脚本
├── config.yaml           # 配置文件
├── requirements.txt      # Python 依赖
├── setup.bat             # 一键环境准备(虚拟环境 + 依赖 + 设备初始化)
├── run.bat               # 一键启动挂机
├── tools/
│   ├── dump_ui.py        # 控件树调试工具
│   └── platform-tools/   # 便携版 adb(无线连接用, 无需另行安装)
└── README.md
```

## 1. 环境准备

1. **Python 3.10+**（本机已装 3.12.10）
2. 手机：设置 → 关于手机 → 连点「版本号」开启开发者模式 → 开发者选项 → 打开 **USB 调试**
3. 手机安装「番茄免费小说」并登录账号，把想读的书加入书架
4. USB 数据线连接电脑（首次连接在手机上点「允许 USB 调试」）

> 关于 adb：uiautomator2 自带内置 adb，USB 连接无需单独安装。
> 本包附带便携版 adb（`tools\platform-tools\adb.exe`），无线连接也无需另行安装。

## 2. 安装依赖

最快方式：双击 `setup.bat`，自动完成虚拟环境创建、依赖安装和设备初始化（需要联网）。

手动方式：

```powershell
cd <你的项目目录>
pip install -r requirements.txt
```

## 3. 初始化设备（每台设备只需一次）

```powershell
python -m uiautomator2 init
```

初始化会在手机上安装 ATX 守护程序，手机弹窗需允许。看到 `Successfully init AdbDevice` 即成功。

验证连接：

```powershell
python -c "import uiautomator2 as u2; print(u2.connect().device_info)"
```

## 4. 配置

编辑 `config.yaml`，至少填写书名：

```yaml
book:
  name: "你的书名"        # 书架上显示的名字
reader:
  page_mode: swipe       # swipe | scroll | tap
  interval_range: [8, 15] # 每页 8~15 秒随机
  duration_minutes: 60    # 挂 60 分钟; 0 = 无限
urge:
  enabled: true           # 自动点催更
```

翻页模式说明：

| 模式 | 适用场景 |
| --- | --- |
| `swipe` | 左右翻页（默认），番茄阅读器设置为「覆盖/仿真/平移」均可 |
| `scroll` | 阅读器设置为「上下滚动」 |
| `tap` | 点击右缘翻页 |

如果只用番茄 App 自带的「自动阅读」功能，可以把 `duration_minutes` 调长、`interval_range` 调大，让脚本只负责跳过广告和点催更。

## 5. 运行

双击 `run.bat`（使用 setup.bat 创建的虚拟环境），或命令行：

```powershell
python fanqie_reader.py
```

常用参数（均可覆盖配置）：

```powershell
python fanqie_reader.py --book "书名" --minutes 30        # 换书/改时长
python fanqie_reader.py --serial 192.168.1.10             # 无线连接指定设备
```

挂机过程中按 `Ctrl+C` 随时停止。

## 6. 无线连接（可选）

手机与电脑连同一 WiFi，先用 USB 执行一次（本包自带 adb，无需安装 platform-tools）：

```powershell
tools\platform-tools\adb.exe tcpip 5555
```

然后拔掉数据线：

```powershell
tools\platform-tools\adb.exe connect 192.168.1.10:5555   # 换成手机的实际 IP(设置→关于手机→状态)
```

在 `config.yaml` 中把 `device.serial` 填为 `192.168.1.10`（不带端口），或运行时加 `--serial`。

> 以下章节中的 `adb` 命令均可替换为 `tools\platform-tools\adb.exe`。

## 7. 模拟器（可选）

| 模拟器 | 连接命令 |
| --- | --- |
| 雷电 | `adb connect 127.0.0.1:5555` |
| 夜神 | `adb connect 127.0.0.1:62001` |
| MuMu | `adb connect 127.0.0.1:7555` |

连接后在 `config.yaml` 填写对应 `device.serial`。

## 8. 控件定位调试

不同版本的番茄 App 界面略有差异，如果脚本找不到按钮（日志报「找不到《xxx》」或催更不生效），用 dump 工具查看真实控件文字：

```powershell
python tools/dump_ui.py
```

- 会在 `ui_dump.xml` 里输出当前界面完整控件树
- 搜 `text="催更"`、`text="跳过"` 等确认按钮的实际文字
- 若弹窗关闭按钮是别的文字，把它加到 `config.yaml` 的 `reader.extra_close_texts` 里

## 9. 常见问题

| 现象 | 处理 |
| --- | --- |
| `无法连接设备` | 检查 USB 调试授权；重新执行 `python -m uiautomator2 init`；无线模式确认已 `adb connect` |
| 找不到书 | 确认书名与书架显示一致；确认书已加入书架；用 `dump_ui.py` 查看书架控件文字 |
| 翻页不动 | 检查阅读器翻页模式与 `page_mode` 是否匹配 |
| 催更不生效 | 用 `dump_ui.py` 进入书籍主页确认按钮文字；「已催更」后当天不会再点 |
| 广告点不掉 | 用 `dump_ui.py` 看广告页关闭按钮文字，加入 `extra_close_texts` |

## 10. OpenAI 视觉逐页阅读与章节总结

控制台的「AI 阅读」页可以在现有翻页任务运行时读取设备截图：每次翻页前投递一张正文截图，视觉模型提取正文；检测到「本章讨论」/章末卡片后，等待本章页面完成并生成一段摘要。摘要、识别计数、章节、错误和最近 30 条摘要会实时显示，也会写入 `vision_summaries.json` 供服务重启后查看。

视觉阅读是独立能力，未配置或调用失败不会停止翻页、催更、礼物、书评等原有流程。建议用环境变量提供 Key：

```powershell
$env:OPENAI_API_KEY = "sk-..."
python app.py
```

也可以在控制台「AI 阅读」页填写接口地址、模型、图像细节、并发和页数上限。页数上限按“每本书、每次任务运行”分别计数，不是所有书共享一个总数；达到上限后只停止该书的 AI 截图识别，原有自动翻页仍会继续。「书籍管理」中可以为每本书单独关闭“AI总结”；全局视觉阅读开启后，只有勾选该项的书才会上传截图并生成章节摘要。旧版书籍配置缺少该字段时默认开启。「检测模型」会调用当前接口的 `GET /models` 并生成模型下拉候选；API Key 输入框可临时切换显示/隐藏。配置会写入 `config.yaml` 的 `ai.vision` 节；服务端接口返回和高级配置中的 API Key 始终脱敏。默认使用 OpenAI-compatible 的 `https://api.openai.com/v1` 和 `gpt-4o-mini`，中转服务不支持 Responses API 时会自动回退到 `/chat/completions`。

启动控制台并部署到局域网：

```powershell
python app.py
# 浏览器访问 http://127.0.0.1:8899
```

服务监听 `0.0.0.0:8899`，可由现有 `run.bat`、任务计划程序或 Windows 服务托管。不要把 `config.yaml`、`vision_summaries.json` 或包含 Key 的日志复制到公共目录；反向代理部署时请限制控制台访问来源。

离线协议测试：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

相关接口：`GET /api/vision-config`（脱敏配置）、`POST /api/vision-config`（保存配置）、`POST /api/vision-models`（检测模型）、`GET /api/vision/status`（视觉状态与摘要），现有 `GET /api/status` 同时包含每台设备的 `vision` 字段。

## 11. 网页自动发布作家后台章节

控制台的「自动发布」页签融合了部署包中的 Playwright 发文能力，不再启动 PyWebView 桌面窗口。它支持：

- 在本机 Edge 中扫码登录番茄作家后台，登录状态只保存在 `publisher_data/account_state.json`
- 扫描 `publisher_data/chapters/<书名>/` 下的 TXT 章节，立即发布、定时发布和重新提交
- 实时查看当前章节、进度、成功/失败/跳过数量和脱敏日志
- 只有提交面板关闭并且平台章节管理列表能找到对应章节时，才把源稿移动到归档目录

首次使用时打开 `http://127.0.0.1:8899`，进入「自动发布」，确认目录后点击「扫码登录」。如果需要改目录，直接在页签中修改并保存；待发目录和归档目录不能相同或互相包含。服务只允许回环地址调用自动发布接口，局域网其他设备仍可使用原有阅读控制，但不能触发作家账号发布。

自动发布依赖 Playwright。项目虚拟环境安装 Python 包后，优先使用本机 Edge；如果机器没有可探测的 Edge/Chrome，再执行：

```powershell
.venv\Scripts\python.exe -m playwright install chromium
```

发布配置、登录态、原稿和归档目录均已加入 Git 忽略规则。不要手动把 `publisher_data`、`chapters`、`uploaded` 或 `state.json` 复制到公共仓库。

## 12. 免责声明

本项目仅供学习与个人阅读辅助，请勿用于刷量、刷收益等违反平台协议的行为；使用过程中产生的账号风险由使用者自行承担。
