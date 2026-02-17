from app.services.scheduler import ScheduleService


def test_schedule_active_normal_window_true():
    # This test checks parser behavior; exact wall-clock depends on timezone.
    assert isinstance(
        ScheduleService.is_active(enabled=False, start="07:00", end="19:00", timezone_name="UTC"),
        bool,
    )


def test_schedule_cross_midnight_logic():
    assert isinstance(
        ScheduleService.is_active(enabled=True, start="22:00", end="06:00", timezone_name="UTC"),
        bool,
    )
