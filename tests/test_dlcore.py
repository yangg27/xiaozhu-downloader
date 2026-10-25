#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dlcore 核心函数单元测试

优先运行方式：
    python -m pytest tests/test_dlcore.py -v

当环境中未安装 pytest 时，可退回到标准库 unittest：
    python -m unittest tests.test_dlcore -v
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlcore


class FakeTask:
    """用于测试 Manager 调度算法的轻量任务桩。"""
    def __init__(self, added_at=0.0, priority=False, requested_conns=24):
        self.added_at = added_at
        self._slot_since = 0.0
        self._priority = priority
        self.requested_conns = requested_conns
        self.tid = f"t{added_at}"
        self.state = dlcore.STATE_DOWNLOADING
        self.target = requested_conns
        self._stop = False

    def set_target_conns(self, n: int):
        self.target = n


class TestFairTargetConns(unittest.TestCase):
    def _make_manager(self, max_slots=3, connections=24):
        m = dlcore.Manager(data_dir=tempfile.mkdtemp())
        m.cfg["max_parallel_tasks"] = max_slots
        m.cfg["connections"] = connections
        return m

    def test_single_task_full_speed(self):
        m = self._make_manager()
        t = FakeTask(added_at=1.0)
        m.tasks = {t.tid: t}
        self.assertEqual(m.fair_target_conns(t), 24)

    def test_two_tasks_80_percent_chain(self):
        m = self._make_manager()
        t1 = FakeTask(added_at=1.0)
        t2 = FakeTask(added_at=2.0)
        m.tasks = {t1.tid: t1, t2.tid: t2}
        # t1 排第 0，满速；t2 排第 1，24 * 0.8 = 19（向下取整）
        self.assertEqual(m.fair_target_conns(t1), 24)
        self.assertEqual(m.fair_target_conns(t2), 19)

    def test_three_tasks_chain(self):
        m = self._make_manager()
        t1 = FakeTask(added_at=1.0)
        t2 = FakeTask(added_at=2.0)
        t3 = FakeTask(added_at=3.0)
        m.tasks = {t1.tid: t1, t2.tid: t2, t3.tid: t3}
        self.assertEqual(m.fair_target_conns(t1), 24)
        self.assertEqual(m.fair_target_conns(t2), 19)
        self.assertEqual(m.fair_target_conns(t3), 15)  # 19 * 0.8 = 15

    def test_fourth_task_queued(self):
        m = self._make_manager()
        tasks = [FakeTask(added_at=i) for i in range(1, 5)]
        m.tasks = {t.tid: t for t in tasks}
        self.assertEqual(m.fair_target_conns(tasks[3]), 0)

    def test_rebalance_after_first_done(self):
        m = self._make_manager()
        t1 = FakeTask(added_at=1.0)
        t2 = FakeTask(added_at=2.0)
        t3 = FakeTask(added_at=3.0)
        t4 = FakeTask(added_at=4.0)
        m.tasks = {t1.tid: t1, t2.tid: t2, t3.tid: t3, t4.tid: t4}
        # 模拟 t1 完成退出
        t1.state = dlcore.STATE_DONE
        self.assertEqual(m.fair_target_conns(t2), 24)
        self.assertEqual(m.fair_target_conns(t3), 19)
        self.assertEqual(m.fair_target_conns(t4), 15)

    def test_min_4_threads(self):
        m = self._make_manager(max_slots=3, connections=4)
        t1 = FakeTask(added_at=1.0)
        t2 = FakeTask(added_at=2.0)
        t3 = FakeTask(added_at=3.0)
        m.tasks = {t1.tid: t1, t2.tid: t2, t3.tid: t3}
        self.assertEqual(m.fair_target_conns(t1), 4)
        self.assertEqual(m.fair_target_conns(t2), 4)
        self.assertEqual(m.fair_target_conns(t3), 4)

    def test_priority_bonus(self):
        m = self._make_manager()
        t1 = FakeTask(added_at=1.0)
        t2 = FakeTask(added_at=2.0, priority=True)
        m.tasks = {t1.tid: t1, t2.tid: t2}
        self.assertEqual(m.fair_target_conns(t2), 19 + 2)


