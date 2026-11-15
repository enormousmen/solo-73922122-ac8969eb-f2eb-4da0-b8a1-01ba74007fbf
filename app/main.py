"""FastAPI 应用：报告提交、隔离与判读 API。

- POST /reports                          提交（或重传）某站点某采样日的完整日报
- GET  /reports/{station}/{date}         读取现行（或 ?revision= 指定）报告
- GET  /reports/{station}/{date}/revisions  列出该日全部已提交修订
- POST /reports/{report_id}/quarantine   按 report_id 隔离现行报告（记录原因）
- GET  /interpretation                   七日窗口判读
"""

from __future__ import annotations

import datetime as dt
import math
import os
import sqlite3
from typing import Annotated, Any, Optional

from fastapi import FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from .db import Database, rollback_quietly
from .logic import (
    STATUS_ZH,
    WINDOW_DAYS,
    canonical_content,
    content_hash,
    interpret_window,
)
from .models import (
    Name,
    NonNegFinite,
    QuarantineRequest,
    QuarantineResponse,
    ReportSubmission,
    SubmitResponse,
)

DESCRIPTION = """\
流域监测报告提交与判读 API。

**提交规则**
- 实验室按站点+采样日提交包含全部污染物的完整日报，修订号递增（正整数）。
- 更高修订**完整替换**当天报告，删除的项目不沿用旧值。
- 同修订同内容重传：幂等复用（返回 200 与原 report_id）。
- 旧修订、或同修订但内容不同：拒绝（409）。
- 提交整体落库或整体回滚，并发/重试不会留下半份报告；历史修订始终可读取。

**判读规则**
- 窗口为结束日（含）向前连续 7 天，取**同一已提交时点**各日的现行日报。
- 缺日或缺项目 → `insufficient_data`（资料不足）。
- “未检出”按半开区间 [0, 检出限) 参与均值；实测值即点值。
- 均值必定 ≤ 限值 → `compliant`（达标）；必定 > 限值 → `exceeds`（超标）；
  其余 → `pending_review`（待复核）。响应含所用修订号。

**隔离规则**
- 仪器故障复核时，可按 `report_id` 隔离**现行**报告并记录原因。
- 同一报告重复隔离：幂等返回原记录（即使之后已有新修订）；
  从未隔离的旧修订不得新隔离（409）。
- 判读遇到被隔离的现行报告：该日按资料不足处理，列出隔离日期与
  `report_id`，**不回退**旧修订；补交更高修订后该日重新参与判读。
- 历史修订的隔离原因始终可在报告详情中查看；隔离、补报与判读并发时，
  一次判读所用修订、隔离状态与结论来自同一已提交快照。
"""


def _item_json(row: sqlite3.Row) -> dict[str, Any]:
    if row["type"] == "value":
        return {"type": "value", "pollutant": row["pollutant"], "value": row["value"]}
    return {
        "type": "nd",
        "pollutant": row["pollutant"],
        "detection_limit": row["detection_limit"],
    }


