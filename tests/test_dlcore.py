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

    def pause(self):
        self.state = dlcore.STATE_PAUSED
        if getattr(self, "_evicted", False):
            self._evicted = False


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

    def test_priority_full_speed(self):
        m = self._make_manager()
        t1 = FakeTask(added_at=1.0)
        t2 = FakeTask(added_at=2.0, priority=True)
        m.tasks = {t1.tid: t1, t2.tid: t2}
        # 插队任务默认满速，不受 80% 链式递减限制
        self.assertEqual(m.fair_target_conns(t2), 24)

    def test_evict_youngest(self):
        m = self._make_manager(max_slots=2)
        t1 = FakeTask(added_at=1.0)
        t2 = FakeTask(added_at=2.0)
        t3 = FakeTask(added_at=3.0, priority=True)
        t1.state = dlcore.STATE_DOWNLOADING
        t2.state = dlcore.STATE_DOWNLOADING
        t1._slot_since = 100.0
        t2._slot_since = 200.0
        m.tasks = {t1.tid: t1, t2.tid: t2, t3.tid: t3}
        m._slot_holders = {t1.tid, t2.tid}
        m._evict_youngest_for_priority(t3)
        # 最新唤醒的 t2 应该被暂停并移出槽位
        self.assertEqual(t2.state, dlcore.STATE_PAUSED)
        self.assertNotIn(t2.tid, m._slot_holders)
        self.assertIn(t1.tid, m._slot_holders)


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
    def test_low_rate_elapsed_not_prepaid(self):
        """v3.1.10: 低速限速不能因预支令牌而瞬间跑完整个数据块。"""
        lim = dlcore.RateLimiter(256 * 1024)
        t0 = time.monotonic()
        lim.consume(256 * 1024)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.75)
        self.assertLess(elapsed, 3.0)

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


class TestV318Regression(unittest.TestCase):
    """v3.1.8 全功能实测中定位到的缺陷，逐条固化为回归用例。

    每一条都对应一次真实故障（详见 TEST_REPORT_v3.1.8.md），
    目的是防止后续改动无意间把它们改回去。
    """

    def test_rate_limiter_no_deadlock(self):
        """限速小于单次请求量时不得死锁（实测曾卡死 120 秒）。"""
        rl = dlcore.RateLimiter(256 * 1024)      # 256KB/s
        t0 = time.time()
        rl.consume(1024 * 1024)                  # 单次要 1MB > burst
        el = time.time() - t0
        self.assertLess(el, 10.0, "限速器疑似死锁：%.1fs" % el)

    def test_human_size_extended_units(self):
        """超过 TB 应进位到 PB/EB，不能显示 4194304.0 TB。"""
        self.assertIn("PB", dlcore.human_size(5 * 1024 ** 5))
        self.assertIn("EB", dlcore.human_size(4 * 1024 ** 6))
        self.assertNotIn("TB", dlcore.human_size(1 << 62))

    def test_mime_torrent(self):
        """application/x-bittorrent 必须能推出 .torrent（曾返回空串）。"""
        self.assertEqual(dlcore.guess_ext_from_mime("application/x-bittorrent"),
                         ".torrent")
        self.assertEqual(dlcore.guess_ext_from_mime("video/mp4"), ".mp4")

    def test_m4s_is_media(self):
        """.m4s（B站/YouTube DASH 分片）必须被识别为媒体（曾漏在白名单外）。"""
        self.assertIn(".m4s", dlcore.MEDIA_EXTS)
        html = ('<html><body><script>var v = '
                '"https://x.com/up/123_u1-1-100001.m4s";</script></body></html>')
        srv = None
        try:
            from tests.range_server import RangeServer
            srv = RangeServer({"/p.html": (html.encode(),
                                           {"Content-Type": "text/html"})}).start()
            r = dlcore.scan_webpage(srv.url("/p.html"))
            urls = " ".join(it["url"] for it in r["items"])
            self.assertIn("100001.m4s", urls, "DASH 分片未被嗅探到")
        finally:
            if srv is not None:
                srv.stop()

    def test_tracker_parser_rejects_html(self):
        """tracker 解析必须丢弃 HTML 页面内容（曾把整页源码当 tracker 存缓存）。"""
        import bt_trackers
        text = (
            "\ufeff<!DOCTYPE html>\n<html>\n<head>\n"
            '<meta charset="UTF-8" />\n<script>var a=1;</script>\n'
            "udp://tracker.opentrackr.org:1337/announce\n"
            "https://tracker.torrent.eu.org:443/announce\n"
            "udp://tracker.opentrackr.org:1337/announce\n"      # 重复
            "# 这是注释\n"
            "  \n"
        )
        got = bt_trackers.parse_trackers(text)
        self.assertEqual(len(got), 2, "HTML 未过滤或去重失败: %r" % got)
        for x in got:
            self.assertTrue(
                x.startswith(("udp://", "http://", "https://", "ws://", "wss://")),
                "出现非法 tracker: %r" % x)

    def test_find_tmp_dir_rejects_traversal(self):
        """续传目录定位必须拒绝路径穿越（安全基线）。"""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(dlcore.find_tmp_dir(tmp, "../evil"))
            self.assertIsNone(dlcore.find_tmp_dir(tmp, "a/b"))
            self.assertIsNone(dlcore.find_tmp_dir(tmp, ""))


