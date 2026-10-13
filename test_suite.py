"""
Comprehensive verification test suite for Employee Attendance & Analytics API.
"""
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import threading
import pytest
from fastapi.testclient import TestClient

# Ensure test env
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["MONGO_DB"] = "test_attendance_verification"

import app.main as main
from app.main import app, get_db, init_indexes, IST, UTC

client = TestClient(app)


@pytest.fixture(autouse=True, scope="session")
def setup_db():
    db = get_db()
    db.employees.drop()
    db.attendance_logs.drop()
    init_indexes(db)
    main._indexes_ready = True
    yield
    db.employees.drop()
    db.attendance_logs.drop()


# --------------------------------------------------------------------------- #
# 1. Health and Readiness
# --------------------------------------------------------------------------- #
def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# 2. Employee CRUD and Validation
# --------------------------------------------------------------------------- #
def test_create_employee_success():
    payload = {
        "emp_code": "EMP0101",
        "name": "Arjun Sharma",
        "email": "arjun@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0101"
    assert "_id" not in data
    assert "id" not in data
    assert isinstance(data["created_at"], int)
    assert data["created_at"] % 1000 == 0  # truncated to whole seconds


def test_create_employee_duplicate_emp_code():
    payload = {
        "emp_code": "EMP0101",
        "name": "Duplicate Arjun",
        "email": "dup@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-15",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 409
    assert "detail" in res.json()


def test_create_employee_validation_errors():
    # Bad emp_code (not matching ^EMP\d{4,6}$)
    res = client.post("/employees", json={
        "emp_code": "BAD123",
        "name": "Test",
        "email": "test@example.com",
        "department": "HR",
        "joined_on": "2026-01-01",
    })
    assert res.status_code == 422

    # shift_start == shift_end
    res = client.post("/employees", json={
        "emp_code": "EMP0102",
        "name": "Test",
        "email": "test@example.com",
        "department": "HR",
        "shift_start": "09:00",
        "shift_end": "09:00",
        "joined_on": "2026-01-01",
    })
    assert res.status_code == 422

    # Invalid email
    res = client.post("/employees", json={
        "emp_code": "EMP0102",
        "name": "Test",
        "email": "invalid-email",
        "department": "HR",
        "joined_on": "2026-01-01",
    })
    assert res.status_code == 422

    # Invalid calendar date (e.g. Feb 30)
    res = client.post("/employees", json={
        "emp_code": "EMP0102",
        "name": "Test",
        "email": "test@example.com",
        "department": "HR",
        "joined_on": "2026-02-30",
    })
    assert res.status_code == 422


def test_list_employees_pagination_and_filter():
    # Add another employee
    client.post("/employees", json={
        "emp_code": "EMP0100",
        "name": "Bina Rai",
        "email": "bina@example.com",
        "department": "Marketing",
        "joined_on": "2026-02-01",
    })

    res = client.get("/employees?department=Engineering")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["emp_code"] == "EMP0101"

    # Sorted by emp_code ascending
    res_all = client.get("/employees")
    assert res_all.status_code == 200
    items = res_all.json()["items"]
    assert items[0]["emp_code"] == "EMP0100"
    assert items[1]["emp_code"] == "EMP0101"


# --------------------------------------------------------------------------- #
# 3. Punch-In, R1, R2, Concurrency
# --------------------------------------------------------------------------- #
def test_punch_in_r1_and_r2():
    # Setup overnight shift employee
    client.post("/employees", json={
        "emp_code": "EMP0200",
        "name": "Night Owl",
        "email": "owl@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01",
    })

    # 1. Normal day shift punch-in (09:30 shift start)
    # Punch at 09:40:00 -> exactly 10 min -> 0 late minutes
    # 2026-07-06 09:40:00 IST = 04:10:00 UTC
    dt_940 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    ms_940 = int(dt_940.timestamp() * 1000)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0101",
        "punched_at": ms_940,
        "status": "PRESENT",
    })
    assert res.status_code == 201
    rec = res.json()
    assert rec["date"] == "2026-07-06"
    assert rec["late_minutes"] == 0
    assert rec["punch_out"] is None
    assert rec["work_hours"] is None
    assert "_id" not in rec and "id" not in rec

    # 2. Punch at 09:40:01 on another day -> 10 minutes late!
    dt_94001 = datetime(2026, 7, 7, 4, 10, 1, tzinfo=timezone.utc)
    ms_94001 = int(dt_94001.timestamp() * 1000)
    res2 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0101",
        "punched_at": ms_94001,
    })
    assert res2.status_code == 201
    assert res2.json()["date"] == "2026-07-07"
    assert res2.json()["late_minutes"] == 10

    # 3. Punch at 10:15:59 on another day -> 45 minutes late!
    dt_101559 = datetime(2026, 7, 8, 4, 45, 59, tzinfo=timezone.utc)
    ms_101559 = int(dt_101559.timestamp() * 1000)
    res3 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0101",
        "punched_at": ms_101559,
    })
    assert res3.status_code == 201
    assert res3.json()["date"] == "2026-07-08"
    assert res3.json()["late_minutes"] == 45

    # 4. Overnight shift: 22:00 - 06:00
    # Punch-in at 05:45 IST on 2026-07-07 (< 06:00) -> belongs to 2026-07-06 shift!
    # 05:45 IST = 00:15 UTC
    dt_night = datetime(2026, 7, 7, 0, 15, 0, tzinfo=timezone.utc)
    ms_night = int(dt_night.timestamp() * 1000)
    res_night = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0200",
        "punched_at": ms_night,
    })
    assert res_night.status_code == 201
    assert res_night.json()["date"] == "2026-07-06"


