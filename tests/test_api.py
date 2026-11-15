"""端到端 API 测试：提交、修订语义、判读规则、并发与原子性。"""

import datetime as dt
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

END = dt.date(2026, 11, 14)  # 判读结束日


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def val(pollutant, value):
    return {"type": "value", "pollutant": pollutant, "value": value}


def nd(pollutant, detection_limit):
    return {"type": "nd", "pollutant": pollutant, "detection_limit": detection_limit}


def submit(client, station, date, revision, items):
    return client.post(
        "/reports",
        json={
            "station": station,
            "sampling_date": str(date),
            "revision": revision,
            "items": items,
        },
    )


def fill_week(client, station, end, pollutant, week, revision=1):
    """week: 7 个元素（最早→最新），float 表示实测值，("nd", dl) 表示未检出。"""
    assert len(week) == 7
    for i, entry in enumerate(week):
        day = end - dt.timedelta(days=6 - i)
        item = nd(pollutant, entry[1]) if isinstance(entry, tuple) else val(pollutant, entry)
        r = submit(client, station, day, revision, [item])
        assert r.status_code == 201, r.text


def interpret(client, station, end, pollutant, limit):
    return client.get(
        "/interpretation",
        params={
            "station": station,
            "end_date": str(end),
            "pollutant": pollutant,
            "limit": limit,
        },
    )


def quarantine(client, report_id, reason="仪器故障复核"):
    return client.post(f"/reports/{report_id}/quarantine", json={"reason": reason})


def current_id(client, station, date):
    return client.get(f"/reports/{station}/{date}").json()["report_id"]


# ---------------- 提交与修订语义 ----------------


def test_submit_and_read_current(client):
    r = submit(client, "S1", "2026-11-10", 1, [val("COD", 12.3), nd("NH3-N", 0.05)])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "created"
    assert body["revision"] == 1

    g = client.get("/reports/S1/2026-11-10")
    assert g.status_code == 200
    data = g.json()
    assert data["is_current"] is True
    assert data["items"] == [
        {"type": "value", "pollutant": "COD", "value": 12.3},
        {"type": "nd", "pollutant": "NH3-N", "detection_limit": 0.05},
    ]


def test_same_revision_same_content_reused(client):
    payload = [val("COD", 1.5), nd("NH3-N", 0.05)]
    r1 = submit(client, "S1", "2026-11-10", 1, payload)
    assert r1.status_code == 201
    # 项目顺序不同、数值写法等价，仍视为同内容
    r2 = submit(client, "S1", "2026-11-10", 1, [nd("NH3-N", 0.05), val("COD", 1.50)])
    assert r2.status_code == 200
    assert r2.json()["status"] == "reused"
    assert r2.json()["report_id"] == r1.json()["report_id"]

    revs = client.get("/reports/S1/2026-11-10/revisions").json()["revisions"]
    assert len(revs) == 1  # 未产生新记录


def test_same_revision_different_content_rejected(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.5)])
    r = submit(client, "S1", "2026-11-10", 1, [val("COD", 2.0)])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "revision_content_mismatch"
    # 原报告未被破坏
    g = client.get("/reports/S1/2026-11-10").json()
    assert g["items"] == [{"type": "value", "pollutant": "COD", "value": 1.5}]


def test_stale_revision_rejected(client):
    submit(client, "S1", "2026-11-10", 3, [val("COD", 1.0)])
    r = submit(client, "S1", "2026-11-10", 2, [val("COD", 9.9)])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "stale_revision"
    # 同内容的旧修订同样拒收
    r2 = submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0)])
    assert r2.status_code == 409


