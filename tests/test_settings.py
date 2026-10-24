from config.settings import DEFAULT_PERMANENT_PRIORITY_REGIONS, _regions


def test_regions_default_when_unset(monkeypatch):
    monkeypatch.delenv("X_REGIONS", raising=False)
    assert _regions("X_REGIONS", DEFAULT_PERMANENT_PRIORITY_REGIONS) == frozenset(
        DEFAULT_PERMANENT_PRIORITY_REGIONS
    )
    assert len(DEFAULT_PERMANENT_PRIORITY_REGIONS) == 12


def test_regions_parses_comma_list(monkeypatch):
    monkeypatch.setenv("X_REGIONS", " 대전, 경북 ,, ")
    assert _regions("X_REGIONS", ("광주",)) == {"대전", "경북"}