class TestCompletionConsistency(unittest.TestCase):
    """v3.1.10：完成可见性、最终产物及后处理的回归测试。"""

    def _manager(self, root):
        data = os.path.join(root, "data")
        os.makedirs(data)
        with open(os.path.join(data, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"download_dir": os.path.join(root, "out"),
                       "bt_trackers_autoupdate": False}, f)
        return dlcore.Manager(data_dir=data)

    def _task(self, mgr, root, cls=dlcore.DownloadTask):
        return cls(mgr, "http://127.0.0.1/item.bin",
                   os.path.join(root, "out"), "result.bin")

    def test_history_write_failure_never_publicly_done(self):
        with tempfile.TemporaryDirectory() as root:
            mgr = self._manager(root)
            task = self._task(mgr, root)
            path = os.path.join(root, "out", "result.bin")
            with open(path, "wb") as f:
                f.write(b"payload")
            def fail(_task):
                raise OSError("simulated history disk failure")
            mgr.commit_history = fail
            with self.assertRaises(OSError):
                task.mark_done(path)
            self.assertNotEqual(task.state, dlcore.STATE_DONE)
            self.assertEqual(mgr.history(), [])

    def test_bt_directory_result_uses_actual_file_inventory(self):
        with tempfile.TemporaryDirectory() as root:
            mgr = self._manager(root)
            task = self._task(mgr, root, dlcore.BTDownload)
            bundle = os.path.join(root, "out", "bundle")
            os.makedirs(os.path.join(bundle, "nested"))
            files = [os.path.join(bundle, "a.bin"),
                     os.path.join(bundle, "nested", "b.bin")]
            for i, path in enumerate(files, 1):
                with open(path, "wb") as f:
                    f.write(b"x" * i)
            task.extra["bt_files"] = files
            task.mark_done(bundle)
            self.assertEqual(task.state, dlcore.STATE_DONE)
            self.assertEqual(task.size, 3)
            self.assertTrue(os.path.isdir(task.created_final))
            self.assertEqual(mgr.history()[0]["final"], os.path.abspath(bundle))

    def test_internal_audio_is_not_public_history(self):
        with tempfile.TemporaryDirectory() as root:
            mgr = self._manager(root)
            task = self._task(mgr, root)
            task.extra["internal"] = True
            path = os.path.join(root, "out", "result.bin")
            with open(path, "wb") as f:
                f.write(b"audio")
            task.mark_done(path)
            self.assertEqual(task.state, dlcore.STATE_DONE)
            self.assertEqual(mgr.history(), [])

    def _make_av_fixture(self, path):
        """生成含真实视频流和音频流的短媒体，避免无音轨夹具造成假失败。"""
        import subprocess
        ffmpeg = os.path.join(dlcore.app_root(), "third_party", "ffmpeg.exe")
        proc = subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i",
             "testsrc=duration=0.4:size=64x64:rate=5", "-f", "lavfi", "-i",
             "sine=frequency=880:duration=0.4", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(proc.returncode, 0, proc.stderr[-1000:])
        probe = subprocess.run(
            [ffmpeg, "-i", path, "-map", "0:a:0", "-frames:a", "1",
             "-f", "null", "-"], capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(probe.returncode, 0,
                         "测试媒体必须包含可解码音频流: " + probe.stderr.decode("utf-8", "replace")[-1000:])

    def _make_video_only_fixture(self, path):
        """生成**只有视频流、没有音轨**的短媒体（DASH 视频轨的真实形态）。"""
        import subprocess
        ffmpeg = os.path.join(dlcore.app_root(), "third_party", "ffmpeg.exe")
        proc = subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i",
             "testsrc=duration=1.2:size=160x90:rate=10", "-an",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", path],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(proc.returncode, 0, proc.stderr[-1000:])
        probe = subprocess.run(
            [ffmpeg, "-i", path, "-map", "0:a:0", "-frames:a", "1",
             "-f", "null", "-"], capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertNotEqual(probe.returncode, 0, "该夹具必须确实不含音轨")

    def test_audio_only_source_named_mp3_still_extracts(self):
        """★ v3.1.10 回归（探针 probe-audio-only-path.py：A_DST_SRC_COLLIDE）。

        源文件本身名为 `.mp3` 时（旧版 GUI「仅音频」会预改名成 .mp3），
        `dst = splitext(src)[0] + ".mp3"` 与 src **同一路径** → FFmpeg 拒绝
        原地编辑（"Output ... same as Input #0 - exiting"）→ 凡「视频 → 仅音频」
        必然失败。修复后必须成功产出真实 MP3，且不留坏文件。
        """
        import subprocess
        ffmpeg = os.path.join(dlcore.app_root(), "third_party", "ffmpeg.exe")
        if not os.path.isfile(ffmpeg):
            self.skipTest("未提供 FFmpeg")
        with tempfile.TemporaryDirectory() as root:
            mgr = self._manager(root)
            tmp = os.path.join(root, "_fixture.mp4")
            self._make_av_fixture(tmp)
            src = os.path.join(root, "video.mp3")   # 同名陷阱现场
            os.replace(tmp, src)
            task = self._task(mgr, root)
            task.extra["audio_only"] = True
            task.created_final = src
            task._complete(src)
            self.assertEqual(task.state, dlcore.STATE_DONE)
            self.assertTrue(task.created_final.endswith(".mp3"))
            self.assertTrue(os.path.isfile(task.created_final))
            # 产物必须是真实可解码的音频（而不是改了后缀的 mp4）
            probe = subprocess.run(
                [ffmpeg, "-i", task.created_final, "-map", "0:a:0",
                 "-frames:a", "1", "-f", "null", "-"],
                capture_output=True, timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(probe.returncode, 0,
                             "产物必须可解码音频: " + probe.stderr.decode("utf-8", "replace")[-800:])
            # 临时中转文件不能残留
            self.assertFalse(os.path.exists(src + ".xzd-avsrc"))

    def test_audio_only_without_track_keeps_downloaded_file(self):
        """★ v3.1.10：源确实无音轨 → 如实报错，**下载成果必须原样保留**。

        round12 之前的行为是报错同时留下"后缀 .mp3、内容实为 mp4"的坏文件；
        修复后既要给出可读的中文原因，也不能让下载成果改名或损坏。
        """
        ffmpeg = os.path.join(dlcore.app_root(), "third_party", "ffmpeg.exe")
        if not os.path.isfile(ffmpeg):
            self.skipTest("未提供 FFmpeg")
        with tempfile.TemporaryDirectory() as root:
            mgr = self._manager(root)
            src = os.path.join(root, "video.mp4")
            self._make_video_only_fixture(src)
            before = os.path.getsize(src)
            task = self._task(mgr, root)
            task.extra["audio_only"] = True
            task.created_final = src
            with self.assertRaises(RuntimeError) as ctx:
                task._complete(src)
            self.assertNotEqual(task.state, dlcore.STATE_DONE)
            self.assertIn("不含音轨", str(ctx.exception))
            # 下载成果必须原样保留（大小不变、路径不变）
            self.assertTrue(os.path.isfile(src))
            self.assertEqual(os.path.getsize(src), before)

    def test_audio_only_history_points_to_existing_mp3(self):
        ffmpeg = os.path.join(dlcore.app_root(), "third_party", "ffmpeg.exe")
        if not os.path.isfile(ffmpeg):
            self.skipTest("未提供 FFmpeg")
        with tempfile.TemporaryDirectory() as root:
            mgr = self._manager(root)
            src = os.path.join(root, "video.mp4")
            self._make_av_fixture(src)
            task = self._task(mgr, root)
            task.extra["audio_only"] = True
            task.created_final = src
            task._complete(src)
            hist = mgr.history()
            self.assertEqual(task.state, dlcore.STATE_DONE)
            self.assertTrue(task.created_final.endswith(".mp3"))
            self.assertTrue(os.path.isfile(task.created_final))
            self.assertFalse(os.path.exists(src))
            self.assertEqual(len(hist), 1)
            self.assertEqual(hist[0]["final"], task.created_final)
            self.assertEqual(hist[0]["size"], os.path.getsize(task.created_final))

    def test_history_write_failure_preserves_downloaded_file(self):
        with tempfile.TemporaryDirectory() as root:
            mgr = self._manager(root)
            path = os.path.join(root, "out", "result.mp4")
            self._make_av_fixture(path)
            task = self._task(mgr, root)
            task.extra["audio_only"] = True
            task.created_final = path
            mgr.commit_history = lambda _task: (_ for _ in ()).throw(OSError("disk full"))
            with self.assertRaises(OSError):
                task._complete(path)
            self.assertNotEqual(task.state, dlcore.STATE_DONE)
            # 真实抽音轨已成功，故障发生在历史提交；最终 MP3 必须保留。
            self.assertTrue(task.created_final.endswith(".mp3"))
            self.assertTrue(os.path.isfile(task.created_final))
            self.assertEqual(mgr.history(), [])


if __name__ == "__main__":
    unittest.main()
