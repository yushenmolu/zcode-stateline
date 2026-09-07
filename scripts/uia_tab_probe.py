# -*- coding: utf-8 -*-
"""UIA 主动标签探测（实验性，默认关）——ZCode warm 切换唯一感知路径。

== 实测结论（2026-09-07，PowerShell UIAutomationClient dump）==

ZCode 主窗口（PID 14940，HWND 4066248，Class=Chrome_WidgetWin_1，Title=ZCode）
UIA 树共 13 个后代，全部为窗口框架元素：
  [0] Pane Class=Intermediate D3D Window
  [1] Pane Class=RootView Name=ZCode
  [2] Pane Class=NonClientView
  [3] Pane Class=WinFrameView
  [4] Pane Class=WinCaptionButtonContainer
  [5-7] Button Class=WinCaptionButton（最小化/恢复/关闭）
  [8] Pane Class=ClientView
  [9-12] Pane Class=View（x4，无 Name）
全树 TabItem=0、TabControl=0；其余 ZCode 进程均无主窗口。
结论：**Electron/Chromium 渲染内容未向 UIA 暴露，标签栏不可达**，
probe_active_tab_title 在当前 ZCode 版本上恒返回 None。

== 模块行为 ==

probe_active_tab_title 实现完整的最佳尝试路径：找主窗口 hwnd ->
CoInitialize -> CoCreateInstance(CUIAutomation) -> ElementFromHandle ->
全树搜 ControlType=TabItem（用 IUIAutomation 的 CreatePropertyCondition）
-> 取 IsSelected=True 元素的 Name。若未来 ZCode AX 树暴露标签栏则自动
生效；当前实测下 TabItem 搜索返回 0 元素 -> None。
任何一步失败返回 None，不抛、不猜。开关关闭/信号缺席时判定链行为与
Stage 1/2 完全一致。

resolve_sid_by_title 与 UIA 无关：标题反查 session_id（精确唯一命中
才返回；歧义/无匹配返回 None——宁可不切，不可错切）。

仅标准库（ctypes + ole32/oleaut/UIAutomationCore COM 直调），无第三方
依赖。COM 调用全部手工 vtable 绑定。
"""
import ctypes
import ctypes.wintypes as wintypes
import sqlite3

# ---- COM / UIA 常量 ----
CLSCTX_INPROC_SERVER = 1
S_OK = 0
COINIT_APARTMENTTHREADED = 2
TreeScope_Descendants = 4
UIA_TabItemControlTypeId = 50018   # ControlType_TabItem
UIA_IsSelectedPropertyId = 30022
VT_BOOL = 11

_IID_CUIAutomation = "{ffdb8823-4844-4f2f-9d05-9c4c1a1b2b0a}"


def _guid_from_string(s):
    """"{...}" 字符串 -> ctypes GUID 结构体；失败返回 None。"""
    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong),
                    ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8)]
    g = _GUID()
    hr = ctypes.windll.ole32.CLSIDFromString(
        ctypes.c_wchar_p(s), ctypes.byref(g))
    if hr != S_OK:
        return None
    return g


# CUIAutomation CLSID 与 IUIAutomation IID（公开常量）
_CLSID_CUIAutomation = _guid_from_string(
    "{ff48dba4-60ef-4201-aa87-54103eef594e}")
_IID_IUIAutomation = _guid_from_string(
    "{30cbe57d-d9d3-4184-8817-598639edb8b4}")


def probe_active_tab_title(process_name="ZCode"):
    """经 UIA 枚举 ZCode 主窗口标签栏，返回当前选中标签的标题文本。

    实测当前 ZCode UIA 树无 TabItem（见模块 docstring），恒返回 None；
    任何一步失败返回 None，不抛。
    """
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW("Chrome_WidgetWin_1", process_name)
        if not hwnd:
            return None
        ole32 = ctypes.windll.ole32
        coinit_here = False
        try:
            hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            # RPC_E_CHANGED_MODE(0x80010106) 表示线程已有不同模型，可继续用
            coinit_here = (hr == S_OK)
        except Exception:
            coinit_here = False
        try:
            uia = _create_uia_automation()
            if uia is None:
                return None
            element = _uia_element_from_handle(uia, hwnd)
            if element is None:
                return None
            return _uia_find_selected_tab_name(uia, element)
        finally:
            if coinit_here:
                try:
                    ole32.CoUninitialize()
                except Exception:
                    pass
    except Exception:
        return None
    return None


def _create_uia_automation():
    """CoCreateInstance(CLSID_CUIAutomation, IID_IUIAutomation) -> COM 指针。

    返回 (vtbl_getter 可用的裸指针) 或 None。失败不抛。
    """
    try:
        if _CLSID_CUIAutomation is None or _IID_IUIAutomation is None:
            return None
        ole32 = ctypes.windll.ole32
        p = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_CUIAutomation), None,
            CLSCTX_INPROC_SERVER, ctypes.byref(_IID_IUIAutomation),
            ctypes.byref(p))
        if hr != S_OK or not p:
            return None
        return p
    except Exception:
        return None


