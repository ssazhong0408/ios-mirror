#!/usr/bin/env python3
"""
iOS Screen Mirror — 实时查看 iOS 设备屏幕、截图与复制
=====================================================
通过 USB 连接 iOS 设备，实时镜像屏幕画面。
支持刷新、复制截图到剪贴板、保存截图到文件。

依赖:
  - customtkinter (GUI)
  - Pillow (图像处理)
  - libimobiledevice (系统工具: idevice_id, ideviceinfo)
  - pymobiledevice3 (iOS 17+ 截图支持)
  - pyobjc-framework-AppKit (可选, macOS 剪贴板增强)

用法:
  python3 ios_mirror.py
"""

import customtkinter as ctk
from PIL import Image, ImageTk
import subprocess
import tempfile
import signal
import os
import io
import sys
import json
import threading
import time
import math
from datetime import datetime
from pathlib import Path

# ============================================================
# 剪贴板工具 — 优先使用 pyobjc, 回退到 osascript, 再回退到 Pillow
# ============================================================

def _copy_png_to_clipboard_macos(pil_image: Image.Image) -> bool:
    """
    将 PIL Image 以 PNG 格式写入 macOS 剪贴板。
    依次尝试三种方式，确保最大兼容性。
    """
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    png_data = buf.getvalue()

    # 方式 1: pyobjc (最可靠)
    try:
        from AppKit import NSPasteboard, NSData
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        ns_data = NSData.dataWithBytes_length_(png_data, len(png_data))
        pb.setData_forType_(ns_data, "public.png")
        return True
    except ImportError:
        pass

    # 方式 2: osascript + 临时文件 (无需额外依赖)
    try:
        tmp_path = tempfile.mktemp(suffix=".png")
        pil_image.save(tmp_path, format="PNG")
        script = f'''
        use framework "AppKit"
        set theData to current application's NSData's dataWithContentsOfFile:"{tmp_path}"
        set pb to current application's NSPasteboard's generalPasteboard()
        pb's clearContents()
        pb's setData:theData forType:"public.png"
        '''
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=5
        )
        os.unlink(tmp_path)
        if result.returncode == 0:
            return True
    except Exception:
        pass

    # 方式 3: Pillow 内置 (需要 python3 以 framework 方式安装)
    try:
        from PIL import ImageGrab  # noqa: F401
        pil_image_rgba = pil_image.convert("RGBA")
        pil_image_rgba.copy()
    except Exception:
        pass

    return False


# ============================================================
# Tunneld 管理器 — 管理 pymobiledevice3 tunneld 守护进程
# ============================================================

class TunneldManager:
    """
    管理 pymobiledevice3 的 tunneld 守护进程。
    iOS 17+ 的开发者服务需要通过 tunneld 建立隧道才能访问。
    """

    TUNNELD_PORT = 49151

    def __init__(self):
        self._tunneld_pid: int | None = None
        self._started_by_us = False

    @staticmethod
    def is_running() -> bool:
        """检查 tunneld 是否已在运行。"""
        try:
            r = subprocess.run(
                ["pgrep", "-f", "pymobiledevice3 remote tunneld"],
                capture_output=True, text=True, timeout=3
            )
            return r.returncode == 0 and r.stdout.strip() != ""
        except Exception:
            return False

    @staticmethod
    def get_tunnel_info() -> dict | None:
        """从 tunneld REST API 获取隧道信息。"""
        try:
            import urllib.request
            url = f"http://127.0.0.1:{TunneldManager.TUNNELD_PORT}/"
            req = urllib.request.urlopen(url, timeout=3)
            data = json.loads(req.read())
            return data
        except Exception:
            return None

    def start(self) -> bool:
        """
        启动 tunneld 守护进程。
        需要管理员权限，会通过 macOS 系统对话框请求密码。
        返回是否成功。
        """
        if self.is_running():
            return True

        try:
            python_path = sys.executable
            # 使用 osascript 以管理员权限启动 tunneld
            script = (
                f'do shell script '
                f'"{python_path} -m pymobiledevice3 remote tunneld '
                f'> /tmp/tunneld.log 2>&1 &" '
                f'with administrator privileges'
            )
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=30
            )
            if r.returncode != 0:
                return False

            # 等待 tunneld 启动
            for _ in range(15):
                time.sleep(1)
                if self.is_running():
                    self._started_by_us = True
                    return True

            return False
        except Exception:
            return False

    def stop(self):
        """如果 tunneld 是由我们启动的，停止它。"""
        if not self._started_by_us:
            return
        try:
            r = subprocess.run(
                ["pgrep", "-f", "pymobiledevice3 remote tunneld"],
                capture_output=True, text=True, timeout=3
            )
            if r.returncode == 0:
                for pid in r.stdout.strip().splitlines():
                    pid = int(pid.strip())
                    os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        self._started_by_us = False

    def ensure_ready(self) -> tuple[bool, str]:
        """
        确保 tunneld 已就绪。如果未运行则尝试启动。
        返回 (success, message)。
        """
        if self.is_running():
            return True, "tunneld 已在运行"

        # 需要启动
        started = self.start()
        if started:
            return True, "tunneld 已启动"
        else:
            return False, (
                "需要管理员权限启动 tunneld 服务。\n"
                "请在终端手动运行:\n"
                "  sudo python3 -m pymobiledevice3 remote tunneld"
            )


# ============================================================
# 设备管理器 — 封装 libimobiledevice + pymobiledevice3
# ============================================================