def test_punch_in_timestamp_validation():
    # String rejected with 422
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0101", "punched_at": "1783312500000"})
    assert res.status_code == 422

    # Float rejected with 422
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0101", "punched_at": 1783312500000.5})
    assert res.status_code == 422

    # Boolean rejected with 422
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0101", "punched_at": True})
    assert res.status_code == 422

    # Seconds-looking value (< 100000000000) rejected with 422
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0101", "punched_at": 1783312500})
    assert res.status_code == 422


def test_punch_in_concurrency_race():
    # Parallel punch-in for same (emp_code, date)
    dt = datetime(2026, 7, 9, 4, 0, 0, tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)

    results = []

    def make_punch():
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0101", "punched_at": ms})
        results.append(r.status_code)

    t1 = threading.Thread(target=make_punch)
    t2 = threading.Thread(target=make_punch)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one 201 and one 409
    assert sorted(results) == [201, 409]


# --------------------------------------------------------------------------- #
# 4. Punch-Out, R3, R4, R5, Concurrency
# --------------------------------------------------------------------------- #
def test_punch_out_rules():
    # On 2026-07-06: EMP0101 punched in at 04:10:00 UTC (09:40:00 IST). Shift end is 18:30 IST = 13:00:00 UTC.
    # Punch out at 19:10:00 IST = 13:40:00 UTC (40 min after shift end -> overtime = 40)
    # Duration: 04:10 to 13:40 = 9 hours 30 min = 9.5 hours. work_hours = 9.50, half_day = False.
    dt_out = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    ms_out = int(dt_out.timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0101", "punched_at": ms_out})
    assert res.status_code == 200
    rec = res.json()
    assert rec["work_hours"] == 9.5
    assert rec["overtime_minutes"] == 40
    assert rec["half_day"] is False

    # Second punch out for already punched-out record -> 409
    res_dup = client.post("/attendance/punch-out", json={"emp_code": "EMP0101", "punched_at": ms_out + 10000})
    assert res_dup.status_code == 409

    # Half day boundary check:
    # 2026-07-07: In at 04:10:01 UTC. Punch out 4.49 hours later:
    # 4.49 hours = 16164 seconds.
    dt_out_hd = datetime(2026, 7, 7, 4, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=16164)
    res_hd = client.post("/attendance/punch-out", json={"emp_code": "EMP0101", "punched_at": int(dt_out_hd.timestamp() * 1000)})
    assert res_hd.status_code == 200
    assert res_hd.json()["work_hours"] == 4.49
    assert res_hd.json()["half_day"] is True


def test_punch_out_concurrency_race():
    # 2026-07-08 record is open. Two parallel punch outs.
    dt_out = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
    ms_out = int(dt_out.timestamp() * 1000)

    results = []

    def make_punch_out():
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0101", "punched_at": ms_out})
        results.append(r.status_code)

    t1 = threading.Thread(target=make_punch_out)
    t2 = threading.Thread(target=make_punch_out)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one 200 and one 409
    assert sorted(results) == [200, 409]


# --------------------------------------------------------------------------- #
# 5. Regularize (PATCH) and History
# --------------------------------------------------------------------------- #
def test_regularize_success_and_history():
    # Modify 2026-07-06 record
    dt_new_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)  # 09:28 IST
    ms_new_in = int(dt_new_in.timestamp() * 1000)

    res = client.patch(
        "/attendance/EMP0101/2026-07-06",
        json={
            "reason": "Biometric scanner glitch at entry",
            "regularized_by": "hr_manager",
            "punch_in": ms_new_in,
        },
    )
    assert res.status_code == 200
    rec = res.json()
    assert len(rec["history"]) == 1
    hist = rec["history"][0]
    assert hist["by"] == "hr_manager"
    assert hist["reason"] == "Biometric scanner glitch at entry"
    assert "punch_in" in hist["changes"]
    assert "work_hours" in hist["changes"]
    assert isinstance(hist["at"], int)
    assert isinstance(hist["changes"]["punch_in"]["from"], int)
    assert isinstance(hist["changes"]["punch_in"]["to"], int)


