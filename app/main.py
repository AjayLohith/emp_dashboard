"""
Employee Attendance & Analytics API - Production Implementation
Contract: openapi.yaml & DATA_MODEL.md
Rules: R1 - R10
"""
from __future__ import annotations

import calendar
import math
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Tuple

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
import pymongo
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Section 1: Configuration & Database Connection
# --------------------------------------------------------------------------- #
load_dotenv()

# IST timezone definition (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

EPOCH_MIN = 100000000000
EPOCH_MAX = 4102444800000

_mongo_client: Optional[MongoClient] = None
_db_instance: Optional[Any] = None
_indexes_ready: bool = False


def get_db():
    """
    Get or initialize the shared MongoClient and database handle.
    No hard-coded connection URI or database fallbacks.
    """
    global _mongo_client, _db_instance
    if _db_instance is not None:
        return _db_instance

    uri = os.environ.get("MONGO_URI")
    db_name = os.environ.get("MONGO_DB")
    if not uri or not db_name:
        return None

    try:
        _mongo_client = MongoClient(
            uri,
            tz_aware=True,
            serverSelectionTimeoutMS=2000,
            connectTimeoutMS=2000,
        )
        _db_instance = _mongo_client[db_name]
        return _db_instance
    except Exception:
        return None


def init_indexes(db) -> None:
    """
    Idempotently create all required named indexes at application startup.
    """
    # 1. Employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_emp_dept_joined")
    db.employees.create_index([("joined_on", 1)], name="idx_emp_joined")

    # 2. Attendance logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_date")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_emp_punch_in_desc")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_date_desc_emp")
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)], name="idx_date_late_emp")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)], name="idx_emp_date_status")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context: ping MongoDB and initialize indexes idempotently.
    """
    global _indexes_ready
    db = get_db()
    if db is not None:
        try:
            db.command("ping")
            init_indexes(db)
            _indexes_ready = True
        except Exception:
            _indexes_ready = False
    else:
        _indexes_ready = False
    yield
    if _mongo_client is not None:
        _mongo_client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


def ensure_ready():
    """
    Verify that MongoDB is reachable and startup indexes are initialized.
    """
    db = get_db()
    if db is None or not _indexes_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable or startup indexes not ready",
        )
    return db


# --------------------------------------------------------------------------- #
# Section 2: Validation Models & Constants
# --------------------------------------------------------------------------- #
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


def validate_strict_epoch_millis(v: Any) -> int:
    """
    Validates strict integer epoch milliseconds within contract bounds [100000000000, 4102444800000].
    Rejects booleans, strings, floats, and out-of-range seconds.
    """
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Must be a strict integer epoch millisecond timestamp")
    if v < EPOCH_MIN or v > EPOCH_MAX:
        raise ValueError(f"Timestamp out of allowed range [{EPOCH_MIN}, {EPOCH_MAX}]")
    return v


def validate_calendar_date(v: str) -> str:
    """
    Validate that a string is a real calendar date in YYYY-MM-DD format.
    """
    if not isinstance(v, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        raise ValueError("Date must be formatted as YYYY-MM-DD")
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid calendar date: {v}")
    return v


def validate_shift_time(v: str) -> str:
    """
    Validate HH:MM 24-hour time format.
    """
    if not isinstance(v, str) or not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
        raise ValueError("Shift time must be 24-hour HH:MM format")
    return v


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30")
    shift_end: str = Field(default="18:30")
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def check_shift(cls, v: str) -> str:
        return validate_shift_time(v)

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, v: str) -> str:
        return validate_calendar_date(v)

    @model_validator(mode="after")
    def check_shift_different(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end cannot be equal")
        return self


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[EmployeeOut]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: PresenceStatusType = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        if v is not None:
            return validate_strict_epoch_millis(v)
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        if v is not None:
            return validate_strict_epoch_millis(v)
        return v


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[StatusType] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def check_punch_instants(cls, v: Any) -> Optional[int]:
        if v is not None:
            return validate_strict_epoch_millis(v)
        return v


class HistoryEntryOut(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceRecordOut(BaseModel):
    emp_code: str
    date: str
    status: StatusType
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntryOut]


class AttendancePage(BaseModel):
    items: List[AttendanceRecordOut]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryOut(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardOut(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class TrendOut(BaseModel):
    department: str
    items: List[TrendItem]


class ExplainOut(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Section 3: Time, IST/UTC, Rounding & Serialization Helpers
# --------------------------------------------------------------------------- #
def truncate_ms_to_second(ms: int) -> int:
    """Truncates an epoch-millisecond timestamp to whole seconds."""
    return (ms // 1000) * 1000


def ms_to_utc_dt(ms: int) -> datetime:
    """Converts epoch ms (truncated to whole seconds) to UTC datetime."""
    truncated = truncate_ms_to_second(ms)
    return datetime.fromtimestamp(truncated / 1000, tz=UTC)


def utc_dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts UTC datetime to integer epoch ms."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1000


def get_current_utc_dt() -> datetime:
    """Returns current UTC instant truncated to whole seconds."""
    now = datetime.now(UTC)
    return now.replace(microsecond=0)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Determines whether a shift spans overnight (shift_end <= shift_start)."""
    return shift_end <= shift_start


