"""최우선 등급: 저장, 추첨, 유효 기간, 화면."""
import asyncio
import json
import random

from commands.ui.views import DashboardSnapshot, DashboardView, PriorityManageView, _format_pending_lines
from models.application import Application, ApplicationStatus, ApplicationStore
from models.draw_orchestrator import DrawOrchestrator
from models.draw_state import DrawState, DrawStatus
from models.priority import Priority, PriorityStore
from tests.test_dashboard_remove import _controller, _controller_with_permanent, _Interaction

D = "2026-05-30"
NEXT = "2026-05-31"


def _apps(regions, date=D):
    return [
        Application(
            team_id=f"{date}-{r}",
            region=r,
            applicant_id=f"u{r}",
            applicant_display=f"{r}) name",
            applied_at=f"2026-05-29T{i:02d}:00:00+09:00",
            scrim_date=date,
            status=ApplicationStatus.PENDING,
        )
        for i, r in enumerate(regions)
    ]


def _draw(tmp_path, pri, regions, date=D):
    apps = ApplicationStore(path=tmp_path / f"a-{date}.json", scrim_date=date, applications=_apps(regions, date))
    ds = DrawState.load(tmp_path / f"d-{date}.json", date)
    return DrawOrchestrator(apps, pri, ds, team_slots=8)


def _regions(n):
    return [f"R{i}" for i in range(n)]


# 저장 -----------------------------------------------------------------
def test_tier_default_and_old_json(tmp_path):
    assert Priority(region="광주", granted_at="x", valid_for_date=D).tier == "normal"
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"priorities": [
        {"region": "광주", "granted_at": "x", "valid_for_date": D, "consumed": False}
    ]}))
    store = PriorityStore.load(path)
    assert store.priorities[0].tier == "normal"
    assert store.super_regions_for(D) == set()


def test_application_had_super_old_json():
    app = Application.from_dict({
        "team_id": "t", "region": "광주", "applicant_id": "1",
        "applicant_display": "광주) a", "applied_at": "x", "scrim_date": D,
    })
    assert app.had_super is False


def test_super_regions_for_and_inclusion(tmp_path):
    store = PriorityStore(path=tmp_path / "p.json")
    store.grant(["부산"], D, slot_cap=8)
    store.grant_super(["광주"], D)
    store.grant_super(["대구"], NEXT)
    assert store.super_regions_for(D) == {"광주"}
    assert store.regions_for(D) == {"광주", "부산"}
    assert store.dated_regions_for(D) == {"광주", "부산"}
    assert store.has("광주", D)
    store.consume("광주", D)
    assert store.super_regions_for(D) == set()


def test_grant_super_upgrades_normal_and_skips_duplicate(tmp_path):
    store = PriorityStore(path=tmp_path / "p.json")
    store.grant(["광주"], D, slot_cap=8)
    granted = store.grant_super(["광주", "부산"], D)
    assert [p.region for p in granted] == ["광주", "부산"]
    assert len(store.list_for(D)) == 2
    assert store.grant_super(["광주"], D) == []
    assert len(store.list_for(D)) == 2
    assert PriorityStore.load(tmp_path / "p.json").super_regions_for(D) == {"광주", "부산"}


def test_grant_super_ignores_cap_and_includes_permanent(tmp_path):
    store = PriorityStore(path=tmp_path / "p.json", permanent=frozenset({"대전"}))
    regions = ["대전"] + _regions(9)
    granted = store.grant_super(regions, D)
    assert len(granted) == 10
    assert store.super_regions_for(D) == set(regions)


# 추첨 -----------------------------------------------------------------
def test_super_selected_before_priority(tmp_path):
    for seed in range(10):
        random.seed(seed)
        base = tmp_path / str(seed)
        base.mkdir()
        pri = PriorityStore.load(base / "p.json", frozenset({f"R{i}" for i in range(6)}))
        pri.grant([f"R{i}" for i in range(6, 10)], D, slot_cap=8)
        pri.grant_super(["R10", "R11"], D)
        res = _draw(base, pri, _regions(12))._execute_draw()
        selected = {a.region for a in res.selected}
        assert {"R10", "R11"} <= selected
        assert all(a.had_super and a.had_priority for a in res.selected if a.region in ("R10", "R11"))
        assert not any(a.had_super for a in res.selected if a.region not in ("R10", "R11"))


