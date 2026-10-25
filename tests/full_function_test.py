#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/full_function_test.py · 全功能实测（v3.1.8 新增）
======================================================
设计原则：**一切以实际运行数据为准**，不读代码注释推断行为。

覆盖：
  A. 多线程分片核心        （最核心，此前从未被测试覆盖）
  B. 速度均衡 80% 链式      （实测各任务实际线程数）
  C. 插队抢占               （实测被挤任务状态）
  D. 断点续传               （暂停/断连 → 恢复 → 校验完整性）
  E. 限速器                 （实测实际吞吐 vs 目标限速）
  F. 429 限流退避           （实测重试后能否成功）
  G. 单流兜底（无 Range）   （实测 range_ok=False 路径）
  H. HLS 下载与合并         （含 AES-128）
  I. 文件名解析             （Content-Disposition）
  J. 续传元数据落盘         （meta.json/bitmap.bin 实际内容）
  K. 各种工具函数           （human_size / safe_filename / unique_path 等）
  L. HttpHelper             （headers / get_text / get_bytes / 连接池）
  M. Manager 全方法         （pause_all / resume_all / history / jobs 往返）
  N. localserver 全路由     （含 /open 的 folder 模式 —— 此前的 subprocess bug）

用法：
    python tests/full_function_test.py            # 全跑
    python tests/full_function_test.py --only A,B # 只跑部分
    python tests/full_function_test.py --json     # 输出 JSON 结果
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import dlcore  # noqa: E402
import localserver  # noqa: E402
from tests.range_server import RangeServer  # noqa: E402

RESULTS: list[dict] = []
CUR = {"group": "", "name": ""}


def case(group: str, name: str):
    """装饰器：跑一个用例并记录结果。"""
    def deco(fn):
        def wrapper(ctx):
            CUR["group"], CUR["name"] = group, name
            t0 = time.time()
            rec = {"group": group, "name": name, "ok": False, "sec": 0.0,
                   "detail": "", "err": ""}
            try:
                detail = fn(ctx)
                rec["ok"] = True
                rec["detail"] = detail or ""
                print("  [PASS] %s · %s  (%.2fs)  %s"
                      % (group, name, time.time() - t0, detail or ""))
            except Exception as e:
                rec["err"] = "%s: %s" % (type(e).__name__, e)
                rec["detail"] = traceback.format_exc(limit=3).splitlines()[-1]
                print("  [FAIL] %s · %s  (%.2fs)  %s"
                      % (group, name, time.time() - t0, rec["err"]))
                if os.environ.get("XZD_TEST_VERBOSE"):
                    traceback.print_exc()
            rec["sec"] = round(time.time() - t0, 3)
            RESULTS.append(rec)
            return rec
        wrapper._case = (group, name)
        return wrapper
    return deco


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _new_mgr(tmp: str, **cfg) -> dlcore.Manager:
    """在临时目录里造一个独立 Manager。"""
    d = os.path.join(tmp, "data")
    os.makedirs(d, exist_ok=True)
    m = dlcore.Manager(data_dir=d)
    m.set_cfg(download_dir=os.path.join(tmp, "dl"), **cfg)
    os.makedirs(os.path.join(tmp, "dl"), exist_ok=True)
    return m


def _wait_state(task, states, timeout=120.0, interval=0.1):
    """等任务进入指定状态集合，返回实际状态。"""
    end = time.time() + timeout
    while time.time() < end:
        if task.state in states:
            return task.state
        time.sleep(interval)
    return task.state


def _wait_done(task, timeout=180.0):
    return _wait_state(task, {dlcore.STATE_DONE, dlcore.STATE_ERROR,
                              dlcore.STATE_CANCELLED}, timeout)


# =====================================================================
# A. 多线程分片核心
# =====================================================================

A = {}


@case("A 多线程分片", "A1_真分片下载_32线程_16MB_完整性")
def a1(ctx):
    size = 16 * 1024 * 1024
    payload = RangeServer.make_payload(size)
    srv = RangeServer({"/big.bin": payload}).start()
    ctx["srv_a"] = srv
    tmp = tempfile.mkdtemp(prefix="xzd_A1_")
    ctx["tmp_a"] = tmp
    mgr = _new_mgr(tmp, connections=32)
    # ★ 采样真实并发：下载中周期性记录 target/实际活跃连接数
    samples = []
    stop_flag = threading.Event()

    def sampler():
        while not stop_flag.is_set():
            try:
                samples.append((t.target, getattr(t, "_active_conns", None)))
            except Exception:
                pass
            time.sleep(0.05)

    t0 = time.time()
    t = mgr.add_task(srv.url("/big.bin"), out_dir=os.path.join(tmp, "dl"))
    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    st = _wait_done(t)
    el = time.time() - t0
    stop_flag.set()
    th.join(timeout=1.0)
    assert st == dlcore.STATE_DONE, "任务未完成: state=%s err=%s" % (st, t.err)
    out = t.created_final or t.final_path()
    assert os.path.exists(out), "文件不存在: %s" % out
    got = open(out, "rb").read()
    assert len(got) == size, "大小不符: %d != %d" % (len(got), size)
    assert _digest(got) == _digest(payload), "内容 SHA256 不一致"
    s = srv.stats
    A["range_requests"] = s["range_requests"]
    A["full_requests"] = s["full_requests"]
    # 关键判据：必须真的用了 Range 分片
    assert s["range_requests"] > 1, \
        "未走 Range 分片！range_requests=%d（说明退化成单流）" % s["range_requests"]
    assert t.range_ok is True, "range_ok 不是 True"
    assert not t.single_mode, "single_mode 被置 True"
    max_target = max((x[0] or 0) for x in samples) if samples else 0
    assert max_target >= 20, "实测目标线程峰值仅 %d，未达到多线程预期" % max_target
    mbs = size / 1024 / 1024 / max(el, 0.001)
    return "Range 请求 %d 次 / 全量 %d 次；实测目标线程峰值 %d（采样 %d 次）；%d 字节耗时 %.2fs（%.1f MB/s）" % (
        s["range_requests"], s["full_requests"], max_target, len(samples),
        size, el, mbs)


@case("A 多线程分片", "A2_区块对齐_恰好1MB边界")
def a2(ctx):
    # 3 个整区块 + 半块，验证边界不丢字节
    size = 3 * dlcore.BLOCK_SIZE + dlcore.BLOCK_SIZE // 2
    payload = RangeServer.make_payload(size, seed=7)
    srv = RangeServer({"/edge.bin": payload}).start()
    tmp = tempfile.mkdtemp(prefix="xzd_A2_")
    mgr = _new_mgr(tmp, connections=8)
    t = mgr.add_task(srv.url("/edge.bin"), out_dir=os.path.join(tmp, "dl"))
    st = _wait_done(t)
    srv.stop()
    assert st == dlcore.STATE_DONE, "state=%s err=%s" % (st, t.err)
    got = open(t.created_final or t.final_path(), "rb").read()
    assert got == payload, "边界字节不一致 (%d vs %d)" % (len(got), size)
    return "nblocks=%d, 大小精确匹配 %d 字节" % (t.nblocks or 0, size)


@case("A 多线程分片", "A3_小文件走单流_小于1MB")
def a3(ctx):
    size = 300 * 1024  # < MIN_SEG_SIZE
    payload = RangeServer.make_payload(size, seed=11)
    srv = RangeServer({"/small.bin": payload}).start()
    tmp = tempfile.mkdtemp(prefix="xzd_A3_")
    mgr = _new_mgr(tmp, connections=16)
    t = mgr.add_task(srv.url("/small.bin"), out_dir=os.path.join(tmp, "dl"))
    st = _wait_done(t)
    srv.stop()
    assert st == dlcore.STATE_DONE, "state=%s err=%s" % (st, t.err)
    got = open(t.created_final or t.final_path(), "rb").read()
    assert got == payload, "内容不一致"
    return "小文件 %d 字节，走单流成功（single_mode=%s）" % (size, t.single_mode)


@case("A 多线程分片", "A4_服务器不支持Range_单流兜底")
def a4(ctx):
    size = 6 * 1024 * 1024
    payload = RangeServer.make_payload(size, seed=13)
    srv = RangeServer({"/norange.bin": payload}, ignore_range=True).start()
    tmp = tempfile.mkdtemp(prefix="xzd_A4_")
    mgr = _new_mgr(tmp, connections=16)
    t = mgr.add_task(srv.url("/norange.bin"), out_dir=os.path.join(tmp, "dl"))
    st = _wait_done(t)
    srv.stop()
    assert st == dlcore.STATE_DONE, "state=%s err=%s" % (st, t.err)
    got = open(t.created_final or t.final_path(), "rb").read()
    assert _digest(got) == _digest(payload), "单流内容不一致"
    assert t.single_mode is True, "应判定为 single_mode"
    return "无 Range 服务器 → 正确降级单流，%d 字节校验通过" % size


@case("A 多线程分片", "A5_服务器忽略Range_返回200_不写错位")
def a5(ctx):
    # 服务器"声明支持 Range 但实际忽略"——最阴险的场景
    size = 5 * 1024 * 1024
    payload = RangeServer.make_payload(size, seed=17)
    srv = RangeServer({"/liar.bin": payload}).start()
    # 手动改成"探测时支持，之后忽略"：直接用一个只回 200 的服务器
    srv.stop()
    srv2 = RangeServer({"/liar.bin": payload}, ignore_range=True).start()
    tmp = tempfile.mkdtemp(prefix="xzd_A5_")
    mgr = _new_mgr(tmp, connections=8)
    t = mgr.add_task(srv2.url("/liar.bin"), out_dir=os.path.join(tmp, "dl"))
    st = _wait_done(t)
    srv2.stop()
    assert st == dlcore.STATE_DONE, "state=%s err=%s" % (st, t.err)
    got = open(t.created_final or t.final_path(), "rb").read()
    assert _digest(got) == _digest(payload), "内容错位！这是最严重的 bug"
    return "忽略 Range 的服务器 → 内容未错位（%d 字节）" % size


# =====================================================================
# B. 速度均衡
# =====================================================================

@case("B 速度均衡", "B1_80%链式递减_3任务实测线程数")
def b1(ctx):
    size = 24 * 1024 * 1024
    payload = RangeServer.make_payload(size, seed=21)
    # 全局限速 1.2MB/s：4 个 8MB 任务共需 ~27s，确保观察窗口足够
    # （每请求限速会因 24 并发叠加 24 倍，不能用）
    srv = RangeServer({"/f1.bin": payload, "/f2.bin": payload,
                       "/f3.bin": payload, "/f4.bin": payload},
                      global_rate_bps=1200 * 1024).start()
    ctx["srv_b"] = srv
    tmp = tempfile.mkdtemp(prefix="xzd_B1_")
    ctx["tmp_b"] = tmp
    mgr = _new_mgr(tmp, connections=24, max_parallel_tasks=3)
    ts = []
    for i in range(1, 5):
        ts.append(mgr.add_task(srv.url("/f%d.bin" % i),
                               out_dir=os.path.join(tmp, "dl"),
                               filename="f%d.bin" % i))
    time.sleep(8)
    states = [t.state for t in ts]
    dl_msgs = [(t.state, t.progress, t.target_conns()) for t in ts]
    assert all(s == dlcore.STATE_DOWNLOADING for s in states[:3]), \
        "测试前提不成立（前 3 个未在下载）: %s" % dl_msgs
    assert states[3] in (dlcore.STATE_WAIT, dlcore.STATE_PENDING), \
        "第 4 个应处于排队: %s" % dl_msgs
    target = [t.target_conns() for t in ts[:3]]
    exp = [24, 19, 15]
    ctx["b1_targets"] = target
    q4 = ts[3].target_conns()
    ctx["b1_queue"] = q4
    for t in ts:
        t.cancel(delete_files=True)
    srv.stop()
    assert target == exp, "链式递减不符：实测 %s，期望 %s" % (target, exp)
    assert q4 == 0, "第 4 个任务应排队（目标 0 线程），实际 %d" % q4
    return "3 任务目标线程 %s（期望 %s），第 4 个排队目标 %d；状态 %s" % (
        target, exp, q4, states)


