# 定时截图组升级版：支持普通脚本、脚本组、截图流程组
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox, filedialog
import sys
import json
import time
import threading
import ctypes
import numpy as np
import cv2
from pynput import mouse, keyboard
from pynput.mouse import Controller as MouseController
from pynput.keyboard import Controller as KeyboardController, Key, KeyCode
from threading import Lock, Event
from PIL import Image, ImageGrab, ImageTk  # 需要安装Pillow库
from queue import Queue
import pygetwindow as gw  # 用于获取窗口信息
from datetime import datetime, timedelta
import calendar
import os
import shutil
from pathlib import Path


def get_dpi_scaling():
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        return dpi / 96.0
    except:
        return 1.0


def set_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 设置DPI感知为系统级别
        return True
    except:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # 回退到旧版API
            return True
        except:
            return False


# ---------- 文本输入（Win32 SendInput，不占用系统剪贴板） ----------
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

try:
    from ctypes import wintypes

    _ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

    _SENDINPUT_READY = True
except Exception:
    _SENDINPUT_READY = False


def _make_key_input(scan=0, flags=0, vk=0):
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union = _INPUTUNION(
        ki=_KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None))
    return inp


def send_unicode_text(text, per_char_delay=0.008):
    """
    直接向系统发送 Unicode 字符（支持中文、emoji），不需要剪贴板。
    返回 True 表示全部发送成功。
    """
    if not _SENDINPUT_READY:
        return False
    text = str(text or "")
    if not text:
        return True
    user32 = ctypes.windll.user32
    for ch in text:
        if ch in ("\n", "\r"):
            seq = [_make_key_input(vk=0x0D), _make_key_input(flags=KEYEVENTF_KEYUP, vk=0x0D)]
        elif ch == "\t":
            seq = [_make_key_input(vk=0x09), _make_key_input(flags=KEYEVENTF_KEYUP, vk=0x09)]
        else:
            # 超出 BMP 的字符（如 emoji）拆成代理对，由系统自行组合
            raw = ch.encode("utf-16-le", errors="ignore")
            units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
            seq = []
            for unit in units:
                seq.append(_make_key_input(scan=unit, flags=KEYEVENTF_UNICODE))
                seq.append(_make_key_input(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        if not seq:
            continue
        arr = (_INPUT * len(seq))(*seq)
        sent = user32.SendInput(len(seq), arr, ctypes.sizeof(_INPUT))
        if sent != len(seq):
            return False
        if per_char_delay:
            time.sleep(per_char_delay)
    return True


class DPIScaler:
    def __init__(self):
        self.scale_factor = 1.0
        self.root = tk.Tk()
        self.root.withdraw()
    
    def get_scaling_factor(self):
        try:
            self.scale_factor = self.root.call('tk', 'scaling')
            return self.scale_factor / 1.3333  # 修正Windows的缩放因子计算
        except:
            return 1.0


class EventType:
    KEYBOARD = "keyboard"
    MOUSE = "mouse"


# ---------- 用户配置（跨会话保存） ----------
DEFAULT_SCHEDULER_COUNTDOWN = 3  # 定时任务默认启动倒计时（秒）
LOG_MAX_LINES = 1500  # 执行日志最多保留的行数，超出后自动裁剪最旧内容


def get_config_dir():
    """返回用户配置文件目录（与定时任务配置同目录）。"""
    try:
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", str(Path.home()))) / "MiaoXiangQiJi"
        else:
            base = Path.home() / ".config" / "miaoxiang_qiji"
        base.mkdir(parents=True, exist_ok=True)
        return base
    except Exception:
        return Path.home()


APP_SETTINGS_FILE = get_config_dir() / "app_settings.json"


def load_app_settings():
    try:
        if APP_SETTINGS_FILE.exists():
            data = json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_app_settings(settings):
    try:
        APP_SETTINGS_FILE.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


# ---------- 键盘按键解析（截图流程的键盘操作） ----------
KEY_ACTION = "键盘按键"
TEXT_ACTION = "输入文本"
MOUSE_ACTIONS = [
    "左键单击", "左键双击", "右键单击", "右键双击", "中键单击",
    "左键按下", "左键释放", "右键按下", "右键释放", "仅移动鼠标",
    "滚轮下滚", "滚轮上滚",
]
SCROLL_ACTIONS = ("滚轮下滚", "滚轮上滚")
SCREENSHOT_ACTIONS = MOUSE_ACTIONS + [KEY_ACTION, TEXT_ACTION]

KEY_TOKEN_ALIASES = {
    "ctrl": Key.ctrl, "control": Key.ctrl, "ctl": Key.ctrl,
    "ctrl_l": Key.ctrl_l, "ctrl_r": Key.ctrl_r, "左ctrl": Key.ctrl_l, "右ctrl": Key.ctrl_r,
    "alt": Key.alt, "alt_l": Key.alt_l, "alt_r": Key.alt_r, "altgr": Key.alt_gr,
    "shift": Key.shift, "shift_l": Key.shift_l, "shift_r": Key.shift_r,
    "win": Key.cmd, "windows": Key.cmd, "cmd": Key.cmd, "meta": Key.cmd,
    "cmd_l": Key.cmd_l, "cmd_r": Key.cmd_r,
    "enter": Key.enter, "return": Key.enter, "回车": Key.enter, "确认": Key.enter,
    "tab": Key.tab, "制表": Key.tab,
    "esc": Key.esc, "escape": Key.esc, "退出": Key.esc,
    "space": Key.space, "空格": Key.space,
    "backspace": Key.backspace, "退格": Key.backspace,
    "delete": Key.delete, "del": Key.delete, "删除": Key.delete,
    "insert": Key.insert, "ins": Key.insert, "插入": Key.insert,
    "home": Key.home, "end": Key.end,
    "pageup": Key.page_up, "page_up": Key.page_up, "pgup": Key.page_up,
    "pagedown": Key.page_down, "page_down": Key.page_down, "pgdn": Key.page_down,
    "up": Key.up, "上": Key.up, "↑": Key.up,
    "down": Key.down, "下": Key.down, "↓": Key.down,
    "left": Key.left, "左": Key.left, "←": Key.left,
    "right": Key.right, "右": Key.right, "→": Key.right,
    "capslock": Key.caps_lock, "caps_lock": Key.caps_lock,
    "numlock": Key.num_lock, "num_lock": Key.num_lock,
    "scrolllock": Key.scroll_lock, "scroll_lock": Key.scroll_lock,
    "printscreen": Key.print_screen, "print_screen": Key.print_screen,
    "pause": Key.pause, "menu": Key.menu,
    "plus": KeyCode.from_char("+"),
}

for _i in range(1, 21):
    _fkey = getattr(Key, f"f{_i}", None)
    if _fkey is not None:
        KEY_TOKEN_ALIASES[f"f{_i}"] = _fkey


def resolve_key_token(token):
    """把用户输入的按键名解析成 pynput 按键对象；无法识别时返回 None。"""
    if token is None:
        return None
    raw = str(token).strip()
    if not raw:
        return None
    low = raw.lower()
    if low in KEY_TOKEN_ALIASES:
        return KEY_TOKEN_ALIASES[low]
    if len(raw) == 1:
        return KeyCode.from_char(raw)
    return None


def parse_key_sequence(combo):
    """
    解析按键组合/序列字符串。
    组合用 + 连接（如 ctrl+shift+s）；多个动作可用逗号或空格分隔（如 tab, enter）。
    返回 [ [按键对象, ...], ... ]，每一步是一组需要同时按下的键。
    """
    combo = str(combo or "").strip()
    if not combo:
        return []
    # 支持 ctrl++ 这种“加号键”的写法
    normalized = combo.replace("++", "+plus+")
    steps = []
    for chunk in normalized.replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        tokens = [t for t in chunk.split("+") if t.strip()]
        keys = [resolve_key_token(t) for t in tokens]
        if not keys or any(k is None for k in keys):
            # 只要有无法识别的键名，就视为整条无效，避免只按下修饰键的“半截操作”
            return []
        steps.append(keys)
    return steps


class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None
        self.widget.bind("<Enter>", self.show_tip)
        self.widget.bind("<Leave>", self.hide_tip)
        self.drag_data = {'item': None, 'index': None}
    
    def show_tip(self, event=None):
        """显示提示"""
        if self.tip_window:
            return
        x, y, _, _ = self.widget.bbox("insert")
        x += self.widget.winfo_rootx() + 25
        y += self.widget.winfo_rooty() + 25
        
        self.tip_window = tk.Toplevel(self.widget)
        self.tip_window.wm_overrideredirect(True)
        self.tip_window.wm_geometry(f"+{x}+{y}")
        
        label = tk.Label(self.tip_window, text=self.text, justify=tk.LEFT,
                         background="#ffffe0", relief=tk.SOLID, borderwidth=1,
                         font=("微软雅黑", 10))
        label.pack()
    
    def hide_tip(self, event=None):
        """隐藏提示"""
        if self.tip_window:
            self.tip_window.destroy()
        self.tip_window = None


class ImageMatcher:
    def __init__(self):
        self.method = cv2.TM_CCOEFF_NORMED
        self.min_match_value = 0.5  # 默认匹配阈值
        self.screenshot_queue = Queue()
        self.active = False
        self.lock = Lock()
        self.current_target = None
        self.match_found = Event()
        self.window_title = None
        self.window_rect = None
    
    def set_target(self, image_path, threshold=0.5):
        with self.lock:
            self.current_target = {
                'image':     cv2.imread(image_path, cv2.IMREAD_COLOR),
                'threshold': threshold,
                'path':      image_path
            }
            self.min_match_value = threshold
            self.match_found.clear()
    
    def start(self, window_title=None):
        self.active = True
        self.window_title = window_title
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._match_loop, daemon=True).start()
    
    def stop(self):
        self.active = False
    
    def _capture_loop(self):
        while self.active:
            try:
                # 获取指定窗口
                if self.window_title:
                    windows = gw.getWindowsWithTitle(self.window_title)
                    if windows:
                        win = windows[0]
                        if win.isMinimized:
                            continue
                        self.window_rect = (win.left, win.top, win.width, win.height)
                        screenshot = ImageGrab.grab(bbox=self.window_rect)
                    else:
                        # 如果找不到窗口，捕获整个屏幕
                        screenshot = ImageGrab.grab()
                        self.window_rect = None
                else:
                    # 捕获整个屏幕
                    screenshot = ImageGrab.grab()
                    self.window_rect = None
                
                # 转换为OpenCV格式
                screenshot_cv = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
                
                # 添加到队列
                if self.screenshot_queue.qsize() < 3:  # 限制队列大小
                    self.screenshot_queue.put(screenshot_cv)
                
                time.sleep(0.3)  # 控制捕获频率
            except Exception as e:
                print(f"截图捕获错误: {str(e)}")
                time.sleep(1)
    
    def _match_loop(self):
        while self.active:
            try:
                if self.screenshot_queue.empty() or not self.current_target:
                    time.sleep(0.1)
                    continue
                
                screenshot = self.screenshot_queue.get()
                target = self.current_target['image']
                
                # 如果目标图像比屏幕截图大，跳过
                if target.shape[0] > screenshot.shape[0] or target.shape[1] > screenshot.shape[1]:
                    continue
                
                # 模板匹配
                result = cv2.matchTemplate(screenshot, target, self.method)
                min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)
                
                # 检查是否找到匹配
                if max_val >= self.min_match_value:
                    self.match_found.set()
                    # 清空队列，避免重复触发
                    while not self.screenshot_queue.empty():
                        self.screenshot_queue.get()
            except Exception as e:
                print(f"图像匹配错误: {str(e)}")
                time.sleep(0.5)


class ScriptRecorder:
    """
    工业级录制器（稳定版）
    特性：
    - 完整记录所有键盘/鼠标事件
    - 支持 Ctrl/Shift/Alt 组合键
    - 不丢事件、不吞键
    - 统一 key 规范化
    """
    
    def __init__(self, dpi_scale=1.0):
        self.dpi_scale = dpi_scale
        
        self.events = []
        self.start_time = None
        
        self.mouse_listener = None
        self.keyboard_listener = None
        
        self.is_recording = False
        
        # 当前按下的修饰键状态（工业级关键）
        self.modifiers = {
            "ctrl":  False,
            "alt":   False,
            "shift": False
        }
        
        # 当前按下的普通键集合（防止重复释放）
        self.pressed_keys = set()
        
        # 线程锁（防止回调冲突）
        self.lock = Lock()
    
    # =========================
    # 启动 / 停止
    # =========================
    
    def start_recording(self):
        with self.lock:
            self.events = []
            self.start_time = time.time()
            self.is_recording = True
            self.pressed_keys.clear()
            
            self.modifiers = {
                "ctrl":  False,
                "alt":   False,
                "shift": False
            }
        
        self.mouse_listener = mouse.Listener(
            on_move=self.on_move,
            on_click=self.on_click,
            on_scroll=self.on_scroll
        )
        self.mouse_listener.start()
        
        self.keyboard_listener = keyboard.Listener(
            on_press=self.on_press,
            on_release=self.on_release
        )
        self.keyboard_listener.start()
    
    def stop_recording(self):
        if not self.is_recording:
            return None
        
        self.is_recording = False
        
        if self.mouse_listener:
            self.mouse_listener.stop()
        
        if self.keyboard_listener:
            self.keyboard_listener.stop()
        
        # 强制释放残留按键（工业级一致性）
        with self.lock:
            for k in list(self.pressed_keys):
                self._push("keyboard", ("release", k))
            self.pressed_keys.clear()
        
        return self.events
    
    # =========================
    # 事件记录核心
    # =========================
    
    def _push(self, event_type, event):
        timestamp = time.time() - self.start_time
        self.events.append((timestamp, event_type, event))
    
    # =========================
    # 鼠标
    # =========================
    
    def on_move(self, x, y):
        if not self.is_recording:
            return
        self._push("mouse", ("move", (x / self.dpi_scale, y / self.dpi_scale)))
    
    def on_click(self, x, y, button, pressed):
        if not self.is_recording:
            return
        self._push(
            "mouse",
            ("click", (
                x / self.dpi_scale,
                y / self.dpi_scale,
                button.name,
                pressed
            ))
        )
    
    def on_scroll(self, x, y, dx, dy):
        if not self.is_recording:
            return
        self._push(
            "mouse",
            ("scroll", (
                x / self.dpi_scale,
                y / self.dpi_scale,
                dx,
                dy
            ))
        )
    
    # =========================
    # 键盘（工业核心）
    # =========================
    
    def on_press(self, key):
        if not self.is_recording:
            return
        
        key_name = self._normalize_key(key)
        if key_name == "unknown":
            return
        
        # 先更新修饰键状态，确保 Ctrl+C / Ctrl+V 等组合键状态正确。
        if key_name == "ctrl":
            self.modifiers["ctrl"] = True
        elif key_name == "alt":
            self.modifiers["alt"] = True
        elif key_name == "shift":
            self.modifiers["shift"] = True
        
        # 防止系统按键自动重复导致重复 press。
        if key_name not in self.pressed_keys:
            self.pressed_keys.add(key_name)
            self._push("keyboard", ("press", self._format_key_output(key_name)))
    
    def on_release(self, key):
        if not self.is_recording:
            return
        
        key_name = self._normalize_key(key)
        if key_name == "unknown":
            return
        
        # 先记录 release，再清除修饰键状态，保持完整的组合键事件顺序。
        if key_name in self.pressed_keys:
            self.pressed_keys.remove(key_name)
            self._push("keyboard", ("release", self._format_key_output(key_name)))
        
        if key_name == "ctrl":
            self.modifiers["ctrl"] = False
        elif key_name == "alt":
            self.modifiers["alt"] = False
        elif key_name == "shift":
            self.modifiers["shift"] = False
    
    # =========================
    # Key 统一标准化
    # =========================
    
    def _normalize_key(self, key):
        """
        将 pynput 按键统一转换为可保存、可回放的字符串。

        Windows 在 Ctrl 按下时，字符键的 char 可能变成控制字符，
        例如 Ctrl+C 可能得到 '\x03'，Ctrl+V 可能得到 '\x16'。
        因此优先使用虚拟键码 VK 还原 A-Z 和 0-9。
        """
        
        # 左右修饰键统一名称，避免录制与回放不一致。
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            return "ctrl"
        if key in (keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr):
            return "alt"
        if key in (keyboard.Key.shift_l, keyboard.Key.shift_r):
            return "shift"
        
        # 其他特殊键，如 enter、tab、esc、f1 等。
        if isinstance(key, keyboard.Key):
            return key.name
        
        vk = getattr(key, "vk", None)
        
        # Windows 虚拟键码：A-Z 为 65-90，统一保存为小写字符。
        if vk is not None and 65 <= vk <= 90:
            return chr(vk).lower()
        
        # 主键盘数字 0-9。
        if vk is not None and 48 <= vk <= 57:
            return chr(vk)
        
        char = getattr(key, "char", None)
        if char:
            # Ctrl+A-Z 有时会被 pynput 表示为 ASCII 控制字符 1-26。
            if len(char) == 1 and 1 <= ord(char) <= 26:
                return chr(ord("a") + ord(char) - 1)
            return char.lower() if len(char) == 1 else char
        
        # 无法按字符表示的键保留 VK，供播放器使用 KeyCode.from_vk 回放。
        if vk is not None:
            return f"vk_{vk}"
        
        return "unknown"
    
    # =========================
    # 输出格式统一（关键）
    # =========================
    
    def _format_key_output(self, key_name):
        """
        工业级输出结构：
        统一保证 replay 可解析
        """
        
        # 虚拟键
        if key_name.startswith("vk_"):
            return key_name
        
        # 单字符
        if len(key_name) == 1:
            return key_name
        
        # 特殊键
        return key_name


class ScriptPlayer:
    def __init__(self, dpi_scale=1.0, app=None):  # 新增app参数
        self.dpi_scale = dpi_scale  # 添加当前DPI缩放属性
        self.app = app  # 保存应用实例引用
        self.mouse_controller = MouseController()
        self.keyboard_controller = KeyboardController()
        self.is_playing = False
        self.should_stop = False
        self.paused = False
        self.pause_flag = threading.Event()
        self.pause_condition = threading.Condition()
        self.interrupt_flag = False  # 新增中断标志
        self.current_script = None  # 当前执行的脚本名称
    
    def pause_execution(self):
        with self.pause_condition:
            self.paused = True
    
    def resume_execution(self):
        with self.pause_condition:
            self.paused = False
            self.pause_condition.notify_all()
    
    def interrupt(self):
        """中断当前脚本的执行"""
        self.interrupt_flag = True
        self.should_stop = True
    
    def play_events(self, events, speed=1, repeat=1, progress_callback=None):
        self._thread = threading.current_thread()
        self.is_playing = True
        self.should_stop = False
        self.interrupt_flag = False  # 重置中断标志
        total_repeats = repeat
        
        try:
            for current_repeat in range(total_repeats):
                if self.should_stop or self.interrupt_flag:
                    break
                
                prev_time = 0
                for i, (timestamp, event_type, event) in enumerate(events):
                    with self.pause_condition:
                        while self.paused and not self.should_stop and not self.interrupt_flag:
                            self.pause_condition.wait()
                    
                    # 检查是否被中断
                    if self.interrupt_flag:
                        self.app.log_message(f"脚本被中断: {self.current_script}")
                        break
                    
                    # 进度更新（每个事件）
                    if progress_callback and callable(progress_callback):
                        try:
                            progress_callback(1)
                        except Exception as e:
                            if self.app:
                                self.app.log_message(f"进度回调错误: {str(e)}")
                    
                    # 延迟控制
                    delay = max(0, (timestamp - prev_time) / speed)
                    if delay > 0:
                        time.sleep(delay)
                    
                    # 执行事件
                    if event_type == EventType.MOUSE:
                        self.handle_mouse_event(event)
                    elif event_type == EventType.KEYBOARD:
                        self.handle_keyboard_event(event)
                    
                    prev_time = timestamp
                    
                    if self.should_stop or self.interrupt_flag:
                        break
        finally:
            self.is_playing = False
            self.interrupt_flag = False
    
    def stop(self):
        """新增安全停止方法"""
        self.should_stop = True
        self.is_playing = False
        if self._thread and self._thread.is_alive():
            try:
                self._thread.join(timeout=0.5)
            except RuntimeError:
                pass
    
    # 在 ScriptPlayer 类中修改 handle_mouse_event 方法
    def handle_mouse_event(self, event):
        action, data = event
        try:
            if action == 'move':
                x, y = data
                scaled_x = int(x * self.dpi_scale + 0.5)  # 四舍五入
                scaled_y = int(y * self.dpi_scale + 0.5)
                self.mouse_controller.position = (scaled_x, scaled_y)
            elif action == 'click':
                x, y, button, pressed = data
                scaled_x = int(x * self.dpi_scale + 0.5)
                scaled_y = int(y * self.dpi_scale + 0.5)
                self.mouse_controller.position = (scaled_x, scaled_y)
                btn = getattr(mouse.Button, button)
                if pressed:
                    self.mouse_controller.press(btn)
                else:
                    self.mouse_controller.release(btn)
            elif action == 'scroll':
                x, y, dx, dy = data
                scaled_x = int(x * self.dpi_scale + 0.5)
                scaled_y = int(y * self.dpi_scale + 0.5)
                self.mouse_controller.position = (scaled_x, scaled_y)
                self.mouse_controller.scroll(int(dx), int(dy))
        except Exception as e:
            if self.app:
                self.app.log_message(f"鼠标事件执行出错: {str(e)}")
    
    def handle_keyboard_event(self, event):
        action, key = event
        key = self.parse_key(key)
        # 标记为“软件自己发出的按键”，避免被按键触发监听误判成用户按键
        if self.app:
            try:
                self.app.set_synthetic_input(True)
            except Exception:
                pass
        try:
            if action == 'press':
                self.keyboard_controller.press(key)
            elif action == 'release':
                self.keyboard_controller.release(key)
        finally:
            if self.app:
                try:
                    self.app.set_synthetic_input(False)
                except Exception:
                    pass
    
    def parse_key(self, key_str):
        if key_str is None:
            return Key.unknown
        try:
            special_mapping = {
                'cmd':              Key.cmd,
                'cmd_r':            Key.cmd_r,
                'ctrl':             Key.ctrl,
                'ctrl_r':           Key.ctrl_r,
                'media_play_pause': Key.media_play_pause
            }
            if key_str in special_mapping:
                return special_mapping[key_str]
            # 处理虚拟键码
            if key_str.startswith('vk_'):
                vk = int(key_str.split('_')[1])
                return KeyCode.from_vk(vk)
            # 处理Key的名称（例如：将'Key.shift'转换为Key.shift）
            if key_str.startswith('Key.'):
                key_name = key_str[4:]
                return Key[key_name]
            # 尝试直接获取Key
            try:
                return Key[key_str]
            except KeyError:
                # 尝试作为字符处理
                return KeyCode.from_char(key_str)
        except Exception as e:
            if self.app:
                self.app.log_message(f"解析按键失败：{key_str} - {str(e)}")
            return Key.unknown


