# -*- coding: utf-8 -*-
"""跳点分析器测试：算法正确性、精确判定、拒绝条件、时效与 Web 流程。

运行：PYTHONPATH=. python3 -I -m unittest -v tests.test_analyzer
"""

import json
import os
import random
import site
import sys
import tempfile
import unittest
from fractions import Fraction
from itertools import combinations

for _p in os.environ.get("TEST_SITE", "").split(os.pathsep):
    if _p:
        site.addsitedir(_p)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analyzer, collector  # noqa: E402


def raw_line(seq: int, ts: str, temp) -> str:
    return json.dumps({"seq": seq, "ts": ts, "temp": temp}, ensure_ascii=False)


def insert_records(db_path: str, specs):
    """直接把记录写入 records 表（绕过采集器，用于构造分析输入）。"""
    analyzer.init_analysis_db(db_path)
    now = collector.utc_now_iso()
    with collector.get_db(db_path) as conn:
        for seq, ts, temp in specs:
            raw = raw_line(seq, ts, temp)
            conn.execute(
                "INSERT INTO records(seq, ts, temp, raw, source_path, source_inode, collected_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (seq, ts, float(Fraction(str(temp))) if not isinstance(temp, float) else temp,
                 raw, "active.ndjson", 1000 + seq, now),
            )


def classify(payload):
    return {p["seq"]: p["classification"] for p in payload["points"]}


class AlgorithmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_a(self, specs, start_seq=1, limit="1"):
        insert_records(self.db, specs)
        lim, text = analyzer.parse_limit(limit)
        return analyzer.run_analysis(self.db, start_seq, lim, text)

    def test_single_jump_must_remove(self):
        # 每秒 1 个点、上限 1℃/秒；seq2 跳到 40 且回落，只能剔除它
        specs = [
            (1, "2026-11-15T08:00:00Z", 25),
            (2, "2026-11-15T08:00:01Z", 40),
            (3, "2026-11-15T08:00:02Z", 25),
        ]
        r = self.run_a(specs, limit="1")
        self.assertEqual(r["status"], "ok")
        p = r["payload"]
        self.assertEqual((p["n_input"], p["n_kept"], p["n_removed"]), (3, 2, 1))
        self.assertEqual(classify(p), {1: "must_keep", 2: "must_remove", 3: "must_keep"})
        self.assertEqual(p["sample_plan_seqs"], [1, 3])

    def test_optional_alternative_branches(self):
        # 点 2、3 互斥（温差 1.5，1 秒内上限 1 不允许相邻），但都可接 1、4：
        # 最长路径长度 3（1-2-4 或 1-3-4），2、3 同层可选；1、4 必留
        specs = [
            (1, "2026-11-15T08:00:00Z", 25),
            (2, "2026-11-15T08:00:01Z", 26),
            (3, "2026-11-15T08:00:02Z", 24.5),
            (4, "2026-11-15T08:00:03Z", 25),
        ]
        r = self.run_a(specs, limit="1")
        p = r["payload"]
        self.assertEqual(p["n_kept"], 3)
        self.assertEqual(p["n_removed"], 1)
        cls = classify(p)
        self.assertEqual(cls[1], "must_keep")
        self.assertEqual(cls[4], "must_keep")
        self.assertEqual(cls[2], "optional")
        self.assertEqual(cls[3], "optional")
        # 示例方案本身合法且为 3 个点
        self.assertEqual(len(p["sample_plan_seqs"]), 3)
        self.assertEqual(p["sample_plan_seqs"][0], 1)
        self.assertEqual(p["sample_plan_seqs"][-1], 4)

    def test_all_points_kept_when_within_limit(self):
        specs = [(i, f"2026-11-15T08:00:{i:02d}Z", 20 + i * 0.5) for i in range(1, 6)]
        r = self.run_a(specs, limit="1")
        p = r["payload"]
        self.assertEqual(p["n_removed"], 0)
        self.assertTrue(all(c == "must_keep" for c in classify(p).values()))
        self.assertEqual(p["sample_plan_seqs"], [1, 2, 3, 4, 5])

    def test_single_record_is_trivially_kept(self):
        r = self.run_a([(7, "2026-11-15T08:00:00Z", 25)], start_seq=7, limit="0")
        p = r["payload"]
        self.assertEqual((p["n_input"], p["n_kept"], p["n_removed"]), (1, 1, 0))
        self.assertEqual(classify(p), {7: "must_keep"})

    def test_zero_limit_requires_equal_temps(self):
        specs = [
            (1, "2026-11-15T08:00:00Z", 25),
            (2, "2026-11-15T08:00:01Z", 25),
            (3, "2026-11-15T08:00:02Z", 25.1),
            (4, "2026-11-15T08:00:03Z", 25),
        ]
        r = self.run_a(specs, limit="0")
        p = r["payload"]
        self.assertEqual(p["n_kept"], 3)
        self.assertEqual(classify(p)[3], "must_remove")

    def test_tied_optimal_solutions_classification(self):
        # 两条等长最长路径 1-2-4 与 1-3-4：2、3 可选
        specs = [
            (1, "2026-11-15T08:00:00Z", 25),
            (2, "2026-11-15T08:00:01Z", 26),
            (3, "2026-11-15T08:00:02Z", 24),
            (4, "2026-11-15T08:00:05Z", 25),
        ]
        r = self.run_a(specs, limit="1")
        p = r["payload"]
        self.assertEqual(p["n_kept"], 3)
        cls = classify(p)
        self.assertEqual(cls[1], "must_keep")
        self.assertEqual(cls[4], "must_keep")
        self.assertEqual(cls[2], "optional")
        self.assertEqual(cls[3], "optional")

    def test_large_gap_time_allows_big_temp_change(self):
        # ΔT=10，Δt=100s，上限 0.1 -> 恰好允许（10 == 0.1*100），精确相等不算超
        specs = [
            (1, "2026-11-15T08:00:00Z", 20),
            (2, "2026-11-15T08:01:40Z", 30),
        ]
        r = self.run_a(specs, limit="0.1")
        self.assertEqual(r["payload"]["n_removed"], 0)

    def test_exact_decimal_no_float_tolerance(self):
        # 经典浮点反例：0.1+0.2 != 0.3。用原文精确判定：
        # 25.1 -> 25.3，Δt=2s，上限 0.1 => 允许量恰为 0.2，温差 0.2 -> 允许
        specs = [
            (1, "2026-11-15T08:00:00Z", 25.1),
            (2, "2026-11-15T08:00:02Z", 25.3),
        ]
        r = self.run_a(specs, limit="0.1")
        self.assertEqual(r["payload"]["n_removed"], 0)
        # 上限改为 0.099：允许量 0.198 < 0.2 -> 必须剔除其一
        lim, text = analyzer.parse_limit("0.099")
        r2 = analyzer.run_analysis(self.db, 1, lim, text)
        self.assertEqual(r2["payload"]["n_kept"], 1)
        self.assertEqual(set(classify(r2["payload"]).values()), {"optional"})

    def test_reject_when_more_than_300_records(self):
        specs = [
            (i, f"2026-11-15T08:{i // 60:02d}:{i % 60:02d}Z", 25)
            for i in range(1, 303)  # 302 条
        ]
        r = self.run_a(specs)
        self.assertEqual(r["status"], "rejected")
        self.assertIn("300", r["reason"])
        # 拒绝留痕
        runs = analyzer.list_runs(self.db)
        self.assertEqual(runs[0]["status"], "rejected")
        self.assertEqual(runs[0]["state"], analyzer.STATE_REJECTED)

    def test_reject_on_non_increasing_timestamp(self):
        specs = [
            (1, "2026-11-15T08:00:02Z", 25),
            (2, "2026-11-15T08:00:02Z", 25),  # 相等时刻
            (3, "2026-11-15T08:00:01Z", 25),  # 倒退
        ]
        r = self.run_a(specs)
        self.assertEqual(r["status"], "rejected")
        self.assertIn("严格递增", r["reason"])

    def test_reject_when_no_records_from_start_seq(self):
        insert_records(self.db, [(1, "2026-11-15T08:00:00Z", 25)])
        lim, text = analyzer.parse_limit("1")
        r = analyzer.run_analysis(self.db, 50, lim, text)
        self.assertEqual(r["status"], "rejected")
        self.assertIn("没有任何已采集记录", r["reason"])

    def test_start_seq_reads_only_that_seq_onward(self):
        specs = [(i, f"2026-11-15T08:00:{i:02d}Z", 25) for i in range(1, 6)]
        r = self.run_a(specs, start_seq=3, limit="1")
        p = r["payload"]
        self.assertEqual([pt["seq"] for pt in p["points"]], [3, 4, 5])

    def test_limit_boundary_equality_is_allowed(self):
        # ΔT=0.5, Δt=1s, limit=0.5 -> 相等边界允许（精确）
        specs = [
            (1, "2026-11-15T08:00:00Z", 25.0),
            (2, "2026-11-15T08:00:01Z", 25.5),
        ]
        r = self.run_a(specs, limit="0.5")
        self.assertEqual(r["payload"]["n_removed"], 0)

    def test_microsecond_precision_timestamps(self):
        specs = [
            (1, "2026-11-15T08:00:00.000000Z", 25),
            (2, "2026-11-15T08:00:00.500000Z", 25.1),  # 0.5s 允许 0.1? limit 0.2 => 0.1 允许
        ]
        r = self.run_a(specs, limit="0.2")
        self.assertEqual(r["payload"]["n_removed"], 0)