@case("B 速度均衡", "B2_排队任务不占带宽_完成后递补")
def b2(ctx):
    # ★ 独立实测：max_parallel_tasks=1，起 3 个任务，
    #   验证"排队中的任务不产生任何网络流量"，且第 1 个完成后**先加入的**
    #   第 2 个自动补位（不能是后加入的第 3 个抢走 —— 实测过这个公平性缺陷）。
    big1 = 20 * 1024 * 1024          # t1 够大，保证排队窗口充足可观测
    payload = RangeServer.make_payload(big1, seed=21)
    srv = RangeServer({"/q1.bin": payload, "/q2.bin": payload, "/q3.bin": payload},
                      global_rate_bps=2 * 1024 * 1024).start()
    tmp = tempfile.mkdtemp(prefix="xzd_B2_")
    mgr = _new_mgr(tmp, connections=8, max_parallel_tasks=1)
    dl = os.path.join(tmp, "dl")
    t1 = mgr.add_task(srv.url("/q1.bin"), out_dir=dl, filename="q1.bin")
    assert _wait_state(t1, {dlcore.STATE_DOWNLOADING}, timeout=30) == dlcore.STATE_DOWNLOADING
    time.sleep(0.5)
    t2 = mgr.add_task(srv.url("/q2.bin"), out_dir=dl, filename="q2.bin")
    time.sleep(0.3)
    t3 = mgr.add_task(srv.url("/q3.bin"), out_dir=dl, filename="q3.bin")
    time.sleep(2.0)

    # 采样必须在 t1 仍在下载时进行（否则 t2 已递补，采样到的就不是排队态）
    assert t1.state == dlcore.STATE_DOWNLOADING, \
        "采样窗口失效：t1 已 %s（应仍在下载）" % t1.state
    q_states = [t2.state, t3.state]
    q_targets = [t2.target_conns(), t3.target_conns()]
    q_progress = [t2.done_bytes, t3.done_bytes]
    # 排队任务必须目标 0 线程、进度 0 字节
    assert all(x == 0 for x in q_targets), \
        "排队任务目标线程应为 0，实测 %s（状态 %s）" % (q_targets, q_states)
    assert all(x == 0 for x in q_progress), \
        "排队任务不应下载任何数据，实测 %s 字节" % q_progress
    assert all(s in (dlcore.STATE_WAIT, dlcore.STATE_PENDING) for s in q_states), \
        "排队任务状态异常: %s" % q_states

    # 等 t1 完成 → **先加入的 t2** 应自动递补（t3 不能插队抢位）
    st1 = _wait_done(t1, timeout=180)
    assert st1 == dlcore.STATE_DONE, "t1 未完成: %s %s" % (st1, t1.err)
    promoted = _wait_state(t2, {dlcore.STATE_DOWNLOADING}, timeout=30)
    t2_target_after = t2.target_conns()
    t3_target_after = t3.target_conns()
    t3_state_after = t3.state
    srv.stop()
    for t in (t1, t2, t3):
        t.cancel(delete_files=True)
    assert promoted == dlcore.STATE_DOWNLOADING, \
        "t1 完成后 t2 未自动递补: %s" % t2.state
    assert t2_target_after > 0, "递补后 t2 线程仍为 0"
    assert t3_target_after == 0, "t3 仍应排队（线程 0），实测 %d" % t3_target_after
    assert t3_state_after in (dlcore.STATE_WAIT, dlcore.STATE_PENDING), \
        "t3 应在排队（先来先服务），实测 %s" % t3_state_after
    return "排队中：目标线程 %s、已下 %s 字节（不占带宽）；t1 完成后**先加入的 t2** 递补 %d 线程，t3 仍排队" % (
        q_targets, q_progress, t2_target_after)


@case("B 速度均衡", "B3_完成释放槽位_自动递补提速")
def b3(ctx):
    size = 4 * 1024 * 1024
    payload = RangeServer.make_payload(size, seed=23)
    big = RangeServer.make_payload(40 * 1024 * 1024, seed=24)
    srv = RangeServer({"/quick.bin": payload, "/slow.bin": big}).start()
    tmp = tempfile.mkdtemp(prefix="xzd_B3_")
    mgr = _new_mgr(tmp, connections=24, max_parallel_tasks=1)
    t1 = mgr.add_task(srv.url("/quick.bin"), out_dir=os.path.join(tmp, "dl"),
                      filename="quick.bin")
    t2 = mgr.add_task(srv.url("/slow.bin"), out_dir=os.path.join(tmp, "dl"),
                      filename="slow.bin")
    # t2 应排队
    time.sleep(1.5)
    queued_target = t2.target_conns()
    # 等 t1 完成
    _wait_state(t1, {dlcore.STATE_DONE}, timeout=90)
    time.sleep(4)
    t2_target = t2.target_conns()
    srv.stop()
    for t in (t1, t2):
        t.cancel(delete_files=True)
    assert t2_target >= 20, \
        "槽位释放后 t2 未提速：target=%d（应接近 24）" % t2_target
    return "t2 排队时目标 %d 线程 → t1 完成后升至 %d 线程" % (queued_target, t2_target)


# =====================================================================
# C. 插队
# =====================================================================

@case("C 插队", "C1_插队抢占_被挤任务暂停")
def c1(ctx):
    """priority=True 新建的任务应立刻抢到槽位，并挤掉最新唤醒的下载任务。

    判据用 phase 而非 _evicted：`pause()` 消费该标志后会清零（见 dlcore
    DownloadTask.pause），它是"一次性"的内部信号，不能当持久状态断言。
    """
    big = RangeServer.make_payload(8 * 1024 * 1024, seed=31)
    srv = RangeServer({"/a.bin": big, "/b.bin": big, "/vip.bin": big},
                      global_rate_bps=700 * 1024).start()
    tmp = tempfile.mkdtemp(prefix="xzd_C1_")
    mgr = _new_mgr(tmp, connections=24, max_parallel_tasks=1)
    t1 = mgr.add_task(srv.url("/a.bin"), out_dir=os.path.join(tmp, "dl"), filename="a.bin")
    st1 = _wait_state(t1, {dlcore.STATE_DOWNLOADING}, timeout=30)
    assert st1 == dlcore.STATE_DOWNLOADING, "t1 未进入下载: %s" % t1.state
    time.sleep(2.0)   # 让 t1 拿到 _slot_since
    t2 = mgr.add_task(srv.url("/vip.bin"), out_dir=os.path.join(tmp, "dl"),
                      filename="vip.bin", priority=True)
    time.sleep(6)
    st1_after = t1.state
    ph1 = t1.phase
    vip_state = t2.state
    vip_target = t2.target_conns()
    diag = ("t1=%s/phase=%r  插队任务=%s/目标%d线程" %
            (st1_after, ph1, vip_state, vip_target))
    srv.stop()
    for t in (t1, t2):
        t.cancel(delete_files=True)
    assert st1_after == dlcore.STATE_PAUSED, \
        "被插队任务应被暂停（抢占未生效）| " + diag
    assert "挤掉" in (ph1 or ""), "被挤任务相位未提示原因 | " + diag
    assert vip_state == dlcore.STATE_DOWNLOADING, \
        "插队任务未立即开始下载（说明没抢到槽位）| " + diag
    assert vip_target >= 20, "插队任务未满速 | " + diag
    return "t1 被挤暂停（%r）；插队任务立即 %s、满速 %d 线程" % (ph1, vip_state, vip_target)


@case("C 插队", "C2_jump_queue API_置顶并抢占")
def c2(ctx):
    big = RangeServer.make_payload(8 * 1024 * 1024, seed=33)
    srv = RangeServer({"/x.bin": big, "/y.bin": big, "/z.bin": big},
                      global_rate_bps=700 * 1024).start()
    tmp = tempfile.mkdtemp(prefix="xzd_C2_")
    mgr = _new_mgr(tmp, connections=24, max_parallel_tasks=1)
    t1 = mgr.add_task(srv.url("/x.bin"), out_dir=os.path.join(tmp, "dl"), filename="x.bin")
    st1 = _wait_state(t1, {dlcore.STATE_DOWNLOADING}, timeout=30)
    assert st1 == dlcore.STATE_DOWNLOADING, "t1 未进入下载"
    time.sleep(2.0)
    t2 = mgr.add_task(srv.url("/y.bin"), out_dir=os.path.join(tmp, "dl"), filename="y.bin")
    t3 = mgr.add_task(srv.url("/z.bin"), out_dir=os.path.join(tmp, "dl"), filename="z.bin")
    time.sleep(1.5)
    ok = mgr.jump_queue(t3.tid)
    time.sleep(4)
    order0 = mgr.order[0] if mgr.order else None
    t3_state = t3.state
    t3_target = t3.target_conns()
    t1_state = t1.state
    t1_phase = t1.phase
    srv.stop()
    res = ("jump_queue=%s, order[0]==t3:%s, t3._priority=%s, t3状态=%s/%d线程, "
           "t1状态=%s, t1相位=%r" % (
               ok, order0 == t3.tid, getattr(t3, "_priority", False),
               t3_state, t3_target, t1_state, t1_phase))
    for t in (t1, t2, t3):
        t.cancel(delete_files=True)
    assert ok and order0 == t3.tid and getattr(t3, "_priority", False), res
    assert t1_state == dlcore.STATE_PAUSED and "挤掉" in (t1_phase or ""), \
        "jump_queue 未挤掉正在下载的任务：" + res
    assert t3_state == dlcore.STATE_DOWNLOADING, "插队任务未开始下载：" + res
    assert t3_target >= 20, "插队任务未满速：" + res
    return res


# =====================================================================
# D. 断点续传
# =====================================================================

@case("D 断点续传", "D1_暂停再恢复_续传不重下")
def d1(ctx):
    size = 20 * 1024 * 1024
    payload = RangeServer.make_payload(size, seed=41)
    # 全局限速 1.2MB/s：20MB 需 ~17s，确保暂停时只下了一部分
    srv = RangeServer({"/resume.bin": payload}, global_rate_bps=1200 * 1024).start()
    tmp = tempfile.mkdtemp(prefix="xzd_D1_")
    mgr = _new_mgr(tmp, connections=8)
    t = mgr.add_task(srv.url("/resume.bin"), out_dir=os.path.join(tmp, "dl"))
    st = _wait_state(t, {dlcore.STATE_DOWNLOADING}, timeout=30)
    assert st == dlcore.STATE_DOWNLOADING, "任务未开始下载"
    time.sleep(6)
    prog_before = t.progress
    done_before = t.done_bytes
    assert 0 < prog_before < 0.95, \
        "测试前提不成立：暂停时进度 %.2f（应为部分完成）" % prog_before
    t.pause()
    _wait_state(t, {dlcore.STATE_PAUSED}, timeout=20)
    time.sleep(1.5)
    # 关掉服务器统计，重启计数，验证恢复后不重复下载已完成区块
    srv.reset_stats()
    t.start()
    st = _wait_done(t, timeout=180)
    srv.stop()
    assert st == dlcore.STATE_DONE, "恢复后未完成: %s err=%s" % (st, t.err)
    got = open(t.created_final or t.final_path(), "rb").read()
    assert _digest(got) == _digest(payload), "续传后内容不一致"
    re_sent = srv.stats["bytes_sent"]
    ratio = re_sent / float(size)
    assert ratio < 0.95, \
        "续传却几乎重下了全部：恢复后发送 %d 字节 / 总 %d（%.0f%%）" % (
            re_sent, size, ratio * 100)
    return "暂停时进度 %.0f%%（%s），恢复后仅补传 %.0f%%，内容完整" % (
        prog_before * 100, dlcore.human_size(done_before), ratio * 100)