def compute_r1_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date in IST.
    If overnight shift and punch-in clock is strictly earlier than shift_end,
    it belongs to the shift that started the previous calendar day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    clock_str = punch_in_ist.strftime("%H:%M:%S")
    shift_end_str = shift_end + ":00"
    ist_date = punch_in_ist.date()

    if is_overnight_shift(shift_start, shift_end) and clock_str < shift_end_str:
        return (ist_date - timedelta(days=1)).isoformat()
    return ist_date.isoformat()


def get_shift_instants(date_str: str, shift_start: str, shift_end: str) -> Tuple[datetime, datetime]:
    """
    Returns (shift_start_utc, shift_end_utc) anchored to the attendance date.
    For overnight shifts, shift_end is on the following calendar day.
    """
    d = date.fromisoformat(date_str)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    start_dt_ist = datetime(d.year, d.month, d.day, sh, sm, 0, tzinfo=IST)
    if is_overnight_shift(shift_start, shift_end):
        next_d = d + timedelta(days=1)
        end_dt_ist = datetime(next_d.year, next_d.month, next_d.day, eh, em, 0, tzinfo=IST)
    else:
        end_dt_ist = datetime(d.year, d.month, d.day, eh, em, 0, tzinfo=IST)

    return start_dt_ist.astimezone(UTC), end_dt_ist.astimezone(UTC)


