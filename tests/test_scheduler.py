from datetime import datetime as real_datetime

from app.services import scheduler
from app.services.scheduler import ScheduleService


def _freeze_clock(monkeypatch, hour: int) -> None:
    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 1, 1, hour, 0, tzinfo=tz)

    monkeypatch.setattr(scheduler, "datetime", FrozenDateTime)


def test_schedule_active_normal_window_true(monkeypatch):
    _freeze_clock(monkeypatch, 12)
    assert ScheduleService.is_active(
        enabled=True,
        start="07:00",
        end="19:00",
        timezone_name="UTC",
    )


def test_schedule_cross_midnight_logic(monkeypatch):
    _freeze_clock(monkeypatch, 23)
    assert ScheduleService.is_active(
        enabled=True,
        start="22:00",
        end="06:00",
        timezone_name="UTC",
    )
    _freeze_clock(monkeypatch, 12)
    assert not ScheduleService.is_active(
        enabled=True,
        start="22:00",
        end="06:00",
        timezone_name="UTC",
    )