def test_rejected_normal_gets_priority_only(tmp_path):
    random.seed(0)
    pri = PriorityStore.load(tmp_path / "p.json")
    pri.grant(["R0", "R1", "R2"], D, slot_cap=8)
    res = _draw(tmp_path, pri, ["R0", "R1", "R2"] + [f"N{i}" for i in range(10)])._execute_draw()
    rejected = {a.region for a in res.rejected}
    assert res.granted_super_regions == []
    assert set(res.granted_priority_regions) == rejected
    assert pri.super_regions_for(NEXT) == set()


def test_rejected_dated_and_permanent_get_super(tmp_path):
    random.seed(0)
    permanent = frozenset({f"P{i}" for i in range(5)})
    pri = PriorityStore.load(tmp_path / "p.json", permanent)
    dated = [f"R{i}" for i in range(5)]
    pri.grant(dated, D, slot_cap=8)
    res = _draw(tmp_path, pri, sorted(permanent) + dated + ["N0", "N1"])._execute_draw()
    rejected = {a.region for a in res.rejected}
    assert {"N0", "N1"} <= rejected
    assert set(res.granted_super_regions) == rejected - {"N0", "N1"}
    assert set(res.granted_priority_regions) == {"N0", "N1"}
    assert set(res.granted_super_regions).isdisjoint(res.granted_priority_regions)
    assert pri.super_regions_for(NEXT) == rejected - {"N0", "N1"}


def test_rejected_permanent_gets_super(tmp_path):
    random.seed(0)
    permanent = frozenset(_regions(10))
    pri = PriorityStore.load(tmp_path / "p.json", permanent)
    res = _draw(tmp_path, pri, _regions(10))._execute_draw()
    rejected = {a.region for a in res.rejected}
    assert len(rejected) == 2
    assert set(res.granted_super_regions) == rejected
    assert res.granted_priority_regions == []
    assert pri.super_regions_for(NEXT) == rejected


def test_no_region_gets_both_tiers(tmp_path):
    random.seed(0)
    pri = PriorityStore.load(tmp_path / "p.json")
    pri.grant(_regions(6), D, slot_cap=8)
    res = _draw(tmp_path, pri, _regions(13))._execute_draw()
    assert set(res.granted_super_regions).isdisjoint(res.granted_priority_regions)
    for region in {p.region for p in pri.list_for(NEXT)}:
        assert len([p for p in pri.list_for(NEXT) if p.region == region]) == 1


def test_all_priority_13_then_rejected_all_selected_next_day(tmp_path):
    random.seed(0)
    pri = PriorityStore.load(tmp_path / "p.json", frozenset(_regions(13)))
    res = _draw(tmp_path, pri, _regions(13))._execute_draw()
    rejected = {a.region for a in res.rejected}
    assert len(rejected) == 5
    assert set(res.granted_super_regions) == rejected
    random.seed(1)
    (tmp_path / "next").mkdir()
    nxt = _draw(tmp_path / "next", pri, _regions(13), date=NEXT)._execute_draw()
    assert rejected <= {a.region for a in nxt.selected}
    assert all(a.had_super for a in nxt.selected if a.region in rejected)


def test_14_teams_unchanged_by_super(tmp_path):
    pri = PriorityStore.load(tmp_path / "p.json")
    pri.grant_super(["R0", "R1"], D)
    res = _draw(tmp_path, pri, _regions(14))._execute_draw()
    assert res.groups is not None
    assert len(res.selected) == 14
    assert res.rejected == []
    assert res.granted_super_regions == [] and res.granted_priority_regions == []
    assert not any(a.had_super for a in res.selected)


# 유효 기간 --------------------------------------------------------------
def test_super_expires_after_next_day_cancel(tmp_path):
    random.seed(0)
    pri = PriorityStore.load(tmp_path / "p.json", frozenset(_regions(10)))
    _draw(tmp_path, pri, _regions(10))._execute_draw()
    assert pri.super_regions_for(NEXT)
    (tmp_path / "next").mkdir()
    nxt = _draw(tmp_path / "next", pri, ["R0"], date=NEXT)
    assert nxt.force_deadline_cancel() is True
    assert pri.super_regions_for(NEXT) == set()
    assert pri.dated_regions_for(NEXT) == set()
    pri.purge_outdated("2026-06-01")
    assert pri.super_regions_for("2026-06-01") == set()


# 화면 -----------------------------------------------------------------
def _snapshot(apps, priority, super_regions, status=DrawStatus.PENDING, next_priority=(), next_super=()):
    return DashboardSnapshot(
        scrim_date=D,
        team_slots=8,
        applications=apps,
        priority_regions=set(priority),
        next_day_priority_regions=set(next_priority),
        draw_status=status,
        drawn_at=None,
        application_open=True,
        super_regions=set(super_regions),
        next_day_super_regions=set(next_super),
    )