def compute_late_minutes(punch_in_utc: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R2: Late minutes.
    Grace period is 10 minutes.
    Strictly > 10:00 after shift_start -> floor(minutes from shift_start).
    Otherwise 0.
    """
    start_utc, _ = get_shift_instants(date_str, shift_start, shift_end)
    diff_sec = (punch_in_utc - start_utc).total_seconds()
    if diff_sec > 600:
        return int(math.floor(diff_sec / 60))
    return 0


def compute_overtime_minutes(punch_out_utc: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R3: Overtime minutes.
    Floor of elapsed minutes from shift_end to punch_out.
    Counted only when >= 30 minutes; otherwise 0.
    """
    _, end_utc = get_shift_instants(date_str, shift_start, shift_end)
    diff_sec = (punch_out_utc - end_utc).total_seconds()
    diff_min = int(math.floor(diff_sec / 60))
    if diff_min >= 30:
        return diff_min
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours_dec = Decimal(str(diff_sec)) / Decimal("3600")
    rounded = hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: half_day is true when already-rounded work_hours < 4.50.
    Exactly 4.50 is false.
    """
    if work_hours is None:
        return False
    return Decimal(str(work_hours)) < Decimal("4.50")


def round_half_up(val: Optional[float], decimals: int) -> Optional[float]:
    """
    R8: Decimal half-up rounding helper.
    """
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** (-decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serializes an employee document from MongoDB for HTTP response."""
    doc.pop("_id", None)
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        doc["created_at"] = utc_dt_to_ms(created_at)
    return doc


def serialize_history_entry(item: Dict[str, Any]) -> Dict[str, Any]:
    """Serializes a regularization history entry."""
    at_val = item.get("at")
    if isinstance(at_val, datetime):
        at_ms = utc_dt_to_ms(at_val)
    else:
        at_ms = at_val

    raw_changes = item.get("changes", {})
    serialized_changes: Dict[str, Dict[str, Any]] = {}
    for k, v in raw_changes.items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = utc_dt_to_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = utc_dt_to_ms(to_val)
        serialized_changes[k] = {"from": from_val, "to": to_val}

    return {
        "at": at_ms,
        "by": item.get("by"),
        "reason": item.get("reason"),
        "changes": serialized_changes,
    }


def serialize_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Serializes an attendance log document from MongoDB for HTTP response.
    Normalizes legacy missing fields and ensures no _id or id fields.
    """
    doc.pop("_id", None)
    doc.pop("id", None)

    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")

    raw_history = doc.get("history") or []
    serialized_history = [serialize_history_entry(h) for h in raw_history]

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_dt_to_ms(punch_in) if isinstance(punch_in, datetime) else punch_in,
        "punch_out": utc_dt_to_ms(punch_out) if isinstance(punch_out, datetime) else punch_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialized_history,
    }


def to_json_safe(obj: Any) -> Any:
    """Recursively converts BSON/MongoDB explain output into JSON-serializable types."""
    if isinstance(obj, dict):
        return {k: to_json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [to_json_safe(v) for v in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, ObjectId):
        return str(obj)
    elif isinstance(obj, Decimal):
        return float(obj)
    return obj


# --------------------------------------------------------------------------- #
# Section 4: Query & Pipeline Builders (Shared between Endpoints and Explain)
# --------------------------------------------------------------------------- #
def build_attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status_val: Optional[str],
) -> Tuple[Dict[str, Any], List[Tuple[str, int]]]:
    """Builds MongoDB query and sort criteria for listing attendance logs."""
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_val:
        q["status"] = status_val
    sort_order = [("date", -1), ("emp_code", 1)]
    return q, sort_order


def compute_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Counts Monday-Friday working days in the month from max(month_start, joined_on) through month end.
    If joined_on > month_end, working_days is 0.
    """
    year_int, mon_int = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year_int, mon_int)
    m_start = date(year_int, mon_int, 1)
    m_end = date(year_int, mon_int, last_day)

    joined_dt = date.fromisoformat(joined_on_str)
    if joined_dt > m_end:
        return 0

    start_date = max(m_start, joined_dt)
    cur = start_date
    working_days = 0
    while cur <= m_end:
        if cur.weekday() < 5:  # Monday to Friday
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def build_employee_monthly_pipeline(
    emp_code: str,
    month: str,
) -> List[Dict[str, Any]]:
    """Builds the MongoDB aggregation pipeline for single-employee monthly summary."""
    year_int, mon_int = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year_int, mon_int)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},  # Monday to Friday
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(
    month: str,
    department: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Builds the cross-collection MongoDB aggregation pipeline for department summary.
    Roots in employees to guarantee employees with 0 logs are counted in headcount.
    """
    year_int, mon_int = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year_int, mon_int)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.dayOfWeek", [2, 3, 4, 5, 6]]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": "$$l.late_minutes",
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "eligible_work_hours": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "l",
                        "in": "$$l.work_hours",
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "all_work_hours": {"$push": "$eligible_work_hours"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "_id": 0,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "flattened_work_hours": {
                    "$reduce": {
                        "input": "$all_work_hours",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$flattened_work_hours"}, 0]},
                        {"$avg": "$flattened_work_hours"},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Builds the MongoDB aggregation pipeline for the late-comers leaderboard.
    Uses competition ranking ($rank) and applies limit after ranking.
    """
    year_int, mon_int = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year_int, mon_int)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def build_department_trend_pipeline(
    department: str,
    from_str: str,
    to_str: str,
    dept_emp_codes: List[str],
) -> List[Dict[str, Any]]:
    """
    Builds the MongoDB aggregation pipeline for department daily attendance trend.
    Uses $densify for database-side gap-filling and $setWindowFields for 7-day moving average.
    """
    from_date = date.fromisoformat(from_str)
    to_date = date.fromisoformat(to_str)
    from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=UTC)
    to_dt_plus_1 = datetime(to_date.year, to_date.month, to_date.day, tzinfo=UTC) + timedelta(days=1)

    return [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "active_emps",
            }
        },
        {
            "$project": {
                "date": 1,
                "headcount": {"$size": "$active_emps"},
                "present_count": 1,
                "late_count": 1,
                "dayOfWeek": {"$dayOfWeek": "$date_dt"},
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Section 5: API Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: 200 only if MongoDB responds to ping and indexes are initialized."""
    if not _indexes_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database initializing or startup indexes not ready",
        )
    db = get_db()
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection not configured",
        )
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database ping failed: {str(e)}",
        )


@app.post("/employees", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Creates a new employee record. Uniqueness is enforced by database unique index."""
    db = ensure_ready()
    now_utc = get_current_utc_dt()
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Employee with emp_code '{body.emp_code}' already exists",
        )

    return serialize_employee(doc)


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Lists employees with optional department filter, sorted by emp_code ascending."""
    db = ensure_ready()
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", pymongo.ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post(
    "/attendance/punch-in",
    response_model=AttendanceRecordOut,
    status_code=status.HTTP_201_CREATED,
    tags=["attendance"],
)
def punch_in(body: PunchInRequest):
    """
    Records an employee punch-in. Computes date (R1) and late minutes (R2).
    Concurrent duplicate requests for (emp_code, date) are handled atomically via unique index.
    """
    db = ensure_ready()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{body.emp_code}' not found",
        )

    if body.punched_at is not None:
        dt_in = ms_to_utc_dt(body.punched_at)
    else:
        dt_in = get_current_utc_dt()

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    att_date = compute_r1_date(dt_in, shift_start, shift_end)
    late_mins = compute_late_minutes(dt_in, att_date, shift_start, shift_end)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": dt_in,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Attendance record already exists for employee '{body.emp_code}' on date '{att_date}'",
        )

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", response_model=AttendanceRecordOut, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Closes the most recent open record whose punch_in <= punched_at.
    Computes work hours (R4), overtime (R3), and half day (R5).
    """
    db = ensure_ready()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{body.emp_code}' not found",
        )

    if body.punched_at is not None:
        dt_out = ms_to_utc_dt(body.punched_at)
    else:
        dt_out = get_current_utc_dt()

    # Find the most recent record whose punch_in <= punched_at
    latest_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": dt_out}},
        sort=[("punch_in", pymongo.DESCENDING)],
    )

    if not latest_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No matching punch-in found for employee '{body.emp_code}'",
        )

    # If the record is already punched out, return 409
    if latest_record.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The latest shift has already been punched out",
        )

    dt_in = latest_record["punch_in"]
    if dt_in.tzinfo is None:
        dt_in = dt_in.replace(tzinfo=UTC)

    if dt_out <= dt_in:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Punch-out timestamp must be strictly greater than punch-in",
        )

    if (dt_out - dt_in).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    att_date = latest_record["date"]

    work_hours = compute_work_hours(dt_in, dt_out)
    overtime_mins = compute_overtime_minutes(dt_out, att_date, shift_start, shift_end)
    half_day = compute_half_day(work_hours)

    # Atomic conditional update: only update if punch_out is still None
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": latest_record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": dt_out,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The record was already punched out by a concurrent request",
        )

    return serialize_attendance(updated_doc)


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Lists attendance records with filters, sorted by date descending then emp_code ascending."""
    db = ensure_ready()

    if date_from:
        validate_calendar_date(date_from)
    if date_to:
        validate_calendar_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be <= date_to",
        )
    if status_filter and status_filter not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status filter '{status_filter}'",
        )

    q, sort_order = build_attendance_list_query(emp_code, date_from, date_to, status_filter)

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort(sort_order).skip(skip).limit(page_size)
    items = [serialize_attendance(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecordOut, tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """
    Manually regularize an attendance record. Recomputes derived fields and appends an audit history entry.
    Concurrent edits preserve history entries atomically.
    """
    db = ensure_ready()
    validate_calendar_date(date)

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{emp_code}' not found",
        )

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Attendance record for employee '{emp_code}' on date '{date}' not found",
        )

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    # Target status
    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Punch times cannot be provided for ABSENT or LEAVE status",
            )
        new_punch_in = None
        new_punch_out = None
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in = ms_to_utc_dt(body.punch_in)
        else:
            new_punch_in = record.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Presence status '{target_status}' requires a punch-in timestamp",
            )

        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=UTC)

        # Validate that punch_in maps to the route's attendance date under R1
        assigned_date = compute_r1_date(new_punch_in, shift_start, shift_end)
        if assigned_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Punch-in maps to date '{assigned_date}', which does not match record date '{date}'",
            )

        if body.punch_out is not None:
            new_punch_out = ms_to_utc_dt(body.punch_out)
        else:
            new_punch_out = record.get("punch_out")

        if new_punch_out is not None and new_punch_out.tzinfo is None:
            new_punch_out = new_punch_out.replace(tzinfo=UTC)

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Punch-out timestamp must be strictly greater than punch-in",
                )
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Punch-out cannot be more than 24 hours after punch-in",
                )

        # Recompute derived fields
        new_late_minutes = compute_late_minutes(new_punch_in, date, shift_start, shift_end)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime_minutes(new_punch_out, date, shift_start, shift_end)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Detect actual changes
    field_changes: Dict[str, Dict[str, Any]] = {}

    def check_diff(field_name: str, old_val: Any, new_val: Any):
        if old_val != new_val:
            field_changes[field_name] = {"from": old_val, "to": new_val}

    check_diff("status", record.get("status"), target_status)
    check_diff("punch_in", record.get("punch_in"), new_punch_in)
    check_diff("punch_out", record.get("punch_out"), new_punch_out)
    check_diff("work_hours", record.get("work_hours"), new_work_hours)
    check_diff("late_minutes", record.get("late_minutes", 0), new_late_minutes)
    check_diff("overtime_minutes", record.get("overtime_minutes", 0), new_overtime_minutes)
    check_diff("half_day", record.get("half_day", False), new_half_day)

    if not field_changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Regularization request resulted in no changes to the record",
        )

    # Server instant for history
    now_utc = get_current_utc_dt()
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": field_changes,
    }

    # Atomic conditional update using optimistic concurrency matching the current state
    filter_doc = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }

    set_doc = {
        "status": target_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    updated_doc = db.attendance_logs.find_one_and_update(
        filter_doc,
        {
            "$set": set_doc,
            "$push": {"history": history_entry},
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification conflict: record was updated by another request",
        )

    return serialize_attendance(updated_doc)


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthlyOut, tags=["analytics"])
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Calculates monthly attendance summary for one employee via MongoDB aggregation."""
    db = ensure_ready()
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{emp_code}' not found",
        )

    working_days = compute_working_days(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late = int(res.get("total_late_minutes", 0))
        total_ot = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        raw_pct = (present_days / working_days) * 100.0
        attendance_pct = round_half_up(raw_pct, 4)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummaryOut, tags=["analytics"])