def test_higher_revision_replaces_fully_and_old_stays_readable(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0), val("NH3-N", 2.0)])
    r = submit(client, "S1", "2026-11-10", 2, [val("COD", 3.0)])
    assert r.status_code == 201
    assert r.json()["superseded_revision"] == 1

    cur = client.get("/reports/S1/2026-11-10").json()
    assert cur["revision"] == 2
    assert cur["items"] == [{"type": "value", "pollutant": "COD", "value": 3.0}]
    # 删除的项目不得沿用旧值：现行报告里已无 NH3-N
    assert all(i["pollutant"] != "NH3-N" for i in cur["items"])

    # 已提交的旧修订仍可读取
    old = client.get("/reports/S1/2026-11-10", params={"revision": 1})
    assert old.status_code == 200
    assert old.json()["is_current"] is False
    assert {i["pollutant"] for i in old.json()["items"]} == {"COD", "NH3-N"}

    revs = client.get("/reports/S1/2026-11-10/revisions").json()["revisions"]
    assert [r["revision"] for r in revs] == [1, 2]


def test_revision_gaps_allowed(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0)])
    r = submit(client, "S1", "2026-11-10", 5, [val("COD", 2.0)])
    assert r.status_code == 201
    assert client.get("/reports/S1/2026-11-10").json()["revision"] == 5


# ---------------- 判读规则 ----------------


def test_interpret_compliant_and_revisions_returned(client):
    fill_week(client, "S1", END, "COD", [10.0] * 7)
    r = interpret(client, "S1", END, "COD", 10.0)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "compliant"
    assert body["status_zh"] == "达标"
    assert body["mean"] == {"lower": 10.0, "upper": 10.0, "upper_exclusive": False}
    assert len(body["revisions"]) == 7
    assert body["revisions"][0]["date"] == "2026-11-08"
    assert body["revisions"][-1]["date"] == "2026-11-14"
    assert all(rv["revision"] == 1 for rv in body["revisions"])
    assert body["window_start"] == "2026-11-08"


def test_interpret_exceeds(client):
    fill_week(client, "S1", END, "COD", [10.0] * 6 + [11.0])
    body = interpret(client, "S1", END, "COD", 10.0).json()
    assert body["status"] == "exceeds"
    assert body["status_zh"] == "超标"


def test_interpret_pending_review_when_interval_straddles(client):
    # 6 天实测 9.0，1 天未检出(检出限 20)：均值 ∈ [7.714..., 10.571...) 跨过 10
    week = [9.0] * 6 + [("nd", 20.0)]
    fill_week(client, "S1", END, "COD", week)
    body = interpret(client, "S1", END, "COD", 10.0).json()
    assert body["status"] == "pending_review"
    assert body["status_zh"] == "待复核"
    assert body["mean"]["upper_exclusive"] is True
    assert body["mean"]["lower"] < 10.0 < body["mean"]["upper"]


def test_nd_upper_bound_inclusive_for_compliance(client):
    # 未检出按 [0, 检出限) 参与：均值上界恰等于限值时仍必定不高于限值 → 达标
    week = [0.0] * 6 + [("nd", 0.07)]  # mean_hi = 0.01
    fill_week(client, "S1", END, "COD", week)
    body = interpret(client, "S1", END, "COD", 0.01).json()
    assert body["status"] == "compliant"
    assert body["mean"]["upper"] == pytest.approx(0.01)

    # 检出限略大则上界超过限值 → 待复核
    fill_week(client, "S2", END, "COD", [0.0] * 6 + [("nd", 0.0700001)])
    body2 = interpret(client, "S2", END, "COD", 0.01).json()
    assert body2["status"] == "pending_review"


def test_all_nd_against_zero_limit_is_pending(client):
    fill_week(client, "S1", END, "COD", [("nd", 0.05)] * 7)
    body = interpret(client, "S1", END, "COD", 0.0).json()
    assert body["status"] == "pending_review"
    assert body["mean"]["lower"] == 0.0


def test_all_zero_against_zero_limit_is_compliant(client):
    fill_week(client, "S1", END, "COD", [0.0] * 7)
    body = interpret(client, "S1", END, "COD", 0.0).json()
    assert body["status"] == "compliant"


