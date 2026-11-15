# -*- coding: utf-8 -*-
"""发酵罐温度日志质控 Web 入口（Flask + SQLite）。"""

from __future__ import annotations

import csv
import io
import json
import os
import threading

from flask import Flask, Response, flash, redirect, render_template, request, url_for

from . import analyzer, collector

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.environ.get("DB_PATH", "/instance/fermenter.db")
ENABLE_DEMO_RESET = os.environ.get("ENABLE_DEMO_RESET", "0") == "1"

_collect_lock = threading.Lock()


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "fermenter-demo-secret-key")
    os.makedirs(DATA_DIR, exist_ok=True)
    collector.init_db(DB_PATH)
    analyzer.init_analysis_db(DB_PATH)

    @app.get("/")
    def index():
        try:
            page = max(int(request.args.get("page", 1)), 1)
        except (TypeError, ValueError):
            page = 1
        page_size = 25
        rows, total = collector.list_records(
            DB_PATH, limit=page_size, offset=(page - 1) * page_size
        )
        runs = collector.list_runs(DB_PATH)
        states = collector.list_file_states(DB_PATH)
        files = collector._scan_files(DATA_DIR)  # noqa: SLF001 - 展示用
        return render_template(
            "index.html",
            rows=rows,
            total=total,
            page=page,
            page_size=page_size,
            runs=runs,
            states=states,
            files=files,
            data_dir=DATA_DIR,
            demo_reset=ENABLE_DEMO_RESET,
        )

    @app.post("/collect")
    def collect():
        if not _collect_lock.acquire(blocking=False):
            flash("已有一轮采集正在执行，请稍后刷新查看结果。", "warning")
            return redirect(url_for("index"))
        try:
            result = collector.collect_once(DATA_DIR, DB_PATH)
        finally:
            _collect_lock.release()
        if result["status"] == "ok":
            # 新记录入库后，既有当前分析结论依据过期，标记为 stale（历史结论原样保留）
            if result["records_inserted"] > 0:
                analyzer.mark_current_stale(
                    DB_PATH,
                    "采集到新记录（新增 {} 条，最大序号变化），原结论依据已过期。".format(
                        result["records_inserted"]
                    ),
                )
            flash(
                "采集成功：新增 {ins} 条，重复跳过 {dup} 条，完整行 {seen} 行，"
                "半行 {half} 字节留待下次。".format(
                    ins=result["records_inserted"],
                    dup=result["duplicates"],
                    seen=result["lines_seen"],
                    half=result["half_line_bytes"],
                ),
                "success",
            )
        else:
            level = "danger" if result["status"] == "rejected" else "warning"
            flash(
                "整轮{}（已入库记录与采集进度不变）：{}".format(
                    "拒绝" if result["status"] == "rejected" else "出错",
                    result["error"],
                ),
                level,
            )
        return redirect(url_for("index"))

    @app.get("/records.csv")
    def export_csv():
        rows, _ = collector.list_records(DB_PATH, limit=10_000_000, offset=0)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["seq", "ts_utc", "temp", "source_file", "inode", "collected_at", "raw"])
        for r in rows:
            writer.writerow(
                [r["seq"], r["ts"], r["temp"], r["source_path"], r["source_inode"], r["collected_at"], r["raw"]]
            )
        # utf-8-sig 便于 Excel 直接打开中文不乱码；rows 按 seq 倒序，导出时转正序
        text = buf.getvalue()
        lines = text.splitlines()
        header, body = lines[0], lines[1:]
        body.reverse()
        payload = "﻿" + header + "\r\n" + "\r\n".join(body) + ("\r\n" if body else "")
        return Response(
            payload,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=temperature_records.csv"},
        )

    @app.post("/demo/reset")
    def demo_reset():
        if not ENABLE_DEMO_RESET:
            flash("演示重置未开启（需 ENABLE_DEMO_RESET=1）。", "warning")
            return redirect(url_for("index"))
        collector.reset_all(DB_PATH, DATA_DIR)
        analyzer.mark_current_stale(DB_PATH, "演示重置：原始记录与采集进度已清空，原结论依据失效。")
        flash("已清空数据库与挂载目录中的日志文件。", "success")
        return redirect(url_for("index"))

    # ------------------------------------------------------------------ #
    # 跳点分析
    # ------------------------------------------------------------------ #
    @app.get("/analysis")
    def analysis_page():
        analyzer.reconcile_current(DB_PATH)
        current = analyzer.get_current(DB_PATH)
        current_payload = analyzer.decode_result(current) if current else None
        history = analyzer.list_runs(DB_PATH, limit=30)
        history_rows = []
        for r in history:
            history_rows.append(
                {
                    "id": r["id"],
                    "start_seq": r["start_seq"],
                    "limit_text": r["limit_text"],
                    "status": r["status"],
                    "state": r["state"],
                    "reason": r["reason"],
                    "n_input": r["n_input"],
                    "n_kept": r["n_kept"],
                    "n_removed": r["n_removed"],
                    "created_at": r["created_at"],
                }
            )
        return render_template(
            "analysis.html",
            current=current,
            current_payload=current_payload,
            history=history_rows,
            form=request.args,
        )

    @app.post("/analysis/run")
    def analysis_run():
        try:
            start_seq = analyzer.parse_start_seq(request.form.get("start_seq", ""))
            limit, limit_text = analyzer.parse_limit(request.form.get("limit", ""))
        except analyzer.AnalysisParamError as exc:
            flash(f"参数不合法，未执行分析：{exc}", "danger")
            return redirect(url_for("analysis_page"))
        result = analyzer.run_analysis(DB_PATH, start_seq, limit, limit_text)
        if result["status"] == "rejected":
            flash(f"分析被拒绝（已留存为历史）：{result['reason']}", "danger")
        elif result.get("changed_during"):
            flash(
                "分析期间采集到新记录，本次结论只存为历史（未成为当前结论）；"
                "可重新分析以获得当前结论。",
                "warning",
            )
        else:
            p = result["payload"]
            flash(
                "分析完成：本批 {n} 条，最优方案保留 {k} 条、剔除 {r} 条"
                "（必留 {mk} / 可选 {op} / 必剔 {mr}）。".format(
                    n=p["n_input"],
                    k=p["n_kept"],
                    r=p["n_removed"],
                    mk=result["counts"]["must_keep"],
                    op=result["counts"]["optional"],
                    mr=result["counts"]["must_remove"],
                ),
                "success",
            )
        return redirect(url_for("analysis_detail", run_id=result["run_id"]))

    @app.get("/analysis/runs/<int:run_id>")
    def analysis_detail(run_id: int):
        analyzer.reconcile_current(DB_PATH)
        row = analyzer.get_run(DB_PATH, run_id)
        if row is None:
            flash("找不到该分析记录。", "warning")
            return redirect(url_for("analysis_page"))
        payload = analyzer.decode_result(row)
        watermark = json.loads(row["watermark_json"]) if row["watermark_json"] else None
        return render_template(
            "analysis_detail.html", row=row, payload=payload, watermark=watermark
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
