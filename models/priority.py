"""우선권(Priority) 데이터 모델 + 매니저."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from models.storage import read_json, write_json
from utils.time import iso_now


@dataclass
class Priority:
    region: str
    granted_at: str
    valid_for_date: str  # YYYY-MM-DD
    consumed: bool = False
    tier: str = "normal"  # "normal" | "super"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Priority":
        return cls(
            region=data["region"],
            granted_at=data["granted_at"],
            valid_for_date=data["valid_for_date"],
            consumed=data.get("consumed", False),
            tier=data.get("tier", "normal"),
        )


@dataclass
class PriorityStore:
    path: Path
    priorities: list[Priority] = field(default_factory=list)
    permanent: frozenset[str] = frozenset()  # 설정값, 파일에 저장하지 않음

    @classmethod
    def load(cls, path: Path, permanent: frozenset[str] = frozenset()) -> "PriorityStore":
        raw = read_json(path, default={"priorities": []})
        items = [Priority.from_dict(p) for p in raw.get("priorities", [])]
        store = cls(path=path, priorities=items, permanent=permanent)
        return store

    def save(self) -> None:
        write_json(
            self.path,
            {"priorities": [p.to_dict() for p in self.priorities]},
        )

    # 조회 ---------------------------------------------------------------
    def regions_for(self, scrim_date: str) -> set[str]:
        return self.dated_regions_for(scrim_date) | self.permanent

    def dated_regions_for(self, scrim_date: str) -> set[str]:
        """상시 우선권을 뺀 해당 일자 활성 우선권. 수동 제거 대상."""
        return {p.region for p in self.priorities if p.valid_for_date == scrim_date and not p.consumed}

    def super_regions_for(self, scrim_date: str) -> set[str]:
        return {
            p.region
            for p in self.priorities
            if p.valid_for_date == scrim_date and not p.consumed and p.tier == "super"
        }

    def has(self, region: str, scrim_date: str) -> bool:
        return region in self.regions_for(scrim_date)

    def list_for(self, scrim_date: str) -> list[Priority]:
        return [p for p in self.priorities if p.valid_for_date == scrim_date]

    # 변경 ---------------------------------------------------------------
    def grant(self, regions: list[str], valid_for_date: str, slot_cap: int) -> list[Priority]:
        """탈락 지역에 우선권 발급. 상시 우선권 지역은 건너뛰고, 정원(slot_cap) 초과 시 앞쪽만 발급."""
        granted: list[Priority] = []
        for region in [r for r in regions if r not in self.permanent][:slot_cap]:
            p = Priority(
                region=region,
                granted_at=iso_now(),
                valid_for_date=valid_for_date,
                consumed=False,
            )
            self.priorities.append(p)
            granted.append(p)
        self.save()
        return granted

    def grant_super(self, regions: list[str], valid_for_date: str) -> list[Priority]:
        """최우선 발급. 정원 제한 없고 상시 우선권 지역에도 발급. 같은 날 활성 일반 항목은 승급."""
        granted: list[Priority] = []
        for region in regions:
            active = [
                p for p in self.priorities
                if p.region == region and p.valid_for_date == valid_for_date and not p.consumed
            ]
            if any(p.tier == "super" for p in active):
                continue
            if active:
                active[0].tier = "super"
                granted.append(active[0])
                continue
            p = Priority(
                region=region,
                granted_at=iso_now(),
                valid_for_date=valid_for_date,
                consumed=False,
                tier="super",
            )
            self.priorities.append(p)
            granted.append(p)
        self.save()
        return granted

    def grant_one(self, region: str, valid_for_date: str) -> bool:
        """단일 지역 수동 우선권 발급(정원 무관). 이미 활성이면 False."""
        if self.has(region, valid_for_date):
            return False
        self.priorities.append(
            Priority(
                region=region,
                granted_at=iso_now(),
                valid_for_date=valid_for_date,
                consumed=False,
            )
        )
        self.save()
        return True

    def consume(self, region: str, valid_for_date: str) -> None:
        for p in self.priorities:
            if p.region == region and p.valid_for_date == valid_for_date and not p.consumed:
                p.consumed = True
        self.save()

    def revoke(self, region: str, valid_for_date: str) -> bool:
        """지역 단위 활성(미consumed) 우선권 제거. 반환: 제거 여부."""
        before = len(self.priorities)
        self.priorities = [
            p
            for p in self.priorities
            if not (p.region == region and p.valid_for_date == valid_for_date and not p.consumed)
        ]
        removed = len(self.priorities) < before
        if removed:
            self.save()
        return removed

    def expire_for(self, scrim_date: str) -> int:
        """주어진 일자 우선권 모두 제거 (소멸). 반환: 제거 수."""
        before = len(self.priorities)
        self.priorities = [p for p in self.priorities if p.valid_for_date != scrim_date]
        self.save()
        return before - len(self.priorities)

    def purge_outdated(self, current_scrim_date: str) -> None:
        """current 이전 일자 우선권 제거 (안전 정리)."""
        self.priorities = [p for p in self.priorities if p.valid_for_date >= current_scrim_date]
        self.save()