def test_regularize_no_op_is_422():
    # Try regularizing without changing anything
    res = client.patch(
        "/attendance/EMP0101/2026-07-06",
        json={
            "reason": "No actual change attempted",
            "regularized_by": "hr_manager",
            "status": "PRESENT",
        },
    )
    assert res.status_code == 422


def test_regularize_absent_clears_times():
    # Mark 2026-07-06 as ABSENT
    res = client.patch(
        "/attendance/EMP0101/2026-07-06",
        json={
            "reason": "Employee actually on unapproved absence",
            "regularized_by": "hr_manager",
            "status": "ABSENT",
        },
    )
    assert res.status_code == 200
    rec = res.json()
    assert rec["status"] == "ABSENT"
    assert rec["punch_in"] is None
    assert rec["punch_out"] is None
    assert rec["work_hours"] is None
    assert rec["half_day"] is False
    assert len(rec["history"]) == 2  # history appended


# --------------------------------------------------------------------------- #
# 6. Analytics: Monthly, Dept Summary, Leaderboard, Trend
# --------------------------------------------------------------------------- #
def test_analytics_monthly():
    res = client.get("/analytics/employees/EMP0101/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0101"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23  # July 2026 has 23 weekdays
    assert "present_days" in data
    assert "attendance_pct" in data


def test_analytics_department_summary():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert isinstance(data["items"], list)
    depts = [item["department"] for item in data["items"]]
    assert depts == sorted(depts)  # sorted ascending


def test_analytics_late_leaderboard():
    res = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    for item in data["items"]:
        assert item["rank"] >= 1
        assert item["total_late_minutes"] > 0


def test_analytics_department_trend():
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 10  # exactly 10 days
    # Check gap filling and moving avg
    for item in data["items"]:
        assert "date" in item
        assert "headcount" in item
        assert "is_working_day" in item


def test_analytics_department_trend_range_limit_422():
    res = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert res.status_code == 422  # exceeds 92 days


# --------------------------------------------------------------------------- #
# 7. Admin Explain Endpoint
# --------------------------------------------------------------------------- #
def test_admin_explain_endpoints():
    # 1. attendance_list
    res1 = client.get("/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31")
    assert res1.status_code == 200
    assert res1.json()["collection"] == "attendance_logs"
    assert "explain" in res1.json()

    # 2. employee_monthly
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0101&month=2026-07")
    assert res2.status_code == 200
    assert res2.json()["collection"] == "attendance_logs"

    # 3. department_summary
    res3 = client.get("/admin/explain/department_summary?month=2026-07")
    assert res3.status_code == 200
    assert res3.json()["collection"] == "employees"

    # 4. late_leaderboard
    res4 = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res4.status_code == 200
    assert res4.json()["collection"] == "attendance_logs"

    # 5. department_trend
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
    assert res5.status_code == 200
    assert res5.json()["collection"] == "attendance_logs"
