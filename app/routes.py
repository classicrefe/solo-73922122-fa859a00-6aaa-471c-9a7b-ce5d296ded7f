"""Web pages, CSV download and JSON API."""
from __future__ import annotations

import csv
import io
import json
import os

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import catalog, graph
from .db import active_version, get_db

bp = Blueprint("main", __name__)


# --------------------------------------------------------------------------- #
# View-model assembly
# --------------------------------------------------------------------------- #
def _load_state(conn) -> dict:
    version = active_version(conn)
    version_row = conn.execute(
        "SELECT * FROM index_versions WHERE version = ?", (version,)
    ).fetchone()

    serving_rows = conn.execute(
        """
        SELECT plate_number AS number, title, refs_text
          FROM index_entries
         WHERE version = ?
         ORDER BY plate_number
        """,
        (version,),
    ).fetchall()
    serving = [
        {
            "number": r["number"],
            "title": r["title"],
            "refs": [x for x in r["refs_text"].split(";") if x],
        }
        for r in serving_rows
    ]

    suspended_rows = conn.execute(
        """
        SELECT p.number, p.title, s.blocker, s.witness, s.reason, s.updated_at,
               (SELECT GROUP_CONCAT(r2.plate_number)
                  FROM refs r2 WHERE r2.ref_number = p.number) AS referrers
          FROM plate_status s
          JOIN plates p ON p.number = s.plate_number
         WHERE s.state = 'suspended'
         ORDER BY p.number
        """
    ).fetchall()
    suspended = []
    for r in suspended_rows:
        try:
            witness = json.loads(r["witness"] or "[]")
        except json.JSONDecodeError:
            witness = []
        suspended.append(
            {
                "number": r["number"],
                "title": r["title"],
                "blocker": r["blocker"],
                "witness": witness,
                "reason": r["reason"],
                "updated_at": r["updated_at"],
                "referrers": sorted((r["referrers"] or "").split(",")) if r["referrers"] else [],
            }
        )

    missing_rows = conn.execute(
        """
        SELECT r.ref_number AS number,
               COUNT(DISTINCT r.plate_number) AS referrer_count,
               GROUP_CONCAT(DISTINCT r.plate_number) AS referrers
          FROM refs r
          LEFT JOIN plates p ON p.number = r.ref_number
         WHERE p.number IS NULL
         GROUP BY r.ref_number
         ORDER BY r.ref_number
        """
    ).fetchall()
    missing = [
        {
            "number": r["number"],
            "referrer_count": r["referrer_count"],
            "referrers": sorted((r["referrers"] or "").split(",")),
        }
        for r in missing_rows
    ]

    total = conn.execute("SELECT COUNT(*) AS c FROM plates").fetchone()["c"]
    latest_change = conn.execute(
        "SELECT * FROM change_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    recent_versions = conn.execute(
        "SELECT * FROM index_versions ORDER BY version DESC LIMIT 8"
    ).fetchall()

    return {
        "active_version": version,
        "version_row": version_row,
        "serving": serving,
        "suspended": suspended,
        "missing": missing,
        "stats": {
            "total": total,
            "serving": len(serving),
            "suspended": len(suspended),
            "missing": len(missing),
        },
        "latest_change": latest_change,
        "recent_versions": recent_versions,
    }


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@bp.route("/")
def dashboard():
    conn = get_db()
    state = _load_state(conn)
    return render_template("index.html", **state)


@bp.route("/upload", methods=["GET"])
def upload_page():
    return render_template("upload.html", parse_result=None)