def test_mean_lower_above_limit_is_exceeds_with_nd(client):
    # 6 天 11.0 + 1 天 nd(30.0)：均值 ∈ [9.428..., 13.714...) 跨过 10 → 待复核
    fill_week(client, "S1", END, "COD", [11.0] * 6 + [("nd", 30.0)])
    assert interpret(client, "S1", END, "COD", 10.0).json()["status"] == "pending_review"
    # 限值 9.0：均值下界 9.428... > 9 → 必定超标
    assert interpret(client, "S1", END, "COD", 9.0).json()["status"] == "exceeds"
    # 6 天 11.0 + 1 天 nd(0.05)：均值上界 66.05/7 ≈ 9.44 ≤ 10 → 必定达标
    fill_week(client, "S2", END, "COD", [11.0] * 6 + [("nd", 0.05)])
    assert interpret(client, "S2", END, "COD", 10.0).json()["status"] == "compliant"


def test_insufficient_when_missing_day(client):
    fill_week(client, "S1", END, "COD", [1.0] * 7)
    # 删不掉已提交报告，换一个只有 6 天数据的站点
    for i in range(6):
        day = END - dt.timedelta(days=6 - i)
        submit(client, "S2", day, 1, [val("COD", 1.0)])
    body = interpret(client, "S2", END, "COD", 5.0).json()
    assert body["status"] == "insufficient_data"
    assert body["status_zh"] == "资料不足"
    assert body["missing_dates"] == ["2026-11-14"]
    assert body["mean"] is None
    assert len(body["revisions"]) == 6


def test_insufficient_when_missing_pollutant(client):
    for i in range(7):
        day = END - dt.timedelta(days=6 - i)
        items = [val("COD", 1.0)] if i < 3 else [val("NH3-N", 1.0)]
        submit(client, "S1", day, 1, items)
    body = interpret(client, "S1", END, "COD", 5.0).json()
    assert body["status"] == "insufficient_data"
    assert body["missing_pollutant_dates"] == ["2026-11-11", "2026-11-12", "2026-11-13", "2026-11-14"]


def test_interpret_uses_current_revision_only(client):
    # 第 1 天 rev1 含 COD=100（会超标），rev2 删除 COD → 资料不足而非沿用旧值
    fill_week(client, "S1", END, "COD", [1.0] * 7)
    first_day = END - dt.timedelta(days=6)
    submit(client, "S1", first_day, 2, [val("NH3-N", 0.5)])
    body = interpret(client, "S1", END, "COD", 5.0).json()
    assert body["status"] == "insufficient_data"
    assert body["missing_pollutant_dates"] == [str(first_day)]
    revs = {r["date"]: r["revision"] for r in body["revisions"]}
    assert revs[str(first_day)] == 2

    # 第 1 天 rev3 重新加入 COD=0.5 → 用最新修订判读
    submit(client, "S1", first_day, 3, [val("COD", 0.5)])
    body2 = interpret(client, "S1", END, "COD", 5.0).json()
    assert body2["status"] == "compliant"
    revs2 = {r["date"]: r["revision"] for r in body2["revisions"]}
    assert revs2[str(first_day)] == 3


def test_interpret_unknown_station(client):
    body = interpret(client, "NOPE", END, "COD", 1.0).json()
    assert body["status"] == "insufficient_data"
    assert len(body["missing_dates"]) == 7
    assert body["revisions"] == []


# ---------------- 校验 ----------------


@pytest.mark.parametrize(
    "item",
    [
        {"type": "value", "pollutant": "COD", "value": -0.1},          # 负浓度
        {"type": "value", "pollutant": "COD", "value": "abc"},         # 非数值
        {"type": "value", "pollutant": "COD"},                          # 缺 value
        {"type": "value", "pollutant": "COD", "value": 1.0, "detection_limit": 0.5},  # 多余字段
        {"type": "nd", "pollutant": "COD"},                             # 缺检出限
        {"type": "nd", "pollutant": "COD", "detection_limit": 0},       # 检出限须为正
        {"type": "nd", "pollutant": "COD", "detection_limit": -1},
        {"type": "nd", "pollutant": "COD", "value": 1.0, "detection_limit": 0.5},
        {"type": "other", "pollutant": "COD"},
        {"type": "value", "pollutant": "", "value": 1.0},               # 空污染物名
    ],
)
def test_invalid_items_rejected(client, item):
    r = submit(client, "S1", "2026-11-10", 1, [item])
    assert r.status_code == 422, r.text


