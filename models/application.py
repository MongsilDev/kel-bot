"""신청 데이터 모델 + 매니저."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from models.storage import read_json, write_json
from utils.time import iso_now


class ApplicationStatus(str, Enum):
    PENDING = "pending"
    SELECTED = "selected"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ApplicationError(Exception):
    """신청 유효성/중복 오류."""


@dataclass
class Application:
    team_id: str
    region: str
    applicant_id: str
    applicant_display: str
    applied_at: str
    scrim_date: str
    had_priority: bool = False
    status: ApplicationStatus = ApplicationStatus.PENDING
    group: str | None = None  # 두 중대 편성 시 "A"(1중대) / "B"(2중대)
    draw_order: int | None = None  # 추첨 시 조 내 무작위 표시 순서 (셔플 결과 고정)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Application":
        return cls(
            team_id=data["team_id"],
            region=data["region"],
            applicant_id=data["applicant_id"],
            applicant_display=data["applicant_display"],
            applied_at=data["applied_at"],
            scrim_date=data["scrim_date"],
            had_priority=data.get("had_priority", False),
            status=ApplicationStatus(data.get("status", "pending")),
            group=data.get("group"),
            draw_order=data.get("draw_order"),
        )


@dataclass
class ApplicationStore:
    """현재 일자(`scrim_date`) 신청 목록 관리. JSON 영속화."""

    path: Path
    scrim_date: str
    applications: list[Application] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, scrim_date_hint: str) -> "ApplicationStore":
        """저장된 데이터가 hint보다 신/동일하면 보존, 과거면 hint로 리셋."""
        raw = read_json(path, default=None)
        if raw and raw.get("scrim_date") and raw["scrim_date"] >= scrim_date_hint:
            stored = raw["scrim_date"]
            apps = [Application.from_dict(item) for item in raw.get("applications", [])]
            return cls(path=path, scrim_date=stored, applications=apps)
        store = cls(path=path, scrim_date=scrim_date_hint, applications=[])
        store.save()
        return store

    def save(self) -> None:
        write_json(
            self.path,
            {
                "scrim_date": self.scrim_date,
                "applications": [app.to_dict() for app in self.applications],
            },
        )

    def reset(self, new_scrim_date: str) -> None:
        self.scrim_date = new_scrim_date
        self.applications = []
        self.save()

    # 조회 ---------------------------------------------------------------
    def active(self) -> list[Application]:
        return [a for a in self.applications if a.status != ApplicationStatus.CANCELLED]

    def by_user(self, applicant_id: str) -> Application | None:
        for app in self.active():
            if app.applicant_id == applicant_id:
                return app
        return None

    def by_region(self, region: str) -> Application | None:
        for app in self.active():
            if app.region == region:
                return app
        return None

    def count_active(self) -> int:
        return len(self.active())

    # 변경 ---------------------------------------------------------------
    def add(
        self,
        *,
        region: str,
        applicant_id: str,
        applicant_display: str,
        had_priority: bool = False,
    ) -> Application:
        if self.by_user(applicant_id):
            raise ApplicationError("이미 신청했습니다.")
        if self.by_region(region):
            raise ApplicationError(f"`{region}` 지역은 이미 신청되어 있습니다.")
        app = Application(
            team_id=str(uuid.uuid4()),
            region=region,
            applicant_id=applicant_id,
            applicant_display=applicant_display,
            applied_at=iso_now(),
            scrim_date=self.scrim_date,
            had_priority=had_priority,
        )
        self.applications.append(app)
        self.save()
        return app

    def cancel_by_user(self, applicant_id: str) -> Application:
        app = self.by_user(applicant_id)
        if not app:
            raise ApplicationError("등록된 신청이 없습니다.")
        if app.status is not ApplicationStatus.PENDING:
            raise ApplicationError("추첨이 완료되어 취소할 수 없습니다.")
        app.status = ApplicationStatus.CANCELLED
        self.save()
        return app

    def mark_status(self, team_ids: Iterable[str], status: ApplicationStatus) -> None:
        wanted = set(team_ids)
        for app in self.applications:
            if app.team_id in wanted:
                app.status = status
        self.save()