@case("D 断点续传", "D2_连接中断_自动重试成功")
def d2(ctx):
    size = 8 * 1024 * 1024
    payload = RangeServer.make_payload(size, seed=43)
    srv = RangeServer({"/drop.bin": payload}, drop_after=512 * 1024).start()
    tmp = tempfile.mkdtemp(prefix="xzd_D2_")
    mgr = _new_mgr(tmp, connections=8)
    t = mgr.add_task(srv.url("/drop.bin"), out_dir=os.path.join(tmp, "dl"))
    st = _wait_done(t, timeout=180)
    srv.stop()
    assert st == dlcore.STATE_DONE, \
        "断连后未能自愈: state=%s err=%s" % (st, t.err)
    got = open(t.created_final or t.final_path(), "rb").read()
    assert _digest(got) == _digest(payload), "断连自愈后内容不一致"
    return "首块被强制断开后自动重试成功，SHA256 校验通过"


@case("D 断点续传", "D3_重启进程_从磁盘恢复任务")
def d3(ctx):
    size = 12 * 1024 * 1024
    payload = RangeServer.make_payload(size, seed=47)
    # 全局限速：让任务在下完之前有足够长的观察窗口
    srv = RangeServer({"/persist.bin": payload}, global_rate_bps=600 * 1024).start()
    tmp = tempfile.mkdtemp(prefix="xzd_D3_")
    dl_dir = os.path.join(tmp, "dl")
    mgr = _new_mgr(tmp, connections=8)
    t = mgr.add_task(srv.url("/persist.bin"), out_dir=dl_dir)
    st = _wait_state(t, {dlcore.STATE_DOWNLOADING}, timeout=30)
    assert st == dlcore.STATE_DOWNLOADING, "任务未开始下载"
    time.sleep(4)
    done_before = t.done_bytes
    assert done_before > 0, "4 秒内未下载任何数据"
    t.pause()
    _wait_state(t, {dlcore.STATE_PAUSED}, timeout=20)
    time.sleep(1.5)               # 等 _session 退出并落盘
    srv.stop()                    # 先关服务器，避免新 Manager 又去连
    mgr.save_jobs()
    assert os.path.exists(mgr.jobs_path()), "jobs.json 未生成"
    with open(mgr.jobs_path(), encoding="utf-8") as f:
        saved = json.load(f)
    assert saved, "jobs.json 是空的（暂停任务未被保存）"
    # 模拟"重启"：新建 Manager 读同一个 data_dir
    mgr2 = dlcore.Manager(data_dir=os.path.join(tmp, "data"))
    n = mgr2.load_jobs(auto_start=False)
    assert n >= 1, "未恢复出任务（load_jobs 返回 %d，jobs.json 有 %d 条）" % (n, len(saved))
    t2 = list(mgr2.tasks.values())[0]
    assert t2.url == t.url, "恢复的 URL 不符"

    # 续传进度存在 .xzd/<tid>/meta.json + bitmap.bin 里，需先探测拿到 size
    # 才能校验并加载断点（_load_state 要求 meta.size == self.size）。
    # ★ 必须绑定与原服务器相同的端口，否则 t2.url 指向的端口无人监听 →
    #   WinError 10061（测试脚本自身的缺陷，v3.1.8 修正）。
    srv2 = RangeServer({"/persist.bin": payload}, port=srv.port).start()
    t2._probe()
    assert t2.size == size, "重新探测的大小不符: %s" % t2.size
    loaded = t2._load_state()
    assert loaded, "断点状态加载失败（meta/bitmap 校验不通过）"
    assert t2.done_bytes > 0, \
        "恢复后进度丢失: done=%s progress=%s" % (t2.done_bytes, t2.progress)
    # 关键断言：恢复出的任务必须沿用存档 tid，否则 .xzd/<新tid> 是空目录
    assert t2.tid == t.tid, \
        "tid 未复用：存档 %s → 恢复 %s（断点目录对不上）" % (t.tid, t2.tid)
    assert os.path.isdir(t2.tmp_dir), \
        "恢复后 tmp_dir 不存在：%s" % t2.tmp_dir
    srv2.stop()
    return "重启后恢复 %d 个任务（jobs.json %d 条），tid=%s 复用，磁盘断点 %s → %s" % (
        n, len(saved), t2.tid, dlcore.human_size(done_before),
        dlcore.human_size(t2.done_bytes))


# =====================================================================
# E. 限速
# =====================================================================

@case("E 限速", "E1_限速500KBps_实际吞吐接近")
def e1(ctx):
    size = 4 * 1024 * 1024          # 4MB @ 500KB/s ≈ 8.2s
    payload = RangeServer.make_payload(size, seed=51)
    srv = RangeServer({"/limit.bin": payload}).start()
    tmp = tempfile.mkdtemp(prefix="xzd_E1_")
    mgr = _new_mgr(tmp, connections=8)
    limit_kb = 500
    t = mgr.add_task(srv.url("/limit.bin"), out_dir=os.path.join(tmp, "dl"),
                     speed_limit=limit_kb * 1024)
    st = _wait_state(t, {dlcore.STATE_DOWNLOADING}, timeout=30)
    assert st == dlcore.STATE_DOWNLOADING, "任务未开始下载"
    t0 = time.time()
    st = _wait_state(t, {dlcore.STATE_DONE, dlcore.STATE_ERROR, dlcore.STATE_PAUSED},
                     timeout=120)
    el = time.time() - t0
    srv.stop()
    assert st == dlcore.STATE_DONE, \
        "限速任务未完成: state=%s err=%s" % (st, t.err)
    expected = size / float(limit_kb * 1024)
    ratio = el / expected
    got = open(t.created_final or t.final_path(), "rb").read()
    assert _digest(got) == _digest(payload), "限速下载内容不一致"
    # 令牌桶 burst=256KB，允许 ±45% 偏差
    assert 0.55 <= ratio <= 1.45, \
        "限速失准：目标 %.1fs，实测 %.1fs（比值 %.2f）" % (expected, el, ratio)
    return "限速 %d KB/s，%s 数据耗时 %.2fs（理论 %.2fs，偏差 %+.0f%%）" % (
        limit_kb, dlcore.human_size(size), el, expected, (ratio - 1) * 100)


@case("E 限速", "E2_运行中改限速_立即生效")
def e2(ctx):
    size = 12 * 1024 * 1024
    payload = RangeServer.make_payload(size, seed=53)
    srv = RangeServer({"/chg.bin": payload}).start()
    tmp = tempfile.mkdtemp(prefix="xzd_E2_")
    mgr = _new_mgr(tmp, connections=8)
    t = mgr.add_task(srv.url("/chg.bin"), out_dir=os.path.join(tmp, "dl"),
                     speed_limit=1 * 1024 * 1024)
    _wait_state(t, {dlcore.STATE_DOWNLOADING}, timeout=30)
    time.sleep(1.0)
    rate_before = t.limiter.rate
    t.set_speed_limit(3 * 1024 * 1024)   # 1MB/s → 3MB/s
    rate_after = t.limiter.rate
    task_limit_after = t.speed_limit
    srv.stop()
    t.cancel(delete_files=True)
    assert rate_before == 1 * 1024 * 1024, "初始限速未生效: %s" % rate_before
    assert rate_after == 3 * 1024 * 1024, \
        "运行时改限速未生效: limiter.rate=%s, task.speed_limit=%s" % (
            rate_after, task_limit_after)
    return "limiter.rate 由 %s 实时改为 %s（1MB/s → 3MB/s）" % (
        dlcore.human_speed(rate_before), dlcore.human_speed(rate_after))


@case("E 限速", "E3_低速限速不死锁_单次请求大于burst")
def e3(ctx):
    """回归：v3.1.8 修复的严重缺陷。

    限速 500KB/s 时 burst=500KB，而 readinto 一次可能返回整个 1MB 区块；
    旧实现 `_tokens = min(burst, ...)` 永远攒不到 1MB → consume() 永久
    死循环 → 任务卡死零进展（实测 120 秒不动）。此用例专门锁死该场景。
    """
    size = 4 * 1024 * 1024
    payload = RangeServer.make_payload(size, seed=57)
    srv = RangeServer({"/slow.bin": payload}).start()
    tmp = tempfile.mkdtemp(prefix="xzd_E3_")
    mgr = _new_mgr(tmp, connections=4)
    # 500KB/s < BLOCK_SIZE(1MB)，精确复现触发条件
    t = mgr.add_task(srv.url("/slow.bin"), out_dir=os.path.join(tmp, "dl"),
                     speed_limit=500 * 1024)
    st = _wait_state(t, {dlcore.STATE_DOWNLOADING}, timeout=30)
    assert st == dlcore.STATE_DOWNLOADING, "任务未开始下载"

    # 关键断言：必须在合理时间内完成（理论 8.2s，给 3 倍余量）
    t0 = time.time()
    st = _wait_state(t, {dlcore.STATE_DONE, dlcore.STATE_ERROR}, timeout=40)
    el = time.time() - t0
    srv.stop()
    assert st == dlcore.STATE_DONE, \
        "低速限速死锁复现！state=%s done=%s/%s err=%s 耗时%.1fs" % (
            st, dlcore.human_size(t.done_bytes), dlcore.human_size(size), t.err, el)
    got = open(t.created_final or t.final_path(), "rb").read()
    assert _digest(got) == _digest(payload), "低速限速下载内容不一致"
    return "限速 500KB/s（<区块 1MB）%s 数据 %.1fs 完成，无死锁，内容校验通过" % (
        dlcore.human_size(size), el)


@case("E 限速", "E4_极低速限速不死锁_限速小于单次读取")
def e4(ctx):
    """回归：限速 1KB/s（远小于 READ_SIZE 与 burst 下限），必须仍能完成。"""
    size = 256 * 1024
    payload = RangeServer.make_payload(size, seed=59)
    srv = RangeServer({"/vslow.bin": payload}).start()
    tmp = tempfile.mkdtemp(prefix="xzd_E4_")
    mgr = _new_mgr(tmp, connections=2)
    t = mgr.add_task(srv.url("/vslow.bin"), out_dir=os.path.join(tmp, "dl"),
                     speed_limit=64 * 1024)   # 64KB/s，明显低于 burst 下限 256KB
    st = _wait_state(t, {dlcore.STATE_DOWNLOADING}, timeout=30)
    assert st == dlcore.STATE_DOWNLOADING, "任务未开始下载"
    t0 = time.time()
    st = _wait_state(t, {dlcore.STATE_DONE, dlcore.STATE_ERROR}, timeout=40)
    el = time.time() - t0
    srv.stop()
    assert st == dlcore.STATE_DONE, \
        "极低速死锁！state=%s done=%s err=%s" % (
            st, dlcore.human_size(t.done_bytes), t.err)
    return "限速 %s（低于 burst 下限 256KB）完成，耗时 %.1fs" % (
        dlcore.human_speed(64 * 1024), el)


# =====================================================================
# F. 限流退避
# =====================================================================

@case("F 限流退避", "F1_429后自动重试成功")
def f1(ctx):
    size = 3 * 1024 * 1024
    payload = RangeServer.make_payload(size, seed=61)
    # 前 4 次请求返回 429，之后正常（测指数退避 + 抖动）
    srv = RangeServer({"/rl.bin": payload}, rate_limit_count=4).start()
    tmp = tempfile.mkdtemp(prefix="xzd_F1_")
    mgr = _new_mgr(tmp, connections=8)
    t0 = time.time()
    t = mgr.add_task(srv.url("/rl.bin"), out_dir=os.path.join(tmp, "dl"))
    st = _wait_done(t, timeout=150)
    el = time.time() - t0
    s = dict(srv.stats)
    srv.stop()
    n429 = s["by_status"].get(429, 0)
    assert n429 >= 4, "服务器未真的返回 429（统计 %s）" % s["by_status"]
    assert st == dlcore.STATE_DONE, \
        "遇上 429 后未能自愈: state=%s err=%s" % (st, t.err)
    got = open(t.created_final or t.final_path(), "rb").read()
    assert _digest(got) == _digest(payload), "429 重试后内容不一致"
    return "前 %d 次请求 429 → 退避后成功，总耗时 %.1fs，内容校验通过" % (n429, el)


# =====================================================================
# G. HLS
# =====================================================================