class TestHumanSize(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(dlcore.human_size(0), "0 B")
        self.assertEqual(dlcore.human_size(512), "512 B")
        self.assertEqual(dlcore.human_size(1023), "1023 B")

    def test_kb(self):
        self.assertEqual(dlcore.human_size(1024), "1.0 KB")
        self.assertEqual(dlcore.human_size(1536), "1.5 KB")

    def test_mb_gb(self):
        self.assertEqual(dlcore.human_size(1024 ** 2), "1.0 MB")
        self.assertEqual(dlcore.human_size(1024 ** 3 * 2), "2.0 GB")

    def test_none(self):
        self.assertEqual(dlcore.human_size(None), "未知")


class TestSafeFilename(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(dlcore.safe_filename("file.txt"), "file.txt")

    def test_illegal_chars(self):
        self.assertEqual(dlcore.safe_filename("a/b:c*d?e<f>g|h"), "a_b_c_d_e_f_g_h")

    def test_url_decode(self):
        self.assertEqual(dlcore.safe_filename("hello%20world.zip"), "hello world.zip")

    def test_trim(self):
        self.assertEqual(dlcore.safe_filename("  name.txt  "), "name.txt")
        self.assertEqual(dlcore.safe_filename(".hidden."), "hidden")

    def test_empty(self):
        self.assertEqual(dlcore.safe_filename(""), "download")
        self.assertEqual(dlcore.safe_filename(None), "download")

    def test_fallback(self):
        self.assertEqual(dlcore.safe_filename("", fallback="xz"), "xz")


class TestRateLimiter(unittest.TestCase):
    def test_unlimited(self):
        lim = dlcore.RateLimiter(0)
        t0 = time.time()
        lim.consume(1024)
        self.assertLess(time.time() - t0, 0.01)

    def test_limited(self):
        # 10 KB/s，初始 burst 较大，先耗尽再测限速
        lim = dlcore.RateLimiter(10 * 1024)
        while lim._tokens >= 1024:
            lim.consume(1024)
        t0 = time.time()
        lim.consume(1024)
        self.assertGreaterEqual(time.time() - t0, 0.08)

    def test_set_rate(self):
        lim = dlcore.RateLimiter(100 * 1024)
        lim.set_rate(0)
        t0 = time.time()
        lim.consume(1024 * 1024)
        self.assertLess(time.time() - t0, 0.01)

    def test_min_burst_no_deadlock(self):
        # 极小限速不应死循环（MIN_BURST 兜底）
        lim = dlcore.RateLimiter(64)
        t0 = time.time()
        lim.consume(1)
        self.assertLess(time.time() - t0, 0.5)

    def test_thread_safety(self):
        lim = dlcore.RateLimiter(1 * 1024 * 1024)
        errors = []

        def worker():
            try:
                for _ in range(20):
                    lim.consume(1024)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)


class TestBitmap(unittest.TestCase):
    def _make_task(self, tmp_dir: str, nblocks: int = 8):
        class FakeTask:
            def __init__(self, d, n):
                self.tmp_dir = d
                self.block_size = dlcore.BLOCK_SIZE
                self.nblocks = n
                self.size = n * self.block_size
                self.bitmap = bytearray(n)
                self.done_bytes = 0
                self._block_lock = threading.Lock()
                self._done_evt = threading.Event()

            @property
            def bitmap_path(self):
                return os.path.join(self.tmp_dir, "bitmap.bin")

            @property
            def meta_path(self):
                return os.path.join(self.tmp_dir, "meta.json")

            def _block_len(self, i: int) -> int:
                start = i * self.block_size
                return min(self.block_size, self.size - start)

        return FakeTask(tmp_dir, nblocks)

    def test_mark_and_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._make_task(tmp, 4)
            for i in (0, 2):
                if not t.bitmap[i]:
                    t.bitmap[i] = 1
                    t.done_bytes += t._block_len(i)
            with open(t.bitmap_path, "wb") as f:
                f.write(bytes(t.bitmap))
            self.assertEqual(os.path.getsize(t.bitmap_path), 4)
            with open(t.bitmap_path, "rb") as f:
                blob = f.read()
            self.assertEqual(blob, bytes([1, 0, 1, 0]))

    def test_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._make_task(tmp, 4)
            t.bitmap[1] = 1
            with open(t.bitmap_path, "wb") as f:
                f.write(bytes(t.bitmap))
            with open(t.bitmap_path, "rb") as f:
                loaded = bytearray(f.read())
            self.assertEqual(loaded[1], 1)
            self.assertEqual(loaded[0], 0)

    def test_meta_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._make_task(tmp, 4)
            meta = {
                "url": "http://example.com/f.zip", "filename": "f.zip",
                "size": t.size, "nblocks": t.nblocks,
                "block_size": t.block_size, "single_mode": False,
                "range_ok": True, "done_bytes": 0,
                "referer": "", "host": "example.com",
            }
            with open(t.meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f)
            with open(t.meta_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded["nblocks"], 4)
            self.assertEqual(loaded["size"], t.size)


if __name__ == "__main__":
    unittest.main()
