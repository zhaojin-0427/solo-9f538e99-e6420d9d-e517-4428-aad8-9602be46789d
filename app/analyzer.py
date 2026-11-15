# -*- coding: utf-8 -*-
"""发酵罐温度探头跳点分析（最长保留方案 + 必留/必剔/可选判定）。

业务目标
========
质控员给定：
  * 本批起始序号 ``start_seq``；
  * 非负有限的温变上限 ``limit``（单位 ℃/秒，按原文十进制精确解析）。
系统读取该序号起已采集的记录（最多 300 条），在
  * 记录数超过 300 条，或
  * 时刻未按序号严格递增
时拒绝分析。

随后求 **剔除记录最少** 的方案，使保留下来的相邻点满足

    |T_j - T_i| <= limit * (t_j - t_i)     （j 在 i 之后）

即把“可相邻保留”视为有向边，问题等价于兼容 DAG 上的 **最长路径**（路径上的
点即保留点）。对每个点分别计算以其结尾/开头的最长路径长度，可严格判定该点
是否出现在至少一条最长路径上；再按最长路径的“层”统计点数：

  * 某层只有一个点 —— 所有最长路径都经过它，**必留**；
  * 某层有多个点 —— 同层点彼此可替换，单个点仅出现在部分最优方案中，**可选**；
  * 不在任何最长路径上 —— **必剔**。

精确性
======
温度直接从入库原始行 ``raw`` 中按 JSON 数字语法取出十进制原文，用
:class:`fractions.Fraction` 精确表示；温变上限同样按十进制原文转 Fraction；
时间差换算为整数微秒。所有比较均为有理数精确比较，**不使用浮点容差**
（float 仅在最后展示时使用）。

时效与并发
==========
每次分析保存参数版与“采集水位”（记录数、最大序号、最近成功采集轮次、
文件进度快照）。分析采用两次事务：先快照读取、再计算，发布结果前复检水位；
复检发现期间有新记录入库时，本次结论只存为历史（stale_history），不会成为
当前结论。分析与采集使用各自的锁，分析期间采集仍可完成。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Callable, Dict, List, Optional, Tuple

from . import collector

MAX_BATCH = 300
ANALYSIS_LOCK = threading.Lock()

# 状态取值
STATE_CURRENT = "current"
STATE_STALE = "stale"                    # 曾为当前结论，依据已过期
STATE_STALE_HISTORY = "stale_history"    # 发布前即过期，从未成为当前结论
STATE_SUPERSEDED = "superseded"          # 被同参数的新结论取代
STATE_REJECTED = "rejected"              # 分析被拒绝（拒绝原因已留存）


class AnalysisReject(Exception):
    """分析被拒绝（数据层面的拒绝原因，需入库留痕）。"""


class AnalysisParamError(Exception):
    """表单参数本身不合法（不入库，直接回显提示）。"""


# JSON number 语法（不允许前导 + / NaN / Infinity）
_NUMBER_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


def _skip_json_string(text: str, i: int) -> int:
    """text[i] 为起始引号，返回闭合引号后的下标（支持 JSON 转义）。"""
    i += 1
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise AnalysisReject(f"原始记录 JSON 字符串未闭合：{text[:120]!r}")


def _temp_token_from_raw(raw: str) -> str:
    """从入库原始 JSON 行中精确取出 temp 的数字原文。

    用字符串感知扫描定位 ``"temp"`` 键（不会误匹配 ``"x_temp"`` 等），
    再跳过键与值之间的 JSON 空白，按 JSON number 语法切下数字原文。
    """
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c != '"':
            i += 1
            continue
        end = _skip_json_string(raw, i)
        if raw[i + 1 : end - 1] == "temp":
            j = end
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if j >= n or raw[j] != ":":
                raise AnalysisReject(f"temp 键后缺少冒号：{raw[:120]!r}")
            j += 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            m = _NUMBER_RE.match(raw, j)
            if m is None or not _NUMBER_RE.fullmatch(m.group(0)):
                raise AnalysisReject(f"temp 值不是合法 JSON 数字：{raw[:120]!r}")
            return m.group(0)
        i = end
    raise AnalysisReject(f"原始记录中找不到 temp 键：{raw[:120]!r}")


def parse_limit(text: str) -> Tuple[Fraction, str]:
    """把温变上限输入精确解析为 Fraction，返回 (值, 规范原文)。

    要求非负、有限的十进制数（不接受 NaN/Infinity/科学计数以外的表达式）。
    """
    if not isinstance(text, str):
        raise AnalysisParamError("温变上限必须是数字。")
    s = text.strip()
    if not s or not _NUMBER_RE.fullmatch(s):
        raise AnalysisParamError("温变上限必须是有限十进制数字（如 0.5 或 1）。")
    value = Fraction(s)  # 十进制字符串 -> 精确有理数
    if value < 0:
        raise AnalysisParamError("温变上限必须是非负数。")
    if not math.isfinite(float(value)):  # 理论上 Fraction 总是有限，防御性保留
        raise AnalysisParamError("温变上限必须有限。")
    # 规范化展示原文（保留用户输入的有效数字语义，去掉首尾空白）
    return value, s


def parse_start_seq(text: str) -> int:
    if not isinstance(text, str) or not text.strip().lstrip("-").isdigit():
        raise AnalysisParamError("起始序号必须是整数。")
    value = int(text.strip())
    if value <= 0:
        raise AnalysisParamError("起始序号必须是正整数。")
    return value


def _parse_utc_micros(ts: str) -> int:
    """UTC ISO-8601 时刻 -> 整数微秒（精确，无浮点）。"""
    text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AnalysisReject(f"时刻无法解析：{ts!r}（{exc}）")
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise AnalysisReject(f"时刻不是 UTC：{ts!r}")
    # timedelta 微秒级精确
    return int((dt - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1_000_000)


# --------------------------------------------------------------------------- #
# 建表
# --------------------------------------------------------------------------- #
def init_analysis_db(db_path: str) -> None:
    """建表并开启 WAL（分析期间允许并发采集，读写互不阻塞）。"""
    collector.init_db(db_path)
    with collector.get_db(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS analysis_params (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                start_seq  INTEGER NOT NULL,
                limit_text TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                UNIQUE(start_seq, limit_text)
            );

            CREATE TABLE IF NOT EXISTS analysis_runs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                param_id         INTEGER NOT NULL REFERENCES analysis_params(id),
                start_seq        INTEGER NOT NULL,
                limit_text       TEXT    NOT NULL,
                status           TEXT    NOT NULL,   -- ok / rejected
                state            TEXT    NOT NULL,   -- 见模块顶部 STATE_*
                reason           TEXT    NOT NULL DEFAULT '',
                n_input          INTEGER NOT NULL DEFAULT 0,
                n_kept           INTEGER NOT NULL DEFAULT 0,
                n_removed        INTEGER NOT NULL DEFAULT 0,
                n_must_keep      INTEGER NOT NULL DEFAULT 0,
                n_must_remove    INTEGER NOT NULL DEFAULT 0,
                n_optional       INTEGER NOT NULL DEFAULT 0,
                result_json      TEXT    NOT NULL DEFAULT '',
                watermark_json   TEXT    NOT NULL DEFAULT '',
                created_at       TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_analysis_runs_state
                ON analysis_runs(state, id DESC);
            """
        )


