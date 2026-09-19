# -*- coding: utf-8 -*-
"""Stage 3: UIA warm 切换探测——标题反查 + VARIANT 布局 + 真机 COM 冒烟测。

probe_active_tab_title 的完整链路依赖 ZCode 窗口的实时 UIA 树（树可达、但没有
选中态可指认当前会话，恒返回 None），不在单测里断言；本文件测三块：
  - resolve_sid_by_title：纯 sqlite 的标题反查语义；
  - _VARIANT / _uia_get_current_property / _uia_get_name：出参内存布局与 BSTR
    读写。GetCurrentPropertyValue 的出参按 16 字节写回，结构体声明小了会越界
    写坏栈（不报错、随机崩在别处），故按真实 ctypes 回调跑一遍，而不是只读代码；
  - TestRealComBinding：真的 CoCreateInstance + AddRef/Release/CreateProperty
    Condition。这条链路此前只被 mock 测过，`_vtbl_func` 坏了很久没人发现（每次
    抛 TypeError 都被 except 吞成 None，看起来像「UIA 树不可达」）。
"""
import ctypes
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import uia_tab_probe as probe


def _mkedb():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE session (id TEXT, title TEXT)")
    return conn


class TestResolveSidByTitle(unittest.TestCase):
    """标题反查：精确唯一命中；0 行 None；重名歧义 None；截断退化前缀。"""

    def test_exact_match_unique(self):
        conn = _mkedb()
        try:
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_a", "我的小说项目"))
            conn.commit()
            self.assertEqual(probe.resolve_sid_by_title(conn, "我的小说项目"),
                             "sess_a")
        finally:
            conn.close()

    def test_no_match_returns_none(self):
        conn = _mkedb()
        try:
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_a", "我的小说项目"))
            conn.commit()
            self.assertIsNone(probe.resolve_sid_by_title(conn, "不存在的标题"))
            self.assertIsNone(probe.resolve_sid_by_title(conn, ""))
            self.assertIsNone(probe.resolve_sid_by_title(conn, None))
        finally:
            conn.close()

    def test_ambiguous_returns_none(self):
        """精确匹配多行（重名）-> None（宁可不切，不可错切）。"""
        conn = _mkedb()
        try:
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_a", "untitled"))
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_b", "untitled"))
            conn.commit()
            self.assertIsNone(probe.resolve_sid_by_title(conn, "untitled"))
        finally:
            conn.close()

    def test_truncated_ellipsis_falls_back_to_prefix(self):
        """标题以 …（U+2026）或 ... 结尾 -> LIKE '前缀%'；前缀多行 -> None。"""
        conn = _mkedb()
        try:
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_a", "这是一个很长的会话标题abc"))
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_b", "这是另一个会话标题xyz"))
            conn.commit()
            # 截断唯一命中
            self.assertEqual(
                probe.resolve_sid_by_title(conn, "这是一个很长的会话…"),
                "sess_a")
            self.assertEqual(
                probe.resolve_sid_by_title(conn, "这是一个很长的会话..."),
                "sess_a")
            # 前缀仍多行 -> None
            self.assertIsNone(probe.resolve_sid_by_title(conn, "这是…"))
            # LIKE 转义：前缀含 % 时按字面匹配（防 % 变通配扩大匹配面）。
            # 若未转义，前缀 "100%" 会变成 LIKE '100%%'（= 以 100 开头），
            # 将同时命中 "100%完成" 与 "100x完成" -> 歧义 None；
            # 转义正确则只命中字面前缀 "100%" -> sess_c。
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_c", "100%完成"))
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_d", "100x完成"))
            conn.commit()
            self.assertEqual(probe.resolve_sid_by_title(conn, "100%完成"),
                             "sess_c")
            self.assertEqual(probe.resolve_sid_by_title(conn, "100%…"),
                             "sess_c")
        finally:
            conn.close()


