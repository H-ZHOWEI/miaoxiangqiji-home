# -*- coding: utf-8 -*-
"""
喵想奇记 - 手机联动模块
=====================================================================
职责：让电脑软件通过「云端信箱」与手机 App 双向通信。

  * 电脑 -> 手机：任务开始/完成/失败/停止时写入一条通知消息
  * 手机 -> 电脑：手机下发「执行某任务组」指令，电脑轮询读到后执行

云端信箱是一张 PostgreSQL 表（WorkBuddy 云服务托管），通过纯 HTTP REST
读写，不依赖任何 JS SDK。手机 App 与电脑用同一「配对码」隔离，只有配对码
相同的两端才会互相看到消息。

设计原则：
  - 完全解耦：本模块不直接操作 UI，所有回调通过传入的 app 对象方法完成
  - 线程安全：网络轮询在后台线程，UI 操作一律经 app.after(0, ...)
  - 失败静默：网络异常只写日志、重试，绝不影响自动化主流程
"""
import json
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

# 云端信箱 REST 数据面（WorkBuddy 云服务「喵想联动」应用）
CLOUD_ENDPOINT = "https://wbapp-gp4ndbcf8opv7nyrqcfllg.app.workbuddy.host"
REST_BASE = CLOUD_ENDPOINT + "/.cloud/database/rest/v1"
ACCESS_KEY = "wbpk_gp4ndbCF8Opv7NYrQcfLlG_2zil329GWxaMLCznWPjTPICBb0JFzsLU"
TABLE = "link_messages"

# 方向常量
DIR_PC2PHONE = "pc2phone"     # 电脑写给手机（通知 / 状态）
DIR_PHONE2PC = "phone2pc"     # 手机写给电脑（指令）

# 消息类型
MSG_TASK_DONE = "task_done"       # 任务正常完成
MSG_TASK_FAILED = "task_failed"   # 任务失败/异常
MSG_TASK_START = "task_start"     # 任务开始
MSG_TASK_STOP = "task_stop"       # 任务被手动停止
MSG_COMMAND = "command"           # 手机下发的执行指令
MSG_ACK = "ack"                   # 电脑对指令的回执
MSG_HEARTBEAT = "heartbeat"       # 电脑在线心跳
MSG_GROUP_LIST = "group_list"     # 电脑上报的可执行任务组清单（手机据此展示）

# 设置键（存进 app_settings.json）
SETTING_ENABLED = "phone_link_enabled"
SETTING_PAIR_CODE = "phone_link_pair_code"
SETTING_POLL_SEC = "phone_link_poll_sec"


