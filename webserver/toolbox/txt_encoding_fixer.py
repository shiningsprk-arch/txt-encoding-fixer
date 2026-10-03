# -*- coding: utf-8 -*-
"""TXT 编码修复工具

检测 TXT 电子书的编码（BOM / 候选编码打分 / chardet 投票 / mojibake 反转链），
解码为正确的 UTF-8（无 BOM）文本，并以「生成新书」模式入库，原书零改动。

对外接口：
- :meth:`analyze` 同步检测，返回检测报告 + 修复后预览（供前端展示）；
- :meth:`fix` 后台任务，解码修复 → UTF-8 无 BOM 写出 → 新书入库。

@author: 黏菌, 2026
"""
import logging
import os
import threading
import time
import traceback
from typing import Optional

from webserver.i18n import _
from webserver.services import AsyncService
from webserver.services.background_service import BackgroundService, BackgroundTask
from webserver.toolbox.base_tool import BaseTool

from webserver.toolbox.utils import book_utils
from webserver.toolbox.utils import encoding_detect

PREVIEW_CHARS = 500  # analyze 报告中的修复预览长度
ANALYZE_LIMIT = 2 * 1024 * 1024  # analyze 检测读取上限（编码检测取前缀即可，防大文件阻塞请求线程）


class TxtEncodingFixerTool(BaseTool):
    """对指定书籍的 TXT 格式执行编码检测与修复。"""

    service_item_name = "TXT编码修复"

    _fix_lock = threading.Lock()
    _last_task_id: Optional[int] = None

    @classmethod
    def is_running(cls) -> bool:
        task = cls.get_last_task()
        return bool(task and task.get("status") == BackgroundTask.STATUS_RUNNING)

    @classmethod
    def get_last_task(cls) -> Optional[dict]:
        if cls._last_task_id is None:
            return None
        return BackgroundService().get_task(cls._last_task_id)

    @staticmethod
    def info() -> dict:
        return {
            "tool_id": "txt_encoding_fixer",
            "name": "TXT编码修复",
            "description": "检测 TXT 电子书编码（含乱码反转恢复），修复为 UTF-8 并另存为新书",
            "revision": "0.1.0",
            "author": "黏菌",
            "publish_date": "2026-08-09",
        }

    @AsyncService.register_function
    def analyze(self, book_id: int) -> dict:
        """同步检测书籍 TXT 文件的编码，返回报告 + 修复后预览。

        :param book_id: Calibre 书籍 ID。
        :return dict: ``encoding`` / ``confidence`` / ``mojibake`` / ``garbage`` /
            ``sample``（原始可读性样本）/ ``preview``（修复后预览）/
            ``reasons``（检测依据列表）。
        :raises RuntimeError: 书籍不存在 / 无 TXT 格式 / 文件缺失。
        """
        txt_path = book_utils.get_book_file(self, book_id, "TXT")
        with open(txt_path, "rb") as f:
            data = f.read(ANALYZE_LIMIT)

        text, report = encoding_detect.decode_with_report(data)
        report["preview"] = text[:PREVIEW_CHARS]
        report["book_id"] = book_id
        return report

    @AsyncService.register_service
    def fix(self, book_id: int, user_id: int) -> None:
        """后台执行编码修复：解码 → UTF-8 无 BOM 写出 → 新书入库。

        :param book_id: Calibre 书籍 ID。
        :param user_id: 操作用户 ID（记录日志 / 创建 Item 记录）。
        """
        if not TxtEncodingFixerTool._fix_lock.acquire(blocking=False):
            logging.warning(
                "[TxtEncodingFixerTool] Already running, skipping fix for book_id=%d [uid:%d]",
                book_id, user_id,
            )
            return

        # create_task 等全部放入 try：若中途抛异常，finally 仍会释放锁，
        # 避免锁永久泄漏导致工具不可用（需重启服务才能恢复）
        task_id = None
        error_message = None
        book_title = "Unknown"

        try:
            task_id = self.create_task(progress_data={"status": "starting", "book_id": book_id})
            TxtEncodingFixerTool._last_task_id = task_id
            progress_callback = self.make_progress_callback(task_id)

            books = self.api.calibre.get_data_as_dict([book_id])
            if not books:
                error_message = _("书籍不存在：ID=%d") % book_id
                self.update_task_progress(task_id, 0, {"status": "failed", "stage": "failed"})
                logging.error("[TxtEncodingFixerTool] Book not found: ID=%d [uid:%d]", book_id, user_id)
                return

            book = books[0]
            book_title = book.get("title", "Unknown")
            fmts = [f.upper() for f in (book.get("available_formats") or [])]
            if "TXT" not in fmts:
                error_message = _("该书籍没有 TXT 格式，无法执行修复")
                self.update_task_progress(task_id, 0, {"status": "failed", "stage": "failed"})
                logging.error("[TxtEncodingFixerTool] No TXT format for book_id=%d [uid:%d]", book_id, user_id)
                return

            txt_path = self.api.calibre.format_abspath(book_id, "TXT")
            if not txt_path or not os.path.exists(txt_path):
                error_message = _("找不到 TXT 文件，可能已被移除")
                self.update_task_progress(task_id, 0, {"status": "failed", "stage": "failed"})
                logging.error("[TxtEncodingFixerTool] TXT file missing for book_id=%d [uid:%d]", book_id, user_id)
                return

            self.update_task_progress(task_id, 10, {"status": "running", "stage": "reading"})
            progress_callback(10)

            with open(txt_path, "rb") as f:
                data = f.read()

            self.update_task_progress(task_id, 40, {"status": "running", "stage": "detecting"})
            progress_callback(40)

            text, report = encoding_detect.decode_with_report(data)
            if report.get("irreversible"):
                error_message = _("乱码链路不可逆（字节级信息已毁），无法自动修复")
                self.update_task_progress(task_id, 0, {"status": "failed", "stage": "failed"})
                logging.error("[TxtEncodingFixerTool] Irreversible encoding chain for book_id=%d", book_id)
                return
            if report["unrecoverable"]:
                error_message = _("文件疑似多重误读乱码（反转循环），无法自动修复")
                self.update_task_progress(task_id, 0, {"status": "failed", "stage": "failed"})
                logging.error("[TxtEncodingFixerTool] Unrecoverable mojibake cycle for book_id=%d", book_id)
                return
            if report["garbage"] and not report["mojibake"]:
                error_message = _("文件疑似二进制或混用编码，无法安全修复（编码：%s）") % report["encoding"]
                self.update_task_progress(task_id, 0, {"status": "failed", "stage": "failed"})
                logging.error("[TxtEncodingFixerTool] Garbage content for book_id=%d: %s", book_id, report["encoding"])
                return

            self.update_task_progress(task_id, 70, {"status": "running", "stage": "saving"})
            progress_callback(70)

            work_dir = self.get_work_dir(str(book_id))
            out_path = os.path.join(work_dir, "fixed_%d.txt" % int(time.time()))
            with open(out_path, "wb") as f:
                f.write(text.encode("utf-8"))  # UTF-8 无 BOM

            new_book_id = book_utils.import_as_new_book(
                self, book_id, out_path, _("（编码修复版）"), user_id,
            )
            logging.info(
                "[TxtEncodingFixerTool] Fixed book_id=%d (%s) -> new book_id=%d [uid:%d]",
                book_id, report["encoding"], new_book_id, user_id,
            )
            self.cleanup_work_dir(work_dir)

            self.add_msg(
                user_id, "success",
                _(u"书籍 [%s] TXT 编码修复成功！已生成新书（编码：%s）") % (book_title, report["encoding"]),
            )

        except Exception as err:
            error_message = str(err)
            self.add_msg(user_id, "danger", _(u"书籍 [%s] TXT 编码修复失败！") % book_title)
            logging.error("[TxtEncodingFixerTool] Unexpected error for book_id=%d: %s", book_id, err)
            logging.error(traceback.format_exc())
        finally:
            # create_task 失败时 task_id 为 None，跳过任务收尾（锁仍必须释放）
            if task_id is not None:
                self.complete_task(task_id, error_message=error_message)
                if error_message is None:
                    self.update_task_progress(task_id, 100, {"status": "completed", "book_id": book_id})
            TxtEncodingFixerTool._fix_lock.release()
