"""우선권 변경 로그(부여/제거): 당일 이력 영속화."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from models.storage import read_json, write_json
from utils.time import iso_now


@dataclass
class RemovalEntry:
    region: str
    actor_id: str
    actor_name: str
    removed_at: str  # ISO8601, 이벤트 발생 시각
    scrim_date: str
    action: str = "revoke"  # "grant"(부여) | "revoke"(제거)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RemovalEntry":
        return cls(
            region=data["region"],
            actor_id=data["actor_id"],
            actor_name=data["actor_name"],
            removed_at=data["removed_at"],
            scrim_date=data["scrim_date"],
            action=data.get("action", "revoke"),  # 구버전 로그는 제거로 간주
        )


@dataclass
class PriorityAuditStore:
    """우선권 제거 이벤트 로그. 당일분만 노출, 이전 일자는 purge."""

    path: Path
    entries: list[RemovalEntry] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "PriorityAuditStore":
        raw = read_json(path, default={"entries": []})
        items = [RemovalEntry.from_dict(e) for e in raw.get("entries", [])]
        return cls(path=path, entries=items)

    def save(self) -> None:
        write_json(self.path, {"entries": [e.to_dict() for e in self.entries]})

    def record(
        self,
        *,
        region: str,
        actor_id: str,
        actor_name: str,
        scrim_date: str,
        action: str = "revoke",
    ) -> RemovalEntry:
        entry = RemovalEntry(
            region=region,
            actor_id=actor_id,
            actor_name=actor_name,
            removed_at=iso_now(),
            scrim_date=scrim_date,
            action=action,
        )
        self.entries.append(entry)
        self.save()
        return entry

    def entries_for(self, scrim_date: str) -> list[RemovalEntry]:
        return sorted(
            (e for e in self.entries if e.scrim_date == scrim_date),
            key=lambda e: e.removed_at,
        )

    def purge_outdated(self, current_scrim_date: str) -> None:
        self.entries = [e for e in self.entries if e.scrim_date >= current_scrim_date]
        self.save()