def _json_safe(obj: Any) -> Any:
    """把校验错误中的非有限浮点（inf/nan 输入值）转为字符串，保证可 JSON 序列化。"""
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def create_app(db_path: Optional[str] = None) -> FastAPI:
    db = Database(db_path or os.environ.get("DB_PATH", "app.db"))
    app = FastAPI(
        title="流域监测报告提交与判读 API",
        version="1.0.0",
        description=DESCRIPTION,
    )
    app.state.db = db

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"detail": _json_safe(exc.errors())}),
        )

    # ---------------- 系统 ----------------

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---------------- 报告提交 ----------------

    @app.post(
        "/reports",
        status_code=201,
        response_model=SubmitResponse,
        tags=["reports"],
        responses={
            200: {"description": "同修订同内容重传，幂等复用"},
            409: {"description": "旧修订或同修订异内容，拒收"},
            422: {"description": "请求体校验失败"},
            503: {"description": "数据库繁忙，可重试"},
        },
    )
    def submit_report(payload: ReportSubmission, response: Response) -> SubmitResponse:
        items = [it.model_dump() for it in payload.items]
        canon = canonical_content(items)
        digest = content_hash(items)
        date_s = payload.sampling_date.isoformat()

        conn = db.connect()
        try:
            # BEGIN IMMEDIATE 立即取得写锁：检查+插入在同一写事务内串行执行
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, revision, content_hash FROM reports "
                "WHERE station=? AND sampling_date=? ORDER BY revision DESC LIMIT 1",
                (payload.station, date_s),
            ).fetchone()

            if row is not None and payload.revision < row["revision"]:
                conn.execute("ROLLBACK")
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "stale_revision",
                        "message": f"revision {payload.revision} 低于当前修订 {row['revision']}，拒收",
                        "current_revision": row["revision"],
                    },
                )

            if row is not None and payload.revision == row["revision"]:
                if row["content_hash"] == digest:
                    conn.execute("COMMIT")  # 幂等重传：复用已有结果
                    response.status_code = 200
                    return SubmitResponse(
                        report_id=row["id"],
                        station=payload.station,
                        sampling_date=date_s,
                        revision=payload.revision,
                        status="reused",
                        superseded_revision=None,
                    )
                conn.execute("ROLLBACK")
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "revision_content_mismatch",
                        "message": f"修订 {payload.revision} 已存在且内容不同，拒收",
                        "current_revision": row["revision"],
                    },
                )

            now = dt.datetime.now(dt.timezone.utc).isoformat()
            cur = conn.execute(
                "INSERT INTO reports (station, sampling_date, revision, content_hash, "
                "content_json, submitted_at) VALUES (?,?,?,?,?,?)",
                (payload.station, date_s, payload.revision, digest, canon, now),
            )
            report_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO report_items (report_id, pollutant, type, value, "
                "detection_limit) VALUES (?,?,?,?,?)",
                [
                    (
                        report_id,
                        it["pollutant"],
                        it["type"],
                        it.get("value"),
                        it.get("detection_limit"),
                    )
                    for it in items
                ],
            )
            conn.execute("COMMIT")
            return SubmitResponse(
                report_id=report_id,
                station=payload.station,
                sampling_date=date_s,
                revision=payload.revision,
                status="created",
                superseded_revision=row["revision"] if row is not None else None,
            )
        except HTTPException:
            raise
        except sqlite3.OperationalError as exc:
            rollback_quietly(conn)
            if "locked" in str(exc).lower():
                raise HTTPException(
                    status_code=503,
                    detail={"error": "busy", "message": "数据库繁忙，请重试"},
                ) from exc
            raise
        except Exception:
            rollback_quietly(conn)
            raise
        finally:
            conn.close()

    # ---------------- 报告隔离 ----------------

    @app.post(
        "/reports/{report_id}/quarantine",
        status_code=201,
        response_model=QuarantineResponse,
        tags=["quarantine"],
        responses={
            200: {"description": "该报告已隔离过，幂等返回原隔离记录"},
            404: {"description": "报告不存在"},
            409: {"description": "旧修订从未隔离过，不得新隔离"},
            422: {"description": "请求体校验失败"},
            503: {"description": "数据库繁忙，可重试"},
        },
    )
    def quarantine_report(
        payload: QuarantineRequest,
        response: Response,
        report_id: Annotated[int, Path(ge=1, description="要隔离的现行报告 ID")],
    ) -> QuarantineResponse:
        conn = db.connect()
        try:
            # BEGIN IMMEDIATE：与提交串行化，"查现行 → 写隔离"不会与补报交错
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, station, sampling_date, revision FROM reports WHERE id=?",
                (report_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise HTTPException(
                    status_code=404,
                    detail={"error": "not_found", "message": "报告不存在"},
                )

            # 幂等优先：已隔离过的报告重复隔离返回原记录，
            # 即使此后已有更高修订（旧修订的隔离记录仍然有效）
            existing = conn.execute(
                "SELECT reason, quarantined_at FROM quarantines WHERE report_id=?",
                (report_id,),
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                response.status_code = 200
                return QuarantineResponse(
                    report_id=row["id"],
                    station=row["station"],
                    sampling_date=row["sampling_date"],
                    revision=row["revision"],
                    reason=existing["reason"],
                    quarantined_at=existing["quarantined_at"],
                    status="already_quarantined",
                )

            current = conn.execute(
                "SELECT MAX(revision) FROM reports WHERE station=? AND sampling_date=?",
                (row["station"], row["sampling_date"]),
            ).fetchone()[0]
            if row["revision"] != current:
                conn.execute("ROLLBACK")
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "not_current_revision",
                        "message": f"修订 {row['revision']} 已被修订 {current} 取代且从未隔离过，不得新隔离",
                        "current_revision": current,
                    },
                )

            now = dt.datetime.now(dt.timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO quarantines (report_id, reason, quarantined_at) "
                "VALUES (?,?,?)",
                (report_id, payload.reason, now),
            )
            conn.execute("COMMIT")
            return QuarantineResponse(
                report_id=row["id"],
                station=row["station"],
                sampling_date=row["sampling_date"],
                revision=row["revision"],
                reason=payload.reason,
                quarantined_at=now,
                status="quarantined",
            )
        except HTTPException:
            raise
        except sqlite3.OperationalError as exc:
            rollback_quietly(conn)
            if "locked" in str(exc).lower():
                raise HTTPException(
                    status_code=503,
                    detail={"error": "busy", "message": "数据库繁忙，请重试"},
                ) from exc
            raise
        except Exception:
            rollback_quietly(conn)
            raise
        finally:
            conn.close()

    # ---------------- 报告读取 ----------------

    @app.get("/reports/{station}/{sampling_date}", tags=["reports"])
    def get_report(
        station: Name,
        sampling_date: dt.date,
        revision: Annotated[Optional[int], Query(ge=1, description="缺省读取现行（最高）修订")] = None,
    ) -> dict[str, Any]:
        conn = db.connect()
        try:
            if revision is None:
                row = conn.execute(
                    "SELECT * FROM reports WHERE station=? AND sampling_date=? "
                    "ORDER BY revision DESC LIMIT 1",
                    (station, sampling_date.isoformat()),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM reports WHERE station=? AND sampling_date=? AND revision=?",
                    (station, sampling_date.isoformat(), revision),
                ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "not_found", "message": "报告不存在"},
                )
            items = conn.execute(
                "SELECT pollutant, type, value, detection_limit FROM report_items "
                "WHERE report_id=? ORDER BY pollutant",
                (row["id"],),
            ).fetchall()
            current = conn.execute(
                "SELECT MAX(revision) FROM reports WHERE station=? AND sampling_date=?",
                (station, sampling_date.isoformat()),
            ).fetchone()[0]
            quarantine = conn.execute(
                "SELECT reason, quarantined_at FROM quarantines WHERE report_id=?",
                (row["id"],),
            ).fetchone()
            return {
                "report_id": row["id"],
                "station": row["station"],
                "sampling_date": row["sampling_date"],
                "revision": row["revision"],
                "is_current": row["revision"] == current,
                "submitted_at": row["submitted_at"],
                # 历史修订的隔离原因始终可查（隔离记录永不删除）
                "quarantine": (
                    {
                        "reason": quarantine["reason"],
                        "quarantined_at": quarantine["quarantined_at"],
                    }
                    if quarantine is not None
                    else None
                ),
                "items": [_item_json(r) for r in items],
            }
        finally:
            conn.close()

    @app.get("/reports/{station}/{sampling_date}/revisions", tags=["reports"])
    def list_revisions(station: Name, sampling_date: dt.date) -> dict[str, Any]:
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT r.id, r.revision, r.submitted_at, "
                "(q.report_id IS NOT NULL) AS is_quarantined "
                "FROM reports r LEFT JOIN quarantines q ON q.report_id = r.id "
                "WHERE r.station=? AND r.sampling_date=? ORDER BY r.revision",
                (station, sampling_date.isoformat()),
            ).fetchall()
            if not rows:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "not_found", "message": "报告不存在"},
                )
            current = max(r["revision"] for r in rows)
            return {
                "station": station,
                "sampling_date": sampling_date.isoformat(),
                "revisions": [
                    {
                        "report_id": r["id"],
                        "revision": r["revision"],
                        "submitted_at": r["submitted_at"],
                        "is_current": r["revision"] == current,
                        "is_quarantined": bool(r["is_quarantined"]),
                    }
                    for r in rows
                ],
            }
        finally:
            conn.close()

    # ---------------- 判读 ----------------

    @app.get("/interpretation", tags=["interpretation"])
    def get_interpretation(
        station: Annotated[Name, Query(description="站点")],
        end_date: Annotated[dt.date, Query(description="窗口结束日（含），向前连续 7 天")],
        pollutant: Annotated[Name, Query(description="污染物")],
        limit: Annotated[NonNegFinite, Query(description="非负限值 mg/L")],
    ) -> dict[str, Any]:
        conn = db.connect()
        try:
            # 单事务读取：七份现行日报及其隔离状态取自同一已提交时点（快照），
            # 并发的提交/隔离不会混入本次判读
            conn.execute("BEGIN")
            snapshot = conn.execute("SELECT COALESCE(MAX(id),0) FROM reports").fetchone()[0]
            days: list[dict[str, Any]] = []
            for offset in range(WINDOW_DAYS - 1, -1, -1):
                d = end_date - dt.timedelta(days=offset)
                row = conn.execute(
                    "SELECT id, revision FROM reports "
                    "WHERE station=? AND sampling_date=? AND id<=? "
                    "ORDER BY revision DESC LIMIT 1",
                    (station, d.isoformat(), snapshot),
                ).fetchone()
                if row is None:
                    days.append(
                        {
                            "date": d.isoformat(),
                            "revision": None,
                            "report_id": None,
                            "items": None,
                            "quarantine": None,
                        }
                    )
                    continue
                quarantine = conn.execute(
                    "SELECT reason, quarantined_at FROM quarantines WHERE report_id=?",
                    (row["id"],),
                ).fetchone()
                if quarantine is not None:
                    # 现行报告被隔离：当日不参与判读，不回退旧修订
                    days.append(
                        {
                            "date": d.isoformat(),
                            "revision": row["revision"],
                            "report_id": row["id"],
                            "items": None,
                            "quarantine": {
                                "reason": quarantine["reason"],
                                "quarantined_at": quarantine["quarantined_at"],
                            },
                        }
                    )
                    continue
                rows = conn.execute(
                    "SELECT pollutant, type, value, detection_limit FROM report_items "
                    "WHERE report_id=?",
                    (row["id"],),
                ).fetchall()
                items = {
                    r["pollutant"]: {
                        "type": r["type"],
                        "value": r["value"],
                        "detection_limit": r["detection_limit"],
                    }
                    for r in rows
                }
                days.append(
                    {
                        "date": d.isoformat(),
                        "revision": row["revision"],
                        "report_id": row["id"],
                        "items": items,
                        "quarantine": None,
                    }
                )
            conn.execute("COMMIT")
        except Exception:
            rollback_quietly(conn)
            raise
        finally:
            conn.close()

        result = interpret_window(days, pollutant, limit)
        return {
            "station": station,
            "pollutant": pollutant,
            "end_date": end_date.isoformat(),
            "window_start": (end_date - dt.timedelta(days=WINDOW_DAYS - 1)).isoformat(),
            "window_days": WINDOW_DAYS,
            "limit": limit,
            "status": result["status"],
            "status_zh": STATUS_ZH[result["status"]],
            "mean": result["mean"],
            "revisions": result["revisions"],
            "missing_dates": result["missing_dates"],
            "missing_pollutant_dates": result["missing_pollutant_dates"],
            "quarantined": result["quarantined"],
            "snapshot_seq": snapshot,
        }

    return app


app = create_app()
