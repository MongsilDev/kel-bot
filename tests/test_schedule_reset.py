"""리셋 시 대시보드 메시지 재생성 콜백(on_reset) 배선 검증."""
import asyncio

from config.settings import Settings
from models.draw_state import DrawStatus
from models.schedule_manager import ScheduleManager


def _settings(tmp_path):
    return Settings(
        discord_token="x",
        apply_channel_id=1,
        reset_hour=21,
        draw_hour=0,
        draw_minute=30,
        deadline_hour=17,
        team_slots=8,
        data_dir=tmp_path,
    )


def _manager(tmp_path, calls):
    def make(name):
        async def _cb(*a, **k):
            calls.append(name)
        return _cb

    return ScheduleManager(
        _settings(tmp_path),
        tmp_path,
        on_draw=make("draw"),
        on_state_changed=make("state"),
        on_deadline_cancelled=make("deadline_cancelled"),
        on_reset=make("reset"),
    )


def test_daily_reset_calls_on_reset(tmp_path):
    calls = []
    mgr = _manager(tmp_path, calls)
    # 과거 일자로 맞춰 실제 리셋이 일어나도록 함
    mgr.state.applications.reset("2020-01-01")
    mgr.state.draw_state.reset("2020-01-01")
    calls.clear()
    asyncio.run(mgr._run_reset())
    assert "reset" in calls
    assert "state" not in calls  # 일반 갱신이 아니라 재생성 경로


def test_deadline_cancel_calls_on_reset(tmp_path):
    calls = []
    mgr = _manager(tmp_path, calls)
    # PENDING + 미달 상태라 데드라인에서 취소되고 조기 초기화
    asyncio.run(mgr._run_deadline())
    assert "reset" in calls


def test_deadline_after_draw_does_not_recreate(tmp_path):
    calls = []
    mgr = _manager(tmp_path, calls)
    mgr.state.draw_state.status = DrawStatus.DONE  # 이미 추첨 완료
    asyncio.run(mgr._run_deadline())
    assert "reset" not in calls
    assert "state" in calls


def test_reset_noop_skips_recreate(tmp_path):
    """이미 대상 일자로 초기화돼 있으면(예: 17시 조기초기화 후) 21시 _run_reset은
    데이터와 메시지 모두 스킵한다."""
    calls = []
    mgr = _manager(tmp_path, calls)
    # 현재 표시 일자를 21시 리셋이 도달할 일자와 동일하게 미리 맞춰 no-op 유도
    from utils.time import current_scrim_date

    target = current_scrim_date(mgr.settings.reset_hour).isoformat()
    mgr.state.applications.reset(target)
    mgr.state.draw_state.reset(target)
    calls.clear()
    asyncio.run(mgr._run_reset())
    assert "reset" not in calls  # no-op이라 메시지 재생성 스킵


def test_reset_actual_change_recreates(tmp_path):
    """실제로 새 일자로 초기화되면 메시지를 재생성한다(정상 21시 흐름)."""
    calls = []
    mgr = _manager(tmp_path, calls)
    # 과거 일자로 맞춰두면 21시 리셋이 실제로 일자를 바꾼다
    mgr.state.applications.reset("2020-01-01")
    mgr.state.draw_state.reset("2020-01-01")
    calls.clear()
    asyncio.run(mgr._run_reset())
    assert "reset" in calls


def test_guarded_job_swallows_exception(tmp_path, caplog):
    async def boom():
        raise RuntimeError("x")

    runner = ScheduleManager._guarded("테스트", boom)
    asyncio.run(runner())
    assert any(r.levelname == "ERROR" and r.exc_info for r in caplog.records)


def test_deadline_cancel_keeps_previous_result(tmp_path):
    calls = []
    mgr = _manager(tmp_path, calls)
    cancelled_date = mgr.state.draw_state.scrim_date
    mgr.state.applications.add(region="광주", applicant_id="1", applicant_display="광주) 가", had_priority=False)
    asyncio.run(mgr._run_deadline())
    assert mgr.state.draw_state.previous_cancel == {"scrim_date": cancelled_date, "applicants": 1}
    mgr.state.draw_state.reset("2099-01-01")
    assert mgr.state.draw_state.previous_cancel is None
