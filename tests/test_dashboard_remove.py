import asyncio
from datetime import date as date_cls, timedelta

from commands.dashboard import DashboardController
from config.settings import Settings
from models.draw_state import DrawStatus
from models.schedule_manager import ScheduleManager


def _next(d):
    return (date_cls.fromisoformat(d) + timedelta(days=1)).isoformat()


async def _noop(*args, **kwargs):
    pass


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


class _Resp:
    def __init__(self):
        self.sent = None
        self.edited = None

    async def send_message(self, *, view=None, ephemeral=False):
        self.sent = {"view": view, "ephemeral": ephemeral}

    async def edit_message(self, *, view=None):
        self.edited = {"view": view}

    def is_done(self):
        return False


class _User:
    def __init__(self, uid, display):
        self.id = uid
        self.display_name = display
        self.name = display


class _Interaction:
    def __init__(self, uid, display):
        self.user = _User(uid, display)
        self.response = _Resp()


def _controller(tmp_path):
    settings = _settings(tmp_path)
    mgr = ScheduleManager(
        settings,
        tmp_path,
        on_draw=_noop,
        on_state_changed=_noop,
        on_deadline_cancelled=_noop,
        on_reset=_noop,
    )
    ctrl = DashboardController(bot=object(), settings=settings, schedule=mgr)
    ctrl._dashboard_message = None  # refresh no-op
    return ctrl, mgr


class _FakeMsg:
    def __init__(self, mid):
        self.id = mid
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        m = _FakeMsg(2000 + len(self.sent))
        self.sent.append(m)
        return m


def test_recreate_deletes_old_and_sends_new(tmp_path, monkeypatch):
    import commands.dashboard as dash

    ctrl, mgr = _controller(tmp_path)
    old = _FakeMsg(1111)
    ctrl._dashboard_message = old
    chan = _FakeChannel()
    saved = {}

    async def fake_fetch(bot, cid):
        return chan

    monkeypatch.setattr(dash, "fetch_text_channel", fake_fetch)
    monkeypatch.setattr(dash, "save_message_id", lambda path, mid: saved.update(id=mid))

    asyncio.run(ctrl.recreate())

    assert old.deleted is True                       # 기존 메시지 삭제
    assert len(chan.sent) == 1                        # 새 메시지 전송
    assert ctrl._dashboard_message is chan.sent[0]    # 새 메시지로 바인딩
    assert saved["id"] == chan.sent[0].id             # 새 id 저장


