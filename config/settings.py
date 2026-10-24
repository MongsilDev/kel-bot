"""환경변수 + 상수."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

load_dotenv(PROJECT_ROOT / ".env")

# 대전 진출 12팀 지역
DEFAULT_PERMANENT_PRIORITY_REGIONS = (
    "경남", "성남", "충남", "강원", "전남", "대구", "세종", "경기",
    "부산", "대전", "인천", "경북",
)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"환경변수 누락: {name}")
    return value


def _optional_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _regions(name: str, default: tuple[str, ...]) -> frozenset[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset(default)
    return frozenset(r.strip() for r in raw.split(",") if r.strip())


@dataclass(frozen=True)
class Settings:
    discord_token: str
    apply_channel_id: int
    reset_hour: int
    draw_hour: int
    draw_minute: int
    deadline_hour: int
    team_slots: int
    data_dir: Path
    permanent_priority_regions: frozenset[str] = frozenset()


def load_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings(
        discord_token=_required("DISCORD_TOKEN"),
        apply_channel_id=int(_required("APPLY_CHANNEL_ID")),
        reset_hour=_optional_int("RESET_HOUR", 21),
        draw_hour=_optional_int("DRAW_HOUR", 0),
        draw_minute=_optional_int("DRAW_MINUTE", 30),
        deadline_hour=_optional_int("DEADLINE_HOUR", 17),
        team_slots=_optional_int("TEAM_SLOTS", 8),
        data_dir=DATA_DIR,
        permanent_priority_regions=_regions(
            "PERMANENT_PRIORITY_REGIONS", DEFAULT_PERMANENT_PRIORITY_REGIONS
        ),
    )


SETTINGS = load_settings()
