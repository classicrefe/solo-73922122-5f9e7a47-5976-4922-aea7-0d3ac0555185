"""Flask app factory for the survey coverage review board."""

from __future__ import annotations

import os
import secrets

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import db, importer


def _decode_csv(raw: bytes) -> str | None:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("gb18030")
        except UnicodeDecodeError:
            return None


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        CSRF_ENABLED=True,
    )
    if config:
        app.config.update(config)

    db.init_db(app.config.get("DATABASE"))

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(24)
            session["csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def csrf_protect():
        if not app.config.get("CSRF_ENABLED", True):
            return None
        if request.method != "POST":
            return None
        expected = session.get("csrf_token")
        supplied = request.form.get("csrf_token", "")
        if not expected or not secrets.compare_digest(expected, supplied):
            abort(400, description="CSRF 校验失败，请刷新页面后重试")
        return None

    @app.get("/")
    def index():
        conn = db.open_db(app.config.get("DATABASE"))
        try:
            grids = db.grid_summary(conn)
            stats = {
                "grids": len(grids),
                "needs_more": sum(1 for g in grids if g["needs_more"]),
                "usable_nights": sum(g["usable_nights"] for g in grids),
                "imports": [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM imports ORDER BY id DESC LIMIT 5"
                    ).fetchall()
                ],
            }
            return render_template("index.html", grids=grids, stats=stats)
        finally:
            conn.close()

    @app.post("/import")
    def upload():
        upload_file = request.files.get("csv")
        if upload_file is None or not upload_file.filename:
            flash("请选择 CSV 文件", "error")
            return redirect(url_for("index"))
        raw = upload_file.read()
        text = _decode_csv(raw)
        if text is None:
            flash("无法解码 CSV，请使用 UTF-8 或 GB18030", "error")
            return redirect(url_for("index"))
        parsed = importer.parse_csv(text)
        if parsed.errors:
            for message in parsed.errors[:20]:
                flash(message, "error")
            if len(parsed.errors) > 20:
                flash(f"另有 {len(parsed.errors) - 20} 条错误未显示", "error")
            return redirect(url_for("index"))
        conn = db.open_db(app.config.get("DATABASE"))
        try:
            try:
                outcome = db.import_rows(conn, parsed.rows, upload_file.filename)
            except db.ImportConflict as exc:
                flash(f"导入被拒绝，数据未改动：{exc}", "error")
                return redirect(url_for("index"))
        finally:
            conn.close()
        flash(
            f"导入完成：新增曝光 {outcome['inserted']} 次，新增覆盖关系 {outcome['linked']} 条，"
            f"跳过重复 {outcome['skipped']} 条",
            "success",
        )
        return redirect(url_for("index"))

    @app.get("/grids/<grid_id>")
    def grid_page(grid_id: str):
        conn = db.open_db(app.config.get("DATABASE"))
        try:
            detail = db.grid_detail(conn, grid_id)
            if detail is None:
                flash(f"没有网格 {grid_id} 的曝光记录", "error")
                return redirect(url_for("index"))
            comparison = None
            target = request.args.get("target", "").strip()
            first = request.args.get("first", "").strip()
            second = request.args.get("second", "").strip()
            if target or first or second:
                comparison = db.compare_colour(conn, grid_id, target, first, second)
            return render_template(
                "grid.html", grid=detail, comparison=comparison
            )
        finally:
            conn.close()

    @app.post("/grids/<grid_id>/photometry")
    def upload_photometry(grid_id: str):
        upload_file = request.files.get("csv")
        if upload_file is None or not upload_file.filename:
            flash("请选择测光 CSV 文件", "error")
            return redirect(url_for("grid_page", grid_id=grid_id))
        text = _decode_csv(upload_file.read())
        if text is None:
            flash("无法解码 CSV，请使用 UTF-8 或 GB18030", "error")
            return redirect(url_for("grid_page", grid_id=grid_id))
        parsed = importer.parse_photometry_csv(text)
        if parsed.errors:
            for message in parsed.errors[:20]:
                flash(message, "error")
            if len(parsed.errors) > 20:
                flash(f"另有 {len(parsed.errors) - 20} 条错误未显示", "error")
            return redirect(url_for("grid_page", grid_id=grid_id))
        conn = db.open_db(app.config.get("DATABASE"))
        try:
            try:
                outcome = db.import_photometry(conn, parsed.rows, upload_file.filename)
            except db.PhotometryConflict as exc:
                flash(f"测光导入被拒绝，已入库的星等未改动：{exc}", "error")
                return redirect(url_for("grid_page", grid_id=grid_id))
        finally:
            conn.close()
        flash(
            f"测光导入完成：新增 {outcome['inserted']} 行，跳过同键同值 {outcome['skipped']} 行",
            "success",
        )
        return redirect(url_for("grid_page", grid_id=grid_id))

    @app.post("/exposures/<int:exposure_id>/validity")
    def toggle(exposure_id: int):
        desired = request.form.get("valid")
        if desired not in ("0", "1"):
            flash("无效的请求参数", "error")
            return redirect(request.form.get("next") or url_for("index"))
        conn = db.open_db(app.config.get("DATABASE"))
        try:
            try:
                result = db.set_valid(conn, exposure_id, desired == "1")
            except LookupError:
                flash(f"曝光 {exposure_id} 不存在", "error")
                return redirect(url_for("index"))
        finally:
            conn.close()
        cov_rows = result["coverage"]
        state = "有效" if desired == "1" else "已剔除"
        if result["changed"]:
            flash(f"曝光 {exposure_id} 已改为{state}", "success")
        else:
            flash(f"曝光 {exposure_id} 本来已是{state}，本次未改动", "info")
        for cov in cov_rows:
            if not cov:
                continue
            if cov.get("status") == "paired":
                summary = f"可用，g={cov['g_no']} r={cov['r_no']} 相隔 {cov['pair_seconds']} 秒"
            else:
                summary = f"未配对（{cov.get('reason')}）"
            flash(f"网格 {cov['grid_id']} 观测夜 {cov['night']} 重核：{summary}", "info")
        if result["changed"] and len(cov_rows) > 1:
            flash(f"这次改动共影响 {len(cov_rows)} 个网格在当夜的配对", "info")
        target = request.form.get("next")
        if target and target.startswith("/") and not target.startswith("//"):
            return redirect(target)
        return redirect(url_for("index"))

    @app.get("/api/grids")
    def api_grids():
        conn = db.open_db(app.config.get("DATABASE"))
        try:
            grids = db.grid_summary(conn)
            return {
                "grids": [
                    {
                        "grid_id": g["grid_id"],
                        "usable_nights": g["usable_nights"],
                        "needs_more": g["needs_more"],
                        "nights": [
                            {
                                "night": n["night"],
                                "usable": n["usable"],
                                "g_exposure_no": n["g_no"],
                                "r_exposure_no": n["r_no"],
                                "seconds": n["seconds"],
                                "reason": n["reason"],
                            }
                            for n in g["nights"]
                        ],
                    }
                    for g in grids
                ]
            }
        finally:
            conn.close()

    @app.get("/healthz")
    def healthz():
        conn = db.open_db(app.config.get("DATABASE"))
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return {"ok": True}

    return app