def department_summary_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Calculates department-level summary metrics across collections via MongoDB aggregation.
    Includes employees with zero logs in headcount.
    """
    db = ensure_ready()
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        headcount = int(r.get("headcount", 0))
        if headcount == 0:
            continue
        avg_wh = r.get("avg_work_hours")
        items.append({
            "department": r["department"],
            "headcount": headcount,
            "present_days": float(r.get("present_days", 0.0)),
            "avg_work_hours": round_half_up(avg_wh, 2) if avg_wh is not None else None,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=LeaderboardOut, tags=["analytics"])
def late_leaderboard_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Ranks late-comers using competition ranking ($rank).
    Applies limit after ranking; cutoff ties share rank and remain in response.
    """
    db = ensure_ready()
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in results
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", response_model=TrendOut, tags=["analytics"])
def department_trend_analytics(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """
    Daily attendance trend for a department via MongoDB aggregation.
    Uses $densify for database-side gap-filling and $setWindowFields for moving average.
    """
    db = ensure_ready()
    validate_calendar_date(from_date)
    validate_calendar_date(to_date)

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)

    if d_to < d_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date must be >= 'from' date",
        )

    days_diff = (d_to - d_from).days + 1
    if days_diff > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Requested range of {days_diff} days exceeds maximum allowed 92 days",
        )

    # Verify department exists
    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
    if not dept_emps:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Department '{department}' not found",
        )

    dept_emp_codes = [e["emp_code"] for e in dept_emps]
    pipeline = build_department_trend_pipeline(department, from_date, to_date, dept_emp_codes)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        rate = r.get("attendance_rate")
        m_avg = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r.get("is_working_day", False)),
            "headcount": int(r.get("headcount", 0)),
            "present_count": float(r.get("present_count", 0.0)),
            "late_count": int(r.get("late_count", 0)),
            "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": round_half_up(m_avg, 4) if m_avg is not None else None,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", response_model=ExplainOut, tags=["admin"])
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    """
    Runs MongoDB explain with executionStats verbosity on the endpoint's main query or pipeline.
    Uses the exact shared builders as the real endpoints.
    """
    db = ensure_ready()
    allowed_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in allowed_endpoints:
        raise HTTPException(
            status_code=422,
            detail=f"Endpoint '{endpoint}' is not valid for explain. Must be one of {allowed_endpoints}",
        )

    if endpoint == "attendance_list":
        if page < 1 or page_size < 1 or page_size > 100:
            raise HTTPException(status_code=422, detail="Invalid pagination parameters")
        if date_from:
            validate_calendar_date(date_from)
        if date_to:
            validate_calendar_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")

        q, sort_order = build_attendance_list_query(emp_code, date_from, date_to, status_filter)
        skip = (page - 1) * page_size
        explain_raw = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": dict(sort_order),
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": to_json_safe(explain_raw),
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="Missing required query parameter: emp_code")
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=422, detail="Missing or invalid required query parameter: month (YYYY-MM)")

        emp = db.employees.find_one({"emp_code": emp_code})
        if not emp:
            raise HTTPException(status_code=404, detail=f"Employee '{emp_code}' not found")

        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_raw = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": to_json_safe(explain_raw),
        }

    elif endpoint == "department_summary":
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=422, detail="Missing or invalid required query parameter: month (YYYY-MM)")

        pipeline = build_department_summary_pipeline(month, department)
        explain_raw = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": to_json_safe(explain_raw),
        }

    elif endpoint == "late_leaderboard":
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=422, detail="Missing or invalid required query parameter: month (YYYY-MM)")
        if limit < 1 or limit > 50:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 50")

        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_raw = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": to_json_safe(explain_raw),
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="Missing required query parameter: department")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="Missing required query parameters: from and to")
        validate_calendar_date(from_date)
        validate_calendar_date(to_date)
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Requested range exceeds 92 days")

        dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
        if not dept_emps:
            raise HTTPException(status_code=404, detail=f"Department '{department}' not found")
        dept_emp_codes = [e["emp_code"] for e in dept_emps]

        pipeline = build_department_trend_pipeline(department, from_date, to_date, dept_emp_codes)
        explain_raw = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": to_json_safe(explain_raw),
        }