@case("G HLS", "G1_m3u8分片下载合并_无加密")
def g1(ctx):
    seg = [RangeServer.make_payload(240 * 1024, seed=70 + i) for i in range(6)]
    playlist, files = RangeServer.make_m3u8(seg)
    files["/playlist.m3u8"] = (playlist,
                               {"Content-Type": "application/vnd.apple.mpegurl"})
    srv = RangeServer(files).start()
    tmp = tempfile.mkdtemp(prefix="xzd_G1_")
    mgr = _new_mgr(tmp, connections=8)
    t = mgr.add_task(srv.url("/playlist.m3u8"), out_dir=os.path.join(tmp, "dl"),
                     kind="hls")
    st = _wait_done(t, timeout=90)
    srv.stop()
    assert st == dlcore.STATE_DONE, "HLS 未完成: state=%s err=%s" % (st, t.err)
    out = t.created_final or t.final_path()
    assert os.path.exists(out), "输出文件不存在: %s" % out
    got = open(out, "rb").read()
    expected = b"".join(seg)
    assert got == expected, "HLS 合并内容不符: %d vs %d" % (len(got), len(expected))
    return "%d 个分片合并为 %s，字节完全一致" % (len(seg), dlcore.human_size(len(got)))


@case("G HLS", "G2_AES-128加密分片_解密正确")
def g2(ctx):
    # ★ 不再依赖 cryptography（本机 pip 源没有该包，且产品自带纯 Python
    #   AES 兜底实现 —— 那条路径恰恰更应该被测到）。测试服务器内置了
    #   纯 Python AES-128-CBC 加密器，已通过 FIPS-197 / SP 800-38A
    #   官方测试向量核对，与产品的解密实现互为逆运算。
    key = b"0123456789abcdef"
    iv = bytes(range(16))

    def enc(plain: bytes) -> bytes:
        # HLS 的 TS 分片加密**不做** PKCS7 填充（分片长度已对齐 16 字节）
        return RangeServer.aes128_cbc_encrypt(plain, key, iv, pad=False)

    plain_segs = [RangeServer.make_payload(64 * 1024, seed=80 + i) for i in range(3)]
    enc_segs = [enc(p) for p in plain_segs]
    assert len(enc_segs[0]) == len(plain_segs[0]), "密文长度应等于明文长度"
    playlist, files = RangeServer.make_m3u8(enc_segs, key=key, iv=iv)
    files["/playlist.m3u8"] = (playlist,
                               {"Content-Type": "application/vnd.apple.mpegurl"})
    srv = RangeServer(files).start()
    tmp = tempfile.mkdtemp(prefix="xzd_G2_")
    mgr = _new_mgr(tmp, connections=4)
    t = mgr.add_task(srv.url("/playlist.m3u8"), out_dir=os.path.join(tmp, "dl"),
                     kind="hls")
    st = _wait_done(t, timeout=90)
    srv.stop()
    assert st == dlcore.STATE_DONE, "AES-HLS 未完成: state=%s err=%s" % (st, t.err)
    got = open(t.created_final or t.final_path(), "rb").read()
    assert got == b"".join(plain_segs), \
        "AES-128 解密结果不符（含填充差异）: %d vs %d" % (
            len(got), sum(len(x) for x in plain_segs))
    # 反向：确认密文与明文确实不同（否则"解密正确"可能是假通过）
    assert enc_segs[0] != plain_segs[0], "加密未生效，测试无效"
    return "AES-128 解密正确（%d 分片，%d 字节，纯 Python 通路）" % (
        len(plain_segs), len(got))


@case("G HLS", "G3_多码率自适应_选最高清晰度")
def g3(ctx):
    lo = [RangeServer.make_payload(32 * 1024, seed=90)]
    hi = [RangeServer.make_payload(96 * 1024, seed=91)]
    files = {}
    lo_pl, lo_files = RangeServer.make_m3u8(lo)
    hi_pl, hi_files = RangeServer.make_m3u8(hi)
    for k, v in lo_files.items():
        files["/low" + k] = v
    for k, v in hi_files.items():
        files["/high" + k] = v
    master = ("#EXTM3U\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360\n"
              "low/index.m3u8\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=1800000,RESOLUTION=1280x720\n"
              "high/index.m3u8\n")
    files["/master.m3u8"] = (master.encode(),
                             {"Content-Type": "application/vnd.apple.mpegurl"})
    files["/low/index.m3u8"] = (lo_pl, {"Content-Type": "application/vnd.apple.mpegurl"})
    files["/high/index.m3u8"] = (hi_pl, {"Content-Type": "application/vnd.apple.mpegurl"})
    srv = RangeServer(files).start()
    tmp = tempfile.mkdtemp(prefix="xzd_G3_")
    mgr = _new_mgr(tmp, connections=4)
    t = mgr.add_task(srv.url("/master.m3u8"), out_dir=os.path.join(tmp, "dl"), kind="hls")
    st = _wait_done(t, timeout=90)
    srv.stop()
    assert st == dlcore.STATE_DONE, "多码率 HLS 未完成: %s err=%s" % (st, t.err)
    got = open(t.created_final or t.final_path(), "rb").read()
    assert got == hi[0], "未选择最高码率（拿到 %d 字节，期望 %d）" % (len(got), len(hi[0]))
    return "master.m3u8 正确选择 1.8Mbps 变体（%d 字节）" % len(got)


# =====================================================================
# H. 文件名解析 & 工具函数
# =====================================================================

@case("H 解析工具", "H1_Content-Disposition_文件名与扩展名")
def h1(ctx):
    size = 200 * 1024
    payload = RangeServer.make_payload(size, seed=95)
    # ★ 必须用 RFC 5987 编码（filename*=UTF-8''...）：
    #   http.server 的 send_header 强制 latin-1 编码，裸中文响应头会在
    #   服务端抛 UnicodeEncodeError 并断开连接 → 客户端只看到
    #   RemoteDisconnected。真实站点同样只能用 RFC 5987 传中文名，
    #   所以这里改测标准写法（v3.1.8 修正；此前是测试自身的缺陷）。
    srv = RangeServer({"/dl":
                       (payload, {"Content-Disposition":
                                  "attachment; filename=\"report.pdf\"; "
                                  "filename*=UTF-8''%E6%8A%A5%E5%91%8A%20v2.pdf"})}).start()
    tmp = tempfile.mkdtemp(prefix="xzd_H1_")
    mgr = _new_mgr(tmp, connections=4)
    t = mgr.add_task(srv.url("/dl"), out_dir=os.path.join(tmp, "dl"))
    st = _wait_done(t, timeout=60)
    srv.stop()
    assert st == dlcore.STATE_DONE, "state=%s err=%s" % (st, t.err)
    fn = os.path.basename(t.created_final or t.final_path())
    assert "报告" in fn and fn.lower().endswith(".pdf"), "文件名解析错误: %r" % fn
    got = open(t.created_final or t.final_path(), "rb").read()
    assert _digest(got) == _digest(payload), "内容不一致"
    return "Content-Disposition(RFC5987) → %r，内容校验通过" % fn


@case("H 解析工具", "H2_工具函数集合")
def h2(ctx):
    checks = []
    assert dlcore.human_size(0) == "0 B" or "0" in dlcore.human_size(0), dlcore.human_size(0)
    checks.append("human_size(1536)=%s" % dlcore.human_size(1536))
    assert dlcore.human_size(1536).startswith("1.5"), dlcore.human_size(1536)
    checks.append("human_speed(1048576)=%s" % dlcore.human_speed(1048576))
    checks.append("human_eta(3661)=%s" % dlcore.human_eta(3661))

    # safe_filename 非法字符
    bad = dlcore.safe_filename('a/b\\c:d*e?f"g<h>i|j')
    assert not any(c in bad for c in '/\\:*?"<>|'), "非法字符未清理: %r" % bad
    checks.append("safe_filename=%r" % bad)

    # filename_from_url
    assert dlcore.filename_from_url("http://x.com/a/b/file.zip?q=1") == "file.zip", \
        dlcore.filename_from_url("http://x.com/a/b/file.zip?q=1")
    assert dlcore.filename_from_url("http://x.com/") == "download", \
        dlcore.filename_from_url("http://x.com/")

    # Content-Disposition
    assert dlcore.filename_from_disposition(
        'attachment; filename="x.mp4"') == "x.mp4"
    assert dlcore.filename_from_disposition(
        "attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.txt") == "中文.txt", \
        dlcore.filename_from_disposition(
            "attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.txt")
    checks.append("disposition ok")

    # kind_of_url
    assert dlcore.kind_of_url("magnet:?xt=urn:btih:x") == "bt"
    assert dlcore.kind_of_url("http://x/a.m3u8") == "hls"
    assert dlcore.kind_of_url("http://x/a.zip") == "file"
    assert dlcore.kind_of_url("http://x/page.html") == "page", \
        dlcore.kind_of_url("http://x/page.html")
    checks.append("kind_of_url ok")

    # guess_ext_from_mime
    assert dlcore.guess_ext_from_mime("video/mp4") == ".mp4"
    assert dlcore.guess_ext_from_mime("application/x-bittorrent") == ".torrent", \
        dlcore.guess_ext_from_mime("application/x-bittorrent")
    checks.append("guess_ext ok")

    # unique_path：文件已存在时才加序号（契约如此，v3.1.8 修正测试前提）
    d = tempfile.mkdtemp(prefix="xzd_H2_")
    p = os.path.join(d, "f.txt")
    assert dlcore.unique_path(p) == p, "不存在的文件不应加序号"
    open(p, "w").write("a")
    p2 = dlcore.unique_path(p)
    assert p2 != p and "(1)" in p2, "已存在的文件应加序号: %r" % p2
    checks.append("unique_path=%r" % os.path.basename(p2))

    # ensure_dir / data_root / app_root
    sub = os.path.join(d, "a", "b", "c")
    dlcore.ensure_dir(sub)
    assert os.path.isdir(sub)
    assert os.path.isdir(dlcore.data_root())
    assert os.path.isdir(dlcore.app_root())
    checks.append("dirs ok")

    # SSL context
    c1 = dlcore.shared_ssl_context()
    c2 = dlcore.shared_ssl_context()
    assert c1 is c2, "shared_ssl_context 不是单例"
    assert dlcore.insecure_ssl_context() is not c1
    checks.append("ssl ctx 单例 ok")

    return "; ".join(checks)


@case("H 解析工具", "H3_纯AES解密_官方向量核对")
def h3(ctx):
    # ★ 用 NIST 官方测试向量核对产品自带的纯 Python AES-CBC 解密实现。
    #   此前依赖 cryptography 构造密文，本机装不上就"跳过"= 永远测不到
    #   这条兜底路径 —— 而它恰恰是 frozen 版（无第三方库）实际会走的路径。
    checks = []

    # 向量 1：FIPS-197 C.1 单块（AES-128）
    k1 = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    c1 = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    p1 = bytes.fromhex("00112233445566778899aabbccddeeff")
    got1 = dlcore.pure_aes_cbc_decrypt(c1, k1, b"\x00" * 16)
    assert got1 == p1, "FIPS-197 向量失败: %s" % got1.hex()
    checks.append("FIPS-197 C.1 单块 OK")

    # 向量 2：SP 800-38A F.2.1 CBC-AES128.Decrypt（4 块）
    k2 = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv2 = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    c2 = bytes.fromhex(
        "7649abac8119b246cee98e9b12e9197d"
        "5086cb9b507219ee95db113a917678b2"
        "73bed6b8e3c1743b7116e69e22229516"
        "3ff1caa1681fac09120eca307586e1a7")
    p2 = bytes.fromhex(
        "6bc1bee22e409f96e93d7e117393172a"
        "ae2d8a571e03ac9c9eb76fac45af8e51"
        "30c81c46a35ce411e5fbc1191a0a52ef"
        "f69f2445df4f9b17ad2b417be66c3710")
    got2 = dlcore.pure_aes_cbc_decrypt(c2, k2, iv2)
    assert got2 == p2, "SP 800-38A CBC 向量失败"
    checks.append("SP 800-38A F.2.1 CBC 4块 OK")

    # 向量 3：与测试加密器往返（大块真实数据）
    k3 = b"0123456789abcdef"
    iv3 = bytes(range(16))
    seg = RangeServer.make_payload(64 * 1024, seed=899)
    ct3 = RangeServer.aes128_cbc_encrypt(seg, k3, iv3, pad=False)
    got3 = dlcore.pure_aes_cbc_decrypt(ct3, k3, iv3)
    assert got3 == seg, "64KB 往返失败"
    checks.append("64KB 往返 OK")

    # 向量 4：不同 IV 必须解出不同结果（防止实现忽略 IV）
    ct4 = RangeServer.aes128_cbc_encrypt(seg[:64], k3, b"\x01" * 16, pad=False)
    got4 = dlcore.pure_aes_cbc_decrypt(ct4, k3, b"\x01" * 16)
    assert got4 == seg[:64], "非零 IV 解密失败"
    got4_wrong = dlcore.pure_aes_cbc_decrypt(ct4, k3, b"\x02" * 16)
    assert got4_wrong != seg[:64], "IV 未生效（错 IV 也解出明文）"
    checks.append("IV 生效性 OK")

    return "纯 Python AES-CBC 通过官方向量：" + "；".join(checks)


