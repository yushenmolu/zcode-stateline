# -*- coding: utf-8 -*-
"""UIA 主动标签探测（实验性，默认关）——ZCode warm 切换的候选感知路径。

== 真机复核（2026-09-19，本模块直调，逐槽/逐 ControlType 取证）==

手工 COM 绑定此前**从未真正跑通**，probe 恒 None 的根因不是「UIA 不可达」，
而是绑定代码本身有错（清单见下）。修好后同一进程实测：ZCode 主窗口
（Class=Chrome_WidgetWin_1, Name='ZCode'）的 Electron 内容**确实已暴露给 UIA**
——按 ControlType 逐个 id 扫 descendants：

  Button(50000)=189 ['搜索 Ctrl+K','自动化','插件市场','收起全部','归档'…]
  ListItem(50007)=84（10 项的 Name 以数据库里的会话标题开头，全在 idx≤15；
    idx≥21 那 63 项 Name 看是当前会话的消息正文，如
    '根因：编辑器把“当前在哪一章”只记在页面内存里…'）
  Text(50020)=358  List=27  Group=59  Pane=9  Document=1('ZCode')
  Tab(50018)=1（无名容器）  TabItem(50019)=2（'分组'/'项目'，侧栏模式切换）

**但仍然取不到「当前是哪个会话」**，两道闸各断一次：
1. **没有选中态**：IsSelected 为真的 63 项全在 idx≥21 的消息正文段；拿数据库
   237 个标题去前缀匹配这 84 个 Name，命中 10 项（= 侧栏当前渲染出来的会话
   行），**10 项全为 False**——其中包括正开着的那个会话。
   选中态指不出当前会话。
2. **Name 不是裸标题**：侧栏行的 Name 是「会话标题 + 相对时间」拼起来的
   （形如「标题+刚刚」「标题+8天」），而
   `resolve_sid_by_title` 只做「精确相等」和「标题以省略号截断时按前缀 LIKE」
   两档，方向正好相反，一条都命中不了。

所以 `probe_active_tab_title` 依旧返回 None，理由从「树不可达」改成「可达但
没有选中态可取」。`enable_uia_tab_probe` 继续默认关；将来若要启用，得换判别量
（SelectionItemPattern / AutomationId / 类名）**并**给反查加一档
「Name 以标题开头」的匹配，光修这条路径的 bug 不够。

2026-09-07 那次 PowerShell UIAutomationClient dump（全树 13 个元素、
TabItem=0、「内容未暴露」）在当前版本上**已不成立**，只作为历史结论保留。

已核对的 vtable 槽位（QI(IDispatch) 返回 E_NOINTERFACE ⇒ IUIAutomation 以
IUnknown 为基，首方法在槽 3）：

  IUIAutomation:  **6 ElementFromHandle**, 21 CreateTrueCondition,
                  22 CreateFalseCondition, **23 CreatePropertyCondition**
  IUIAutomationElement: 5 FindFirst, **6 FindAll**, 7/8 *BuildCache,
                        **10 GetCurrentPropertyValue**
  IUIAutomationElementArray: 2 Release, 3 get_Length, 4 GetElement

加粗的是**逐个自证过**的（不靠记忆）：ElementFromHandle(6) 返回的对象
QI(IUIAutomationElement) 成功且 Name='ZCode'；CreatePropertyCondition(23) 的
产物喂给 FindAll(6)+ControlType=Button 返回数组 len=189，GetElement(i) 逐个
读出的 Name 就是界面上看得见的按钮文字。5/7/8 靠排除法确认（5 只回一个元素，
7/8 按 FindAll 签名调用直接 AV——它们多一个 cacheRequest 入参）。
21/22 只验到「单出参、返回 S_OK 且给出一个对象」这一层，具体身份按官方顺序
推算；本模块不用它们。同理「15 是 get_ControlViewWalker」也是推算而非实测。

自证为什么必须做：这条链路此前所有单元测试都走 mock，`_vtbl_func` 坏了很久
没有任何信号。

今天从这条链路上挖掉并修好的 7 处错（1/3/4/5 静态可见，2/6/7 靠真机取证）：
1. `_vtbl_func` 写成 `.contents[slot]`：`.contents` 给的是 c_void_p 实例，
   不可下标 -> 每次 TypeError 被 except 吞掉 -> 全模块 COM 调用恒返回 None。
2. `IID_IUIAutomation` 值错：原 `{30cbe57d-d9d3-4184-8817-598639edb8b4}`
   CoCreateInstance 返回 E_NOINTERFACE(0x80004002)；注册表 HKCR\\Interface
   核对到的 `{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}` 返回 S_OK。
3. GetCurrentPropertyValue 的 VARIANT 出参结构体只有 10 字节（COM 按 16 字节
   写回，越界改坏栈帧）。
4. `VT_BSTR` 从未定义（用到即 NameError）。
5. `SysFreeString` 未声明 argtypes（x64 上按 32 位传指针 -> OverflowError，
   于是「读到文本之后才失败」，Name 恒 None 且看不出原因）。
6. CreatePropertyCondition 槽 15 -> 23，条件值 VT_BOOL -> VT_I4。
7. TabItem 的 ControlType id 50018 -> 50019。

另记一笔：所有绑定都用 `restype=ctypes.HRESULT`，ctypes 对失败码是**抛
OSError**而不是返回负数，所以 `if hr != S_OK` 分支实际靠外层 except 兜住
（行为等价，但光看返回值看不到失败原因）。

== 模块行为 ==

probe_active_tab_title 实现完整的最佳尝试路径：找主窗口 hwnd ->
CoInitialize -> CoCreateInstance(CUIAutomation) -> ElementFromHandle ->
全树搜 ControlType=TabItem（用 IUIAutomation 的 CreatePropertyCondition）
-> 取 IsSelected=True 元素的 Name。当前该路径能一路走到底，但树里仅有的
2 个 TabItem（分组/项目）都未选中，故返回 None。任何一步失败返回 None，
不抛、不猜。开关关闭/信号缺席时判定链行为与 Stage 1/2 完全一致。

COM 指针收尾：uia / element / condition / 数组 / 逐个元素都显式 Release
（探测每 3 个 tick 一次，长期跑的 GUI 进程里漏 Release 就是稳定泄漏）。

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
# ControlType id 经真机核对（2026-09-19 逐个 id 扫 ZCode 主窗口，见 docstring）：
# 50018 是 Tab（1 个无名容器），50019 才是 TabItem（'分组'/'项目' 两个）。
# 此前把 TabItem 写成 50018，差一个。
UIA_TabItemControlTypeId = 50019
UIA_ControlTypePropertyId = 30003
UIA_IsSelectedPropertyId = 30022
UIA_NamePropertyId = 30005
VT_BOOL = 11
VT_BSTR = 8
VT_I4 = 3


class _VARIANT_UNION(ctypes.Union):
    """VARIANT 的 8 字节载荷（只声明本模块用到的成员）。"""
    _fields_ = [("boolVal", ctypes.c_short),
                ("lVal", ctypes.c_int),
                ("bstrVal", ctypes.c_void_p),
                ("pad", ctypes.c_ubyte * 8)]


class _VARIANT(ctypes.Structure):
    """Win32 VARIANT，必须整块 16 字节。

    GetCurrentPropertyValue 的出参是 VARIANT*，按 16 字节写回；若声明成
    2+2+2+2+2=10 字节的结构体，COM 会越界写坏栈帧（不报错，随机崩在别处）。
    """
    _fields_ = [("vt", ctypes.c_ushort),
                ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort),
                ("wReserved3", ctypes.c_ushort),
                ("u", _VARIANT_UNION)]


assert ctypes.sizeof(_VARIANT) == 16

_SysFreeString = None


def _free_bstr(addr):
    """释放 UIA 交还的 BSTR。

    必须显式声明签名：不设 argtypes 时 ctypes 按 32 位 int 传指针，x64 上
    地址高位溢出直接抛 OverflowError（实测），于是取到文本的那一行之后才
    失败——Name 永远返回不了，且异常被外层 except 吞掉，看不出原因。
    """
    global _SysFreeString
    if _SysFreeString is None:
        fn = ctypes.windll.oleaut32.SysFreeString
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_long   # HRESULT
        _SysFreeString = fn
    _SysFreeString(addr)


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


# CUIAutomation CLSID 与 IUIAutomation IID。
# IID 取自本机注册表 HKCR\Interface（其默认值就是接口名），且实测
# CoCreateInstance 返回 S_OK；旧值 {30cbe57d-d9d3-4184-8817-598639edb8b4}
# 注册表里根本不存在，CoCreateInstance 恒返回 E_NOINTERFACE——本模块此前
# 从未真正取到过接口指针。
_CLSID_CUIAutomation = _guid_from_string(
    "{ff48dba4-60ef-4201-aa87-54103eef594e}")
_IID_IUIAutomation = _guid_from_string(
    "{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}")


def probe_active_tab_title(process_name="ZCode"):
    """经 UIA 枚举 ZCode 主窗口标签栏，返回当前选中标签的标题文本。

    实测（2026-09-19）：树可达且有内容，但取不到「当前是哪个」——IsSelected
    为真的只有消息气泡那一段（84 个 ListItem 里 63 个），侧栏会话项与仅有的
    2 个 TabItem（分组/项目）全部为假，故恒返回 None；单次约 85ms。
    任何一步失败返回 None，不抛。
    """
    try:
        user32 = ctypes.windll.user32
        # 显式 HWND 返回类型：默认 c_int 在 x64 上可能把句柄截成负数，
        # 后面按 c_void_p 传给 ElementFromHandle 会直接抛。
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
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
            try:
                element = _uia_element_from_handle(uia, hwnd)
                if element is None:
                    return None
                try:
                    return _uia_find_selected_tab_name(uia, element)
                finally:
                    _com_release(element)
            finally:
                _com_release(uia)
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
    """取 COM 对象 vtable 第 slot 槽函数并按签名绑定。失败返回 None。

    两次解引用：对象地址 -> 第 0 个 qword = vtable 地址；vtable 地址 +
    8*slot = 函数地址。此前写成 `cast(...).contents[slot]`，`.contents` 给出
    的是 c_void_p **实例**而非数组，实例不可下标 -> 每次都在 except 里被吞掉，
    于是全模块 COM 调用恒返回 None（probe 恒 None 的真正原因）。
    """
    try:
        # 空指针必须挡在这里：往下 cast 会解引用地址 0，那是 AV 直接崩进程，
        # 不是能靠 except 兜住的异常。（不写 `if not com_ptr` 是因为 c_void_p
        # 实例的真假语义跨版本不一致，取 .value 才是确定的。）
        addr = (com_ptr.value
                if isinstance(com_ptr, ctypes.c_void_p) else com_ptr)
        if not addr:
            return None
        vtbl = ctypes.cast(addr, ctypes.POINTER(ctypes.c_void_p))[0]
        if not vtbl:
            return None
        fn_addr = ctypes.cast(ctypes.c_void_p(vtbl),
                              ctypes.POINTER(ctypes.c_void_p * (slot + 1)))[0][slot]
        if not fn_addr:
            return None
        proto = ctypes.CFUNCTYPE(restype, *argtypes)
        return proto(fn_addr)
    except Exception:
        return None


def _com_release(ptr):
    """IUnknown::Release（槽 2）。返回引用计数，这里不关心，失败也静默：
    收尾失败不该让已经读到的结果作废。空指针由 _vtbl_func 的守卫挡掉。"""
    try:
        fn = _vtbl_func(ptr, 2, ctypes.c_ulong, ctypes.c_void_p)
        if fn is not None:
            fn(ptr)
    except Exception:
        pass


def _uia_element_from_handle(uia_ptr, hwnd):
    """IUIAutomation::ElementFromHandle（vtable 槽 6）-> IUIAutomationElement。"""
    try:
        # IUIAutomation 以 IUnknown 为基（QI(IDispatch)=E_NOINTERFACE），
        # 首方法在槽 3：3 CompareElements,4 CompareRuntimeIds,5 GetRootElement,
        # 6 ElementFromHandle,7 ElementFromPoint,8 GetFocusedElement, ...
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


def _uia_create_control_type_condition(uia_ptr,
                                       control_type=UIA_TabItemControlTypeId):
    """IUIAutomation::CreatePropertyCondition(ControlType=<control_type>)。

    vtable 槽 **23**，真机自证过：本函数的产物交给 FindAll 能搜出界面上的按钮。
    官方顺序为 21 CreateTrueCondition、22 CreateFalseCondition、23
    CreatePropertyCondition；此前用的 15 按同一顺序推算是 walker 访问器，
    签名不符。属性值按 UIA 约定以 **VT_I4** 承载 ControlType 的 id（此前写成
    VT_BOOL：条件值类型不对，永远匹配不到元素）。失败返回 None。
    """
    try:
        var = _VARIANT()
        var.vt = VT_I4
        var.u.lVal = int(control_type)
        fn = _vtbl_func(uia_ptr, 23, ctypes.HRESULT,
                        ctypes.c_void_p, ctypes.c_int,
                        _VARIANT, ctypes.POINTER(ctypes.c_void_p))
        if fn is None:
            return None
        out = ctypes.c_void_p()
        hr = fn(uia_ptr, UIA_ControlTypePropertyId, var, ctypes.byref(out))
        if hr != S_OK or not out:
            return None
        return out
    except Exception:
        return None


def _uia_find_all(uia_element_ptr, scope, condition_ptr):
    """IUIAutomationElement::FindAll（vtable 槽 6）-> 数组指针或 None。"""
    try:
        # 真机核对（2026-09-19）：IUIAutomationElement 首方法在槽 3
        # （3 SetFocus,4 GetRuntimeId,5 FindFirst,6 FindAll,7 FindFirstBuildCache,
        # 8 FindAllBuildCache,9 BuildUpdatedCache,10 GetCurrentPropertyValue）。
        # 判别按行为不按记忆：5 号槽返回单个元素，6 号返回数组
        # （get_Length@3=189、GetElement@4 逐个读出界面按钮名），
        # 7/8 号按此签名调用直接 AV（多一个 cacheRequest 入参）。
        # 此处曾用 7 -> 恒 None，整棵搜索从未真正执行过。
        fn = _vtbl_func(uia_element_ptr, 6, ctypes.HRESULT,
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


def _uia_get_current_property(element_ptr, prop_id, vt_expect):
    """IUIAutomationElement::GetCurrentPropertyValue（vtable 槽 10）。

    只在返回的 VARIANT 类型正是 vt_expect 时给出结构体，否则 None：类型不符
    说明属性取错或实现变了，硬解载荷会读到垃圾。调用方用完 BSTR 须自行
    SysFreeString（见 _uia_get_name）。
    """
    try:
        # vtable 续：8 FindFirstBuildCache,9 BuildUpdatedCache,
        # 10 GetCurrentPropertyValue,11 GetCurrentPropertyValueBuildCache, ...
        # 官方顺序：3 SetFocus,4 GetRuntimeId,5 FindFirst,6 FindAllBuildCache,
        # 7 FindAll,8 FindFirstBuildCache,9 BuildUpdatedCache,
        # 10 GetCurrentPropertyValue, 11 GetCurrentPropertyValueBuildCache, ...
        fn = _vtbl_func(element_ptr, 10, ctypes.HRESULT,
                        ctypes.c_void_p, ctypes.c_int,
                        ctypes.POINTER(_VARIANT))
        if fn is None:
            return None
        out = _VARIANT()
        hr = fn(element_ptr, prop_id, ctypes.byref(out))
        if hr != S_OK or out.vt != vt_expect:
            return None
        return out
    except Exception:
        return None


def _uia_get_name(element_ptr):
    """IUIAutomationElement::get_CurrentName（vtable 槽 43 起，属性访问器区）。

    直接用 GetCurrentPropertyValue(UIA_NamePropertyId) 更稳（槽 10）。"""
    try:
        var = _uia_get_current_property(element_ptr, UIA_NamePropertyId,
                                        VT_BSTR)
        if var is None or not var.u.bstrVal:
            return None
        val = ctypes.wstring_at(var.u.bstrVal)
        try:
            _free_bstr(var.u.bstrVal)
        except Exception:
            pass   # 释放失败只是泄漏一块 BSTR，不该因此丢掉读到的标题
        return val
    except Exception:
        return None


def _uia_find_selected_tab_name(uia_ptr, root_element_ptr):
    """全树搜 TabItem -> 取 IsSelected=True 的 Name；无命中返回 None。"""
    try:
        cond = _uia_create_control_type_condition(uia_ptr)
        if cond is None:
            return None
        try:
            arr = _uia_find_all(root_element_ptr, TreeScope_Descendants, cond)
        finally:
            _com_release(cond)
        if arr is None:
            return None
        # IUIAutomationElementArray: 0 QI,1 AddRef,2 Release,
        # 3 get_Length,4 GetElement（槽位经真机核对，见模块 docstring）
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
                try:
                    sel = _uia_get_current_property(item,
                                                    UIA_IsSelectedPropertyId,
                                                    VT_BOOL)
                    if sel is not None and sel.u.boolVal:
                        name = _uia_get_name(item)
                        if name:
                            return name
                finally:
                    _com_release(item)
        finally:
            _com_release(arr)
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
