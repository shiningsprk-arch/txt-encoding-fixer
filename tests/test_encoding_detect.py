# -*- coding: utf-8 -*-
"""encoding_detect 核心单元测试（standalone，不依赖 MyBooks）。

运行：python -m unittest discover -s tests 或 python tests/test_encoding_detect.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "webserver", "toolbox"))

from encoding_detect import (  # noqa: E402
    detect_encoding,
    decode_with_report,
    fix_to_utf8,
)

BIG5_TEXT = "第一章\u3000序章\n這是一本關於人工智慧發展的書籍，內容涵蓋機器學習與深度學習。"
GBK_TEXT = "第一章\u3000序章\n人工智能的发展历程，包括机器学习与深度学习。"


class TestDetectBasic(unittest.TestCase):
    """常规编码检测。"""

    def test_utf8(self):
        data = GBK_TEXT.encode("utf-8")
        r = detect_encoding(data)
        self.assertEqual(r["encoding"], "utf-8")
        self.assertGreaterEqual(r["confidence"], 0.9)
        self.assertFalse(r["mojibake"])
        self.assertFalse(r["garbage"])

    def test_gb18030(self):
        data = GBK_TEXT.encode("gb18030")
        r = detect_encoding(data)
        self.assertEqual(r["encoding"], "gb18030")
        self.assertFalse(r["mojibake"])

    def test_big5(self):
        data = BIG5_TEXT.encode("big5")
        r = detect_encoding(data)
        self.assertEqual(r["encoding"], "big5")
        self.assertFalse(r["mojibake"])

    def test_utf8_bom(self):
        data = b"\xef\xbb\xbf" + GBK_TEXT.encode("utf-8")
        r = detect_encoding(data)
        self.assertEqual(r["encoding"], "utf-8-sig")
        self.assertEqual(r["confidence"], 1.0)
        self.assertFalse(r["mojibake"])

    def test_english_text(self):
        data = "Hello world, this is a plain English book sample.\n".encode("utf-8")
        r = detect_encoding(data)
        self.assertEqual(r["encoding"], "utf-8")
        self.assertFalse(r["garbage"])

    def test_binary_garbage(self):
        data = bytes(range(256)) * 4
        r = detect_encoding(data)
        self.assertTrue(r["garbage"])

    def test_empty(self):
        r = detect_encoding(b"")
        self.assertEqual(r["encoding"], "utf-8")
        self.assertEqual(r["confidence"], 0.0)

    def test_str_input_guard(self):
        # str 输入自动按 UTF-8 编码，不应抛 TypeError
        r = detect_encoding(GBK_TEXT)
        self.assertIn("encoding", r)


class TestDecodeRoundtrip(unittest.TestCase):
    """decode_with_report / fix_to_utf8 解码一致性。"""

    def test_gb18030_roundtrip(self):
        data = GBK_TEXT.encode("gb18030")
        text, report = decode_with_report(data)
        self.assertEqual(text, GBK_TEXT)
        out, _ = fix_to_utf8(data)
        self.assertEqual(out.decode("utf-8"), GBK_TEXT)

    def test_big5_roundtrip(self):
        data = BIG5_TEXT.encode("big5")
        text, report = decode_with_report(data)
        self.assertEqual(text, BIG5_TEXT)
        out, _ = fix_to_utf8(data)
        self.assertEqual(out.decode("utf-8"), BIG5_TEXT)

    def test_bom_stripped(self):
        data = b"\xef\xbb\xbf" + GBK_TEXT.encode("utf-8")
        text, report = decode_with_report(data)
        self.assertEqual(text, GBK_TEXT)
        self.assertFalse(text.startswith("\ufeff"))


class TestMojibake(unittest.TestCase):
    """乱码反转恢复：BIG5 字节被按 GB18030 误读后以 UTF-8 存盘。"""

    def test_big5_as_gbk_saved_utf8(self):
        mojibake_str = BIG5_TEXT.encode("big5").decode("gb18030")
        data = mojibake_str.encode("utf-8")  # 乱码以 UTF-8 写入文件
        r = detect_encoding(data)
        self.assertTrue(r["mojibake"])
        self.assertEqual(r["encoding"], "big5")
        text, _ = decode_with_report(data)
        self.assertEqual(text, BIG5_TEXT)
        out, _ = fix_to_utf8(data)
        self.assertEqual(out.decode("utf-8"), BIG5_TEXT)


if __name__ == "__main__":
    unittest.main()
