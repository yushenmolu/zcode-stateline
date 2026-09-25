#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
statusbar_gui.py — docked_statusbar 的 GUI 应用层（Round2 Step 5 抽离）。

承载原 run_gui 函数体内全部嵌套闭包：StatusBarApp 类把它们方法化，
闭包共享的 state dict / cfg / root / canvas / hwnd / watcher / tooltip /
手势机等捕获变量全部转为实例属性（self.state、self.cfg、self.root ……）。
run_gui 在 docked_statusbar 内退化为薄封装（创建 StatusBarApp 并 run()）。

依赖方向 dsb -> gui（gui 经参数注入 win() 惰性单例，不反向 import dsb，
无环）。docked_statusbar 通过 `from statusbar_gui import *` 整体 re-export，
旧名（dsb.StatusBarApp、dsb.run_gui 等）全部保留。

行为契约与原闭包版逐行一致：所有 state 键（shown/last_xy/last_info/
db_read_count/cur_w/cfg_mtime/refresh_ms/dragging/drag_offset/
manual_position/manual_xy/close_box/collapsed/manual_handle/press_xy/
last_drag_xy/handle_drag_offset/hover_grace_until/ignore_click_until/
handle_armed/zcode_gone_since + 动态 badge_shown/force_db/last_collapse_err/
speed_last/speed_last_at/speed_last_sid）语义不变；_after 自排程链、
collapse/expand 冷却守卫、双击尾巴遮蔽、拖动偏移冻结等时序原样保留。
"""

import os
import queue
import sys
import traceback

import ctypes
import ctypes.wintypes as wintypes

from statusbar_layout import *
from statusbar_db import *
from statusbar_status import *
from statusbar_session import *
from statusbar_config import *

__all__ = [
    "StatusBarApp",
    # 下划线私有名经 re-export 沿用（P1-001 教训：显式列全）
    "_run_gui_impl",
]


def _log_throttled(data_dir, cache_dict, exc):
    """poll 等热路径异常的节流日志：同 key（异常类型名+稳定消息前80字符）
    60 秒内只写一条；cache_dict 超 100 项按最旧淘汰。写盘失败忽略。"""
    import re as _re
    import time as _time
    try:
        raw = "%s: %s" % (type(exc).__name__, exc)
        stable = _re.sub(r"0x[0-9a-fA-F]+", "0x*", raw)
        stable = _re.sub(r"\b\d{6,}\b", "*", stable)
        key = stable[:80]
        now = _time.time()
        last = cache_dict.get(key)
        if last is not None and now - last < 60:
            return
        if len(cache_dict) >= 100 and key not in cache_dict:
            oldest = min(cache_dict, key=lambda k: cache_dict[k])
            del cache_dict[oldest]
        cache_dict[key] = now
        _log_err(data_dir, "poll recurring error: %s" % raw)
    except Exception:
        pass


def _run_gui_impl(deps, data_dir, db_path, refresh_ms, cfg, config_path=None,
                  interval_fixed=False):
    """docked_statusbar.run_gui 的薄封装目标：创建 StatusBarApp 并进入主循环。
    deps 为主文件注入的依赖命名空间（win/dir_watcher/GestureState 等）。"""
    app = StatusBarApp(deps, data_dir, db_path, refresh_ms, cfg,
                       config_path=config_path, interval_fixed=interval_fixed)
    return app.run()


class StatusBarApp(object):
    """贴边状态条 GUI 应用（原 run_gui 全部嵌套闭包的方法化）。

    构造只做依赖与配置接管（不建窗口）；run() 建 root/canvas/菜单/手势机/
    watcher，排刷新与轮询链后进 mainloop。实例属性与原闭包捕获变量一一对应：
      data_dir/db_path/cfg/config_path/interval_fixed —— 构造参数；
      root/canvas/menu/display_menu/show_vars —— tk 对象；
      f_dim/f_main/f_num/f_icon —— 字体实测对象；
      hwnd —— 顶层原生句柄；state —— 原 state dict（键集合不变）；
      event_queue/watcher —— 事件驱动刷新；hover_expand_id —— 悬停展开计时；
      tip/tip_after_id —— 全局复用 tooltip；report_win —— 报告窗单实例；
      hand_gesture —— GestureState（注入 after/after_cancel/回调）。
    """

    def __init__(self, deps, data_dir, db_path, refresh_ms, cfg,
                 config_path=None, interval_fixed=False):
        self._deps = deps
        self.data_dir = data_dir
        self.db_path = db_path
        self.cfg = cfg
        self.config_path = config_path
        self.interval_fixed = interval_fixed
        # 构造参数 refresh_ms：state 初始化与 poll 的 reschedule 回退共用基线
        self._initial_refresh_ms = refresh_ms
        # 原 run_gui 局部可变量（nonlocal 捕获）-> 实例属性
        self.state = None
        self.root = None
        self.canvas = None
        self.hwnd = None
        self.event_queue = None
        self.watcher = None
        self.hover_expand_id = None
        self.tip = None
        self.tip_after_id = None
        self.report_win = None
        self.hand_gesture = None
        self.f_dim = None
        self.f_main = None
        self.f_num = None
        self.f_icon = None
        self.show_vars = None
        # poll 异常节流日志缓存（key->上次写盘时间戳），_log_throttled 用
        self._poll_err_cache = {}

    # -- 依赖快捷访问（主文件注入，避免反向 import dsb）--
    @property
    def _win(self):
        return self._deps.win

    @property
    def _dir_watcher(self):
        return self._deps.dir_watcher

    @property
    def _gesture_cls(self):
        return self._deps.GestureState

    def run(self):
        """原 run_gui 函数体（建窗 -> 绑事件 -> 排链 -> mainloop）。"""
        deps = self._deps
        data_dir = self.data_dir
        db_path = self.db_path
        refresh_ms = self._initial_refresh_ms
        cfg = self.cfg
        config_path = self.config_path
        import tkinter as tk  # 懒加载：GUI 路径才依赖桌面
        import tkinter.font as tkfont  # 各段文字宽度按字体实测
        self._tk = tk

        root = tk.Tk()
        self.root = root
        root.withdraw()
        root.overrideredirect(True)
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        try:
            root.attributes("-toolwindow", True)
        except Exception:
            pass
        root.configure(bg=deps.BG)
        root.resizable(False, False)
        root.geometry("%dx%d+0+0" % (deps.WINDOW_W, deps.WINDOW_H))
        # Stage 1 半透明：整条 95% 不透明（含收起把手——同一 root 窗口两态复用，
        # 一次设置两态生效；内容文字略受影响但 0.95 足够近不透明）。
        try:
            root.attributes("-alpha", 0.95)
        except Exception:
            pass

        # ---- 单 Canvas 绘制层（徽标圆角胶囊 + 两行分段文字 + close 小块）----
        canvas = tk.Canvas(root, bg=deps.BG, highlightthickness=0,
                           width=deps.WINDOW_W, height=deps.WINDOW_H)
        self.canvas = canvas
        canvas.pack(fill="both", expand=True)

        # 字体实测对象（块宽按文本实测，不拍脑袋定宽）
        self.f_dim = tkfont.Font(root=root, font=deps.FONT_DIM)
        self.f_main = tkfont.Font(root=root, font=deps.FONT_MAIN)
        self.f_num = tkfont.Font(root=root, font=deps.FONT_NUM)
        self.f_icon = tkfont.Font(root=root, font=deps.ICON_FONT)

        # ---- 右键菜单 ----
        menu = tk.Menu(root, tearoff=0, bd=0, bg=deps.MENU_BG, fg=deps.FG,
                       activebackground=deps.MENU_ACTIVE, activeforeground=deps.FG)
        self.menu = menu
        menu.add_command(label=u"重新贴边", command=lambda: self.re_dock())
        menu.add_command(label=u"收起到边缘",
                         command=lambda: self.collapse_bar())
        menu.add_separator()
        menu.add_command(label=u"复制当前统计到剪贴板",
                         command=lambda: self.copy_current_stats())
        menu.add_command(label=u"打开统计报告",
                         command=lambda: self.show_report_window())
        menu.add_command(label=u"最近会话速览",
                         command=lambda: self.show_sessions_overview_window())
        menu.add_separator()

        # 「显示项」子菜单：每个 show_* 一项，checkbutton 勾选态绑定当前配置；
        # 点击即切换 -> 立即重画 -> 原子写回 statusbar-config.json（失败只记 err 日志）。
        show_vars = {}
        self.show_vars = show_vars
        display_menu = tk.Menu(menu, tearoff=0, bd=0, bg=deps.MENU_BG, fg=deps.FG,
                               activebackground=deps.MENU_ACTIVE,
                               activeforeground=deps.FG)
        for _lbl, _key in SHOW_MENU_ITEMS:
            _var = tk.BooleanVar(value=bool(cfg.get(_key, False)))
            show_vars[_key] = _var
            display_menu.add_checkbutton(
                label=_lbl, variable=_var,
                command=(lambda k: lambda: self.toggle_show(k))(_key))
        menu.add_cascade(label=u"显示项", menu=display_menu)
        menu.add_separator()
        menu.add_command(label=u"退出 statusbar", command=lambda: self.quit_app())

        # 右键菜单绑到 Canvas（全区域可呼出）
        canvas.bind("<Button-3>", self.on_right_click)

        # close「×」小块改由 Canvas 绘制并按 tag 绑定（见 _draw_close）
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        try:
            root.update_idletasks()
        except Exception:
            pass

        # 取窗口原生 hwnd：tk 顶层由 winfo_id 的父级承载（已核实）
        try:
            wid = root.winfo_id()
            hwnd = self._win().get_parent(wid) or wid
        except Exception:
            hwnd = None
        self.hwnd = hwnd

        if hwnd:
            # 不抢焦点：加 WS_EX_NOACTIVATE，鼠标点击小条不会把前台抢成
            # pythonw（否则前台判定误判 -> 小条自隐藏/拖动被打断）。
            try:
                style = self._win().get_window_long(hwnd, deps.GWL_EXSTYLE)
                self._win().set_window_long(
                    hwnd, deps.GWL_EXSTYLE, style | deps.WS_EX_NOACTIVATE)
            except Exception:
                pass
            try:
                self._win().set_window_pos(
                    hwnd, deps.HWND_TOPMOST, 0, 0, 0, 0,
                    deps.SWP_NOMOVE | deps.SWP_NOSIZE | deps.SWP_NOACTIVATE
                    | deps.SWP_NOOWNERZORDER,
                )
            except Exception:
                pass

        state = {
            "shown": False,
            "last_xy": None,          # 最近一次贴边 MoveWindow 的目标（仅贴边用）
            "last_info": None,
            "db_read_count": 0,
            "cur_w": deps.WINDOW_W,   # 当前生效窗口宽（自适应 + 防抖后的值）
            # ---- 配置热加载 ----
            "cfg_mtime": None,        # statusbar-config.json 上次读取的 mtime
            "refresh_ms": refresh_ms,  # 当前生效刷新间隔（热加载可更新）
            # ---- 拖动状态（与贴边 last_xy 互相独立，互不污染）----
            "dragging": False,        # 正在拖动（按下->释放）
            "drag_offset": None,      # (x_root - winfo_rootx, y_root - winfo_rooty)
            "manual_position": False,  # 拖动过 -> poll 跳过吸回
            "manual_xy": None,        # 拖动后的稳定位置（供 re_dock 前保持）
            "close_box": None,        # close 小块的 Canvas 坐标（拖动按下时排除）
            # ---- 靠边收起（collapsed 把手）----
            "collapsed": bool(cfg.get("collapsed", False)),  # 启动按配置进收起/展开态
            "manual_handle": False,   # 收起态把手被手动拖过 -> poll 不再吸回（直到展开）
            "press_xy": None,         # 完整态按下时指针屏幕坐标（区分「单击」与「拖动」）
            "last_drag_xy": None,     # 收起态拖动最近一次被 clamp 后的目标坐标
            "handle_drag_offset": None,   # 收起态本次拖动的抓取偏移（一次拖拽内冻结）
            "hover_grace_until": 0,   # 收起冷却截止（epoch 毫秒）：此之前悬停展开不排程
            "ignore_click_until": 0,  # 双击尾巴遮蔽截止（epoch 毫秒）：此之前收起态
                                      # 释放事件不喂手势机（防收起后残余 release 又展开）
            "handle_armed": False,    # 悬停展开武装标志：需 leave->enter 才重新武装
            # ---- 生命周期绑定（0.9.3）----
            "zcode_gone_since": None,  # ZCode 进程首次被判缺席的时刻（epoch 毫秒）；
                                       # 进程一恢复立即清零（升级/重装的短空窗不退出）
            # ---- Stage 1 窗口级圆角（SetWindowRgn）----
            "last_region": None,      # (w, h, radius) 上次生效的 region 参数；
                                      # 相同则跳过重建成 GDI 对象（每秒重画防抖）
            # ---- Stage 4 把手 hover 微亮 ----
            "handle_hovered": False,  # 把手 hover 状态：True=微亮（BG_SECOND 底）
            # ---- Stage 4 展开/收起淡入淡出 ----
            "anim_after_id": None,    # 单例帧循环 after id（新动画启动前 cancel 旧 id）
            "anim_gen": 0,            # 代际令牌：每次启动新动画递增，帧回调比对代际
        }
        self.state = state

        # ---- 0.8.0 事件驱动刷新：目录监听 + 事件队列 ----
        # 队列在 Tk 主线程创建；watcher 回调在**工作线程**里只准 put 标记，绝不
        # 触碰任何 Tk 对象（Tkinter 非线程安全）；排空与刷新都在主线程做
        # （poll_fs_events，每 FS_POLL_MS 一次）。监听不可用时静默退回纯轮询。
        self.event_queue = queue.Queue()
        self.watcher = None
        dir_watcher = self._dir_watcher
        if dir_watcher is not None and getattr(dir_watcher, "AVAILABLE", False):
            def _on_fs_change(name):
                # 此回调在 watcher 工作线程里执行：只准往队列放标记，
                # 绝不触碰任何 Tk 对象（Tkinter 非线程安全）
                try:
                    self.event_queue.put(name)
                except Exception:
                    pass
            try:
                self.watcher = dir_watcher.DirWatcher(
                    data_dir, on_change=_on_fs_change,
                    names=set(deps.WATCH_NAMES), debounce_ms=60)
                self.watcher.start()
            except Exception:
                # 监听不可用不致命：静默退回纯轮询（1 秒兜底仍在跑）
                _log_err(data_dir, "dir_watcher start failed:\n%s"
                         % traceback.format_exc())
                self.watcher = None
        self.hover_expand_id = None      # 把手悬停自动展开的 after 计时器（cancel 用）

        # ---- 收起把手手势状态机（单击/双击/拖动裁决）----
        # 注入 GUI 的 after / after_cancel / 副作用回调；函数体在调用期解析。
        self.hand_gesture = self._gesture_cls(
            after=self._gesture_after,
            after_cancel=lambda i: root.after_cancel(i),
            now=time_ms,
            on_expand=self._gesture_click_expand,
            on_persist=self._gesture_persist,
        )

        # 记录配置文件初始 mtime（热加载基线；文件暂不存在为 None）
        try:
            state["cfg_mtime"] = os.path.getmtime(config_path)
        except Exception:
            state["cfg_mtime"] = None

        # ---- tooltip（单一全局 toplevel 复用，跟随鼠标）----
        self.tip = None
        self.tip_after_id = None
        self.report_win = None

        # 拖动绑到 Canvas 全区域（含两行文字与各指标块；close 小块在 drag_start 内排除）
        canvas.bind("<ButtonPress-1>", self.drag_start)
        canvas.bind("<B1-Motion>", self.drag_move)
        canvas.bind("<ButtonRelease-1>", self.drag_stop)
        # 双击状态条任意区域 -> 收起到边缘（collapsed 把手）；收起态双击由手势
        # 状态机裁决（第二次抬起不作任何展开/收起，双击收起态无意义）
        canvas.bind("<Double-Button-1>",
                    lambda _e: self.collapse_bar()
                    if not state.get("collapsed") else None)

        try:
            self.refresh_tick()      # 启动即有一行内容（refresh_once 同步执行），
                                     # 并排上 1 秒兜底链
        except Exception:
            pass
        try:
            # 事件驱动链首拍：FS_POLL_MS 后开始排空 event_queue
            self._after(deps.FS_POLL_MS, self.poll_fs_events)
        except Exception:
            pass
        try:
            self._after(state.get("refresh_ms", refresh_ms), self.poll)
        except Exception:
            pass
        try:
            root.mainloop()
        finally:
            deps._cleanup_pid(data_dir)
        return 0

    # ------------------------------------------------------------------
    # 右键菜单 / 退出
    # ------------------------------------------------------------------
    def on_right_click(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def copy_current_stats(self):
        """右键「复制当前统计到剪贴板」：取最近一拍 info 拼两行文本进剪贴板。
        无数据 / 剪贴板异常只记 err 日志，不打断 GUI。"""
        try:
            text = self._deps.build_copy_text(self.state.get("last_info"),
                                              self.cfg)
            if not text:
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception:
            _log_err(self.data_dir, "copy_current_stats error:\n%s"
                     % traceback.format_exc())

    def show_report_window(self):
        """右键「打开统计报告」：db_stats 当前会话报告，Toplevel + 滚动文本框
        （单实例复用；风格沿用 tooltip 的深底色）。任何异常只记日志。"""
        tk = self._tk
        deps = self._deps
        try:
            info = self.state.get("last_info") or {}
            text = deps.build_report_text(self.db_path, info.get("session_id"))
            if self.report_win is not None:
                try:
                    self.report_win.destroy()
                except Exception:
                    pass
                self.report_win = None
            win = tk.Toplevel(self.root)
            self.report_win = win
            win.title(u"统计报告")
            win.configure(bg=deps.BG_SECOND)
            win.geometry("460x420")
            txt = tk.Text(win, bg=deps.BG_SECOND, fg=deps.FG, font=deps.FONT_DIM,
                          relief="flat", wrap="none")
            sb = tk.Scrollbar(win, command=txt.yview)
            txt.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            txt.pack(side="left", fill="both", expand=True)
            txt.insert("1.0", text)
            txt.configure(state="disabled")

            def _on_close():
                try:
                    win.destroy()
                finally:
                    self.report_win = None

            win.protocol("WM_DELETE_WINDOW", _on_close)
        except Exception:
            self.report_win = None
            _log_err(self.data_dir, "show_report_window error:\n%s"
                     % traceback.format_exc())

    def show_sessions_overview_window(self):
        """右键「最近会话速览」：最近 3 个活跃主会话（标题+状态+本轮耗时），
        Toplevel + 滚动文本框（复用统计报告的单实例窗口槽与深底色风格）。
        任何异常只记日志。"""
        tk = self._tk
        deps = self._deps
        try:
            text = deps.build_sessions_overview_text(self.db_path, limit=3)
            if self.report_win is not None:
                try:
                    self.report_win.destroy()
                except Exception:
                    pass
                self.report_win = None
            win = tk.Toplevel(self.root)
            self.report_win = win
            win.title(u"最近会话速览")
            win.configure(bg=deps.BG_SECOND)
            win.geometry("420x260")
            txt = tk.Text(win, bg=deps.BG_SECOND, fg=deps.FG, font=deps.FONT_DIM,
                          relief="flat", wrap="none")
            sb = tk.Scrollbar(win, command=txt.yview)
            txt.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            txt.pack(side="left", fill="both", expand=True)
            txt.insert("1.0", text)
            txt.configure(state="disabled")

            def _on_close():
                try:
                    win.destroy()
                finally:
                    self.report_win = None

            win.protocol("WM_DELETE_WINDOW", _on_close)
        except Exception:
            self.report_win = None
            _log_err(self.data_dir, "show_sessions_overview_window error:\n%s"
                     % traceback.format_exc())

    def quit_app(self):
        # 先停 watcher（幂等；失败不阻塞退出）再销毁窗口：避免回调线程在
        # 解释器收尾阶段还往队列里放事件
        try:
            if self.watcher is not None:
                self.watcher.stop()
        except Exception:
            pass
        # 关闭 DB 单例连接（释放只读句柄；幂等，未建立过时为 no-op）
        try:
            _db_close_cached()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        deps._cleanup_pid(self.data_dir)
        sys.exit(0)

    def on_close(self):
        self.quit_app()

    # ------------------------------------------------------------------
    # 手势状态机注入回调
    # ------------------------------------------------------------------
    def _gesture_after(self, ms, fn):
        def _wrapped():
            try:
                fn()
            except Exception:
                try:
                    _log_err(self.data_dir, "handle gesture callback error:\n%s"
                             % traceback.format_exc())
                except Exception:
                    pass
        try:
            return self.root.after(ms, _wrapped)
        except Exception:
            return None

    def _gesture_click_expand(self):
        """单击确认展开（手势状态机 _fire_click -> on_expand）：source
        =="click"。**不受收起冷却窗拦截**——单击是用户主动明确意图，收起
        冷却窗内点一下立即展开。防自恢复由手势机侧兜住：双击第二次抬起
        （in_double_press）不武装单击待定，残余 release 不再走 click 路径。"""
        try:
            self.expand_bar(source="click")
        except Exception:
            try:
                _log_err(self.data_dir, "handle gesture expand error:\n%s"
                         % traceback.format_exc())
            except Exception:
                pass

    def _gesture_persist(self, x, y):
        # 拖动结束 -> 持久化把手坐标（原子写），poll 不再吸回直到展开
        self.cfg["handle_x"] = int(x)
        self.cfg["handle_y"] = int(y)
        self.state["manual_handle"] = True
        save_config_keys(self.config_path, self.cfg, self.data_dir,
                         ["handle_x", "handle_y"])
        self._sync_cfg_mtime()

    def _after(self, ms, fn):
        """root.after 包装：回调异常写 err 日志并退避重排（断路器 10 次熔断）。"""
        fails = [0]

        def _wrapped():
            try:
                fn()
                fails[0] = 0
            except Exception:
                try:
                    _log_err(self.data_dir,
                             "statusbar after-callback error:\n%s"
                             % traceback.format_exc())
                except Exception:
                    pass
                fails[0] += 1
                if fails[0] >= 10:
                    try:
                        _log_err(self.data_dir,
                                 "statusbar after-callback circuit open: %s"
                                 % getattr(fn, "__name__", repr(fn)))
                    except Exception:
                        pass
                    return
                backoff = max(ms, 1000) * min(2 ** fails[0], 30)
                try:
                    self.root.after(int(backoff), _wrapped)
                except Exception:
                    pass
        try:
            self.root.after(ms, _wrapped)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # tooltip（单一全局 toplevel 复用，跟随鼠标）
    # ------------------------------------------------------------------
    def _ensure_tip(self):
        deps = self._deps
        if self.tip is None:
            try:
                tip = self._tk.Toplevel(self.root)
                self.tip = tip
                tip.withdraw()
                tip.overrideredirect(True)
                try:
                    tip.attributes("-topmost", True)
                except Exception:
                    pass
                tip.configure(bg=deps.BG_SECOND)
                tip_lbl = self._tk.Label(
                    tip, text="", bg=deps.BG_SECOND, fg=deps.FG,
                    font=deps.FONT_DIM, anchor="w", justify="left",
                    padx=8, pady=5, wraplength=520,
                )
                tip_lbl.pack()
                tip.tip_lbl = tip_lbl
            except Exception:
                self.tip = None
        return self.tip

    def _tip_visible(self):
        try:
            return self.tip is not None and self.tip.winfo_ismapped()
        except Exception:
            return False

    def show_tooltip(self, text, x, y):
        """显示 tooltip：落在小条**外侧**（优先上方），并按小条所在显示器的
        工作区夹紧；工作区取不到时退回主屏。摆放规则见纯函数 tooltip_placement。"""
        deps = self._deps
        t = self._ensure_tip()
        if t is None:
            return
        try:
            t.tip_lbl.config(text=text)
            t.update_idletasks()
            tw = t.winfo_reqwidth()
            th = t.winfo_reqheight()
            bar_rect = deps.window_rect_of(self.hwnd) if self.hwnd else None
            wa = deps.work_area_of_rect(bar_rect) if bar_rect else None
            if not wa:
                wa = (0, 0, t.winfo_screenwidth(), t.winfo_screenheight())
            tx, ty = tooltip_placement(tw, th, x, y, bar_rect, wa)
            t.geometry("+%d+%d" % (tx, ty))
            t.deiconify()
            t.lift()
        except Exception:
            pass

    def hide_tooltip(self):
        try:
            if self.tip is not None:
                self.tip.withdraw()
        except Exception:
            pass

    def tooltip_enter(self, text):
        """悬停进入：延时 ~400ms 后显示（避免乱闪）。"""
        try:
            if self.tip_after_id is not None:
                self.root.after_cancel(self.tip_after_id)
        except Exception:
            pass
        try:
            self.tip_after_id = self.root.after(
                400, lambda: self._show_tip_at_pointer(text))
        except Exception:
            self.tip_after_id = None

    def _show_tip_at_pointer(self, text):
        try:
            self.show_tooltip(text, self.root.winfo_pointerx(),
                              self.root.winfo_pointery())
        except Exception:
            pass

    def tooltip_leave(self):
        """悬停离开：立即隐藏并取消延时任务。"""
        try:
            if self.tip_after_id is not None:
                self.root.after_cancel(self.tip_after_id)
                self.tip_after_id = None
        except Exception:
            pass
        self.hide_tooltip()

    def bind_hover(self, tag, tip_text, rect_id=None, base_fill=None,
                   hover_fill=None):
        """Canvas tag 级悬停：块背景高亮 + tooltip（延时显示/跟随/离开隐藏）。"""
        canvas = self.canvas

        def enter(_e):
            if rect_id is not None and hover_fill is not None:
                try:
                    canvas.itemconfigure(rect_id, fill=hover_fill)
                except Exception:
                    pass
            if tip_text:
                self.tooltip_enter(tip_text)

        def leave(_e):
            if rect_id is not None and base_fill is not None:
                try:
                    canvas.itemconfigure(rect_id, fill=base_fill)
                except Exception:
                    pass
            self.tooltip_leave()

        def motion(e):
            if tip_text and self._tip_visible():
                self.show_tooltip(tip_text, e.x_root, e.y_root)

        canvas.tag_bind(tag, "<Enter>", enter)
        canvas.tag_bind(tag, "<Leave>", leave)
        canvas.tag_bind(tag, "<Motion>", motion)

    def _draw_close(self, win_w):
        """右上「×」close 小块（圆角底 + hover 红 + 点击退出；每次重画时重建）。
        win_w 为本帧生效窗口宽（自适应）。"""
        deps = self._deps
        canvas = self.canvas
        cw, ch = 24, 16
        x1 = win_w - 10
        x0 = x1 - cw
        y0, y1 = 4, 4 + ch
        rect = deps.round_rect(canvas, x0, y0, x1, y1, 5, fill=deps.BG_SECOND,
                          outline="")
        ctxt = canvas.create_text((x0 + x1) / 2.0, (y0 + y1) / 2.0,
                                  text=u"×", font=deps.FONT_CLOSE,
                                  fill=deps.FG_DIM, tags=("close",))
        self.state["close_box"] = (x0, y0, x1, y1)

        def enter(_e):
            try:
                canvas.itemconfigure(rect, fill=deps.CLOSE_HOVER_BG)
                canvas.itemconfigure(ctxt, fill=deps.CLOSE_HOVER_FG)
            except Exception:
                pass

        def leave(_e):
            try:
                canvas.itemconfigure(rect, fill=deps.BG_SECOND)
                canvas.itemconfigure(ctxt, fill=deps.FG_DIM)
            except Exception:
                pass

        canvas.tag_bind("close", "<Enter>", enter)
        canvas.tag_bind("close", "<Leave>", leave)

        def on_click(_e):
            self.quit_app()
            return "break"   # 阻断 Canvas 级拖动绑定

        canvas.tag_bind("close", "<Button-1>", on_click)

    def _avail_work_w(self):
        """窗口宽上限 = 所在显示器工作区宽 - 16px；取不到工作区退回屏幕宽。"""
        deps = self._deps
        try:
            if self.hwnd:
                rect = deps.window_rect_of(self.hwnd)
                if rect:
                    wa = deps.work_area_of_rect(rect)
                    if wa and wa[2] > wa[0]:
                        return (wa[2] - wa[0]) - 16
        except Exception:
            pass
        try:
            return self.root.winfo_screenwidth() - 16
        except Exception:
            return deps.WINDOW_W

    def _apply_width(self, new_w):
        """窗口宽自适应落地（带 ±WIDTH_HYSTERESIS 防抖）：
        - 变化小于阈值 -> 保持当前宽（数字位数跳动不引起窗口频繁缩放闪烁）；
        - cur_w 为 None（收起<->展开刚切换）-> 跳过防抖强制生效；
        - 拖动中 -> 不改尺寸（避免干扰拖动；释放后下一拍渲染补上）；
        - 生效时只改宽度、保持左上角不动。
        返回本帧实际生效宽度（防抖后可能与 new_w 不同，渲染以返回值为准）。"""
        deps = self._deps
        state = self.state
        cur = state.get("cur_w")
        if cur is not None and abs(new_w - cur) < deps.WIDTH_HYSTERESIS:
            return cur
        if state.get("dragging"):
            return (cur if cur is not None else new_w)
        state["cur_w"] = new_w
        try:
            self.canvas.config(width=new_w)
        except Exception:
            pass
        try:
            if self.hwnd:
                xy = self.current_window_xy()
                if xy:
                    self._win().move_window(self.hwnd, xy[0], xy[1], new_w,
                                            deps.WINDOW_H, True)
            else:
                self.root.geometry("%dx%d+0+0" % (new_w, deps.WINDOW_H))
        except Exception:
            pass
        return new_w

    def _apply_window_region(self, w, h, radius):
        """Stage 1 窗口级圆角：ctypes 调 CreateRoundRectRgn + SetWindowRgn。

        生命周期约束（总计划写死）：
          - SetWindowRgn 成功（返回非 0）-> region 所有权移交系统，**禁止**
            DeleteObject；
          - 失败（返回 0）-> 必须立即 DeleteObject，防 GDI 句柄泄漏；
          - 尺寸/半径与上次相同（state["last_region"] 命中）-> 不重建（每秒
            重画时每帧新建 GDI 对象会泄漏）。
        坐标用 Tk 逻辑像素（与 create_* 同坐标系）。
        TODO(高 DPI)：winfo 像素与 Win32 物理像素不一致时乘
        winfo_fpixels('1i')/96 缩放修正——当前按 100% DPI 实现，待验证。
        任一环节抛异常静默吞掉（圆角失败不影响功能，只是视觉退回直角）。
        """
        state = self.state
        params = (int(w), int(h), int(radius))
        if state.get("last_region") == params:
            return
        if not self.hwnd:
            return
        rgn = None
        try:
            rgn = self._win().create_round_rect_rgn(
                0, 0, params[0] + 1, params[1] + 1,
                params[2] * 2, params[2] * 2)
            if not rgn:
                return
            ok = self._win().set_window_rgn(self.hwnd, rgn, True)
            if ok == 0:
                # 失败：region 所有权未移交，立即回收
                try:
                    self._win().delete_object(rgn)
                except Exception:
                    pass
                return
            # 成功：region 归系统，只记参数不再触碰句柄
            state["last_region"] = params
        except Exception:
            # 创建途中抛异常且 rgn 已出：兜底回收（此时 set_window_rgn 未调）
            if rgn:
                try:
                    self._win().delete_object(rgn)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # 靠边收起：小把手（collapsed 模式）
    # ------------------------------------------------------------------
    def _cancel_hover_expand(self):
        """取消把手悬停自动展开的计时器（离开把手 / 已展开时调用）。"""
        try:
            if self.hover_expand_id is not None:
                self.root.after_cancel(self.hover_expand_id)
        except Exception:
            pass
        self.hover_expand_id = None

    def _schedule_hover_expand(self):
        """（重新）启动悬停 0.5s 自动展开计时（Move 事件刷新 = 重置计时）。

        守卫（判定抽为模块级 hover_expand_should_schedule）：收起冷却期内不
        排程；未武装不排程；手势按压/拖动进行中不排程。排程成功后 consume
        武装（handle_armed=False），一次 leave->enter 只武装一次。"""
        state = self.state
        if not hover_expand_should_schedule(state, time_ms(),
                                            self.hand_gesture.press_xy is not None,
                                            self.hand_gesture.dragging):
            return
        state["handle_armed"] = False   # 武装只消费一次
        self._cancel_hover_expand()
        try:
            self.hover_expand_id = self.root.after(
                self._deps.HANDLE_HOVER_MS,
                lambda: self.expand_bar(source=None))
        except Exception:
            self.hover_expand_id = None

    def _sync_cfg_mtime(self):
        """写回配置后同步热加载基线 mtime，避免下一拍重读同值文件。"""
        try:
            self.state["cfg_mtime"] = os.path.getmtime(self.config_path)
        except Exception:
            pass

    def collapse_bar(self):
        """收起：完整状态条 -> 小把手。清完整态手动定位/清理把手手势待定，
        持久化 collapsed=true，立即以把手尺寸重画。把手停靠位由 poll 决定：
        无记忆 -> 工作区底部居中；有记忆 -> 记忆位置（越界回退右下角）。"""
        state = self.state
        cfg = self.cfg
        if state.get("collapsed"):
            return
        state["collapsed"] = True
        cfg["collapsed"] = True
        save_config_keys(self.config_path, cfg, self.data_dir, ["collapsed"])
        self._sync_cfg_mtime()
        self.hide_tooltip()
        self._cancel_hover_expand()
        self.hand_gesture.cancel_click()     # 收起瞬间清理未决单击待定/拖动态
        # 双击尾巴遮蔽：双击最后一次抬起的残余释放事件若在收起后仍到达手势机，
        # 这里置遮蔽窗：窗口内下次 release 不再喂手势机，双击尾巴被吃掉。
        state["ignore_click_until"] = time_ms() + self._deps.DOUBLE_CLICK_TAIL_MS
        # 防自恢复：收起后进入悬停展开冷却窗，并把把手 disarm —— 把手恰在
        # 指针下也不会 500ms 后自展开；需 leave->enter 且过冷却后才恢复悬停展开。
        state["hover_grace_until"] = time_ms() + self._deps.COLLAPSE_HOVER_GRACE_MS
        state["handle_armed"] = False
        # Stage 4 hover 微亮：收起后把手未 hover（几何/内容已切换）
        state["handle_hovered"] = False
        # 完整态默认贴边（manual_position 是完整态拖动记忆；收起态不沿用）
        state["manual_position"] = False
        state["manual_xy"] = None
        state["last_xy"] = None
        state["close_box"] = None
        try:
            self.render_ui(state.get("last_info") or {
                "text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None})
        except Exception:
            try:
                _log_err(self.data_dir, "render after collapse error:\n%s"
                         % traceback.format_exc())
            except Exception:
                pass
        # Stage 4 淡出：几何/内容已切换，alpha 从 0.95 渐隐到 0.0 再恢复
        # （收起后窗口仍可见——把手也是可见窗口的一部分，不做全隐；
        #  淡出只作为视觉过渡，终值恢复 0.95）。
        # 注：淡出终值不是 0.0，而是 0.95——把手本身需要可见。
        # 视觉效果 = 先闪一下再稳定，等同于"眨眼"提示切换完成。
        self._start_alpha_anim(0.95, 0.95)

    # ------------------------------------------------------------------
    # Stage 4 展开/收起淡入淡出（150ms alpha 过渡）
    # ------------------------------------------------------------------
    def _cancel_alpha_anim(self):
        """取消进行中的 alpha 动画：递增代际（已排队回调失效）+ 立即写终值。

        任何路径结束后 alpha ∈ {0.95, 0.0→隐藏}。取消时窗口保持当前几何
        不变（几何已在动画前一次性切好），只把 alpha 一步写到位。"""
        state = self.state
        state["anim_gen"] = state.get("anim_gen", 0) + 1
        try:
            if state.get("anim_after_id") is not None:
                self.root.after_cancel(state["anim_after_id"])
        except Exception:
            pass
        state["anim_after_id"] = None
        # 立即写当前目标终值：展开=0.95，收起=0.95（把手也可见）
        # （淡出到 0.0 的动画只用于视觉过渡，实际终值始终 0.95）
        try:
            self.root.attributes("-alpha", 0.95)
        except Exception:
            pass

    def _start_alpha_anim(self, from_alpha, to_alpha):
        """启动 150ms alpha 过渡帧循环（展开/收起共用）。

        五条防护落点：
          1. 单例帧循环：state["anim_after_id"] 单槽位登记；新动画启动前
             after_cancel 旧 id。
          2. 代际令牌：state["anim_gen"] 每次启动递增并闭包进帧回调；帧回调
             先比对代际，不等直接退出（不写 alpha、不排下一帧）。
          3. 目标态快照：启动时把终值快照进闭包（_to_alpha 局部变量），动画
             途中目标不可变。
          4. 强制终值 finally：帧循环尾部 finally 段——若当前代际仍是自己
             则写终值 alpha；若已被取代则不写（新代际会写它自己的）。
          5. 取消语义：_cancel_alpha_anim 递增代际 + 立即写终值。

        帧循环只改 -alpha，不调 SetWindowRgn、不改几何（几何已在动画前一次
        性切好）。动画结束后 anim_after_id=None，每秒重画链路不触碰 alpha。
        """
        state = self.state
        # 防护 1：单例帧循环——cancel 旧动画
        try:
            if state.get("anim_after_id") is not None:
                self.root.after_cancel(state["anim_after_id"])
        except Exception:
            pass
        state["anim_after_id"] = None

        # 防护 2：代际令牌递增
        state["anim_gen"] = state.get("anim_gen", 0) + 1
        my_gen = state["anim_gen"]

        # 防护 3：目标态快照（闭包捕获，动画途中不可变）
        _from = from_alpha
        _to = to_alpha

        FRAMES = 10         # 150ms / 16ms ≈ 10 帧（写死）
        FRAME_MS = 16       # 每帧间隔 ~16ms（60fps）

        def _frame(frame_idx):
            # 防护 2：代际比对——已被取代直接退出（不写 alpha、不排下一帧）
            if state.get("anim_gen") != my_gen:
                return
            t = min(frame_idx / float(FRAMES), 1.0)
            alpha = _from + (_to - _from) * t
            try:
                self.root.attributes("-alpha", alpha)
            except Exception:
                pass
            if frame_idx < FRAMES:
                # 防护 1：单槽位登记下一帧
                try:
                    state["anim_after_id"] = self.root.after(
                        FRAME_MS, lambda: _frame(frame_idx + 1))
                except Exception:
                    state["anim_after_id"] = None
            else:
                # 最后一帧：防护 4 强制终值
                state["anim_after_id"] = None
                # 防护 4：finally 段——代际仍是自己才写终值
                if state.get("anim_gen") == my_gen:
                    try:
                        self.root.attributes("-alpha", _to)
                    except Exception:
                        pass

        # 启动第一帧
        try:
            state["anim_after_id"] = self.root.after(0, lambda: _frame(0))
        except Exception:
            state["anim_after_id"] = None

    def expand_bar(self, source=None):
        """展开：把手 -> 完整状态条（重新贴边 ZCode 底部，不还原收起前的
        手动位置）。持久化 collapsed=false，清掉把手手动记忆。恢复完整条
        高度并强制宽度生效（cur_w=None 绕过防抖）。

        source 区分展开路径（冷却守卫对点击来源豁免）：悬停/菜单受冷却
        守卫；单击确认（"click"）不受冷却拦截。"""
        state = self.state
        cfg = self.cfg
        deps = self._deps
        if not state.get("collapsed"):
            return
        if source != "click" and time_ms() < state.get("hover_grace_until", 0):
            return  # 非点击来源在收起冷却窗内不展开（悬停/菜单防自恢复）
        state["collapsed"] = False
        cfg["collapsed"] = False
        save_config_keys(self.config_path, cfg, self.data_dir, ["collapsed"])
        self._sync_cfg_mtime()
        self.hide_tooltip()
        self._cancel_hover_expand()
        self.hand_gesture.cancel_click()
        state["ignore_click_until"] = 0   # 展开后清双击尾巴遮蔽
        state["manual_handle"] = False   # 展开后清标志：下次收起重新按记忆位置
        state["manual_position"] = False
        state["manual_xy"] = None
        state["last_xy"] = None
        state["hover_grace_until"] = 0   # 展开后清冷却
        state["handle_armed"] = False    # 展开后 disarm：下次收起仍需 leave->enter
        state["cur_w"] = None      # 绕过宽度防抖：72 -> ~620 必须立刻生效
        try:
            self.canvas.config(height=deps.WINDOW_H)  # 恢复完整条高度
        except Exception:
            pass
        # Stage 4 展开时清把手 hover 标志（把手已不存在）
        state["handle_hovered"] = False
        try:
            self.render_ui(state.get("last_info") or {
                "text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None})
        except Exception:
            try:
                _log_err(self.data_dir, "render after expand error:\n%s"
                         % traceback.format_exc())
            except Exception:
                pass
        # Stage 4 淡入：几何/内容已切换，alpha 从 0.0 渐入到 0.95
        self._start_alpha_anim(0.0, 0.95)

    def _apply_handle_geometry(self, new_w):
        """收起把手尺寸落地：cur_w 直接生效（把手宽稳定，无需防抖），窗口
        高度切到 HANDLE_H；位置本函数不动（poll 下一拍按 collapsed 停靠位
        贴屏幕底部），仅保持当前左上角。"""
        deps = self._deps
        self.state["cur_w"] = new_w
        try:
            self.canvas.config(width=new_w, height=deps.HANDLE_H)
        except Exception:
            pass
        try:
            if self.hwnd:
                xy = self.current_window_xy()
                if xy:
                    self._win().move_window(self.hwnd, xy[0], xy[1], new_w,
                                            deps.HANDLE_H, True)
            else:
                self.root.geometry("%dx%d+0+0" % (new_w, deps.HANDLE_H))
        except Exception:
            pass

    def _render_handle(self, info):
        """收起态渲染（render_ui 的 collapsed 分支调用；canvas 已清空）：
        ~72x18 深底小把手 + 顶部 1px 分隔线 + 「◐ 84.9%」浓缩缓存命中率
        （绿字）；悬停 0.5s / 单击展开。"""
        deps = self._deps
        canvas = self.canvas
        state = self.state
        stats = (info or {}).get("stats")
        hit_pct = deps._hit_rate(stats) if stats else 0.0
        hit_txt = (u"%.1f%%" % hit_pct) if stats else u"—"
        # ◐ 图标颜色跟随当前状态色（STATUS_COLORS）；命中率数字按命中率档位
        # 分色（cache_hit_color，与展开态口径一致）。
        icon_color = deps.handle_status_color(info)
        w = deps.BLOCK_PAD * 2 + self.f_icon.measure(deps.ICON_HIT) + 5 \
            + self.f_num.measure(hit_txt)
        self._apply_handle_geometry(w)
        # Stage 1 窗口级圆角（把手 radius=8：小半径保 round 感又不裁字）。
        self._apply_window_region(w, deps.HANDLE_H, 8)
        # Stage 4 hover 微亮：hover 时在内容之下画 BG_SECOND 底的圆角背景层
        # （z 序最低——最先创建，其余内容叠在上面），未 hover 时画 BG 底（与
        # 窗口同色，视觉无变化）。radius=8 与 SetWindowRgn 一致。
        handle_bg = deps.BG_SECOND if state.get("handle_hovered") else deps.BG
        deps.round_rect(canvas, 0, 0, w, deps.HANDLE_H, 8,
                        fill=handle_bg, outline="")
        canvas.create_rectangle(0, 0, w, 1, fill=deps.EDGE_LINE, outline="")
        # 底部 5px 状态色带（Stage 2：4px→5px + 渐变）：自 v0.12.4
        # 起由顶部挪到底部，与展开态一致——顶部色带与 ◐ 图标 / × 视觉重叠。
        # v0.12.5：渐变改为中间深→两边各一半深（center=True）；HANDLE_H
        # 18→24 拉开文字与色带间距。贴把手底缘（y=HANDLE_H-5..HANDLE_H，
        # 即 19..24）；文字中心 cy=12（HANDLE_H/2，size 10 字体实际高 ~12px，
        # 字形范围 ~6..18），文字底 18 与色带顶 19 留 1px 净距不重叠。
        # base_color 与 ◐ 图标同源 icon_color。
        deps._draw_gradient_band(canvas, 0, deps.HANDLE_H - 5, w,
                                 deps.HANDLE_H, icon_color, deps.BG,
                                 tags=("hdl",), center=True)
        tx = deps.BLOCK_PAD
        cy = deps.HANDLE_H / 2.0
        canvas.create_text(tx, cy, text=deps.ICON_HIT, font=deps.ICON_FONT,
                           fill=icon_color, anchor="w", tags=("hdl",))
        tx += self.f_icon.measure(deps.ICON_HIT) + 5
        canvas.create_text(tx, cy, text=hit_txt, font=deps.FONT_NUM,
                           fill=deps.cache_hit_color(hit_pct), anchor="w",
                           tags=("hdl",))

        # 把手 tooltip：固定说明 + 今日用量（今日聚合取自 info，无数据时只有固定说明）
        handle_tip_txt = deps.handle_tip((info or {}).get("today_stats"))

        def _enter(_e):
            if state.get("handle_hovered"):
                return  # 幂等守卫：已是 hover 态，delete("all") 重建会再触发 <Enter>，防自激振荡
            # Stage 4 hover 微亮：即时反馈（不等 500ms 展开计时器）
            state["handle_hovered"] = True
            self.render_ui(state.get("last_info") or {
                "text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None})
            # 仅当已武装（leave->enter 后）才排悬停展开；冷却期内的 enter
            # 因冷却守卫不排程，小幅度移动不重复武装。
            if state.get("handle_armed", False):
                self._schedule_hover_expand()      # 悬停 0.5s 自动展开
            self.tooltip_enter(handle_tip_txt)     # 「已收起——悬停或单击展开」+今日

        def _motion(e):
            if state.get("handle_armed", False):
                self._schedule_hover_expand()      # Move 刷新（重置）展开计时
            if self._tip_visible():
                self.show_tooltip(handle_tip_txt, e.x_root, e.y_root)

        def _leave(_e):
            if not state.get("handle_hovered"):
                return  # 幂等守卫：已非 hover 态，防 delete("all") 触发的伪 <Leave> 引发重画振荡
            # Stage 4 hover 微亮：离开恢复 BG 底
            state["handle_hovered"] = False
            self.render_ui(state.get("last_info") or {
                "text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None})
            self._cancel_hover_expand()
            state["handle_armed"] = True      # 离开把手 -> 重新武装
            self.tooltip_leave()

        canvas.tag_bind("hdl", "<Enter>", _enter)
        canvas.tag_bind("hdl", "<Motion>", _motion)
        canvas.tag_bind("hdl", "<Leave>", _leave)

    def render_ui(self, info):
        """整幅重画（同一回调内 delete+create，Tk 单次刷帧无闪烁）：顶部
        1px 分隔线 + 第一行（状态徽标 + 对话名）+ 第二行（本轮统计）+
        右侧小字（会话累计）+ close 小块。窗口宽按内容自适应
        （plan_statusbar_layout_3zone）：状态徽标 / 本轮统计 / 累计小字
        **永不因宽度裁剪**，超上限时依次收对话名 -> 缩间距 -> 省累计标注。"""
        deps = self._deps
        canvas = self.canvas
        state = self.state
        cfg = self.cfg
        canvas.delete("all")
        if state.get("collapsed"):
            # 收起态：只画底部小把手（◐ 命中率），几何走 HANDLE_H 分支
            self._render_handle(info)
            return
        show_status = cfg.get("show_status", True)
        show_turn = cfg.get("show_recent_turn", True)
        show_cum = cfg.get("show_cumulative", True)
        status = info.get("status") or "idle"
        speed = info.get("status_speed")
        model = info.get("model")
        label = info.get("session_label")
        turn = info.get("turn_stats")
        cum = info.get("stats")

        _fmap = {deps.FONT_MAIN: self.f_main, deps.FONT_NUM: self.f_num,
                 deps.FONT_DIM: self.f_dim, deps.ICON_FONT: self.f_icon}

        # 卡片段内边距（左右）与卡片间距（Stage 3：第二行 4 段数字装进圆角
        # 小卡片；卡片高 20px = ROW2_TURN_Y 上下各 10，圆角半径 5）。
        CARD_PAD_X = 6
        CARD_GAP = 6
        CARD_RADIUS = 5
        CARD_HALF_H = 10

        def _turn_segments(ts):
            """本轮统计分段，返回 (cards, trailing)：
            - cards：4 张圆角小卡片（耗时 / in / out / cache hit），每张是
              [(text, font, fg), ...] 子段列表（标签灰 + 数字原色/档位色）；
              无数据时为空列表。
            - trailing：卡片组之后的裸文字段（工具 N / 实时 / ⚡tok/s /
              （数据异常）等），平铺 (text, font, fg) 列表；无数据时单段
              占位（本轮统计待更新）。
            数字来源三态标注——live / stale_turn（上一轮）/ 无标记（本轮
            权威行）。"""
            cards = []
            if ts:
                inp = int(ts.get("inputTokens") or 0)
                cache_rd = int(ts.get("cacheReadTokens") or 0)
                hit = (cache_rd / float(inp) * 100.0) if inp > 0 else 0.0
                # 卡片 1：◷ 耗时（图标 + 秒数）
                cards.append([
                    (deps.ICON_DUR, deps.ICON_FONT, deps.FG_DIM),
                    (u"%.1fs" % ((ts.get("durationMs") or 0) / 1000.0),
                     deps.FONT_NUM, deps.FG),
                ])
                # 卡片 2：in
                cards.append([
                    (u"in ", deps.FONT_MAIN, deps.FG_DIM),
                    (deps.format_tokens(inp), deps.FONT_NUM, deps.FG),
                ])
                # 卡片 3：out
                cards.append([
                    (u"out ", deps.FONT_MAIN, deps.FG_DIM),
                    (deps.format_tokens(ts.get("outputTokens") or 0),
                     deps.FONT_NUM, deps.FG),
                ])
                # 卡片 4：cache hit（数字用档位色）
                cards.append([
                    (u"cache hit ", deps.FONT_MAIN, deps.FG_DIM),
                    (u"%.1f%%" % hit, deps.FONT_NUM,
                     deps.cache_hit_color(hit)),
                ])
            trailing = []
            if not ts:
                trailing.append((deps.TURN_PENDING, deps.FONT_MAIN,
                                 deps.FG_DIM))
            else:
                # 本轮工具调用数（0.9.2）
                trailing.extend(deps.turn_tool_segments(ts))
                if ts.get("live"):
                    trailing.append((u" · 实时", deps.FONT_MAIN,
                                     deps.ACCENT_GREEN))
                elif ts.get("stale_turn"):
                    trailing.append((u" · 上一轮", deps.FONT_MAIN,
                                     deps.FG_DIM))
            # 速度段扩到整个 busy 族，并在轮次收尾后短时保留最后已知值。
            trailing.extend(speed_display(
                status, speed, info.get("status_speed_held"),
                cfg.get("show_live", True), cfg.get("show_speed", True)))
            # 数据异常标注：info["error"] 有值时第二行尾部追加「（数据异常）」。
            if info.get("error"):
                trailing.append((deps.DATA_ANOMALY_NOTE, deps.FONT_MAIN,
                                 deps.FG_DIM))
            return cards, trailing

        def _card_text_w(card):
            """一张卡片内所有子段的文字总宽（字体实测）。"""
            return sum(_fmap[f].measure(t) for t, f, _ in card)

        def _turn_total_w(cards, trailing):
            """第二行内容总宽：各卡片（文字宽+内边距×2）+ 卡片间距 +
            尾部裸文字宽。"""
            w = 0
            for card in cards:
                w += _card_text_w(card) + CARD_PAD_X * 2 + CARD_GAP
            if cards and trailing:
                # 卡片组与尾部裸文字之间留一个段间距（原平铺段首自带
                # 前导空格/「 · 」，此处补 2px 视觉呼吸位）
                w += 2
            for t, f, _ in trailing:
                w += _fmap[f].measure(t)
            return w

        def _draw_turn_stats(x, y, ts):
            """第二行：4 段数字装进圆角小卡片（耗时/in/out/cache hit），
            尾部可选段裸文字平铺；整行悬停 tooltip。数字等宽防跳字。"""
            cards, trailing = _turn_segments(ts)
            x0 = x
            for card in cards:
                tw = _card_text_w(card)
                cw = tw + CARD_PAD_X * 2
                # 卡片底（先画底再画字，z 序由创建顺序决定）
                deps.round_rect(canvas, x, y - CARD_HALF_H, x + cw,
                                y + CARD_HALF_H, CARD_RADIUS,
                                fill=deps.BG_SECOND, outline="",
                                tags=("m_turn",))
                # 卡片内文字（左对齐依次排，垂直中心与卡片中心对齐）
                tx = x + CARD_PAD_X
                for t, f, c in card:
                    canvas.create_text(tx, y, text=t, font=f, fill=c,
                                       anchor="w", tags=("m_turn",))
                    tx += _fmap[f].measure(t)
                x += cw + CARD_GAP
            if cards and trailing:
                x += 2
            for t, f, c in trailing:
                canvas.create_text(x, y, text=t, font=f, fill=c,
                                   anchor="w", tags=("m_turn",))
                x += _fmap[f].measure(t)
            self.bind_hover("m_turn", deps.turn_tooltip(
                ts, latest_input=info.get("latest_input"), model=model, cfg=cfg))

        # ---- 文本拼装 ----
        badge_txt = status_badge_text(status, turn)
        turn_cards, turn_trailing = _turn_segments(turn)
        turn_w = _turn_total_w(turn_cards, turn_trailing)
        cum_txt = deps.cumulative_text(cum, cfg.get("show_cache_hit", True))
        has_note = bool(cum and info.get("recent_note"))

        # ---- 先实测、后布局（三区宽度全部 tkinter.font 实测）----
        badge_w = 0
        if show_status:
            badge_w = deps.BADGE_PAD_X * 2 + self.f_main.measure(badge_txt)
        cum_w = self.f_dim.measure(cum_txt) if (show_cum and cum_txt) else 0
        note_w = self.f_dim.measure(deps.SESSION_RECENT_NOTE) if has_note else 0
        # 模型名并入不可裁槽（与徽标同槽）
        if cfg.get("show_model", True) and model:
            badge_w += self.f_dim.measure(deps._truncate(model, 20) or "model?") + 8

        def _title_w(lm):
            if not label:
                return 0
            return self.f_dim.measure(deps._truncate(label, lm) or u"")

        plan = deps.plan_statusbar_layout_3zone(badge_w, _title_w, turn_w, cum_w,
                                           note_w, self._avail_work_w())
        win_w = self._apply_width(plan["win_w"])
        badge_gap = plan["badge_gap"]
        show_cum_now = bool(plan["show_cum"] and cum_txt)
        show_note_now = bool(plan["show_note"] and has_note)

        # Stage 1 窗口级圆角（展开态 radius=10）：尺寸变化才重建（函数内防抖）。
        self._apply_window_region(win_w, deps.WINDOW_H, 10)

        # Stage 1 阴影（仅展开态）：主矩形底部错位 2px、右偏 1px 画深色矩形
        # 模拟投影。先画阴影（在所有内容之下），区域 y=WINDOW_H-3..WINDOW_H-1
        # （WINDOW_H 已含底部 3px 阴影带，此带内不放任何内容）。
        canvas.create_rectangle(3, deps.WINDOW_H - 3, win_w + 3,
                                deps.WINDOW_H - 1,
                                fill="#0a0c0f", outline="")

        # 顶部 1px 分隔线（提质感）
        canvas.create_rectangle(0, 0, win_w, 1, fill=deps.EDGE_LINE, outline="")
        # 底部 5px 状态色带（Stage 2：4px→5px + 左深→右浅渐变）：自 v0.12.4
        # 起由顶部挪到底部——顶部色带与第一行徽标/对话名/右上角 × 视觉重叠，
        # 用户反馈放底部更干净。贴底部 3px 阴影带正上方（y=WINDOW_H-8..
        # WINDOW_H-3，即 51..56），与第二行文字（中心 ROW2_TURN_Y=40，底
        # ~50）相切不重叠。取色口径与状态徽标一致（STATUS_COLORS.get(status,
        # idle)）；即使 show_status=False 也画，否则关徽标后会丢失状态指示。
        deps._draw_gradient_band(
            canvas, 0, deps.WINDOW_H - 8, win_w, deps.WINDOW_H - 3,
            STATUS_COLORS.get(status, STATUS_COLORS["idle"]), deps.BG,
            center=True)

        # ---- 第一行：状态徽标（色块 + 深色粗体字）+ 模型名（蓝）+ 对话名 ----
        x = 12
        if show_status:
            color = STATUS_COLORS.get(status, STATUS_COLORS["idle"])
            badge_only_w = deps.BADGE_PAD_X * 2 + self.f_main.measure(badge_txt)
            rect = deps.round_rect(canvas, x, deps.ROW1_CY - deps.BADGE_H / 2.0,
                              x + badge_only_w,
                              deps.ROW1_CY + deps.BADGE_H / 2.0, 9,
                              fill=color, outline="")
            canvas.create_text(x + badge_only_w / 2.0, deps.ROW1_CY,
                               text=badge_txt, font=deps.BADGE_FONT,
                               fill=deps.BADGE_FG, anchor="center",
                               tags=("m_badge",))
            self.bind_hover("m_badge",
                            status_badge_tip(status, turn),
                            rect, color, color)
            x += badge_only_w
            x += badge_gap
        if cfg.get("show_model", True) and model:
            mtxt = deps._truncate(model, 20) or "model?"
            canvas.create_text(x, deps.ROW1_CY, text=mtxt, font=deps.FONT_DIM,
                               fill=deps.ACCENT_BLUE, anchor="w",
                               tags=("m_model",))
            x += self.f_dim.measure(mtxt) + 8
            self.bind_hover("m_model", u"模型：%s" % model)
        if label:
            canvas.create_text(x, deps.ROW1_CY,
                               text=deps._truncate(label, plan["label_max"])
                               or u"（未命名会话）",
                               font=deps.FONT_DIM, fill=deps.FG_DIM, anchor="w",
                               tags=("m_sess",))
            tip = u"会话：%s" % label
            if model:
                tip = tip + u"\n模型：%s" % model
            self.bind_hover("m_sess", tip)
        elif not show_status:
            # fail-closed：徽标也关掉且无对话名 -> 会话未识别占位
            canvas.create_text(12, deps.ROW1_CY, text=deps.SESSION_UNKNOWN,
                               font=deps.FONT_DIM, fill=deps.FG_DIM, anchor="w")

        # ---- 右上 close 小块 ----
        self._draw_close(win_w)

        # ---- 第二行：本轮统计（左侧）+ 会话累计小字（右侧）----
        if show_turn:
            _draw_turn_stats(10, deps.ROW2_TURN_Y, turn)
        if show_cum_now:
            # 累计小字右对齐（close 小块左侧留白），jsonl 兜底判定时左侧
            # 附「（最近会话累计）」标注。
            cx = win_w - deps.CLOSE_RESERVE_W - 6
            canvas.create_text(cx, deps.ROW2_TURN_Y, text=cum_txt,
                               font=deps.FONT_DIM, fill=deps.FG_DIM, anchor="e",
                               tags=("m_cum",))
            self.bind_hover("m_cum", deps.cum_tooltip(
                cum, today=info.get("today_stats"), model=model, cfg=cfg))
            if show_note_now:
                canvas.create_text(cx - self.f_dim.measure(cum_txt) - 6,
                                   deps.ROW2_TURN_Y,
                                   text=deps.SESSION_RECENT_NOTE,
                                   font=deps.FONT_DIM, fill=deps.FG_DIM,
                                   anchor="e", tags=("m_cum_note",))

    def current_window_xy(self):
        """取小条当前屏幕坐标 (x, y)；失败返回 None。"""
        try:
            if not self.hwnd:
                return None
            wid = self.root.winfo_id()
            child_hwnd = self._win().get_parent(wid) or wid
            if not child_hwnd:
                return None
            rect = wintypes.RECT()
            if self._win().get_window_rect(child_hwnd, ctypes.byref(rect)):
                return rect.left, rect.top
        except Exception:
            pass
        return None

    def _default_dock_xy(self, bar_w):
        """坐标/工作区瞬时不可得时的回退停靠：优先小条自身所在显示器工作区，
        再退主显示器（_primary_work_area）；仍失败返回 None。定位用生产
        default_handle_xy（工作区底部居中）。"""
        deps = self._deps
        wa = None
        try:
            rect = wintypes.RECT()
            if self.hwnd and self._win().get_window_rect(self.hwnd,
                                                         ctypes.byref(rect)):
                wa = deps.work_area_of_rect((rect.left, rect.top,
                                             rect.right, rect.bottom))
        except Exception:
            wa = None
        if wa is None:
            wa = deps._primary_work_area()
        if wa is None:
            return None
        return default_handle_xy(wa, bar_w)

    # ------------------------------------------------------------------
    # 拖动（完整态偏移拖动；收起态走手势状态机）
    # ------------------------------------------------------------------
    def drag_start(self, event):
        """按下：完整态（未收起）延用偏移拖动；收起态走手势状态机 press
        （记录坐标+时间，无动作；250ms 内第二次按下到达会取消单击待定）。
        close 小块上的按下走退出逻辑，不进入任何拖动。"""
        state = self.state
        if state.get("dragging") or self.hand_gesture.dragging:
            return
        # close 小块上的按下走退出逻辑（把手态无 close，恒 None）
        cb = state.get("close_box")
        if cb and cb[0] <= event.x <= cb[2] and cb[1] <= event.y <= cb[3]:
            return
        if state.get("collapsed"):
            self._cancel_hover_expand()   # 按下即取消悬停展开，交手势裁决
            # 新的一次按下 = 新的一次抓取：作废上次拖动的偏移，避免复用陈旧
            # 抓取量（release 不按时到达时尤其要紧）。
            state["handle_drag_offset"] = None
            self.hand_gesture.press(event.x_root, event.y_root)
            return
        xy = self.current_window_xy()
        if xy is None:
            return
        state["dragging"] = True
        state["press_xy"] = (event.x_root, event.y_root)
        state["drag_offset"] = (event.x_root - xy[0], event.y_root - xy[1])

    def drag_move(self, event):
        """按住左键移动：收起态 -> 手势状态机裁决（位移 >3px 才进入拖动态
        跟随移动，仍 clamp_to_work_area）；完整态 -> 原偏移拖动跟随。"""
        deps = self._deps
        state = self.state
        if state.get("collapsed"):
            # 调用前快照：motion() 内部会在越过阈值时把 dragging 置 True，
            # 用"调用前未在拖动态 + 调用后返回 DRAG"判定"本帧首次进入拖动态"。
            was_dragging = bool(self.hand_gesture.dragging)
            act = self.hand_gesture.motion({"x_root": event.x_root,
                                            "y_root": event.y_root})
            if act != deps.E_ACTION_DRAG:
                return
            if not was_dragging:
                # 防护 5：进入拖动态的瞬间取消一切进行中的 alpha 动画——
                # 拖动中不应有动画在跑（动画写 alpha 会与拖动视觉冲突）。
                self._cancel_alpha_anim()
            # 拖动态：目标坐标 clamp_to_work_area，防止拖出屏幕无法自救。
            bar_w = state.get("cur_w") or deps.HANDLE_W_DEFAULT
            bar_h = deps.HANDLE_H
            # 抓取偏移在一次拖拽内冻结（每帧现算 -> 恒等式，把手拖不动）。
            off = drag_grab_offset((event.x_root, event.y_root),
                                   self.current_window_xy(),
                                   state.get("handle_drag_offset"))
            if off is None:
                return            # 窗口坐标瞬时取不到：本帧不移动
            state["handle_drag_offset"] = off
            new_x, new_y = drag_target_xy((event.x_root, event.y_root), off,
                                          bar_w, bar_h,
                                          deps.clamp_to_work_area)
            try:
                if self.hwnd:
                    self._win().move_window(self.hwnd, new_x, new_y,
                                            bar_w, bar_h, True)
            except Exception:
                return
            state["last_drag_xy"] = (new_x, new_y)
            return
        if not state.get("dragging"):
            return
        off = state.get("drag_offset")
        if not off:
            return
        bar_w = state.get("cur_w") or deps.WINDOW_W
        bar_h = deps.WINDOW_H
        new_x, new_y = drag_target_xy((event.x_root, event.y_root), off,
                                      bar_w, bar_h, deps.clamp_to_work_area)
        try:
            if self.hwnd:
                self._win().move_window(self.hwnd, new_x, new_y,
                                        bar_w, bar_h, True)
        except Exception:
            return
        state["manual_xy"] = (new_x, new_y)

    def drag_stop(self, event):
        """释放：收起态 -> 手势状态机裁决（拖动 -> on_persist；双击第二次 ->
        不动作；单击 -> 排 250ms 待定展开）；完整态 -> 结束拖动、标记手动
        定位（poll 不再吸回），记录稳定位置。"""
        state = self.state
        if state.get("collapsed"):
            cur_xy = state.get("last_drag_xy")
            # 双击尾巴遮蔽：收起后 DOUBLE_CLICK_TAIL_MS 内到达的释放事件直接
            # 取消待定并丢弃，不喂手势机（防收起被残余 release 弹回）。
            if time_ms() < state.get("ignore_click_until", 0):
                # 防护 5：动画途中被打断（双击快速收起尾巴），立即收敛到终值。
                self._cancel_alpha_anim()
                self.hand_gesture.cancel_click()
                state["last_drag_xy"] = None
                return
            self.hand_gesture.release({"x_root": event.x_root,
                                       "y_root": event.y_root}, cur_xy)
            state["last_drag_xy"] = None
            # 拖动裁决（E_ACTION_PERSIST）release() 内部已按 cur_xy 回调
            # on_persist 写过 handle_x/handle_y 并置 manual_handle=True。
            return
        if not state.get("dragging"):
            return
        state["dragging"] = False
        state["drag_offset"] = None
        state["press_xy"] = None
        state["manual_position"] = True
        try:
            xy = self.current_window_xy()
            if xy is not None:
                state["manual_xy"] = xy
        except Exception:
            pass

    def re_dock(self):
        """重新贴边：清除手动定位标志（完整态 manual_position 与收起态
        manual_handle），让 poll 下一拍恢复停靠/记忆位置。"""
        state = self.state
        state["manual_position"] = False
        state["manual_xy"] = None
        state["last_xy"] = None
        state["manual_handle"] = False

    def toggle_show(self, key):
        """右键「显示项」开关：更新内存配置 -> 立即重画 -> 原子写回配置文件。
        写回失败不崩（save_config_show_keys 内已记 err 日志）；重画失败也只记日志。"""
        try:
            val = bool(self.show_vars[key].get())
        except Exception:
            return
        self.cfg[key] = val
        save_config_show_keys(self.config_path, self.cfg, self.data_dir)
        self._sync_cfg_mtime()   # 写回后 mtime 已变：同步热加载基线
        # 立即重画（不等下一拍刷新）；启动早期 last_info 可能仍是全 None dict
        try:
            self.render_ui(self.state.get("last_info") or {
                "text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None})
        except Exception:
            try:
                _log_err(self.data_dir, "render after toggle_show error:\n%s"
                         % traceback.format_exc())
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 刷新链（1 秒兜底 + 文件事件驱动）
    # ------------------------------------------------------------------
    def refresh_once(self):
        """单次数据组装 + 渲染（不排任何定时器）。异常契约：内部自吞
        （err 日志 + error 标记），绝不向上抛——供 1 秒兜底链
        （refresh_tick）与文件事件链（poll_fs_events）共同复用。"""
        deps = self._deps
        data_dir = self.data_dir
        db_path = self.db_path
        cfg = self.cfg
        state = self.state
        info = {"text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None}
        try:
            # 配置热加载：mtime 变化则重读（坏 JSON 保留当前配置 + err 日志一行）
            state["cfg_mtime"] = hot_reload_config(
                cfg, self.config_path, data_dir, state.get("cfg_mtime"))
            if not self.interval_fixed:
                try:
                    state["refresh_ms"] = max(
                        int(cfg.get("refresh_ms", deps.REFRESH_MS_DEFAULT)), 200)
                except Exception:
                    pass
        except Exception:
            pass
        # 完全不可见（SW_HIDE）：配置热加载照走，数据组装整段跳过。
        if hidden_skip_refresh(state):
            return
        try:
            # N6 性能：逐秒全量解析改走指纹缓存，文件未变时复用上次结果
            rows, _err = deps.read_jsonl_cached(
                os.path.join(data_dir, deps.JSONL_NAME))
            # 会话判定改为信号驱动 + 粘滞；model/title 缓存复用由
            # resolve_gui_info 内建 reuse 判定；此处仅保留周期性强刷
            # （db_read_count == 0 时每 DB_READ_INTERVAL 拍重读一次）。
            prev_info = state["last_info"]
            cur_cache = (None if (prev_info is None
                                  or state["db_read_count"] == 0)
                         else prev_info)
            # 状态徽标来源为 ZCode 钩子时序 status-state.json；会话判定走
            # sticky 信号链；sid 要等 resolve_gui_info 判定出来，故取记录
            # 推迟到其后的 status_state_for。
            status_doc = None
            if cfg.get("show_live", True):
                status_doc = _read_status_state(data_dir)
            # R4 拍级连接复用：一拍数据组装共用同一只读连接。
            # 第二轮 Stage5：升级为 GUI 生命周期级单例（_db_connect_cached，
            # 跨拍复用，每次返回前 rollback 刷新 WAL 读快照，保证读到新
            # 提交；单例建立失败时返回 None，此处降级为每拍自开自关）。
            tick_conn = _db_connect_cached(db_path)
            tick_own = False
            if tick_conn is None:
                tick_conn = _db_connect(db_path)
                tick_own = True
            try:
                info = deps.resolve_gui_info(rows, data_dir, db_path, cfg=cfg,
                                             cur=cur_cache,
                                             sess_state=state,
                                             db_conn=tick_conn)
                session_changed = (prev_info is not None
                                   and info.get("session_id")
                                   != prev_info.get("session_id"))
                if session_changed:
                    # 换会话 = 新的徽标上下文：清掉去抖驻留，新会话状态立即生效
                    state["badge_shown"] = None
                force_db = (prev_info is None
                            or session_changed
                            or state["db_read_count"] == 0)
                state["force_db"] = force_db  # 诊断用
                # 钩子时序 -> 状态徽标；只采信**本会话**的事件记录。
                status, spd, turn_stats = resolve_turn_status(
                    status_state_for(status_doc, info.get("session_id")),
                    db_path, info.get("session_id"),
                    conn=tick_conn)
            finally:
                # 单例连接不随拍关闭（跨拍复用，退出时由 quit_app 统一关）；
                # 仅降级路径（单例建立失败、本拍自开）才在拍末关闭。
                if tick_own and tick_conn is not None:
                    try:
                        tick_conn.close()
                    except Exception:
                        pass
            info["status"] = status_debounce(state, status)
            # busy 族取本轮速度；收尾后短时保留最后已知值（缓存随会话变化
            # 作废），held 标记让渲染层加「（上次）」。
            spd, held = speed_hold(state, info["status"], spd,
                                   info.get("session_id"))
            info["status_speed"] = spd
            info["status_speed_held"] = held
            info["turn_stats"] = turn_stats
        except Exception:
            # 不静默吞错：落 err 日志 + 置 error 标记，绝不向上抛
            _log_err(data_dir, "refresh_once error:\n%s" % traceback.format_exc())
            info["error"] = "refresh"
        state["last_info"] = info
        state["db_read_count"] += 1
        if state["db_read_count"] >= deps.DB_READ_INTERVAL:
            state["db_read_count"] = 0
        try:
            self.render_ui(info)
        except Exception:
            # 渲染层异常落 err 日志（pythonw 无 console，静默空白无法诊断）
            try:
                _log_err(data_dir, "render_ui error:\n%s" % traceback.format_exc())
            except Exception:
                pass

    def refresh_tick(self):
        # 全程序唯一的 1 秒排程点：兜底轮询链独占。refresh_once 自吞异常
        # 不会断链；_after 包装再兜一层，绝不让链意外终止。
        self.refresh_once()
        self._after(1000, self.refresh_tick)

    def poll_fs_events(self):
        """文件事件队列消费者：每 FS_POLL_MS 排空一次 event_queue，取到事件
        就合并刷新一次（一次 refresh_once，不逐事件刷新、不排 1 秒定时器）。
        自排队常驻；队列空时零开销轮转。"""
        drained = False
        while True:
            try:
                self.event_queue.get_nowait()
            except queue.Empty:
                break
            drained = True
        if drained:
            # watcher 不可用时队列恒空（drained 恒 False），本循环退化为
            # 每 FS_POLL_MS 一次的空转，纯轮询路径不受影响
            self.refresh_once()
        self._after(self._deps.FS_POLL_MS, self.poll_fs_events)

    # ------------------------------------------------------------------
    # 显隐与轮询
    # ------------------------------------------------------------------
    def _apply_noactivate(self):
        """持续保障 WS_EX_NOACTIVATE：拖动/点击激活会重入清除扩展样式，
        每次显示前重新 Apply + SWP_NOACTIVATE。"""
        deps = self._deps
        if not self.hwnd:
            return
        try:
            style = self._win().get_window_long(self.hwnd, deps.GWL_EXSTYLE)
            if not (style & deps.WS_EX_NOACTIVATE):
                self._win().set_window_long(self.hwnd, deps.GWL_EXSTYLE,
                                            style | deps.WS_EX_NOACTIVATE)
            self._win().set_window_pos(
                self.hwnd, deps.HWND_TOPMOST, 0, 0, 0, 0,
                deps.SWP_NOMOVE | deps.SWP_NOSIZE | deps.SWP_NOACTIVATE
                | deps.SWP_NOOWNERZORDER,
            )
        except Exception:
            pass

    def set_visible(self, show):
        if not self.hwnd:
            return
        if show == self.state["shown"]:
            return
        self.state["shown"] = show
        if show:
            # 恢复可见：强制下一拍全量重读（隐藏期间跳过数据组装，model/title
            # 等缓存可能陈旧），自然补读出新数据，不残留旧值
            self.state["db_read_count"] = 0
        try:
            if show:
                self._win().show_window(self.hwnd, self._deps.SW_SHOWNOACTIVATE)
            else:
                self._win().show_window(self.hwnd, self._deps.SW_HIDE)
        except Exception:
            pass

    def poll(self):
        deps = self._deps
        data_dir = self.data_dir
        state = self.state
        cfg = self.cfg
        refresh_ms = self._initial_refresh_ms
        hwnd = self.hwnd
        # 生命周期绑定（0.9.3）：宿主 ZCode 进程真退出 -> 小条一起退出。
        # 刻意只看进程表不看窗口——ZCode 最小化/收进托盘时窗口判定同样失败，
        # 那时只该由下面的显隐逻辑隐藏，不该把小条退出。
        quit_now, state["zcode_gone_since"] = deps.zcode_gone_exit_due(
            deps.zcode_app_running(), state.get("zcode_gone_since"), time_ms())
        if quit_now:
            _log_err(data_dir, "zcode 进程缺席满 %dms，小条随宿主退出"
                     % deps.ZCODE_GONE_QUIT_MS)
            self.set_visible(False)
            try:
                # mainloop 返回 -> run 的 finally 清 statusbar.pid，
                # 下次 ZCode 的 SessionStart 钩子据此判断该重开一条。
                self.root.quit()
            except Exception:
                pass
            return
        if not hwnd:
            self._after(state.get("refresh_ms", refresh_ms), self.poll)
            return
        try:
            # 正在拖动：跳过前台/最小化/找窗等全部隐藏判定（拖动中绝不
            # withdraw），也不贴边 MoveWindow；释放后恢复完整显隐判定 +
            # manual_position 接管停靠位置。
            # 0.9.6：条件补上手势机的 hand_gesture.dragging（收起态拖动）。
            # 收起分支原先只判 state["dragging"]（完整态专用，收起态恒 False），
            # 于是 poll 每拍（~1 秒）都按记忆/默认位 MoveWindow 把手拽回去
            # ——拖动刚改好就会变成「拖一下、弹回一下」。
            hg = self.hand_gesture
            if state.get("dragging") or hg.dragging:
                self._apply_noactivate()   # 拖动中显示同样持续保障 NOACTIVATE
                self.set_visible(True)
                return
            collapsed = bool(state.get("collapsed"))
            if collapsed:
                # 收起态分支：收起把手**不依赖 ZCode 窗口查找**（ZCode 标题
                # 是终端风格时可能找不到，下一拍 poll 用 SW_HIDE 会把双击收起
                # 后的把手藏掉）；坐标/工作区判定**瞬时失败**不再 SW_HIDE，
                # 回退默认停靠并保持可见；判定抽为模块级
                # collapsed_poll_decision（依赖显式注入，可独立重放测试），
                # 本方法只做副作用与诊断落盘。
                bar_w = state.get("cur_w") or deps.HANDLE_W_DEFAULT

                def _collapsed_err(name):
                    # 同因连续失败只记一行（瞬时失败通常下一拍自愈；持续失败
                    # 也不刷屏），判定恢复后由成功路径清 last_collapse_err。
                    deps._throttled_collapsed_err(state, data_dir, name)

                visible, hxy = collapsed_poll_decision(
                    state, cfg, bar_w,
                    alive=deps._zcode_process_alive(),
                    zcode_hwnd=deps.find_zcode_window(),
                    is_iconic=self._win().is_iconic,
                    current_xy=self.current_window_xy,
                    work_area=deps.work_area_of_rect,
                    valid_and_clamped=lambda xy, wa: (
                        valid_and_clamped_handle_xy(xy, wa, bar_w)),
                    default_dock=self._default_dock_xy,
                    on_err=_collapsed_err,
                )
                if not visible:
                    self.set_visible(False)
                    return
                if state.get("last_collapse_err") is not None:
                    state["last_collapse_err"] = None   # 判定恢复 -> 允许再记
                if hxy is not None and hxy != state["last_xy"]:
                    self._win().move_window(hwnd, hxy[0], hxy[1], bar_w,
                                            deps.HANDLE_H, True)
                    state["last_xy"] = hxy
                self.set_visible(True)
                return
            # 完整态（未收起）：贴 ZCode 底部外沿（水平居中），钳制工作区。
            # 可见性只由「ZCode 窗口存在 + 未最小化 + 矩形可得」决定；判定抽为
            # 模块级 expanded_poll_decision，本方法只做副作用。
            bar_w = state.get("cur_w") or deps.WINDOW_W
            visible, xy = expanded_poll_decision(
                state, bar_w,
                zcode_hwnd=deps.find_zcode_window(),
                is_iconic=self._win().is_iconic,
                rect_of=deps.window_rect_of,
                foreground=deps.is_foreground_zcode,
                dock=deps.dock_rect,
                clamp=deps.clamp_to_work_area,
                work_area=deps.work_area_of_rect,
            )
            if not visible:
                self.set_visible(False)
                return
            self._apply_noactivate()
            # xy is None：manual_position，停在用户放下的位置，不动坐标也不记
            # last_xy（与外提前同一支路）。
            if xy is not None and xy != state["last_xy"]:
                self._win().move_window(hwnd, xy[0], xy[1], bar_w,
                                        deps.WINDOW_H, True)
                state["last_xy"] = xy
            self.set_visible(True)
        except Exception as e:
            # 任何异常只隐藏或保持现状，绝不让小条抢焦点、绝不弹错。
            # 仅隐藏本拍，下一拍 poll 仍会重新判定显示（异常多为瞬时，
            # fail-closed 防抢焦点但不可把条永远藏没）。
            _log_throttled(self.data_dir, self._poll_err_cache, e)
            try:
                self.set_visible(False)
            except Exception:
                pass
        finally:
            self._after(state.get("refresh_ms", refresh_ms), self.poll)