def test_confirm_remove_revokes_and_logs(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    mgr.state.priorities.grant(["광주"], d, slot_cap=8)
    it = _Interaction(1, "광주) 홍길동")
    asyncio.run(ctrl._confirm_remove_priority(it, "광주", d))
    assert mgr.state.priorities.regions_for(d) == set()
    entries = mgr.state.audit.entries_for(d)
    assert len(entries) == 1
    assert entries[0].region == "광주"
    assert entries[0].actor_id == "1"


def test_handle_manage_shows_panel(tmp_path):
    # 활성 우선권이 없어도 관리 패널(추가 버튼 포함)은 항상 표시됨
    ctrl, mgr = _controller(tmp_path)
    it = _Interaction(1, "광주) 홍길동")
    asyncio.run(ctrl.handle_manage_priority(it))
    assert it.response.sent is not None
    assert it.response.sent["ephemeral"] is True


def test_confirm_add_grants_and_logs(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    it = _Interaction(7, "서울) 운영자")
    asyncio.run(ctrl._confirm_add_priority(it, "광주", d))
    assert mgr.state.priorities.regions_for(d) == {"광주"}
    entries = mgr.state.audit.entries_for(d)
    assert len(entries) == 1
    assert entries[0].region == "광주"
    assert entries[0].action == "grant"
    assert entries[0].actor_id == "7"


def test_confirm_add_strips_region(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    it = _Interaction(7, "서울) 운영자")
    asyncio.run(ctrl._confirm_add_priority(it, "  광주  ", d))
    assert mgr.state.priorities.regions_for(d) == {"광주"}


def test_confirm_add_empty_region_noop(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    it = _Interaction(7, "서울) 운영자")
    asyncio.run(ctrl._confirm_add_priority(it, "   ", d))
    assert mgr.state.priorities.regions_for(d) == set()
    assert it.response.sent is not None  # 안내 메시지


def test_confirm_add_duplicate_noop(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    mgr.state.priorities.grant(["광주"], d, slot_cap=8)
    it = _Interaction(7, "서울) 운영자")
    asyncio.run(ctrl._confirm_add_priority(it, "광주", d))
    assert mgr.state.priorities.regions_for(d) == {"광주"}          # 중복 없음
    assert [e for e in mgr.state.audit.entries_for(d) if e.action == "grant"] == []
    assert it.response.sent is not None                             # 이미 있음 안내


def test_target_label_today(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    dd = date_cls.fromisoformat(d)
    assert ctrl._target_label(d) == f"{dd.month}/{dd.day} 오늘"


def test_target_label_tomorrow(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    nxt = _next(d)
    dd = date_cls.fromisoformat(nxt)
    assert ctrl._target_label(nxt) == f"{dd.month}/{dd.day} 내일"


def test_confirm_add_rejects_when_target_changed_by_draw(tmp_path):
    """패널을 D(오늘) 기준으로 연 뒤 추첨이 발생해 대상이 D+1로 바뀌면 추가를 거부한다."""
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    nxt = _next(d)
    # 오픈 시점: PENDING(target=D). 그 사이 추첨 발생해 DONE(target=D+1)
    mgr.state.draw_state.status = DrawStatus.DONE
    it = _Interaction(1, "광주) 홍길동")
    asyncio.run(ctrl._confirm_add_priority(it, "광주", d))  # opened_target=d(과거)
    assert mgr.state.priorities.regions_for(d) == set()
    assert mgr.state.priorities.regions_for(nxt) == set()
    assert mgr.state.audit.entries_for(d) == []


def test_removable_target_pending(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    mgr.state.priorities.grant(["광주"], d, slot_cap=8)
    target, regions = ctrl._removable_priority()
    assert target == d
    assert regions == ["광주"]


def test_removable_target_after_draw(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    nxt = _next(d)
    mgr.state.draw_state.status = DrawStatus.DONE
    mgr.state.priorities.grant(["대전"], nxt, slot_cap=8)
    target, regions = ctrl._removable_priority()
    assert target == nxt
    assert regions == ["대전"]


def test_confirm_remove_after_draw_targets_next_day(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    nxt = _next(d)
    mgr.state.draw_state.status = DrawStatus.DONE
    mgr.state.priorities.grant(["대전"], nxt, slot_cap=8)
    it = _Interaction(42, "서울) 운영자")
    asyncio.run(ctrl._confirm_remove_priority(it, "대전", nxt))
    assert mgr.state.priorities.regions_for(nxt) == set()
    # D+1 리셋 뒤에도 남도록 대상 일자로 기록
    assert len(mgr.state.audit.entries_for(nxt)) == 1


def test_confirm_remove_rejects_when_target_changed_by_draw(tmp_path):
    """패널을 D(오늘) 기준으로 연 뒤 추첨이 발생해 대상이 D+1로 바뀌면 제거를 거부한다."""
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    nxt = _next(d)
    # 패널 오픈 시점: PENDING, 오늘(D) 우선권 광주 존재
    mgr.state.priorities.grant(["광주"], d, slot_cap=8)
    # 그 사이 추첨 발생해 DONE, 내일(D+1)에 우연히 같은 지역 우선권 발급
    mgr.state.draw_state.status = DrawStatus.DONE
    mgr.state.priorities.grant(["광주"], nxt, slot_cap=8)
    it = _Interaction(1, "광주) 홍길동")
    # 오픈 시점 캡처값은 D 였음
    asyncio.run(ctrl._confirm_remove_priority(it, "광주", d))
    # D+1 우선권은 건드리지 않아야 함 (엉뚱한 일자 무음 삭제 방지)
    assert mgr.state.priorities.regions_for(nxt) == {"광주"}
    # 제거 로그도 남지 않음
    assert mgr.state.audit.entries_for(d) == []
