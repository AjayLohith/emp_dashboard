# DECISIONS.md

1. **Indexes.** We created `employees(emp_code)` (unique), `employees(department, joined_on)`, and `employees(joined_on)` for headcount; `attendance_logs(emp_code, date)` (unique natural key); `attendance_logs(emp_code, punch_in: -1)` for punch-out; `attendance_logs(date: -1, emp_code: 1)` for sorted listing; and `attendance_logs(date, late_minutes, emp_code)` for leaderboard covered scans. We considered and rejected a standalone index on `attendance_logs(status)` due to low cardinality and lack of independent filter usage.

2. **Punch-in race.** Both requests resolve the attendance date and attempt `insert_one`. MongoDB's compound unique index `(emp_code, date)` atomically admits exactly one write (HTTP 201). The concurrent duplicate fails with `DuplicateKeyError`, which our handler intercepts to return HTTP 409 without corrupted state.

3. **Ties.** MongoDB's `$rank` window function assigns competition ranks (1, 2, 2, 4). Filtering `rank <= limit` applies after ranking. If employees tie at the cutoff rank, all qualifying ties are retained, so returned rows can exceed `limit`.

4. **Headcount.** The summary aggregation begins on `employees`, matching `joined_on <= month_end`, and left-joins `attendance_logs`. Employees with zero logs produce empty arrays, preserving their inclusion in departmental headcount.

5. **100x data change.** Incrementally maintain monthly department summaries and daily totals in dedicated rollup collections via change streams, eliminating real-time multi-stage aggregations over raw punch logs.