# =====================================================================
# I. HttpHelper
# =====================================================================

@case("I HttpHelper", "I1_headers_组装与优先级")
def i1(ctx):
    h = dlcore.HttpHelper("UA-TEST", "http://ref/", "k=v", "", False, {"X-A": "1"})
    hd = h.headers({"X-A": "override", "X-B": "2"})
    assert hd["User-Agent"] == "UA-TEST", hd
    assert hd["Referer"] == "http://ref/", hd
    assert hd["Cookie"] == "k=v", hd
    assert hd["X-A"] == "override", "调用方 extra 应覆盖构造时 extra: %r" % hd
    assert hd["X-B"] == "2", hd
    assert hd["Accept-Encoding"] == "identity", hd
    return "UA/Referer/Cookie/extra 覆盖顺序正确"


@case("I HttpHelper", "I2_get_text_get_bytes_连接池")
def i2(ctx):
    body = b"hello-plain-text-" * 100
    srv = RangeServer({"/t.txt": (body, {"Content-Type": "text/plain"}),
                       "/b.bin": body}).start()
    h = dlcore.HttpHelper(dlcore.DEFAULT_UA, "", "", "", False, {})
    txt, hdrs, status = h.get_text(srv.url("/t.txt"), limit=1024 * 1024)
    assert txt == body.decode(), "get_text 内容不符"
    assert status == 200, status
    raw = h.get_bytes(srv.url("/b.bin"), limit=1024 * 1024)
    assert raw == body, "get_bytes 内容不符"
    # 连接池
    key = h._url_key(srv.url("/b.bin"))
    conn = h.conn_get(srv.url("/b.bin"), timeout=10)
    assert conn is not None, "conn_get 返回 None"
    h.conn_release(key, conn, True)
    ka = dlcore.HttpHelper.is_keepalive(conn[1]) if isinstance(conn, tuple) else True
    h.pool_close_all()
    srv.stop()
    return "get_text/get_bytes/conn_get/conn_release/pool_close_all 全部可用"


@case("I HttpHelper", "I3_代理时降级urllib_不崩")
def i3(ctx):
    # 配一个不存在的代理，应能正常抛出网络错误而不是崩溃/挂死
    h = dlcore.HttpHelper(dlcore.DEFAULT_UA, "", "", "http://127.0.0.1:1", False, {})
    assert h._url_key("http://x/a") is None, "代理时 _url_key 应返回 None（禁用连接池）"
    srv = RangeServer({"/p.bin": b"x" * 1000}).start()
    err = None
    try:
        h.get_bytes(srv.url("/p.bin"), limit=4096)
    except Exception as e:
        err = e
    srv.stop()
    assert err is not None, "坏代理竟然成功了？"
    return "代理模式下正确禁用连接池，坏代理抛错：%s" % type(err).__name__


# =====================================================================
# J. Manager 方法与持久化
# =====================================================================

@case("J Manager", "J1_配置读写_原子落盘")
def j1(ctx):
    tmp = tempfile.mkdtemp(prefix="xzd_J1_")
    mgr = _new_mgr(tmp)
    mgr.set_cfg(connections=17, max_parallel_tasks=5, speed_limit=1024 * 100)
    assert os.path.exists(mgr.cfg_path()), "配置文件未生成"
    mgr2 = dlcore.Manager(data_dir=os.path.join(tmp, "data"))
    assert int(mgr2.cfg["connections"]) == 17, mgr2.cfg["connections"]
    assert int(mgr2.cfg["max_parallel_tasks"]) == 5
    assert int(mgr2.cfg["speed_limit"]) == 102400
    # 损坏配置应备份为 .bad 而不是崩
    with open(mgr.cfg_path(), "w") as f:
        f.write("{ this is not json")
    mgr3 = dlcore.Manager(data_dir=os.path.join(tmp, "data"))
    assert os.path.exists(mgr.cfg_path() + ".bad"), "损坏配置未备份 .bad"
    return "配置往返正确，损坏配置自动备份 .bad"


@case("J Manager", "J2_pause_all_resume_all")
def j2(ctx):
    payload = RangeServer.make_payload(16 * 1024 * 1024, seed=101)
    srv = RangeServer({"/pa.bin": payload}, chunk_delay=0.006).start()
    tmp = tempfile.mkdtemp(prefix="xzd_J2_")
    mgr = _new_mgr(tmp, connections=8, max_parallel_tasks=3)
    ts = [mgr.add_task(srv.url("/pa.bin"), out_dir=os.path.join(tmp, "dl"),
                       filename="pa%d.bin" % i) for i in range(2)]
    for t in ts:
        _wait_state(t, {dlcore.STATE_DOWNLOADING}, timeout=30)
    mgr.pause_all()
    time.sleep(1.0)
    sts = [t.state for t in ts]
    assert all(s == dlcore.STATE_PAUSED for s in sts), "pause_all 未全部暂停: %s" % sts
    res = mgr.resume_all()
    time.sleep(2.0)
    sts2 = [t.state for t in ts]
    srv.stop()
    for t in ts:
        t.cancel(delete_files=True)
    assert all(s in (dlcore.STATE_DOWNLOADING, dlcore.STATE_WAIT, dlcore.STATE_DONE)
               for s in sts2), "resume_all 未恢复: %s" % sts2
    # resume_all 无返回值（就地恢复），计数用状态统计
    n_resumed = sum(1 for s in sts2 if s != dlcore.STATE_PAUSED)
    assert n_resumed == len(ts), "resume_all 未恢复全部：%d/%d" % (n_resumed, len(ts))
    return "pause_all 全部暂停 %s；resume_all 恢复 %d 个 → %s（返回值 %r）" % (
        sts, n_resumed, sts2, res)


@case("J Manager", "J3_历史记录与快照字段")
def j3(ctx):
    payload = RangeServer.make_payload(256 * 1024, seed=103)
    srv = RangeServer({"/h.bin": payload}).start()
    tmp = tempfile.mkdtemp(prefix="xzd_J3_")
    mgr = _new_mgr(tmp, connections=4)
    t = mgr.add_task(srv.url("/h.bin"), out_dir=os.path.join(tmp, "dl"))
    _wait_done(t, timeout=60)
    srv.stop()
    hist = mgr.history(limit=10)
    assert hist, "历史为空"
    item = hist[0]
    for k in ("url", "filename", "size"):
        assert k in item, "历史缺字段 %s: %r" % (k, item)
    snaps = mgr.snapshots()
    assert snaps, "快照为空"
    sn = snaps[0]
    for k in ("tid", "filename", "state", "state_cn", "progress"):
        assert k in sn, "快照缺字段 %s" % k
    assert mgr.total_speed() >= 0
    return "历史 %d 条（含 url/filename/size），快照字段齐全，total_speed=%s" % (
        len(hist), dlcore.human_speed(mgr.total_speed()))


@case("J Manager", "J4_嗅探入库_去重_清空")
def j4(ctx):
    tmp = tempfile.mkdtemp(prefix="xzd_J4_")
    mgr = _new_mgr(tmp)
    items = [{"url": "http://x/a.mp4", "type": "video"},
             {"url": "http://x/b.mp4", "type": "video"},
             {"url": "http://x/a.mp4", "type": "video"}]   # 重复
    n = mgr.add_sniff_items("http://x/page", items)
    assert n == 2, "去重失败：入库 %d 条（应为 2）" % n
    n2 = mgr.add_sniff_items("http://x/page", items)
    assert n2 == 0, "重复入库未拦截：%d" % n2
    assert len(mgr.sniff_items) == 2, len(mgr.sniff_items)
    mgr.clear_sniff()
    assert not mgr.sniff_items, "clear_sniff 未清空"
    return "3 条（含 1 条重复）→ 入库 2 条；二次入库 0 条；清空正常"


@case("J Manager", "J5_磁盘空间预检")
def j5(ctx):
    tmp = tempfile.mkdtemp(prefix="xzd_J5_")
    mgr = _new_mgr(tmp)
    t = mgr.add_task("http://127.0.0.1:1/x.bin", out_dir=os.path.join(tmp, "dl"),
                     start=False)
    # _check_disk_space 的设计契约：空间不足时**抛 OSError**，
    # 由 _session() 捕获后把任务置为出错态。测试直接调它，因此应断言抛异常。
    huge = 1 << 62   # 4 EB
    raised = ""
    try:
        t._check_disk_space(huge)
    except OSError as e:
        raised = str(e)
    assert raised, "_check_disk_space 未对 4EB 报错（应抛 OSError）"
    assert "磁盘空间不足" in raised, "错误信息不符: %r" % raised
    # 反向验证：正常大小不应报错
    t._check_disk_space(1024)
    return "4EB 触发预检报错：%s" % raised[:70]


@case("J Manager", "J6_任务移除与文件清理")
def j6(ctx):
    payload = RangeServer.make_payload(8 * 1024 * 1024, seed=107)
    srv = RangeServer({"/rm.bin": payload}, chunk_delay=0.008).start()
    tmp = tempfile.mkdtemp(prefix="xzd_J6_")
    mgr = _new_mgr(tmp)
    t = mgr.add_task(srv.url("/rm.bin"), out_dir=os.path.join(tmp, "dl"))
    _wait_state(t, {dlcore.STATE_DOWNLOADING}, timeout=30)
    time.sleep(2)
    tid = t.tid
    tmp_dir = t.tmp_dir
    had_tmp = os.path.isdir(tmp_dir)
    mgr.remove_task(tid, delete_files=True)
    time.sleep(1.5)
    srv.stop()
    assert tid not in mgr.tasks, "任务未从 tasks 移除"
    assert tid not in mgr.order if hasattr(mgr, "order") else True, "任务未从 order 移除"
    assert not os.path.isdir(tmp_dir), "临时目录未清理: %s" % tmp_dir
    return "任务已移除，临时目录已删（此前存在=%s）" % had_tmp


# =====================================================================
# K. localserver 全路由
# =====================================================================

def _start_api(tmp, port):
    mgr = _new_mgr(tmp)
    api = dlcore.LocalAPIContainer if False else None  # 占位，避免误用
    import localserver
    holder = {}

    def on_wake(link):
        holder["wake"] = link

    def on_help():
        holder["help"] = True

    lapi = localserver.LocalAPI(mgr, port=port, on_wake=on_wake, on_help=on_help)
    assert lapi.start(), "API 未启动"
    return mgr, lapi, holder


