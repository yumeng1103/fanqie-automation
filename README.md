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

## 10. 免责声明

本项目仅供学习与个人阅读辅助，请勿用于刷量、刷收益等违反平台协议的行为；使用过程中产生的账号风险由使用者自行承担。
