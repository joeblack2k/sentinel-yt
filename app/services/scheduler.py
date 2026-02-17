from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def _to_minutes(t: str) -> int:
    h, m = t.split(":", 1)
    return int(h) * 60 + int(m)


class ScheduleService:
    @staticmethod
    def is_active(*, enabled: bool, start: str, end: str, timezone_name: str) -> bool:
        if not enabled:
            return True
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        now_min = now.hour * 60 + now.minute
        start_min = _to_minutes(start)
        end_min = _to_minutes(end)

        if start_min == end_min:
            return True
        if start_min < end_min:
            return start_min <= now_min < end_min
        return now_min >= start_min or now_min < end_min

    @staticmethod
    def pick_active_window(schedules: list[dict]) -> dict | None:
        for row in schedules:
            enabled = bool(row.get("enabled", True))
            if not enabled:
                continue
            if ScheduleService.is_active(
                enabled=True,
                start=str(row.get("start", "00:00")),
                end=str(row.get("end", "23:59")),
                timezone_name=str(row.get("timezone", "UTC")),
            ):
                return row
        return None