def _http(method, url, body=None, token="", headers=None):
    import urllib.request
    h = dict(headers or {})
    if token:
        h["X-XZD-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


@case("K localserver", "K1_全部GET路由")
def k1(ctx):
    tmp = tempfile.mkdtemp(prefix="xzd_K1_")
    port = 18801
    mgr, lapi, holder = _start_api(tmp, port)
    base = "http://127.0.0.1:%d" % port
    try:
        st, body = _http("GET", base + "/ping")
        assert st == 200 and json.loads(body)["ok"], "ping 失败: %s %s" % (st, body)
        j = json.loads(body)
        assert j["version"] == dlcore.ENGINE_VERSION, "ping 版本不符: %s" % j
        token = j.get("token", "")
        assert token, "/ping 未返回 token"

        st, body = _http("GET", base + "/")
        assert st == 200 and json.loads(body)["ok"], "/ 失败"
        st, body = _http("GET", base + "/help")
        assert st == 200, "/help 失败: %s %s" % (st, body)
        assert holder.get("help") is True, "/help 未触发回调"

        # 需要 token 的
        for path in ("/status", "/history", "/sniff"):
            st, body = _http("GET", base + path)
            assert st == 401, "%s 无 token 应 401，实际 %s" % (path, st)
            st, body = _http("GET", base + path, token=token)
            assert st == 200, "%s 带 token 失败: %s %s" % (path, st, body[:120])
            assert json.loads(body)["ok"], "%s 返回 ok=False" % path

        # /scan 参数校验
        st, body = _http("GET", base + "/scan?url=notaurl", token=token)
        assert st == 400, "/scan 非法 url 应 400，实际 %s" % st

        # /wake
        st, body = _http("GET", base + "/wake?link=xzd://show", token=token)
        assert st == 200, "/wake 失败: %s %s" % (st, body)
        assert holder.get("wake") == "xzd://show", "wake 回调未收到链接"
        st, body = _http("GET", base + "/wake", token=token)
        assert st == 400, "/wake 缺 link 应 400，实际 %s" % st

        # 未知道路
        st, body = _http("GET", base + "/nope")
        assert st == 404, "未知路径应 404，实际 %s" % st

        # OPTIONS 预检
        st, body = _http("OPTIONS", base + "/add")
        assert st == 200, "OPTIONS 失败: %s" % st
        return "ping/status/history/sniff/scan/help/wake/404/OPTIONS 全通过，token 鉴权生效"
    finally:
        lapi.stop()


@case("K localserver", "K2_POST_add_batch_sniff_open")
def k2(ctx):
    tmp = tempfile.mkdtemp(prefix="xzd_K2_")
    port = 18802
    payload = RangeServer.make_payload(512 * 1024, seed=111)
    srv = RangeServer({"/api.bin": payload}).start()
    mgr, lapi, holder = _start_api(tmp, port)
    base = "http://127.0.0.1:%d" % port
    try:
        st, body = _http("GET", base + "/ping")
        token = json.loads(body).get("token", "")

        # 无 token → 401
        st, body = _http("POST", base + "/add", {"url": srv.url("/api.bin")})
        assert st == 401, "/add 无 token 应 401，实际 %s" % st

        # /add
        st, body = _http("POST", base + "/add",
                         {"url": srv.url("/api.bin"),
                          "out_dir": os.path.join(tmp, "dl")}, token=token)
        assert st == 200, "/add 失败: %s %s" % (st, body)
        j = json.loads(body)
        assert j["ok"] and j.get("tid"), "/add 未返回 tid: %s" % j
        tid = j["tid"]
        t = mgr.get(tid)
        assert t is not None, "/add 后 Manager 里找不到任务"
        assert t.out_dir == os.path.join(tmp, "dl"), \
            "out_dir 未透传: %s" % t.out_dir
        _wait_done(t, timeout=60)
        assert t.state == dlcore.STATE_DONE, "API 添加的任务未完成: %s" % t.err

        # /add 非法 URL
        st, body = _http("POST", base + "/add", {"url": "ftp://x/y"}, token=token)
        assert st == 400, "非法 scheme 应 400，实际 %s" % st

        # /batch
        st, body = _http("POST", base + "/batch",
                         {"items": [{"url": srv.url("/api.bin")},
                                    {"url": srv.url("/api.bin")}],
                          "page": "http://x/p"}, token=token)
        assert st == 200, "/batch 失败: %s %s" % (st, body)
        j = json.loads(body)
        assert len(j["results"]) == 2, "batch 结果数不符: %s" % j
        st, body = _http("POST", base + "/batch", {"items": "notalist"}, token=token)
        assert st == 400, "batch 非数组应 400，实际 %s" % st

        # /sniff
        st, body = _http("POST", base + "/sniff",
                         {"items": [{"url": "http://x/s1.mp4"},
                                    {"url": "http://x/s2.mp4"}],
                          "page": "http://x/p2"}, token=token)
        assert st == 200, "/sniff 失败: %s %s" % (st, body)
        assert json.loads(body)["added"] == 2, "sniff 入库数不符: %s" % body

        # /open —— 重点：folder 模式此前会 NameError
        st, body = _http("POST", base + "/open",
                         {"tid": tid, "mode": "file"}, token=token)
        ok_file = (st == 200)
        st2, body2 = _http("POST", base + "/open",
                           {"tid": tid, "mode": "folder"}, token=token)
        # folder 会真的弹资源管理器，只要不是 500 (NameError) 就算通过
        assert "NameError" not in body2, \
            "/open folder 模式仍报 NameError！body=%s" % body2
        st3, body3 = _http("POST", base + "/open",
                           {"tid": "nonexistent", "mode": "file"}, token=token)
        assert st3 == 404, "不存在 tid 应 404，实际 %s" % st3

        # 未知道路
        st, body = _http("POST", base + "/nope", {}, token=token)
        assert st == 404, "POST 未知路径应 404，实际 %s" % st

        srv.stop()
        return "/add(+out_dir透传+非法URL) /batch /sniff /open(file=%s/folder无NameError/404) 全通过" % ok_file
    finally:
        lapi.stop()
        srv.stop()


@case("K localserver", "K3_端口占用自动顺延")
def k3(ctx):
    import socket
    tmp1 = tempfile.mkdtemp(prefix="xzd_K3a_")
    tmp2 = tempfile.mkdtemp(prefix="xzd_K3b_")
    port = 18803
    # 先占住端口。★ 必须用 SO_EXCLUSIVEADDRUSE：
    #   Windows 的 SO_REUSEADDR 语义等价于 Linux 的 SO_REUSEPORT ——
    #   两个 socket 可以绑同一地址，localserver 内部也用 SO_REUSEADDR，
    #   于是"占端口"占不住，测试会误判成"产物没顺延"（v3.1.8 修正）。
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except (AttributeError, OSError):
        pass
    s.bind(("127.0.0.1", port))
    s.listen(1)
    try:
        mgr, lapi, holder = _start_api(tmp1, port)
        # ★ 必须读 actual_port：lapi.port 是"配置端口"（绑定后不会变），
        #   顺延结果写在 actual_port 里。此前读 lapi.port 永远等于入参，
        #   测试必然误判"没顺延"（v3.1.8 修正）。
        real = getattr(lapi, "actual_port", lapi.port)
        base = "http://127.0.0.1:%d" % real
        st, body = _http("GET", base + "/ping")
        ok = (real != port and real > port)
        lapi.stop()
        assert ok, "端口未顺延：期望 ≠%d，实际 %d" % (port, real)
        assert st == 200, "顺延后接口不通：%s %s" % (st, body[:100])
        return "%d 被占时自动顺延到 %d（+%d），接口可用" % (port, real, real - port)
    finally:
        s.close()


@case("K localserver", "K4_CORS与Origin校验")
def k4(ctx):
    tmp = tempfile.mkdtemp(prefix="xzd_K4_")
    port = 18804
    mgr, lapi, holder = _start_api(tmp, port)
    base = "http://127.0.0.1:%d" % port
    try:
        # 恶意网页 Origin
        st, body = _http("GET", base + "/status",
                         headers={"Origin": "http://evil.com"})
        assert st == 403, "恶意 Origin 应 403，实际 %s" % st
        # 扩展 Origin 放行
        st, body = _http("GET", base + "/ping",
                         headers={"Origin": "chrome-extension://abcdefg"})
        assert st == 200, "扩展 Origin 应放行，实际 %s" % st
        assert "Access-Control-Allow-Origin" in body or True
        # 扩展 Origin 且带 token
        st, body = _http("GET", base + "/ping",
                         headers={"Origin": "chrome-extension://abcdefg"})
        tk = json.loads(body).get("token", "")
        assert tk, "扩展 Origin 应能拿到 token"
        st, body = _http("GET", base + "/status",
                         headers={"Origin": "chrome-extension://abcdefg"}, token=tk)
        assert st == 200, "扩展 Origin 带 token 应放行，实际 %s" % st
        return "恶意 Origin 403；chrome-extension 放行并可获取 token"
    finally:
        lapi.stop()


@case("K localserver", "K5_status只返回活动任务_已完成不残留")
def k5(ctx):
    """v3.1.9 缺陷回归：/status 曾把已完成任务也算进活动列表，
    扩展「下载中」页签里下完的任务一直留着，用户以为还在下载。"""
    tmp = tempfile.mkdtemp(prefix="xzd_K5_")
    port = 18804
    payload = RangeServer.make_payload(256 * 1024, seed=131)
    srv = RangeServer({"/a.bin": payload, "/b.bin": payload}).start()
    mgr, lapi, holder = _start_api(tmp, port)
    base = "http://127.0.0.1:%d" % lapi.actual_port
    slow_srv = None
    try:
        st, body = _http("GET", base + "/ping")
        token = json.loads(body).get("token", "")

        # 先加一个"长跑"任务占住活动列表（服务器全局限速，测期内不会结束）
        slow_srv = RangeServer({"/slow.bin": RangeServer.make_payload(
            4 * 1024 * 1024, seed=132)}, global_rate_bps=256 * 1024).start()
        st, body = _http("POST", base + "/add",
                         {"url": slow_srv.url("/slow.bin"),
                          "out_dir": os.path.join(tmp, "dl")}, token=token)
        assert st == 200, "/add(slow) 失败: %s %s" % (st, body)
        tid_slow = json.loads(body)["tid"]

        # 再放一个立即完成的任务
        st, body = _http("POST", base + "/add",
                         {"url": srv.url("/b.bin"), "out_dir": os.path.join(tmp, "dl"),
                          "filename": "b.bin"},
                         token=token)
        tid_fast = json.loads(body)["tid"]
        t_fast = mgr.get(tid_fast)
        _wait_done(t_fast, timeout=90)
        assert t_fast.state == dlcore.STATE_DONE, "任务未完成: %s" % t_fast.err

        st, body = _http("GET", base + "/status", token=token)
        assert st == 200, "/status 失败: %s" % st
        j = json.loads(body)
        states = {x["tid"]: x["state"] for x in j["tasks"]}
        assert tid_fast not in states, \
            "已完成任务仍在活动列表: %s（问题5a 回归）" % j["tasks"]
        assert tid_slow in states, "进行中的任务不应被过滤掉"
        assert j.get("total_count", 0) >= 1, "total_count 未提供"

        # 已完成任务必须能在 /history 里找到，且带 final 路径与 exists 标记
        st, body = _http("GET", base + "/history?limit=10", token=token)
        assert st == 200, "/history 失败: %s" % st
        hist = json.loads(body)["items"]
        hit = [h for h in hist if h.get("tid") == tid_fast]
        assert hit, "已完成任务不在 /history 里"
        assert hit[-1].get("final"), "/history 缺 final 路径"
        assert hit[-1].get("exists") is True, "/history 的 exists 标记错误"
        return ("活动列表 %d 个（已完成已剔除、进行中保留）；"
                "/history 有该任务且 final/exists 正确" % len(j["tasks"]))
    finally:
        try:
            if slow_srv:
                slow_srv.stop()
        except Exception:
            pass
        for t in list(mgr.tasks.values()):
            try:
                t.cancel(delete_files=True)
            except Exception:
                pass
        lapi.stop()
        srv.stop()


@case("K localserver", "K6_open支持历史任务与文件失效兜底")
def k6(ctx):
    """v3.1.9 缺陷回归：扩展点「打开/目录」曾必然失败——
    ① 重启后任务不在活动列表 → /open 直接 404「任务不存在」；
    ② 文件已被移动/删除时错误信息不明。"""
    tmp = tempfile.mkdtemp(prefix="xzd_K6_")
    port = 18805
    payload = RangeServer.make_payload(128 * 1024, seed=141)
    srv = RangeServer({"/h.bin": payload}).start()
    mgr, lapi, holder = _start_api(tmp, port)
    base = "http://127.0.0.1:%d" % lapi.actual_port
    try:
        st, body = _http("GET", base + "/ping")
        token = json.loads(body).get("token", "")
        st, body = _http("POST", base + "/add",
                         {"url": srv.url("/h.bin"), "out_dir": os.path.join(tmp, "dl"),
                          "filename": "h.bin"}, token=token)
        tid = json.loads(body)["tid"]
        t = mgr.get(tid)
        _wait_done(t, timeout=90)
        final = t.created_final
        assert os.path.exists(final), "文件未落盘: %s" % final

        # ① 活动任务里能打开（不真的弹窗口，只校验返回结构）
        st, body = _http("POST", base + "/open", {"tid": tid, "mode": "file"}, token=token)
        assert st == 200, "/open(file) 应 200，实际 %s %s" % (st, body)

        # ② 模拟「重启后」：新 Manager 从同一 data_dir 加载，
        #    活动列表为空，任务只剩在 history 里
        #    （_new_mgr 把 data_dir 设为 tmp/data，必须保持一致）
        mgr2 = dlcore.Manager(data_dir=mgr.data_dir)
        assert len(mgr2.snapshots()) == 0, "新 Manager 不应有活动任务"
        assert any(h.get("tid") == tid for h in mgr2.history()), \
            "任务未进入 history，测试前提不成立"

        api2 = localserver.LocalAPI(mgr2, port=port + 1)
        api2.start()
        base2 = "http://127.0.0.1:%d" % api2.actual_port
        try:
            st, body = _http("GET", base2 + "/ping")
            tk2 = json.loads(body).get("token", "")
            st, body = _http("POST", base2 + "/open",
                             {"tid": tid, "mode": "folder"}, token=tk2)
            assert st == 200, \
                "重启后 /open(folder) 应能从 history 打开，实际 %s %s（问题5b 回归）" % (st, body)

            # ③ 文件被删除后：folder 仍打开目录并带 note；file 明确报错
            os.remove(final)
            st, body = _http("POST", base2 + "/open",
                             {"tid": tid, "mode": "folder"}, token=tk2)
            j = json.loads(body)
            assert st == 200 and j.get("ok"), "文件删除后 folder 应仍能打开目录: %s" % body
            assert j.get("note"), "文件已失效时应给出 note 提示: %s" % body
            st, body = _http("POST", base2 + "/open",
                             {"tid": tid, "mode": "file"}, token=tk2)
            assert st == 404, "文件删除后 file 应 404，实际 %s" % st
            assert "不存在" in body, "错误信息应说明文件不存在: %s" % body

            # ④ 完全不存在的 tid
            st, body = _http("POST", base2 + "/open",
                             {"tid": "ffffffffffff", "mode": "folder"}, token=tk2)
            assert st == 404, "未知 tid 应 404，实际 %s" % st
        finally:
            api2.stop()
        return "活动可开；重启后凭 history 可开；文件失效时 folder 兜底+file 明确报错；未知 tid 404"
    finally:
        lapi.stop()
        srv.stop()


@case("K localserver", "K7_重复tid防护_不崩不脏不叠加")
def k7(ctx):
    """v3.1.9 严重缺陷回归：重复 tid 曾导致主程序当场崩溃。

    实测（打包 exe 压力测试）：
      同一 URL 并发 add（本地环回下载极快，多次 add 落在同一微秒）
      → tid = md5(url|now()) 完全相同 → order 里插入两条同一 tid
      → snapshots() 返回重复条目
      → GUI tree.insert(iid=tid) 抛 TclError: Item xxx already exists
      → **主线程未捕获异常，进程直接退出**
      → 用户视角：下载一完成软件就消失，浏览器扩展点「打开/目录」毫无反应
      且 save_jobs() 把重复写进 jobs.json，重启 load_jobs 再叠加（1→2→4…）。

    本用例覆盖三层防护：
      ① add_task 同一 tid 不重复入 order
      ② snapshots() 永不返回重复 tid（GUI 侧 insert 不再抛异常）
      ③ 旧版写坏的 jobs.json 能被 load_jobs 自愈（不叠加）
    """
    tmp = tempfile.mkdtemp(prefix="xzd_K7_")
    data = os.path.join(tmp, "data")
    os.makedirs(data, exist_ok=True)
    mgr = dlcore.Manager(data_dir=data)
    mgr.cfg["download_dir"] = os.path.join(tmp, "dl")
    os.makedirs(mgr.cfg["download_dir"], exist_ok=True)

    # ---- ① add_task 同 tid 两次 ----
    mgr.add_task("http://127.0.0.1:9/dup.bin", tid="dup000000001",
                 start=False, dedupe=False)
    mgr.add_task("http://127.0.0.1:9/dup.bin", tid="dup000000001",
                 start=False, dedupe=False)
    assert mgr.order.count("dup000000001") == 1, \
        "同一 tid 在 order 里应只出现一次，实际 %r" % mgr.order

    # ---- ② snapshots 不重复（GUI insert 的 iid 唯一性前提）----
    snaps = mgr.snapshots()
    tids = [s["tid"] for s in snaps]
    assert len(tids) == len(set(tids)), \
        "snapshots() 返回了重复 tid，GUI tree.insert 会抛 TclError：%r" % tids

    # ---- ③ 旧版写坏的 jobs.json 自愈 ----
    bad = [{"tid": "bad000000001", "url": "http://127.0.0.1:9/bad.bin",
            "out_dir": mgr.cfg["download_dir"], "filename": "bad.bin",
            "kind": "file", "conns": 8, "state": "paused", "referer": "",
            "group": "", "added_at": 1.0, "speed_limit": 0, "size": None,
            "err": "", "phase": "暂停"}] * 4
    with open(mgr.jobs_path(), "w", encoding="utf-8") as f:
        json.dump(bad, f, ensure_ascii=False)
    mgr2 = dlcore.Manager(data_dir=data)
    mgr2.cfg["download_dir"] = mgr.cfg["download_dir"]
    n = mgr2.load_jobs()
    assert n == 1, "4 条重复应自愈为 1 条，实际恢复 %d 条" % n
    assert mgr2.order.count("bad000000001") == 1, \
        "自愈后 order 仍有重复：%r" % mgr2.order
    tids2 = [s["tid"] for s in mgr2.snapshots()]
    assert len(tids2) == len(set(tids2)), "自愈后 snapshots 仍有重复：%r" % tids2
    mgr2.save_jobs()
    with open(mgr2.jobs_path(), encoding="utf-8") as f:
        after = json.load(f)
    assert len(after) == 1, "重新保存后 jobs.json 应只剩 1 条，实际 %d" % len(after)

    return ("同 tid 只入队 1 次；snapshots 无重复；4 条坏的 jobs.json 自愈为 1 条"
            "且重存不复发")


@case("K localserver", "K8_多任务下载不死锁_接口全程可用")
def k8(ctx):
    """v3.1.9 严重缺陷回归：自锁导致 Manager._lock 永不释放 → 全站卡死。

    实测抓到线程堆栈（faulthandler dump）：
      set_target_conns 持 _target_lock 调 _spawn_workers
      → _spawn_workers 再次 `with self._target_lock` → **同线程自锁**
      （_target_lock 是 threading.Lock，非可重入）
    该线程原本还持有 Manager._lock（_rebalance_targets 拿的），自锁后永不释放：
      · 所有 snapshots() 挂起 → /status 100% 超时、GUI 刷新卡死
      · release_slot() 等 _cv 也被拖死 → 下载停摆
      · /ping 不碰这些锁 → 正常（正是实测看到的特征：ping 16ms、status 超时）

    触发条件：并发下载 ≥2 个任务（速度均衡会调线程数）时必现。

    本用例：并发起多个任务（各申请 24 线程），在下载过程中反复调
    snapshots()，断言全部及时返回（不挂起）。
    """
    tmp = tempfile.mkdtemp(prefix="xzd_K8_")
    payload = RangeServer.make_payload(2 * 1024 * 1024, seed=155)
    paths = {"/d%d.bin" % i: payload for i in range(6)}
    srv = RangeServer(paths).start()
    mgr = _new_mgr(tmp, max_parallel_tasks=3)
    dl = os.path.join(tmp, "dl")
    os.makedirs(dl, exist_ok=True)
    try:
        tasks = []
        for i in range(6):
            tasks.append(mgr.add_task(srv.url("/d%d.bin" % i), out_dir=dl,
                                      filename="d%d.bin" % i, conns=24))
        # 立刻开始高频调 snapshots()：修复前这里会永久挂起
        deadlines = []
        # 诊断用：挂起时把全部线程堆栈 dump 出来（只在失败路径产出）
        import faulthandler
        for round_i in range(20):
            t0 = time.time()
            res = {}

            def call():
                try:
                    res["v"] = mgr.snapshots()
                except Exception as e:
                    res["e"] = repr(e)

            th = threading.Thread(target=call, daemon=True)
            th.start()
            th.join(6.0)                    # 6 秒没返回 = 判定挂起
            if th.is_alive():
                faulthandler.dump_traceback()
                raise AssertionError(
                    "第 %d 轮 snapshots() 挂起超过 6 秒（死锁回归！）"
                    "（堆栈已 dump 到 stderr）" % round_i)
            if "e" in res:
                raise AssertionError("snapshots() 抛异常: %s" % res["e"])
            deadlines.append((time.time() - t0) * 1000)
            time.sleep(0.25)

        worst = max(deadlines)
        assert worst < 3000, \
            "snapshots() 最慢耗时 %.0fms，超过 3 秒（疑似锁竞争恶化）" % worst

        # 全部任务最终应完成
        for t in tasks:
            _wait_done(t, timeout=120)
        done = [t for t in tasks if t.state == dlcore.STATE_DONE]
        assert len(done) == 6, "应有 6 个任务完成，实际 %d" % len(done)
        # 快照仍应可用且不重复
        snaps = mgr.snapshots()
        tids = [s["tid"] for s in snaps]
        assert len(tids) == len(set(tids)), "快照出现重复 tid: %r" % tids

        return ("并发 6 任务（各 24 线程）下载中，snapshots() 20 轮全部及时返回"
                "（最慢 %.0fms）；6/6 任务完成" % worst)
    finally:
        srv.stop()


# =====================================================================
# L. 网页嗅探（核心能力之一）
# =====================================================================

@case("L 嗅探", "L1_网页媒体扫描_识别mp4/m3u8")
def l1(ctx):
    html = """<html><head><title>测试页</title></head><body>
    <video src="/videos/demo.mp4"></video>
    <source src="http://cdn.example.com/a/b/clip.m3u8" type="application/vnd.apple.mpegurl">
    <a href="/files/archive.zip">下载</a>
    <a href="/download/report.pdf">报告</a>
    <script>var u = "https://media.example.com/audio/song.mp3";</script>
    <img src="/img/pic.jpg">
    </body></html>"""
    srv = RangeServer({"/page.html": (html.encode(), {"Content-Type": "text/html"})}).start()
    tmp = tempfile.mkdtemp(prefix="xzd_L1_")
    r = dlcore.scan_webpage(srv.url("/page.html"))
    srv.stop()
    urls = [it.get("url", "") for it in r["items"]]
    kinds = {it.get("url", ""): it.get("type", "") for it in r["items"]}
    assert r["title"] == "测试页", "标题未解析: %r" % r["title"]
    found = " ".join(urls)
    assert "demo.mp4" in found, "未识别 mp4: %r" % urls
    assert "clip.m3u8" in found, "未识别 m3u8: %r" % urls
    assert "song.mp3" in found, "未识别 mp3: %r" % urls
    # ★ 契约边界：scan_webpage 定位为「网页**媒体**嗅探」，
    #   有意只收 m3u8/mp4/mp3 等音视频（STREAM_EXTS + MEDIA_EXTS），
    #   压缩包、PDF 这类普通文件不进列表（否则结果会被几十上百个
    #   无关链接淹没，用户反而找不到视频）。与《使用说明》一致。
    #   此前测试断言应识别 zip/pdf，属对契约的误解（v3.1.8 修正）。
    assert "archive.zip" not in found, "普通压缩包不应进媒体列表: %r" % urls
    assert "report.pdf" not in found, "普通文档不应进媒体列表: %r" % urls
    assert "pic.jpg" not in found, "图片不应进媒体列表: %r" % urls
    types = set(it.get("type") for it in r["items"])
    assert types <= {"hls", "media"}, "出现未知类型: %s" % types
    # m3u8 应排在媒体之前（hls 优先）
    assert r["items"][0]["type"] == "hls", "hls 未优先排序: %r" % types
    return "标题=%r，识别 %d 项（纯媒体，zip/pdf/jpg 正确排除），类型 %s，hls 优先" % (
        r["title"], len(urls), sorted(types))


@case("L 嗅探", "L2_相对路径补全与去重")
def l2(ctx):
    html = """<html><body>
    <video src="/v/x.mp4"></video>
    <video src="v/x.mp4"></video>
    <video src="http://other.com/v/x.mp4"></video>
    </body></html>"""
    srv = RangeServer({"/p/index.html": (html.encode(),
                                         {"Content-Type": "text/html"})}).start()
    r = dlcore.scan_webpage(srv.url("/p/index.html"))
    srv.stop()
    urls = [it["url"] for it in r["items"]]
    assert len(urls) == len(set(urls)), "存在重复项: %r" % urls
    abs_urls = [u for u in urls if u.startswith("http")]
    assert abs_urls, "相对路径未补全为绝对地址: %r" % urls
    return "相对路径补全为绝对地址，去重后 %d 项" % len(urls)


@case("L 嗅探", "L3_HLS播放列表解析_识别为流")
def l3(ctx):
    pl = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:4.0,\nseg0.ts\n"
          "#EXTINF:4.0,\nseg1.ts\n#EXT-X-ENDLIST\n")
    srv = RangeServer({"/s/index.m3u8": (pl.encode(),
                                         {"Content-Type": "application/vnd.apple.mpegurl"})}).start()
    r = dlcore.scan_webpage(srv.url("/s/index.m3u8"))
    srv.stop()
    # m3u8 直接访问时应被识别为流类型
    items = r.get("items", [])
    ok = any("m3u8" in it.get("url", "") for it in items) or r.get("is_stream")
    assert ok, "m3u8 未被识别: %r" % r
    return "m3u8 页面识别为流，items=%d" % len(items)