# --------------------------------------------------------------------------- #
# 水位
# --------------------------------------------------------------------------- #
def read_watermark(db_path: str) -> dict:
    """采集水位：判断“依据是否过期”的最小事实集 + 展示用快照。"""
    with collector.get_db(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(seq), 0) AS m FROM records"
        ).fetchone()
        last_ok = conn.execute(
            "SELECT id, finished_at FROM collect_runs WHERE status = 'ok'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        states = [
            dict(r)
            for r in conn.execute(
                "SELECT inode, path, offset, file_size, updated_at FROM file_state"
            ).fetchall()
        ]
    return {
        "record_count": row["c"],
        "max_seq": row["m"],
        "last_ok_run_id": last_ok["id"] if last_ok else None,
        "last_ok_run_at": last_ok["finished_at"] if last_ok else None,
        "file_states": states,
    }


def watermark_changed(before: dict, after: dict) -> bool:
    """只有新记录入库（记录数或最大序号变化）才使依据过期。"""
    return (
        before.get("record_count") != after.get("record_count")
        or before.get("max_seq") != after.get("max_seq")
    )


def mark_current_stale(db_path: str, reason: str) -> int:
    """把所有 current 结论标记为过期（stale），返回受影响行数。"""
    with collector.get_db(db_path) as conn:
        # 先锁定本次会被转换的行，避免给更早的历史行重复追加事件
        targets = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM analysis_runs WHERE state = ?", (STATE_CURRENT,)
            ).fetchall()
        ]
        if not targets:
            return 0
        conn.execute(
            "UPDATE analysis_runs SET state = ? WHERE state = ?",
            (STATE_STALE, STATE_CURRENT),
        )
        now = collector.utc_now_iso()
        for rid in targets:
            r = conn.execute(
                "SELECT result_json FROM analysis_runs WHERE id = ?", (rid,)
            ).fetchone()
            payload = json.loads(r["result_json"]) if r["result_json"] else {}
            # 只追加过期事件，原结论字段一律不动
            payload.setdefault("stale_events", []).append({"at": now, "reason": reason})
            conn.execute(
                "UPDATE analysis_runs SET result_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), rid),
            )
        return len(targets)