class DeviceManager:
    """
    封装 iOS 设备截图功能，支持多种后端:
      1. pymobiledevice3 dvt screenshot (iOS 17+, 需要 tunneld)
      2. idevicescreenshot (libimobiledevice CLI, iOS < 17)

    iOS 17+ 注意:
      - 需要设备处于解锁状态
      - 需要挂载 DeveloperDiskImage (Personalized Image)
      - 需要 tunneld 运行
      - 首次连接需在设备上点"信任此电脑"
    """

    def __init__(self):
        self.udid: str | None = None
        self.tmp_dir: str | None = None
        self.ddi_mounted = False
        self.ddi_mounting = False
        self.ios_version: str = ""
        self.ios_major: int = 0
        self.last_error: str = ""
        self._has_pymobiledevice3 = self._check_pymobiledevice3()
        self.tunneld = TunneldManager()

    @staticmethod
    def _check_pymobiledevice3() -> bool:
        try:
            import pymobiledevice3  # noqa: F401
            return True
        except ImportError:
            return False

    # ---------- 设备发现 ----------

    @staticmethod
    def list_devices() -> list[str]:
        """返回通过 USB 连接的 iOS 设备 UDID 列表。"""
        try:
            r = subprocess.run(
                ["idevice_id", "-l"],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode != 0:
                return []
            return [line.strip() for line in r.stdout.strip().splitlines() if line.strip()]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

    @staticmethod
    def get_device_info(udid: str | None = None) -> dict[str, str]:
        """获取设备名称、型号、iOS 版本等信息。"""
        cmd = ["ideviceinfo"]
        if udid:
            cmd += ["-u", udid]
        info: dict[str, str] = {}
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    if ": " in line:
                        k, v = line.split(": ", 1)
                        info[k.strip()] = v.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return info

    # ---------- 连接管理 ----------

    def connect(self, udid: str) -> bool:
        self.udid = udid
        self.ddi_mounted = False
        self.tmp_dir = tempfile.mkdtemp(prefix="ios_mirror_")
        return True

    def disconnect(self):
        self.udid = None
        self.ddi_mounted = False
        if self.tmp_dir and os.path.isdir(self.tmp_dir):
            import shutil
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.tmp_dir = None

    # ---------- DeveloperDiskImage 自动挂载 ----------

    def auto_mount_ddi(self) -> tuple[bool, str]:
        """
        自动挂载 DeveloperDiskImage。
        iOS 17+ 使用 Personalized Image，iOS < 17 使用传统 DDI。
        """
        if not self._has_pymobiledevice3:
            return False, "pymobiledevice3 未安装"

        self.ddi_mounting = True
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pymobiledevice3", "mounter", "auto-mount"],
                capture_output=True, text=True, timeout=60
            )
            combined = (r.stdout + "\n" + r.stderr).strip()

            if r.returncode == 0:
                self.ddi_mounted = True
                self.last_error = ""
                return True, "DeveloperDiskImage 挂载成功"

            # 分析错误
            if "DeviceLocked" in combined:
                self.last_error = "设备已锁定，请先解锁 iPhone"
                return False, self.last_error
            elif "already mounted" in combined.lower() or "AlreadyMounted" in combined:
                self.ddi_mounted = True
                return True, "DeveloperDiskImage 已挂载"
            elif "Trust" in combined or "pair" in combined.lower():
                self.last_error = "请在 iPhone 上点击「信任此电脑」"
                return False, self.last_error
            else:
                msg = combined.split("\n")[-1][:150] if combined else "未知错误"
                self.last_error = f"挂载失败: {msg}"
                return False, self.last_error
        except subprocess.TimeoutExpired:
            self.last_error = "挂载超时"
            return False, self.last_error
        except Exception as e:
            self.last_error = f"挂载异常: {e}"
            return False, self.last_error
        finally:
            self.ddi_mounting = False

    # ---------- Tunneld 管理 ----------

    def ensure_tunneld(self) -> tuple[bool, str]:
        """确保 tunneld 正在运行 (iOS 17+ 需要)。"""
        return self.tunneld.ensure_ready()

    # ---------- 截图 ----------

    def take_screenshot(self) -> Image.Image:
        """
        截取 iOS 设备当前屏幕，返回 PIL.Image (RGB)。
        自动选择最佳截图方式。
        """
        if not self.udid or not self.tmp_dir:
            raise RuntimeError("设备未连接")

        # iOS 17+ 优先使用 pymobiledevice3 DVT (通过 tunneld)
        if self.ios_major >= 17 and self._has_pymobiledevice3:
            try:
                return self._screenshot_pymobile_dvt()
            except Exception as e:
                err_msg = str(e)
                # 如果 tunneld 未运行，尝试启动后重试
                if "tunneld" in err_msg.lower() or "tunnel" in err_msg.lower():
                    ok, msg = self.ensure_tunneld()
                    if ok:
                        try:
                            return self._screenshot_pymobile_dvt()
                        except Exception as e2:
                            raise RuntimeError(
                                f"截图失败: {e2}\n"
                                f"请确保:\n"
                                f"1. iPhone 已解锁\n"
                                f"2. 已信任此电脑\n"
                                f"3. 已运行 tunneld (sudo python3 -m pymobiledevice3 remote tunneld)"
                            )
                    else:
                        raise RuntimeError(msg)
                raise

        # iOS < 17: 尝试 idevicescreenshot
        try:
            return self._screenshot_idevice()
        except Exception:
            pass

        # 回退: pymobiledevice3 legacy
        if self._has_pymobiledevice3:
            try:
                return self._screenshot_pymobile_legacy()
            except Exception:
                pass

        err = self.last_error or "截图失败"
        raise RuntimeError(err)

    def _screenshot_pymobile_dvt(self) -> Image.Image:
        """使用 pymobiledevice3 DVT screenshot (iOS 17+)。"""
        png_path = os.path.join(self.tmp_dir, f"screen_{time.time_ns()}.png")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pymobiledevice3",
                 "developer", "dvt", "screenshot", png_path,
                 "--tunnel", self.udid],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip()
                raise RuntimeError(err or "DVT screenshot 失败")

            if not os.path.exists(png_path):
                raise RuntimeError("截图文件未生成")

            img = Image.open(png_path).convert("RGB")
            return img
        finally:
            if os.path.exists(png_path):
                os.unlink(png_path)

    def _screenshot_idevice(self) -> Image.Image:
        """使用 idevicescreenshot CLI 截图 (TIFF → PNG)。"""
        tiff_path = os.path.join(self.tmp_dir, f"screen_{time.time_ns()}.tiff")
        try:
            r = subprocess.run(
                ["idevicescreenshot", "-u", self.udid, tiff_path],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip()
                raise RuntimeError(err or "idevicescreenshot 失败")

            img = Image.open(tiff_path).convert("RGB")
            return img
        finally:
            if os.path.exists(tiff_path):
                os.unlink(tiff_path)

    def _screenshot_pymobile_legacy(self) -> Image.Image:
        """使用 pymobiledevice3 legacy ScreenshotService。"""
        import asyncio

        async def _capture():
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.screenshot import ScreenshotService

            lockdown = await create_using_usbmux()
            try:
                service = ScreenshotService(lockdown)
                img_data = await service.take_screenshot()
                await service.close()
                return img_data
            finally:
                await lockdown.close()

        data = asyncio.run(_capture())
        return Image.open(io.BytesIO(data)).convert("RGB")


# ============================================================
# 工具: 霓虹辉光颜色插值
# ============================================================

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """将 #RRGGBB 转换为 (R, G, B) 元组。"""
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    """将 (R, G, B) 转换为 #RRGGBB。"""
    return f"#{r:02x}{g:02x}{b:02x}"


def _lerp_color(c1: str, c2: str, t: float) -> str:
    """在两个十六进制颜色之间线性插值。t=0 返回 c1, t=1 返回 c2。"""
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return _rgb_to_hex(r, g, b)


# ============================================================
# 滚屏截图管理器 — 支持手动逐屏 + 自动滚动 + 图像拼接
# ============================================================

class ScrollCaptureManager:
    """
    管理滚屏截图流程:
      - manual: 用户手动滚动设备，每次点击按钮截取一屏
      - auto:   程序自动在设备上执行滑动手势，连续截取多屏
    截取完成后自动拼接为一张长图。
    """

    def __init__(self):
        self.mode: str = "manual"   # "manual" | "auto"
        self.images: list[Image.Image] = []
        self._capturing = False
        self._running = False       # 控制自动循环是否继续
        self.result: Image.Image | None = None
        self.on_progress: callable | None = None  # (captured, total) callback
        self.on_done: callable | None = None      # (result_image) callback
        self.on_error: callable | None = None     # (error_msg) callback

    @property
    def count(self) -> int:
        return len(self.images)

    def reset(self):
        """重置状态，清空已截取的图片。"""
        self.images.clear()
        self._capturing = False
        self._running = False
        self.result = None

    def add_screenshot(self, img: Image.Image):
        """添加一张截图到队列。"""
        self.images.append(img.copy())
        if self.on_progress:
            self.on_progress(len(self.images), 0)

    # ==================== 手动模式 ====================

    def start_manual(self):
        """进入手动逐屏截取模式。"""
        self.reset()
        self._capturing = True
        self.mode = "manual"

    def manual_capture(self, img: Image.Image):
        """手动模式: 添加当前屏幕截图。"""
        if not self._capturing:
            return
        self.add_screenshot(img)

    # ==================== 自动模式 ====================

    def start_auto(self, device_mgr, total_screens: int = 5,
                    scroll_delay: float = 0.8):
        """
        自动滚屏截图 (iOS 17+ 需要 tunneld)。
        在后台线程中执行: 截图 → 模拟滑动 → 截图 → ... → 拼接。
        """
        self.reset()
        self._capturing = True
        self._running = True
        self.mode = "auto"
        threading.Thread(
            target=self._auto_loop,
            args=(device_mgr, total_screens, scroll_delay),
            daemon=True
        ).start()

    def stop_auto(self):
        """停止自动滚屏 (截取已完成的帧并拼接)。"""
        self._running = False

    def _auto_loop(self, device_mgr, total: int, delay: float):
        """自动截图 + 模拟滚动的后台循环。"""
        try:
            for i in range(total):
                if not self._running:
                    break

                # 1. 截取当前屏幕
                img = device_mgr.take_screenshot()
                self.add_screenshot(img)

                if self.on_progress:
                    self.on_progress(i + 1, total)

                # 2. 如果不是最后一张，模拟滑动
                if i < total - 1 and self._running:
                    try:
                        self._simulate_scroll(device_mgr)
                    except Exception as e:
                        # 滑动失败，停止自动模式但保留已截取的帧
                        if self.on_error:
                            self.on_error(f"自动滚动失败: {e}\n已截取 {i+1} 帧")
                        break
                    time.sleep(delay)

            # 3. 循环结束，拼接
            self._capturing = False
            if self.count >= 2:
                self._stitch_all()
                if self.on_done:
                    self.on_done(self.result)
            elif self.count == 1:
                self.result = self.images[0]
                if self.on_done:
                    self.on_done(self.result)

        except Exception as e:
            self._capturing = False
            if self.on_error:
                self.on_error(str(e))

    @staticmethod
    def _simulate_scroll(device_mgr):
        """
        在 iOS 设备上模拟向下滑动手势。
        需要 iOS 17+ 和 pymobiledevice3 DVT 触控模拟。
        """
        try:
            from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService
            from pymobiledevice3.services.dvt.testmanagerd import SimulatedEvent

            dvt = DvtSecureSocketProxyService(lockdown=device_mgr._lockdown_client)
            # 模拟从屏幕中部向下滑动
            screen_w, screen_h = 585, 1266  # 逻辑分辨率 (iPhone 12 默认)
            start_y = int(screen_h * 0.7)
            end_y = int(screen_h * 0.3)
            center_x = screen_w // 2

            dvt.simulate_touch_gesture(
                start_x=center_x, start_y=start_y,
                end_x=center_x, end_y=end_y,
                duration=0.4
            )
        except ImportError:
            # pymobiledevice3 版本不支持触控模拟，尝试备选方案
            import asyncio
            asyncio.run(ScrollCaptureManager._scroll_via_accessibility(device_mgr))
        except Exception:
            raise RuntimeError(
                "自动滚动需要 iOS 17+ 且 pymobiledevice3 支持触控模拟。\n"
                "请切换到手动模式。"
            )

    @staticmethod
    async def _scroll_via_accessibility(device_mgr):
        """备选方案: 通过 accessibility 服务触发滚动。"""
        try:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.accessibilityaudit import AccessibilityAuditService

            lockdown = await create_using_usbmux()
            try:
                service = AccessibilityAuditService(lockdown)
                # 尝试执行向下滚动手势
                await service.perform_action("scroll_down")
                await service.close()
            finally:
                await lockdown.close()
        except Exception as e:
            raise RuntimeError(f"Accessibility 滚动失败: {e}")

    # ==================== 图像拼接 ====================

    def finish(self) -> Image.Image | None:
        """完成截取并拼接所有帧，返回长图。"""
        self._capturing = False
        self._running = False
        if len(self.images) == 0:
            return None
        if len(self.images) == 1:
            self.result = self.images[0].copy()
            return self.result
        self._stitch_all()
        return self.result

    def _stitch_all(self):
        """将所有截图按顺序拼接为一张长图。"""
        if len(self.images) < 2:
            self.result = self.images[0].copy() if self.images else None
            return

        result = self.images[0]
        for i in range(1, len(self.images)):
            result = self._stitch_pair(result, self.images[i])

        self.result = result

    @staticmethod
    def _stitch_pair(img1: Image.Image, img2: Image.Image) -> Image.Image:
        """
        拼接两张上下相邻的截图:
        在 img1 底部和 img2 顶部寻找重叠区域，去除重复后纵向拼合。
        """
        import numpy as np

        w1, h1 = img1.size
        w2, h2 = img2.size

        # 宽度不同则统一为 img2 的宽度
        if w1 != w2:
            img1 = img1.resize((w2, int(h1 * w2 / w1)), Image.LANCZOS)
            w1, h1 = img1.size

        arr1 = np.array(img1)
        arr2 = np.array(img2)

        best_overlap = 0
        best_score = float("inf")

        # 在 img1 底部区域和 img2 顶部区域搜索最佳重叠
        # 取 img1 底部 10px 高的水平条作为搜索模板
        strip_h = 10
        max_search = min(h1 // 2, h2 // 2, 500)
        template = arr1[-strip_h:]

        for overlap in range(strip_h, max_search):
            # 将模板放在 img2 的第 (overlap - strip_h) 行位置进行比对
            y_start = overlap - strip_h
            y_end = y_start + strip_h
            if y_end > h2:
                break
            candidate = arr2[y_start:y_end]
            if candidate.shape != template.shape:
                continue
            diff = np.sum(np.abs(candidate.astype(np.int16) - template.astype(np.int16)))
            if diff < best_score:
                best_score = diff
                best_overlap = overlap

        # 如果匹配置信度太低，退回到简单纵向堆叠
        per_pixel = best_score / max(1, strip_h * w1 * 3)
        if per_pixel > 35:
            best_overlap = 0

        if best_overlap > 0:
            top = arr1[:h1 - best_overlap + strip_h // 2]
            bottom = arr2[best_overlap - strip_h // 2:]
            stitched = np.vstack([top, bottom])
            return Image.fromarray(stitched)
        else:
            # 无重叠: 直接堆叠
            stitched = np.vstack([arr1, arr2])
            return Image.fromarray(stitched)


# ============================================================
# 主应用 — iOS Screen Mirror GUI (Cyberpunk Neon Edition)
# ============================================================

class IOSScreenMirrorApp:
    """iOS 屏幕镜像工具主窗口 — 赛博朋克霓虹版。"""

    # ---- 赛博朋克霓虹主题配色 ----
    BG_DARK        = "#06060f"
    BG_DEEP        = "#0a0a18"
    BG_PRIMARY     = "#0e0e1e"
    BG_CARD        = "#111128"
    BG_SURFACE     = "#1a1a38"
    BG_INPUT       = "#0d0d22"

    # 霓虹主色
    NEON_CYAN      = "#00e5ff"
    NEON_MAGENTA   = "#ff2dcb"
    NEON_PURPLE    = "#8b5cf6"
    NEON_BLUE      = "#3b82f6"

    # 功能色
    SUCCESS        = "#00ff94"
    WARNING        = "#ffb800"
    ERROR          = "#ff2d55"

    # 文字色
    TEXT_PRIMARY   = "#f0f0ff"
    TEXT_SECONDARY = "#7878a0"
    TEXT_DIM       = "#44446a"

    # 辉光色 (用于边框模拟霓虹灯光)
    GLOW_CYAN      = "#002838"
    GLOW_MAGENTA   = "#380028"
    GLOW_PURPLE    = "#1a0038"
    GLOW_SUCCESS   = "#003818"

    # 边框
    BORDER         = "#1a1a3a"
    BORDER_NEON    = "#00354a"

    def __init__(self):
        # --- customtkinter 全局设置 ---
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.title("iOS Screen Mirror")
        self.root.geometry("1020x600")
        self.root.minsize(860, 500)
        self.root.configure(fg_color=self.BG_DARK)

        # macOS 窗口标题栏深色
        if sys.platform == "darwin":
            try:
                self.root.tk.call(
                    "::tk::unsupported::MacWindowStyle", "style",
                    self.root._w, "document", "closeBox collapseBox zoomBox"
                )
            except Exception:
                pass

        # --- 状态变量 ---
        self.device_mgr = DeviceManager()
        self.connected = False
        self.running = True
        self.current_image: Image.Image | None = None
        self.photo_image: ImageTk.PhotoImage | None = None
        self.auto_refresh_on = False
        self.refresh_ms = 1000
        self.auto_id: int | None = None
        self.refresh_lock = threading.Lock()
        self.screenshot_count = 0
        self.last_refresh_time: float | None = None

        # 滚屏截图管理器
        self.scroll_mgr = ScrollCaptureManager()
        self.scroll_mgr.on_progress = self._on_scroll_progress
        self.scroll_mgr.on_done = self._on_scroll_done
        self.scroll_mgr.on_error = self._on_scroll_error
        self.scroll_active = False  # 是否处于滚屏截图模式

        # 动画状态
        self._pulse_phase = 0.0

        # --- 构建 UI ---
        self._build_ui()

        # --- 启动设备检测 ---
        self._poll_device()

        # --- 启动视觉动画 ---
        self._animate_pulse()

        # --- 键盘快捷键 ---
        self.root.bind("<F5>", lambda e: self._do_refresh())
        self.root.bind("<Command-c>" if sys.platform == "darwin" else "<Control-c>",
                        lambda e: self._do_copy())
        self.root.bind("<Command-s>" if sys.platform == "darwin" else "<Control-s>",
                        lambda e: self._do_save())
        self.root.bind("<F6>", lambda e: self._do_scroll_snap())
        self.root.bind("<F7>", lambda e: self._do_scroll_finish())

        # --- 窗口缩放时等比重绘画面 ---
        self._resize_after_id: str | None = None
        self.screen_frame.bind("<Configure>", self._on_screen_resize)

    # ==================== UI 构建 ====================

    def _build_ui(self):
        # ========== 主容器 ==========
        main = ctk.CTkFrame(self.root, fg_color=self.BG_DARK, corner_radius=0)
        main.pack(fill="both", expand=True)

        # ========== 顶部霓虹装饰线 ==========
        neon_line = ctk.CTkFrame(main, fg_color=self.NEON_CYAN, height=2, corner_radius=0)
        neon_line.pack(fill="x")

        # 第二条品红色细线 (双色霓虹效果)
        neon_line2 = ctk.CTkFrame(main, fg_color=self.NEON_MAGENTA, height=1, corner_radius=0)
        neon_line2.pack(fill="x")

        # ========== 顶部标题栏 ==========
        header = ctk.CTkFrame(main, fg_color=self.BG_DEEP, corner_radius=0, height=52)
        header.pack(fill="x")
        header.pack_propagate(False)

        header_inner = ctk.CTkFrame(header, fg_color="transparent")
        header_inner.pack(fill="both", expand=True, padx=16, pady=8)

        # 标题文字
        title_frame = ctk.CTkFrame(header_inner, fg_color="transparent")
        title_frame.pack(side="left")

        ctk.CTkLabel(
            title_frame, text="iOS Screen Mirror",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=self.TEXT_PRIMARY
        ).pack(side="left")

        # 霓虹标签 [CYBER]
        cyber_badge = ctk.CTkLabel(
            title_frame, text=" CYBER ",
            font=ctk.CTkFont(size=9, weight="bold"),
            fg_color=self.GLOW_CYAN,
            text_color=self.NEON_CYAN,
            corner_radius=4, height=18
        )
        cyber_badge.pack(side="left", padx=(10, 0))

        # ---- 紧凑统计条 (嵌入标题栏) ----
        stats_bar = ctk.CTkFrame(header_inner, fg_color="transparent")
        stats_bar.pack(side="left", padx=(20, 0))

        # RES
        ctk.CTkLabel(
            stats_bar, text="分辨率",
            font=ctk.CTkFont(size=9, weight="bold"),
            text_color=self.NEON_CYAN
        ).pack(side="left", padx=(0, 3))

        self.lbl_dim = ctk.CTkLabel(
            stats_bar, text="—",
            font=ctk.CTkFont(size=10),
            text_color=self.TEXT_SECONDARY
        )
        self.lbl_dim.pack(side="left", padx=(0, 10))

        # 分隔竖线
        ctk.CTkFrame(
            stats_bar, fg_color=self.BORDER, width=1, height=14, corner_radius=0
        ).pack(side="left", padx=(0, 10))

        # SHOTS
        ctk.CTkLabel(
            stats_bar, text="截图数",
            font=ctk.CTkFont(size=9, weight="bold"),
            text_color=self.NEON_MAGENTA
        ).pack(side="left", padx=(0, 3))

        self.lbl_count = ctk.CTkLabel(
            stats_bar, text="0",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=self.TEXT_PRIMARY
        )
        self.lbl_count.pack(side="left", padx=(0, 10))

        # 分隔竖线
        ctk.CTkFrame(
            stats_bar, fg_color=self.BORDER, width=1, height=14, corner_radius=0
        ).pack(side="left", padx=(0, 10))

        # REFRESH
        ctk.CTkLabel(
            stats_bar, text="刷新",
            font=ctk.CTkFont(size=9, weight="bold"),
            text_color=self.NEON_CYAN
        ).pack(side="left", padx=(0, 3))

        self.lbl_time = ctk.CTkLabel(
            stats_bar, text="—",
            font=ctk.CTkFont(size=10),
            text_color=self.TEXT_SECONDARY
        )
        self.lbl_time.pack(side="left")

        # 连接状态徽章
        self.badge = ctk.CTkLabel(
            header_inner, text="  未连接  ",
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color=self.ERROR, text_color="white",
            corner_radius=12, height=24
        )
        self.badge.pack(side="right", padx=(0, 4))

        # ========== 标题栏底部分隔线 ==========
        sep = ctk.CTkFrame(main, fg_color=self.BORDER, height=1, corner_radius=0)
        sep.pack(fill="x")

        # ========== 左右分栏主体 ==========
        body = ctk.CTkFrame(main, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=(8, 4))

        # ============================================================
        # 左侧: 屏幕显示区域 (带霓虹辉光边框)
        # ============================================================
        left = ctk.CTkFrame(body, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        # --- 外层辉光: 品红色 (模拟外层霓虹灯光) ---
        glow_outer = ctk.CTkFrame(
            left, fg_color=self.NEON_MAGENTA, corner_radius=14
        )
        glow_outer.pack(fill="both", expand=True)

        # --- 中层辉光: 青色 (模拟内层霓虹灯光) ---
        glow_inner = ctk.CTkFrame(
            glow_outer, fg_color=self.NEON_CYAN, corner_radius=12
        )
        glow_inner.pack(fill="both", expand=True, padx=2, pady=2)

        # --- 屏幕容器: 纯黑背景 ---
        self.screen_outer = ctk.CTkFrame(
            glow_inner, fg_color="#000000", corner_radius=10
        )
        self.screen_outer.pack(fill="both", expand=True, padx=1, pady=1)

        # --- 四角装饰 (赛博朋克角标) ---
        corner_size = 20
        corner_thickness = 2
        # 左上角
        self._corner_tl = ctk.CTkFrame(
            self.screen_outer, fg_color=self.NEON_CYAN, corner_radius=0,
            width=corner_size, height=corner_thickness
        )
        self._corner_tl.place(x=6, y=6)
        self._corner_tl2 = ctk.CTkFrame(
            self.screen_outer, fg_color=self.NEON_CYAN, corner_radius=0,
            width=corner_thickness, height=corner_size
        )
        self._corner_tl2.place(x=6, y=6)

        # 右上角
        self._corner_tr = ctk.CTkFrame(
            self.screen_outer, fg_color=self.NEON_CYAN, corner_radius=0,
            width=corner_size, height=corner_thickness
        )
        self._corner_tr.place(x=0, y=6, relx=1.0, anchor="ne")
        self._corner_tr2 = ctk.CTkFrame(
            self.screen_outer, fg_color=self.NEON_CYAN, corner_radius=0,
            width=corner_thickness, height=corner_size
        )
        self._corner_tr2.place(x=0, y=6, relx=1.0, anchor="ne")
        # 修正: 右上角需要偏移
        self._corner_tr.place_configure(relx=1.0, x=-6)
        self._corner_tr2.place_configure(relx=1.0, x=-6)

        # 左下角
        self._corner_bl = ctk.CTkFrame(
            self.screen_outer, fg_color=self.NEON_MAGENTA, corner_radius=0,
            width=corner_size, height=corner_thickness
        )
        self._corner_bl.place(x=6, y=0, rely=1.0, anchor="sw")
        self._corner_bl2 = ctk.CTkFrame(
            self.screen_outer, fg_color=self.NEON_MAGENTA, corner_radius=0,
            width=corner_thickness, height=corner_size
        )
        self._corner_bl2.place(x=6, y=0, rely=1.0, anchor="sw")
        self._corner_bl.place_configure(rely=1.0, y=-6)
        self._corner_bl2.place_configure(rely=1.0, y=-6)

        # 右下角
        self._corner_br = ctk.CTkFrame(
            self.screen_outer, fg_color=self.NEON_MAGENTA, corner_radius=0,
            width=corner_size, height=corner_thickness
        )
        self._corner_br.place(x=0, y=0, relx=1.0, rely=1.0, anchor="se")
        self._corner_br2 = ctk.CTkFrame(
            self.screen_outer, fg_color=self.NEON_MAGENTA, corner_radius=0,
            width=corner_thickness, height=corner_size
        )
        self._corner_br2.place(x=0, y=0, relx=1.0, rely=1.0, anchor="se")
        self._corner_br.place_configure(relx=1.0, rely=1.0, x=-6, y=-6)
        self._corner_br2.place_configure(relx=1.0, rely=1.0, x=-6, y=-6)

        # --- 屏幕内容区域 ---
        self.screen_frame = ctk.CTkFrame(
            self.screen_outer, fg_color="#000000", corner_radius=8
        )
        self.screen_frame.pack(fill="both", expand=True, padx=8, pady=8)

        # 占位符 — 赛博朋克风格
        placeholder_frame = ctk.CTkFrame(
            self.screen_frame, fg_color="transparent"
        )
        placeholder_frame.pack(expand=True)

        self.lbl_placeholder = ctk.CTkLabel(
            placeholder_frame,
            text="等待设备连接",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=self.NEON_CYAN
        )
        self.lbl_placeholder.pack(pady=(4, 0))

        # 屏幕显示标签
        self.lbl_screen = ctk.CTkLabel(
            self.screen_frame, text="", anchor="nw"
        )

        # ============================================================
        # 右侧: 控制面板
        # ============================================================
        right = ctk.CTkFrame(body, fg_color="transparent", width=240)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        # ---- 设备信息 (嵌入标题栏) ----
        self.lbl_device = ctk.CTkLabel(
            header_inner, text="",
            font=ctk.CTkFont(size=10),
            text_color=self.TEXT_DIM
        )
        self.lbl_device.pack(side="right", padx=(0, 8))

        # ---- 主要操作按钮 ----
        btns = ctk.CTkFrame(right, fg_color="transparent")
        btns.pack(fill="x", pady=(4, 0))

        # 刷新屏幕 — 大号霓虹青色按钮
        self.btn_refresh = ctk.CTkButton(
            btns, text="刷新屏幕",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=self.NEON_CYAN, hover_color="#00b8d4",
            text_color="#000000",
            height=40, corner_radius=10,
            border_width=0,
            command=self._do_refresh, state="disabled"
        )
        self.btn_refresh.pack(fill="x", pady=(0, 6))

        # 复制截图 + 保存截图 — 双按钮行
        row2 = ctk.CTkFrame(btns, fg_color="transparent")
        row2.pack(fill="x", pady=(0, 6))

        self.btn_copy = ctk.CTkButton(
            row2, text="复制截图",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="transparent",
            hover_color=self.GLOW_SUCCESS,
            text_color=self.SUCCESS,
            border_width=2,
            border_color=self.SUCCESS,
            height=34, corner_radius=10,
            command=self._do_copy, state="disabled"
        )
        self.btn_copy.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.btn_save = ctk.CTkButton(
            row2, text="保存截图",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="transparent",
            hover_color=self.GLOW_PURPLE,
            text_color=self.NEON_PURPLE,
            border_width=2,
            border_color=self.NEON_PURPLE,
            height=34, corner_radius=10,
            command=self._do_save, state="disabled"
        )
        self.btn_save.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # 挂载 DDI + 启动 tunneld — 辅助按钮行
        row3 = ctk.CTkFrame(btns, fg_color="transparent")
        row3.pack(fill="x", pady=(0, 8))

        self.btn_mount = ctk.CTkButton(
            row3, text="挂载 DDI",
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color="transparent",
            hover_color="#382800",
            text_color=self.WARNING,
            border_width=1,
            border_color="#554400",
            height=28, corner_radius=8,
            command=self._do_mount_ddi, state="disabled"
        )
        self.btn_mount.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.btn_tunnel = ctk.CTkButton(
            row3, text="启动隧道",
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color="transparent",
            hover_color=self.GLOW_CYAN,
            text_color=self.NEON_CYAN,
            border_width=1,
            border_color=self.BORDER_NEON,
            height=28, corner_radius=8,
            command=self._do_start_tunneld, state="disabled"
        )
        self.btn_tunnel.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # ---- 自动刷新开关 (霓虹卡片) ----
        auto_glow = ctk.CTkFrame(
            right, fg_color=self.BORDER_NEON, corner_radius=12, height=44
        )
        auto_glow.pack(fill="x", pady=(0, 8))
        auto_glow.pack_propagate(False)

        auto_card = ctk.CTkFrame(
            auto_glow, fg_color=self.BG_CARD, corner_radius=11, height=42
        )
        auto_card.pack(fill="both", padx=1, pady=1)
        auto_card.pack_propagate(False)

        self.var_auto = ctk.BooleanVar(value=False)
        self.chk_auto = ctk.CTkCheckBox(
            auto_card,
            text="自动刷新",
            font=ctk.CTkFont(size=11, weight="bold"),
            variable=self.var_auto,
            command=self._toggle_auto,
            fg_color=self.NEON_CYAN,
            hover_color=self.GLOW_CYAN,
            border_color=self.NEON_CYAN,
            text_color=self.TEXT_SECONDARY,
            checkmark_color="#000000"
        )
        self.chk_auto.pack(side="left", padx=12, pady=8)

        self.lbl_fps = ctk.CTkLabel(
            auto_card, text="1.0s",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=self.TEXT_DIM
        )
        self.lbl_fps.pack(side="right", padx=12)

        # ---- 滚屏截图 (霓虹卡片) ----
        scroll_glow = ctk.CTkFrame(
            right, fg_color=self.NEON_MAGENTA, corner_radius=12
        )
        scroll_glow.pack(fill="x", pady=(0, 8))

        scroll_card = ctk.CTkFrame(
            scroll_glow, fg_color=self.BG_CARD, corner_radius=10
        )
        scroll_card.pack(fill="x", padx=1, pady=1)

        scroll_inner = ctk.CTkFrame(scroll_card, fg_color="transparent")
        scroll_inner.pack(fill="x", padx=10, pady=8)

        # 标题行
        scroll_header = ctk.CTkFrame(scroll_inner, fg_color="transparent")
        scroll_header.pack(fill="x", pady=(0, 6))

        ctk.CTkLabel(
            scroll_header, text=">",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=self.NEON_MAGENTA
        ).pack(side="left", padx=(0, 3))

        ctk.CTkLabel(
            scroll_header, text="滚动截图",
            font=ctk.CTkFont(size=9, weight="bold"),
            text_color=self.NEON_MAGENTA
        ).pack(side="left")

        self.lbl_scroll_progress = ctk.CTkLabel(
            scroll_header, text="",
            font=ctk.CTkFont(size=9, weight="bold"),
            text_color=self.NEON_CYAN
        )
        self.lbl_scroll_progress.pack(side="right")

        # 模式切换按钮
        mode_row = ctk.CTkFrame(scroll_inner, fg_color="transparent")
        mode_row.pack(fill="x", pady=(0, 6))

        self.btn_mode_manual = ctk.CTkButton(
            mode_row, text="手动",
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color=self.GLOW_MAGENTA,
            hover_color="#38002a",
            text_color=self.NEON_MAGENTA,
            border_width=1,
            border_color=self.NEON_MAGENTA,
            height=26, corner_radius=6,
            command=lambda: self._set_scroll_mode("manual")
        )
        self.btn_mode_manual.pack(side="left", fill="x", expand=True, padx=(0, 3))

        self.btn_mode_auto = ctk.CTkButton(
            mode_row, text="自动",
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color="transparent",
            hover_color=self.GLOW_MAGENTA,
            text_color=self.TEXT_DIM,
            border_width=1,
            border_color=self.BORDER,
            height=26, corner_radius=6,
            command=lambda: self._set_scroll_mode("auto")
        )
        self.btn_mode_auto.pack(side="left", fill="x", expand=True, padx=(3, 0))

        # 操作按钮
        action_row = ctk.CTkFrame(scroll_inner, fg_color="transparent")
        action_row.pack(fill="x")

        self.btn_scroll_snap = ctk.CTkButton(
            action_row, text="截取",
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color="transparent",
            hover_color=self.GLOW_MAGENTA,
            text_color=self.NEON_MAGENTA,
            border_width=1,
            border_color=self.NEON_MAGENTA,
            height=28, corner_radius=8,
            command=self._do_scroll_snap, state="disabled"
        )
        self.btn_scroll_snap.pack(side="left", fill="x", expand=True, padx=(0, 3))

        self.btn_scroll_done = ctk.CTkButton(
            action_row, text="拼接",
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color="transparent",
            hover_color=self.GLOW_SUCCESS,
            text_color=self.SUCCESS,
            border_width=1,
            border_color=self.SUCCESS,
            height=28, corner_radius=8,
            command=self._do_scroll_finish, state="disabled"
        )
        self.btn_scroll_done.pack(side="left", fill="x", expand=True, padx=(3, 0))


        keys_frame = ctk.CTkFrame(right, fg_color="transparent")
        keys_frame.pack(fill="x", pady=(2, 0))

        shortcuts = [
            ("F5", "刷新"),
            ("F6", "截取"),
            ("F7", "拼接"),
            ("Cmd+C", "复制"),
            ("Cmd+S", "保存"),
        ]
        for key, action in shortcuts:
            row = ctk.CTkFrame(keys_frame, fg_color="transparent")
            row.pack(fill="x", pady=1)

            key_label = ctk.CTkLabel(
                row, text=f" {key} ",
                font=ctk.CTkFont(size=9, weight="bold"),
                fg_color=self.BG_SURFACE,
                text_color=self.NEON_CYAN,
                corner_radius=4, height=18
            )
            key_label.pack(side="left", padx=(0, 6))

            ctk.CTkLabel(
                row, text=action,
                font=ctk.CTkFont(size=10),
                text_color=self.TEXT_DIM
            ).pack(side="left")

        # ========== 底部状态栏 ==========
        # 霓虹分隔线
        bottom_neon = ctk.CTkFrame(main, fg_color=self.NEON_PURPLE, height=1, corner_radius=0)
        bottom_neon.pack(fill="x")

        status_bar = ctk.CTkFrame(main, fg_color=self.BG_DEEP, corner_radius=0, height=28)
        status_bar.pack(fill="x")
        status_bar.pack_propagate(False)

        # 状态指示灯
        self.status_dot = ctk.CTkFrame(
            status_bar, fg_color=self.NEON_CYAN, corner_radius=4,
            width=8, height=8
        )
        self.status_dot.pack(side="left", padx=(12, 6), pady=10)

        self.lbl_status = ctk.CTkLabel(
            status_bar, text="就绪",
            font=ctk.CTkFont(size=11),
            text_color=self.TEXT_DIM, anchor="w"
        )
        self.lbl_status.pack(side="left", pady=6)

        # 右侧时间戳
        self.lbl_clock = ctk.CTkLabel(
            status_bar, text="",
            font=ctk.CTkFont(size=10),
            text_color=self.TEXT_DIM
        )
        self.lbl_clock.pack(side="right", padx=12, pady=6)

        self._update_clock()

    # ==================== 视觉动画 ====================

    def _animate_pulse(self):
        """脉冲动画: 状态徽章在空闲/连接时缓慢呼吸发光。"""
        if not self.running:
            return

        self._pulse_phase += 0.06
        if self._pulse_phase > 2 * math.pi:
            self._pulse_phase -= 2 * math.pi

        # 计算呼吸亮度 (0.3 ~ 1.0)
        brightness = 0.3 + 0.7 * (0.5 + 0.5 * math.sin(self._pulse_phase))

        if not self.connected:
            # 未连接: 红色呼吸
            base = self.ERROR
            dim = "#330010"
            color = _lerp_color(dim, base, brightness)
            try:
                self.badge.configure(fg_color=color)
            except Exception:
                pass
        else:
            # 已连接: 绿色呼吸
            base = self.SUCCESS
            dim = "#003310"
            color = _lerp_color(dim, base, brightness)
            try:
                self.badge.configure(fg_color=color)
            except Exception:
                pass

        # 状态指示灯同步呼吸
        if hasattr(self, 'status_dot'):
            dot_base = self._status_dot_color if hasattr(self, '_status_dot_color') else self.NEON_CYAN
            dot_dim = self.GLOW_CYAN
            dot_color = _lerp_color(dot_dim, dot_base, brightness)
            try:
                self.status_dot.configure(fg_color=dot_color)
            except Exception:
                pass

        self.root.after(50, self._animate_pulse)

    def _update_clock(self):
        """更新底部状态栏的实时时钟。"""
        if not self.running:
            return
        try:
            now = datetime.now().strftime("%H:%M:%S")
            self.lbl_clock.configure(text=now)
        except Exception:
            pass
        self.root.after(1000, self._update_clock)

    # ==================== 设备检测 ====================

    def _poll_device(self):
        if not self.running:
            return
        try:
            if not self.connected:
                devices = DeviceManager.list_devices()
                if devices:
                    self._on_connect(devices[0])
            else:
                devices = DeviceManager.list_devices()
                if self.device_mgr.udid not in devices:
                    self._on_disconnect()
        except Exception as exc:
            self._status(f"检测异常: {exc}", self.WARNING)
        finally:
            if self.running:
                self.root.after(3000, self._poll_device)

    def _on_connect(self, udid: str):
        self.device_mgr.connect(udid)
        info = DeviceManager.get_device_info(udid)
        name = info.get("DeviceName", "未知设备")
        model = info.get("ProductType", "")
        ios_ver = info.get("ProductVersion", "")

        # 记录 iOS 版本
        self.device_mgr.ios_version = ios_ver
        try:
            self.device_mgr.ios_major = int(ios_ver.split(".")[0]) if ios_ver else 0
        except (ValueError, IndexError):
            self.device_mgr.ios_major = 0

        self.lbl_device.configure(text=f"{name}  {model}  iOS {ios_ver}", text_color=self.NEON_PURPLE)

        self.badge.configure(text="  已连接  ", fg_color=self.SUCCESS)

        for btn in (self.btn_refresh, self.btn_copy, self.btn_save,
                    self.btn_mount, self.btn_tunnel):
            btn.configure(state="normal")

        self.connected = True
        self._status(f"已连接: {name}", self.SUCCESS)

        # 更新占位符文字
        self.lbl_placeholder.configure(text="连接成功", text_color=self.SUCCESS)

        # iOS 17+: 自动挂载 DDI + 启动 tunneld
        if self.device_mgr.ios_major >= 17 and self.device_mgr._has_pymobiledevice3:
            threading.Thread(target=self._ios17_setup_thread, daemon=True).start()
        else:
            self.root.after(300, self._do_refresh)

    def _on_disconnect(self):
        self.device_mgr.disconnect()
        self.connected = False

        if self.auto_refresh_on:
            self.var_auto.set(False)
            self.auto_refresh_on = False
            if self.auto_id is not None:
                self.root.after_cancel(self.auto_id)
                self.auto_id = None

        self.lbl_device.configure(text="", text_color=self.TEXT_DIM)
        self.badge.configure(text="  未连接  ", fg_color=self.ERROR)

        for btn in (self.btn_refresh, self.btn_copy, self.btn_save,
                    self.btn_mount, self.btn_tunnel):
            btn.configure(state="disabled")

        # 重置挂载按钮状态
        self.btn_mount.configure(text="挂载 DDI", fg_color="transparent",
                                 border_color="#554400", text_color=self.WARNING)
        self.btn_tunnel.configure(text="启动隧道", fg_color="transparent",
                                  border_color=self.BORDER_NEON, text_color=self.NEON_CYAN)

        self.current_image = None
        self.photo_image = None
        self.lbl_screen.configure(image="")
        self.lbl_placeholder.configure(text="等待设备连接", text_color=self.NEON_CYAN)

        self._status("设备已断开", self.WARNING)

    # ==================== iOS 17+ 初始化 ====================

    def _ios17_setup_thread(self):
        """后台线程: 为 iOS 17+ 设备执行 DDI 挂载 + tunneld 启动 + 首次截图。"""
        try:
            # Step 1: 挂载 DDI
            self.root.after(0, lambda: self._status("正在挂载 DDI...", self.WARNING))
            ok_ddi, msg_ddi = self.device_mgr.auto_mount_ddi()
            if ok_ddi:
                self.root.after(0, lambda: self.btn_mount.configure(
                    text="DDI 已挂载", fg_color="transparent",
                    border_color=self.SUCCESS, text_color=self.SUCCESS))
            else:
                self.root.after(0, lambda: self._status(f"DDI: {msg_ddi}", self.WARNING))

            # Step 2: 启动 tunneld (如果需要)
            self.root.after(0, lambda: self._status("正在检查 tunneld...", self.WARNING))
            ok_tunnel, msg_tunnel = self.device_mgr.ensure_tunneld()
            if ok_tunnel:
                self.root.after(0, lambda: self.btn_tunnel.configure(
                    text="隧道已启动", fg_color="transparent",
                    border_color=self.SUCCESS, text_color=self.SUCCESS))
            else:
                self.root.after(0, lambda: self._status(f"tunneld: {msg_tunnel}", self.ERROR))
                return

            # Step 3: 尝试截图
            self.root.after(0, lambda: self._status("准备就绪，正在截图...", self.SUCCESS))
            self.root.after(300, self._do_refresh)

        except Exception as exc:
            self.root.after(0, lambda: self._status(f"初始化失败: {exc}", self.ERROR))

    # ==================== 核心操作 ====================

    def _do_mount_ddi(self):
        """手动触发挂载 DeveloperDiskImage。"""
        if not self.connected:
            return
        self.btn_mount.configure(state="disabled", text="挂载中...")
        self._status("正在挂载 DDI...", self.WARNING)

        def _thread():
            try:
                ok, msg = self.device_mgr.auto_mount_ddi()
                if ok:
                    self.root.after(0, lambda: self.btn_mount.configure(
                        state="normal", text="DDI 已挂载", fg_color="transparent",
                        border_color=self.SUCCESS, text_color=self.SUCCESS))
                    self.root.after(0, lambda: self._status(msg, self.SUCCESS))
                else:
                    self.root.after(0, lambda: self.btn_mount.configure(
                        state="normal", text="挂载 DDI", fg_color="transparent",
                        border_color="#554400", text_color=self.WARNING))
                    self.root.after(0, lambda: self._status(msg, self.ERROR))
            except Exception as exc:
                self.root.after(0, lambda: self.btn_mount.configure(
                    state="normal", text="挂载 DDI", fg_color="transparent",
                    border_color="#554400", text_color=self.WARNING))
                self.root.after(0, lambda: self._status(f"挂载失败: {exc}", self.ERROR))

        threading.Thread(target=_thread, daemon=True).start()

    def _do_start_tunneld(self):
        """手动触发启动 tunneld。"""
        if not self.connected:
            return
        self.btn_tunnel.configure(state="disabled", text="启动中...")
        self._status("正在启动 tunneld (可能需要输入密码)...", self.WARNING)

        def _thread():
            try:
                ok, msg = self.device_mgr.ensure_tunneld()
                if ok:
                    self.root.after(0, lambda: self.btn_tunnel.configure(
                        state="normal", text="隧道已启动", fg_color="transparent",
                        border_color=self.SUCCESS, text_color=self.SUCCESS))
                    self.root.after(0, lambda: self._status(msg, self.SUCCESS))
                else:
                    self.root.after(0, lambda: self.btn_tunnel.configure(
                        state="normal", text="启动隧道", fg_color="transparent",
                        border_color=self.BORDER_NEON, text_color=self.NEON_CYAN))
                    self.root.after(0, lambda: self._status(msg, self.ERROR))
            except Exception as exc:
                self.root.after(0, lambda: self.btn_tunnel.configure(
                    state="normal", text="启动隧道", fg_color="transparent",
                    border_color=self.BORDER_NEON, text_color=self.NEON_CYAN))
                self.root.after(0, lambda: self._status(f"tunneld 启动失败: {exc}", self.ERROR))

        threading.Thread(target=_thread, daemon=True).start()

    def _do_refresh(self):
        if not self.connected:
            return
        self.btn_refresh.configure(state="disabled", text="截取中...")
        threading.Thread(target=self._refresh_thread, daemon=True).start()

    def _refresh_thread(self):
        if not self.refresh_lock.acquire(blocking=False):
            self.root.after(0, lambda: self.btn_refresh.configure(
                state="normal", text="刷新屏幕"))
            return
        try:
            t0 = time.time()
            img = self.device_mgr.take_screenshot()
            elapsed = time.time() - t0
            self.root.after(0, self._display_image, img, elapsed)
        except Exception as exc:
            self.root.after(0, self._refresh_error, str(exc))
        finally:
            self.refresh_lock.release()

    def _display_image(self, img: Image.Image, elapsed: float):
        self.current_image = img
        self.last_refresh_time = time.time()
        self.screenshot_count += 1

        w, h = img.size
        self.lbl_dim.configure(text=f"{w} x {h}")
        self.lbl_count.configure(text=f"{self.screenshot_count}")
        self.lbl_time.configure(
            text=f"{datetime.now().strftime('%H:%M:%S')} {elapsed:.1f}s")

        # 自适应缩放
        try:
            fw = self.screen_frame.winfo_width() or 700
            fh = self.screen_frame.winfo_height() or 500
        except Exception:
            fw, fh = 440, 600

        ratio = min(fw / w, fh / h, 1.0)
        if ratio < 1.0:
            display_img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        else:
            display_img = img

        self.photo_image = ImageTk.PhotoImage(display_img)
        self.lbl_placeholder.pack_forget()
        self.lbl_screen.configure(image=self.photo_image)
        self.lbl_screen.pack(expand=True)

        self.btn_refresh.configure(state="normal", text="刷新屏幕")
        self._status(f"截图成功 ({elapsed:.1f}s)", self.SUCCESS)

        # 如果开启了自动刷新，安排下一次
        if self.auto_refresh_on and self.connected and self.running:
            self.auto_id = self.root.after(self.refresh_ms, self._auto_tick)

    def _on_screen_resize(self, event):
        """窗口/面板缩放时，防抖后重新等比缩放画面。"""
        if self.current_image is None:
            return
        if self._resize_after_id is not None:
            self.root.after_cancel(self._resize_after_id)
        self._resize_after_id = self.root.after(150, self._rescale_display)

    def _rescale_display(self):
        """根据当前面板尺寸，重新等比缩放并显示 current_image。"""
        if self.current_image is None:
            return
        img = self.current_image
        w, h = img.size

        try:
            fw = self.screen_frame.winfo_width()
            fh = self.screen_frame.winfo_height()
        except Exception:
            return

        if fw < 10 or fh < 10:
            return

        ratio = min(fw / w, fh / h, 1.0)
        if ratio < 1.0:
            display_img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        else:
            display_img = img

        self.photo_image = ImageTk.PhotoImage(display_img)
        self.lbl_screen.configure(image=self.photo_image)

    def _refresh_error(self, err: str):
        self.btn_refresh.configure(state="normal", text="刷新屏幕")
        msg = err.split("\n")[0][:150]

        if "developer" in msg.lower() or "disk" in msg.lower() or "ddi" in msg.lower():
            self._status("需要挂载 DDI，请点击「挂载 DDI」", self.WARNING)
        elif "tunnel" in msg.lower():
            self._status("需要启动 tunneld，请点击「启动隧道」", self.WARNING)
        elif "no device" in msg.lower() or "could not" in msg.lower():
            self._status("设备连接异常，请检查 USB", self.ERROR)
        elif "locked" in msg.lower() or "锁定" in msg.lower():
            self._status("设备已锁定，请解锁 iPhone 后重试", self.WARNING)
        else:
            self._status(f"截图失败: {msg}", self.ERROR)

        # 自动刷新模式下继续尝试
        if self.auto_refresh_on and self.connected and self.running:
            self.auto_id = self.root.after(self.refresh_ms * 2, self._auto_tick)

    def _do_copy(self):
        """将当前截图以 PNG 格式复制到系统剪贴板。"""
        if self.current_image is None:
            self._status("尚无截图，请先刷新屏幕", self.WARNING)
            return

        self.btn_copy.configure(text="复制中...", border_color=self.TEXT_DIM)
        self.root.update_idletasks()

        try:
            ok = _copy_png_to_clipboard_macos(self.current_image)
            if ok:
                self.btn_copy.configure(
                    text="已复制!", fg_color=self.SUCCESS,
                    text_color="#000000", border_color=self.SUCCESS)
                self._status("截图已复制到剪贴板", self.SUCCESS)
                self.root.after(1500, lambda: self.btn_copy.configure(
                    text="复制截图", fg_color="transparent",
                    text_color=self.SUCCESS, border_color=self.SUCCESS))
            else:
                # 回退: 保存为临时文件并提示
                tmp = tempfile.mktemp(suffix=".png")
                self.current_image.save(tmp, "PNG")
                self._status(
                    f"剪贴板写入受限，已保存到: {tmp}",
                    self.WARNING)
        except Exception as exc:
            self._status(f"COPY FAIL: {exc}", self.ERROR)
        finally:
            self.btn_copy.configure(text="复制截图")

    def _do_save(self):
        """保存截图到文件。"""
        if self.current_image is None:
            self._status("尚无截图，请先刷新屏幕", self.WARNING)
            return

        desktop = Path.home() / "Desktop"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"ios_screenshot_{ts}.png"

        from tkinter import filedialog
        filepath = filedialog.asksaveasfilename(
            title="保存截图",
            initialdir=str(desktop),
            initialfile=default_name,
            defaultextension=".png",
            filetypes=[("PNG 图片", "*.png"), ("JPEG 图片", "*.jpg"), ("所有文件", "*.*")]
        )

        if filepath:
            self.current_image.save(filepath)
            self._status(f"已保存: {os.path.basename(filepath)}", self.SUCCESS)

    # ==================== 自动刷新 ====================

    def _toggle_auto(self):
        self.auto_refresh_on = self.var_auto.get()
        if self.auto_refresh_on:
            self.chk_auto.configure(text="自动刷新 ON")
            self.lbl_fps.configure(text_color=self.NEON_CYAN)
            self._do_refresh()
        else:
            self.chk_auto.configure(text="自动刷新")
            self.lbl_fps.configure(text_color=self.TEXT_DIM)
            if self.auto_id is not None:
                self.root.after_cancel(self.auto_id)
                self.auto_id = None

    def _auto_tick(self):
        """由 after() 调度，触发一次新的截图刷新。"""
        if not self.running or not self.auto_refresh_on or not self.connected:
            return
        self._do_refresh()

    # ==================== 滚屏截图 ====================

    def _set_scroll_mode(self, mode: str):
        """切换滚屏截图模式 (manual / auto)。"""
        if mode == "manual":
            self.btn_mode_manual.configure(
                fg_color=self.GLOW_MAGENTA, text_color=self.NEON_MAGENTA,
                border_color=self.NEON_MAGENTA)
            self.btn_mode_auto.configure(
                fg_color="transparent", text_color=self.TEXT_DIM,
                border_color=self.BORDER)
        else:
            self.btn_mode_auto.configure(
                fg_color=self.GLOW_MAGENTA, text_color=self.NEON_MAGENTA,
                border_color=self.NEON_MAGENTA)
            self.btn_mode_manual.configure(
                fg_color="transparent", text_color=self.TEXT_DIM,
                border_color=self.BORDER)

        self.scroll_mgr.mode = mode
        self.scroll_mgr.reset()
        self.lbl_scroll_progress.configure(text="")
        self.btn_scroll_snap.configure(state="normal" if self.connected else "disabled")
        self.btn_scroll_done.configure(state="disabled")
        mode_cn = "手动模式" if mode == "manual" else "自动模式"
        self._status(f"滚动截图: {mode_cn}", self.NEON_MAGENTA)

    def _do_scroll_snap(self):
        """滚屏截图: 截取当前屏幕并加入队列。"""
        if not self.connected:
            return

        if self.scroll_mgr.mode == "auto":
            # 自动模式: 启动自动滚屏循环
            if not self.scroll_mgr._running:
                self.btn_scroll_snap.configure(state="disabled", text="停止")
                self.scroll_mgr.start_auto(self.device_mgr, total_screens=5)
                self._status("自动滚动截取中...", self.NEON_MAGENTA)
            else:
                self.scroll_mgr.stop_auto()
                self.btn_scroll_snap.configure(text="截取")
                self._status("自动滚动已停止", self.WARNING)
        else:
            # 手动模式: 截取当前屏幕
            if self.current_image is None:
                self._status("尚无截图，请先刷新屏幕", self.WARNING)
                return

            if self.scroll_mgr.count == 0:
                self.scroll_mgr.start_manual()

            self.scroll_mgr.manual_capture(self.current_image)
            self.btn_scroll_done.configure(state="normal")
            self._status(f"滚动截取第 {self.scroll_mgr.count} 帧", self.NEON_MAGENTA)

            # 提示用户滚动到下一个位置
            if self.scroll_mgr.count < 10:
                self.root.after(300, lambda: self._status(
                    f"请滚动后再次截取 (已截取 {self.scroll_mgr.count} 帧)",
                    self.NEON_MAGENTA))

    def _do_scroll_finish(self):
        """完成滚屏截图: 拼接所有帧并保存。"""
        if self.scroll_mgr.count == 0:
            self._status("尚无截帧，请先截取", self.WARNING)
            return

        self._status(f"正在拼接 {self.scroll_mgr.count} 帧...", self.NEON_CYAN)
        self.btn_scroll_snap.configure(state="disabled")
        self.btn_scroll_done.configure(state="disabled", text="拼接中...")
        self.root.update_idletasks()

        def _stitch_thread():
            try:
                result = self.scroll_mgr.finish()
                if result:
                    self.root.after(0, self._scroll_save_result, result)
                else:
                    self.root.after(0, lambda: self._status("拼接失败", self.ERROR))
            except Exception as e:
                self.root.after(0, lambda: self._status(f"STITCH ERROR: {e}", self.ERROR))
            finally:
                self.root.after(0, self._scroll_reset_ui)

        threading.Thread(target=_stitch_thread, daemon=True).start()

    def _scroll_save_result(self, result: Image.Image):
        """保存拼接后的长图。"""
        # 更新当前显示图片为长图预览
        self.current_image = result
        self._display_image(result, 0)

        # 弹出保存对话框
        desktop = Path.home() / "Desktop"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"ios_scroll_{ts}.png"

        from tkinter import filedialog
        filepath = filedialog.asksaveasfilename(
            title="保存滚屏长图",
            initialdir=str(desktop),
            initialfile=default_name,
            defaultextension=".png",
            filetypes=[("PNG 图片", "*.png"), ("JPEG 图片", "*.jpg"), ("所有文件", "*.*")]
        )

        if filepath:
            result.save(filepath)
            self._status(f"长图已保存: {os.path.basename(filepath)}", self.SUCCESS)
        else:
            self._status(f"长图就绪 ({result.size[0]}x{result.size[1]})", self.SUCCESS)

    def _scroll_reset_ui(self):
        """重置滚屏截图 UI 状态。"""
        self.btn_scroll_snap.configure(state="normal" if self.connected else "disabled", text="截取")
        self.btn_scroll_done.configure(state="disabled", text="拼接")
        self.scroll_mgr.reset()
        self.lbl_scroll_progress.configure(text="")

    def _on_scroll_progress(self, captured: int, total: int):
        """滚屏截图进度回调 (可能从非主线程调用)。"""
        def _update():
            if total > 0:
                self.lbl_scroll_progress.configure(text=f"{captured}/{total}")
            else:
                self.lbl_scroll_progress.configure(text=f"{captured}")
            # 自动模式下更新按钮
            if self.scroll_mgr.mode == "auto" and self.scroll_mgr._running:
                self.btn_scroll_snap.configure(text="停止")
        self.root.after(0, _update)

    def _on_scroll_done(self, result: Image.Image):
        """自动滚屏完成回调 (从后台线程调用)。"""
        self.root.after(0, self._scroll_save_result, result)

    def _on_scroll_error(self, msg: str):
        """滚屏截图错误回调 (从后台线程调用)。"""
        self.root.after(0, lambda: self._status(f"滚动截图错误: {msg[:80]}", self.ERROR))
        self.root.after(0, self._scroll_reset_ui)

    # ==================== 工具方法 ====================

    def _status(self, msg: str, color: str = ""):
        """更新状态栏消息和指示灯颜色。"""
        self.lbl_status.configure(
            text=f"  {msg}",
            text_color=color or self.TEXT_DIM
        )
        # 同步状态指示灯颜色
        if hasattr(self, 'status_dot'):
            dot_color = color or self.NEON_CYAN
            self._status_dot_color = dot_color
            try:
                self.status_dot.configure(fg_color=dot_color)
            except Exception:
                pass

    # ==================== 生命周期 ====================

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        self.running = False
        self.auto_refresh_on = False
        self.device_mgr.tunneld.stop()
        self.device_mgr.disconnect()
        self.root.destroy()


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    if sys.platform == "darwin":
        import multiprocessing
        try:
            multiprocessing.set_start_method("spawn")
        except RuntimeError:
            pass

    app = IOSScreenMirrorApp()
    app.run()