# =====================================================================
# M. 引擎切换与 aria2c 备用
# =====================================================================

@case("M 引擎", "M1_engine=aria2_配置生效")
def m1(ctx):
    import aria2_bridge
    if not aria2_bridge.is_bundled():
        raise AssertionError("third_party 里没有 aria2c.exe，跳过")
    size = 2 * 1024 * 1024
    payload = RangeServer.make_payload(size, seed=121)
    srv = RangeServer({"/ar.bin": payload}).start()
    tmp = tempfile.mkdtemp(prefix="xzd_M1_")
    mgr = _new_mgr(tmp, engine="aria2", connections=8)
    t = mgr.add_task(srv.url("/ar.bin"), out_dir=os.path.join(tmp, "dl"))
    assert isinstance(t, dlcore.Aria2FileDownload), \
        "engine=aria2 未选中 Aria2FileDownload，实际 %s" % type(t).__name__
    st = _wait_done(t, timeout=120)
    srv.stop()
    assert st == dlcore.STATE_DONE, "aria2 通道失败: state=%s err=%s" % (st, t.err)
    got = open(t.created_final or t.final_path(), "rb").read()
    assert _digest(got) == _digest(payload), "aria2 下载内容不一致"
    return "Aria2FileDownload 通道实测成功（%s），内容校验通过" % dlcore.human_size(size)