def test_non_finite_values_rejected(client):
    r = client.post(
        "/reports",
        content=(
            b'{"station":"S1","sampling_date":"2026-11-10","revision":1,'
            b'"items":[{"type":"value","pollutant":"COD","value":Infinity}]}'
        ),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


def test_duplicate_pollutant_rejected(client):
    r = submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0), val("COD", 2.0)])
    assert r.status_code == 422


@pytest.mark.parametrize("revision", [0, -1, 1.5, "2", True])
def test_invalid_revision_rejected(client, revision):
    r = client.post(
        "/reports",
        json={
            "station": "S1",
            "sampling_date": "2026-11-10",
            "revision": revision,
            "items": [val("COD", 1.0)],
        },
    )
    assert r.status_code == 422, r.text


def test_empty_items_rejected(client):
    r = submit(client, "S1", "2026-11-10", 1, [])
    assert r.status_code == 422


@pytest.mark.parametrize("limit", [-0.5, "abc"])
def test_invalid_limit_rejected(client, limit):
    r = interpret(client, "S1", END, "COD", limit)
    assert r.status_code == 422, r.text


def test_failed_submission_leaves_nothing(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0)])
    # 一次被拒的提交不应改变已存数据，也不产生半成品
    submit(client, "S1", "2026-11-10", 1, [val("COD", 9.9)])  # 409
    submit(client, "S1", "2026-11-10", 0, [val("COD", 9.9)])  # 422
    g = client.get("/reports/S1/2026-11-10").json()
    assert g["revision"] == 1
    assert g["items"] == [{"type": "value", "pollutant": "COD", "value": 1.0}]
    revs = client.get("/reports/S1/2026-11-10/revisions").json()["revisions"]
    assert len(revs) == 1


# ---------------- 隔离 ----------------


def test_quarantine_current_report_blocks_day_without_fallback(client):
    # 第 1 天 rev1 含错误浓度 100.0，rev2 修正为 1.0；隔离 rev2 后不得回退 rev1
    fill_week(client, "S1", END, "COD", [1.0] * 7)
    first_day = END - dt.timedelta(days=6)
    submit(client, "S1", first_day, 2, [val("COD", 100.0)])
    rid = current_id(client, "S1", first_day)

    r = quarantine(client, rid, "仪器故障复核：消解仪温度漂移")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "quarantined"
    assert body["report_id"] == rid
    assert body["revision"] == 2
    assert body["reason"] == "仪器故障复核：消解仪温度漂移"
    assert body["quarantined_at"]

    interp = interpret(client, "S1", END, "COD", 5.0).json()
    assert interp["status"] == "insufficient_data"
    assert interp["status_zh"] == "资料不足"
    assert interp["mean"] is None
    # 列出隔离日期与报告 ID，且不回退旧修订
    assert interp["quarantined"] == [
        {"date": str(first_day), "revision": 2, "report_id": rid}
    ]
    used = {rv["date"] for rv in interp["revisions"]}
    assert str(first_day) not in used  # 被隔离的当日不参与判读
    assert len(interp["revisions"]) == 6


def test_quarantine_repeat_returns_original_record(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0)])
    rid = current_id(client, "S1", "2026-11-10")
    r1 = quarantine(client, rid, "原始原因")
    assert r1.status_code == 201
    # 重复隔离（即使原因不同）返回原记录，不覆盖
    r2 = quarantine(client, rid, "另一个原因")
    assert r2.status_code == 200
    assert r2.json()["status"] == "already_quarantined"
    assert r2.json()["reason"] == "原始原因"
    assert r2.json()["quarantined_at"] == r1.json()["quarantined_at"]