# ---------- 截图顺序点击引擎（第二种操作模式） ----------
class ScreenshotClickEngine:
    """截图流程引擎：支持任务组、普通节点、分支节点，以及每次重复前重新识别。"""
    
    def __init__(self, app):
        self.app = app
        self.groups = []
        self.active_group_name = "默认组"
        self.current_index = 0
        self.running = False
        self.thread = None
        self.lock = Lock()
        # 防死机：单步（一次识别操作）最长等待时间，超过则自动停止当前任务
        self.step_timeout_seconds = 300.0
        # 整组循环次数：一次启动里把任务列表从头到尾跑几遍
        self.run_times = 1
        self._loop_base = 0
        self._loop_total = 0
        
        if sys.platform == "win32":
            base_dir = Path(os.environ.get("APPDATA", Path.home())) / "MiaoXiangQiJi"
        else:
            base_dir = Path.home() / ".config" / "miaoxiang_qiji"
        self.data_dir = base_dir / "screenshot_click"
        self.image_dir = self.data_dir / "images"
        self.save_file = self.data_dir / "tasks.json"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.load_tasks()
    
    @property
    def tasks(self):
        group = self.get_active_group()
        return group["tasks"] if group else []
    
    def get_active_group(self):
        for group in self.groups:
            if group.get("name") == self.active_group_name:
                return group
        if self.groups:
            self.active_group_name = self.groups[0]["name"]
            return self.groups[0]
        return None
    
    def list_groups(self):
        return [g.get("name", "未命名组") for g in self.groups]
    
    def set_active_group(self, name):
        with self.lock:
            if any(g.get("name") == name for g in self.groups):
                if self.running:
                    self.stop(log=False)
                self.active_group_name = name
                self.current_index = 0
                self.save_tasks()
                return True
        return False
    
    def add_group(self, name):
        name = str(name).strip()
        if not name:
            raise ValueError("组名不能为空")
        with self.lock:
            if any(g.get("name") == name for g in self.groups):
                raise ValueError("该组名已经存在")
            self.groups.append({"name": name, "tasks": []})
            self.active_group_name = name
            self.save_tasks()
    
    def rename_group(self, old_name, new_name):
        new_name = str(new_name).strip()
        if not new_name:
            raise ValueError("组名不能为空")
        with self.lock:
            if new_name != old_name and any(g.get("name") == new_name for g in self.groups):
                raise ValueError("该组名已经存在")
            for group in self.groups:
                if group.get("name") == old_name:
                    group["name"] = new_name
                    self.active_group_name = new_name
                    self.save_tasks()
                    return True
        return False
    
    def duplicate_group(self, source_name, new_name):
        source = next((g for g in self.groups if g.get("name") == source_name), None)
        if source is None:
            raise ValueError("找不到要复制的组")
        new_name = str(new_name).strip()
        if not new_name:
            raise ValueError("新组名不能为空")
        if any(g.get("name") == new_name for g in self.groups):
            raise ValueError("该组名已经存在")
        copied_tasks = json.loads(json.dumps(source.get("tasks", []), ensure_ascii=False))
        self.groups.append({"name": new_name, "tasks": copied_tasks})
        self.active_group_name = new_name
        self.save_tasks()
    
    def remove_group(self, name):
        with self.lock:
            if len(self.groups) <= 1:
                raise ValueError("至少需要保留一个截图组")
            group = next((g for g in self.groups if g.get("name") == name), None)
            if group is None:
                return False
            paths = self._all_image_paths(group.get("tasks", []))
            self.groups.remove(group)
            self.active_group_name = self.groups[0]["name"]
            for path in paths:
                self._delete_unused_image(path)
            self.save_tasks()
            return True
    
    def run_single_task(self, group_name, task_index):
        """执行指定组中的单个任务（只执行一次），不改变引擎运行状态"""
        # 保存当前运行状态
        was_running = self.running
        if was_running:
            self.stop(log=False)
        # 切换组
        if not self.set_active_group(group_name):
            raise ValueError(f"组 {group_name} 不存在")
        tasks = self.tasks
        if task_index < 0 or task_index >= len(tasks):
            raise IndexError("任务索引超出范围")
        task = tasks[task_index]
        # 执行单个任务（直接调用 _execute_normal 或 _execute_branch）
        # 注意：_execute_normal 和 _execute_branch 是内部方法，依赖 self.running 等，但我们可以临时设置 running 为 True，执行完毕后恢复
        self.running = True  # 模拟运行状态
        try:
            if task.get("type", "normal") == "branch":
                success = self._execute_branch(task, task_index + 1, len(tasks))
            else:
                success = self._execute_normal(task, task_index + 1, len(tasks))
            # 执行完不管成功与否，都视为完成
            return success
        finally:
            self.running = False
            # 恢复之前的状态（如果之前是运行状态，需要重新启动？但这里我们只执行单次，不恢复）
            # 但为了不影响后续，我们保持停止状态
            # 如果之前是运行状态，用户可能期望继续，但这里我们只执行单次，所以不恢复运行。
    
    def _copy_image(self, image_path):
        source = Path(image_path)
        if not source.exists():
            raise FileNotFoundError(f"截图文件不存在：{source}")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target = self.image_dir / f"{stamp}{source.suffix.lower()}"
        shutil.copy2(source, target)
        return str(target), source.name
    
    def add_task(self, image_path, anchor_x=None, anchor_y=None, offset_x=0, offset_y=0,
                 threshold=0.75, action="左键单击", wait_seconds=0.0, repeat_count=1,
                 key_combo="", key_text="", key_enter_after=False, scroll_notches=5):
        target, display_name = self._copy_image(image_path)
        with self.lock:
            self.tasks.append({
                "type":         "normal", "image_path": target, "display_name": display_name,
                "anchor_x":     None if anchor_x is None else int(anchor_x),
                "anchor_y":     None if anchor_y is None else int(anchor_y),
                "offset_x":     int(offset_x), "offset_y": int(offset_y),
                "threshold":    max(0.01, min(1.0, float(threshold))),
                "action":       action, "wait_seconds": max(0.0, float(wait_seconds)),
                "repeat_count": max(1, int(repeat_count)),
                "key_combo":    str(key_combo or ""),
                "key_text":     str(key_text or ""),
                "key_enter_after": bool(key_enter_after),
                "scroll_notches": max(1, int(scroll_notches))
            })
            self.save_tasks()
    
    def add_branch_task(self, image_paths, threshold=0.75, action="左键单击", wait_seconds=0.0):
        """旧版兼容：使用中心点，无超时（无限等待）"""
        branches_details = [{"image_path": p} for p in image_paths]
        self.add_branch_task_detailed(branches_details, threshold, action, wait_seconds, branch_timeout=0.0)
        if not image_paths:
            raise ValueError("分支节点至少需要一张截图")
        branches = []
        try:
            for path in image_paths:
                target, display_name = self._copy_image(path)
                branches.append({
                    "image_path": target, "display_name": display_name,
                    "anchor_x":   None, "anchor_y": None, "offset_x": 0, "offset_y": 0,
                    "threshold":  max(0.01, min(1.0, float(threshold))),
                    "action":     action, "wait_seconds": max(0.0, float(wait_seconds))
                })
            self.tasks.append({"type": "branch", "display_name": "条件分支", "branches": branches})
            self.save_tasks()
        except Exception:
            for branch in branches:
                self._delete_unused_image(branch.get("image_path"))
            raise
    
    def add_branch_task_detailed(self, branches_details, threshold=0.75, action="左键单击",
                                 wait_seconds=0.0, branch_timeout=0.0,
                                 key_combo="", key_text="", key_enter_after=False,
                                 scroll_notches=5):
        """
        添加条件分支任务（详细版）
        branches_details: list of dict, each dict must contain:
            - 'image_path': 截图文件路径
            - 'anchor_x' (optional): 基准点x，默认图片中心
            - 'anchor_y' (optional): 基准点y，默认图片中心
            - 'offset_x' (optional): x偏移，默认0
            - 'offset_y' (optional): y偏移，默认0
        threshold, action, wait_seconds 为全局统一参数
        branch_timeout: 等待超时秒数，0 表示无限等待
        """
        if not branches_details:
            raise ValueError("分支节点至少需要一张截图")
        branches = []
        try:
            for detail in branches_details:
                image_path = detail.get('image_path')
                if not image_path or not os.path.exists(image_path):
                    raise FileNotFoundError(f"截图文件不存在：{image_path}")
                target, display_name = self._copy_image(image_path)
                with Image.open(target) as img:
                    w, h = img.size
                anchor_x = detail.get('anchor_x')
                anchor_y = detail.get('anchor_y')
                if anchor_x is None:
                    anchor_x = w // 2
                if anchor_y is None:
                    anchor_y = h // 2
                branches.append({
                    "image_path":   target,
                    "display_name": display_name,
                    "anchor_x":     int(anchor_x),
                    "anchor_y":     int(anchor_y),
                    "offset_x":     int(detail.get('offset_x', 0)),
                    "offset_y":     int(detail.get('offset_y', 0)),
                    "threshold":    max(0.01, min(1.0, float(threshold))),
                    "action":       detail.get('action') or action,
                    "wait_seconds": max(0.0, float(wait_seconds)),
                    "key_combo":    str(detail.get('key_combo', key_combo) or ""),
                    "key_text":     str(detail.get('key_text', key_text) or ""),
                    "key_enter_after": bool(detail.get('key_enter_after', key_enter_after)),
                    "scroll_notches": max(1, int(detail.get('scroll_notches', scroll_notches)))
                })
            with self.lock:
                self.tasks.append({
                    "type":           "branch",
                    "display_name":   "条件分支",
                    "branches":       branches,
                    "branch_timeout": max(0.0, float(branch_timeout))
                })
                self.save_tasks()
        except Exception:
            for branch in branches:
                self._delete_unused_image(branch.get("image_path"))
            raise
    
    def update_task(self, index, anchor_x=None, anchor_y=None, offset_x=0, offset_y=0,
                    threshold=0.75, action="左键单击", wait_seconds=0.0, repeat_count=1,
                    key_combo="", key_text="", key_enter_after=False, scroll_notches=5):
        with self.lock:
            if not (0 <= index < len(self.tasks)) or self.tasks[index].get("type", "normal") != "normal":
                return False
            task = self.tasks[index]
            task.update({
                "anchor_x":     None if anchor_x is None else int(anchor_x),
                "anchor_y":     None if anchor_y is None else int(anchor_y),
                "offset_x":     int(offset_x), "offset_y": int(offset_y),
                "threshold":    max(0.01, min(1.0, float(threshold))),
                "action":       action, "wait_seconds": max(0.0, float(wait_seconds)),
                "repeat_count": max(1, int(repeat_count)),
                "key_combo":    str(key_combo or ""),
                "key_text":     str(key_text or ""),
                "key_enter_after": bool(key_enter_after),
                "scroll_notches": max(1, int(scroll_notches))
            })
            self.save_tasks()
            return True
    
    def remove_task(self, index):
        with self.lock:
            if 0 <= index < len(self.tasks):
                task = self.tasks.pop(index)
                for path in self._all_image_paths([task]):
                    self._delete_unused_image(path)
                self.current_index = min(self.current_index, len(self.tasks))
                self.save_tasks()
    
    def move_up(self, index):
        with self.lock:
            if 1 <= index < len(self.tasks):
                self.tasks[index - 1], self.tasks[index] = self.tasks[index], self.tasks[index - 1]
                self.save_tasks()
    
    def move_down(self, index):
        with self.lock:
            if 0 <= index < len(self.tasks) - 1:
                self.tasks[index + 1], self.tasks[index] = self.tasks[index], self.tasks[index + 1]
                self.save_tasks()
    
    def clear(self):
        with self.lock:
            paths = self._all_image_paths(self.tasks)
            self.tasks.clear()
            self.current_index = 0
            for path in paths:
                self._delete_unused_image(path)
            self.save_tasks()
    
    def start(self, times=None):
        if self.running:
            self.app.log_message("截图流程已在运行")
            return
        if not self.tasks:
            self.app.log_message("当前截图组没有任务")
            return
        if times is not None:
            try:
                self.run_times = max(1, min(9999, int(times)))
            except Exception:
                self.run_times = 1
        self.running = True
        self.stop_reason = ""
        self.current_index = 0
        self._report_progress()   # 进度条归零
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
    
    def stop(self, log=True):
        was_running = self.running
        self.running = False
        self.stop_reason = ""
        if log and was_running:
            self.app.log_message("截图流程已停止")
        self.app.after(0, lambda: self.app.update_screenshot_mode_status("已停止"))
    
    def trigger_restart(self):
        """按键触发：立刻中断当前执行，并让当前截图组从第一个任务重新开始。"""
        threading.Thread(target=self._trigger_restart_worker, daemon=True).start()
    
    def _trigger_restart_worker(self):
        group_name = self.active_group_name
        if self.running:
            self.running = False
            worker = self.thread
            if worker and worker.is_alive() and worker is not threading.current_thread():
                worker.join(timeout=3.0)
        self.current_index = 0
        if not self.tasks:
            self.app.log_message(f"按键触发：截图组 [{group_name}] 没有任务，已忽略")
            self.app.after(0, lambda: self.app.update_screenshot_mode_status(
                f"按键触发失败：组 [{group_name}] 为空"))
            return
        self.app.log_message(f"按键触发：截图组 [{group_name}] 从头开始执行")
        self.start()
    
    def _check_step_timeout(self, started_at, task_number, total, display_name):
        """单步超时（默认 5 分钟）自动停止，防止一直卡在等截图。"""
        timeout = float(getattr(self, "step_timeout_seconds", 300.0) or 0)
        if timeout <= 0 or (time.time() - started_at) < timeout:
            return False
        span = f"{timeout / 60:g} 分钟" if timeout >= 60 else f"{timeout:g} 秒"
        reason = f"已超时自动停止：第 {task_number} 步「{display_name}」等待超过 {span}"
        self.stop_reason = reason
        self.app.log_message(
            f"防死机保护：第 {task_number}/{total} 步「{display_name}」连续 {span}没有识别成功，"
            f"已自动停止当前任务")
        self.running = False
        self.app.after(0, lambda: self.app.update_screenshot_mode_status(reason))
        return True
    
    def _report_progress(self):
        """把截图流程进度推送到主界面进度条（跨轮累计：已完成步数 / 总步数）"""
        try:
            total_tasks = len(self.tasks)
            base = int(getattr(self, "_loop_base", 0))
            grand_total = int(getattr(self, "_loop_total", 0)) or total_tasks
            done = base + max(0, min(self.current_index, total_tasks))
            pct = (done / grand_total * 100.0) if grand_total else 0.0
            detail = f"{done}/{grand_total} 步" if grand_total else ""
            app = self.app
            app.after(0, lambda p=pct, d=detail: app.update_progress(p, d))
        except Exception:
            pass

    def _run_loop(self):
        group_name = self.active_group_name
        times = max(1, int(getattr(self, "run_times", 1)))
        total_tasks = len(self.tasks)
        completed_rounds = 0
        self.app.log_message(
            f"截图流程组 [{group_name}] 开始运行" + (f"，共 {times} 轮" if times > 1 else ""))

        for round_i in range(times):
            if not self.running or not self.tasks:
                break
            if times > 1:
                self.app.log_message(f"── 第 {round_i + 1}/{times} 轮 ──")
            self.current_index = 0
            self._loop_base = round_i * total_tasks
            self._loop_total = times * total_tasks
            self._report_progress()
            finished = self._run_tasks_once(group_name)
            if finished and self.running:
                completed_rounds += 1
                if round_i < times - 1:
                    self._interruptible_wait(0.5)
            else:
                break

        if not self.tasks:
            return
        if completed_rounds >= times and self.running:
            self.app.log_message(
                f"截图流程组 [{group_name}] 已全部完成" + (f"（共 {times} 轮）" if times > 1 else ""))
            grand_total = times * total_tasks
            self.app.after(0, lambda: self.app.update_progress(100, f"{grand_total}/{grand_total} 步"))
            self.app.after(0, lambda: self.app.update_screenshot_mode_status("全部任务已完成"))
        else:
            reason = getattr(self, "stop_reason", "")
            if reason:
                self.stop_reason = ""
                self.app.after(0, lambda r=reason: self.app.update_screenshot_mode_status(r))
            else:
                self.app.after(0, lambda: self.app.update_screenshot_mode_status("已停止"))

    def _run_tasks_once(self, group_name):
        """从当前组第一个任务跑到最后一个；返回 True=正常跑完一轮，False=中途停止。"""
        while self.running:
            with self.lock:
                if self.current_index >= len(self.tasks):
                    return True
                task = json.loads(json.dumps(self.tasks[self.current_index], ensure_ascii=False))
                task_number = self.current_index + 1
                total = len(self.tasks)

            if task.get("type", "normal") == "branch":
                success = self._execute_branch(task, task_number, total)
            else:
                success = self._execute_normal(task, task_number, total)

            if not self.running:
                return False
            if success:
                with self.lock:
                    self.current_index += 1
                self._report_progress()
                self._interruptible_wait(0.3)
            else:
                time.sleep(0.2)
        return False
    
    def run_single_task(self, group_name, task_index):
        """
        执行指定组中的单个任务（只执行一次），不改变引擎的持续运行状态。
        返回 True 表示任务执行完成（无论是否成功匹配），False 表示出错。
        """
        # 保存当前运行状态
        was_running = self.running
        if was_running:
            self.stop(log=False)
        # 切换组
        if not self.set_active_group(group_name):
            raise ValueError(f"组 {group_name} 不存在")
        tasks = self.tasks
        if task_index < 0 or task_index >= len(tasks):
            raise IndexError("任务索引超出范围")
        task = tasks[task_index]
        # 临时设置 running 为 True 让内部方法可以执行
        self.running = True
        try:
            total = len(tasks)
            task_number = task_index + 1
            if task.get("type", "normal") == "branch":
                success = self._execute_branch(task, task_number, total)
            else:
                success = self._execute_normal(task, task_number, total)
            # 执行完，不论结果，都视为完成（内部已记录日志）
            return True
        except Exception as e:
            self.app.log_message(f"执行单个截图任务出错：{str(e)}")
            return False
        finally:
            self.running = False
            # 如果之前是运行状态，我们选择不自动恢复，因为单次任务执行后应该停止。
            # 如果用户希望之后继续运行整个组，需要手动点击“开始”。
    
    def _execute_normal(self, task, task_number, total):
        repeat_count = max(1, int(task.get("repeat_count", 1)))
        completed = 0
        require_disappear = False
        display_name = task.get("display_name") or "截图任务"
        step_started = time.time()
        while self.running and completed < repeat_count:
            if self._check_step_timeout(step_started, task_number, total, display_name):
                return False
            self.app.after(0, lambda n=task_number, t=total, c=completed, r=repeat_count:
            self.app.update_screenshot_mode_status(
                f"等待第 {n}/{t} 个截图：已完成 {c}/{r} 次"))
            match = self._find_on_screen(task)
            if not match:
                require_disappear = False
                time.sleep(0.2)
                continue
            if require_disappear:
                # 防止同一画面持续存在时被连续点击；必须先消失，再次出现才算下一次识别。
                time.sleep(0.2)
                continue
            if not self._execute_match(task, match, task_number, total):
                return False
            completed += 1
            step_started = time.time()
            require_disappear = completed < repeat_count
            self.app.log_message(
                f"第 {task_number}/{total} 个任务完成第 {completed}/{repeat_count} 次识别操作")
        return completed >= repeat_count
    
    def _execute_branch(self, task, task_number, total):
        branches = task.get("branches", [])
        if not branches:
            self.app.log_message(f"第 {task_number} 个分支节点没有候选截图，已跳过")
            return True
        
        names = " / ".join(b.get("display_name", "分支") for b in branches)
        # 获取超时设置（秒），0 或 None 表示无限等待
        timeout_seconds = float(task.get("branch_timeout", 0.0))
        start_time = time.time()
        
        while self.running:
            # 防死机：分支即使设为无限等待，也不能真的无限等下去
            if self._check_step_timeout(start_time, task_number, total,
                                        task.get("display_name") or "条件分支"):
                return False
            # 同时识别所有候选分支
            for branch in branches:
                match = self._find_on_screen(branch)
                if match:
                    # 命中某一个分支，只执行该分支的操作，然后立即进入下一步
                    if self._execute_match(branch, match, task_number, total):
                        self.app.log_message(
                            f"分支节点 {task_number}/{total} 命中：{branch.get('display_name', '截图')}，"
                            f"执行该分支操作后进入下一任务")
                        return True
                    return False
            
            # 检查是否超时
            if timeout_seconds > 0:
                elapsed = time.time() - start_time
                if elapsed >= timeout_seconds:
                    self.app.log_message(
                        f"分支节点 {task_number}/{total} 等待 {timeout_seconds:.1f} 秒未命中任何候选截图，"
                        f"已跳过该分支，继续主线下一个任务")
                    return True
                # 更新状态提示剩余等待时间
                remaining = timeout_seconds - elapsed
                self.app.after(0, lambda n=task_number, t=total, r=remaining, nm=names:
                self.app.update_screenshot_mode_status(
                    f"等待分支 {n}/{t}：{nm}（剩余 {r:.1f}s）"))
            else:
                self.app.after(0, lambda n=task_number, t=total, nm=names:
                self.app.update_screenshot_mode_status(
                    f"等待分支 {n}/{t}：{nm}（无限等待）"))
            
            time.sleep(0.15)
        
        return False
    
    def _execute_match(self, task, match, task_number, total):
        left_x, top_y, width, height, score = match
        anchor_x = task.get("anchor_x")
        anchor_y = task.get("anchor_y")
        if anchor_x is None: anchor_x = width // 2
        if anchor_y is None: anchor_y = height // 2
        click_x = int(left_x + anchor_x + task.get("offset_x", 0))
        click_y = int(top_y + anchor_y + task.get("offset_y", 0))
        wait_seconds = max(0.0, float(task.get("wait_seconds", 0.0)))
        if wait_seconds and not self._interruptible_wait(wait_seconds):
            return False
        action = task.get("action", "左键单击")
        # 滚轮滚动：滚轮事件作用于鼠标所在位置，先移到屏幕中央再滚
        if action in SCROLL_ACTIONS:
            notches = max(1, int(task.get("scroll_notches", 5) or 5))
            dy = -notches if action == "滚轮上滚" else notches
            try:
                controller = self.app.player.mouse_controller
                dpi = getattr(self.app, "dpi_scale", 1.0) or 1.0
                try:
                    sw = self.app.winfo_screenwidth()
                    sh = self.app.winfo_screenheight()
                    controller.position = (int(sw / dpi / 2), int(sh / dpi / 2))
                except Exception:
                    pass
                time.sleep(0.05)
                controller.scroll(0, dy)
            except Exception as e:
                self.app.log_message(f"滚轮滚动失败：{str(e)}")
                return False
            self.app.log_message(
                f"识别成功：{task.get('display_name', '截图')}，匹配度 {score:.3f}，"
                f"滚轮{'上' if dy < 0 else '下'}滚 {notches} 格")
            return True
        # 键盘类操作不移动鼠标，避免影响当前焦点位置
        if action in (KEY_ACTION, TEXT_ACTION):
            # 标记为软件自己发出的按键，避免触发“按键触发”监听
            try:
                self.app.set_synthetic_input(True)
            except Exception:
                pass
            try:
                ok = self._perform_keyboard_action(task)
            finally:
                try:
                    self.app.set_synthetic_input(False)
                except Exception:
                    pass
            if not ok:
                return False
            self.app.log_message(
                f"识别成功：{task.get('display_name', '截图')}，匹配度 {score:.3f}，"
                f"执行{self._describe_key_action(task)}")
            return True
        dpi = getattr(self.app, "dpi_scale", 1.0) or 1.0
        self.app.player.mouse_controller.position = (int(click_x / dpi), int(click_y / dpi))
        self._perform_mouse_action(action)
        self.app.log_message(
            f"识别成功：{task.get('display_name', '截图')}，匹配度 {score:.3f}，坐标 ({click_x}, {click_y})")
        return True
    
    def _interruptible_wait(self, seconds):
        end_time = time.time() + max(0.0, float(seconds))
        while self.running and time.time() < end_time:
            time.sleep(min(0.05, max(0.0, end_time - time.time())))
        return self.running
    
    def _perform_mouse_action(self, action):
        controller = self.app.player.mouse_controller
        actions = {
            "左键单击":   lambda: controller.click(mouse.Button.left, 1),
            "左键双击":   lambda: controller.click(mouse.Button.left, 2),
            "右键单击":   lambda: controller.click(mouse.Button.right, 1),
            "右键双击":   lambda: controller.click(mouse.Button.right, 2),
            "中键单击":   lambda: controller.click(mouse.Button.middle, 1),
            "左键按下":   lambda: controller.press(mouse.Button.left),
            "左键释放":   lambda: controller.release(mouse.Button.left),
            "右键按下":   lambda: controller.press(mouse.Button.right),
            "右键释放":   lambda: controller.release(mouse.Button.right),
            "仅移动鼠标": lambda: None,
        }
        actions.get(action, actions["左键单击"])()
    
    # ---------- 键盘操作 ----------
    def _describe_key_action(self, task):
        """生成用于日志的键盘操作描述。"""
        if task.get("action") == TEXT_ACTION:
            text = str(task.get("key_text", ""))
            preview = text if len(text) <= 20 else text[:20] + "…"
            suffix = " + 回车" if task.get("key_enter_after") else ""
            return f"输入文本“{preview}”{suffix}"
        return f"按键 [{task.get('key_combo', '')}]"
    
    def _perform_keyboard_action(self, task):
        """执行识别后的键盘操作：按键组合/序列，或文本输入。"""
        controller = self.app.player.keyboard_controller
        if task.get("action") == TEXT_ACTION:
            text = str(task.get("key_text", ""))
            if text and not self._type_text(text):
                return False
            if task.get("key_enter_after"):
                time.sleep(0.08)
                try:
                    controller.press(Key.enter)
                    controller.release(Key.enter)
                except Exception as e:
                    self.app.log_message(f"回车键执行失败：{str(e)}")
                    return False
            return True
        
        combo = str(task.get("key_combo", "")).strip()
        steps = parse_key_sequence(combo)
        if not steps:
            self.app.log_message(f"键盘操作未配置有效按键：{combo or '（空）'}")
            return False
        for step_index, keys in enumerate(steps):
            pressed = []
            try:
                for key in keys:
                    controller.press(key)
                    pressed.append(key)
                for key in reversed(pressed):
                    controller.release(key)
            except Exception as e:
                # 出错时确保按键被释放，避免“卡键”
                for key in reversed(pressed):
                    try:
                        controller.release(key)
                    except Exception:
                        pass
                self.app.log_message(f"键盘按键执行失败（{combo}）：{str(e)}")
                return False
            if step_index < len(steps) - 1:
                time.sleep(0.15)
        return True
    
    def _type_text(self, text):
        """
        输入文本：ASCII 用 pynput 逐字输入，中文等非 ASCII 用 Win32 SendInput。
        全程不读写系统剪贴板，避免把你复制好的文件/图片顶掉。
        """
        text = str(text or "")
        if not text:
            return True
        if all(ord(ch) < 128 for ch in text):
            try:
                self.app.player.keyboard_controller.type(text)
                return True
            except Exception as e:
                self.app.log_message(f"逐字输入失败，改用系统输入：{str(e)}")
        if send_unicode_text(text):
            return True
        self.app.log_message(
            "文本输入失败：系统拒绝了键盘输入（若目标程序以管理员身份运行，请同样以管理员权限启动本软件）")
        return False
    
    def _find_on_screen(self, task):
        try:
            image_path = task["image_path"]
            threshold = float(task.get("threshold", 0.75))
            if not os.path.exists(image_path):
                self.app.log_message(f"截图文件不存在：{image_path}")
                return None
            template = cv2.imread(image_path, cv2.IMREAD_COLOR)
            if template is None:
                return None
            screenshot = ImageGrab.grab(all_screens=True)
            screen = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            th, tw = template.shape[:2]
            sh, sw = screen.shape[:2]
            if th > sh or tw > sw:
                return None
            result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val >= threshold:
                origin = getattr(screenshot, "offset", (0, 0)) if hasattr(screenshot, "offset") else (0, 0)
                return int(origin[0]) + max_loc[0], int(origin[1]) + max_loc[1], tw, th, max_val
            return None
        except Exception as e:
            self.app.log_message(f"截图识别异常：{str(e)}")
            return None
    
    def _all_image_paths(self, tasks):
        paths = []
        for task in tasks:
            if task.get("type", "normal") == "branch":
                paths.extend(b.get("image_path") for b in task.get("branches", []) if b.get("image_path"))
            elif task.get("image_path"):
                paths.append(task.get("image_path"))
        return paths
    
    def save_tasks(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = {"version": 2, "active_group": self.active_group_name, "groups": self.groups}
        with open(self.save_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def load_tasks(self):
        try:
            if not self.save_file.exists():
                self.groups = [{"name": "默认组", "tasks": []}]
                return
            data = json.loads(self.save_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                # 自动迁移旧版单列表格式。
                for item in data:
                    if isinstance(item, dict):
                        item.setdefault("type", "normal")
                self.groups = [{"name": "默认组", "tasks": data}]
                self.active_group_name = "默认组"
                self.save_tasks()
            elif isinstance(data, dict) and isinstance(data.get("groups"), list):
                self.groups = data.get("groups") or [{"name": "默认组", "tasks": []}]
                self.active_group_name = data.get("active_group") or self.groups[0].get("name", "默认组")
            else:
                self.groups = [{"name": "默认组", "tasks": []}]
            for group in self.groups:
                group.setdefault("name", "未命名组")
                group.setdefault("tasks", [])
                for item in group["tasks"]:
                    item.setdefault("type", "normal")
                    if item.get("type") == "normal":
                        item.setdefault("anchor_x", None);
                        item.setdefault("anchor_y", None)
                        item.setdefault("offset_x", 0);
                        item.setdefault("offset_y", 0)
                        item.setdefault("threshold", 0.75);
                        item.setdefault("action", "左键单击")
                        item.setdefault("wait_seconds", 0.0);
                        item.setdefault("repeat_count", 1)
                        item.setdefault("key_combo", "")
                        item.setdefault("key_text", "")
                        item.setdefault("key_enter_after", False)
                        item.setdefault("scroll_notches", 5)
                    elif item.get("type") == "branch":
                        for branch in item.get("branches", []):
                            branch.setdefault("action", "左键单击")
                            branch.setdefault("key_combo", "")
                            branch.setdefault("key_text", "")
                            branch.setdefault("key_enter_after", False)
                            branch.setdefault("scroll_notches", 5)
        except Exception as e:
            self.groups = [{"name": "默认组", "tasks": []}]
            self.active_group_name = "默认组"
            self.app.log_message(f"加载截图流程失败：{str(e)}")
    
    def _delete_unused_image(self, image_path):
        if not image_path:
            return
        all_tasks = []
        for group in self.groups:
            all_tasks.extend(group.get("tasks", []))
        if image_path in self._all_image_paths(all_tasks):
            return
        try:
            path = Path(image_path)
            if path.exists() and path.parent == self.image_dir:
                path.unlink()
        except Exception:
            pass


class ScriptManager:
    def __init__(self, save_dir="scripts", img_dir="images"):
        self.save_dir = save_dir
        self.img_dir = img_dir
        os.makedirs(save_dir, exist_ok=True)
        os.makedirs(img_dir, exist_ok=True)
    
    def save_script(self, name, events):
        try:
            path = os.path.join(self.save_dir, f"{name}.json")
            with open(path, 'w') as f:
                json.dump(events, f, indent=2)
        except Exception as e:
            messagebox.showerror("保存失败", f"错误信息：{str(e)}")
    
    def load_script(self, name):
        path = os.path.join(self.save_dir, f"{name}.json")
        with open(path, 'r') as f:
            return json.load(f)
    
    def list_scripts(self):
        return [f[:-5] for f in os.listdir(self.save_dir) if f.endswith('.json')]
    
    def delete_script(self, name):
        path = os.path.join(self.save_dir, f"{name}.json")
        if os.path.exists(path):
            os.remove(path)
        
        # 同时删除关联的图片
        img_path = os.path.join(self.img_dir, f"{name}.png")
        if os.path.exists(img_path):
            os.remove(img_path)
    
    def save_image(self, script_name, image):
        """保存脚本关联的图像"""
        path = os.path.join(self.img_dir, f"{script_name}.png")
        image.save(path)
        return path
    
    def get_image_path(self, script_name):
        """获取脚本关联的图像路径"""
        path = os.path.join(self.img_dir, f"{script_name}.png")
        return path if os.path.exists(path) else None


class ScriptGroupManager:
    def __init__(self, save_dir="script_groups"):
        self.save_dir = save_dir
        os.makedirs(save_dir, exist_ok=True)
    
    def save_group(self, name, scripts, notice=""):
        # scripts 可以是字符串列表或字典列表，统一转为字典格式
        script_items = []
        for item in scripts:
            if isinstance(item, dict):
                script_items.append(item)
            else:
                script_items.append({"name": item, "notice": ""})
        data = {
            "name":    name,
            "scripts": script_items,
            "notice":  notice
        }
        path = os.path.join(self.save_dir, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def load_group(self, name):
        path = os.path.join(self.save_dir, f"{name}.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 兼容旧版本：为每个步骤添加默认的 wait_seconds 和 auto_continue
        if "steps" in data:
            for step in data["steps"]:
                step.setdefault("wait_seconds", 0.0)
                step.setdefault("auto_continue", True)
        return data
    
    def list_groups(self):
        return [f[:-5] for f in os.listdir(self.save_dir) if f.endswith(".json")]
    
    def delete_group(self, name):
        path = os.path.join(self.save_dir, f"{name}.json")
        if os.path.exists(path):
            os.remove(path)


class MixedGroupManager:
    """混合组管理器：支持混合脚本、脚本组、截图组、截图任务"""
    
    def __init__(self, save_dir="mixed_groups"):
        self.save_dir = save_dir
        os.makedirs(save_dir, exist_ok=True)
    
    def save_group(self, name, steps, notice=""):
        """steps: 列表，每个元素为 dict，必须包含 'type' 和必要的参数"""
        data = {
            "name":   name,
            "steps":  steps,
            "notice": notice
        }
        path = os.path.join(self.save_dir, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def load_group(self, name):
        path = os.path.join(self.save_dir, f"{name}.json")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    
    def list_groups(self):
        return [f[:-5] for f in os.listdir(self.save_dir) if f.endswith(".json")]
    
    def delete_group(self, name):
        path = os.path.join(self.save_dir, f"{name}.json")
        if os.path.exists(path):
            os.remove(path)


# ---------- 定时任务模块 ----------


class CountdownOverlay(tk.Toplevel):
    """
    定时任务启动前的倒计时提示窗：
    - 置顶显示在屏幕中下方，不抢键盘焦点，用户可以先停下手中的输入；
    - 倒计时结束自动执行，也可点“立即开始”或“取消本次”。
    """

    def __init__(self, master, seconds, headline, detail, on_finish):
        super().__init__(master)
        self.duration = max(0.1, float(seconds))
        self._on_finish = on_finish
        self._finished = False
        self._start_time = time.time()
        self._last_second = None

        try:
            self.overrideredirect(True)
            self.attributes("-topmost", True)
        except Exception:
            pass

        width, height = 470, 225
        try:
            screen_w = self.winfo_screenwidth()
            screen_h = self.winfo_screenheight()
            x = int((screen_w - width) / 2)
            y = int(screen_h * 0.60)
            self.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            self.geometry(f"{width}x{height}")

        bg = "#0f2436"
        card = tk.Frame(self, bg=bg, highlightthickness=2, highlightbackground="#37c2ff")
        card.pack(fill=tk.BOTH, expand=True)

        tk.Label(card, text=headline, font=("微软雅黑", 13, "bold"),
                 fg="#e9f6ff", bg=bg).pack(pady=(14, 2))
        tk.Label(card, text=detail, font=("微软雅黑", 9), fg="#9fd0e8", bg=bg,
                 wraplength=430, justify=tk.CENTER).pack(pady=(0, 2))

        self.number_label = tk.Label(card, text=str(int(round(self.duration))),
                                     font=("微软雅黑", 32, "bold"), fg="#37c2ff", bg=bg)
        self.number_label.pack()

        self.progress = ttk.Progressbar(card, orient=tk.HORIZONTAL, length=390,
                                        mode="determinate", maximum=100)
        self.progress.pack(pady=(4, 8))

        btn_row = tk.Frame(card, bg=bg)
        btn_row.pack(pady=(0, 12))
        tk.Button(btn_row, text="立即开始", command=lambda: self._finish(True),
                  bg="#1d7fbf", fg="white", activebackground="#2b95d8", activeforeground="white",
                  relief=tk.FLAT, font=("微软雅黑", 9), width=14).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_row, text="取消本次", command=lambda: self._finish(False),
                  bg="#4a5a66", fg="white", activebackground="#5c6e7b", activeforeground="white",
                  relief=tk.FLAT, font=("微软雅黑", 9), width=14).pack(side=tk.LEFT, padx=6)

        try:
            self.lift()
            self.attributes("-topmost", True)
        except Exception:
            pass

        self._beep(1200, 90)
        self.after(30, self._tick)

    @staticmethod
    def _beep(freq, duration):
        try:
            import winsound
            winsound.Beep(int(freq), int(duration))
        except Exception:
            pass

    def _tick(self):
        if self._finished:
            return
        elapsed = time.time() - self._start_time
        remaining = max(0.0, self.duration - elapsed)
        try:
            self.progress["value"] = min(100.0, elapsed / self.duration * 100.0)
        except Exception:
            pass
        sec_display = int(remaining + 0.999)
        if sec_display != self._last_second:
            self._last_second = sec_display
            try:
                self.number_label.config(text=str(max(1, sec_display)))
            except Exception:
                pass
            if 0 < sec_display <= 3:
                self._beep(880, 60)
        if remaining <= 0:
            self._finish(True)
            return
        self.after(50, self._tick)

    def _finish(self, proceed):
        if self._finished:
            return
        self._finished = True
        try:
            self.destroy()
        except Exception:
            pass
        if callable(self._on_finish):
            try:
                self._on_finish(bool(proceed))
            except Exception:
                pass


def ask_scheduler_countdown(app, seconds, headline, detail):
    """
    在工作线程中调用：主线程弹出倒计时窗，并阻塞等待结果。
    返回 True 表示继续执行，False 表示用户取消本次。
    """
    if seconds is None or float(seconds) <= 0:
        return True
    result = {"proceed": True}
    done = Event()

    def _on_finish(proceed):
        result["proceed"] = proceed
        done.set()

    def _show():
        try:
            CountdownOverlay(app, seconds, headline, detail, _on_finish)
        except Exception as e:
            try:
                app.log_message(f"倒计时提示窗创建失败：{str(e)}")
            except Exception:
                pass
            _on_finish(True)

    try:
        app.after(0, _show)
    except Exception:
        return True
    done.wait(timeout=float(seconds) + 120)
    return bool(result["proceed"])


class ScheduledTask:
    """单个定时任务的数据结构；兼容旧版仅保存 script_name 的配置。"""
    
    TYPE_LABELS = {
        "script":           "普通脚本",
        "script_group":     "脚本组",
        "screenshot_group": "截图流程组",
        "mixed_group":      "混合组",  # 新增
    }
    
    def __init__(self, task_id, target_name=None, rule_type="daily", hour=0, minute=0,
                 weekdays=None, month_day=None, year_month_day=None,
                 repeat_count=0, enabled=True, task_type="script", script_name=None,
                 countdown=-1):
        self.id = task_id
        # 兼容旧调用：ScheduledTask(..., script_name=xxx)
        self.task_type = task_type or "script"
        self.target_name = target_name if target_name is not None else script_name
        self.target_name = self.target_name or ""
        self.rule_type = rule_type
        self.hour = int(hour)
        self.minute = int(minute)
        self.weekdays = weekdays or []
        self.month_day = month_day
        self.year_month_day = year_month_day
        self.repeat_count = repeat_count
        self.enabled = enabled
        # -1 表示跟随全局默认倒计时；0 表示不提示直接执行；>0 为本次倒计时秒数
        self.countdown = -1 if countdown is None else int(countdown)
        self.next_run_time = None

    def resolve_countdown(self, app):
        """返回本任务实际使用的倒计时秒数。"""
        if self.countdown is not None and self.countdown >= 0:
            return float(self.countdown)
        default = getattr(app, "scheduler_countdown", DEFAULT_SCHEDULER_COUNTDOWN)
        try:
            return max(0.0, float(default))
        except Exception:
            return float(DEFAULT_SCHEDULER_COUNTDOWN)
    
    @property
    def script_name(self):
        """旧版代码兼容别名。"""
        return self.target_name
    
    @script_name.setter
    def script_name(self, value):
        self.target_name = value or ""
    
    @property
    def type_text(self):
        return self.TYPE_LABELS.get(self.task_type, self.task_type)
    
    def to_dict(self):
        return {
            "id":             self.id,
            "task_type":      self.task_type,
            "target_name":    self.target_name,
            # 同时保留旧字段，便于旧版本回退读取。
            "script_name":    self.target_name if self.task_type == "script" else None,
            "rule_type":      self.rule_type,
            "hour":           self.hour,
            "minute":         self.minute,
            "weekdays":       self.weekdays,
            "month_day":      self.month_day,
            "year_month_day": self.year_month_day,
            "repeat_count":   self.repeat_count,
            "enabled":        self.enabled,
            "countdown":      self.countdown,
        }
    
    @classmethod
    def from_dict(cls, data):
        # 老配置没有 task_type/target_name 时，自动迁移为普通脚本。
        task_type = data.get("task_type", "script")
        target_name = data.get("target_name") or data.get("script_name", "")
        return cls(
            task_id=data.get("id", -1),
            target_name=target_name,
            task_type=task_type,
            rule_type=data.get("rule_type", "daily"),
            hour=data.get("hour", 0),
            minute=data.get("minute", 0),
            weekdays=data.get("weekdays", []),
            month_day=data.get("month_day"),
            year_month_day=data.get("year_month_day"),
            repeat_count=data.get("repeat_count", 0),
            enabled=data.get("enabled", True),
            countdown=data.get("countdown", -1),
        )


class TaskScheduler:
    """定时任务调度器（单例）"""
    
    def __init__(self, app):
        self.app = app
        self.tasks = []
        self.running = False
        self.thread = None
        self.lock = Lock()
        self.task_counter = 0
        
        # 确定用户配置目录
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
            config_dir = Path(appdata) / "MiaoXiangQiJi"
        else:
            config_dir = Path.home() / ".config" / "miaoxiang_qiji"
        
        # 创建目录（如果不存在）
        config_dir.mkdir(parents=True, exist_ok=True)
        
        self.save_file = config_dir / "scheduled_tasks.json"
        
        # 可选：打印日志确认路径
        self.app.log_message(f"定时任务文件路径: {self.save_file}")
    
    def start(self):
        if self.running:
            return
        self.running = True
        self.load_tasks()
        self.thread = threading.Thread(target=self._schedule_loop, daemon=True)
        self.thread.start()
        self.app.log_message("定时任务调度器已启动")
    
    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)
    
    def add_task(self, task):
        with self.lock:
            task.id = self.task_counter
            self.task_counter += 1
            self.tasks.append(task)
            self._update_next_run_time(task)
        # 异步保存，避免阻塞UI
        threading.Thread(target=self.save_tasks, daemon=True).start()
        self.app.log_message(f"添加定时任务: {task.type_text} [{task.target_name}] ({task.rule_type})")
    
    def remove_task(self, task_id):
        with self.lock:
            for i, t in enumerate(self.tasks):
                if t.id == task_id:
                    del self.tasks[i]
                    threading.Thread(target=self.save_tasks, daemon=True).start()
                    self.app.log_message(f"删除定时任务 ID={task_id}")
                    return True
        return False
    
    def get_tasks(self):
        with self.lock:
            return self.tasks.copy()
    
    def _update_next_run_time(self, task):
        """计算任务的下次执行时间"""
        now = datetime.now()
        base = now.replace(hour=task.hour, minute=task.minute, second=0, microsecond=0)
        if task.rule_type == 'once':
            if base > now:
                task.next_run_time = base
            else:
                task.next_run_time = None
        elif task.rule_type == 'daily':
            if base > now:
                task.next_run_time = base
            else:
                task.next_run_time = base + timedelta(days=1)
        elif task.rule_type == 'weekly':
            target_weekday = task.weekdays
            if not target_weekday:
                target_weekday = [now.weekday()]
            found = None
            for days_ahead in range(1, 8):
                check_date = now + timedelta(days=days_ahead)
                if check_date.weekday() in target_weekday:
                    found = check_date.replace(hour=task.hour, minute=task.minute, second=0, microsecond=0)
                    break
            task.next_run_time = found
        elif task.rule_type == 'monthly':
            target_day = task.month_day if task.month_day else now.day
            year = now.year
            month = now.month
            try:
                candidate = datetime(year, month, target_day, task.hour, task.minute)
            except ValueError:
                candidate = None
            if candidate and candidate > now:
                task.next_run_time = candidate
            else:
                if month == 12:
                    year += 1
                    month = 1
                else:
                    month += 1
                try:
                    candidate = datetime(year, month, target_day, task.hour, task.minute)
                except ValueError:
                    candidate = None
                task.next_run_time = candidate
        elif task.rule_type == 'yearly':
            month, day = task.year_month_day if task.year_month_day else (now.month, now.day)
            year = now.year
            try:
                candidate = datetime(year, month, day, task.hour, task.minute)
            except ValueError:
                candidate = None
            if candidate and candidate > now:
                task.next_run_time = candidate
            else:
                try:
                    candidate = datetime(year + 1, month, day, task.hour, task.minute)
                except ValueError:
                    candidate = None
                task.next_run_time = candidate
        else:
            task.next_run_time = None
    
    def _schedule_loop(self):
        while self.running:
            now = datetime.now()
            tasks_to_execute = []
            with self.lock:
                for task in self.tasks:
                    if not task.enabled:
                        continue
                    if task.next_run_time and task.next_run_time <= now:
                        tasks_to_execute.append(task)
            for task in tasks_to_execute:
                # 执行任务；返回 False 表示用户在倒计时期间取消了本次
                started = self._execute_task(task)
                with self.lock:
                    if not started:
                        self.app.log_message(
                            f"定时任务本次已取消：{task.type_text} [{task.target_name}]")
                        if task.rule_type == 'once':
                            # 单次任务取消后不再重复触发，保留记录但置为禁用
                            task.enabled = False
                            task.next_run_time = None
                        else:
                            self._update_next_run_time(task)
                        threading.Thread(target=self.save_tasks, daemon=True).start()
                        continue
                    # 处理有限重复次数（次数 > 0）
                    if task.repeat_count > 0:
                        task.repeat_count -= 1
                        if task.repeat_count == 0:
                            # 次数用尽，删除任务
                            self.tasks.remove(task)
                            threading.Thread(target=self.save_tasks, daemon=True).start()
                            continue
                    # 更新下次执行时间（无限次或还有剩余次数）
                    self._update_next_run_time(task)
                    # 如果是单次任务且更新后没有下次时间，删除
                    if task.rule_type == 'once' and task.next_run_time is None:
                        self.tasks.remove(task)
                        threading.Thread(target=self.save_tasks, daemon=True).start()
                    else:
                        # 其他情况（包括无限每日任务）保存任务状态
                        threading.Thread(target=self.save_tasks, daemon=True).start()
            time.sleep(1)
    
    def _execute_task(self, task):
        """根据任务类型执行普通脚本、脚本组或截图流程组。

        返回 False 表示用户在启动倒计时中取消了本次执行（不计入执行次数）。
        """
        task_type = getattr(task, "task_type", "script")
        target = getattr(task, "target_name", None) or getattr(task, "script_name", "")
        type_text = getattr(task, "type_text", task_type)
        
        # 有任务在跑时不打扰用户，也不弹倒计时
        try:
            busy = self.app.player.is_playing or self.app.screenshot_engine.running
        except Exception:
            busy = False
        if busy:
            self.app.log_message(
                f"定时任务 [{type_text}：{target}] 跳过：当前已有自动化任务在执行")
            return True
        
        # 启动倒计时：给用户时间先放下手中的输入
        seconds = task.resolve_countdown(self.app)
        if seconds > 0:
            self.app.log_message(
                f"定时任务即将执行（{seconds:g} 秒倒计时）：{type_text} [{target}]")
            if not ask_scheduler_countdown(
                    self.app, seconds,
                    headline="定时任务即将自动执行",
                    detail=f"{type_text}：{target}\n倒计时结束后开始，可点“取消本次”跳过"):
                return False
        
        def run():
            try:
                self.app.log_message(f"定时任务触发：{type_text} [{target}]")
                
                if task_type == "script":
                    events = self.app.script_manager.load_script(target)
                    if not events:
                        self.app.log_message(f"定时任务失败：找不到或脚本为空 [{target}]")
                        return
                    self.app.player.current_script = target
                    speed = max(1, int(self.app.speed_var.get())) if hasattr(self.app, "speed_var") else 1
                    self.app.player.play_events(events, speed=speed, repeat=1)
                    self.app.log_message(f"定时普通脚本完成：[{target}]")
                
                elif task_type == "script_group":
                    if target not in self.app.script_group_manager.list_groups():
                        self.app.log_message(f"定时任务失败：找不到脚本组 [{target}]")
                        return
                    group = self.app.script_group_manager.load_group(target)
                    scripts = group.get("scripts", [])
                    if not scripts:
                        self.app.log_message(f"定时任务失败：脚本组为空 [{target}]")
                        return
                    speed = max(1, int(self.app.speed_var.get())) if hasattr(self.app, "speed_var") else 1
                    for index, item in enumerate(scripts, 1):
                        if self.app.player.should_stop:
                            break
                        script_name = item.get("name") if isinstance(item, dict) else str(item)
                        events = self.app.script_manager.load_script(script_name)
                        if not events:
                            self.app.log_message(
                                f"脚本组 [{target}] 跳过不存在或为空的脚本：{script_name}")
                            continue
                        self.app.log_message(
                            f"定时脚本组 [{target}]：执行 {index}/{len(scripts)} [{script_name}]")
                        self.app.player.current_script = script_name
                        self.app.player.play_events(events, speed=speed, repeat=1)
                    self.app.log_message(f"定时脚本组完成：[{target}]")
                
                elif task_type == "screenshot_group":
                    engine = self.app.screenshot_engine
                    if target not in engine.list_groups():
                        self.app.log_message(f"定时任务失败：找不到截图流程组 [{target}]")
                        return
                    if not engine.set_active_group(target):
                        self.app.log_message(f"定时任务失败：无法切换截图流程组 [{target}]")
                        return
                    if not engine.tasks:
                        self.app.log_message(f"定时任务失败：截图流程组为空 [{target}]")
                        return
                    engine.start()
                    self.app.log_message(f"定时截图流程组已启动：[{target}]")
                
                elif task_type == "mixed_group":
                    mgr = self.app.mixed_group_manager
                    if target not in mgr.list_groups():
                        self.app.log_message(f"定时任务失败：找不到混合组 [{target}]")
                        return
                    group = mgr.load_group(target)
                    steps = group.get("steps", [])
                    if not steps:
                        self.app.log_message(f"定时任务失败：混合组为空 [{target}]")
                        return
                    
                    def run_mixed():
                        # 执行混合组步骤，忽略 auto_continue，全部自动继续
                        for idx, step in enumerate(steps, 1):
                            if self.app.player.should_stop:
                                break
                            success, _ = self.app._execute_mixed_step(step, target, idx, len(steps))
                            if not success:
                                self.app.log_message(f"定时混合组步骤 {idx} 执行失败，终止")
                                break
                        self.app.log_message(f"定时混合组完成：[{target}]")
                    
                    threading.Thread(target=run_mixed, daemon=True).start()
                    self.app.log_message(f"定时混合组已启动：[{target}]")
                
                else:
                    self.app.log_message(f"定时任务失败：未知任务类型 [{task_type}]")
            
            except Exception as e:
                self.app.log_message(f"定时任务执行出错 [{type_text}：{target}]：{str(e)}")
        
        threading.Thread(target=run, daemon=True).start()
        return True
    
    def save_tasks_sync(self):
        """同步保存任务到文件（用于退出时）"""
        try:
            data = []
            with self.lock:
                for task in self.tasks:
                    data.append(task.to_dict())
            with open(self.save_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self.app.log_message(f"定时任务已保存到 {self.save_file}")
        except Exception as e:
            self.app.log_message(f"保存定时任务失败: {str(e)}")
    
    def save_tasks(self):
        """异步保存（供运行时使用）"""
        threading.Thread(target=self.save_tasks_sync, daemon=True).start()
    
    def load_tasks(self):
        try:
            if not os.path.exists(self.save_file):
                self.app.log_message("没有找到定时任务文件，将创建新文件")
                return
            with open(self.save_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            tasks = []
            max_id = 0
            for item in data:
                task = ScheduledTask.from_dict(item)
                tasks.append(task)
                if task.id > max_id:
                    max_id = task.id
            with self.lock:
                self.tasks = tasks
                self.task_counter = max_id + 1
            for task in self.tasks:
                self._update_next_run_time(task)
            self.app.log_message(f"成功加载 {len(self.tasks)} 个定时任务")
        except Exception as e:
            self.app.log_message(f"加载定时任务失败: {str(e)}")
            # 可选择使用 messagebox 警告用户
            # messagebox.showwarning("警告", f"定时任务加载失败，数据可能已损坏: {str(e)}")


class TaskManagementDialog(tk.Toplevel):
    def __init__(self, parent, scheduler):
        super().__init__(parent)
        self.parent = parent
        self.scheduler = scheduler
        self.title("定时任务管理")
        self.geometry("780x560")
        self.minsize(620, 460)
        self._set_window_icon(self)  # 为自己设置图标
        self.setup_ui()
        self.refresh_task_list()
    
    def _set_window_icon(self, window):
        """为指定的窗口设置图标"""
        try:
            if hasattr(self.parent, 'icon_path') and self.parent.icon_path:
                window.iconbitmap(self.parent.icon_path)
        except:
            pass
    
    def _auto_ui_scale(self, event=None):
        """统一UI防溢出保护：窗口缩放时保持控件可见。"""
        try:
            w = self.winfo_width()
            h = self.winfo_height()
            if w < 900 or h < 600:
                return
        except:
            pass
    
    def setup_ui(self):
        # 启动倒计时默认值（全局）
        setting_bar = ttk.LabelFrame(self, text="启动提示", padding=8)
        setting_bar.pack(fill=tk.X, padx=10, pady=(8, 0))
        ttk.Label(setting_bar, text="定时任务触发前倒计时：").pack(side=tk.LEFT)
        self.countdown_var = tk.IntVar(
            value=int(getattr(self.parent, "scheduler_countdown", DEFAULT_SCHEDULER_COUNTDOWN)))
        ttk.Spinbox(setting_bar, from_=0, to=60, textvariable=self.countdown_var,
                    width=5).pack(side=tk.LEFT, padx=5)
        ttk.Label(setting_bar, text="秒（0 = 不提示直接执行；倒计时期间可取消本次）").pack(side=tk.LEFT)
        ttk.Button(setting_bar, text="保存默认值",
                   command=self.save_default_countdown).pack(side=tk.LEFT, padx=10)
        
        list_frame = ttk.LabelFrame(self, text="当前定时任务", padding=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        columns = ('ID', '类型', '目标', '规则', '时间', '剩余次数', '倒计时', '状态')
        self.tree = ttk.Treeview(list_frame, columns=columns, show='headings', height=12)
        for col in columns:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=100)
        self.tree.column('类型', width=95)
        self.tree.column('目标', width=175)
        self.tree.column('规则', width=70)
        self.tree.column('时间', width=70)
        self.tree.column('剩余次数', width=75)
        self.tree.column('倒计时', width=90)
        self.tree.column('状态', width=60)
        self.tree.pack(fill=tk.BOTH, expand=True)
        
        btn_frame = ttk.Frame(list_frame)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="添加任务", command=self.add_task_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="删除任务", command=self.delete_task).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="启用/禁用", command=self.toggle_task).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="刷新", command=self.refresh_task_list).pack(side=tk.LEFT, padx=2)
    
    def save_default_countdown(self):
        try:
            value = int(self.countdown_var.get())
        except Exception:
            messagebox.showerror("错误", "请输入 0～60 之间的整数秒数", parent=self)
            return
        if not 0 <= value <= 60:
            messagebox.showerror("错误", "倒计时范围是 0～60 秒", parent=self)
            return
        self.countdown_var.set(value)
        self.parent.set_scheduler_countdown(value)
        self.refresh_task_list()
        messagebox.showinfo(
            "已保存",
            f"定时任务启动倒计时默认值已设为 {value} 秒。\n"
            f"（0 表示不显示倒计时，直接执行）",
            parent=self)
    
    def refresh_task_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for task in self.scheduler.get_tasks():
            rule_text = {
                'once':    '单次', 'daily': '每日', 'weekly': '每周',
                'monthly': '每月', 'yearly': '每年'
            }.get(task.rule_type, task.rule_type)
            time_str = f"{task.hour:02d}:{task.minute:02d}"
            repeat_str = str(task.repeat_count) if task.repeat_count > 0 else '无限'
            status = '启用' if task.enabled else '禁用'
            if getattr(task, "countdown", -1) is None or task.countdown < 0:
                cd_str = f"默认({getattr(self.parent, 'scheduler_countdown', DEFAULT_SCHEDULER_COUNTDOWN)}s)"
            elif task.countdown == 0:
                cd_str = "不提示"
            else:
                cd_str = f"{task.countdown}s"
            self.tree.insert('', tk.END, values=(
                task.id, task.type_text, task.target_name, rule_text, time_str,
                repeat_str, cd_str, status
            ))
    
    def add_task_dialog(self):
        dialog = tk.Toplevel(self)
        dialog.title("添加定时任务")
        dialog.geometry("430x590")
        dialog.transient(self)
        dialog.grab_set()
        self._set_window_icon(dialog)  # 修正：现在可以传参了
        
        # 任务类型与执行目标
        ttk.Label(dialog, text="任务类型:").pack(pady=(8, 3))
        type_text_var = tk.StringVar(value="普通脚本")
        type_combo = ttk.Combobox(
            dialog, textvariable=type_text_var,
            values=["普通脚本", "脚本组", "截图流程组", "混合组"],
            state="readonly", width=30)
        type_combo.pack(pady=3)
        
        ttk.Label(dialog, text="执行目标:").pack(pady=(8, 3))
        script_var = tk.StringVar()
        script_combo = ttk.Combobox(dialog, textvariable=script_var,
                                    state="readonly", width=30)
        script_combo.pack(pady=3)
        
        def refresh_targets(event=None):
            type_text = type_text_var.get()
            if type_text == "普通脚本":
                values = self.parent.script_manager.list_scripts()
            elif type_text == "脚本组":
                values = self.parent.script_group_manager.list_groups()
            elif type_text == "截图流程组":
                values = self.parent.screenshot_engine.list_groups()
            elif type_text == "混合组":  # 新增
                values = self.parent.mixed_group_manager.list_groups()
            else:
                values = []
            script_combo["values"] = values
            script_var.set(values[0] if values else "")
        
        type_combo.bind("<<ComboboxSelected>>", refresh_targets)
        refresh_targets()
        
        # 规则类型（修正初始值为中文）
        ttk.Label(dialog, text="重复规则:").pack(pady=5)
        rule_var = tk.StringVar(value='每日')
        rule_combo = ttk.Combobox(dialog, textvariable=rule_var,
                                  values=['单次', '每日', '每周', '每月', '每年'], width=20)
        rule_combo.pack(pady=5)
        
        # 时间
        time_frame = ttk.Frame(dialog)
        time_frame.pack(pady=5)
        ttk.Label(time_frame, text="时间:").pack(side=tk.LEFT)
        hour_var = tk.IntVar(value=0)
        minute_var = tk.IntVar(value=0)
        ttk.Spinbox(time_frame, from_=0, to=23, textvariable=hour_var, width=3).pack(side=tk.LEFT, padx=2)
        ttk.Label(time_frame, text=":").pack(side=tk.LEFT)
        ttk.Spinbox(time_frame, from_=0, to=59, textvariable=minute_var, width=3).pack(side=tk.LEFT, padx=2)
        
        # 每周控件
        weekly_frame = ttk.LabelFrame(dialog, text="每周的星期几（多选）")
        weekly_frame.pack(pady=5, fill=tk.X)
        week_vars = {}
        week_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
        for i, name in enumerate(week_names):
            var = tk.BooleanVar()
            week_vars[i] = var
            ttk.Checkbutton(weekly_frame, text=name, variable=var).pack(side=tk.LEFT, padx=2)
        
        # 每月控件
        monthday_frame = ttk.Frame(dialog)
        monthday_frame.pack(pady=5)
        ttk.Label(monthday_frame, text="每月几号 (1-31):").pack(side=tk.LEFT)
        month_day_var = tk.IntVar(value=1)
        ttk.Spinbox(monthday_frame, from_=1, to=31, textvariable=month_day_var, width=5).pack(side=tk.LEFT, padx=5)
        
        # 每年控件
        yearly_frame = ttk.Frame(dialog)
        yearly_frame.pack(pady=5)
        ttk.Label(yearly_frame, text="每年（月/日）:").pack(side=tk.LEFT)
        year_month_var = tk.IntVar(value=1)
        year_day_var = tk.IntVar(value=1)
        ttk.Spinbox(yearly_frame, from_=1, to=12, textvariable=year_month_var, width=3).pack(side=tk.LEFT)
        ttk.Label(yearly_frame, text="/").pack(side=tk.LEFT)
        ttk.Spinbox(yearly_frame, from_=1, to=31, textvariable=year_day_var, width=3).pack(side=tk.LEFT)
        
        # 重复次数
        repeat_frame = ttk.Frame(dialog)
        repeat_frame.pack(pady=5)
        ttk.Label(repeat_frame, text="执行次数 (0=无限):").pack(side=tk.LEFT)
        repeat_var = tk.IntVar(value=0)
        ttk.Spinbox(repeat_frame, from_=0, to=999, textvariable=repeat_var, width=6).pack(side=tk.LEFT, padx=5)
        
        # 启动倒计时（每个任务可单独设置，默认取全局值）
        countdown_frame = ttk.Frame(dialog)
        countdown_frame.pack(pady=5)
        ttk.Label(countdown_frame, text="触发前倒计时 (秒,0=不提示):").pack(side=tk.LEFT)
        countdown_var = tk.IntVar(
            value=int(getattr(self.parent, "scheduler_countdown", DEFAULT_SCHEDULER_COUNTDOWN)))
        ttk.Spinbox(countdown_frame, from_=0, to=60, textvariable=countdown_var,
                    width=6).pack(side=tk.LEFT, padx=5)
        
        def on_rule_change(*args):
            rule = rule_var.get()
            weekly_frame.pack_forget()
            monthday_frame.pack_forget()
            yearly_frame.pack_forget()
            if rule == '每周':
                weekly_frame.pack(pady=5, fill=tk.X)
            elif rule == '每月':
                monthday_frame.pack(pady=5)
            elif rule == '每年':
                yearly_frame.pack(pady=5)
        
        rule_var.trace('w', on_rule_change)
        on_rule_change()
        
        def confirm():
            target_name = script_var.get().strip()
            type_map = {'普通脚本': 'script', '脚本组': 'script_group', '截图流程组': 'screenshot_group',
                        '混合组':   'mixed_group'}
            task_type = type_map.get(type_text_var.get(), 'script')
            if not target_name:
                messagebox.showerror("错误", "请选择脚本")
                return
            rule_map = {'单次': 'once', '每日': 'daily', '每周': 'weekly', '每月': 'monthly', '每年': 'yearly'}
            rule_type = rule_map[rule_var.get()]
            hour = hour_var.get()
            minute = minute_var.get()
            repeat_count = repeat_var.get()
            weekdays = []
            month_day = None
            year_month_day = None
            if rule_type == 'weekly':
                weekdays = [i for i, var in week_vars.items() if var.get()]
                if not weekdays:
                    messagebox.showerror("错误", "请至少选择一个星期几")
                    return
            elif rule_type == 'monthly':
                month_day = month_day_var.get()
            elif rule_type == 'yearly':
                year_month_day = (year_month_var.get(), year_day_var.get())
            task = ScheduledTask(
                task_id=-1, target_name=target_name, task_type=task_type, rule_type=rule_type,
                hour=hour, minute=minute, weekdays=weekdays,
                month_day=month_day, year_month_day=year_month_day,
                repeat_count=repeat_count, enabled=True,
                countdown=countdown_var.get()
            )
            dialog.destroy()  # 先关闭对话框
            self.scheduler.add_task(task)  # 再添加任务（异步保存，不会卡死）
            self.refresh_task_list()
        
        ttk.Button(dialog, text="确定", command=confirm).pack(pady=10)
    
    def delete_task(self):
        selected = self.tree.selection()
        if not selected:
            return
        task_id = int(self.tree.item(selected[0])['values'][0])
        if messagebox.askyesno("确认", f"确定删除任务 ID={task_id} 吗？"):
            self.scheduler.remove_task(task_id)
            self.refresh_task_list()
    
    def toggle_task(self):
        selected = self.tree.selection()
        if not selected:
            return
        task_id = int(self.tree.item(selected[0])['values'][0])
        with self.scheduler.lock:
            for task in self.scheduler.tasks:
                if task.id == task_id:
                    task.enabled = not task.enabled
                    threading.Thread(target=self.scheduler.save_tasks, daemon=True).start()
                    break
        self.refresh_task_list()


# ---------- 主应用 ----------
class AutomationApp(tk.Tk):
    def __init__(self):
        super().__init__()
        
        # 获取图标绝对路径
        self.icon_path = self.get_icon_path()
        
        # 设置窗口图标
        self.set_window_icon()
        
        self.completed = False  # 添加一个标志位，用于确保“所有脚本执行完毕”日志只显示一次
        
        # 新增速度变量初始化
        self.speed_var = tk.IntVar(value=1)  # 默认1倍速
        
        # 应用设置（跨会话保存）
        self.app_settings = load_app_settings()
        try:
            value = int(self.app_settings.get("scheduler_countdown", DEFAULT_SCHEDULER_COUNTDOWN))
        except Exception:
            value = DEFAULT_SCHEDULER_COUNTDOWN
        self.scheduler_countdown = max(0, min(60, value))
        
        # 按键触发（截图顺序识别点击）：软件自己发出的按键需要忽略
        self.synthetic_input_depth = 0
        self.trigger_listener = None
        self.trigger_enabled = False
        self.trigger_steps = []
        self.trigger_pressed = set()
        self.trigger_fired = False
        # 单步防死机超时（秒），默认 5 分钟
        try:
            timeout_minutes = float(self.app_settings.get("screenshot_step_timeout_minutes", 5))
        except Exception:
            timeout_minutes = 5.0
        self.screenshot_step_timeout_minutes = max(0.0, min(120.0, timeout_minutes))
        
        # 在初始化时设置DPI感知
        if sys.platform == "win32":
            set_dpi_awareness()
            self.dpi_scaler = DPIScaler()
            self.dpi_scale = self.dpi_scaler.get_scaling_factor()
        else:
            self.dpi_scale = 1.0
        
        # 添加DPI缩放属性
        self.recording_scale = 1.0  # 录制时的缩放比例
        self.current_scale = self.dpi_scale  # 当前缩放比例
        
        self.title("喵想奇记 - 版权所有 © 爱吃蟹黄先生")
        
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        self.minsize(900, 600)  # 设置最小尺寸
        # 每次启动都铺满全屏（最大化），避免窗口偏小、内容被截断
        self._apply_maximized_launch(screen_w, screen_h)
        # 首帧渲染后再兜底一次（个别环境下 zoomed 需要窗口映射后才生效）
        self.after(150, lambda: self._apply_maximized_launch(
            self.winfo_screenwidth(), self.winfo_screenheight()))
        
        # 配置grid布局权重
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        
        self.search_lock = Lock()
        
        self.drag_data = {'item': None, 'index': None}  # 添加这行
        
        # 新增自动继续变量
        self.auto_continue_var = tk.BooleanVar(value=False)
        
        self.queue_paused = False  # 新增队列暂停状态
        self.current_script_index = 0  # 当前执行脚本索引
        
        self.recorder = ScriptRecorder(dpi_scale=self.dpi_scale)
        self.player = ScriptPlayer(dpi_scale=self.dpi_scale, app=self)
        self.player.app = self
        self.script_manager = ScriptManager()
        self.script_group_manager = ScriptGroupManager()
        self.mixed_group_manager = MixedGroupManager()
        self.expanded_groups = set()
        self.active_group_session = None
        self._mixed_group_session = None
        
        # 截图顺序点击模式
        self.screenshot_engine = ScreenshotClickEngine(self)
        self.screenshot_engine.step_timeout_seconds = self.screenshot_step_timeout_minutes * 60
        
        self.current_queue = []
        self.setup_ui()
        
        self.refresh_script_list()  # 添加这行初始化刷新
        self.setup_hotkeys()
        # 恢复上次启用的按键触发
        if self.app_settings.get("screenshot_trigger_enabled"):
            self.after(800, self.enable_trigger_key)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        
        self.total_events = 0  # 队列总事件数
        self.processed_events = 0  # 已处理事件数
        
        self.bind("<Configure>", self.on_window_resize)
        
        # 添加退出处理
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # 确保热键监听器在类中作为实例变量
        self.hotkey_listener = None
        
        # 图像匹配器
        self.image_matcher = ImageMatcher()
        
        # 设置提示内容
        help_text = """快捷键说明：
        ctrl+alt+F1   - 重新执行
        ctrl+alt+F7   - 暂停执行
        ctrl+alt+F8   - 继续执行
        ctrl+alt+F9   - 开始/停止录制
        ctrl+alt+F10 - 保存录制
        ctrl+alt+F11 - 开始执行脚本
        ctrl+alt+F12 - 停止执行

        脚本组说明：
        未勾选“自动继续”时，脚本组每执行完一个脚本会暂停。
        再次按 Ctrl+Alt+F11 才会继续执行下一个脚本。
        勾选“自动继续”后，脚本组会连续执行完所有脚本。

        定时任务说明：
        任务触发前会显示置顶倒计时（默认 3 秒，可在“定时任务管理”里修改），
        倒计时期间可以点“取消本次”跳过这一轮，不影响下一次执行。

        截图流程说明：
        每张截图识别成功后的操作可选鼠标点击，也可选键盘按键（如 enter、ctrl+s、
        多个动作用逗号分隔）或输入文本（支持中文），不再只能点鼠标。

        按键触发说明：
        在“截图顺序识别点击”里填好触发键（如 ctrl+alt+1）并点“启用按键触发”后，
        每按一次该键，当前截图组就会立刻从第一个任务重新执行；软件自己发出的
        按键会被自动过滤，不会误触发。

        防死机保护：
        任一截图步骤连续 5 分钟没有识别成功，会自动停止当前任务（可在界面调整）。
        """
        Tooltip(self.help_icon, help_text)
        
        # 初始化定时任务调度器
        self.scheduler = TaskScheduler(self)
        self.scheduler.start()
    
    def get_icon_path(self):
        """获取图标的绝对路径"""
        try:
            # 如果是打包后的exe文件
            if getattr(sys, 'frozen', False):
                script_dir = os.path.dirname(sys.executable)
            else:
                # 如果是开发时的脚本
                script_dir = os.path.dirname(os.path.abspath(__file__))
            
            # 构建图标的绝对路径
            icon_path = os.path.join(script_dir, "app_icon.ico")
            
            # 检查图标文件是否存在
            if not os.path.exists(icon_path):
                # 如果图标文件不存在，创建一个简单的临时图标
                self.create_temp_icon(icon_path)
            
            return icon_path
        except Exception as e:
            print(f"获取图标路径时出错: {e}")
            return None
    
    def create_temp_icon(self, icon_path):
        """创建一个简单的临时图标"""
        try:
            # 创建一个简单的16x16像素图标
            img = Image.new('RGB', (16, 16), color='#0078d4')
            img.save(icon_path, format='ICO')
        except Exception as e:
            print(f"创建临时图标时出错: {e}")
    
    def set_window_icon(self):
        """设置窗口图标（兼容模式）"""
        try:
            if self.icon_path and os.path.exists(self.icon_path):
                # 方法1：使用 iconbitmap（Windows 最稳定）
                self.iconbitmap(self.icon_path)
                # 方法2：尝试 PhotoImage（若失败忽略）
                try:
                    self.icon_image = tk.PhotoImage(file=self.icon_path)
                    self.tk.call('wm', 'iconphoto', self._w, self.icon_image)
                except Exception as e:
                    # PhotoImage 失败不影响 iconbitmap
                    pass
                # 递归设置子窗口（可选）
                self._set_all_children_icons()
        except Exception as e:
            print(f"设置图标时出错（已忽略）: {e}")
    
    def _set_all_children_icons(self):
        """递归设置所有子窗口的图标"""
        for child in self.winfo_children():
            try:
                child.iconbitmap(self.icon_path)
                try:
                    child.tk.call('wm', 'iconphoto', child._w, tk.PhotoImage(file=self.icon_path))
                except:
                    pass
                self._set_all_children_icons(child)
            except:
                pass
    
    # =========================
    # 截图点击模式 UI 逻辑
    # =========================
    
    def create_mixed_group_dialog(self):
        """创建混合组对话框"""
        # 获取可用资源
        scripts = self.script_manager.list_scripts()
        script_groups = self.script_group_manager.list_groups()
        screenshot_groups = self.screenshot_engine.list_groups()
        # 截图任务不能直接列表，需要用户选择组和任务，通过对话框实现
        
        dialog = tk.Toplevel(self)
        dialog.title("新建混合组")
        dialog.geometry("700x600")
        dialog.transient(self)
        dialog.grab_set()
        self._set_window_icon(dialog)
        
        # 组名
        ttk.Label(dialog, text="混合组名称：").pack(pady=5)
        name_var = tk.StringVar(value="新混合组")
        ttk.Entry(dialog, textvariable=name_var, width=40).pack(pady=5)
        
        # 组提醒
        ttk.Label(dialog, text="执行前提醒内容（组级别）：").pack(pady=5)
        notice_text = tk.Text(dialog, height=4, width=60)
        notice_text.insert(tk.END, "执行本混合组前，请确认目标环境已就绪。")
        notice_text.pack(pady=5)
        
        # 步骤列表
        step_frame = ttk.LabelFrame(dialog, text="步骤列表（从上到下执行）", padding=5)
        step_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        step_listbox = tk.Listbox(step_frame, selectmode=tk.SINGLE, font=('微软雅黑', 10))
        step_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scroll = ttk.Scrollbar(step_frame, orient=tk.VERTICAL, command=step_listbox.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        step_listbox.config(yscrollcommand=scroll.set)
        
        # 存储步骤数据
        steps = []  # 每个元素为 dict
        
        def refresh_step_list():
            step_listbox.delete(0, tk.END)
            for idx, step in enumerate(steps, 1):
                stype = step.get("type")
                if stype == "script":
                    desc = f"脚本: {step.get('name')}"
                elif stype == "script_group":
                    desc = f"脚本组: {step.get('name')}"
                elif stype == "screenshot_group":
                    desc = f"截图组: {step.get('name')}"
                elif stype == "screenshot_task":
                    desc = f"截图任务: {step.get('display', '任务')} (组:{step.get('group_name')})"
                else:
                    desc = f"步骤{idx}: 未知"
                step_listbox.insert(tk.END, f"{idx}. {desc}")
        
        # 添加步骤的辅助函数
        def add_script_step():
            if not scripts:
                messagebox.showinfo("提示", "没有可用的普通脚本", parent=dialog)
                return
            script_name = self._select_from_list("选择脚本", scripts, parent=dialog)
            if script_name:
                settings = self._show_step_settings_dialog("脚本", script_name, parent=dialog)
                if settings is not None:
                    wait, auto = settings
                    steps.append({
                        "type":          "script",
                        "name":          script_name,
                        "wait_seconds":  wait,
                        "auto_continue": auto
                    })
                    refresh_step_list()
        
        def add_script_group_step():
            if not script_groups:
                messagebox.showinfo("提示", "没有可用的脚本组", parent=dialog)
                return
            group_name = self._select_from_list("选择脚本组", script_groups, parent=dialog)
            if group_name:
                settings = self._show_step_settings_dialog("脚本组", group_name, parent=dialog)
                if settings is not None:
                    wait, auto = settings
                    steps.append({
                        "type":          "script_group",
                        "name":          group_name,
                        "wait_seconds":  wait,
                        "auto_continue": auto
                    })
                    refresh_step_list()
        
        def add_screenshot_group_step():
            if not screenshot_groups:
                messagebox.showinfo("提示", "没有可用的截图组", parent=dialog)
                return
            group_name = self._select_from_list("选择截图组", screenshot_groups, parent=dialog)
            if group_name:
                settings = self._show_step_settings_dialog("截图组", group_name, parent=dialog)
                if settings is not None:
                    wait, auto = settings
                    steps.append({
                        "type":          "screenshot_group",
                        "name":          group_name,
                        "wait_seconds":  wait,
                        "auto_continue": auto
                    })
                    refresh_step_list()
        
        def add_screenshot_task_step():
            if not screenshot_groups:
                messagebox.showinfo("提示", "没有可用的截图组", parent=dialog)
                return
            group_name = self._select_from_list("选择截图组", screenshot_groups, parent=dialog)
            if not group_name:
                return
            if not self.screenshot_engine.set_active_group(group_name):
                messagebox.showerror("错误", "无法切换到该组", parent=dialog)
                return
            tasks = self.screenshot_engine.tasks
            if not tasks:
                messagebox.showinfo("提示", "该截图组没有任务", parent=dialog)
                return
            task_display_list = [f"{i + 1}. {task.get('display_name', f'任务{i + 1}')}" for i, task in enumerate(tasks)]
            selected_display = self._select_from_list("选择截图任务", task_display_list, parent=dialog)
            if not selected_display:
                return
            try:
                idx = int(selected_display.split('.')[0]) - 1
                if idx < 0 or idx >= len(tasks):
                    raise ValueError
            except:
                messagebox.showerror("错误", "无效选择", parent=dialog)
                return
            task = tasks[idx]
            task_name = task.get('display_name', f'任务{idx + 1}')
            settings = self._show_step_settings_dialog("截图任务", task_name, parent=dialog)
            if settings is not None:
                wait, auto = settings
                steps.append({
                    "type":          "screenshot_task",
                    "group_name":    group_name,
                    "task_index":    idx,
                    "display":       task_name,
                    "wait_seconds":  wait,
                    "auto_continue": auto
                })
                refresh_step_list()
        
        # 操作按钮
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill=tk.X, pady=5)
        
        ttk.Button(btn_frame, text="添加脚本", command=add_script_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="添加脚本组", command=add_script_group_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="添加截图组", command=add_screenshot_group_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="添加截图任务", command=add_screenshot_task_step).pack(side=tk.LEFT, padx=2)
        
        def remove_step():
            sel = step_listbox.curselection()
            if not sel:
                return
            idx = sel[0]
            del steps[idx]
            refresh_step_list()
        
        def move_up():
            sel = step_listbox.curselection()
            if not sel or sel[0] == 0:
                return
            i = sel[0]
            steps[i - 1], steps[i] = steps[i], steps[i - 1]
            refresh_step_list()
            step_listbox.selection_set(i - 1)
        
        def move_down():
            sel = step_listbox.curselection()
            if not sel or sel[0] >= len(steps) - 1:
                return
            i = sel[0]
            steps[i + 1], steps[i] = steps[i], steps[i + 1]
            refresh_step_list()
            step_listbox.selection_set(i + 1)
        
        ttk.Button(btn_frame, text="移除步骤", command=remove_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="上移", command=move_up).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="下移", command=move_down).pack(side=tk.LEFT, padx=2)
        
        def confirm():
            group_name = name_var.get().strip()
            if not group_name:
                messagebox.showerror("错误", "请输入组名")
                return
            if not steps:
                messagebox.showerror("错误", "请至少添加一个步骤")
                return
            notice = notice_text.get("1.0", tk.END).strip()
            self.mixed_group_manager.save_group(group_name, steps, notice)
            self.refresh_script_list()
            self.log_message(f"已创建混合组：{group_name}")
            dialog.destroy()
        
        ttk.Button(dialog, text="保存混合组", style="Primary.TButton", command=confirm).pack(pady=10)
    
    def edit_mixed_group(self):
        """编辑混合组"""
        selected = self.script_list.curselection()
        if not selected:
            messagebox.showwarning("提示", "请先选择一个混合组")
            return
        text = self.script_list.get(selected[0])
        if "[混合组]" not in text:
            messagebox.showwarning("提示", "请选择混合组名称")
            return
        group_name = text.replace("▶ [混合组]", "").replace("▼ [混合组]", "").strip()
        
        try:
            group = self.mixed_group_manager.load_group(group_name)
        except Exception as e:
            messagebox.showerror("错误", f"加载混合组失败：{str(e)}")
            return
        
        steps = group.get("steps", [])
        notice = group.get("notice", "")
        
        # 复用创建对话框的代码，但预填数据
        # 为了简化，我们复制创建对话框并进行修改，这里直接调用一个通用编辑器
        # 由于代码较长，我们采用类似创建对话框的方式，但预先填充步骤
        # 以下为一个简化版本，利用之前定义的方法，可以重构，但这里提供完整代码
        # 为了保持回答简洁，我们提供一个可复用的编辑逻辑，但为了完整性，我们重新实现一个编辑对话框
        # （因为用户要求代码块，我们提供完整编辑方法）
        # 但这里我们简单处理：删除原组，重新创建（不推荐，因为会丢失ID），但为了演示，我们采用编辑式对话框。
        # 建议先删除原有组，然后以原名称保存新内容。
        # 这里我们实现一个和创建几乎相同的对话框，只是预填充数据。
        
        dialog = tk.Toplevel(self)
        dialog.title("编辑混合组")
        dialog.geometry("700x600")
        dialog.transient(self)
        dialog.grab_set()
        self._set_window_icon(dialog)
        
        ttk.Label(dialog, text="混合组名称：").pack(pady=5)
        name_var = tk.StringVar(value=group_name)
        ttk.Entry(dialog, textvariable=name_var, width=40).pack(pady=5)
        
        ttk.Label(dialog, text="执行前提醒内容（组级别）：").pack(pady=5)
        notice_text = tk.Text(dialog, height=4, width=60)
        notice_text.insert(tk.END, notice)
        notice_text.pack(pady=5)
        
        step_frame = ttk.LabelFrame(dialog, text="步骤列表", padding=5)
        step_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        step_listbox = tk.Listbox(step_frame, selectmode=tk.SINGLE, font=('微软雅黑', 10))
        step_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(step_frame, orient=tk.VERTICAL, command=step_listbox.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        step_listbox.config(yscrollcommand=scroll.set)
        
        # 复制步骤列表（深拷贝）
        import copy
        current_steps = copy.deepcopy(steps)
        
        def refresh_step_list():
            step_listbox.delete(0, tk.END)
            for idx, step in enumerate(current_steps, 1):
                stype = step.get("type")
                wait = step.get("wait_seconds", 0.0)
                auto = step.get("auto_continue", True)
                auto_str = "自动" if auto else "手动"
                if stype == "script":
                    desc = f"脚本: {step.get('name')}"
                elif stype == "script_group":
                    desc = f"脚本组: {step.get('name')}"
                elif stype == "screenshot_group":
                    desc = f"截图组: {step.get('name')}"
                elif stype == "screenshot_task":
                    desc = f"截图任务: {step.get('display', '任务')} (组:{step.get('group_name')})"
                else:
                    desc = f"步骤{idx}: 未知"
                step_listbox.insert(tk.END, f"{idx}. {desc} | 等待{wait}s | {auto_str}")
        
        # 这里我们采用重建方式（不推荐，但可行）：
        def confirm_edit():
            new_name = name_var.get().strip()
            if not new_name:
                messagebox.showerror("错误", "请输入组名")
                return
            if not current_steps:
                messagebox.showerror("错误", "至少保留一个步骤")
                return
            new_notice = notice_text.get("1.0", tk.END).strip()
            # 先删除旧组（如果名称改变，删除旧名）
            if new_name != group_name:
                self.mixed_group_manager.delete_group(group_name)
            # 保存新组
            self.mixed_group_manager.save_group(new_name, current_steps, new_notice)
            self.refresh_script_list()
            self.log_message(f"已更新混合组：{new_name}")
            dialog.destroy()
        
        # 添加步骤按钮（复用创建逻辑，此处只提供框架）
        # 我们提供几个按钮调用内部添加函数，这些函数与创建时相同，可复制。
        # 为简洁，我们假设读者已理解，可以参照创建对话框实现。
        # 但我们仍提供必要的代码：
        
        # 获取可用资源
        scripts = self.script_manager.list_scripts()
        script_groups = self.script_group_manager.list_groups()
        screenshot_groups = self.screenshot_engine.list_groups()
        
        def add_script_step():
            if not scripts:
                messagebox.showinfo("提示", "没有可用的普通脚本", parent=dialog)
                return
            script_name = self._select_from_list("选择脚本", scripts, parent=dialog)
            if script_name:
                current_steps.append({"type": "script", "name": script_name})
                refresh_step_list()
        
        def add_script_group_step():
            if not script_groups:
                messagebox.showinfo("提示", "没有可用的脚本组", parent=dialog)
                return
            group_name = self._select_from_list("选择脚本组", script_groups, parent=dialog)
            if group_name:
                current_steps.append({"type": "script_group", "name": group_name})
                refresh_step_list()
        
        def add_screenshot_group_step():
            if not screenshot_groups:
                messagebox.showinfo("提示", "没有可用的截图组", parent=dialog)
                return
            group_name = self._select_from_list("选择截图组", screenshot_groups, parent=dialog)
            if group_name:
                current_steps.append({"type": "screenshot_group", "name": group_name})
                refresh_step_list()
        
        def add_screenshot_task_step():
            if not screenshot_groups:
                messagebox.showinfo("提示", "没有可用的截图组", parent=dialog)
                return
            group_name = self._select_from_list("选择截图组", screenshot_groups, parent=dialog)
            if not group_name:
                return
            if not self.screenshot_engine.set_active_group(group_name):
                messagebox.showerror("错误", "无法切换到该组", parent=dialog)
                return
            tasks = self.screenshot_engine.tasks
            if not tasks:
                messagebox.showinfo("提示", "该截图组没有任务", parent=dialog)
                return
            task_display_list = [f"{i + 1}. {task.get('display_name', f'任务{i + 1}')}" for i, task in enumerate(tasks)]
            selected_display = self._select_from_list("选择截图任务", task_display_list, parent=dialog)
            if not selected_display:
                return
            try:
                idx = int(selected_display.split('.')[0]) - 1
                if idx < 0 or idx >= len(tasks):
                    raise ValueError
            except:
                messagebox.showerror("错误", "无效选择", parent=dialog)
                return
            task = tasks[idx]
            current_steps.append({
                "type":       "screenshot_task",
                "group_name": group_name,
                "task_index": idx,
                "display":    task.get('display_name', f'任务{idx + 1}')
            })
            refresh_step_list()
        
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill=tk.X, pady=5)
        
        ttk.Button(btn_frame, text="添加脚本", command=add_script_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="添加脚本组", command=add_script_group_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="添加截图组", command=add_screenshot_group_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="添加截图任务", command=add_screenshot_task_step).pack(side=tk.LEFT, padx=2)
        
        def remove_step():
            sel = step_listbox.curselection()
            if not sel:
                return
            idx = sel[0]
            del current_steps[idx]
            refresh_step_list()
        
        def move_up():
            sel = step_listbox.curselection()
            if not sel or sel[0] == 0:
                return
            i = sel[0]
            current_steps[i - 1], current_steps[i] = current_steps[i], current_steps[i - 1]
            refresh_step_list()
            step_listbox.selection_set(i - 1)
        
        def move_down():
            sel = step_listbox.curselection()
            if not sel or sel[0] >= len(current_steps) - 1:
                return
            i = sel[0]
            current_steps[i + 1], current_steps[i] = current_steps[i], current_steps[i + 1]
            refresh_step_list()
            step_listbox.selection_set(i + 1)
        
        ttk.Button(btn_frame, text="移除步骤", command=remove_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="上移", command=move_up).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="下移", command=move_down).pack(side=tk.LEFT, padx=2)
        
        refresh_step_list()
        ttk.Button(dialog, text="保存修改", style="Primary.TButton", command=confirm_edit).pack(pady=10)
    
    def add_mixed_group_to_queue(self):
        """将选中的混合组加入队列"""
        selected = self.script_list.curselection()
        if not selected:
            messagebox.showwarning("提示", "请先选择一个混合组")
            return
        text = self.script_list.get(selected[0])
        if "[混合组]" not in text:
            messagebox.showwarning("提示", "请选择混合组名称")
            return
        group_name = text.replace("▶ [混合组]", "").replace("▼ [混合组]", "").strip()
        self.current_queue.append({
            "type": "mixed_group",
            "name": group_name
        })
        self.queue_list.insert(tk.END, f"[混合组] {group_name}")
        self.log_message(f"已添加混合组到队列：{group_name}")
    
    def delete_mixed_group(self):
        selected = self.script_list.curselection()
        if not selected:
            return
        text = self.script_list.get(selected[0])
        if "[混合组]" not in text:
            messagebox.showwarning("提示", "请先选择混合组")
            return
        group_name = text.replace("▶ [混合组]", "").replace("▼ [混合组]", "").strip()
        if messagebox.askyesno("确认删除", f"确定删除混合组：{group_name} 吗？"):
            self.mixed_group_manager.delete_group(group_name)
            self.expanded_groups.discard(group_name)
            self.refresh_script_list()
            self.log_message(f"已删除混合组：{group_name}")
    
    def _select_from_list(self, title, items, parent=None):
        """弹出一个列表选择对话框，返回用户选中的项，如果取消返回 None"""
        if not items:
            messagebox.showinfo("提示", "列表为空，没有可选项", parent=parent)
            return None
        if parent is None:
            parent = self
        
        dialog = tk.Toplevel(parent)
        dialog.title(title)
        dialog.geometry("400x300")
        dialog.transient(parent)
        dialog.grab_set()
        self._set_window_icon(dialog)
        
        # 列表
        listbox = tk.Listbox(dialog, selectmode=tk.SINGLE, font=('微软雅黑', 10))
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        for item in items:
            listbox.insert(tk.END, item)
        
        # 按钮
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=5)
        result = [None]  # 用列表保存结果，以便在嵌套函数中修改
        
        def on_confirm():
            sel = listbox.curselection()
            if sel:
                result[0] = listbox.get(sel[0])
            dialog.destroy()
        
        def on_cancel():
            dialog.destroy()
        
        # 双击选择
        listbox.bind("<Double-Button-1>", lambda e: on_confirm())
        
        ttk.Button(btn_frame, text="确定", style="Primary.TButton", command=on_confirm).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="取消", command=on_cancel).pack(side=tk.LEFT, padx=5)
        
        # 居中
        dialog.update_idletasks()
        w = dialog.winfo_width()
        h = dialog.winfo_height()
        x = (dialog.winfo_screenwidth() // 2) - (w // 2)
        y = (dialog.winfo_screenheight() // 2) - (h // 2)
        dialog.geometry(f"+{x}+{y}")
        
        parent.wait_window(dialog)
        return result[0]
    
    def add_screenshot_group_to_queue(self):
        """将当前选中的截图组加入执行队列"""
        group_name = self.screenshot_engine.active_group_name
        if not group_name:
            messagebox.showwarning("提示", "当前没有可用的截图组")
            return
        # 检查是否已存在相同截图组（可选，允许重复添加）
        self.current_queue.append({
            "type": "screenshot_group",
            "name": group_name
        })
        self.queue_list.insert(tk.END, f"[截图组] {group_name}")
        self.log_message(f"已添加截图组到队列：{group_name}")
    
    def add_single_screenshot_task_to_queue(self):
        """将当前选中的单个截图任务加入队列"""
        selected = self.screenshot_task_list.curselection()
        if not selected:
            messagebox.showwarning("提示", "请先选中一个截图任务")
            return
        index = selected[0]
        tasks = self.screenshot_engine.tasks
        if index < 0 or index >= len(tasks):
            messagebox.showerror("错误", "选中的任务无效")
            return
        task = tasks[index]
        # 获取当前组名
        group_name = self.screenshot_engine.active_group_name
        # 添加到队列
        self.current_queue.append({
            "type":       "screenshot_task",
            "group_name": group_name,
            "task_index": index,
            "display":    f"{task.get('display_name', f'任务{index + 1}')} (截图)"
        })
        self.queue_list.insert(tk.END, f"[截图任务] {task.get('display_name', f'任务{index + 1}')}")
        self.log_message(f"已添加单个截图任务到队列：{task.get('display_name', f'任务{index + 1}')}")
    
    def process_screenshot_task(self, item):
        """执行单个截图任务（队列项）"""
        group_name = item.get("group_name")
        task_index = item.get("task_index")
        if group_name is None or task_index is None:
            self.log_message("截图任务队列项缺少组名或索引")
            self.current_script_index += 1
            self.continue_or_finish()
            return
        
        self.log_message(f"执行单个截图任务：组 [{group_name}] 任务 {task_index + 1}")
        try:
            success = self.screenshot_engine.run_single_task(group_name, task_index)
            if success:
                self.log_message("截图任务执行完成")
            else:
                self.log_message("截图任务执行失败（可能未找到匹配）")
        except Exception as e:
            self.log_message(f"执行截图任务出错：{str(e)}")
        
        # 继续队列
        self.current_script_index += 1
        self.continue_or_finish()
    
    def add_screenshot_task(self):
        file_path = filedialog.askopenfilename(
            title="选择用户截图",
            filetypes=[("图片文件", "*.png;*.jpg;*.jpeg;*.bmp")]
        )
        if not file_path:
            return
        self.open_screenshot_task_editor(file_path=file_path)
    
    def edit_screenshot_task(self):
        selected = self.screenshot_task_list.curselection()
        if not selected:
            messagebox.showwarning("提示", "请先选择要编辑的截图任务")
            return
        index = selected[0]
        task = self.screenshot_engine.tasks[index]
        if task.get("type", "normal") == "branch":
            messagebox.showinfo("条件分支", "当前版本可通过删除后重新添加来修改分支候选截图。", parent=self)
            return
        self.open_screenshot_task_editor(
            file_path=task["image_path"],
            task_index=index,
            task=task
        )
    
    def open_screenshot_task_editor(self, file_path, task_index=None, task=None):
        try:
            original = Image.open(file_path).convert("RGB")
        except Exception as e:
            messagebox.showerror("错误", f"无法读取截图：{str(e)}")
            return
        
        dialog = tk.Toplevel(self)
        dialog.title("编辑截图操作" if task_index is not None else "添加截图操作")
        # 窗口高度按屏幕可用高度自适应，避免按钮被挤到屏幕外
        try:
            screen_h = dialog.winfo_screenheight()
            screen_w = dialog.winfo_screenwidth()
        except Exception:
            screen_h, screen_w = 1080, 1920
        win_w = min(940, max(760, screen_w - 80))
        win_h = min(980, max(700, screen_h - 120))
        dialog.geometry(f"{win_w}x{win_h}")
        dialog.minsize(min(720, win_w), min(620, win_h))
        dialog.transient(self)
        dialog.grab_set()
        
        try:
            self._set_window_icon(dialog)
        except Exception:
            pass
        
        # 底部按钮区先占位，窗口变小时也不会被内容挤掉
        btns = ttk.Frame(dialog)
        btns.pack(side=tk.BOTTOM, fill=tk.X, pady=8)
        
        ttk.Label(
            dialog,
            text="请点击截图预览选择基准点。红色十字为基准点，蓝色十字为加入 X/Y 偏移后的最终实际操作点；最终点可位于截图周边。",
            wraplength=760
        ).pack(pady=(10, 5))
        
        preview_frame = ttk.LabelFrame(dialog, text="截图内选点", padding=8)
        preview_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        max_w, max_h = 600, 260
        scale = min(max_w / original.width, max_h / original.height, 1.0)
        preview_size = (max(1, int(original.width * scale)), max(1, int(original.height * scale)))
        preview = original.resize(preview_size, Image.Resampling.LANCZOS)
        preview_photo = ImageTk.PhotoImage(preview)
        
        preview_margin = 90
        image_origin_x = preview_margin
        image_origin_y = preview_margin
        canvas_width = preview_size[0] + preview_margin * 2
        canvas_height = preview_size[1] + preview_margin * 2
        canvas = tk.Canvas(
            preview_frame,
            width=canvas_width,
            height=canvas_height,
            bg="#eeeeee",
            highlightthickness=1,
            highlightbackground="#888"
        )
        canvas.pack(expand=True)
        canvas.create_rectangle(
            image_origin_x - 1, image_origin_y - 1,
            image_origin_x + preview_size[0] + 1, image_origin_y + preview_size[1] + 1,
            outline="#666", width=1
        )
        canvas.create_image(image_origin_x, image_origin_y, anchor=tk.NW, image=preview_photo)
        canvas.image = preview_photo
        
        default_x = original.width // 2
        default_y = original.height // 2
        anchor_x_var = tk.IntVar(
            value=int((task or {}).get("anchor_x") if (task or {}).get("anchor_x") is not None else default_x))
        anchor_y_var = tk.IntVar(
            value=int((task or {}).get("anchor_y") if (task or {}).get("anchor_y") is not None else default_y))
        offset_x_var = tk.IntVar(value=int((task or {}).get("offset_x", 0)))
        offset_y_var = tk.IntVar(value=int((task or {}).get("offset_y", 0)))
        point_status_var = tk.StringVar()
        marker_ids = []
        
        def redraw_marker(*_args):
            for item_id in marker_ids:
                canvas.delete(item_id)
            marker_ids.clear()
            
            base_x = image_origin_x + anchor_x_var.get() * scale
            base_y = image_origin_y + anchor_y_var.get() * scale
            final_source_x = anchor_x_var.get() + offset_x_var.get()
            final_source_y = anchor_y_var.get() + offset_y_var.get()
            final_x = image_origin_x + final_source_x * scale
            final_y = image_origin_y + final_source_y * scale
            
            marker_ids.extend([
                canvas.create_line(base_x - 10, base_y, base_x + 10, base_y, fill="red", width=2),
                canvas.create_line(base_x, base_y - 10, base_x, base_y + 10, fill="red", width=2),
                canvas.create_oval(base_x - 4, base_y - 4, base_x + 4, base_y + 4, outline="red", width=2),
                canvas.create_line(base_x, base_y, final_x, final_y, fill="#2468d4", width=2, dash=(4, 3)),
                canvas.create_line(final_x - 12, final_y, final_x + 12, final_y, fill="#0057d9", width=3),
                canvas.create_line(final_x, final_y - 12, final_x, final_y + 12, fill="#0057d9", width=3),
                canvas.create_oval(final_x - 5, final_y - 5, final_x + 5, final_y + 5, outline="#0057d9", width=2),
                canvas.create_text(final_x + 8, final_y - 10, anchor="sw", text="最终操作点", fill="#0057d9")
            ])
            inside = 0 <= final_source_x < original.width and 0 <= final_source_y < original.height
            location_text = "截图内部" if inside else "截图外部/周边"
            point_status_var.set(
                f"截图尺寸：{original.width}×{original.height}；基准点：({anchor_x_var.get()}, {anchor_y_var.get()})；"
                f"最终点：({final_source_x}, {final_source_y})，位于{location_text}"
            )
        
        def choose_point(event):
            local_x = event.x - image_origin_x
            local_y = event.y - image_origin_y
            if not (0 <= local_x < preview_size[0] and 0 <= local_y < preview_size[1]):
                return
            x = max(0, min(original.width - 1, int(round(local_x / scale))))
            y = max(0, min(original.height - 1, int(round(local_y / scale))))
            anchor_x_var.set(x)
            anchor_y_var.set(y)
            redraw_marker()
        
        canvas.bind("<Button-1>", choose_point)
        redraw_marker()
        ttk.Label(preview_frame, textvariable=point_status_var).pack(pady=(5, 0))
        
        settings = ttk.LabelFrame(dialog, text="操作设置", padding=10)
        settings.pack(fill=tk.X, padx=10, pady=5)
        
        row1 = ttk.Frame(settings)
        row1.pack(fill=tk.X, pady=4)
        ttk.Label(row1, text="识别后操作：", width=12).pack(side=tk.LEFT)
        action_var = tk.StringVar(value=(task or {}).get("action", "左键单击"))
        action_combo = ttk.Combobox(
            row1,
            textvariable=action_var,
            state="readonly",
            width=18,
            values=SCREENSHOT_ACTIONS
        )
        action_combo.pack(side=tk.LEFT, padx=5)
        ttk.Label(row1, text="（鼠标操作或键盘操作）", foreground="gray").pack(side=tk.LEFT)

        row_scroll = ttk.Frame(settings)
        row_scroll.pack(fill=tk.X, pady=4)
        ttk.Label(row_scroll, text="滚动格数：", width=12).pack(side=tk.LEFT)
        scroll_notches_var = tk.IntVar(value=int((task or {}).get("scroll_notches", 5) or 5))
        ttk.Spinbox(row_scroll, from_=1, to=50, textvariable=scroll_notches_var,
                    width=10).pack(side=tk.LEFT, padx=5)
        ttk.Label(row_scroll, text="格（仅“滚轮上滚/下滚”时使用，1 格 ≈ 3 行文字，配合分支超时可做“滚动翻页”）",
                  foreground="gray").pack(side=tk.LEFT)
        
        row2 = ttk.Frame(settings)
        row2.pack(fill=tk.X, pady=4)
        ttk.Label(row2, text="X 微调：", width=12).pack(side=tk.LEFT)
        ttk.Spinbox(row2, from_=-5000, to=5000, textvariable=offset_x_var, width=10).pack(side=tk.LEFT, padx=5)
        ttk.Label(row2, text="Y 微调：").pack(side=tk.LEFT, padx=(18, 0))
        ttk.Spinbox(row2, from_=-5000, to=5000, textvariable=offset_y_var, width=10).pack(side=tk.LEFT, padx=5)
        offset_x_var.trace_add("write", redraw_marker)
        offset_y_var.trace_add("write", redraw_marker)
        
        row3 = ttk.Frame(settings)
        row3.pack(fill=tk.X, pady=4)
        threshold_var = tk.DoubleVar(value=float((task or {}).get("threshold", 0.75)))
        ttk.Label(row3, text="匹配阈值：", width=12).pack(side=tk.LEFT)
        ttk.Spinbox(row3, from_=0.01, to=1.0, increment=0.01, textvariable=threshold_var, width=10).pack(side=tk.LEFT,
                                                                                                         padx=5)
        ttk.Label(row3, text="建议 0.70～0.90，越高越严格").pack(side=tk.LEFT, padx=10)
        
        row4 = ttk.Frame(settings)
        row4.pack(fill=tk.X, pady=4)
        wait_seconds_var = tk.DoubleVar(value=float((task or {}).get("wait_seconds", 0.0)))
        repeat_count_var = tk.IntVar(value=int((task or {}).get("repeat_count", 1)))
        ttk.Label(row4, text="识别后等待：", width=12).pack(side=tk.LEFT)
        ttk.Spinbox(row4, from_=0, to=86400, increment=0.1, textvariable=wait_seconds_var, width=10).pack(side=tk.LEFT,
                                                                                                          padx=5)
        ttk.Label(row4, text="秒").pack(side=tk.LEFT)
        ttk.Label(row4, text="重复操作：").pack(side=tk.LEFT, padx=(22, 0))
        ttk.Spinbox(row4, from_=1, to=999999, textvariable=repeat_count_var, width=10).pack(side=tk.LEFT, padx=5)
        ttk.Label(row4, text="次").pack(side=tk.LEFT)
        
        # ---- 键盘操作设置 ----
        kb_frame = ttk.LabelFrame(dialog, text="键盘操作（识别成功后回放的键盘动作）", padding=8)
        kb_frame.pack(fill=tk.X, padx=10, pady=5)
        
        key_combo_var = tk.StringVar(value=str((task or {}).get("key_combo", "enter") or ""))
        key_text_var = tk.StringVar(value=str((task or {}).get("key_text", "") or ""))
        key_enter_var = tk.BooleanVar(value=bool((task or {}).get("key_enter_after", False)))
        
        kb_row1 = ttk.Frame(kb_frame)
        kb_row1.pack(fill=tk.X, pady=3)
        ttk.Label(kb_row1, text="按键组合：", width=12).pack(side=tk.LEFT)
        key_combo = ttk.Combobox(
            kb_row1, textvariable=key_combo_var, width=24, state="normal",
            values=[
                "enter", "tab", "esc", "space", "backspace", "delete",
                "up", "down", "left", "right", "f5",
                "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+s", "ctrl+z", "alt+f4",
                "ctrl+shift+esc", "tab, tab, enter",
            ]
        )
        key_combo.pack(side=tk.LEFT, padx=5)
        ttk.Label(kb_row1, text="用 + 连接组合键，多个动作用逗号分隔（如 tab, enter）",
                  foreground="gray").pack(side=tk.LEFT, padx=5)
        
        kb_row2 = ttk.Frame(kb_frame)
        kb_row2.pack(fill=tk.X, pady=3)
        ttk.Label(kb_row2, text="输入文本：", width=12).pack(side=tk.LEFT)
        ttk.Entry(kb_row2, textvariable=key_text_var, width=40).pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(kb_row2, text="输入后按回车", variable=key_enter_var).pack(side=tk.LEFT, padx=8)
        
        ttk.Label(kb_frame,
                  text="操作选择“键盘按键”时回放按键组合（支持 enter、tab、esc、方向键、F1~F12、ctrl/alt/shift/win 组合）；"
                       "选择“输入文本”时输入上面的文字（支持中文，≤300 字）。",
                  foreground="gray", wraplength=840, justify=tk.LEFT).pack(anchor=tk.W, pady=(2, 0))
        
        def confirm():
            try:
                threshold = float(threshold_var.get())
                if not 0.01 <= threshold <= 1.0:
                    raise ValueError("匹配阈值必须在 0.01～1.00 之间")
                wait_seconds = float(wait_seconds_var.get())
                repeat_count = int(repeat_count_var.get())
                if wait_seconds < 0:
                    raise ValueError("等待时间不能小于 0 秒")
                if repeat_count < 1:
                    raise ValueError("重复操作次数至少为 1 次")
                action = action_var.get()
                key_combo = key_combo_var.get().strip()
                key_text = key_text_var.get()
                if action == KEY_ACTION:
                    if not parse_key_sequence(key_combo):
                        raise ValueError(
                            "按键组合无法识别，请检查写法，例如：enter、ctrl+s、alt+f4、tab, enter")
                if action == TEXT_ACTION and not key_text:
                    raise ValueError("选择了“输入文本”，请输入要输入的文字")
                if len(key_text) > 300:
                    raise ValueError("输入文本过长（最多 300 字）")
                if task_index is None:
                    self.screenshot_engine.add_task(
                        image_path=file_path,
                        anchor_x=anchor_x_var.get(),
                        anchor_y=anchor_y_var.get(),
                        offset_x=offset_x_var.get(),
                        offset_y=offset_y_var.get(),
                        threshold=threshold,
                        action=action,
                        wait_seconds=wait_seconds,
                        repeat_count=repeat_count,
                        key_combo=key_combo,
                        key_text=key_text,
                        key_enter_after=bool(key_enter_var.get()),
                        scroll_notches=int(scroll_notches_var.get())
                    )
                    self.log_message(f"已添加截图任务：{os.path.basename(file_path)} / {action}")
                else:
                    self.screenshot_engine.update_task(
                        task_index,
                        anchor_x=anchor_x_var.get(),
                        anchor_y=anchor_y_var.get(),
                        offset_x=offset_x_var.get(),
                        offset_y=offset_y_var.get(),
                        threshold=threshold,
                        action=action,
                        wait_seconds=wait_seconds,
                        repeat_count=repeat_count,
                        key_combo=key_combo,
                        key_text=key_text,
                        key_enter_after=bool(key_enter_var.get()),
                        scroll_notches=int(scroll_notches_var.get())
                    )
                    self.log_message(f"已更新截图任务：{task.get('display_name', os.path.basename(file_path))}")
                self.refresh_screenshot_task_list()
                if task_index is not None:
                    self.screenshot_task_list.selection_set(task_index)
                dialog.destroy()
            except Exception as e:
                messagebox.showerror("设置错误", str(e), parent=dialog)
        
        ttk.Button(btns, text="保存设置", command=confirm).pack(side=tk.LEFT, padx=10, pady=4)
        ttk.Button(btns, text="取消", command=dialog.destroy).pack(side=tk.LEFT, padx=5, pady=4)
    
    def refresh_screenshot_group_controls(self):
        if not hasattr(self, "screenshot_group_combo"):
            return
        names = self.screenshot_engine.list_groups()
        self.screenshot_group_combo["values"] = names
        self.screenshot_group_var.set(self.screenshot_engine.active_group_name)
    
    def on_screenshot_group_changed(self, event=None):
        name = self.screenshot_group_var.get().strip()
        if self.screenshot_engine.set_active_group(name):
            self.refresh_screenshot_task_list()
            self.update_screenshot_mode_status(f"当前组：{name}")
    
    def add_screenshot_group(self):
        name = simpledialog.askstring("新增截图组", "请输入组名：", parent=self)
        if not name:
            return
        try:
            self.screenshot_engine.add_group(name)
            self.refresh_screenshot_group_controls()
            self.refresh_screenshot_task_list()
            self.log_message(f"已新增截图组：{name}")
        except Exception as e:
            messagebox.showerror("新增失败", str(e), parent=self)
    
    def rename_screenshot_group(self):
        old = self.screenshot_engine.active_group_name
        name = simpledialog.askstring("重命名截图组", "请输入新组名：", initialvalue=old, parent=self)
        if not name or name == old:
            return
        try:
            self.screenshot_engine.rename_group(old, name)
            self.refresh_screenshot_group_controls()
            self.log_message(f"截图组已重命名：{old} → {name}")
        except Exception as e:
            messagebox.showerror("重命名失败", str(e), parent=self)
    
    def duplicate_screenshot_group(self):
        source = self.screenshot_engine.active_group_name
        name = simpledialog.askstring("复制截图组", "请输入新组名：", initialvalue=f"{source}-副本", parent=self)
        if not name:
            return
        try:
            self.screenshot_engine.duplicate_group(source, name)
            self.refresh_screenshot_group_controls()
            self.refresh_screenshot_task_list()
            self.log_message(f"已复制截图组：{source} → {name}")
        except Exception as e:
            messagebox.showerror("复制失败", str(e), parent=self)
    
    def delete_screenshot_group(self):
        name = self.screenshot_engine.active_group_name
        if not messagebox.askyesno("删除截图组", f"确定删除截图组“{name}”及其中全部任务吗？", parent=self):
            return
        try:
            self.screenshot_engine.remove_group(name)
            self.refresh_screenshot_group_controls()
            self.refresh_screenshot_task_list()
            self.log_message(f"已删除截图组：{name}")
        except Exception as e:
            messagebox.showerror("删除失败", str(e), parent=self)
    
    def add_screenshot_branch_task(self):
        """添加条件分支（增强版：可逐个设置每张截图的锚点与偏移，并支持超时跳过）"""
        dialog = tk.Toplevel(self)
        dialog.title("添加条件分支 - 编辑候选截图")
        try:
            screen_h = dialog.winfo_screenheight()
            screen_w = dialog.winfo_screenwidth()
        except Exception:
            screen_h, screen_w = 1080, 1920
        dialog.geometry(f"{min(1200, max(800, screen_w - 80))}x{min(750, max(600, screen_h - 140))}")
        dialog.minsize(800, 560)
        dialog.transient(self)
        dialog.grab_set()
        try:
            self._set_window_icon(dialog)
        except:
            pass
        
        # 底部按钮先占位，保证按钮始终可见
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=8)
        
        branch_items = []
        
        # 全局参数
        global_threshold = tk.DoubleVar(value=0.75)
        global_action = tk.StringVar(value="左键单击")
        global_wait = tk.DoubleVar(value=0.0)
        global_timeout = tk.DoubleVar(value=0.0)  # 新增：分支超时
        
        # ---- 左侧：列表 ----
        left_frame = ttk.LabelFrame(dialog, text="候选截图列表", padding=5)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        listbox = tk.Listbox(left_frame, selectmode=tk.SINGLE, font=('微软雅黑', 10))
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        list_scroll = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=listbox.yview)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        listbox.config(yscrollcommand=list_scroll.set)
        
        list_btn_frame = ttk.Frame(left_frame)
        list_btn_frame.pack(fill=tk.X, pady=5)
        
        def refresh_list():
            listbox.delete(0, tk.END)
            for idx, item in enumerate(branch_items, start=1):
                name = os.path.basename(item.get('image_path', ''))
                anchor = (item.get('anchor_x', '?'), item.get('anchor_y', '?'))
                offset = (item.get('offset_x', 0), item.get('offset_y', 0))
                listbox.insert(tk.END, f"{idx}. {name}  基准{anchor}  偏移{offset}")
        
        def add_branch_item():
            file_path = filedialog.askopenfilename(
                title="选择候选截图",
                filetypes=[("图片文件", "*.png;*.jpg;*.jpeg;*.bmp")]
            )
            if not file_path:
                return
            settings = self._open_single_branch_editor(file_path)
            if settings is None:
                return
            branch_items.append(settings)
            refresh_list()
        
        def remove_branch_item():
            sel = listbox.curselection()
            if not sel:
                return
            index = sel[0]
            del branch_items[index]
            refresh_list()
        
        def edit_branch_item():
            sel = listbox.curselection()
            if not sel:
                return
            index = sel[0]
            current = branch_items[index]
            new_settings = self._open_single_branch_editor(current['image_path'], current)
            if new_settings is not None:
                branch_items[index] = new_settings
                refresh_list()
        
        def move_up():
            sel = listbox.curselection()
            if not sel or sel[0] == 0:
                return
            i = sel[0]
            branch_items[i - 1], branch_items[i] = branch_items[i], branch_items[i - 1]
            refresh_list()
            listbox.selection_set(i - 1)
        
        def move_down():
            sel = listbox.curselection()
            if not sel or sel[0] >= len(branch_items) - 1:
                return
            i = sel[0]
            branch_items[i + 1], branch_items[i] = branch_items[i], branch_items[i + 1]
            refresh_list()
            listbox.selection_set(i + 1)
        
        ttk.Button(list_btn_frame, text="添加截图", command=add_branch_item).pack(side=tk.LEFT, padx=2)
        ttk.Button(list_btn_frame, text="编辑", command=edit_branch_item).pack(side=tk.LEFT, padx=2)
        ttk.Button(list_btn_frame, text="删除", command=remove_branch_item).pack(side=tk.LEFT, padx=2)
        ttk.Button(list_btn_frame, text="上移", command=move_up).pack(side=tk.LEFT, padx=2)
        ttk.Button(list_btn_frame, text="下移", command=move_down).pack(side=tk.LEFT, padx=2)
        
        # ---- 右侧：全局设置 ----
        right_frame = ttk.LabelFrame(dialog, text="全局设置（应用于所有分支）", padding=10)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        ttk.Label(right_frame, text="匹配阈值：").grid(row=0, column=0, sticky='e', pady=5)
        ttk.Spinbox(right_frame, from_=0.01, to=1.0, increment=0.01,
                    textvariable=global_threshold, width=12).grid(row=0, column=1, sticky='w')
        
        ttk.Label(right_frame, text="识别后等待(秒)：").grid(row=1, column=0, sticky='e', pady=5)
        ttk.Spinbox(right_frame, from_=0, to=86400, increment=0.1,
                    textvariable=global_wait, width=12).grid(row=1, column=1, sticky='w')
        
        ttk.Label(right_frame, text="识别后操作：").grid(row=2, column=0, sticky='e', pady=5)
        ttk.Combobox(right_frame, textvariable=global_action, state='readonly', width=16,
                     values=SCREENSHOT_ACTIONS).grid(row=2, column=1, sticky='w')
        
        # 键盘操作（全局）
        global_key_combo = tk.StringVar(value="enter")
        global_key_text = tk.StringVar(value="")
        global_key_enter = tk.BooleanVar(value=False)
        
        ttk.Label(right_frame, text="按键组合：").grid(row=3, column=0, sticky='e', pady=5)
        ttk.Combobox(right_frame, textvariable=global_key_combo, width=20, state='normal',
                     values=["enter", "tab", "esc", "space", "backspace", "delete",
                             "up", "down", "left", "right", "f5",
                             "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+s", "ctrl+z", "alt+f4",
                             "tab, tab, enter"]).grid(row=3, column=1, sticky='w')
        
        ttk.Label(right_frame, text="输入文本：").grid(row=4, column=0, sticky='e', pady=5)
        ttk.Entry(right_frame, textvariable=global_key_text, width=26).grid(row=4, column=1, sticky='w', pady=5)
        
        ttk.Checkbutton(right_frame, text="输入后按回车", variable=global_key_enter).grid(
            row=5, column=0, columnspan=2, sticky='w', pady=(0, 5))
        
        ttk.Label(right_frame, text="分支等待超时(秒)：").grid(row=6, column=0, sticky='e', pady=5)
        ttk.Spinbox(right_frame, from_=0, to=86400, increment=0.5,
                    textvariable=global_timeout, width=12).grid(row=6, column=1, sticky='w')

        global_scroll_notches = tk.IntVar(value=5)
        ttk.Label(right_frame, text="滚动格数：").grid(row=7, column=0, sticky='e', pady=5)
        ttk.Spinbox(right_frame, from_=1, to=50, textvariable=global_scroll_notches,
                    width=12).grid(row=7, column=1, sticky='w')

        ttk.Label(right_frame,
                  text="操作选择“键盘按键”时回放按键组合；选择“输入文本”时输入上面的文字；\n"
                       "“滚轮上滚/下滚”按上面的格数滚动（滚轮事件作用于鼠标所在位置，会先移到屏幕中央）。\n"
                       "0 = 无限等待直到某个分支出现；\n大于0 = 超时后自动跳过该分支节点，继续主线下一个任务。",
                  foreground="gray", justify=tk.LEFT).grid(row=8, column=0, columnspan=2, pady=8, sticky='w')

        ttk.Label(right_frame, text="提示：双击列表项可编辑该分支的锚点与偏移",
                  foreground="gray").grid(row=9, column=0, columnspan=2, pady=8)
        
        listbox.bind("<Double-Button-1>", lambda e: edit_branch_item())
        
        # ---- 底部按钮（btn_frame 已提前创建并占位） ----
        def confirm():
            if not branch_items:
                messagebox.showerror("错误", "请至少添加一个候选截图", parent=dialog)
                return
            action = global_action.get()
            key_combo = global_key_combo.get().strip()
            key_text = global_key_text.get()
            if action == KEY_ACTION and not parse_key_sequence(key_combo):
                messagebox.showerror("错误", "按键组合无法识别，请检查写法，例如：enter、ctrl+s、tab, enter",
                                     parent=dialog)
                return
            if action == TEXT_ACTION and not key_text:
                messagebox.showerror("错误", "选择了“输入文本”，请输入要输入的文字", parent=dialog)
                return
            try:
                self.screenshot_engine.add_branch_task_detailed(
                    branches_details=branch_items,
                    threshold=global_threshold.get(),
                    action=action,
                    wait_seconds=global_wait.get(),
                    branch_timeout=global_timeout.get(),
                    key_combo=key_combo,
                    key_text=key_text,
                    key_enter_after=bool(global_key_enter.get()),
                    scroll_notches=int(global_scroll_notches.get())
                )
                self.refresh_screenshot_task_list()
                timeout_text = f"{global_timeout.get():.1f}s" if global_timeout.get() > 0 else "无限等待"
                self.log_message(
                    f"已添加条件分支：{len(branch_items)} 个候选截图，超时设置：{timeout_text}")
                dialog.destroy()
            except Exception as e:
                messagebox.showerror("添加失败", str(e), parent=dialog)
        
        ttk.Button(btn_frame, text="保存分支", style="Primary.TButton", command=confirm).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="取消", command=dialog.destroy).pack(side=tk.LEFT, padx=5)
        
        refresh_list()
    
    def _open_single_branch_editor(self, file_path, current=None):
        """
        为单个分支截图打开设置编辑器（仅锚点、偏移），返回字典或None（取消）
        """
        try:
            original = Image.open(file_path).convert("RGB")
        except Exception as e:
            messagebox.showerror("错误", f"无法读取截图：{str(e)}")
            return None
        
        # 当前设置
        cur_anchor_x = current.get('anchor_x') if current else None
        cur_anchor_y = current.get('anchor_y') if current else None
        cur_offset_x = current.get('offset_x', 0) if current else 0
        cur_offset_y = current.get('offset_y', 0) if current else 0
        
        # 创建临时对话框
        dialog = tk.Toplevel(self)
        dialog.title("设置分支截图锚点与偏移")
        try:
            screen_h = dialog.winfo_screenheight()
        except Exception:
            screen_h = 1080
        dialog.geometry(f"860x{min(700, max(560, screen_h - 160))}")
        dialog.minsize(700, 520)
        dialog.transient(self)
        dialog.grab_set()
        try:
            self._set_window_icon(dialog)
        except:
            pass
        
        # 底部按钮先占位，避免窗口偏小时按钮被裁掉
        btn_f = ttk.Frame(dialog)
        btn_f.pack(side=tk.BOTTOM, fill=tk.X, pady=8)
        
        ttk.Label(dialog, text="点击截图预览选择基准点（红色十字），蓝色十字为最终操作点（含偏移）。").pack(pady=(10, 5))
        
        preview_frame = ttk.LabelFrame(dialog, text="截图预览", padding=8)
        preview_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        max_w, max_h = 580, 320
        scale = min(max_w / original.width, max_h / original.height, 1.0)
        preview_size = (max(1, int(original.width * scale)), max(1, int(original.height * scale)))
        preview = original.resize(preview_size, Image.Resampling.LANCZOS)
        preview_photo = ImageTk.PhotoImage(preview)
        
        margin = 80
        canvas_w = preview_size[0] + margin * 2
        canvas_h = preview_size[1] + margin * 2
        canvas = tk.Canvas(preview_frame, width=canvas_w, height=canvas_h, bg="#eeeeee")
        canvas.pack(expand=True)
        canvas.create_image(margin, margin, anchor=tk.NW, image=preview_photo)
        canvas.image = preview_photo
        
        # 变量
        default_x = original.width // 2
        default_y = original.height // 2
        anchor_x_var = tk.IntVar(value=cur_anchor_x if cur_anchor_x is not None else default_x)
        anchor_y_var = tk.IntVar(value=cur_anchor_y if cur_anchor_y is not None else default_y)
        offset_x_var = tk.IntVar(value=cur_offset_x)
        offset_y_var = tk.IntVar(value=cur_offset_y)
        point_status_var = tk.StringVar()
        marker_ids = []
        
        def redraw_marker(*args):
            for item in marker_ids:
                canvas.delete(item)
            marker_ids.clear()
            base_x = margin + anchor_x_var.get() * scale
            base_y = margin + anchor_y_var.get() * scale
            final_x = margin + (anchor_x_var.get() + offset_x_var.get()) * scale
            final_y = margin + (anchor_y_var.get() + offset_y_var.get()) * scale
            marker_ids.extend([
                canvas.create_line(base_x - 10, base_y, base_x + 10, base_y, fill="red", width=2),
                canvas.create_line(base_x, base_y - 10, base_x, base_y + 10, fill="red", width=2),
                canvas.create_oval(base_x - 4, base_y - 4, base_x + 4, base_y + 4, outline="red", width=2),
                canvas.create_line(base_x, base_y, final_x, final_y, fill="#2468d4", width=2, dash=(4, 3)),
                canvas.create_line(final_x - 12, final_y, final_x + 12, final_y, fill="#0057d9", width=3),
                canvas.create_line(final_x, final_y - 12, final_x, final_y + 12, fill="#0057d9", width=3),
                canvas.create_oval(final_x - 5, final_y - 5, final_x + 5, final_y + 5, outline="#0057d9", width=2),
                canvas.create_text(final_x + 8, final_y - 10, anchor="sw", text="最终操作点", fill="#0057d9")
            ])
            point_status_var.set(
                f"截图尺寸：{original.width}×{original.height}  基准点：({anchor_x_var.get()}, {anchor_y_var.get()})  偏移：({offset_x_var.get()}, {offset_y_var.get()}) → 最终点：({anchor_x_var.get() + offset_x_var.get()}, {anchor_y_var.get() + offset_y_var.get()})"
            )
        
        def choose_point(event):
            x = event.x - margin
            y = event.y - margin
            if 0 <= x < preview_size[0] and 0 <= y < preview_size[1]:
                x_px = max(0, min(original.width - 1, int(round(x / scale))))
                y_px = max(0, min(original.height - 1, int(round(y / scale))))
                anchor_x_var.set(x_px)
                anchor_y_var.set(y_px)
                redraw_marker()
        
        canvas.bind("<Button-1>", choose_point)
        redraw_marker()
        ttk.Label(preview_frame, textvariable=point_status_var).pack(pady=(5, 0))
        
        # 控制区
        control_frame = ttk.Frame(dialog)
        control_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(control_frame, text="X偏移：").grid(row=0, column=0, sticky='e', padx=5)
        ttk.Spinbox(control_frame, from_=-5000, to=5000, textvariable=offset_x_var, width=10).grid(row=0, column=1,
                                                                                                   padx=5)
        ttk.Label(control_frame, text="Y偏移：").grid(row=0, column=2, sticky='e', padx=5)
        ttk.Spinbox(control_frame, from_=-5000, to=5000, textvariable=offset_y_var, width=10).grid(row=0, column=3,
                                                                                                   padx=5)
        
        # 监视偏移变化重绘
        offset_x_var.trace_add("write", redraw_marker)
        offset_y_var.trace_add("write", redraw_marker)
        
        result = None
        
        def ok():
            nonlocal result
            result = {
                'image_path': file_path,
                'anchor_x':   anchor_x_var.get(),
                'anchor_y':   anchor_y_var.get(),
                'offset_x':   offset_x_var.get(),
                'offset_y':   offset_y_var.get()
            }
            dialog.destroy()
        
        def cancel():
            dialog.destroy()
        
        ttk.Button(btn_f, text="确定", style="Primary.TButton", command=ok).pack(side=tk.LEFT, padx=10, pady=4)
        ttk.Button(btn_f, text="取消", command=cancel).pack(side=tk.LEFT, padx=5, pady=4)
        
        self.wait_window(dialog)
        return result
    
    def refresh_screenshot_task_list(self):
        if not hasattr(self, "screenshot_task_list"):
            return
        
        self.screenshot_task_list.delete(0, tk.END)
        
        for i, task in enumerate(self.screenshot_engine.tasks):
            if task.get("type", "normal") == "branch":
                branches = task.get("branches", [])
                names = " / ".join(b.get("display_name", "截图") for b in branches)
                timeout = float(task.get("branch_timeout", 0.0))
                timeout_text = f"超时{timeout:.1f}s跳过" if timeout > 0 else "无限等待"
                action_text = self._describe_action(branches[0] if branches else {})
                self.screenshot_task_list.insert(
                    tk.END,
                    f"{i + 1}. 【条件分支】命中任一即操作（{action_text}）并进入下一步：{names}（{timeout_text}）"
                )
                continue
            name = task.get("display_name") or os.path.basename(task["image_path"])
            ox = task.get("offset_x", 0);
            oy = task.get("offset_y", 0)
            ax = task.get("anchor_x");
            ay = task.get("anchor_y")
            threshold = task.get("threshold", 0.75)
            action_text = self._describe_action(task)
            wait_seconds = float(task.get("wait_seconds", 0.0))
            repeat_count = int(task.get("repeat_count", 1))
            point_text = "中心" if ax is None or ay is None else f"({ax},{ay})"
            final_text = "中心" if ax is None or ay is None else f"({ax + ox},{ay + oy})"
            self.screenshot_task_list.insert(
                tk.END,
                f"{i + 1}. {name} | {action_text}×{repeat_count}（每次重新识别） | 等待 {wait_seconds:g}s | "
                f"基准 {point_text} → 最终 {final_text} | 阈值 {threshold:.2f}"
            )
    
    def _describe_action(self, task):
        """把任务的操作配置转成一行可读文字（用于列表显示）。"""
        action = task.get("action", "左键单击")
        if action == KEY_ACTION:
            combo = task.get("key_combo", "") or "未设置"
            return f"键盘[{combo}]"
        if action == TEXT_ACTION:
            text = str(task.get("key_text", ""))
            preview = text if len(text) <= 12 else text[:12] + "…"
            suffix = "+回车" if task.get("key_enter_after") else ""
            return f"文本[{preview}]{suffix}"
        if action in SCROLL_ACTIONS:
            return f"{action}×{task.get('scroll_notches', 5)}格"
        return action
    
    def edit_selected_screenshot_task(self, event=None):
        """双击列表项编辑截图任务"""
        selected = self.screenshot_task_list.curselection()
        if selected:
            self.edit_screenshot_task()
    
    def remove_screenshot_task(self):
        selected = self.screenshot_task_list.curselection()
        
        if not selected:
            messagebox.showwarning("提示", "请先选择要删除的截图任务")
            return
        
        index = selected[0]
        self.screenshot_engine.remove_task(index)
        self.refresh_screenshot_task_list()
    
    def move_screenshot_task_up(self):
        selected = self.screenshot_task_list.curselection()
        
        if not selected:
            return
        
        index = selected[0]
        self.screenshot_engine.move_up(index)
        self.refresh_screenshot_task_list()
        
        if index > 0:
            self.screenshot_task_list.selection_set(index - 1)
    
    def move_screenshot_task_down(self):
        selected = self.screenshot_task_list.curselection()
        
        if not selected:
            return
        
        index = selected[0]
        self.screenshot_engine.move_down(index)
        self.refresh_screenshot_task_list()
        
        if index < len(self.screenshot_engine.tasks) - 1:
            self.screenshot_task_list.selection_set(index + 1)
    
    def clear_screenshot_tasks(self):
        if messagebox.askyesno("确认", "确定清空所有截图任务吗？"):
            self.screenshot_engine.clear()
            self.refresh_screenshot_task_list()
            self.log_message("已清空截图任务")
    
    def start_screenshot_click_mode(self):
        try:
            times = max(1, min(9999, int(self.screenshot_loop_var.get())))
        except Exception:
            times = 1
        self.app_settings["screenshot_loop_times"] = times
        try:
            save_app_settings(self.app_settings)
        except Exception:
            pass
        self.screenshot_engine.start(times=times)
    
    def stop_screenshot_click_mode(self):
        self.screenshot_engine.stop()
    
    def update_screenshot_mode_status(self, text):
        if hasattr(self, "screenshot_mode_status_var"):
            self.screenshot_mode_status_var.set(text)
    
    def _show_step_settings_dialog(self, step_type, step_name, parent=None):
        """弹出步骤设置对话框，返回 (wait_seconds, auto_continue)，如果取消返回 None"""
        if parent is None:
            parent = self
        dialog = tk.Toplevel(parent)
        dialog.title("步骤设置")
        dialog.geometry("350x200")
        dialog.transient(parent)
        dialog.grab_set()
        self._set_window_icon(dialog)
        
        ttk.Label(dialog, text=f"步骤类型: {step_type}").pack(pady=5)
        ttk.Label(dialog, text=f"名称: {step_name}").pack(pady=5)
        
        # 等待时间
        wait_frame = ttk.Frame(dialog)
        wait_frame.pack(pady=5)
        ttk.Label(wait_frame, text="执行后等待(秒):").pack(side=tk.LEFT, padx=5)
        wait_var = tk.DoubleVar(value=0.0)
        ttk.Spinbox(wait_frame, from_=0, to=86400, increment=0.1, textvariable=wait_var, width=10).pack(side=tk.LEFT)
        
        # 自动继续
        auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(dialog, text="执行完后自动继续（不暂停）", variable=auto_var).pack(pady=5)
        
        result = [None]
        
        def on_ok():
            result[0] = (wait_var.get(), auto_var.get())
            dialog.destroy()
        
        def on_cancel():
            dialog.destroy()
        
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="确定", style="Primary.TButton", command=on_ok).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="取消", command=on_cancel).pack(side=tk.LEFT, padx=5)
        
        parent.wait_window(dialog)
        return result[0]
    
    def update_total_progress(self, increment=1):
        """累加已处理事件并刷新总进度（线程安全，自动夹紧 0-100）"""
        lock = getattr(self, "_progress_lock", None)
        if lock is None:
            lock = self._progress_lock = threading.Lock()
        with lock:
            self.processed_events += increment
            total = max(0, int(getattr(self, "total_events", 0) or 0))
            done = max(0, int(self.processed_events))
            pct = (done / total * 100.0) if total else 0.0
        detail = f"({min(done, total)}/{total})" if total else ""
        self.update_progress(pct, detail)
    
    def create_script_group_dialog(self):
        scripts = self.script_manager.list_scripts()
        if not scripts:
            messagebox.showwarning("提示", "请先录制至少一个脚本")
            return
        
        dialog = tk.Toplevel(self)
        dialog.title("创建脚本组")
        dialog.geometry("520x560")
        dialog.transient(self)
        dialog.grab_set()
        self._set_window_icon(dialog)
        
        ttk.Label(dialog, text="脚本组名称：").pack(pady=5)
        name_var = tk.StringVar(value="新脚本组")
        ttk.Entry(dialog, textvariable=name_var, width=40).pack(pady=5)
        
        ttk.Label(dialog, text="执行前提醒内容（组级别）：").pack(pady=5)
        notice_text = tk.Text(dialog, height=6, width=55)
        notice_text.insert(tk.END, "执行本脚本组前，请确认目标软件已打开、窗口位置正确。")
        notice_text.pack(pady=5)
        
        body = ttk.Frame(dialog)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        left = ttk.LabelFrame(body, text="已有脚本（双击添加）")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        right = ttk.LabelFrame(body, text="脚本组内脚本（双击修改提醒）")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        available_list = tk.Listbox(left)
        available_list.pack(fill=tk.BOTH, expand=True)
        
        group_list = tk.Listbox(right)
        group_list.pack(fill=tk.BOTH, expand=True)
        
        for s in scripts:
            available_list.insert(tk.END, s)
        
        selected_scripts = []  # 存储字典 {name, notice}
        
        # ----- 添加脚本（支持重复）-----
        def add_script(script_name=None):
            if script_name is None:
                sel = available_list.curselection()
                if not sel:
                    return
                script_name = available_list.get(sel[0])
            # 弹出提醒输入
            notice = simpledialog.askstring(
                "脚本前置提醒",
                f"请输入执行脚本 '{script_name}' 的提醒内容（可为空）：",
                parent=dialog
            )
            if notice is None:
                return
            selected_scripts.append({"name": script_name, "notice": notice})
            refresh_group_list()
        
        # 双击可用脚本直接添加（更方便）
        def on_available_double_click(event):
            add_script()
        
        available_list.bind("<Double-Button-1>", on_available_double_click)
        
        def remove_script():
            sel = group_list.curselection()
            if not sel:
                return
            index = sel[0]
            selected_scripts.pop(index)
            refresh_group_list()
        
        def move_up():
            sel = group_list.curselection()
            if not sel or sel[0] == 0:
                return
            i = sel[0]
            selected_scripts[i - 1], selected_scripts[i] = selected_scripts[i], selected_scripts[i - 1]
            refresh_group_list(i - 1)
        
        def move_down():
            sel = group_list.curselection()
            if not sel or sel[0] >= len(selected_scripts) - 1:
                return
            i = sel[0]
            selected_scripts[i + 1], selected_scripts[i] = selected_scripts[i], selected_scripts[i + 1]
            refresh_group_list(i + 1)
        
        def refresh_group_list(select_index=None):
            group_list.delete(0, tk.END)
            for idx, item in enumerate(selected_scripts, start=1):
                name = item.get("name", "")
                notice = item.get("notice", "")
                preview = notice[:20] + ("..." if len(notice) > 20 else "")
                display = f"{idx}. {name} (提醒: {preview})"
                group_list.insert(tk.END, display)
            if select_index is not None:
                group_list.select_set(select_index)
        
        # 双击修改提醒
        def edit_notice(event):
            sel = group_list.curselection()
            if not sel:
                return
            index = sel[0]
            item = selected_scripts[index]
            new_notice = simpledialog.askstring(
                "修改提醒",
                f"请输入脚本 '{item['name']}' 的新提醒内容：",
                parent=dialog,
                initialvalue=item.get("notice", "")
            )
            if new_notice is not None:
                item["notice"] = new_notice
                refresh_group_list(index)
        
        group_list.bind("<Double-Button-1>", edit_notice)
        
        btns = ttk.Frame(dialog)
        btns.pack(pady=5)
        
        ttk.Button(btns, text="添加 →", command=add_script).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="移除 ←", command=remove_script).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="上移", command=move_up).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="下移", command=move_down).pack(side=tk.LEFT, padx=3)
        
        def confirm():
            group_name = name_var.get().strip()
            notice = notice_text.get("1.0", tk.END).strip()
            if not group_name:
                messagebox.showerror("错误", "请输入脚本组名称")
                return
            if not selected_scripts:
                messagebox.showerror("错误", "请至少添加一个脚本")
                return
            self.script_group_manager.save_group(group_name, selected_scripts, notice)
            self.refresh_script_list()
            self.log_message(f"已创建脚本组：{group_name}")
            dialog.destroy()
        
        ttk.Button(dialog, text="保存脚本组", style="Primary.TButton", command=confirm).pack(pady=10)
    
    def edit_selected_script_group(self):
        selected = self.script_list.curselection()
        if not selected:
            messagebox.showwarning("提示", "请先选择一个脚本组")
            return
        text = self.script_list.get(selected[0])
        if "[脚本组]" not in text:
            messagebox.showwarning("提示", "请选择脚本组名称")
            return
        group_name = text.replace("▶ [脚本组]", "").replace("▼ [脚本组]", "").strip()
        
        try:
            group = self.script_group_manager.load_group(group_name)
        except Exception as e:
            messagebox.showerror("错误", f"加载脚本组失败：{str(e)}")
            return
        
        scripts = group.get("scripts", [])  # 列表字典
        notice = group.get("notice", "")
        
        dialog = tk.Toplevel(self)
        dialog.title("编辑脚本组")
        dialog.geometry("520x560")
        dialog.transient(self)
        dialog.grab_set()
        self._set_window_icon(dialog)
        
        ttk.Label(dialog, text="脚本组名称：").pack(pady=5)
        name_var = tk.StringVar(value=group_name)
        ttk.Entry(dialog, textvariable=name_var, width=40).pack(pady=5)
        
        ttk.Label(dialog, text="执行前提醒内容（组级别）：").pack(pady=5)
        notice_text = tk.Text(dialog, height=6, width=55)
        notice_text.insert(tk.END, notice)
        notice_text.pack(pady=5)
        
        body = ttk.Frame(dialog)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        left = ttk.LabelFrame(body, text="所有脚本（双击添加）")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        right = ttk.LabelFrame(body, text="组内脚本（双击修改提醒）")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        available_list = tk.Listbox(left)
        available_list.pack(fill=tk.BOTH, expand=True)
        
        group_list = tk.Listbox(right)
        group_list.pack(fill=tk.BOTH, expand=True)
        
        all_scripts = self.script_manager.list_scripts()
        for s in all_scripts:
            available_list.insert(tk.END, s)
        
        selected_scripts = scripts.copy()  # 列表字典
        
        # ----- 添加脚本（支持重复）-----
        def add_script(script_name=None):
            if script_name is None:
                sel = available_list.curselection()
                if not sel:
                    return
                script_name = available_list.get(sel[0])
            # 弹出提醒输入
            notice_txt = simpledialog.askstring(
                "脚本前置提醒",
                f"请输入执行脚本 '{script_name}' 的提醒内容（可为空）：",
                parent=dialog
            )
            if notice_txt is None:
                return
            selected_scripts.append({"name": script_name, "notice": notice_txt})
            refresh_group_list()
        
        def on_available_double_click(event):
            add_script()
        
        available_list.bind("<Double-Button-1>", on_available_double_click)
        
        def remove_script():
            sel = group_list.curselection()
            if not sel:
                return
            index = sel[0]
            selected_scripts.pop(index)
            refresh_group_list()
        
        def move_up():
            sel = group_list.curselection()
            if not sel or sel[0] == 0:
                return
            i = sel[0]
            selected_scripts[i - 1], selected_scripts[i] = selected_scripts[i], selected_scripts[i - 1]
            refresh_group_list(i - 1)
        
        def move_down():
            sel = group_list.curselection()
            if not sel or sel[0] >= len(selected_scripts) - 1:
                return
            i = sel[0]
            selected_scripts[i + 1], selected_scripts[i] = selected_scripts[i], selected_scripts[i + 1]
            refresh_group_list(i + 1)
        
        def refresh_group_list(select_index=None):
            group_list.delete(0, tk.END)
            for idx, item in enumerate(selected_scripts, start=1):
                name = item.get("name", "")
                notice_preview = item.get("notice", "")
                preview = notice_preview[:20] + ("..." if len(notice_preview) > 20 else "")
                display = f"{idx}. {name} (提醒: {preview})"
                group_list.insert(tk.END, display)
            if select_index is not None:
                group_list.select_set(select_index)
        
        def edit_notice(event):
            sel = group_list.curselection()
            if not sel:
                return
            index = sel[0]
            item = selected_scripts[index]
            new_notice = simpledialog.askstring(
                "修改提醒",
                f"请输入脚本 '{item['name']}' 的新提醒内容：",
                parent=dialog,
                initialvalue=item.get("notice", "")
            )
            if new_notice is not None:
                item["notice"] = new_notice
                refresh_group_list(index)
        
        group_list.bind("<Double-Button-1>", edit_notice)
        
        # 初始填充
        refresh_group_list()
        
        btns = ttk.Frame(dialog)
        btns.pack(pady=5)
        
        ttk.Button(btns, text="添加 →", command=add_script).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="移除 ←", command=remove_script).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="上移", command=move_up).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="下移", command=move_down).pack(side=tk.LEFT, padx=3)
        
        def confirm():
            new_name = name_var.get().strip()
            new_notice = notice_text.get("1.0", tk.END).strip()
            if not new_name:
                messagebox.showerror("错误", "请输入脚本组名称")
                return
            if not selected_scripts:
                messagebox.showerror("错误", "请至少保留一个脚本")
                return
            if new_name != group_name:
                self.script_group_manager.delete_group(group_name)
            self.script_group_manager.save_group(new_name, selected_scripts, new_notice)
            self.refresh_script_list()
            self.log_message(f"已更新脚本组：{new_name}")
            dialog.destroy()
        
        ttk.Button(dialog, text="保存修改", style="Primary.TButton", command=confirm).pack(pady=10)
    
    def show_script_notice_dialog(self, script_name, notice):
        """显示脚本前置提醒，返回 'confirm'（执行）、'skip'（跳过）、'cancel'（终止）"""
        # 检测主窗口是否最小化
        was_iconic = self.wm_state() == 'iconic'
        
        # 如果最小化，先恢复并透明化，以便弹窗可见
        if was_iconic:
            self.deiconify()
            self.attributes('-alpha', 0.0)
            self.update()
        
        dialog = tk.Toplevel(self)
        self._set_window_icon(dialog)
        dialog.title(f"执行脚本前提醒 - {script_name}")
        dialog.geometry("480x340")
        dialog.transient(self)
        dialog.grab_set()
        dialog.attributes('-topmost', True)
        
        ttk.Label(
            dialog,
            text=f"即将执行脚本：{script_name}",
            font=("微软雅黑", 11, "bold")
        ).pack(pady=10)
        
        text = tk.Text(dialog, height=8, width=55)
        text.pack(padx=10, pady=5)
        text.insert(tk.END, notice or "无提醒内容")
        text.configure(state=tk.DISABLED)
        
        result = {"action": "cancel"}  # 默认取消
        
        def confirm():
            result["action"] = "confirm"
            dialog.destroy()
        
        def skip():
            result["action"] = "skip"
            dialog.destroy()
        
        def cancel():
            result["action"] = "cancel"
            dialog.destroy()
        
        btns = ttk.Frame(dialog)
        btns.pack(pady=10)
        
        ttk.Button(btns, text="我已确认，继续执行", style="Primary.TButton", command=confirm).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="跳过此脚本", command=skip).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="停止执行", style="Warning.TButton", command=cancel).pack(side=tk.LEFT, padx=3)
        
        dialog.focus_force()
        self.wait_window(dialog)
        
        # 关闭对话框后，如果之前最小化，则恢复透明度并重新最小化
        if was_iconic:
            self.attributes('-alpha', 1.0)
            self.iconify()
        
        return result["action"]
    
    def add_selected_group_to_queue(self):
        selected = self.script_list.curselection()
        if not selected:
            messagebox.showwarning("提示", "请先选择脚本组")
            return
        
        text = self.script_list.get(selected[0])
        if "[脚本组]" not in text:
            messagebox.showwarning("提示", "请选择脚本组名称，不要选择组内脚本")
            return
        
        group_name = text.replace("▶ [脚本组]", "").replace("▼ [脚本组]", "").strip()
        
        # ---------- 如果队列为空，重置会话 ----------
        if not self.current_queue:
            self.active_group_session = None
            self.current_script_index = 0
        # -----------------------------------------
        
        self.current_queue.append({
            "type": "group",
            "name": group_name
        })
        
        self.queue_list.insert(tk.END, f"[脚本组] {group_name}")
        self.log_message(f"已添加脚本组到队列：{group_name}")
    
    def delete_selected_script_group(self):
        selected = self.script_list.curselection()
        if not selected:
            return
        
        text = self.script_list.get(selected[0])
        if "[脚本组]" not in text:
            messagebox.showwarning("提示", "请先选择脚本组")
            return
        
        group_name = text.replace("▶ [脚本组]", "").replace("▼ [脚本组]", "").strip()
        
        if messagebox.askyesno("确认删除", f"确定删除脚本组：{group_name} 吗？"):
            self.script_group_manager.delete_group(group_name)
            self.expanded_groups.discard(group_name)
            self.refresh_script_list()
            self.log_message(f"已删除脚本组：{group_name}")
    
    def calculate_total_events(self):
        """精确计算总事件数（考虑重复），兼容脚本和脚本组"""
        total = 0
        for item in self.current_queue:
            if isinstance(item, dict):
                if item.get("type") == "group":
                    group_name = item.get("name")
                    try:
                        group = self.script_group_manager.load_group(group_name)
                        for script_item in group.get("scripts", []):
                            script_name = script_item.get("name") if isinstance(script_item, dict) else script_item
                            events = self.script_manager.load_script(script_name)
                            total += len(events)  # 组内脚本默认执行1次
                    except Exception:
                        pass
                elif item.get("type") == "script":
                    script_name = item.get("name")
                    repeat = item.get("repeat", 1)
                    events = self.script_manager.load_script(script_name)
                    total += len(events) * repeat
            else:
                # 旧格式元组 (name, repeat, img_path, threshold)
                name, repeat, _, _ = item
                events = self.script_manager.load_script(name)
                total += len(events) * repeat
        return total
    
    def calculate_total_time(self, speed):
        """计算队列中所有脚本的总时间（考虑重复次数和速度），兼容脚本和脚本组"""
        total_time = 0.0
        for item in self.current_queue:
            if isinstance(item, dict):
                if item.get("type") == "group":
                    group_name = item.get("name")
                    try:
                        group = self.script_group_manager.load_group(group_name)
                        for script_item in group.get("scripts", []):
                            script_name = script_item.get("name") if isinstance(script_item, dict) else script_item
                            events = self.script_manager.load_script(script_name)
                            if events:
                                total_time += events[-1][0]  # 组内脚本默认执行1次
                    except Exception:
                        pass
                elif item.get("type") == "script":
                    script_name = item.get("name")
                    repeat = item.get("repeat", 1)
                    events = self.script_manager.load_script(script_name)
                    if events:
                        total_time += events[-1][0] * repeat
            else:
                # 旧格式元组 (name, repeat, img_path, threshold)
                name, repeat, _, _ = item
                events = self.script_manager.load_script(name)
                if events:
                    total_time += events[-1][0] * repeat
        return total_time / speed if speed > 0 else total_time
    
    def format_duration(self, seconds):
        """将秒数格式化为 时:分:秒"""
        seconds = int(round(seconds))
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        seconds = seconds % 60
        time_parts = []
        if hours > 0:
            time_parts.append(f"{hours}时")
        if minutes > 0 or hours > 0:
            time_parts.append(f"{minutes}分")
        time_parts.append(f"{seconds}秒")
        return "".join(time_parts)
    
    def on_window_resize(self, event):
        """处理窗口缩放变化"""
        if sys.platform == "win32":
            new_scale = self.dpi_scaler.get_scaling_factor()
            if new_scale != self.current_scale:
                self.log_message(f"检测到DPI缩放变化 {self.current_scale} -> {new_scale}")
                self.current_scale = new_scale
                self.player.dpi_scale = new_scale  # 更新播放器的DPI缩放
    
    def on_search_focus_in(self, event):
        """当搜索框获得焦点时处理提示文字"""
        if self.search_entry.get() == "输入脚本名称搜索...":
            self.search_entry.delete(0, tk.END)
            self.search_entry.configure(foreground='black')
    
    def on_search_focus_out(self, event):
        """当搜索框失去焦点时处理提示文字"""
        if not self.search_entry.get():
            self.search_entry.insert(0, "输入脚本名称搜索...")
            self.search_entry.configure(foreground='grey')
    
    def on_search_key_release(self, event):
        """按键释放时触发搜索"""
        self.refresh_script_list()
    
    def _log_tag_for(self, message):
        """按日志内容判定高亮级别：失败 > 警告 > 成功 > 关键操作 > 步骤 > 普通"""
        text = str(message)
        rules = (
            ("err", ("错误", "失败", "异常", "出错", "无法", "未能", "已超时")),
            ("warn", ("警告", "超时", "跳过", "取消", "未识别", "未找到",
                      "已忽略", "防死机", "未匹配")),
            ("ok", ("成功", "完成", "已启动", "已保存", "已恢复", "✅")),
            ("key", ("定时任务", "按键触发", "截图流程", "倒计时", "已停止")),
            ("step", ("执行脚本", "执行任务", "任务", "步骤", "开始执行", "开始运行")),
        )
        for tag, keywords in rules:
            for kw in keywords:
                if kw in text:
                    return tag
        return "msg"

    def log_message(self, message):
        timestamp = time.strftime("[%Y-%m-%d %H:%M:%S] ")
        text = str(message)
        tag = self._log_tag_for(text)

        def safe_log():
            try:
                self.log_text.configure(state=tk.NORMAL)
                self.log_text.insert(tk.END, timestamp, ("ts",))
                self.log_text.insert(tk.END, f"{text}\n", (tag,))
                # 日志过长时裁掉最旧的部分，避免长时间运行后界面变卡
                try:
                    lines = int(self.log_text.index("end-1c").split(".")[0])
                    if lines > LOG_MAX_LINES:
                        self.log_text.delete("1.0", f"{lines - LOG_MAX_LINES}.0")
                except Exception:
                    pass
                self.log_text.configure(state=tk.DISABLED)
                self.log_text.see(tk.END)  # 滚动到底部
            except Exception:
                pass

        self.after(0, safe_log)
    
    def _auto_ui_scale(self, event=None):
        """统一UI防溢出保护：窗口缩放时保持控件可见。"""
        try:
            w = self.winfo_width()
            h = self.winfo_height()
            if w < 900 or h < 600:
                return
        except:
            pass
    
    def _apply_maximized_launch(self, screen_w, screen_h):
        """启动时让窗口铺满屏幕：优先最大化，失败则按屏幕尺寸铺满。"""
        if getattr(self, "_maximize_done", False):
            return
        try:
            self.state("zoomed")          # Windows：最大化，仍保留任务栏
            self._maximize_done = True
            return
        except Exception:
            pass
        try:
            self.attributes("-zoomed", True)   # 其它平台的 Tk 写法
            self._maximize_done = True
            return
        except Exception:
            pass
        try:
            self.geometry(f"{screen_w}x{screen_h}+0+0")
        except Exception:
            pass

    def setup_ui(self):
        # ---------- 主题样式 ----------
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.style.configure('TFrame', background='#f0f0f0')
        self.style.configure('TLabel', background='#f0f0f0', font=('微软雅黑', 9))
        self.style.configure('TButton', font=('微软雅黑', 9), padding=5)
        self.style.configure('Primary.TButton', foreground='white', background='#0078d4')
        self.style.configure('Warning.TButton', foreground='white', background='#dc3545')
        self.style.configure('TLabelframe', font=('微软雅黑', 9, 'bold'))
        self.style.map('Primary.TButton',
                       background=[('active', '#006cbd'), ('pressed', '#005a9e')])
        self.style.map('Warning.TButton',
                       background=[('active', '#c82333'), ('pressed', '#bd2130')])
        
        # 供主界面和“高级设置”对话框共用的变量
        self.trigger_key_var = tk.StringVar(
            value=str(self.app_settings.get("screenshot_trigger_key", "ctrl+alt+1")))
        self.trigger_status_var = tk.StringVar(value="未启用")
        self.screenshot_mode_status_var = tk.StringVar(value="未开始")
        self.screenshot_timeout_var = tk.IntVar(
            value=int(round(self.screenshot_step_timeout_minutes)))
        
        # ---------- 根容器（网格：顶栏 / 主体 / 日志 / 底部控制） ----------
        root = ttk.Frame(self, padding=(10, 8, 10, 6))
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=4, minsize=300)   # 主体两列
        root.rowconfigure(2, weight=3, minsize=200)   # 执行日志（留足空间）
        
        # ---------- 顶栏 ----------
        toolbar = ttk.Frame(root)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        
        ttk.Label(toolbar, text="搜索：").pack(side=tk.LEFT)
        self.search_entry = ttk.Entry(toolbar, width=30)
        self.search_entry.pack(side=tk.LEFT, padx=(0, 4))
        self.search_entry.insert(0, "输入脚本名称搜索...")
        self.search_entry.configure(foreground='grey')
        self.search_entry.bind("<FocusIn>", self.on_search_focus_in)
        self.search_entry.bind("<FocusOut>", self.on_search_focus_out)
        self.search_entry.bind("<KeyRelease>", self.on_search_key_release)
        ttk.Button(toolbar, text="搜索", style='Primary.TButton',
                   command=self.refresh_script_list).pack(side=tk.LEFT)
        
        # 右侧：常用入口
        ttk.Button(toolbar, text="开始录制", style='Primary.TButton',
                   command=self.safe_toggle_recording).pack(side=tk.RIGHT, padx=2)
        ttk.Button(toolbar, text="停止录制", style='Warning.TButton',
                   command=self.safe_stop_recording).pack(side=tk.RIGHT, padx=2)
        ttk.Button(toolbar, text="定时任务",
                   command=self.open_task_manager).pack(side=tk.RIGHT, padx=(12, 2))
        ttk.Button(toolbar, text="组管理",
                   command=self.open_group_manager_dialog).pack(side=tk.RIGHT, padx=2)
        ttk.Button(toolbar, text="截图高级设置",
                   command=self.open_screenshot_advanced_dialog).pack(side=tk.RIGHT, padx=2)
        
        self.help_icon = ttk.Label(toolbar, text="?", cursor="hand2",
                                   font=('Arial', 12, 'bold'), foreground='#666666')
        self.help_icon.pack(side=tk.RIGHT, padx=(10, 0))
        
        # ---------- 主体：三列（可用脚本 | 截图识别 | 执行队列） ----------
        body = ttk.Frame(root)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=3, uniform="body")
        body.columnconfigure(1, weight=4, uniform="body")
        body.columnconfigure(2, weight=3, uniform="body")
        body.rowconfigure(0, weight=1)
        
        # ---- 左列：可用脚本 / 组 ----
        script_panel = ttk.LabelFrame(body, text="可用脚本 / 脚本组 / 混合组（双击执行）", padding=8)
        script_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        script_panel.rowconfigure(0, weight=1)
        script_panel.columnconfigure(0, weight=1)
        
        self.script_list = tk.Listbox(script_panel, selectmode=tk.SINGLE,
                                      font=('微软雅黑', 10), bg='white',
                                      activestyle='none', exportselection=False)
        self.script_list.grid(row=0, column=0, sticky="nsew")
        script_scroll = ttk.Scrollbar(script_panel, orient=tk.VERTICAL,
                                      command=self.script_list.yview)
        script_scroll.grid(row=0, column=1, sticky="ns")
        self.script_list.config(yscrollcommand=script_scroll.set)
        
        script_controls = ttk.Frame(script_panel)
        script_controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(script_controls, text="重命名", command=self.rename_script).pack(side=tk.LEFT, padx=2)
        ttk.Button(script_controls, text="删除", style='Warning.TButton',
                   command=self.delete_script).pack(side=tk.LEFT, padx=2)
        ttk.Button(script_controls, text="新建脚本组",
                   command=self.create_script_group_dialog).pack(side=tk.LEFT, padx=(10, 2))
        ttk.Button(script_controls, text="新建混合组",
                   command=self.create_mixed_group_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(script_controls, text="组操作…",
                   command=self.open_group_manager_dialog).pack(side=tk.LEFT, padx=2)
        
        # ---- 中列：截图顺序识别点击（独占整列） ----
        screenshot_frame = ttk.LabelFrame(body, text="第二种操作模式：截图顺序识别点击", padding=8)
        screenshot_frame.grid(row=0, column=1, sticky="nsew", padx=6)
        screenshot_frame.columnconfigure(0, weight=1)
        screenshot_frame.rowconfigure(1, weight=1)

        # ---- 右列：执行队列（独占整列） ----
        queue_panel = ttk.LabelFrame(body, text="执行队列", padding=8)
        queue_panel.grid(row=0, column=2, sticky="nsew")
        queue_panel.rowconfigure(0, weight=1)
        queue_panel.columnconfigure(0, weight=1)
        
        self.queue_list = tk.Listbox(queue_panel, selectmode=tk.SINGLE,
                                     font=('微软雅黑', 10), bg='white',
                                     activestyle='none', exportselection=False)
        self.queue_list.grid(row=0, column=0, sticky="nsew")
        queue_scroll = ttk.Scrollbar(queue_panel, orient=tk.VERTICAL,
                                     command=self.queue_list.yview)
        queue_scroll.grid(row=0, column=1, sticky="ns")
        self.queue_list.config(yscrollcommand=queue_scroll.set)
        
        queue_controls = ttk.Frame(queue_panel)
        queue_controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(queue_controls, text="添加选中脚本", style='Primary.TButton',
                   command=self.add_to_queue).pack(side=tk.LEFT, padx=2)
        ttk.Button(queue_controls, text="截图组入队",
                   command=self.add_screenshot_group_to_queue).pack(side=tk.LEFT, padx=2)
        ttk.Button(queue_controls, text="移除", style='Warning.TButton',
                   command=self.remove_from_queue).pack(side=tk.RIGHT, padx=2)
        ttk.Button(queue_controls, text="清空", style='Warning.TButton',
                   command=self.clear_queue).pack(side=tk.RIGHT, padx=2)
        
        group_bar = ttk.Frame(screenshot_frame)
        group_bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(group_bar, text="截图组：").pack(side=tk.LEFT)
        self.screenshot_group_var = tk.StringVar(value=self.screenshot_engine.active_group_name)
        self.screenshot_group_combo = ttk.Combobox(
            group_bar, textvariable=self.screenshot_group_var,
            values=self.screenshot_engine.list_groups(), state="readonly", width=22)
        self.screenshot_group_combo.pack(side=tk.LEFT, padx=(0, 6))
        self.screenshot_group_combo.bind("<<ComboboxSelected>>", self.on_screenshot_group_changed)
        ttk.Button(group_bar, text="组管理…", command=self.open_screenshot_advanced_dialog).pack(side=tk.LEFT, padx=2)
        
        # 任务列表
        self.screenshot_task_list = tk.Listbox(screenshot_frame, height=5,
                                               font=('微软雅黑', 9), bg='white',
                                               activestyle='none', exportselection=False)
        self.screenshot_task_list.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        shot_scroll = ttk.Scrollbar(screenshot_frame, orient=tk.VERTICAL,
                                    command=self.screenshot_task_list.yview)
        shot_scroll.grid(row=1, column=1, sticky="ns", pady=(6, 0))
        self.screenshot_task_list.config(yscrollcommand=shot_scroll.set)
        self.screenshot_task_list.bind("<Double-Button-1>", self.edit_selected_screenshot_task)
        
        # 状态 + 按键触发状态（一行）
        status_line = ttk.Frame(screenshot_frame)
        status_line.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(status_line, text="状态：").pack(side=tk.LEFT)
        ttk.Label(status_line, textvariable=self.screenshot_mode_status_var,
                  font=("微软雅黑", 9, "bold"), foreground="#0b6b3a").pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(status_line, text="按键触发：").pack(side=tk.LEFT)
        ttk.Label(status_line, textvariable=self.trigger_status_var,
                  foreground="#1f6fa8").pack(side=tk.LEFT)
        
        # 操作按钮（两行，避免挤成一长条）
        shot_ops = ttk.Frame(screenshot_frame)
        shot_ops.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(shot_ops, text="添加截图", command=self.add_screenshot_task).pack(side=tk.LEFT, padx=2)
        ttk.Button(shot_ops, text="编辑操作", command=self.edit_screenshot_task).pack(side=tk.LEFT, padx=2)
        ttk.Button(shot_ops, text="添加条件分支", command=self.add_screenshot_branch_task).pack(side=tk.LEFT, padx=2)
        ttk.Button(shot_ops, text="上移", width=4,
                   command=self.move_screenshot_task_up).pack(side=tk.RIGHT, padx=2)
        ttk.Button(shot_ops, text="下移", width=4,
                   command=self.move_screenshot_task_down).pack(side=tk.RIGHT, padx=2)
        ttk.Button(shot_ops, text="删除", width=4, style='Warning.TButton',
                   command=self.remove_screenshot_task).pack(side=tk.RIGHT, padx=2)
        ttk.Button(shot_ops, text="清空", width=4, style='Warning.TButton',
                   command=self.clear_screenshot_tasks).pack(side=tk.RIGHT, padx=2)
        
        shot_run = ttk.Frame(screenshot_frame)
        shot_run.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(shot_run, text="▶ 开始按顺序识别并点击", style='Primary.TButton',
                   command=self.start_screenshot_click_mode).pack(side=tk.LEFT, padx=2)
        ttk.Button(shot_run, text="■ 停止识别", style='Warning.TButton',
                   command=self.stop_screenshot_click_mode).pack(side=tk.LEFT, padx=2)
        ttk.Label(shot_run, text="整组循环：").pack(side=tk.LEFT, padx=(12, 0))
        self.screenshot_loop_var = tk.IntVar(
            value=int(self.app_settings.get("screenshot_loop_times", 1) or 1))
        ttk.Spinbox(shot_run, from_=1, to=9999, width=6,
                    textvariable=self.screenshot_loop_var).pack(side=tk.LEFT)
        ttk.Label(shot_run, text="次（如：循环 20 次 = 每轮点 1 个赞/滚 1 次）").pack(side=tk.LEFT)
        ttk.Button(shot_run, text="入队单个任务",
                   command=self.add_single_screenshot_task_to_queue).pack(side=tk.RIGHT, padx=2)
        
        # ---------- 执行日志（占据剩余空间，不再被挤小） ----------
        log_panel = ttk.LabelFrame(root, text="执行日志", padding=8)
        log_panel.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        log_panel.rowconfigure(0, weight=1)
        log_panel.columnconfigure(0, weight=1)
        
        self.log_text = tk.Text(log_panel, height=8, font=('微软雅黑', 9),
                                bg='white', wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_panel, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        # 日志高亮配色：时间戳灰、成功绿、失败红、警告橙、步骤蓝、关键操作紫
        self.log_text.tag_configure("ts", foreground="#8c8c8c")
        self.log_text.tag_configure("msg", foreground="#222222")
        self.log_text.tag_configure("ok", foreground="#0b6b3a",
                                    font=("微软雅黑", 9, "bold"))
        self.log_text.tag_configure("err", foreground="#c0392b", background="#fdeceb",
                                    font=("微软雅黑", 9, "bold"))
        self.log_text.tag_configure("warn", foreground="#a35b00",
                                    font=("微软雅黑", 9, "bold"))
        self.log_text.tag_configure("step", foreground="#1f6fa8")
        self.log_text.tag_configure("key", foreground="#6a1b9a",
                                    font=("微软雅黑", 9, "bold"))
        
        # ---------- 底部：进度 + 执行控制 ----------
        bottom_panel = ttk.Frame(root)
        bottom_panel.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        
        progress_row = ttk.Frame(bottom_panel)
        progress_row.pack(fill=tk.X, pady=(0, 6))
        self.progress = ttk.Progressbar(progress_row, orient=tk.HORIZONTAL,
                                        mode='determinate', maximum=100)
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress_label = ttk.Label(progress_row, text="就绪", width=20, anchor="e")
        self.progress_label.pack(side=tk.RIGHT, padx=(8, 0))
        
        exec_controls = ttk.Frame(bottom_panel)
        exec_controls.pack(fill=tk.X)
        ttk.Button(exec_controls, text="▶ 开始执行", style='Primary.TButton',
                   command=self.start_execution).pack(side=tk.LEFT, padx=2)
        ttk.Button(exec_controls, text="■ 停止", style='Warning.TButton',
                   command=self.stop_execution).pack(side=tk.LEFT, padx=2)
        ttk.Button(exec_controls, text="⏸ 暂停",
                   command=self.safe_pause_execution).pack(side=tk.LEFT, padx=2)
        ttk.Button(exec_controls, text="▶ 继续",
                   command=self.safe_resume_execution).pack(side=tk.LEFT, padx=2)
        
        settings_frame = ttk.Frame(exec_controls)
        settings_frame.pack(side=tk.RIGHT)
        ttk.Label(settings_frame, text="速度倍数:").pack(side=tk.LEFT)
        self.speed_combobox = ttk.Combobox(settings_frame, textvariable=self.speed_var,
                                           values=list(range(1, 11)), width=4)
        self.speed_combobox.pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(settings_frame, text="自动继续", variable=self.auto_continue_var).pack(side=tk.LEFT)
        
        # ---------- 状态栏 ----------
        status_bar = ttk.Frame(self, relief=tk.SUNKEN)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_label = ttk.Label(status_bar, text="就绪", anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, padx=5)
        
        # ---------- 事件绑定 ----------
        self.script_list.bind('<Double-Button-1>', self.on_script_double_click)
        self.queue_list.bind('<ButtonPress-1>', self.on_drag_start)
        self.queue_list.bind('<B1-Motion>', self.on_drag_motion)
        self.queue_list.bind('<ButtonRelease-1>', self.on_drag_end)
        
        self.refresh_script_list()
        self.refresh_screenshot_task_list()

    # 兼容旧代码：部分逻辑会调用此方法显示提示
    def show_ui_hint(self, text):
        if hasattr(self, "status_label"):
            self.status_label.config(text=text)

    # ---------- 二次打开的窗口 ----------
    def open_group_manager_dialog(self):
        """脚本组 / 混合组管理（从主界面剥离的次要操作）"""
        win = getattr(self, "group_manager_dialog", None)
        try:
            if win is not None and win.winfo_exists():
                win.deiconify()
                win.lift()
                return
        except Exception:
            pass
        
        dialog = tk.Toplevel(self)
        dialog.title("脚本组 / 混合组管理")
        dialog.geometry("560x330")
        dialog.resizable(False, False)
        dialog.transient(self)
        self._set_window_icon(dialog)
        self.group_manager_dialog = dialog
        
        ttk.Label(dialog, text="操作对象：左侧“可用脚本”列表里当前选中的组",
                  foreground="gray").pack(anchor="w", padx=14, pady=(12, 6))
        
        script_frame = ttk.LabelFrame(dialog, text="脚本组", padding=10)
        script_frame.pack(fill=tk.X, padx=12, pady=4)
        srow = ttk.Frame(script_frame)
        srow.pack(fill=tk.X)
        ttk.Button(srow, text="新建脚本组", command=self.create_script_group_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(srow, text="编辑", command=self.edit_selected_script_group).pack(side=tk.LEFT, padx=2)
        ttk.Button(srow, text="入队", style="Primary.TButton",
                   command=self.add_selected_group_to_queue).pack(side=tk.LEFT, padx=2)
        ttk.Button(srow, text="删除", style="Warning.TButton",
                   command=self.delete_selected_script_group).pack(side=tk.LEFT, padx=2)
        
        mixed_frame = ttk.LabelFrame(dialog, text="混合组", padding=10)
        mixed_frame.pack(fill=tk.X, padx=12, pady=4)
        mrow = ttk.Frame(mixed_frame)
        mrow.pack(fill=tk.X)
        ttk.Button(mrow, text="新建混合组", command=self.create_mixed_group_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(mrow, text="编辑", command=self.edit_mixed_group).pack(side=tk.LEFT, padx=2)
        ttk.Button(mrow, text="入队", style="Primary.TButton",
                   command=self.add_mixed_group_to_queue).pack(side=tk.LEFT, padx=2)
        ttk.Button(mrow, text="删除", style="Warning.TButton",
                   command=self.delete_mixed_group).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(dialog, text="关闭", command=dialog.destroy).pack(pady=12)
    
    def open_screenshot_advanced_dialog(self):
        """截图识别高级设置：组管理 + 按键触发 + 防死机超时"""
        win = getattr(self, "screenshot_advanced_dialog", None)
        try:
            if win is not None and win.winfo_exists():
                win.deiconify()
                win.lift()
                return
        except Exception:
            pass
        
        dialog = tk.Toplevel(self)
        dialog.title("截图识别高级设置")
        dialog.geometry("620x420")
        dialog.transient(self)
        self._set_window_icon(dialog)
        self.screenshot_advanced_dialog = dialog
        
        # 截图组管理
        group_frame = ttk.LabelFrame(dialog, text="截图组管理", padding=10)
        group_frame.pack(fill=tk.X, padx=12, pady=(12, 6))
        ttk.Label(group_frame, text="当前组：").pack(side=tk.LEFT)
        ttk.Label(group_frame, textvariable=self.screenshot_group_var,
                  font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(group_frame, text="新增组", command=self.add_screenshot_group).pack(side=tk.LEFT, padx=2)
        ttk.Button(group_frame, text="重命名", command=self.rename_screenshot_group).pack(side=tk.LEFT, padx=2)
        ttk.Button(group_frame, text="复制组", command=self.duplicate_screenshot_group).pack(side=tk.LEFT, padx=2)
        ttk.Button(group_frame, text="删除组", style="Warning.TButton",
                   command=self.delete_screenshot_group).pack(side=tk.LEFT, padx=2)
        
        # 按键触发
        trigger_frame = ttk.LabelFrame(dialog, text="按键触发", padding=10)
        trigger_frame.pack(fill=tk.X, padx=12, pady=6)
        trow = ttk.Frame(trigger_frame)
        trow.pack(fill=tk.X)
        ttk.Label(trow, text="触发按键：").pack(side=tk.LEFT)
        trigger_combo = ttk.Combobox(
            trow, textvariable=self.trigger_key_var, width=16,
            values=["ctrl+alt+1", "ctrl+alt+2", "ctrl+alt+3", "ctrl+shift+f9",
                    "f8", "f9", "enter", "ctrl+enter"])
        trigger_combo.pack(side=tk.LEFT, padx=(0, 6))
        btn_text = "停用按键触发" if getattr(self, "trigger_enabled", False) else "启用按键触发"
        self.trigger_toggle_btn = ttk.Button(trow, text=btn_text, command=self.toggle_trigger_key)
        self.trigger_toggle_btn.pack(side=tk.LEFT, padx=2)
        ttk.Label(trigger_frame, textvariable=self.trigger_status_var,
                  foreground="#1f6fa8").pack(anchor="w", pady=(6, 0))
        ttk.Label(trigger_frame,
                  text="按一次设定键，当前截图组立刻从第一个任务重新执行；软件自身发出的按键不会误触发。\n"
                       "单个按键（如 enter）在任何地方按下都会触发，建议使用组合键。",
                  foreground="gray", justify=tk.LEFT).pack(anchor="w", pady=(4, 0))
        
        # 防死机超时
        timeout_frame = ttk.LabelFrame(dialog, text="防死机保护", padding=10)
        timeout_frame.pack(fill=tk.X, padx=12, pady=6)
        orow = ttk.Frame(timeout_frame)
        orow.pack(fill=tk.X)
        ttk.Label(orow, text="单步连续").pack(side=tk.LEFT)
        ttk.Spinbox(orow, from_=0, to=120, width=5,
                    textvariable=self.screenshot_timeout_var).pack(side=tk.LEFT, padx=4)
        ttk.Label(orow, text="分钟未识别成功则自动停止当前任务（0 = 关闭）").pack(side=tk.LEFT)
        ttk.Button(orow, text="保存", command=self._apply_screenshot_timeout_from_ui).pack(side=tk.LEFT, padx=8)
        
        ttk.Button(dialog, text="关闭", command=dialog.destroy).pack(pady=12)
    def show_group_notice_dialog(self, group_name, notice):
        # 检测主窗口是否最小化
        was_iconic = self.wm_state() == 'iconic'
        
        # 如果最小化，先恢复并透明化，以便弹窗可见
        if was_iconic:
            self.deiconify()
            self.attributes('-alpha', 0.0)  # 完全透明
            self.update()  # 确保窗口状态更新
        
        dialog = tk.Toplevel(self)
        self._set_window_icon(dialog)
        dialog.title(f"执行脚本组前提醒 - {group_name}")
        dialog.geometry("520x360")
        dialog.transient(self)
        dialog.grab_set()
        dialog.attributes('-topmost', True)
        
        ttk.Label(
            dialog,
            text=f"即将执行脚本组：{group_name}",
            font=("微软雅黑", 11, "bold")
        ).pack(pady=10)
        
        text = tk.Text(dialog, height=10, width=60)
        text.pack(padx=10, pady=5)
        text.insert(tk.END, notice or "无提醒内容")
        text.configure(state=tk.DISABLED)
        
        result = {"ok": False}
        
        def confirm():
            result["ok"] = True
            dialog.destroy()
        
        def cancel():
            result["ok"] = False
            dialog.destroy()
        
        btns = ttk.Frame(dialog)
        btns.pack(pady=10)
        
        ttk.Button(btns, text="我已确认，开始执行", style="Primary.TButton", command=confirm).pack(side=tk.LEFT, padx=5)
        ttk.Button(btns, text="取消", command=cancel).pack(side=tk.LEFT, padx=5)
        
        dialog.focus_force()
        self.wait_window(dialog)
        
        # 关闭对话框后，如果之前最小化，则恢复透明度并重新最小化
        if was_iconic:
            self.attributes('-alpha', 1.0)
            self.iconify()
        
        return result["ok"]
    
    def _set_window_icon(self, window):
        """为窗口设置图标"""
        try:
            if self.icon_path:
                window.iconbitmap(self.icon_path)
                # 对于某些系统，还需要设置PhotImage
                try:
                    window.tk.call('wm', 'iconphoto', window._w, tk.PhotoImage(file=self.icon_path))
                except:
                    pass
        except Exception as e:
            print(f"无法加载图标: {e}")
    
    # 新增重新执行相关方法
    def safe_restart_execution(self):
        """线程安全的重新执行方法"""
        self.after(0, self.restart_execution)
    
    # 修改 restart_execution 方法
    def restart_execution(self):
        """完全重启执行流程"""
        self.log_message("正在重新初始化执行环境...")
        
        # 完全停止当前执行
        self.stop_execution()
        
        # 确保队列状态正确
        if not self.current_queue:
            self.log_message("错误：队列为空，无法执行")
            return
        
        # 重置计数器
        self.current_script_index = 0
        self.processed_events = 0
        self.total_events = self.calculate_total_events()
        
        # 重置进度条
        self.update_progress(0, f"(0/{self.total_events})" if self.total_events else "")
        
        self.status_label.config(text="准备执行")
        
        # 开始新执行
        self.start_execution()
    
    def on_drag_start(self, event):
        """开始拖动事件处理"""
        index = self.queue_list.nearest(event.y)
        if index >= 0:
            self.drag_data['item'] = self.queue_list.get(index)
            self.drag_data['index'] = index
            self.queue_list.select_set(index)
    
    def on_drag_motion(self, event):
        """拖动过程中处理"""
        if self.drag_data['item'] is None:
            return
        # 添加边界限制
        if event.y < 0:
            current_index = 0
        elif event.y > self.queue_list.winfo_height():
            current_index = self.queue_list.size() - 1
        else:
            current_index = self.queue_list.nearest(event.y)
        
        current_index = self.queue_list.nearest(event.y)
        if current_index < 0:
            return
        
        # 当拖动到新位置时交换项目
        if current_index != self.drag_data['index']:
            # 更新列表显示
            self.queue_list.delete(self.drag_data['index'])
            self.queue_list.insert(current_index, self.drag_data['item'])
            
            # 更新数据存储
            item = self.current_queue.pop(self.drag_data['index'])
            self.current_queue.insert(current_index, item)
            
            # 更新当前拖动索引
            self.drag_data['index'] = current_index
            self.queue_list.select_set(current_index)
    
    def on_drag_end(self, event):
        """结束拖动事件处理"""
        self.drag_data['item'] = None
        self.drag_data['index'] = None
    
    def on_script_double_click(self, event):
        selected = self.script_list.curselection()
        if not selected:
            return
        
        text = self.script_list.get(selected[0])
        
        # 处理脚本组
        if "[脚本组]" in text:
            group_name = text.replace("▶ [脚本组]", "").replace("▼ [脚本组]", "").strip()
            if group_name in self.expanded_groups:
                self.expanded_groups.remove(group_name)
            else:
                self.expanded_groups.add(group_name)
            self.refresh_script_list()
            return
        
        # 处理混合组
        if "[混合组]" in text:
            group_name = text.replace("▶ [混合组]", "").replace("▼ [混合组]", "").strip()
            if group_name in self.expanded_groups:
                self.expanded_groups.remove(group_name)
            else:
                self.expanded_groups.add(group_name)
            self.refresh_script_list()
            return
        
        # 如果是脚本（以 └ 开头或普通脚本名），则执行添加队列逻辑
        if text.strip().startswith("└"):
            return  # 双击子项不处理，或可忽略
        
        # 普通脚本双击添加队列
        script_name = text.strip()
        repeat = simpledialog.askinteger(
            "重复次数",
            f"请输入脚本 '{script_name}' 的重复次数(1-99):",
            minvalue=1,
            maxvalue=99,
            initialvalue=1
        )
        if repeat and repeat > 0:
            img_path = self.script_manager.get_image_path(script_name) or "无图片"
            self.current_queue.append({
                "type":      "script",
                "name":      script_name,
                "repeat":    repeat,
                "img_path":  img_path,
                "threshold": 0.5
            })
            self.queue_list.insert(tk.END, f"{script_name} (x{repeat})")
            self.log_message(f"已添加脚本到队列: {script_name} x{repeat}")
    
    def toggle_auto_continue(self):
        """切换自动继续状态"""
        state = "启用" if self.auto_continue_var.get() else "关闭"
        self.log_message(f"自动继续功能已{state}")
    
    def refresh_script_list(self):
        search_term = self.search_entry.get().strip().lower()
        if search_term == "输入脚本名称搜索...":
            search_term = ""
        
        self.script_list.delete(0, tk.END)
        
        all_groups = self.script_group_manager.list_groups()
        all_scripts = self.script_manager.list_scripts()
        all_mixed = self.mixed_group_manager.list_groups()
        
        # ---------- 收集所有已归入脚本组的脚本名（兼容新旧格式） ----------
        grouped_scripts = set()
        for group_name in all_groups:
            try:
                group = self.script_group_manager.load_group(group_name)
                for script_item in group.get("scripts", []):
                    if isinstance(script_item, dict):
                        grouped_scripts.add(script_item.get("name"))
                    else:
                        grouped_scripts.add(script_item)
            except:
                pass
        # ----------------------------------------------------------------
        
        # 1. 显示脚本组（始终显示组名）
        for group_name in all_groups:
            # 搜索过滤：组名或组内脚本匹配时才显示组名
            show_group = True
            if search_term:
                # 检查组名是否匹配
                if search_term in group_name.lower():
                    show_group = True
                else:
                    # 检查组内是否有匹配的脚本
                    try:
                        group = self.script_group_manager.load_group(group_name)
                        has_match = any(
                            search_term in (s.get("name") if isinstance(s, dict) else s).lower()
                            for s in group.get("scripts", [])
                        )
                        show_group = has_match
                    except:
                        show_group = False
            if not show_group:
                continue
            
            prefix = "▼" if group_name in self.expanded_groups else "▶"
            self.script_list.insert(tk.END, f"{prefix} [脚本组] {group_name}")
            
            # 如果组已展开，则显示组内脚本
            if group_name in self.expanded_groups:
                try:
                    group = self.script_group_manager.load_group(group_name)
                    for script_item in group.get("scripts", []):
                        script_name = script_item.get("name") if isinstance(script_item, dict) else script_item
                        if not search_term or search_term in script_name.lower():
                            self.script_list.insert(tk.END, f"    └ {script_name}")
                except:
                    pass
        
        # 2. 显示混合组（独立循环，不受脚本组数量影响）
        for group_name in all_mixed:
            show_group = True
            if search_term:
                if search_term in group_name.lower():
                    show_group = True
                else:
                    # 检查组内步骤是否有匹配
                    try:
                        group = self.mixed_group_manager.load_group(group_name)
                        has_match = False
                        for step in group.get("steps", []):
                            step_type = step.get("type")
                            if step_type == "script":
                                if search_term in step.get("name", "").lower():
                                    has_match = True
                                    break
                            elif step_type == "script_group":
                                if search_term in step.get("name", "").lower():
                                    has_match = True
                                    break
                            elif step_type == "screenshot_group":
                                if search_term in step.get("name", "").lower():
                                    has_match = True
                                    break
                            elif step_type == "screenshot_task":
                                if search_term in step.get("display", "").lower():
                                    has_match = True
                                    break
                        show_group = has_match
                    except:
                        show_group = False
            if not show_group:
                continue
            
            prefix = "▼" if group_name in self.expanded_groups else "▶"
            self.script_list.insert(tk.END, f"{prefix} [混合组] {group_name}")
            
            if group_name in self.expanded_groups:
                try:
                    group = self.mixed_group_manager.load_group(group_name)
                    steps = group.get("steps", [])
                    for idx, step in enumerate(steps, 1):
                        step_type = step.get("type", "unknown")
                        if step_type == "script":
                            desc = f"脚本: {step.get('name', '')}"
                        elif step_type == "script_group":
                            desc = f"脚本组: {step.get('name', '')}"
                        elif step_type == "screenshot_group":
                            desc = f"截图组: {step.get('name', '')}"
                        elif step_type == "screenshot_task":
                            task_name = step.get("display", f"任务{step.get('task_index', 0) + 1}")
                            desc = f"截图任务: {task_name} (组:{step.get('group_name', '')})"
                        else:
                            desc = f"步骤{idx}: 未知类型"
                        if not search_term or search_term in desc.lower():
                            self.script_list.insert(tk.END, f"    └ {idx}. {desc}")
                except Exception:
                    pass
        
        # 3. 遍历单独的脚本，跳过已归入组的脚本
        for script in all_scripts:
            if script in grouped_scripts:
                continue
            if search_term and search_term not in script.lower():
                continue
            self.script_list.insert(tk.END, script)
    
    def update_progress(self, value, detail=""):
        """通用进度更新：value 取 0-100，自动夹紧并显示百分比与进度明细"""
        
        def safe_update():
            try:
                v = float(value)
            except Exception:
                v = 0.0
            v = max(0.0, min(100.0, v))
            try:
                self.progress['value'] = v
                if hasattr(self, "progress_label"):
                    text = f"{v:.0f}%"
                    if detail:
                        text += f" {detail}"
                    self.progress_label.config(text=text)
            except Exception:
                pass
        
        self.after(0, safe_update)
    
    def setup_hotkeys(self):
        """设置全局热键"""
        try:
            from pynput import keyboard
            self.hotkey_listener = keyboard.GlobalHotKeys({
                '<ctrl>+<alt>+<f1>':  self.safe_restart_execution,
                '<ctrl>+<alt>+<f7>':  self.safe_pause_execution,
                '<ctrl>+<alt>+<f8>':  self.safe_resume_execution,
                '<ctrl>+<alt>+<f9>':  self.safe_toggle_recording,
                '<ctrl>+<alt>+<f10>': self.safe_stop_recording,
                '<ctrl>+<alt>+<f11>': self.safe_start_execution,
                '<ctrl>+<alt>+<f12>': self.safe_stop_execution
            })
            self.hotkey_listener.start()
        except Exception as e:
            self.log_message(f"热键初始化失败: {str(e)}")
    
    # 新增暂停/继续方法
    def safe_pause_execution(self):
        self.log_message("暂停执行")
        self.after(0, self.pause_execution)
    
    def safe_resume_execution(self):
        self.log_message("继续执行")
        self.after(0, self.resume_execution)
    
    def pause_execution(self):
        if self.player.is_playing:
            self.player.pause_execution()
            self.status_label.config(text="已暂停")
            self.log_message("执行已暂停")
    
    def resume_execution(self):
        if self.queue_paused:  # 处理队列暂停的情况
            self.queue_paused = False
            self.process_next_script()
            self.log_message("继续执行队列")
        elif self.player.is_playing and self.player.paused:
            self.player.resume_execution()
            self.status_label.config(text="执行中...")
            self.log_message("执行已继续")
    
    def process_next_script(self):
        try:
            if self.active_group_session:
                group_name = self.active_group_session.get("name")
                has_group = any(
                    isinstance(item, dict) and item.get("type") == "group" and item.get("name") == group_name
                    for item in self.current_queue
                )
                if not has_group:
                    self.active_group_session = None
                    self.log_message("检测到脚本组会话已过期，已重置")
            
            if not self.current_queue:
                self.log_message("执行队列为空")
                return
            
            if self.current_script_index >= len(self.current_queue):
                self.current_script_index = 0
            
            item = self.current_queue[self.current_script_index]
            
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "group":
                    self.process_script_group(item)
                elif item_type == "screenshot_group":
                    self.process_screenshot_group(item)
                elif item_type == "screenshot_task":
                    self.process_screenshot_task(item)
                elif item_type == "mixed_group":
                    self.process_mixed_group(item)
                else:
                    self.process_normal_script_item(item)
            else:
                self.process_normal_script_item(item)
        
        except Exception as e:
            self.log_message(f"执行队列出错：{str(e)}")
    
    def process_screenshot_group(self, item):
        """执行截图流程组（阻塞直到完成或停止）"""
        group_name = item.get("name")
        if not group_name:
            self.log_message("截图组名称为空")
            return
        
        # 切换到指定截图组
        if not self.screenshot_engine.set_active_group(group_name):
            self.log_message(f"切换截图组失败：{group_name}")
            return
        
        tasks = self.screenshot_engine.tasks
        if not tasks:
            self.log_message(f"截图组 {group_name} 为空，跳过")
            self.current_script_index += 1
            self.continue_or_finish()
            return
        
        self.log_message(f"开始执行截图流程组：{group_name}，共 {len(tasks)} 个任务")
        # 启动截图引擎（非阻塞）
        self.screenshot_engine.start()
        
        # 等待引擎执行完毕，同时检查停止标志
        while self.screenshot_engine.running:
            if self.player.should_stop:
                self.log_message("用户停止，正在终止截图流程...")
                self.screenshot_engine.stop(log=False)
                break
            time.sleep(0.1)
        
        # 如果正常完成，继续队列
        if not self.player.should_stop:
            self.log_message(f"截图流程组 {group_name} 执行完成")
        else:
            self.log_message(f"截图流程组 {group_name} 被中断")
        
        # 继续下一个队列项
        self.current_script_index += 1
        self.continue_or_finish()
    
    def continue_or_finish(self):
        """检查队列是否还有剩余，并决定继续或结束"""
        if self.auto_continue_var.get() and self.current_script_index < len(self.current_queue):
            self.process_next_script()
        elif self.current_script_index >= len(self.current_queue):
            self.log_message("✅ 队列执行完成")
            self.show_completion_dialog()
        # 如果未开启自动继续，则暂停等待用户手动继续（通过 F11）
        else:
            self.queue_paused = True
            self.log_message("队列已暂停，按 Ctrl+Alt+F11 继续执行下一个任务")
    
    def process_normal_script_item(self, item):
        try:
            if isinstance(item, dict):
                script_name = item.get("name")
                repeat = int(item.get("repeat", 1))
            else:
                script_name, repeat, img_path, threshold = item
            
            self.log_message(f"执行脚本：{script_name} x{repeat}")
            
            events = self.script_manager.load_script(script_name)
            
            self.player.current_script = script_name
            self.player.play_events(
                events,
                speed=max(1, int(self.speed_var.get())),
                repeat=max(1, int(repeat)),
                progress_callback=lambda inc: self.update_total_progress(inc)
            )
            
            self.log_message(f"脚本执行完成：{script_name}")
            
            self.current_script_index += 1
            
            if self.auto_continue_var.get() and self.current_script_index < len(self.current_queue):
                self.process_next_script()
            elif self.current_script_index >= len(self.current_queue):
                self.log_message("✅ 队列执行完成")
                self.show_completion_dialog()
        
        except Exception as e:
            self.log_message(f"执行脚本失败：{str(e)}")
    
    def process_script_group(self, item):
        group_name = item.get("name")
        try:
            group = self.script_group_manager.load_group(group_name)
            scripts = group.get("scripts", [])
            notice = group.get("notice", "")
            
            if not scripts:
                self.log_message(f"脚本组为空：{group_name}")
                return
            
            # 首次执行该组时显示组级别提醒
            if not self.active_group_session or self.active_group_session.get("name") != group_name:
                ok = self.show_group_notice_dialog(group_name, notice)
                if not ok:
                    self.log_message(f"用户取消执行脚本组：{group_name}")
                    return
                self.active_group_session = {
                    "name":    group_name,
                    "scripts": scripts,
                    "index":   0
                }
            
            session = self.active_group_session
            
            while session["index"] < len(session["scripts"]):
                script_item = session["scripts"][session["index"]]
                script_name = script_item.get("name")
                script_notice = script_item.get("notice", "")
                
                # 显示当前脚本的前置提醒（如果有）
                action = "confirm"  # 默认执行
                if script_notice:
                    action = self.show_script_notice_dialog(script_name, script_notice)
                
                # 处理动作
                if action == "confirm":
                    # 执行当前脚本
                    self.log_message(
                        f"执行脚本组 [{group_name}] 第 {session['index'] + 1}/{len(session['scripts'])} 个脚本：{script_name}")
                    events = self.script_manager.load_script(script_name)
                    self.player.current_script = script_name
                    self.player.play_events(
                        events,
                        speed=max(1, int(self.speed_var.get())),
                        repeat=1,
                        progress_callback=lambda inc: self.update_total_progress(inc)
                    )
                    self.log_message(f"脚本组内脚本执行完成：{script_name}")
                    # 执行完成后索引+1
                    session["index"] += 1
                
                elif action == "skip":
                    # 跳过当前脚本，索引+1
                    self.log_message(f"用户跳过脚本：{script_name}")
                    session["index"] += 1
                    # 如果自动继续，则继续循环；否则暂停等待
                    if not self.auto_continue_var.get():
                        if session["index"] < len(session["scripts"]):
                            self.log_message("脚本组已暂停，按 Ctrl+Alt+F11 继续执行下一个脚本")
                            return
                        else:
                            break
                
                else:  # cancel
                    # 用户选择停止执行，终止整个脚本组
                    self.log_message(f"用户停止执行脚本组：{group_name}")
                    self.active_group_session = None
                    self.current_script_index += 1  # 跳过该组，继续队列下一项（或结束队列）
                    if self.current_script_index < len(self.current_queue):
                        self.process_next_script()
                    else:
                        self.log_message("✅ 队列执行完成")
                        self.show_completion_dialog()
                    return
                
                # 手动继续模式：执行完或跳过脚本后，若还有后续，暂停等待
                if not self.auto_continue_var.get() and session["index"] < len(session["scripts"]):
                    self.log_message("脚本组已暂停，按 Ctrl+Alt+F11 继续执行下一个脚本")
                    return
            
            # 组内所有脚本执行完成
            self.log_message(f"✅ 脚本组执行完成：{group_name}")
            self.active_group_session = None
            self.current_script_index += 1
            if self.auto_continue_var.get() and self.current_script_index < len(self.current_queue):
                self.process_next_script()
            elif self.current_script_index >= len(self.current_queue):
                self.log_message("✅ 队列执行完成")
                self.show_completion_dialog()
        except Exception as e:
            self.log_message(f"执行脚本组失败：{group_name} - {str(e)}")
    
    def process_mixed_group(self, item):
        """执行混合组（按步骤顺序执行）"""
        group_name = item.get("name")
        if not group_name:
            self.log_message("混合组名称为空")
            self.current_script_index += 1
            self.continue_or_finish()
            return
        
        try:
            group = self.mixed_group_manager.load_group(group_name)
            steps = group.get("steps", [])
            notice = group.get("notice", "")
            
            if not steps:
                self.log_message(f"混合组为空：{group_name}")
                self.current_script_index += 1
                self.continue_or_finish()
                return
            
            # 用局部变量取当前会话，避免对 None 调用 .get
            session = self._mixed_group_session
            if session is None or session.get("name") != group_name:
                ok = self.show_group_notice_dialog(group_name, notice)
                if not ok:
                    self.log_message(f"用户取消执行混合组：{group_name}")
                    self._mixed_group_session = None
                    self.current_script_index += 1
                    self.continue_or_finish()
                    return
                session = {
                    "name":  group_name,
                    "steps": steps,
                    "index": 0
                }
                self._mixed_group_session = session
            
            while session["index"] < len(session["steps"]):
                step = session["steps"][session["index"]]
                success, auto_continue = self._execute_mixed_step(
                    step, group_name, session["index"] + 1, len(steps)
                )
                if not success:
                    self.log_message(f"混合组 {group_name} 执行中断")
                    self._mixed_group_session = None
                    self.current_script_index += 1
                    self.continue_or_finish()
                    return
                
                # 步骤设置为手动继续，则暂停
                if not auto_continue and session["index"] < len(session["steps"]) - 1:
                    self.log_message("混合组当前步骤设置为手动继续，已暂停")
                    self.queue_paused = True
                    return
                
                session["index"] += 1
                
                # 未开启全局自动继续，且还有后续步骤，则暂停等待
                if not self.auto_continue_var.get() and session["index"] < len(session["steps"]):
                    self.log_message("混合组已暂停，按 Ctrl+Alt+F11 继续执行下一步")
                    self.queue_paused = True
                    return
            
            # 所有步骤完成
            self.log_message(f"✅ 混合组执行完成：{group_name}")
            self._mixed_group_session = None
            self.current_script_index += 1
            self.continue_or_finish()
        
        except Exception as e:
            self.log_message(f"执行混合组失败：{group_name} - {str(e)}")
            self._mixed_group_session = None
            self.current_script_index += 1
            self.continue_or_finish()
    
    def _execute_mixed_step(self, step, group_name, step_num, total_steps):
        """执行单个步骤，返回 (是否成功, 是否自动继续)"""
        step_type = step.get("type")
        wait_seconds = step.get("wait_seconds", 0.0)
        auto_continue = step.get("auto_continue", True)
        self.log_message(
            f"混合组 [{group_name}] 执行步骤 {step_num}/{total_steps}：{step_type} - {step.get('name', '')}")
        
        success = False
        
        if step_type == "script":
            script_name = step.get("name")
            if not script_name:
                self.log_message("步骤缺少脚本名")
                return False, False
            events = self.script_manager.load_script(script_name)
            if not events:
                self.log_message(f"脚本不存在或为空：{script_name}")
                return False, False
            self.player.current_script = script_name
            self.player.play_events(
                events,
                speed=max(1, int(self.speed_var.get())),
                repeat=1,
                progress_callback=lambda inc: self.update_total_progress(inc)
            )
            self.log_message(f"脚本执行完成：{script_name}")
            success = True
        
        elif step_type == "script_group":
            sg_name = step.get("name")
            if not sg_name:
                self.log_message("步骤缺少脚本组名")
                return False, False
            self._execute_script_group_internal(sg_name)
            success = True
        
        elif step_type == "screenshot_group":
            sg_name = step.get("name")
            if not sg_name:
                self.log_message("步骤缺少截图组名")
                return False, False
            if not self.screenshot_engine.set_active_group(sg_name):
                self.log_message(f"切换截图组失败：{sg_name}")
                return False, False
            if not self.screenshot_engine.tasks:
                self.log_message(f"截图组为空：{sg_name}")
                return False, False
            self.screenshot_engine.start()
            while self.screenshot_engine.running:
                if self.player.should_stop:
                    self.screenshot_engine.stop(log=False)
                    return False, False
                time.sleep(0.1)
            self.log_message(f"截图组执行完成：{sg_name}")
            success = True
        
        elif step_type == "screenshot_task":
            g_name = step.get("group_name")
            task_idx = step.get("task_index")
            if g_name is None or task_idx is None:
                self.log_message("步骤缺少截图任务组名或索引")
                return False, False
            try:
                success_flag = self.screenshot_engine.run_single_task(g_name, task_idx)
                if success_flag:
                    self.log_message("截图任务执行完成")
                else:
                    self.log_message("截图任务执行失败（可能未找到匹配）")
                success = True  # 不管匹配与否，任务执行完毕
            except Exception as e:
                self.log_message(f"执行截图任务出错：{str(e)}")
                return False, False
        
        else:
            self.log_message(f"未知步骤类型：{step_type}")
            return False, False
        
        # 执行成功后等待指定时间
        if success and wait_seconds > 0:
            self.log_message(f"等待 {wait_seconds} 秒...")
            time.sleep(wait_seconds)
        
        return success, auto_continue
    
    def _execute_script_group_internal(self, group_name):
        """内部执行脚本组（不依赖队列索引），用于混合组内调用"""
        try:
            group = self.script_group_manager.load_group(group_name)
            scripts = group.get("scripts", [])
            if not scripts:
                self.log_message(f"脚本组为空：{group_name}")
                return
            # 执行每个脚本
            for idx, script_item in enumerate(scripts, 1):
                if self.player.should_stop:
                    break
                script_name = script_item.get("name") if isinstance(script_item, dict) else script_item
                events = self.script_manager.load_script(script_name)
                if not events:
                    self.log_message(f"脚本组内脚本不存在或为空：{script_name}")
                    continue
                self.player.current_script = script_name
                self.player.play_events(
                    events,
                    speed=max(1, int(self.speed_var.get())),
                    repeat=1,
                    progress_callback=lambda inc: self.update_total_progress(inc)
                )
                self.log_message(f"脚本组内脚本执行完成：{script_name}")
                # 如果未开启自动继续，在混合组内我们不暂停，因为混合组本身会控制暂停
        except Exception as e:
            self.log_message(f"执行脚本组 {group_name} 出错：{str(e)}")
    
    def safe_start_execution(self):
        self.after(0, self.start_execution_by_f11)
    
    def start_execution_by_f11(self):
        if self.player.is_playing:
            self.log_message("当前已有脚本正在执行，请等待完成")
            return
        
        threading.Thread(target=self.process_next_script, daemon=True).start()
    
    def safe_stop_execution(self):
        """线程安全的停止执行方法"""
        self.after(0, self.stop_execution)
    
    def safe_toggle_recording(self):
        self.after(0, self.toggle_recording)
    
    def safe_stop_recording(self):
        self.after(0, self.stop_recording)
    
    def toggle_recording(self):
        if not self.recorder.is_recording:
            self.log_message("开始录制脚本")  #
            self.recorder.start_recording()
            self.show_info("提示", "开始录制...")
        else:
            self.stop_recording()
    
    def stop_recording(self):
        events = self.recorder.stop_recording()
        if events:
            script_name = self.ask_string("输入", "请输入脚本名称：")
            if script_name and script_name.strip() != "":
                self.script_manager.save_script(script_name, events)
                self.refresh_script_list()
                self.log_message(f"成功保存脚本：{script_name}")  # 新增日志
                self.show_info("提示", "录制保存成功！")
            else:
                self.log_message("用户取消了脚本保存")
    
    def show_info(self, title, message):
        """显示自动关闭的提示窗口"""
        top = tk.Toplevel(self)
        self._set_window_icon(top)  # 设置图标
        top.title(title)
        # 居中窗口
        top.geometry("+%d+%d" % (self.winfo_x() + 300, self.winfo_y() + 200))
        label = tk.Label(top, text=message, padx=20, pady=10)
        label.pack()
        top.after(2000, top.destroy)
    
    def ask_string(self, title, prompt):
        return simpledialog.askstring(title, prompt)
    
    def set_script_image(self):
        """为脚本设置关联的图片"""
        selected = self.script_list.curselection()
        if not selected:
            messagebox.showinfo("提示", "请先选择一个脚本")
            return
        
        script_name = self.script_list.get(selected[0])
        file_path = filedialog.askopenfilename(
            title="选择参考图像",
            filetypes=[("Image files", "*.png;*.jpg;*.jpeg;*.bmp")]
        )
        
        if not file_path:
            return
        
        try:
            # 打开图片并保存
            img = Image.open(file_path)
            saved_path = self.script_manager.save_image(script_name, img)
            self.log_message(f"为脚本 '{script_name}' 设置图片: {saved_path}")
            messagebox.showinfo("成功", f"已为脚本 '{script_name}' 设置图片")
        except Exception as e:
            messagebox.showerror("错误", f"设置图片失败: {str(e)}")
    
    def add_to_queue(self):
        selected = self.script_list.curselection()
        if selected:
            script_name = self.script_list.get(selected[0])
            
            img_path = self.script_manager.get_image_path(script_name)
            if not img_path:
                img_path = "无图片"
            
            repeat = simpledialog.askinteger("重复次数", "请输入重复次数：", minvalue=1)
            if repeat:
                threshold = simpledialog.askfloat("匹配阈值", "请输入图像匹配阈值(0.1-0.9):",
                                                  minvalue=0.1, maxvalue=0.9, initialvalue=0.5)
                if threshold is None:
                    threshold = 0.5
                
                # ---------- 如果队列为空，重置会话 ----------
                if not self.current_queue:
                    self.active_group_session = None
                    self.current_script_index = 0
                # -----------------------------------------
                
                self.current_queue.append((script_name, repeat, img_path, threshold))
                self.queue_list.insert(tk.END, f"{script_name} (x{repeat})")
    
    def remove_from_queue(self):
        selected = self.queue_list.curselection()
        if selected:
            index = selected[0]  # 添加[0]
            self.queue_list.delete(index)
            del self.current_queue[index]
    
    def clear_queue(self):
        """清空队列前停止执行，并重置所有执行状态"""
        # 停止当前执行
        if self.player.is_playing or self.player.paused:
            self.stop_execution()
        
        # 清空队列数据
        self.queue_list.delete(0, tk.END)
        self.current_queue = []
        
        # 重置执行状态（关键）
        self.active_group_session = None
        self.current_script_index = 0
        self.processed_events = 0
        self.total_events = 0
        self.queue_paused = False
        self.completed = False
        self.player.should_stop = False
        self.player.paused = False
        
        self._mixed_group_session = None
        
        self.log_message("队列已清空，执行状态已重置")
    
    def move_queue_item(self, direction):
        selected = self.queue_list.curselection()
        if selected:
            index = selected
            if 0 <= index + direction < self.queue_list.size():
                item = self.queue_list.get(index)
                script_data = self.current_queue[index]
                
                self.queue_list.delete(index)
                del self.current_queue[index]
                
                new_index = index + direction
                self.queue_list.insert(new_index, item)
                self.current_queue.insert(new_index, script_data)
                self.queue_list.select_set(new_index)
    
    def rename_script(self):
        selected = self.script_list.curselection()
        if selected:
            old_name = self.script_list.get(selected)
            new_name = simpledialog.askstring("重命名", "请输入新名称：", initialvalue=old_name)
            if new_name and new_name != old_name:
                try:
                    # 重命名脚本文件
                    os.rename(
                        os.path.join(self.script_manager.save_dir, f"{old_name}.json"),
                        os.path.join(self.script_manager.save_dir, f"{new_name}.json")
                    )
                    
                    # 重命名图片文件
                    old_img = os.path.join(self.script_manager.img_dir, f"{old_name}.png")
                    new_img = os.path.join(self.script_manager.img_dir, f"{new_name}.png")
                    if os.path.exists(old_img):
                        os.rename(old_img, new_img)
                    
                    self.refresh_script_list()
                    self.log_message(f"原脚本名称‘{old_name}’""更改为→"f"‘{new_name}’")
                except Exception as e:
                    messagebox.showerror("错误", str(e))
    
    def delete_script(self):
        selected = self.script_list.curselection()
        if selected:
            script_name = self.script_list.get(selected)
            if messagebox.askyesno("确认", f"确定删除脚本 {script_name} 吗？"):
                self.script_manager.delete_script(script_name)
                self.refresh_script_list()
                self.log_message(f"删除脚本：{script_name}")
    
    def start_execution(self):
        self.log_message(f"启动执行引擎 | 队列长度：{len(self.current_queue)}")
        self.status_label.config(text="执行中...")
        self.total_events = self.calculate_total_events()
        self.processed_events = 0
        self.update_progress(0)
        
        # 精确计算总时间
        try:
            speed = self.speed_var.get()
            if speed < 1:
                speed = 1
            total_seconds = self.calculate_total_time(speed)
            formatted_time = self.format_duration(total_seconds)
        except Exception as e:
            self.log_message(f"时间计算失败: {str(e)}")
            formatted_time = "N/A"
        
        self.show_info("开始执行",
                       f"开始执行脚本: {len(self.current_queue)} 个\n"
                       f"速度: {speed} 倍速\n"
                       f"预计时间: {formatted_time}")
        
        if not self.player.is_playing and self.current_queue:
            # 重置执行状态
            self.current_script_index = 0
            self.queue_paused = False
            self.active_group_session = None
            self._mixed_group_session = None
            self.player.should_stop = False
            self.player.paused = False
            self.completed = False
            
            # 直接使用 process_next_script 处理队列（支持字典格式、脚本组、截图组、混合组）
            threading.Thread(target=self.process_next_script, daemon=True).start()
    
    def image_detection_loop(self):
        """图像检测循环"""
        # self.log_message("图像检测线程启动")
        last_detected = None
        
        while self.player.is_playing or not self.completed:
            try:
                # 获取当前队列项
                current_item = None
                if self.current_script_index < len(self.current_queue):
                    current_item = self.current_queue[self.current_script_index]
                
                # 检查是否有匹配
                if self.image_matcher.match_found.is_set():
                    self.image_matcher.match_found.clear()
                    
                    # 获取匹配目标
                    target = self.image_matcher.current_target
                    if not target:
                        continue
                    
                    # 查找匹配的脚本
                    matched_script = None
                    for script_name, repeat, img_path, threshold in self.current_queue:
                        if img_path == target['path']:
                            matched_script = (script_name, repeat, img_path, threshold)
                            break
                    
                    if matched_script:
                        # 检查是否是当前正在执行的脚本
                        if current_item and current_item[0] == matched_script[0]:
                            self.log_message(f"检测到当前脚本图像: {matched_script[0]}, 继续执行")
                            continue
                        
                        # 查找匹配脚本在队列中的位置
                        matched_index = None
                        for idx, item in enumerate(self.current_queue):
                            if item[0] == matched_script[0]:
                                matched_index = idx
                                break
                        
                        if matched_index is not None:
                            # 中断当前脚本
                            self.log_message(
                                f"检测到匹配图像，中断当前脚本执行: {current_item[0] if current_item else '无'}")
                            self.player.interrupt()
                            
                            # 等待当前脚本停止
                            while self.player.is_playing:
                                time.sleep(0.05)
                            
                            # 设置当前脚本索引
                            self.current_script_index = matched_index
                            
                            # 执行匹配的脚本
                            script_name, repeat, img_path, threshold = matched_script
                            self.log_message(f"开始执行匹配脚本: {script_name}")
                            
                            # 更新图像匹配目标
                            self.image_matcher.set_target(img_path, threshold)
                            
                            # 执行脚本
                            self.execute_single_script(script_name, 1, self.speed_var.get())
                            
                            # 更新索引
                            self.current_script_index += 1
                            
                            last_detected = time.time()
                
                time.sleep(0.1)  # 避免过高CPU占用
            except Exception as e:
                self.log_message(f"图像检测错误: {str(e)}")
                time.sleep(1)
        
        # self.log_message("图像检测线程停止")
    
    def process_queue(self, speed):
        """处理整个执行队列（增强版）"""
        # 确保之前的执行完全停止
        if self.player.is_playing or self.player.paused:
            self.stop_execution()
        
        # 检查队列是否为空
        if not self.current_queue:
            self.log_message("错误：队列为空，无法执行")
            return
        
        self.completed = False
        self.processed_events = 0
        self.total_events = self.calculate_total_events()
        self.update_progress(0)
        
        # 重置当前脚本索引
        self.current_script_index = 0
        
        # 开始处理队列
        while self.current_script_index < len(self.current_queue) and not self.player.should_stop:
            script_name, repeat, img_path, threshold = self.current_queue[self.current_script_index]
            
            # 设置当前匹配目标
            # if img_path and os.path.exists(img_path):
            #     self.image_matcher.set_target(img_path, threshold)
            #     self.log_message(f"设置匹配目标: {script_name}, 阈值: {threshold}")
            # else:
            #     self.log_message(f"警告: 脚本 '{script_name}' 没有关联的图像，跳过图像检测")
            
            try:
                events = self.script_manager.load_script(script_name)
                self.log_message(f"执行脚本: {script_name} (×{repeat})")
                
                # 设置当前脚本名称
                self.player.current_script = script_name
                
                # 执行单个脚本
                self.player.play_events(
                    events,
                    speed,
                    repeat,
                    lambda inc: self.update_total_progress(inc)
                )
                
                # 自动继续逻辑
                if self.auto_continue_var.get() or self.current_script_index < len(self.current_queue) - 1:
                    self.current_script_index += 1
                else:
                    self.ask_continue()
                    while self.queue_paused and not self.player.should_stop:
                        time.sleep(0.1)
            
            except Exception as e:
                self.log_message(f"执行失败: {str(e)}")
                self.stop_execution()
                return
        
        if not self.player.should_stop:
            self.log_message("✅ 所有脚本执行完毕")
            self.update_progress(100, f"({self.total_events}/{self.total_events})")
            self.completed = True
            self.show_completion_dialog()
            self.image_matcher.stop()
    
    def show_completion_dialog(self):
        """显示完成提示"""
        self.status_label.config(text="执行完成")  # 新增状态更新
        if hasattr(self, 'completion_dialog') and self.completion_dialog.winfo_exists():
            self.completion_dialog.destroy()
        
        self.completion_dialog = tk.Toplevel(self)
        self._set_window_icon(self.completion_dialog)  # 设置图标
        self.completion_dialog.title("执行完成")
        self.completion_dialog.geometry(f"+{self.winfo_x() + 300}+{self.winfo_y() + 200}")
        label = tk.Label(self.completion_dialog, text="所有脚本已执行完毕！", padx=20, pady=10)
        label.pack()
        self.completion_dialog.after(3000, self.completion_dialog.destroy)
    
    def execute_single_script(self, script_name, repeat, speed):
        try:
            events = self.script_manager.load_script(script_name)
            if not events:
                self.log_message(f"脚本为空：{script_name}")
                return
            
            repeat = max(1, int(repeat))
            # 进度基准只统计本脚本自身的事件数（避免进度条瞬间冲到 100%）
            self.total_events = len(events) * repeat
            self.processed_events = 0
            self.update_progress(0, f"(0/{self.total_events})")
            
            # 设置当前脚本名称
            self.player.current_script = script_name
            
            # 执行脚本：每个事件回调 1，与 total_events 口径一致
            self.player.play_events(events, speed, repeat,
                                    lambda increment: self.update_total_progress(increment))
            
            if not self.player.should_stop:
                self.update_progress(100, f"({self.total_events}/{self.total_events})")
        
        except Exception as e:
            self.log_message(f"执行失败: {str(e)}")
            self.stop_execution()
    
    def auto_continue_next(self):
        """自动继续执行下一个脚本"""
        self.current_script_index += 1
        if self.current_script_index < len(self.current_queue):
            self.process_next_script()
        else:
            self.show_completion_dialog()
    
    def ask_continue(self):
        """询问是否继续执行"""
        dialog = tk.Toplevel(self)
        self._set_window_icon(dialog)  # 设置图标
        dialog.title("继续执行")
        dialog.geometry(f"+{self.winfo_x() + 200}+{self.winfo_y() + 200}")
        
        # 对话框内容
        label = tk.Label(dialog, text="是否继续执行下一个脚本？", padx=20, pady=10)
        label.pack(pady=10)
        
        button_frame = tk.Frame(dialog)
        button_frame.pack(pady=10)
        
        tk.Button(button_frame, text="是", command=lambda: self._continue_execution(dialog)).pack(side=tk.LEFT, padx=10)
        tk.Button(button_frame, text="否", command=dialog.destroy).pack(side=tk.LEFT, padx=10)
        
        dialog.transient(self)
        dialog.grab_set()
        self.wait_window(dialog)
    
    def _continue_execution(self, dialog):
        dialog.destroy()
        self.queue_paused = False
        self.process_next_script()
    
    def stop_execution(self):
        """完全停止执行并重置所有状态（同时停止截图流程）"""
        # 停止截图引擎（如果正在运行）
        if self.screenshot_engine.running:
            self.screenshot_engine.stop(log=False)
            self.log_message("已停止截图流程")
        
        self.player.should_stop = True
        self.player.paused = False
        self.player.interrupt_flag = True
        self._mixed_group_session = None  # 重置混合组会话
        self.queue_paused = False
        self.current_script_index = 0
        self.processed_events = 0
        self.total_events = 0
        self.image_matcher.stop()
        
        self.update_progress(0)
        self.log_message("执行已完全停止")
        self.status_label.config(text="已停止")
        try:
            self.after(0, lambda: self.progress_label.config(text="已停止"))
        except Exception:
            pass
        
        # 强制终止可能卡住的线程
        if hasattr(self.player, '_thread') and self.player._thread.is_alive():
            try:
                # 先尝试正常停止
                if hasattr(self.player, 'pause_condition'):
                    with self.player.pause_condition:
                        self.player.pause_condition.notify_all()
                self.player._thread.join(timeout=0.5)
            except RuntimeError:
                pass
        
        # 确保播放器状态重置
        self.player.is_playing = False
        self.player.paused = False
        self.player.should_stop = False
        
        # 更新UI
        self.after(0, self.progress.update)
    
    def open_task_manager(self):
        """打开定时任务管理窗口（只允许同时存在一个）"""
        win = getattr(self, "task_manager_dialog", None)
        try:
            if win is not None and win.winfo_exists():
                win.deiconify()
                win.lift()
                win.focus_set()
                return
        except Exception:
            pass
        self.task_manager_dialog = TaskManagementDialog(self, self.scheduler)
    
    # ---------- 按键触发（截图顺序识别点击） ----------
    def set_synthetic_input(self, active):
        """标记/取消标记“软件自己发出的按键”，用于过滤按键触发监听。"""
        if active:
            self.synthetic_input_depth = getattr(self, "synthetic_input_depth", 0) + 1
        else:
            self.synthetic_input_depth = max(0, getattr(self, "synthetic_input_depth", 0) - 1)
        # 钩子回调可能稍有延迟，留一点静默时间，避免自己发出的按键误触发
        self.synthetic_input_until = time.time() + 0.3
    
    def _trigger_key_token(self, key):
        """把 pynput 按键转成统一的比较用名称（左右修饰键合并）。"""
        try:
            ch = key.char
            if ch:
                return ch.lower()
        except Exception:
            pass
        name = str(key)
        if name.startswith("Key."):
            name = name[4:]
        mapping = {
            "ctrl_l": "ctrl", "ctrl_r": "ctrl",
            "shift_l": "shift", "shift_r": "shift",
            "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
            "cmd_l": "cmd", "cmd_r": "cmd",
        }
        return mapping.get(name, name.lower())
    
    def _trigger_key_listener(self):
        from pynput import keyboard as kb
        
        def on_press(key):
            if not self.trigger_enabled:
                return
            # 软件自身模拟的按键不算
            if getattr(self, "synthetic_input_depth", 0) > 0:
                return
            if time.time() < getattr(self, "synthetic_input_until", 0):
                return
            token = self._trigger_key_token(key)
            self.trigger_pressed.add(token)
            if self.trigger_fired:
                return
            for step in self.trigger_steps:
                if set(step).issubset(self.trigger_pressed):
                    self.trigger_fired = True
                    self.after(0, self._on_trigger_key_fired)
                    break
        
        def on_release(key):
            token = self._trigger_key_token(key)
            self.trigger_pressed.discard(token)
            if not any(set(step).issubset(self.trigger_pressed) for step in self.trigger_steps):
                self.trigger_fired = False
        
        return kb.Listener(on_press=on_press, on_release=on_release)
    
    def enable_trigger_key(self):
        if not hasattr(self, "trigger_key_var"):
            return
        combo = self.trigger_key_var.get().strip()
        if not combo:
            messagebox.showerror("按键触发", "请先填写触发按键，例如 ctrl+alt+1 或 f8", parent=self)
            return
        steps = parse_key_sequence(combo)
        if not steps:
            messagebox.showerror(
                "按键触发",
                f"无法识别按键「{combo}」。\n可用写法：enter、f8、ctrl+alt+1、ctrl+shift+s",
                parent=self)
            return
        # 只支持“同时按下”的组合，取第一步
        tokens = []
        for key in steps[0]:
            tokens.append(self._trigger_key_token(key))
        if not tokens:
            messagebox.showerror("按键触发", "请填写有效的触发按键", parent=self)
            return
        self.trigger_steps = [tokens]
        self.trigger_pressed = set()
        self.trigger_fired = False
        self.trigger_enabled = True
        try:
            if self.trigger_listener is not None:
                self.trigger_listener.stop()
        except Exception:
            pass
        self.trigger_listener = self._trigger_key_listener()
        self.trigger_listener.start()
        if len(tokens) == 1:
            self.log_message(
                f"按键触发已启用：{combo}（单个按键在任何地方按下都会触发，建议用组合键）")
        else:
            self.log_message(f"按键触发已启用：{combo}")
        if hasattr(self, "trigger_toggle_btn"):
            self.trigger_toggle_btn.config(text="停用按键触发")
        if hasattr(self, "trigger_status_var"):
            self.trigger_status_var.set(f"已启用：{combo}（按一次即从头执行）")
        self.app_settings["screenshot_trigger_key"] = combo
        self.app_settings["screenshot_trigger_enabled"] = True
        save_app_settings(self.app_settings)
    
    def disable_trigger_key(self):
        self.trigger_enabled = False
        try:
            if self.trigger_listener is not None:
                self.trigger_listener.stop()
        except Exception:
            pass
        self.trigger_listener = None
        if hasattr(self, "trigger_toggle_btn"):
            self.trigger_toggle_btn.config(text="启用按键触发")
        if hasattr(self, "trigger_status_var"):
            self.trigger_status_var.set("未启用")
        self.log_message("按键触发已停用")
        self.app_settings["screenshot_trigger_enabled"] = False
        save_app_settings(self.app_settings)
    
    def toggle_trigger_key(self):
        if getattr(self, "trigger_enabled", False):
            self.disable_trigger_key()
        else:
            self.enable_trigger_key()
    
    def _on_trigger_key_fired(self):
        combo = self.trigger_key_var.get().strip() if hasattr(self, "trigger_key_var") else ""
        engine = self.screenshot_engine
        group_name = engine.active_group_name
        self.log_message(f"检测到按键 [{combo}]：截图组 [{group_name}] 从头开始执行")
        self.update_screenshot_mode_status(f"按键触发：从头执行 [{group_name}]")
        engine.trigger_restart()
    
    def set_screenshot_step_timeout(self, minutes):
        """设置单步防死机超时（分钟，0 = 关闭保护）。"""
        try:
            minutes = float(minutes)
        except Exception:
            minutes = 5.0
        minutes = max(0.0, min(120.0, minutes))
        self.screenshot_step_timeout_minutes = minutes
        self.screenshot_engine.step_timeout_seconds = minutes * 60
        self.app_settings["screenshot_step_timeout_minutes"] = minutes
        save_app_settings(self.app_settings)
        if minutes <= 0:
            self.log_message("已关闭单步超时保护")
        else:
            self.log_message(f"单步超时保护：{minutes:g} 分钟未成功则自动停止当前任务")
    
    def _apply_screenshot_timeout_from_ui(self):
        try:
            minutes = int(self.screenshot_timeout_var.get())
        except Exception:
            minutes = 5
        minutes = max(0, min(120, minutes))
        self.screenshot_timeout_var.set(minutes)
        self.set_screenshot_step_timeout(minutes)
    
    def set_scheduler_countdown(self, value):
        """设置定时任务触发前的默认倒计时秒数（0 = 不提示）。"""
        try:
            value = int(float(value))
        except Exception:
            value = DEFAULT_SCHEDULER_COUNTDOWN
        self.scheduler_countdown = max(0, min(60, value))
        self.app_settings["scheduler_countdown"] = self.scheduler_countdown
        if save_app_settings(self.app_settings):
            self.log_message(f"定时任务启动倒计时默认值已设为 {self.scheduler_countdown} 秒")
        else:
            self.log_message("保存倒计时设置失败（不影响本次使用）")
    
    def on_close(self):
        """确保程序完全退出"""
        # 停止按键触发监听
        try:
            if getattr(self, "trigger_listener", None) is not None:
                self.trigger_listener.stop()
        except Exception:
            pass
        # 停止定时调度器
        self.scheduler.stop()
        # 同步保存定时任务
        self.scheduler.save_tasks_sync()
        
        self.screenshot_engine.stop(log=False)
        
        # 停止所有正在运行的操作
        self.player.should_stop = True
        if self.recorder.is_recording:
            self.recorder.stop_recording()
        
        # 停止图像匹配
        self.image_matcher.stop()
        
        # 停止热键监听器
        if self.hotkey_listener:
            self.hotkey_listener.stop()
        
        # 关闭所有线程
        if hasattr(self.player, '_thread') and self.player._thread.is_alive():
            self.player._thread.join(timeout=0.5)
        
        # 销毁窗口
        self.destroy()
        
        # 强制退出程序
        sys.exit(0)


if __name__ == "__main__":
    app = AutomationApp()
    app.mainloop()