from models.priority import Priority, PriorityStore

DATE = "2026-05-30"


def _p(region, date=DATE, consumed=False):
    return Priority(
        region=region,
        granted_at="2026-05-29T00:00:00+09:00",
        valid_for_date=date,
        consumed=consumed,
    )


def _store(tmp_path, priorities):
    return PriorityStore(path=tmp_path / "p.json", priorities=priorities)


def test_revoke_removes_active_priority(tmp_path):
    store = _store(tmp_path, [_p("광주"), _p("부산")])
    assert store.revoke("광주", DATE) is True
    assert store.regions_for(DATE) == {"부산"}


def test_revoke_returns_false_when_no_priority(tmp_path):
    store = _store(tmp_path, [_p("부산")])
    assert store.revoke("대구", DATE) is False
    assert store.regions_for(DATE) == {"부산"}


def test_revoke_ignores_consumed(tmp_path):
    store = _store(tmp_path, [_p("광주", consumed=True)])
    assert store.revoke("광주", DATE) is False


def test_revoke_scoped_to_date(tmp_path):
    other = "2026-05-31"
    store = _store(tmp_path, [_p("광주", date=other)])
    assert store.revoke("광주", DATE) is False
    assert store.regions_for(other) == {"광주"}


def test_grant_one_adds_active_priority(tmp_path):
    store = _store(tmp_path, [])
    assert store.grant_one("광주", DATE) is True
    assert store.regions_for(DATE) == {"광주"}


def test_grant_one_rejects_duplicate_active(tmp_path):
    store = _store(tmp_path, [_p("광주")])
    assert store.grant_one("광주", DATE) is False
    assert store.regions_for(DATE) == {"광주"}


def test_grant_one_scoped_to_date(tmp_path):
    other = "2026-05-31"
    store = _store(tmp_path, [_p("광주", date=other)])
    # 다른 날짜엔 활성 우선권이 없으므로 발급 성공
    assert store.grant_one("광주", DATE) is True
    assert store.regions_for(DATE) == {"광주"}
    assert store.regions_for(other) == {"광주"}


def test_grant_one_readds_after_consumed(tmp_path):
    # 이미 소비된 우선권이 있어도 활성으로 재발급 가능
    store = _store(tmp_path, [_p("광주", consumed=True)])
    assert store.grant_one("광주", DATE) is True
    assert store.regions_for(DATE) == {"광주"}


PERMANENT = frozenset({"대전", "경북"})


def _perm_store(tmp_path, priorities):
    return PriorityStore(path=tmp_path / "p.json", priorities=priorities, permanent=PERMANENT)


def test_permanent_included_every_day(tmp_path):
    store = _perm_store(tmp_path, [_p("광주")])
    assert store.regions_for(DATE) == {"광주", "대전", "경북"}
    assert store.regions_for("2026-12-31") == {"대전", "경북"}
    assert store.has("대전", "2026-12-31")
    assert store.dated_regions_for(DATE) == {"광주"}


def test_permanent_survives_consume_revoke_expire_purge(tmp_path):
    store = _perm_store(tmp_path, [])
    store.consume("대전", DATE)
    assert store.revoke("대전", DATE) is False
    store.expire_for(DATE)
    store.purge_outdated("2026-06-30")
    assert store.has("대전", DATE)


def test_permanent_not_saved(tmp_path):
    store = _perm_store(tmp_path, [])
    store.save()
    assert PriorityStore.load(tmp_path / "p.json").regions_for(DATE) == set()


def test_grant_skips_permanent_before_cap(tmp_path):
    store = _perm_store(tmp_path, [])
    granted = store.grant(["대전", "광주", "경북", "부산"], DATE, slot_cap=2)
    assert [p.region for p in granted] == ["광주", "부산"]
    assert store.dated_regions_for(DATE) == {"광주", "부산"}


def test_grant_one_rejects_permanent(tmp_path):
    store = _perm_store(tmp_path, [])
    assert store.grant_one("대전", DATE) is False
    assert store.dated_regions_for(DATE) == set()