def test_quarantine_repeat_allowed_after_newer_revision(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0)])
    rid = current_id(client, "S1", "2026-11-10")
    r1 = quarantine(client, rid, "原始原因")
    assert r1.status_code == 201
    # 补交更高修订后，旧修订的重复隔离仍返回原结果
    submit(client, "S1", "2026-11-10", 2, [val("COD", 2.0)])
    r2 = quarantine(client, rid, "别的原因")
    assert r2.status_code == 200
    assert r2.json()["reason"] == "原始原因"
    assert r2.json()["quarantined_at"] == r1.json()["quarantined_at"]


def test_quarantine_old_revision_never_quarantined_rejected(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0)])
    old_id = current_id(client, "S1", "2026-11-10")
    submit(client, "S1", "2026-11-10", 2, [val("COD", 2.0)])
    r = quarantine(client, old_id)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "not_current_revision"
    assert r.json()["detail"]["current_revision"] == 2
    # 旧修订仍未被隔离
    detail = client.get("/reports/S1/2026-11-10", params={"revision": 1}).json()
    assert detail["quarantine"] is None


def test_quarantine_nonexistent_report(client):
    r = quarantine(client, 999999)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"


@pytest.mark.parametrize("payload", [{}, {"reason": ""}, {"reason": "   "}, {"reason": "x", "extra": 1}])
def test_quarantine_invalid_request_rejected(client, payload):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0)])
    rid = current_id(client, "S1", "2026-11-10")
    r = client.post(f"/reports/{rid}/quarantine", json=payload)
    assert r.status_code == 422, r.text


def test_higher_revision_restores_day_and_old_keeps_reason(client):
    fill_week(client, "S1", END, "COD", [1.0] * 7)
    first_day = END - dt.timedelta(days=6)
    rid = current_id(client, "S1", first_day)
    quarantine(client, rid, "仪器故障复核")
    assert interpret(client, "S1", END, "COD", 5.0).json()["status"] == "insufficient_data"

    # 补交更高修订：该日重新参与判读
    submit(client, "S1", first_day, 2, [val("COD", 0.5)])
    body = interpret(client, "S1", END, "COD", 5.0).json()
    assert body["status"] == "compliant"
    assert body["quarantined"] == []
    revs = {r["date"]: r["revision"] for r in body["revisions"]}
    assert revs[str(first_day)] == 2

    # 报告详情仍可查看旧修订的隔离原因；新修订无隔离记录
    old = client.get(f"/reports/S1/{first_day}", params={"revision": 1}).json()
    assert old["is_current"] is False
    assert old["quarantine"]["reason"] == "仪器故障复核"
    assert old["quarantine"]["quarantined_at"]
    cur = client.get(f"/reports/S1/{first_day}").json()
    assert cur["revision"] == 2
    assert cur["quarantine"] is None

    # 修订列表标注隔离状态
    revs_list = client.get(f"/reports/S1/{first_day}/revisions").json()["revisions"]
    assert [(r["revision"], r["is_quarantined"]) for r in revs_list] == [(1, True), (2, False)]


def test_failed_quarantine_does_not_change_interpretation(client):
    fill_week(client, "S1", END, "COD", [1.0] * 7)
    first_day = END - dt.timedelta(days=6)
    submit(client, "S1", first_day, 2, [val("COD", 1.0)])
    before = interpret(client, "S1", END, "COD", 5.0).json()

    old_id = client.get(f"/reports/S1/{first_day}", params={"revision": 1}).json()["report_id"]
    # 三种失败：旧修订新隔离(409)、不存在(404)、缺原因(422)
    assert quarantine(client, old_id).status_code == 409
    assert quarantine(client, 999999).status_code == 404
    assert client.post(f"/reports/{old_id}/quarantine", json={}).status_code == 422

    # 失败的隔离不改变任何判读结果（响应逐字段一致）
    after = interpret(client, "S1", END, "COD", 5.0).json()
    assert after == before
    assert after["status"] == "compliant"