class BruteForceCrossCheckTest(unittest.TestCase):
    """随机小图：枚举全部子集，暴力求最优与各点在所有最优方案中的真实地位，
    与 DAG 最长路径算法的结论逐一对照。"""

    def _brute(self, specs, limit):
        from datetime import datetime, timezone

        parsed = []
        for seq, ts, temp in specs:
            dt = datetime.fromisoformat(ts[:-1] + "+00:00")
            parsed.append((seq, dt, Fraction(str(temp))))
        lim = Fraction(limit)
        n = len(parsed)

        def feasible(idxs):
            for a, b in zip(idxs, idxs[1:]):
                _, ta, va = parsed[a]
                _, tb, vb = parsed[b]
                delta = tb - ta
                dt_us = (
                    delta.days * 86_400 * 1_000_000
                    + delta.seconds * 1_000_000
                    + delta.microseconds
                )
                dt_s = Fraction(dt_us, 1_000_000)
                if abs(vb - va) > lim * dt_s:
                    return False
            return True

        best_len = 0
        best_sets = []
        for size in range(n, 0, -1):
            for idxs in combinations(range(n), size):
                if feasible(idxs):
                    best_len = size
                    break
            if best_len:
                break
        for idxs in combinations(range(n), best_len):
            if feasible(idxs):
                best_sets.append(frozenset(idxs))
        all_points = frozenset(range(n))
        in_any = frozenset().union(*best_sets) if best_sets else frozenset()
        in_all = frozenset.intersection(*best_sets) if best_sets else frozenset()
        expected = {}
        for i in range(n):
            if i not in in_any:
                expected[i] = "must_remove"
            elif i in in_all:
                expected[i] = "must_keep"
            else:
                expected[i] = "optional"
        return best_len, expected

    def test_random_cross_check(self):
        rng = random.Random(20261115)
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "bf.db")
        try:
            for trial in range(60):
                n = rng.randint(1, 7)
                base = rng.randint(0, 40)
                specs = []
                t = 0
                for i in range(n):
                    t += rng.randint(1, 5)  # 严格递增时刻（整秒）
                    temp = base + rng.randint(-8, 8) * rng.choice([1, 0.5, 0.25])
                    specs.append((i + 1, f"2026-11-15T08:00:{t:02d}Z", temp))
                limit = rng.choice(["0", "0.25", "0.5", "1", "2", "5"])
                # 每轮独立库
                trial_db = os.path.join(tmp.name, f"bf_{trial}.db")
                insert_records(trial_db, specs)
                lim, text = analyzer.parse_limit(limit)
                r = analyzer.run_analysis(trial_db, 1, lim, text)
                self.assertEqual(r["status"], "ok", specs)
                p = r["payload"]
                best_len, expected = self._brute(specs, limit)
                self.assertEqual(p["n_kept"], best_len, (specs, limit))
                got = {pt["pos"]: pt["classification"] for pt in p["points"]}
                self.assertEqual(got, expected, (specs, limit))
                # 示例方案必须可行且最优
                self.assertEqual(len(p["sample_plan_seqs"]), best_len)
        finally:
            tmp.cleanup()


class ParamParseTest(unittest.TestCase):
    def test_limit_rules(self):
        for ok in ["0", "0.0", "1", "0.5", "10.25", "1e3"]:
            val, text = analyzer.parse_limit(ok)
            self.assertIsInstance(val, Fraction)
            self.assertEqual(text, ok)
        for bad in ["", "abc", "-0.1", "NaN", "Infinity", "1,2", "0x1", "+1"]:
            with self.assertRaises(analyzer.AnalysisParamError):
                analyzer.parse_limit(bad)

    def test_start_seq_rules(self):
        self.assertEqual(analyzer.parse_start_seq(" 12 "), 12)
        for bad in ["", "0", "-1", "1.5", "abc"]:
            with self.assertRaises(analyzer.AnalysisParamError):
                analyzer.parse_start_seq(bad)


class StalenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        insert_records(
            self.db,
            [
                (1, "2026-11-15T08:00:00Z", 25),
                (2, "2026-11-15T08:00:01Z", 40),
                (3, "2026-11-15T08:00:02Z", 25),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _analyze(self, start=1, limit="1"):
        lim, text = analyzer.parse_limit(limit)
        return analyzer.run_analysis(self.db, start, lim, text)

    def test_current_then_new_record_marks_stale(self):
        r = self._analyze()
        self.assertEqual(r["state"], analyzer.STATE_CURRENT)
        # 模拟采集到新记录
        insert_records(self.db, [(4, "2026-11-15T08:00:03Z", 25)])
        n = analyzer.mark_current_stale(self.db, "测试：新增记录")
        self.assertEqual(n, 1)
        cur = analyzer.get_current(self.db)
        self.assertIsNone(cur)
        row = analyzer.get_run(self.db, r["run_id"])
        self.assertEqual(row["state"], analyzer.STATE_STALE)
        # 原结论与依据保留
        payload = analyzer.decode_result(row)
        self.assertEqual(payload["n_input"], 3)

    def test_same_param_reanalysis_supersedes_old(self):
        r1 = self._analyze()
        r2 = self._analyze()  # 同参数
        self.assertEqual(r2["state"], analyzer.STATE_CURRENT)
        old = analyzer.get_run(self.db, r1["run_id"])
        self.assertEqual(old["state"], analyzer.STATE_SUPERSEDED)

    def test_param_change_expires_current_via_reject(self):
        self._analyze(limit="1")
        # 换成查不到的起始序号 -> rejected，且参数已变 -> 旧 current 过期
        lim, text = analyzer.parse_limit("1")
        rj = analyzer.run_analysis(self.db, 99, lim, text)
        self.assertEqual(rj["status"], "rejected")
        self.assertIsNone(analyzer.get_current(self.db))

    def test_same_param_rejection_keeps_old_conclusion_intact_as_history(self):
        r1 = self._analyze(limit="1")
        # 新到一条时刻倒退记录（新增记录本身使当前结论按水位规则过期）
        with collector.get_db(self.db) as conn:
            conn.execute(
                "INSERT INTO records(seq, ts, temp, raw, source_path, source_inode, collected_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (5, "2026-11-15T07:59:00Z", 25,
                 raw_line(5, "2026-11-15T07:59:00Z", 25), "active.ndjson", 1005,
                 collector.utc_now_iso()),
            )
        lim, text = analyzer.parse_limit("1")
        rj = analyzer.run_analysis(self.db, 1, lim, text)
        self.assertEqual(rj["status"], "rejected")
        self.assertIn("严格递增", rj["reason"])
        # 拒绝单独立为历史，没有产生新的当前结论
        self.assertIsNone(analyzer.get_current(self.db))
        rejected_row = analyzer.get_run(self.db, rj["run_id"])
        self.assertEqual(rejected_row["state"], analyzer.STATE_REJECTED)
        # 旧结论与依据原样保留（未被拒绝动作改写或取代）
        old = analyzer.get_run(self.db, r1["run_id"])
        self.assertEqual(old["state"], analyzer.STATE_STALE)
        self.assertEqual(analyzer.decode_result(old)["n_input"], 3)

    def test_change_during_analysis_becomes_stale_history_only(self):
        # 测试钩子：快照读取后、发布前模拟采集入库
        def hook():
            insert_records(self.db, [(4, "2026-11-15T08:00:03Z", 25)])

        lim, text = analyzer.parse_limit("1")
        r = analyzer.run_analysis(self.db, 1, lim, text, after_snapshot=hook)
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["changed_during"])
        self.assertEqual(r["state"], analyzer.STATE_STALE_HISTORY)
        # 没有当前结论
        self.assertIsNone(analyzer.get_current(self.db))
        row = analyzer.get_run(self.db, r["run_id"])
        payload = analyzer.decode_result(row)
        self.assertEqual(payload["n_input"], 3)  # 依据仍是旧快照
        self.assertTrue(payload.get("stale_events"))

    def test_reconcile_current_repairs_state(self):
        r = self._analyze()
        # 直接手工把状态改回 current 并在库中加记录，模拟漏标
        insert_records(self.db, [(4, "2026-11-15T08:00:03Z", 25)])
        analyzer.reconcile_current(self.db)
        self.assertIsNone(analyzer.get_current(self.db))
        self.assertEqual(analyzer.get_run(self.db, r["run_id"])["state"],
                         analyzer.STATE_STALE)

    def test_param_versions_are_deduplicated(self):
        self._analyze(limit="1")
        self._analyze(limit="1")
        with collector.get_db(self.db) as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM analysis_params WHERE start_seq=1 AND limit_text='1'"
            ).fetchone()["c"]
        self.assertEqual(n, 1)
        lim, text = analyzer.parse_limit("2")
        analyzer.run_analysis(self.db, 1, lim, text)
        with collector.get_db(self.db) as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM analysis_params").fetchone()["c"]
        self.assertEqual(n, 2)

    def test_watermark_remembers_collection_state(self):
        r = self._analyze()
        row = analyzer.get_run(self.db, r["run_id"])
        wm = json.loads(row["watermark_json"])
        self.assertEqual(wm["record_count"], 3)
        self.assertEqual(wm["max_seq"], 3)
        self.assertIn("file_states", wm)


class WebFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "data")
        self.db = os.path.join(self.tmp.name, "web.db")
        os.makedirs(self.data, exist_ok=True)
        os.environ["DATA_DIR"] = self.data
        os.environ["DB_PATH"] = self.db
        os.environ["ENABLE_DEMO_RESET"] = "1"
        for mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
            del sys.modules[mod]
        import app as flask_app  # noqa: WPS433
        self.app_module = flask_app
        self.client = flask_app.create_app().test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _post_collect(self, lines):
        active = os.path.join(self.data, collector.ACTIVE_NAME)
        with open(active, "a", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln + "\n")
        return self.client.post("/collect", follow_redirects=True)

    def test_analysis_pages_and_full_flow(self):
        # 采集 4 条，其中一个跳点
        lines = [
            raw_line(1, "2026-11-15T08:00:00Z", 25),
            raw_line(2, "2026-11-15T08:00:01Z", 40),
            raw_line(3, "2026-11-15T08:00:02Z", 25),
            raw_line(4, "2026-11-15T08:00:03Z", 25),
        ]
        self._post_collect(lines)

        # 分析页可打开
        page = self.client.get("/analysis").get_data(as_text=True)
        self.assertIn("探头跳点分析", page)

        # 执行分析
        resp = self.client.post(
            "/analysis/run", data={"start_seq": "1", "limit": "1"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        detail_url = resp.headers["Location"]
        detail = self.client.get(detail_url).get_data(as_text=True)
        self.assertIn("最少剔除", detail)
        self.assertIn("必留", detail)
        self.assertIn("必剔", detail)
        self.assertIn("active.ndjson", detail)  # 记录来源
        self.assertIn("采集水位", detail)

        # 分析页显示当前结论
        listing = self.client.get("/analysis").get_data(as_text=True)
        self.assertIn("当前结论", listing)

        # 再采集一条 -> 当前结论过期
        self._post_collect([raw_line(5, "2026-11-15T08:00:04Z", 25)])
        listing = self.client.get("/analysis").get_data(as_text=True)
        self.assertIn("已过期", listing)
        # 历史详情仍保留原结论（n_input=4）
        old_detail = self.client.get(detail_url).get_data(as_text=True)
        self.assertIn("本批记录", old_detail)

    def test_bad_params_flash_without_persisting(self):
        resp = self.client.post(
            "/analysis/run", data={"start_seq": "0", "limit": "-1"},
            follow_redirects=True,
        )
        text = resp.get_data(as_text=True)
        self.assertIn("参数不合法", text)
        # 没有产生任何分析记录
        self.assertEqual(analyzer.list_runs(self.db), [])

    def test_rejected_analysis_listed_in_history(self):
        self._post_collect([
            raw_line(1, "2026-11-15T08:00:00Z", 25),
            raw_line(2, "2026-11-15T08:00:00Z", 25),
        ])
        self.client.post("/analysis/run", data={"start_seq": "1", "limit": "1"})
        listing = self.client.get("/analysis").get_data(as_text=True)
        self.assertIn("已拒绝", listing)
        self.assertIn("严格递增", listing)

    def test_concurrent_collection_during_analysis(self):
        # 先入库 3 条
        self._post_collect([
            raw_line(1, "2026-11-15T08:00:00Z", 25),
            raw_line(2, "2026-11-15T08:00:01Z", 26),
            raw_line(3, "2026-11-15T08:00:02Z", 27),
        ])
        # 用分析器钩子在“分析期间”完成一轮真实采集
        active = os.path.join(self.data, collector.ACTIVE_NAME)
        with open(active, "a", encoding="utf-8") as fh:
            fh.write(raw_line(4, "2026-11-15T08:00:03Z", 28) + "\n")

        def hook():
            r = collector.collect_once(self.data, self.db)
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["records_inserted"], 1)

        lim, text = analyzer.parse_limit("1")
        r = analyzer.run_analysis(self.db, 1, lim, text, after_snapshot=hook)
        self.assertEqual(r["state"], analyzer.STATE_STALE_HISTORY)
        # 采集进度与原始记录未受影响
        rows, total = collector.list_records(self.db, limit=100)
        self.assertEqual(total, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