def _vtbl_func(com_ptr, slot, restype, *argtypes):
    """取 COM 对象 vtable 第 slot 槽函数并按签名绑定。失败返回 None。"""
    try:
        vtbl = ctypes.cast(
            ctypes.cast(com_ptr, ctypes.POINTER(ctypes.c_void_p)).contents,
            ctypes.POINTER(ctypes.c_void_p)).contents
        addr = vtbl[slot]
        if not addr:
            return None
        proto = ctypes.CFUNCTYPE(restype, *argtypes)
        return proto(addr)
    except Exception:
        return None


def _uia_element_from_handle(uia_ptr, hwnd):
    """IUIAutomation::ElementFromHandle（vtable 槽 6）-> IUIAutomationElement。"""
    try:
        # IUIAutomation vtable: 0 QI,1 AddRef,2 Release,3 CompareRuntimeIds,
        # 4 GetRootElement,5 GetRootElementBuildCache,6 ElementFromHandle,...
        fn = _vtbl_func(uia_ptr, 6, ctypes.HRESULT,
                        ctypes.c_void_p, wintypes.HWND,
                        ctypes.POINTER(ctypes.c_void_p))
        if fn is None:
            return None
        out = ctypes.c_void_p()
        hr = fn(uia_ptr, hwnd, ctypes.byref(out))
        if hr != S_OK or not out:
            return None
        return out
    except Exception:
        return None


def _uia_create_true_condition(uia_ptr):
    """IUIAutomation::CreateTrueCondition（vtable 槽 11）。失败返回 None。"""
    try:
        # vtable: ...8 GetFocusedElement,9 GetFocusedElementBuildCache,
        # 10 GetRootElementBuildCache? 顺序以官方头文件为准：
        # 3 CompareElements,4 CompareRuntimeIds,5 GetRootElement,
        # 6 ElementFromHandle,7 ElementFromPoint,8 GetFocusedElement,
        # 9 GetRootElementBuildCache,10 ElementFromHandleBuildCache,
        # 11 ElementFromPointBuildCache,12 GetFocusedElementBuildCache,
        # 13 CreateTrueCondition, ...
        fn = _vtbl_func(uia_ptr, 13, ctypes.HRESULT,
                        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        if fn is None:
            return None
        out = ctypes.c_void_p()
        hr = fn(uia_ptr, ctypes.byref(out))
        if hr != S_OK or not out:
            return None
        return out
    except Exception:
        return None


def _uia_create_tab_item_condition(uia_ptr):
    """IUIAutomation::CreatePropertyCondition（vtable 槽 16 或 17，
    按官方顺序：13 CreateTrueCondition,14 CreateFalseCondition,
    15 CreatePropertyCondition,16 CreatePropertyConditionEx, ...）。"""
    try:
        class _VARIANT(ctypes.Structure):
            _fields_ = [("vt", ctypes.c_ushort),
                        ("wReserved1", ctypes.c_ushort),
                        ("wReserved2", ctypes.c_ushort),
                        ("wReserved3", ctypes.c_ushort),
                        ("data", ctypes.c_ulonglong)]
        var = _VARIANT()
        var.vt = VT_BOOL
        var.data = 1  # VARIANT_TRUE
        # CreatePropertyCondition 在槽 15（QI/AddRef/Release 占 0-2）
        fn = _vtbl_func(uia_ptr, 15, ctypes.HRESULT,
                        ctypes.c_void_p, ctypes.c_int,
                        _VARIANT, ctypes.POINTER(ctypes.c_void_p))
        if fn is None:
            return None
        out = ctypes.c_void_p()
        hr = fn(uia_ptr, UIA_TabItemControlTypeId, var, ctypes.byref(out))
        if hr != S_OK or not out:
            return None
        return out
    except Exception:
        return None


def _uia_find_all(uia_element_ptr, scope, condition_ptr):
    """IUIAutomationElement::FindAll（vtable 槽 7）-> 数组指针或 None。"""
    try:
        # IUIAutomationElement vtable: 0 QI,1 AddRef,2 Release,
        # 3 SetFocus,4 GetRuntimeId,5 FindFirst,6 FindAllBuildCache,7 FindAll, ...
        fn = _vtbl_func(uia_element_ptr, 7, ctypes.HRESULT,
                        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p))
        if fn is None:
            return None
        out = ctypes.c_void_p()
        hr = fn(uia_element_ptr, scope, condition_ptr, ctypes.byref(out))
        if hr != S_OK or not out:
            return None
        return out
    except Exception:
        return None


class _VARIANT_BOOL(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort),
                ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort),
                ("wReserved3", ctypes.c_ushort),
                ("boolVal", ctypes.c_short)]