def test_quarantine_response_visible_in_report_detail(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0)])
    rid = current_id(client, "S1", "2026-11-10")
    quarantine(client, rid, "泵采样管破裂")
    detail = client.get("/reports/S1/2026-11-10").json()
    assert detail["quarantine"] == {
        "reason": "泵采样管破裂",
        "quarantined_at": detail["quarantine"]["quarantined_at"],
    }
    assert detail["items"] == [{"type": "value", "pollutant": "COD", "value": 1.0}]


# ---------------- 并发 ----------------


def test_concurrent_identical_submissions(client):
    payload = {
        "station": "S1",
        "sampling_date": "2026-11-10",
        "revision": 1,
        "items": [val("COD", 1.0), nd("NH3-N", 0.05)],
    }
    results = []

    def worker():
        c = TestClient(client.app)
        r = c.post("/reports", json=payload)
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(code for code, _ in results)
    assert codes.count(201) == 1
    assert codes.count(200) == 7
    ids = {body["report_id"] for _, body in results}
    assert len(ids) == 1  # 全部复用同一份报告
    revs = client.get("/reports/S1/2026-11-10/revisions").json()["revisions"]
    assert len(revs) == 1


def test_concurrent_increasing_revisions(client):
    def worker(rev):
        c = TestClient(client.app)
        r = c.post(
            "/reports",
            json={
                "station": "S1",
                "sampling_date": "2026-11-10",
                "revision": rev,
                "items": [val("COD", float(rev)), nd("NH3-N", 0.05)],
            },
        )
        assert r.status_code in (201, 409), r.text
        return r.status_code

    threads = [threading.Thread(target=worker, args=(rev,)) for rev in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 最终现行报告完整可读，项目无缺失、无混杂
    g = client.get("/reports/S1/2026-11-10")
    assert g.status_code == 200
    data = g.json()
    assert data["revision"] == 8
    assert sorted(i["pollutant"] for i in data["items"]) == ["COD", "NH3-N"]
    cod = next(i for i in data["items"] if i["pollutant"] == "COD")
    assert cod["value"] == 8.0  # 与最高修订一致，而非半份或混杂


def test_concurrent_submit_and_interpret(client):
    fill_week(client, "S1", END, "COD", [1.0] * 7)
    errors = []

    def submitter():
        c = TestClient(client.app)
        for rev in range(2, 12):
            c.post(
                "/reports",
                json={
                    "station": "S1",
                    "sampling_date": str(END),
                    "revision": rev,
                    "items": [val("COD", 1.0)],
                },
            )

    def reader():
        c = TestClient(client.app)
        for _ in range(30):
            r = c.get(
                "/interpretation",
                params={
                    "station": "S1",
                    "end_date": str(END),
                    "pollutant": "COD",
                    "limit": 5.0,
                },
            )
            if r.status_code != 200 or r.json()["status"] != "compliant":
                errors.append(r.text)

    threads = [threading.Thread(target=submitter)] + [
        threading.Thread(target=reader) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    # 判读始终基于同一时点的七份现行日报：最终所用修订号一致
    body = interpret(client, "S1", END, "COD", 5.0).json()
    assert [r["revision"] for r in body["revisions"]] == [1] * 6 + [11]


def test_concurrent_identical_quarantines(client):
    submit(client, "S1", "2026-11-10", 1, [val("COD", 1.0)])
    rid = current_id(client, "S1", "2026-11-10")
    results = []

    def worker():
        c = TestClient(client.app)
        r = c.post(f"/reports/{rid}/quarantine", json={"reason": "仪器故障复核"})
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(code for code, _ in results)
    assert codes.count(201) == 1
    assert codes.count(200) == 7
    # 全部返回同一份隔离记录（同一时间戳与原因）
    assert len({(b["reason"], b["quarantined_at"]) for _, b in results}) == 1
    detail = client.get("/reports/S1/2026-11-10").json()
    assert detail["quarantine"]["reason"] == "仪器故障复核"


def test_concurrent_quarantine_submit_and_interpret(client):
    fill_week(client, "S1", END, "COD", [1.0] * 7)
    first_day = END - dt.timedelta(days=6)
    rid1 = current_id(client, "S1", first_day)
    errors = []
    quarantine_codes = []

    def quarantiner():
        c = TestClient(client.app)
        for _ in range(10):
            r = c.post(f"/reports/{rid1}/quarantine", json={"reason": "仪器故障复核"})
            # 合法线性化结果：201 隔离成功 / 200 幂等复用 /
            # 409 补交的更高修订先落库（旧修订从未隔离，不得新隔离）
            if r.status_code not in (200, 201, 409):
                errors.append(r.text)
            else:
                quarantine_codes.append(r.status_code)

    def submitter():
        c = TestClient(client.app)
        for rev in range(2, 8):
            c.post(
                "/reports",
                json={
                    "station": "S1",
                    "sampling_date": str(first_day),
                    "revision": rev,
                    "items": [val("COD", 1.0)],
                },
            )

    def reader():
        c = TestClient(client.app)
        for _ in range(30):
            r = c.get(
                "/interpretation",
                params={
                    "station": "S1",
                    "end_date": str(END),
                    "pollutant": "COD",
                    "limit": 5.0,
                },
            )
            if r.status_code != 200:
                errors.append(r.text)
                continue
            b = r.json()
            used = {rv["date"] for rv in b["revisions"]}
            quar = {q["date"] for q in b["quarantined"]}
            missing = set(b["missing_dates"])
            # 同一快照：七天在 所用修订/隔离/缺失 三类中恰好各出现一次
            if not (used.isdisjoint(quar) and used.isdisjoint(missing) and quar.isdisjoint(missing)):
                errors.append(f"overlap: {b}")
            elif len(used) + len(quar) + len(missing) != 7:
                errors.append(f"incomplete window: {b}")
            # 有隔离日必为资料不足；无隔离且七天齐全必为达标（浓度均 1.0）
            if quar:
                if b["status"] != "insufficient_data" or b["mean"] is not None:
                    errors.append(f"quarantined but not insufficient: {b}")
                if [q["report_id"] for q in b["quarantined"]] != [rid1]:
                    errors.append(f"wrong quarantined report: {b}")
            elif not missing and len(used) == 7:
                if b["status"] != "compliant":
                    errors.append(f"unexpected status: {b}")

    threads = (
        [threading.Thread(target=quarantiner), threading.Thread(target=submitter)]
        + [threading.Thread(target=reader) for _ in range(4)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    # 隔离结果只有两种合法线性化：{一次 201 + 其余 200} 或 {全部 409}
    assert sorted(set(quarantine_codes)) in ([200, 201], [409])
    assert quarantine_codes.count(201) <= 1

    # 终态与隔离竞争结果一致：rev7 为现行且未隔离，判读恢复达标
    body = interpret(client, "S1", END, "COD", 5.0).json()
    assert body["status"] == "compliant"
    assert body["quarantined"] == []
    revs = {r["date"]: r["revision"] for r in body["revisions"]}
    assert revs[str(first_day)] == 7
    old = client.get(f"/reports/S1/{first_day}", params={"revision": 1}).json()
    if 201 in quarantine_codes:
        # 隔离曾成功：旧修订详情保留隔离原因，重复隔离仍返回原记录
        assert old["quarantine"]["reason"] == "仪器故障复核"
        r = quarantine(client, rid1, "别的原因")
        assert r.status_code == 200 and r.json()["reason"] == "仪器故障复核"
    else:
        # 补报全部抢先：rev1 从未被隔离
        assert old["quarantine"] is None