async def _noop(*a):
    pass


def _texts(view):
    out = []
    for item in view.walk_children():
        content = getattr(item, "content", None)
        if isinstance(content, str):
            out.append(content)
    return "\n".join(out)


def _view(snapshot):
    async def build():
        return DashboardView(snapshot, _noop, _noop, _noop)
    return asyncio.run(build())


def test_header_super_line_and_priority_dedup():
    text = _texts(_view(_snapshot([], {"광주", "대전", "부산"}, {"대전"})))
    lines = text.splitlines()
    i = lines.index("🌟 최우선: 대전")
    assert lines[i + 1] == "⭐ 우선권: 광주, 부산"


def test_header_omits_priority_line_when_only_super():
    text = _texts(_view(_snapshot([], {"대전"}, {"대전"})))
    assert "🌟 최우선: 대전" in text
    assert "⭐ 우선권" not in text


def test_next_day_super_line_after_draw():
    text = _texts(_view(_snapshot(
        [], set(), set(), status=DrawStatus.DONE, next_priority={"광주", "대전"}, next_super={"대전"}
    )))
    lines = text.splitlines()
    i = lines.index("-# 내일 최우선: 대전")
    assert lines[i + 1] == "-# 내일 우선권: 광주"


def test_pending_markers():
    apps = _apps(["대전", "광주", "부산"])
    out = _format_pending_lines(_snapshot(apps, {"대전", "광주"}, {"대전"})).splitlines()
    assert out[0] == "` 1.` 🌟 대전) name"
    assert out[1] == "` 2.` ★ 광주) name"
    assert out[2] == "` 3.` 　 부산) name"


def test_selected_markers():
    apps = _apps(["대전", "광주", "부산"])
    for a in apps:
        a.status = ApplicationStatus.SELECTED
    apps[0].had_priority = apps[0].had_super = True
    apps[1].had_priority = True
    text = _texts(_view(_snapshot(apps, set(), set(), status=DrawStatus.DONE)))
    assert "` 1.` 🌟 대전) name" in text
    assert "` 2.` ★ 광주) name" in text


def test_manage_panel_labels():
    async def build():
        return PriorityManageView(
            target_label="5/30 오늘",
            removable_regions=["광주", "대전"],
            super_regions={"대전"},
            on_add=_noop,
            on_remove_select=_noop,
        )
    view = asyncio.run(build())
    assert "현재 광주, 대전 최우선" in _texts(view)
    assert [(o.label, o.value) for o in view.select.options] == [("광주", "광주"), ("대전 최우선", "대전")]


def test_handle_manage_passes_super_labels(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    mgr.state.priorities.grant_super(["대전"], d)
    it = _Interaction(1, "광주) 홍길동")
    asyncio.run(ctrl.handle_manage_priority(it))
    view = it.response.sent["view"]
    assert [(o.label, o.value) for o in view.select.options] == [("대전 최우선", "대전")]


def _apply(tmp_path, ctrl, display):
    ctrl._is_application_window_open = lambda _d: True
    it = _Interaction(5, display)
    asyncio.run(ctrl.handle_apply(it))
    return it, _texts(it.response.sent["view"])


def test_apply_super_message_and_flag(tmp_path):
    ctrl, mgr = _controller_with_permanent(tmp_path, {"대전"})
    d = mgr.state.draw_state.scrim_date
    mgr.state.priorities.grant_super(["대전"], d)
    _, text = _apply(tmp_path, ctrl, "대전) 홍길동")
    assert text == "`대전` 지역으로 신청했습니다.\n최우선이 적용되었습니다."
    app = mgr.state.applications.by_region("대전")
    assert app.had_super is True and app.had_priority is True


def test_apply_priority_message_unchanged(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    mgr.state.priorities.grant(["광주"], d, slot_cap=8)
    _, text = _apply(tmp_path, ctrl, "광주) 홍길동")
    assert text == "`광주` 지역으로 신청했습니다.\n우선권이 적용되었습니다."
    assert mgr.state.applications.by_region("광주").had_super is False


def test_snapshot_fills_super_sets(tmp_path):
    ctrl, mgr = _controller(tmp_path)
    d = mgr.state.draw_state.scrim_date
    from datetime import date as dc, timedelta
    nxt = (dc.fromisoformat(d) + timedelta(days=1)).isoformat()
    mgr.state.priorities.grant_super(["대전"], d)
    mgr.state.priorities.grant_super(["광주"], nxt)
    snap = ctrl._current_snapshot()
    assert snap.super_regions == {"대전"}
    assert snap.next_day_super_regions == {"광주"}