def reconcile_current(db_path: str) -> None:
    """读路径防御性复检：水位已变而状态仍是 current 时补标过期。"""
    with collector.get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT id, watermark_json FROM analysis_runs WHERE state = ?",
            (STATE_CURRENT,),
        ).fetchall()
        now = read_watermark(db_path)
        for r in rows:
            try:
                old = json.loads(r["watermark_json"])
            except (ValueError, TypeError):
                continue
            if watermark_changed(old, now):
                conn.execute(
                    "UPDATE analysis_runs SET state = ? WHERE id = ?",
                    (STATE_STALE, r["id"]),
                )


# --------------------------------------------------------------------------- #
# 核心算法
# --------------------------------------------------------------------------- #
@dataclass
class Point:
    pos: int                 # 在本批中的位置（0 起）
    seq: int
    ts: str                  # 时刻原文
    t_us: int                # 时刻（微秒，精确）
    temp_token: str          # 温度原文（JSON 数字）
    temp: Fraction           # 温度精确值
    source_path: str
    source_inode: int
    raw: str
    classification: str = ""  # must_keep / optional / must_remove
    level: int = 0


def _compatible(a: Point, b: Point, limit: Fraction) -> bool:
    """b 紧接 a 保留是否满足 |ΔT| <= limit × Δt（精确比较）。"""
    dt_us = b.t_us - a.t_us  # 调用方已保证严格递增，恒为正
    diff = b.temp - a.temp
    if diff < 0:
        diff = -diff
    # diff / dt <= limit  <=>  diff * 1_000_000 <= limit * dt_us
    return diff * 1_000_000 <= limit * dt_us


