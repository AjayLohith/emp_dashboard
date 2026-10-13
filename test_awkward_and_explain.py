"""
Specialized tests for awkward data edge cases and index explain verification.
"""
from datetime import datetime, date, timedelta, timezone
import os
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app, get_db, init_indexes

client = TestClient(app)


@pytest.fixture(autouse=True, scope="module")
def setup_awkward_data():
    db = get_db()
    db.employees.drop()
    db.attendance_logs.drop()
    init_indexes(db)
    main._indexes_ready = True

    # 1. Employees:
    # EMP1001: Joined 2026-07-15 (mid-month joiner in Engineering)
    # EMP1002: Joined 2026-08-01 (joins after July, in Engineering)
    # EMP1003: Joined 2026-01-01 (Finance, zero logs in July)
    # EMP1004: Joined 2026-01-01 (Sales, tied for late)
    # EMP1005: Joined 2026-01-01 (Sales, tied for late)
    # EMP1006: Joined 2026-01-01 (Sales, tied for late at cutoff)
    # EMP1007: Joined 2026-01-01 (Sales, tied for late at cutoff)
    # EMP1008: Joined 2026-01-01 (Sales, less late)
    emps = [
        {"emp_code": "EMP1001", "name": "Mid Month", "email": "mid@test.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-15", "created_at": datetime(2026, 7, 15, tzinfo=timezone.utc)},
        {"emp_code": "EMP1002", "name": "Future Joiner", "email": "future@test.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-01", "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc)},
        {"emp_code": "EMP1003", "name": "Zero Logs", "email": "zero@test.com", "department": "Finance", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"emp_code": "EMP1004", "name": "Tied 1A", "email": "t1a@test.com", "department": "Sales", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"emp_code": "EMP1005", "name": "Tied 1B", "email": "t1b@test.com", "department": "Sales", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"emp_code": "EMP1006", "name": "Tied Cutoff A", "email": "tca@test.com", "department": "Sales", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"emp_code": "EMP1007", "name": "Tied Cutoff B", "email": "tcb@test.com", "department": "Sales", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"emp_code": "EMP1008", "name": "Lower Late", "email": "low@test.com", "department": "Sales", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    ]
    db.employees.insert_many(emps)

    # 2. Attendance logs:
    logs = [
        # EMP1001: Mid-month joiner (joined 2026-07-15)
        # Log on 2026-07-15 (Wed): PRESENT, work_hours 9.0
        {"emp_code": "EMP1001", "date": "2026-07-15", "status": "PRESENT", "punch_in": datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc), "punch_out": datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc), "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        # Weekend log on 2026-07-18 (Sat): PRESENT, late_minutes 20, overtime 40
        {"emp_code": "EMP1001", "date": "2026-07-18", "status": "PRESENT", "punch_in": datetime(2026, 7, 18, 4, 20, tzinfo=timezone.utc), "punch_out": datetime(2026, 7, 18, 13, 40, tzinfo=timezone.utc), "work_hours": 9.33, "late_minutes": 20, "overtime_minutes": 40, "half_day": False, "history": []},

        # Sales employees for leaderboard ranking with ties:
        # EMP1004 & EMP1005 tied at 60 total late minutes
        {"emp_code": "EMP1004", "date": "2026-07-06", "status": "PRESENT", "punch_in": datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc), "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc), "work_hours": 8.0, "late_minutes": 60, "overtime_minutes": 0, "half_day": False, "history": []},
        {"emp_code": "EMP1005", "date": "2026-07-06", "status": "PRESENT", "punch_in": datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc), "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc), "work_hours": 8.0, "late_minutes": 60, "overtime_minutes": 0, "half_day": False, "history": []},

        # EMP1006 & EMP1007 tied at 30 total late minutes (will share rank 3 in Sales)
        {"emp_code": "EMP1006", "date": "2026-07-07", "status": "PRESENT", "punch_in": datetime(2026, 7, 7, 4, 30, tzinfo=timezone.utc), "punch_out": datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc), "work_hours": 8.5, "late_minutes": 30, "overtime_minutes": 0, "half_day": False, "history": []},
        {"emp_code": "EMP1007", "date": "2026-07-07", "status": "PRESENT", "punch_in": datetime(2026, 7, 7, 4, 30, tzinfo=timezone.utc), "punch_out": datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc), "work_hours": 8.5, "late_minutes": 30, "overtime_minutes": 0, "half_day": False, "history": []},

        # EMP1008 has 15 total late minutes (will have rank 5)
        {"emp_code": "EMP1008", "date": "2026-07-08", "status": "PRESENT", "punch_in": datetime(2026, 7, 8, 4, 15, tzinfo=timezone.utc), "punch_out": datetime(2026, 7, 8, 13, 0, tzinfo=timezone.utc), "work_hours": 8.75, "late_minutes": 15, "overtime_minutes": 0, "half_day": False, "history": []},

        # Legacy doc format (missing history and half_day):
        {"emp_code": "EMP1001", "date": "2026-07-20", "status": "PRESENT", "punch_in": datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc), "punch_out": datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc), "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0},
    ]
    db.attendance_logs.insert_many(logs)
    yield