def _uia_get_current_property(element_ptr, prop_id, vt_expect):
    """IUIAutomationElement::GetCurrentPropertyValue（vtable 槽 12）。"""
    try:
        # vtable 续：8 FindFirstBuildCache,9 BuildUpdatedCache,
        # 10 GetCurrentPropertyValue,11 GetCurrentPropertyValueBuildCache, ...
        # 官方顺序：3 SetFocus,4 GetRuntimeId,5 FindFirst,6 FindAllBuildCache,
        # 7 FindAll,8 FindFirstBuildCache,9 BuildUpdatedCache,
        # 10 GetCurrentPropertyValue, 11 GetCurrentPropertyValueBuildCache, ...
        fn = _vtbl_func(element_ptr, 10, ctypes.HRESULT,
                        ctypes.c_void_p, ctypes.c_int, _VARIANT_BOOL,
                        ctypes.POINTER(_VARIANT_BOOL))
        if fn is None:
            return None
        out = _VARIANT_BOOL()
        out.vt = 0
        hr = fn(element_ptr, prop_id, out, ctypes.byref(out))
        if hr != S_OK:
            return None
        return out
    except Exception:
        return None


def _uia_get_name(element_ptr):
    """IUIAutomationElement::get_CurrentName（vtable 槽 43 起，属性访问器区）。

    直接用 GetCurrentPropertyValue(UIA_NamePropertyId) 更稳（槽 10）。"""
    try:
        class _VARIANT_STR(ctypes.Structure):
            _fields_ = [("vt", ctypes.c_ushort),
                        ("wReserved1", ctypes.c_ushort),
                        ("wReserved2", ctypes.c_ushort),
                        ("wReserved3", ctypes.c_ushort),
                        ("bstrVal", ctypes.c_void_p)]
        fn = _vtbl_func(element_ptr, 10, ctypes.HRESULT,
                        ctypes.c_void_p, ctypes.c_int, _VARIANT_STR,
                        ctypes.POINTER(_VARIANT_STR))
        if fn is None:
            return None
        out = _VARIANT_STR()
        hr = fn(element_ptr, 30005, out, ctypes.byref(out))
        if hr != S_OK or out.vt != VT_BSTR or not out.bstrVal:
            return None
        val = ctypes.wstring_at(out.bstrVal)
        ctypes.windll.oleaut32.SysFreeString(out.bstrVal)
        return val
    except Exception:
        return None


def _uia_find_selected_tab_name(uia_ptr, root_element_ptr):
    """全树搜 TabItem -> 取 IsSelected=True 的 Name；无命中返回 None。"""
    try:
        cond = _uia_create_tab_item_condition(uia_ptr)
        if cond is None:
            return None
        arr = _uia_find_all(root_element_ptr, TreeScope_Descendants, cond)
        if arr is None:
            return None
        # IUIAutomationElementArray: 0 QI,1 AddRef,2 Release,
        # 3 get_Length,4 GetElement
        try:
            fn_len = _vtbl_func(arr, 3, ctypes.HRESULT,
                                ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_int))
            if fn_len is None:
                return None
            length = ctypes.c_int()
            if fn_len(arr, ctypes.byref(length)) != S_OK:
                return None
            fn_get = _vtbl_func(arr, 4, ctypes.HRESULT,
                                ctypes.c_void_p, ctypes.c_int,
                                ctypes.POINTER(ctypes.c_void_p))
            if fn_get is None:
                return None
            for i in range(length.value):
                item = ctypes.c_void_p()
                if fn_get(arr, i, ctypes.byref(item)) != S_OK or not item:
                    continue
                sel = _uia_get_current_property(item, UIA_IsSelectedPropertyId,
                                                VT_BOOL)
                if sel is not None and sel.vt == VT_BOOL and sel.boolVal:
                    name = _uia_get_name(item)
                    if name:
                        return name
        finally:
            try:
                release = _vtbl_func(arr, 2, ctypes.HRESULT, ctypes.c_void_p)
                if release:
                    release(arr)
            except Exception:
                pass
        return None
    except Exception:
        return None


def resolve_sid_by_title(conn, title):
    """用标题反查 session_id（与 UIA 无关，纯 sqlite）。

    - 精确匹配 session.title；0 行 -> None；>1 行（重名歧义）-> None
      （宁可不切，不可错切）。
    - 标题带省略号截断（"…" U+2026 或 "..." 结尾）时退化 LIKE '前缀%'，
      仍多行 -> None。
    - conn 为 None / 空标题 / 查询失败 -> None，不抛。
    """
    if conn is None or not title or not isinstance(title, str):
        return None
    t = title.strip()
    if not t:
        return None
    try:
        truncated = False
        if t.endswith("\u2026"):
            t = t[:-1]
            truncated = True
        elif t.endswith("..."):
            t = t[:-3]
            truncated = True
        t = t.strip()
        if not t:
            return None
        if truncated:
            # LIKE 转义 % _（sqlite ESCAPE 语法）；>1 行一律 None，无需排序
            esc = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = conn.execute(
                "SELECT id FROM session WHERE title LIKE ? ESCAPE '\\' "
                "AND id NOT LIKE 'sess_subagent_%'",
                (esc + "%",)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM session WHERE title = ? "
                "AND id NOT LIKE 'sess_subagent_%'", (t,)).fetchall()
        if len(rows) == 1:
            return rows[0][0]
        return None
    except Exception:
        return None
