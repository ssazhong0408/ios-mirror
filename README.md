# iOS Screen Mirror

赛博朋克霓虹风格的 iOS 实时屏幕镜像工具。通过 USB 连接 iOS 设备，实时查看屏幕画面，支持截图、滚屏长图等功能。

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Platform](https://img.shields.io/badge/Platform-macOS-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

## 功能特性

- **实时屏幕镜像** — USB 连接 iOS 设备，一键截取当前屏幕
- **滚屏截图** — 支持手动逐屏截取和自动滚动截取（iOS 17+），自动拼接为长图
- **图像拼接算法** — 基于 numpy 模板匹配，自动检测相邻帧重叠区域并无缝拼合
- **剪贴板支持** — 截图直接复制为 PNG 到系统剪贴板
- **iOS 17+ 适配** — 自动挂载 DeveloperDiskImage、自动管理 tunneld 服务
- **赛博朋克 UI** — 深紫黑底色 + 青色/品红/紫色霓虹配色，呼吸脉冲动画

## 截图预览

赛博朋克霓虹风格界面，双层霓虹辉光边框 + 四角角标装饰：

- 标题栏内联显示分辨率、截图数、刷新时间
- 设备信息紧凑展示在标题栏右侧
- 连接状态呼吸脉冲动画指示

## 环境要求

- macOS 系统
- Python 3.10+
- libimobiledevice（系统工具）
- iOS 设备 + USB 数据线

## 快速开始

### 一键安装

```bash
bash setup.sh
```

安装脚本会自动检测并安装 Homebrew、Python3、libimobiledevice 以及所有 Python 依赖。

### 手动安装

```bash
# 安装系统依赖
brew install libimobiledevice

# 安装 Python 依赖
pip3 install -r requirements.txt
```

### 运行

```bash
python3 ios_mirror.py
```

## 快捷键

| 快捷键 | 功能 |
|--------|------|
| `F5` | 刷新屏幕 |
| `F6` | 滚屏截取 |
| `F7` | 拼接长图 |
| `Cmd+C` | 复制截图到剪贴板 |
| `Cmd+S` | 保存截图到文件 |

## 滚屏截图

提供两种模式：

**手动模式** — 在手机上手动滚动到目标位置，每停一次按 F6 截取一帧，最多 10 帧。截完后按 F7 拼接为长图并保存。

**自动模式** — 程序通过 DVT 触控模拟自动在设备上执行滑动手势（需要 iOS 17+ 和 tunneld），连续截取多帧后自动拼接。如果触控模拟不可用会自动降级提示。

## 依赖

| 依赖 | 用途 |
|------|------|
| `customtkinter` | GUI 框架 |
| `Pillow` | 图像处理 |
| `numpy` | 图像拼接算法 |
| `pymobiledevice3` | iOS 17+ 截图 & tunneld |
| `libimobiledevice` | iOS < 17 截图（系统工具） |

## iOS 17+ 注意事项

1. 设备需处于**解锁**状态
2. 首次连接需在手机上点击**「信任此电脑」**
3. 程序会自动挂载 DeveloperDiskImage 并启动 tunneld（需要输入 macOS 密码）
4. 如自动启动 tunneld 失败，可手动运行：
   ```bash
   sudo python3 -m pymobiledevice3 remote tunneld
   ```

## 项目结构

```
ios-mirror/
├── ios_mirror.py       # 主程序（GUI + 设备管理 + 滚屏截图）
├── requirements.txt    # Python 依赖
├── setup.sh           # 一键安装脚本
└── README.md          # 项目说明
```

## License

MIT