def _headers():
    return {
        "x-wb-webapp-access-key": ACCESS_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _req(method, path, body=None, timeout=15):
    """底层 HTTP 请求，返回 (ok, data)。"""
    url = REST_BASE + path
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return True, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        return False, "HTTP %s: %s" % (e.code, detail[:300])
    except Exception as e:
        return False, str(e)


class CloudMailbox:
    """云端信箱客户端：对 link_messages 表做增删改查。"""

    def __init__(self, pair_code):
        self.pair_code = (pair_code or "").strip()

    def send(self, direction, msg_type, title="", content="", payload=None):
        """写入一条消息。payload 为 dict，自动序列化为 JSON 字符串。"""
        if not self.pair_code:
            return False, "未设置配对码"
        row = {
            "pair_code": self.pair_code,
            "direction": direction,
            "msg_type": msg_type,
            "title": str(title or "")[:200],
            "content": str(content or "")[:2000],
            "payload": json.dumps(payload or {}, ensure_ascii=False),
            "is_read": False,
        }
        return _req("POST", "/" + TABLE, row)

    def fetch_unread(self, direction, limit=30):
        """取指定方向未读消息（按时间升序）。"""
        if not self.pair_code:
            return True, []
        q = urllib.parse.urlencode({
            "pair_code": "eq." + self.pair_code,
            "direction": "eq." + direction,
            "is_read": "eq.false",
            "select": "*",
            "order": "created_at.asc",
            "limit": str(limit),
        })
        return _req("GET", "/" + TABLE + "?" + q)

    def mark_read(self, msg_id):
        return _req("PATCH", "/" + TABLE + "?id=eq.%s" % msg_id, {"is_read": True})

    def delete(self, msg_id):
        return _req("DELETE", "/" + TABLE + "?id=eq.%s" % msg_id)

    def clear_own(self):
        """清空本配对码下的所有消息（用户主动清理时用）。"""
        return _req("DELETE", "/" + TABLE + "?pair_code=eq." +
                    urllib.parse.quote(self.pair_code))

    def get_latest(self, direction, msg_type, limit=10):
        """取某方向某类型的最新消息（不限已读，按时间倒序）。"""
        q = urllib.parse.urlencode({
            "pair_code": "eq." + self.pair_code,
            "direction": "eq." + direction,
            "msg_type": "eq." + msg_type,
            "select": "*",
            "order": "created_at.desc",
            "limit": str(limit),
        })
        return _req("GET", "/" + TABLE + "?" + q)

    def delete_type(self, direction, msg_type):
        """删除本配对码下某方向某类型的全部消息（用于刷新组清单）。"""
        q = urllib.parse.urlencode({
            "pair_code": "eq." + self.pair_code,
            "direction": "eq." + direction,
            "msg_type": "eq." + msg_type,
        })
        return _req("DELETE", "/" + TABLE + "?" + q)


class PhoneLink:
    """
    电脑端联动控制器。

    用法：
        link = PhoneLink(app, settings_dict)
        link.start()      # 启动后台轮询 + 状态监控
        link.stop()       # 停止
        link.notify_task(...)  # 主动上报（可选，状态监控已覆盖）

    app 需提供：
        app.log_message(str)
        app.after(ms, func, *args)
        app.app_settings  (dict，可写)
        app.player.is_playing / app.screenshot_engine.running
        app.scheduler._automation_busy()  (可选)
    """

    def __init__(self, app, settings):
        self.app = app
        self.settings = settings
        self._stop_event = threading.Event()
        self._thread = None
        self._mailbox = None
        # 任务状态边沿检测
        self._was_busy = False
        self._busy_since = 0.0
        self._busy_label = ""
        # 指令去重（msg_id 集合）
        self._handled_ids = set()
        self._last_heartbeat = 0.0
        self._last_error = ""
        self._online = False
        self._last_group_sync = 0.0

    # ------------------------------------------------------------------ 配置
    @property
    def enabled(self):
        return bool(self.settings.get(SETTING_ENABLED, False))

    @property
    def pair_code(self):
        return str(self.settings.get(SETTING_PAIR_CODE, "") or "").strip()

    @property
    def poll_sec(self):
        try:
            v = float(self.settings.get(SETTING_POLL_SEC, 5) or 5)
        except Exception:
            v = 5.0
        return max(2.0, min(60.0, v))

    def refresh_mailbox(self):
        self._mailbox = CloudMailbox(self.pair_code)

    def save_settings(self):
        """由主程序传入的保存函数（若提供）。"""
        fn = getattr(self.app, "save_phone_link_settings", None)
        if callable(fn):
            fn()

    # ------------------------------------------------------------------ 生命周期
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.refresh_mailbox()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="PhoneLinkPoll")
        self._thread.start()
        self.app.log_message("手机联动：后台轮询已启动（配对码 %s）" %
                             (self.pair_code[:4] + "****" if self.pair_code else "未设置"))

    def stop(self):
        self._stop_event.set()

    def set_enabled(self, on):
        self.settings[SETTING_ENABLED] = bool(on)
        self.save_settings()
        if on:
            self.refresh_mailbox()
        else:
            self._online = False

    def set_pair_code(self, code):
        self.settings[SETTING_PAIR_CODE] = str(code or "").strip().upper()
        self.save_settings()
        self.refresh_mailbox()
        self._handled_ids.clear()
        self._last_group_sync = 0.0  # 换码后立即同步组清单

    def collect_groups(self):
        """收集电脑上可执行的任务组清单。返回 [{type, name, label}]。"""
        groups = []
        app = self.app
        # 截图组
        try:
            for name in app.screenshot_engine.list_groups():
                groups.append({"type": "screenshot_group", "name": name,
                               "label": "截图组"})
        except Exception:
            pass
        # 脚本组
        try:
            for name in app.script_group_manager.list_groups():
                groups.append({"type": "group", "name": name, "label": "脚本组"})
        except Exception:
            pass
        # 混合组
        try:
            for name in app.mixed_group_manager.list_groups():
                groups.append({"type": "mixed_group", "name": name, "label": "混合组"})
        except Exception:
            pass
        # 单脚本
        try:
            for name in app.script_manager.list_scripts():
                groups.append({"type": "script", "name": name, "label": "脚本"})
        except Exception:
            pass
        return groups

    def sync_group_list(self):
        """把当前任务组清单上报到云端（手机据此展示可点选任务）。"""
        if not self.enabled or not self.pair_code:
            return False
        box = self._mailbox or CloudMailbox(self.pair_code)
        groups = self.collect_groups()
        # 先删旧清单，再写新清单，避免手机读到过期分组
        box.delete_type(DIR_PC2PHONE, MSG_GROUP_LIST)
        ok, _ = box.send(DIR_PC2PHONE, MSG_GROUP_LIST, "可执行任务组",
                         "共 %d 个" % len(groups), {"groups": groups})
        if ok:
            self._last_group_sync = time.time()
        return ok

    # ------------------------------------------------------------------ 状态
    def _is_busy(self):
        try:
            if self.app.player.is_playing:
                return True
        except Exception:
            pass
        try:
            if self.app.screenshot_engine.running:
                return True
        except Exception:
            pass
        return False

    def _busy_label_now(self):
        """尽量推断当前正在跑的任务名。"""
        try:
            idx = getattr(self.app, "current_script_index", 0)
            queue = getattr(self.app, "current_queue", [])
            if 0 <= idx < len(queue):
                item = queue[idx]
                if isinstance(item, dict):
                    name = item.get("name") or item.get("display") or ""
                    t = item.get("type", "")
                    return name or t
            if getattr(self.app.screenshot_engine, "running", False):
                return self.app.screenshot_engine.active_group_name or "截图组"
        except Exception:
            pass
        return "自动化任务"

    # ------------------------------------------------------------------ 上报
    def notify(self, msg_type, title, content, payload=None):
        """线程安全地写入一条通知到云端。"""
        if not self.enabled or not self.pair_code:
            return
        box = self._mailbox or CloudMailbox(self.pair_code)
        ok, _ = box.send(DIR_PC2PHONE, msg_type, title, content, payload)
        if not ok:
            self._last_error = "上报失败"

    def notify_task(self, status, label, detail=""):
        mapping = {
            "start": MSG_TASK_START,
            "done": MSG_TASK_DONE,
            "failed": MSG_TASK_FAILED,
            "stop": MSG_TASK_STOP,
        }
        self.notify(mapping.get(status, MSG_TASK_DONE),
                    "任务" + {"start": "开始", "done": "完成",
                              "failed": "失败", "stop": "已停止"}.get(status, "更新"),
                    "%s%s" % (label, ("\n" + detail) if detail else ""),
                    {"status": status, "label": label})

    # ------------------------------------------------------------------ 主循环
    def _run(self):
        while not self._stop_event.is_set():
            try:
                if self.enabled and self.pair_code:
                    self._check_task_edge()
                    self._poll_commands()
                    self._heartbeat()
                    if time.time() - self._last_group_sync > 120:
                        self.sync_group_list()
                    self._online = not bool(self._last_error)
                else:
                    self._was_busy = self._is_busy()
                self._last_error = ""
            except Exception as e:
                self._last_error = str(e)
                self._safe_log("手机联动轮询异常：%s" % e)
            self._stop_event.wait(min(self.poll_sec, 3.0) if self.enabled else 3.0)

    def _check_task_edge(self):
        busy = self._is_busy()
        if busy and not self._was_busy:
            # idle -> busy：任务开始
            self._was_busy = True
            self._busy_since = time.time()
            self._busy_label = self._busy_label_now()
            self.notify_task("start", self._busy_label)
        elif not busy and self._was_busy:
            # busy -> idle：任务结束。稍等片刻确认不是暂停间隙
            self._was_busy = False
            elapsed = time.time() - self._busy_since
            self.notify_task("done", self._busy_label,
                             "耗时 %s" % _fmt_duration(elapsed))

    def _poll_commands(self):
        ok, rows = (self._mailbox or CloudMailbox(self.pair_code)).fetch_unread(DIR_PHONE2PC)
        if not ok:
            self._last_error = str(rows)
            return
        for row in (rows or []):
            mid = row.get("id")
            if mid in self._handled_ids:
                continue
            self._handled_ids.add(mid)
            try:
                payload = row.get("payload") or "{}"
                data = json.loads(payload) if isinstance(payload, str) else payload
            except Exception:
                data = {}
            msg_type = row.get("msg_type", "")
            if msg_type == MSG_COMMAND:
                self._on_command(mid, data, row.get("content", ""))
            else:
                # 未知类型，标记已读避免重复处理
                self._safe_mark_read(mid)

    def _on_command(self, msg_id, data, content):
        """收到手机下发的执行指令。"""
        target_type = str(data.get("target_type") or "").strip()
        target_name = str(data.get("target_name") or "").strip()
        if not target_name:
            target_name = str(content or "").strip()
        self._safe_mark_read(msg_id)
        if not target_type or not target_name:
            self._safe_notify(MSG_ACK, "指令无效", "缺少任务组类型或名称")
            return

        # 忙时不打断当前任务
        if self._is_busy():
            self._safe_notify(MSG_ACK, "指令未执行",
                              "电脑正在执行其他任务，请稍后重试")
            return

        # 构造队列项并交给 UI 线程执行
        item = {"type": target_type, "name": target_name}
        if target_type == "screenshot_task":
            item["group_name"] = data.get("group_name", target_name)
            try:
                item["task_index"] = int(data.get("task_index", 0))
            except Exception:
                item["task_index"] = 0
            item["display"] = data.get("display", target_name)
        self.app.after(0, lambda it=item: self._execute_command_on_ui(it))

    def _execute_command_on_ui(self, item):
        """在 UI 线程把指令项入队并启动执行。"""
        try:
            name = item.get("name") or item.get("group_name") or "任务"
            self.app.log_message("手机下发指令：执行 [%s]" % name)
            self._safe_notify(MSG_ACK, "指令已接收",
                              "电脑开始执行 [%s]" % name,
                              {"target_name": name})
            # 用临时队列执行单个组，执行完恢复原队列
            original = list(getattr(self.app, "current_queue", []))
            original_index = getattr(self.app, "current_script_index", 0)
            self.app.current_queue = [item]
            self.app.current_script_index = 0
            self.app.queue_paused = False
            self.app.active_group_session = None
            self.app._mixed_group_session = None
            self.app.player.should_stop = False
            self.app.player.paused = False
            self.app.completed = False
            self.app.auto_continue_var.set(True)
            # 复用现有分发器
            threading.Thread(target=self.app.process_next_script, daemon=True).start()
            self._restore_after_command = (original, original_index)
        except Exception as e:
            self._safe_notify(MSG_ACK, "指令执行失败", str(e))
            self._safe_log("手机指令执行出错：%s" % e)

    def _heartbeat(self):
        now = time.time()
        if now - self._last_heartbeat < 60:
            return
        self._last_heartbeat = now
        self.notify(MSG_HEARTBEAT, "电脑在线", "电脑端联动服务运行中")

    # ------------------------------------------------------------------ 工具
    def _safe_mark_read(self, mid):
        try:
            (self._mailbox or CloudMailbox(self.pair_code)).mark_read(mid)
        except Exception:
            pass

    def _safe_notify(self, msg_type, title, content, payload=None):
        try:
            self.notify(msg_type, title, content, payload)
        except Exception:
            pass

    def _safe_log(self, msg):
        try:
            self.app.log_message(msg)
        except Exception:
            pass

    def status_text(self):
        if not self.enabled:
            return "手机联动：未启用"
        if not self.pair_code:
            return "手机联动：未设置配对码"
        if not self._online:
            return "手机联动：连接中/离线"
        return "手机联动：在线（%s）" % (self.pair_code[:4] + "****")


def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d 秒" % seconds
    m, s = divmod(seconds, 60)
    if m < 60:
        return "%d 分 %d 秒" % (m, s)
    h, m = divmod(m, 60)
    return "%d 小时 %d 分" % (h, m)


def new_pair_code():
    """生成一个人类易读的 8 位配对码。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(uuid.uuid4().hex and alphabet[int(uuid.uuid4().hex[0], 16) % len(alphabet)]
                   for _ in range(8))