@bp.route("/plate")
def plate_detail():
    number = request.args.get("n", "").strip()
    if not number:
        return redirect(url_for("main.dashboard"))
    conn = get_db()
    plate = conn.execute("SELECT * FROM plates WHERE number = ?", (number,)).fetchone()

    status = None
    outgoing: list[dict] = []
    incoming: list[dict] = []
    missing_view = None

    if plate is not None:
        status = conn.execute(
            "SELECT * FROM plate_status WHERE plate_number = ?", (number,)
        ).fetchone()
        if status is not None:
            status = dict(status)
            status["witness_list"] = json.loads(status.get("witness") or "[]")
        for r in conn.execute(
            "SELECT ref_number FROM refs WHERE plate_number = ? ORDER BY position",
            (number,),
        ):
            ref = r["ref_number"]
            exists = conn.execute(
                "SELECT 1 FROM plates WHERE number = ?", (ref,)
            ).fetchone()
            st = conn.execute(
                "SELECT state FROM plate_status WHERE plate_number = ?", (ref,)
            ).fetchone()
            outgoing.append(
                {
                    "number": ref,
                    "exists": bool(exists),
                    "state": st["state"] if st else ("missing" if not exists else "?"),
                }
            )
        for r in conn.execute(
            "SELECT plate_number FROM refs WHERE ref_number = ? ORDER BY plate_number",
            (number,),
        ):
            src = r["plate_number"]
            st = conn.execute(
                "SELECT state FROM plate_status WHERE plate_number = ?", (src,)
            ).fetchone()
            incoming.append({"number": src, "state": st["state"] if st else "?"})
    else:
        # A missing reference number: show who points at it.
        referrers = [
            r["plate_number"]
            for r in conn.execute(
                "SELECT DISTINCT plate_number FROM refs WHERE ref_number = ? "
                "ORDER BY plate_number",
                (number,),
            )
        ]
        if not referrers:
            flash(f"图版号 {number} 既不在库中，也没有被任何图版引用。", "warning")
        missing_view = {"number": number, "referrers": referrers}

    return render_template(
        "plate.html",
        number=number,
        plate=plate,
        status=status,
        outgoing=outgoing,
        incoming=incoming,
        missing_view=missing_view,
    )


# --------------------------------------------------------------------------- #
# Upload / replace
# --------------------------------------------------------------------------- #
def _read_upload_payload() -> tuple[bytes | None, str | None, str]:
    """Return (raw_bytes, source_label, pasted_text)."""
    upload = request.files.get("csv_file")
    if upload is not None and upload.filename:
        return upload.read(), f"CSV 文件 {upload.filename}", ""
    text = request.form.get("csv_text", "")
    if text.strip():
        return text.encode("utf-8"), "网页粘贴 CSV", text
    return None, None, ""


@bp.route("/upload", methods=["POST"])
def upload_submit():
    data, source, _text = _read_upload_payload()
    if data is None:
        flash("请选择 CSV 文件，或把 CSV 内容粘贴到文本框。", "error")
        return render_template("upload.html", parse_result=None), 400

    parsed = graph.parse_catalog(data)
    if not parsed.ok:
        return (
            render_template("upload.html", parse_result=parsed, source=source),
            422,
        )

    conn = get_db()
    try:
        report = catalog.apply_changes(
            conn,
            upserts=parsed.rows,
            source=source or "CSV 上传",
            action="upload",
        )
    except Exception as exc:  # noqa: BLE001 - show failure, old index stays live
        current_app.logger.exception("catalog upload failed")
        flash(f"入库失败，已整体回滚，仍供原索引。原因：{exc}", "error")
        return (
            render_template("upload.html", parse_result=parsed, source=source),
            500,
        )

    for warning in parsed.warnings:
        flash(warning, "warning")
    flash(
        f"已整号替换/补录 {len(parsed.rows)} 行。{report.detail}",
        "success",
    )
    return redirect(url_for("main.dashboard"))


@bp.route("/demo", methods=["POST"])
def load_demo():
    demo_path = os.path.join(
        current_app.root_path, "static", "sample_catalog.csv"
    )
    with open(demo_path, "rb") as fh:
        data = fh.read()
    parsed = graph.parse_catalog(data)
    conn = get_db()
    report = catalog.apply_changes(
        conn,
        upserts=parsed.rows,
        source="内置演示数据",
        action="upload",
    )
    flash("已载入内置演示数据。" + report.detail, "success")
    return redirect(url_for("main.dashboard"))