class TestVariantLayout(unittest.TestCase):
    """手工 COM 绑定的出参布局：VARIANT 必须整块 16 字节。"""

    def _fake_fn(self, writer):
        """造一个真实 ctypes 回调，签名同 GetCurrentPropertyValue。"""
        proto = ctypes.CFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_int,
                                 ctypes.POINTER(probe._VARIANT))
        return proto(writer)

    def test_struct_is_16_bytes(self):
        self.assertEqual(ctypes.sizeof(probe._VARIANT), 16)
        self.assertEqual(ctypes.sizeof(probe._VARIANT_UNION), 8)

    def test_wrong_vt_is_rejected(self):
        """返回类型与 vt_expect 不符 -> None（宁可不取，不硬解垃圾载荷）。"""
        def writer(_e, _p, out):
            out.contents.vt = probe.VT_BOOL
            out.contents.u.boolVal = 1
            return probe.S_OK
        with mock.patch.object(probe, "_vtbl_func",
                               return_value=self._fake_fn(writer)):
            self.assertIsNone(
                probe._uia_get_current_property(ctypes.c_void_p(1),
                                                probe.UIA_NamePropertyId,
                                                probe.VT_BSTR))

    def test_bool_payload_roundtrip(self):
        def writer(_e, _p, out):
            out.contents.vt = probe.VT_BOOL
            out.contents.u.boolVal = -1
            return probe.S_OK
        with mock.patch.object(probe, "_vtbl_func",
                               return_value=self._fake_fn(writer)):
            got = probe._uia_get_current_property(
                ctypes.c_void_p(1), probe.UIA_IsSelectedPropertyId,
                probe.VT_BOOL)
        self.assertIsNotNone(got)
        self.assertTrue(got.u.boolVal)

    def test_get_name_reads_and_frees_bstr(self):
        """_uia_get_name 走真实的 VT_BSTR 判定 + SysFreeString 收尾。"""
        oleaut = ctypes.windll.oleaut32
        oleaut.SysAllocString.argtypes = [ctypes.c_wchar_p]
        oleaut.SysAllocString.restype = ctypes.c_void_p
        bstr = oleaut.SysAllocString(u"会话甲")
        self.assertTrue(bstr)

        def writer(_e, _p, out):
            out.contents.vt = probe.VT_BSTR
            out.contents.u.bstrVal = bstr
            return probe.S_OK
        with mock.patch.object(probe, "_vtbl_func",
                               return_value=self._fake_fn(writer)):
            self.assertEqual(probe._uia_get_name(ctypes.c_void_p(1)),
                             u"会话甲")

    def test_garbage_element_ptr_is_none_not_raise(self):
        self.assertIsNone(probe._uia_get_current_property(
            ctypes.c_void_p(0), probe.UIA_IsSelectedPropertyId, probe.VT_BOOL))
        self.assertIsNone(probe._uia_get_name(None))


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class TestRealComBinding(unittest.TestCase):
    """真机 COM 冒烟测：_vtbl_func / _create_uia_automation / _com_release。

    这是本模块历史上唯一没有测试覆盖的环节，也正是它悄悄坏掉——
    `cast(...).contents[slot]` 每次抛 TypeError 都被 `except Exception` 吞成
    None，于是所有探测恒返回 None，看起来却像"ZCode 没暴露 UIA 树"。
    用 AddRef/Release 自证：它们不需要任何窗口，计数能读回就说明绑定真的通。
    """

    def setUp(self):
        self.ole32 = ctypes.windll.ole32
        # S_OK=新初始化 / S_FALSE=已初始化：两者都可继续用，都要在 tearDown 配对
        self.hr = self.ole32.CoInitializeEx(None, probe.COINIT_APARTMENTTHREADED)
        self.assertIn(self.hr & 0xFFFFFFFF, (0x00000000, 0x00000001))

    def tearDown(self):
        self.ole32.CoUninitialize()

    def test_null_pointer_binds_to_none_not_av(self):
        # 空指针必须返回 None：真解引用地址 0 是 AV（杀进程），不是异常
        self.assertIsNone(probe._vtbl_func(
            ctypes.c_void_p(), 6, ctypes.HRESULT, ctypes.c_void_p))
        self.assertIsNone(probe._vtbl_func(
            None, 6, ctypes.HRESULT, ctypes.c_void_p))

    def test_addref_release_roundtrip(self):
        uia = probe._create_uia_automation()
        self.assertIsNotNone(uia, "CoCreateInstance(CUIAutomation) 取不到接口")
        try:
            addref = probe._vtbl_func(uia, 1, ctypes.c_ulong, ctypes.c_void_p)
            self.assertIsNotNone(addref, "AddRef 槽位绑定失败 = _vtbl_func 又坏了")
            self.assertGreater(addref(uia), 0)
            probe._com_release(uia)      # 配平 AddRef
        finally:
            probe._com_release(uia)      # 配平 CoCreateInstance 的那一次

    def test_control_type_condition_binds_and_returns_object(self):
        """CreatePropertyCondition 槽 23：真机返回条件对象（非 None）。

        50000 = ControlType_Button（不依赖模块常量，测试自己钉住这个 id）。
        """
        uia = probe._create_uia_automation()
        self.assertIsNotNone(uia)
        try:
            cond = probe._uia_create_control_type_condition(uia, 50000)
            self.assertIsNotNone(
                cond, "条件构造失败 = 槽位或 VARIANT 布局又被改坏了")
            probe._com_release(cond)
        finally:
            probe._com_release(uia)


if __name__ == "__main__":
    unittest.main()
