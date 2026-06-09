"""
推送通知 —— Server酱 微信推送 + 跨平台系统通知（Windows/macOS）
"""

from __future__ import annotations
import os
import platform
import subprocess
from datetime import datetime
from typing import Callable

from app.models import Alert, AnalysisStats
from app.config import Config
from app.utils import log
from app.http_client import serverchan_client


def _send_macos_notification(
    title: str,
    message: str,
) -> bool:
    """发送 macOS 系统通知（右上角气泡）"""
    if platform.system() != "Darwin":
        return False
    try:
        script = f'display notification "{message}" with title "{title}"'
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
        return True
    except Exception as e:
        log.warning(f"macOS 通知发送失败: {e}")
        return False


def _send_windows_notification(
    title: str,
    message: str,
) -> bool:
    """发送 Windows 系统通知（右下角气泡）"""
    if platform.system() != "Windows":
        return False
    try:
        # 方法1: 尝试用 plyer（跨平台库，推荐安装）
        try:
            from plyer import notification
            notification.notify(
                title=title,
                message=message,
                app_name="MarketWatcher",
                timeout=10
            )
            return True
        except ImportError:
            pass
        
        # 方法2: Windows 10+ 原生（无需依赖）
        import ctypes
        from ctypes import wintypes
        
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        
        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeout", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
            ]
        
        NIF_INFO = 0x00000010
        NIIF_INFO = 0x00000001
        
        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = None
        nid.uID = 0
        nid.uFlags = NIF_INFO
        nid.szInfo = message[:255]
        nid.szInfoTitle = title[:63]
        nid.dwInfoFlags = NIIF_INFO
        nid.uTimeout = 10000
        
        shell32.Shell_NotifyIconW(0, ctypes.byref(nid))
        
        return True
    except Exception as e:
        log.warning(f"Windows 通知发送失败: {e}")
        return False


def send_desktop_notification(
    title: str,
    message: str,
) -> bool:
    """发送跨平台桌面通知（自动适配 Windows/macOS）"""
    sys = platform.system()
    if sys == "Darwin":
        return _send_macos_notification(title, message)
    elif sys == "Windows":
        return _send_windows_notification(title, message)
    else:
        log.debug(f"当前系统 {sys} 暂不支持桌面通知")
        return False


def push_alert(
    alerts: list[Alert],
    stats: AnalysisStats,
    config: Config,
    llm_result: str | None = None,
) -> bool:
    """推送异动提醒到微信（Server酱）"""
    if not config.push_enabled:
        return False
    if config.push_trigger == "仅异动时" and stats.alert_count == 0:
        return False

    sendkey = os.environ.get("SCT_SENDKEY")
    if not sendkey:
        return False

    # 构建标题
    s = stats.sentiment
    now_str = datetime.now().strftime("%m-%d %H:%M")
    title = f"📈 盯盘提醒 | {s.label} {stats.up}涨{stats.down}跌"

    # 构建内容（Markdown）
    lines = [
        f"## 📊 市场概览",
        f"- **情绪**: {s.score}/100 {s.label}",
        f"- **涨跌**: {stats.up}涨 / {stats.down}跌 / {stats.flat}平",
        f"- **异常**: {stats.alert_count} 条",
        "",
    ]

    if alerts:
        lines.append("## 🔔 异动详情")
        for a in alerts[:5]:
            lines.append(f"- **{a.name}**: {' | '.join(a.messages)}")
        if len(alerts) > 5:
            lines.append(f"- ...还有 {len(alerts) - 5} 条")
        lines.append("")

    if llm_result and config.push_include_llm:
        lines.append("## 🤖 AI研判")
        lines.append(llm_result.strip())
        lines.append("")

    lines.append("---")
    lines.append(f"*{now_str} · 15分钟后自动更新*")

    content = "\n".join(lines)

    url = f"/{sendkey}.send"
    resp = serverchan_client.post(url, data={"title": title, "desp": content})

    if resp is None:
        log.warning("推送失败")
        return False

    try:
        return resp.status_code == 200 and resp.json().get("code") == 0
    except Exception as e:
        log.warning(f"推送解析失败: {e}")
        return False