def solve(points: List[Point], limit: Fraction) -> dict:
    """求最少剔除方案，并对每个点给出必留/可选/必剔判定。

    以“可相邻保留”为边建 DAG（边按时间方向，故无环）：
      f[i] = 以 i 结尾的最长路径点数；g[i] = 以 i 开头的最长路径点数。
    点 i 属于某条最长路径  <=>  f[i] + g[i] - 1 == L（L 为最长路径长度）。
    把这些点按 f[i]-1 分层：每层恰为最长路径在该层的全部候选——
    层内仅一个点则所有最长路径都经过它（必留），否则层内点可互换（可选）。
    """
    n = len(points)
    f = [1] * n
    for j in range(n):
        best = 1
        for i in range(j):
            if _compatible(points[i], points[j], limit) and f[i] + 1 > best:
                best = f[i] + 1
        f[j] = best
    g = [1] * n
    for i in range(n - 1, -1, -1):
        best = 1
        for j in range(i + 1, n):
            if _compatible(points[i], points[j], limit) and g[j] + 1 > best:
                best = g[j] + 1
        g[i] = best

    longest = max(f) if n else 0
    on_path = [f[i] + g[i] - 1 == longest for i in range(n)]

    # 按层统计候选数
    level_counts: Dict[int, int] = {}
    for i, ok in enumerate(on_path):
        if ok:
            lvl = f[i] - 1
            points[i].level = lvl
            level_counts[lvl] = level_counts.get(lvl, 0) + 1

    for i, p in enumerate(points):
        if not on_path[i]:
            p.classification = "must_remove"
        elif level_counts[p.level] == 1:
            p.classification = "must_keep"
        else:
            p.classification = "optional"

    # 重构一组具体最优方案：从 f 最大的点沿“f 恰好 -1 且兼容”的前驱回溯，再反转
    chosen: List[int] = []
    if n:
        cur = next(i for i in range(n) if f[i] == longest)
        trail = [cur]
        while f[cur] > 1:
            prv = next(
                i
                for i in range(cur)
                if f[i] == f[cur] - 1 and _compatible(points[i], points[cur], limit)
            )
            trail.append(prv)
            cur = prv
        chosen = list(reversed(trail))
    chosen_set = set(chosen)

    # 自检：重构出的方案必须逐对满足约束且长度等于最长路径
    for a, b in zip(chosen, chosen[1:]):
        if not _compatible(points[a], points[b], limit):
            raise RuntimeError("内部错误：重构方案不满足温变约束")
    if len(chosen) != longest:
        raise RuntimeError("内部错误：重构方案不是最优长度")

    return {
        "longest": longest,
        "f": f,
        "g": g,
        "on_path": on_path,
        "level_counts": level_counts,
        "chosen": chosen,
        "chosen_set": chosen_set,
    }


def _load_snapshot(conn: sqlite3.Connection, start_seq: int) -> List[sqlite3.Row]:
    """单事务内读取起始序号起的记录（最多 MAX_BATCH+1 条，用于超限判定）。"""
    return conn.execute(
        "SELECT * FROM records WHERE seq >= ? ORDER BY seq ASC LIMIT ?",
        (start_seq, MAX_BATCH + 1),
    ).fetchall()


def analyze_snapshot(rows: List[sqlite3.Row], limit: Fraction) -> Tuple[List[Point], dict]:
    """对快照行执行校验与求解，返回 (点列表, 求解详情)。"""
    points: List[Point] = []
    prev_us: Optional[int] = None
    prev_seq: Optional[int] = None
    for pos, r in enumerate(rows):
        t_us = _parse_utc_micros(r["ts"])
        if prev_us is not None and t_us <= prev_us:
            raise AnalysisReject(
                f"时刻未按序号严格递增：序号 {prev_seq}（{rows[pos - 1]['ts']}）"
                f"之后的序号 {r['seq']}（{r['ts']}）时刻未严格变晚，拒绝分析。"
            )
        token = _temp_token_from_raw(r["raw"])
        points.append(
            Point(
                pos=pos,
                seq=r["seq"],
                ts=r["ts"],
                t_us=t_us,
                temp_token=token,
                temp=Fraction(token),
                source_path=r["source_path"],
                source_inode=r["source_inode"],
                raw=r["raw"],
            )
        )
        prev_us, prev_seq = t_us, r["seq"]
    detail = solve(points, limit)
    return points, detail


# --------------------------------------------------------------------------- #
# 参数版与结果持久化
# --------------------------------------------------------------------------- #
def _get_or_create_param(conn: sqlite3.Connection, start_seq: int, limit_text: str) -> int:
    row = conn.execute(
        "SELECT id FROM analysis_params WHERE start_seq = ? AND limit_text = ?",
        (start_seq, limit_text),
    ).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO analysis_params(start_seq, limit_text, created_at)"
        " VALUES (?, ?, ?)",
        (start_seq, limit_text, collector.utc_now_iso()),
    )
    return int(cur.lastrowid)