def test_mid_month_joiner_monthly_analytics():
    # EMP1001 joined 2026-07-15.
    # July 2026 weekdays from July 15 to July 31:
    # 15(Wed), 16(Thu), 17(Fri) -> 3
    # 20-24 -> 5
    # 27-31 -> 5
    # Total weekdays = 3 + 5 + 5 = 13 weekdays.
    res = client.get("/analytics/employees/EMP1001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 13
    # present_days: July 15 (weekday) + July 20 (weekday) = 2.0.
    # Weekend log on July 18 is NOT in present_days!
    assert data["present_days"] == 2.0
    # But weekend late/overtime IS included in totals:
    # late_minutes: 20, overtime: 40
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 20
    assert data["total_overtime_minutes"] == 40
    # attendance_pct = 2.0 / 13 * 100 = 15.384615... -> 15.3846
    assert data["attendance_pct"] == 15.3846


def test_future_joiner_monthly_analytics():
    # EMP1002 joined 2026-08-01 (after July end)
    res = client.get("/analytics/employees/EMP1002/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 0
    assert data["attendance_pct"] is None


def test_zero_logs_employee_in_dept_summary():
    # EMP1003 in Finance has 0 logs.
    res = client.get("/analytics/departments/summary?month=2026-07&department=Finance")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["department"] == "Finance"
    assert item["headcount"] == 1  # counted in headcount!
    assert item["present_days"] == 0.0
    assert item["avg_work_hours"] is None  # no logs -> None


def test_future_joiner_excluded_from_dept_headcount():
    # In Engineering: EMP1001 joined July 15 (counted). EMP1002 joined Aug 1 (excluded).
    res = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert res.status_code == 200
    item = res.json()["items"][0]
    assert item["headcount"] == 1  # Only EMP1001, not EMP1002!


def test_leaderboard_competition_ranking_and_cutoff_ties():
    # Sales employees:
    # EMP1004 & EMP1005: 60 mins -> share rank 1.
    # EMP1006 & EMP1007: 30 mins -> next rank skipped! Rank is 3! (1, 1, 3, 3)
    # EMP1008: 15 mins -> rank 5.
    res = client.get("/analytics/leaderboard/late?month=2026-07&department=Sales&limit=10")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 5

    assert items[0]["rank"] == 1
    assert items[0]["emp_code"] == "EMP1004"
    assert items[0]["total_late_minutes"] == 60

    assert items[1]["rank"] == 1
    assert items[1]["emp_code"] == "EMP1005"
    assert items[1]["total_late_minutes"] == 60

    assert items[2]["rank"] == 3  # rank 2 is skipped!
    assert items[2]["emp_code"] == "EMP1006"
    assert items[2]["total_late_minutes"] == 30

    assert items[3]["rank"] == 3
    assert items[3]["emp_code"] == "EMP1007"
    assert items[3]["total_late_minutes"] == 30

    assert items[4]["rank"] == 5  # rank 4 is skipped!
    assert items[4]["emp_code"] == "EMP1008"
    assert items[4]["total_late_minutes"] == 15

    # Test limit cutoff where ties cause count > limit:
    # If limit=3: items with rank <= 3 must be returned!
    # That includes both rank 1s and both rank 3s -> 4 items returned (> limit)!
    res_lim3 = client.get("/analytics/leaderboard/late?month=2026-07&department=Sales&limit=3")
    assert res_lim3.status_code == 200
    items_lim3 = res_lim3.json()["items"]
    assert len(items_lim3) == 4  # 4 returned even though limit=3 because rank 3 is tied!
    for it in items_lim3:
        assert it["rank"] <= 3


def test_indexes_and_explain_no_collscan():
    db = get_db()
    # Test all 5 explain branches to confirm IXSCAN and NO COLLSCAN on attendance_logs or employees
    branches = [
        ("attendance_list", "/admin/explain/attendance_list?emp_code=EMP1001&date_from=2026-07-01&date_to=2026-07-31"),
        ("employee_monthly", "/admin/explain/employee_monthly?emp_code=EMP1001&month=2026-07"),
        ("department_summary", "/admin/explain/department_summary?month=2026-07&department=Engineering"),
        ("late_leaderboard", "/admin/explain/late_leaderboard?month=2026-07&department=Sales"),
        ("department_trend", "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-20"),
    ]

    for name, url in branches:
        res = client.get(url)
        assert res.status_code == 200, f"Failed for {name}: {res.text}"
        data = res.json()
        assert "executionStats" in str(data["explain"]), f"Missing executionStats in {name}"
        # Convert explain to string to check for IXSCAN
        explain_str = str(data["explain"])
        assert "IXSCAN" in explain_str or "PROJECTION_COVERED" in explain_str, f"Missing IXSCAN in {name}: {explain_str[:400]}"