# --------------------------------------------------------------------------- #
# Withdraw
# --------------------------------------------------------------------------- #
@bp.route("/withdraw", methods=["GET", "POST"])
def withdraw_submit():
    if request.method == "GET":
        return redirect(url_for("main.dashboard") + "#withdraw")
    numbers: list[str] = []
    single = request.form.get("number", "").strip()
    if single:
        numbers.append(single)
    numbers.extend(graph.parse_numbers(request.form.get("numbers", "")))

    upload = request.files.get("withdraw_file")
    if upload is not None and upload.filename:
        raw_text, _enc = graph.decode_catalog_bytes(upload.read())
        parsed = graph.parse_catalog(graph.strip_comment_lines(raw_text))
        if parsed.ok:
            numbers.extend(row.number for row in parsed.rows)
        else:
            flash(
                "撤下名单文件解析失败："
                + "；".join(f"第{e.line_no}行 {e.message}" for e in parsed.errors),
                "error",
            )
            return redirect(url_for("main.dashboard"))

    numbers = list(dict.fromkeys(n for n in numbers if n))
    if not numbers:
        flash("没有填写要撤下的图版号。", "warning")
        return redirect(url_for("main.dashboard"))

    conn = get_db()
    existing = {
        r[0]
        for r in conn.execute(
            f"SELECT number FROM plates WHERE number IN "
            f"({','.join('?' for _ in numbers)})",
            tuple(numbers),
        )
    }
    missing_numbers = [n for n in numbers if n not in existing]
    try:
        report = catalog.apply_changes(
            conn,
            withdrawals=numbers,
            source="按号撤下",
            action="withdraw",
        )
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("withdraw failed")
        flash(f"撤下失败，已整体回滚，仍供原索引。原因：{exc}", "error")
        return redirect(url_for("main.dashboard"))

    if missing_numbers:
        flash(
            "以下号本来就不在库中，已按撤下名单参与连通重算："
            + ", ".join(missing_numbers),
            "warning",
        )
    flash(
        f"已撤下 {len(report.withdrawals)} 个号；直接/间接引用它们的图版已同步退出供数。"
        + report.detail,
        "success",
    )
    return redirect(url_for("main.dashboard"))


# --------------------------------------------------------------------------- #
# Published-index CSV download — reads the active published version directly
# --------------------------------------------------------------------------- #
def _csv_response(conn) -> Response:
    version = active_version(conn)
    rows = conn.execute(
        """
        SELECT plate_number, title, refs_text
          FROM index_entries
         WHERE version = ?
         ORDER BY plate_number
        """,
        (version,),
    ).fetchall()

    buffer = io.StringIO()
    buffer.write("﻿")  # UTF-8 BOM so Excel opens Chinese correctly
    writer = csv.writer(buffer)
    writer.writerow(["图版号", "题名", "引用号"])
    for row in rows:
        writer.writerow([row["plate_number"], row["title"], row["refs_text"]])

    filename = f"published_index_v{version}.csv"
    return Response(
        buffer.getvalue(),
        content_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "X-Index-Version": str(version),
        },
    )


@bp.route("/download.csv")
def download_csv():
    return _csv_response(get_db())


@bp.route("/download")
def download_alias():
    return _csv_response(get_db())


@bp.route("/index.csv")
def index_csv_alias():
    return _csv_response(get_db())


# --------------------------------------------------------------------------- #
# JSON / health endpoints (handy for ops and automated checks)
# --------------------------------------------------------------------------- #
@bp.route("/api/state")
def api_state():
    state = _load_state(get_db())
    latest = state["latest_change"]
    return {
        "active_version": state["active_version"],
        "stats": state["stats"],
        "serving": state["serving"],
        "suspended": state["suspended"],
        "missing": state["missing"],
        "latest_change": (
            {
                "action": latest["action"],
                "changed": json.loads(latest["changed_json"]),
                "scope": json.loads(latest["scope_json"]),
                "detail": latest["detail"],
                "created_at": latest["created_at"],
            }
            if latest is not None
            else None
        ),
    }


@bp.route("/api/index")
def api_index():
    conn = get_db()
    version = active_version(conn)
    rows = conn.execute(
        "SELECT plate_number, title, refs_text FROM index_entries "
        "WHERE version = ? ORDER BY plate_number",
        (version,),
    ).fetchall()
    return {
        "version": version,
        "entries": [
            {"number": r["plate_number"], "title": r["title"],
             "refs": [x for x in r["refs_text"].split(";") if x]}
            for r in rows
        ],
    }


@bp.route("/healthz")
def healthz():
    conn = get_db()
    version = active_version(conn)
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "active_index_version": version}