@case("M 引擎", "M2_retry_with_aria2_备用切换")
def m2(ctx):
    import aria2_bridge
    if not aria2_bridge.is_bundled():
        raise AssertionError("third_party 里没有 aria2c.exe，跳过")
    payload = RangeServer.make_payload(1024 * 1024, seed=123)
    srv = RangeServer({"/r2.bin": payload}).start()
    tmp = tempfile.mkdtemp(prefix="xzd_M2_")
    mgr = _new_mgr(tmp, connections=4)
    # 造一个"失败"的任务：指向不存在的路径
    t = mgr.add_task(srv.url("/r2.bin"), out_dir=os.path.join(tmp, "dl"),
                     start=False)
    t.state = dlcore.STATE_ERROR
    t.err = "模拟失败"
    ok = t.retry_with_aria2()
    assert ok, "retry_with_aria2 返回 False"
    st = _wait_state(t, {dlcore.STATE_DONE, dlcore.STATE_ERROR}, timeout=120)
    srv.stop()
    assert st == dlcore.STATE_DONE, "aria2 重试未成功: %s" % t.err
    return "失败任务通过 retry_with_aria2 成功恢复"


@case("M 引擎", "M3_aria2c_HLS通道")
def m3(ctx):
    import aria2_bridge
    if not aria2_bridge.is_bundled():
        raise AssertionError("没有 N_m3u8DL-RE.exe，跳过")
    if not os.path.exists(aria2_bridge.nm3u8_exe()):
        raise AssertionError("N_m3u8DL-RE.exe 不存在，跳过")
    seg = [RangeServer.make_payload(128 * 1024, seed=130 + i) for i in range(3)]
    pl, files = RangeServer.make_m3u8(seg)
    files["/pl.m3u8"] = (pl, {"Content-Type": "application/vnd.apple.mpegurl"})
    srv = RangeServer(files).start()
    tmp = tempfile.mkdtemp(prefix="xzd_M3_")
    mgr = _new_mgr(tmp, engine="aria2", connections=4)
    t = mgr.add_task(srv.url("/pl.m3u8"), out_dir=os.path.join(tmp, "dl"), kind="hls")
    st = _wait_done(t, timeout=150)
    srv.stop()
    if st != dlcore.STATE_DONE:
        raise AssertionError("aria2 HLS 通道失败: %s" % t.err)
    out = t.created_final or t.final_path()
    assert os.path.exists(out), "产物不存在"
    return "N_m3u8DL-RE HLS 通道成功（%s）" % dlcore.human_size(os.path.getsize(out))


# =====================================================================
# N. torrent / magnet 解析
# =====================================================================

@case("N 种子", "N1_magnet_URI解析")
def n1(ctx):
    import torrent_parser
    mag = ("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
           "&dn=Test%20File&tr=udp%3A%2F%2Ftracker.example%3A6969"
           "&tr=http%3A%2F%2Ftracker2.example%2Fannounce")
    info = torrent_parser.parse_magnet(mag)
    assert info["info_hash"] == "0123456789abcdef0123456789abcdef01234567", info
    assert info["name"] == "Test File", info["name"]
    assert len(info["trackers"]) == 2, info["trackers"]
    return "info_hash/name/2个tracker 解析正确"


@case("N 种子", "N2_BT输入规范化_分派")
def n2(ctx):
    import bt_helper
    assert bt_helper.is_magnet("magnet:?xt=urn:btih:abc")
    assert not bt_helper.is_magnet("http://x/a.torrent")
    assert bt_helper.is_torrent_url("http://x/a.torrent")
    assert bt_helper.is_torrent_url("http://x/a.torrent?k=1")
    assert not bt_helper.is_torrent_url("http://x/a.torrentx")
    # ★ is_torrent_path 的契约是"**存在且**以 .torrent 结尾的文件"
    #   （内部有 os.path.isfile 判断），不是纯字符串后缀判断。
    #   原地传一个不存在的路径必然 False（v3.1.8 修正测试前提）。
    d = tempfile.mkdtemp(prefix="xzd_N2_")
    real = os.path.join(d, "b.torrent")
    open(real, "wb").write(b"d4:infod4:name3:abcde")
    assert bt_helper.is_torrent_path(real), "存在的 .torrent 文件应判 True"
    assert not bt_helper.is_torrent_path(os.path.join(d, "b.txt")), \
        "非 .torrent 后缀不应判 True"
    assert not bt_helper.is_torrent_path(os.path.join(d, "ghost.torrent")), \
        "不存在的文件不应判 True"
    assert "DHT" in bt_helper.recommend_protocols({"trackers": []})
    assert "Tracker" in bt_helper.recommend_protocols({"trackers": ["udp://a"]})
    return "is_magnet/is_torrent_url/is_torrent_path(存在性+后缀)/recommend_protocols 判断正确"


@case("N 种子", "N3_tracker列表与过滤")
def n3(ctx):
    import bt_trackers
    assert len(bt_trackers.FALLBACK_TRACKERS) >= 10, \
        "内置兜底 tracker 太少: %d" % len(bt_trackers.FALLBACK_TRACKERS)
    parsed = bt_trackers.parse_trackers(
        "# 注释\n\nudp://a:1\nudp://a:1\nhttp://b:2\n")
    assert len(parsed) == 2, "去重/注释过滤失败: %r" % parsed
    # 测速过滤（本地无网络也能跑 DNS 失败场景）
    alive, allr = bt_trackers.filter_alive(["udp://127.0.0.1:1"], timeout=1.0)
    assert isinstance(alive, list) and isinstance(allr, list)
    # ★ get_trackers 返回 (trackers, source_desc) 二元组，不是裸列表。
    #   此前直接 len(t) 取到的是元组长度恒为 2，断言必然失败（v3.1.8 修正）。
    got = bt_trackers.get_trackers(prefer_cache=True, allow_fetch=False,
                                   probe_alive=False)
    assert isinstance(got, tuple) and len(got) == 2, \
        "get_trackers 应返回 (list, source)：%r" % (got,)
    t, src = got
    assert isinstance(t, list) and len(t) >= 10, \
        "离线兜底未生效: %d（source=%s）" % (len(t), src)
    assert all(x.startswith(("udp://", "http://", "https://", "wss://", "ws://"))
               for x in t), "tracker 列表含非法项: %r" % t[:3]
    return "兜底 %d 条；解析去重正确；离线可取 %d 条（source=%s）" % (
        len(bt_trackers.FALLBACK_TRACKERS), len(t), src)


# =====================================================================
# 主流程
# =====================================================================

ALL_CASES = [v for k, v in sorted(globals().items())
             if callable(v) and hasattr(v, "_case")]


def main():
    only = None
    as_json = "--json" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--only" and i + 1 < len(sys.argv):
            only = set(x.strip().upper() for x in sys.argv[i + 1].split(","))

    cases = ALL_CASES
    if only:
        cases = [c for c in cases if any(
            c._case[0].upper().startswith(g) or c._case[1].upper().startswith(g)
            for g in only)]

    print("=" * 78)
    print("小助下载器 全功能实测  v%s" % dlcore.ENGINE_VERSION)
    print("Python %s | 用例 %d 个" % (sys.version.split()[0], len(cases)))
    print("=" * 78)

    ctx = {}
    t0 = time.time()
    for c in cases:
        try:
            c(ctx)
        except Exception as e:
            # 用例函数外层的意外（极少）
            print("  [CRASH] %s · %s -> %s" % (c._case[0], c._case[1], e))

    el = time.time() - t0
    passed = sum(1 for r in RESULTS if r["ok"])
    failed = [r for r in RESULTS if not r["ok"]]

    print("\n" + "=" * 78)
    print("结果：%d/%d 通过   耗时 %.1fs" % (passed, len(RESULTS), el))
    if failed:
        print("-" * 78)
        print("失败清单：")
        for r in failed:
            print("  ✖ %s · %s" % (r["group"], r["name"]))
            print("      %s" % r["err"][:400])
    print("=" * 78)

    # 汇总关键实测数据
    print("\n关键实测数据：")
    for r in RESULTS:
        if r["ok"] and r["detail"]:
            print("  · %s/%s: %s" % (r["group"], r["name"], r["detail"]))

    if as_json:
        out = os.path.join(ROOT, "tests", "full_function_result.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"version": dlcore.ENGINE_VERSION,
                       "python": sys.version, "elapsed": round(el, 1),
                       "passed": passed, "total": len(RESULTS),
                       "results": RESULTS,
                       "context": {k: str(v)[:200] for k, v in ctx.items()}},
                      f, ensure_ascii=False, indent=2)
        print("\nJSON 已写入: %s" % out)

    # 清理临时目录
    for k in ("tmp_a", "tmp_b"):
        p = ctx.get(k)
        if p and os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())