def _insert_run(conn: sqlite3.Connection, **kw) -> int:
    cur = conn.execute(
        "INSERT INTO analysis_runs(param_id, start_seq, limit_text, status, state, reason,"
        " n_input, n_kept, n_removed, n_must_keep, n_must_remove, n_optional,"
        " result_json, watermark_json, created_at)"
        " VALUES (:param_id, :start_seq, :limit_text, :status, :state, :reason,"
        " :n_input, :n_kept, :n_removed, :n_must_keep, :n_must_remove, :n_optional,"
        " :result_json, :watermark_json, :created_at)",
        kw,
    )
    return int(cur.lastrowid)


def list_runs(db_path: str, limit: int = 50) -> List[sqlite3.Row]:
    with collector.get_db(db_path) as conn:
        return conn.execute(
            "SELECT * FROM analysis_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def get_run(db_path: str, run_id: int) -> Optional[sqlite3.Row]:
    with collector.get_db(db_path) as conn:
        return conn.execute(
            "SELECT * FROM analysis_runs WHERE id = ?", (run_id,)
        ).fetchone()


def get_current(db_path: str) -> Optional[sqlite3.Row]:
    with collector.get_db(db_path) as conn:
        return conn.execute(
            "SELECT * FROM analysis_runs WHERE state = ? ORDER BY id DESC LIMIT 1",
            (STATE_CURRENT,),
        ).fetchone()


def run_analysis(
    db_path: str,
    start_seq: int,
    limit: Fraction,
    limit_text: str,
    after_snapshot: Optional[Callable[[], None]] = None,
) -> dict:
    """执行一次完整分析并持久化，返回可直接渲染的结果字典。

    ``after_snapshot`` 为测试钩子：在快照读取完成、结果发布前调用，
    用于模拟“分析期间有新记录采集入库”。
    """
    init_analysis_db(db_path)
    with ANALYSIS_LOCK:
        # 1) 读路径先做一次防御性对账
        reconcile_current(db_path)

        # 2) 快照事务：水位 + 参数版 + 记录快照
        with collector.get_db(db_path) as conn:
            watermark_before = read_watermark(db_path)
            param_id = _get_or_create_param(conn, start_seq, limit_text)
            rows = _load_snapshot(conn, start_seq)

        if after_snapshot is not None:
            after_snapshot()

        created_at = collector.utc_now_iso()
        wm_json = json.dumps(watermark_before, ensure_ascii=False)

        # “修改参数”判定：与现存当前结论的参数是否不同（只有改参数/新记录才使结论过期）
        current_row = get_current(db_path)
        params_changed = (
            current_row is None
            or current_row["start_seq"] != start_seq
            or current_row["limit_text"] != limit_text
        )

        # 3) 拒绝条件（数据层面，留痕为 rejected 历史）
        reject_reason = ""
        if not rows:
            reject_reason = (
                f"起始序号 {start_seq} 起没有任何已采集记录，无法分析。"
            )
        elif len(rows) > MAX_BATCH:
            reject_reason = (
                f"起始序号 {start_seq} 起的已采集记录超过 {MAX_BATCH} 条上限"
                f"（实际至少 {len(rows)} 条），拒绝分析；请调大本批起始序号缩小批次。"
            )
        if reject_reason:
            with collector.get_db(db_path) as conn:
                run_id = _insert_run(
                    conn,
                    param_id=param_id,
                    start_seq=start_seq,
                    limit_text=limit_text,
                    status="rejected",
                    state=STATE_REJECTED,
                    reason=reject_reason,
                    n_input=len(rows),
                    n_kept=0,
                    n_removed=0,
                    n_must_keep=0,
                    n_must_remove=0,
                    n_optional=0,
                    result_json="",
                    watermark_json=wm_json,
                    created_at=created_at,
                )
                # 仅当本次参数与当前结论不同（修改参数）时，旧当前结论才过期；
                # 同参数的失败尝试不影响既有当前结论
                if params_changed:
                    conn.execute(
                        "UPDATE analysis_runs SET state = ? WHERE state = ?",
                        (STATE_STALE, STATE_CURRENT),
                    )
            return {"status": "rejected", "reason": reject_reason, "run_id": run_id}

        # 4) 精确求解（库外计算，不持有事务，采集可并发写）
        try:
            points, detail = analyze_snapshot(rows, limit)
        except AnalysisReject as exc:
            with collector.get_db(db_path) as conn:
                run_id = _insert_run(
                    conn,
                    param_id=param_id,
                    start_seq=start_seq,
                    limit_text=limit_text,
                    status="rejected",
                    state=STATE_REJECTED,
                    reason=str(exc),
                    n_input=len(rows),
                    n_kept=0,
                    n_removed=0,
                    n_must_keep=0,
                    n_must_remove=0,
                    n_optional=0,
                    result_json="",
                    watermark_json=wm_json,
                    created_at=created_at,
                )
                if params_changed:
                    conn.execute(
                        "UPDATE analysis_runs SET state = ? WHERE state = ?",
                        (STATE_STALE, STATE_CURRENT),
                    )
            return {"status": "rejected", "reason": str(exc), "run_id": run_id}

        # 5) 组装结果载荷（保留原文温度与来源，不改原始记录）
        chosen_set = detail["chosen_set"]
        result_payload = {
            "params": {"start_seq": start_seq, "limit_text": limit_text},
            "watermark": watermark_before,
            "n_input": len(points),
            "n_kept": detail["longest"],
            "n_removed": len(points) - detail["longest"],
            "points": [
                {
                    "pos": p.pos,
                    "seq": p.seq,
                    "ts": p.ts,
                    "temp_token": p.temp_token,
                    "source_path": p.source_path,
                    "source_inode": p.source_inode,
                    "raw": p.raw,
                    "classification": p.classification,
                    "level": p.level,
                    "in_sample_plan": p.pos in chosen_set,
                    "f": detail["f"][p.pos],
                    "g": detail["g"][p.pos],
                }
                for p in points
            ],
            "sample_plan_seqs": [points[i].seq for i in detail["chosen"]],
            "level_counts": [
                {"level": lvl, "count": detail["level_counts"][lvl]}
                for lvl in sorted(detail["level_counts"])
            ],
        }
        counts = {
            "must_keep": sum(1 for p in points if p.classification == "must_keep"),
            "must_remove": sum(1 for p in points if p.classification == "must_remove"),
            "optional": sum(1 for p in points if p.classification == "optional"),
        }

        # 6) 发布事务：复检水位；期间有新记录则只存历史，不成为当前结论
        with collector.get_db(db_path) as conn:
            watermark_after = read_watermark(db_path)
            changed_during = watermark_changed(watermark_before, watermark_after)
            if changed_during:
                state = STATE_STALE_HISTORY
                reason = (
                    "分析期间采集到新记录（水位：记录数 "
                    f"{watermark_before['record_count']} -> {watermark_after['record_count']}，"
                    f"最大序号 {watermark_before['max_seq']} -> {watermark_after['max_seq']}），"
                    "本结论仅存为历史，不是当前结论。"
                )
                result_payload["stale_events"] = [
                    {"at": collector.utc_now_iso(), "reason": reason}
                ]
            else:
                # 同参数旧结论让位为历史；不同参数的当前结论按“参数修改即过期”处理
                conn.execute(
                    "UPDATE analysis_runs SET state = ? WHERE state = ?",
                    (STATE_SUPERSEDED, STATE_CURRENT),
                )
                state = STATE_CURRENT
                reason = ""
            run_id = _insert_run(
                conn,
                param_id=param_id,
                start_seq=start_seq,
                limit_text=limit_text,
                status="ok",
                state=state,
                reason=reason,
                n_input=len(points),
                n_kept=detail["longest"],
                n_removed=len(points) - detail["longest"],
                n_must_keep=counts["must_keep"],
                n_must_remove=counts["must_remove"],
                n_optional=counts["optional"],
                result_json=json.dumps(result_payload, ensure_ascii=False),
                watermark_json=wm_json,
                created_at=created_at,
            )

        return {
            "status": "ok",
            "state": state,
            "run_id": run_id,
            "payload": result_payload,
            "counts": counts,
            "changed_during": changed_during,
        }


def decode_result(row: sqlite3.Row) -> Optional[dict]:
    if not row or not row["result_json"]:
        return None
    return json.loads(row["result_json"])
