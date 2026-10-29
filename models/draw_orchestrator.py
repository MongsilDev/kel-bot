"""추첨 실행: 우선권 자동 확정 + 랜덤 추첨."""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from models.application import Application, ApplicationStatus, ApplicationStore
from models.draw_state import DrawState, DrawStatus
from models.priority import PriorityStore
from utils.time import iso_now

logger = logging.getLogger("kel-bot.draw_orchestrator")

# 이 팀 수 이상이면 8팀 선정 대신 두 중대로 나눠 전원 편성 (14팀 7/7, 15팀 8/7, 16팀 8/8)
TWO_COMPANY_TEAMS = 14


@dataclass
class DrawResult:
    selected: list[Application]
    rejected: list[Application]
    granted_priority_regions: list[str]  # D+1 우선권 발급된 지역
    groups: dict[str, list[Application]] | None = None  # 두 중대 모드(14~16팀): {"A": 1중대, "B": 2중대}
    granted_super_regions: list[str] = field(default_factory=list)  # D+1 최우선 발급된 지역


class DrawOrchestrator:
    """추첨 의사결정과 실행. 멱등성 보장."""

    def __init__(
        self,
        applications: ApplicationStore,
        priorities: PriorityStore,
        draw_state: DrawState,
        team_slots: int,
    ) -> None:
        self.applications = applications
        self.priorities = priorities
        self.draw_state = draw_state
        self.team_slots = team_slots

    # 판정 ---------------------------------------------------------------
    def total_active(self) -> int:
        return self.applications.count_active()

    def can_draw_now(self) -> bool:
        if self.draw_state.is_drawn():
            return False
        return self.total_active() >= self.team_slots

    def attempt_primary_draw(self) -> DrawResult | None:
        """00:30 1차 추첨: 8팀 이상이면 추첨, 미달이면 HELD."""
        if self.draw_state.is_drawn():
            return None
        if self.total_active() >= self.team_slots:
            return self._execute_draw()
        # 미달: 보류 모드 전환
        if self.draw_state.status != DrawStatus.HELD:
            self.draw_state.status = DrawStatus.HELD
            self.draw_state.save()
        return None

    def attempt_instant_draw(self) -> DrawResult | None:
        """보류(HELD) 상태에서 8팀 도달 시 즉시 추첨.

        00:30 1차 추첨 이전(PENDING)에는 8팀이 차도 추첨하지 않음. 1차 추첨 시각까지 대기.
        """
        if self.draw_state.status is not DrawStatus.HELD:
            return None
        if self.total_active() >= self.team_slots:
            return self._execute_draw()
        return None

    def force_deadline_cancel(self) -> bool:
        """17:00 데드라인: 미실행이면 취소 확정, 우선권 소멸."""
        if self.draw_state.deadline_processed:
            return False
        self.draw_state.deadline_processed = True
        if not self.draw_state.is_drawn():
            self.draw_state.status = DrawStatus.CANCELLED
            # 오늘 일자 우선권 모두 소멸 (이월 없음)
            self.priorities.expire_for(self.draw_state.scrim_date)
            self.draw_state.save()
            return True
        self.draw_state.save()
        return False

    # 실행 ---------------------------------------------------------------
    def _execute_draw(self) -> DrawResult:
        scrim_date = self.draw_state.scrim_date
        team_slots = self.team_slots
        two_group_threshold = team_slots * 2  # 16
        active = self.applications.active()

        # 우선권 자동 확정 (had_priority 갱신)
        priority_regions = self.priorities.regions_for(scrim_date)
        super_regions = self.priorities.super_regions_for(scrim_date)
        priority_apps = [a for a in active if a.region in priority_regions]
        for app in priority_apps:
            app.had_priority = True
        # 신청 뒤 최우선이 제거됐을 수 있어 추첨 시점 기준으로 다시 정함
        for app in active:
            app.had_super = app.region in super_regions

        groups: dict[str, list[Application]] | None = None

        if len(active) >= TWO_COMPANY_TEAMS:
            # 14팀 이상: 두 중대 랜덤 분할, 16팀까지 전원 선정 (탈락 없음, 우선권 미발급).
            # 1중대는 올림(많은 쪽), 2중대는 나머지. 정원 초과분(17+, 이론상 X)만 탈락.
            #   14팀 7/7, 15팀 8/7, 16팀 8/8
            pool = list(active)
            random.shuffle(pool)
            seated = pool[:two_group_threshold]  # 최대 16팀 착석
            a_size = min(team_slots, (len(seated) + 1) // 2)
            group_a = seated[:a_size]
            group_b = seated[a_size:]
            # seated가 이미 셔플돼 조 내 순서(draw_order)도 무작위. 표시 순서로 고정.
            for i, a in enumerate(group_a):
                a.group = "A"
                a.draw_order = i
            for i, a in enumerate(group_b):
                a.group = "B"
                a.draw_order = i
            selected = group_a + group_b
            rejected = pool[two_group_threshold:]  # 17+ 케이스 (이론상 발생 X)
            groups = {"A": group_a, "B": group_b}
        else:
            super_apps = [a for a in priority_apps if a.region in super_regions]
            rest_priority = [a for a in priority_apps if a.region not in super_regions]
            normal_apps = [a for a in active if a.region not in priority_regions]
            if len(super_apps) >= team_slots:
                super_pool = list(super_apps)
                random.shuffle(super_pool)
                selected = super_pool[:team_slots]
                rejected = super_pool[team_slots:] + rest_priority + normal_apps
            else:
                selected, rejected = self._pick(
                    rest_priority, normal_apps, team_slots - len(super_apps)
                )
                selected = super_apps + selected
            # 1개조(단일 8팀) 모드: 선정 팀 표시 순서(번호)도 무작위로 고정
            draw_seq = list(selected)
            random.shuffle(draw_seq)
            for i, a in enumerate(draw_seq):
                a.draw_order = i

        self.applications.mark_status((a.team_id for a in selected), ApplicationStatus.SELECTED)
        self.applications.mark_status((a.team_id for a in rejected), ApplicationStatus.REJECTED)

        # 실제 신청하고 확정된 우선권만 소비 처리
        applied_priority_regions = {a.region for a in selected if a.region in priority_regions}
        for region in applied_priority_regions:
            self.priorities.consume(region, scrim_date)

        # 다음날(D+1) 발급: 우선권이 있던 탈락 팀은 최우선, 일반 탈락 팀은 신청 시각 빠른 순 상위 8에 우선권
        rejected_sorted = sorted(rejected, key=lambda a: a.applied_at)
        next_date = (date.fromisoformat(scrim_date) + timedelta(days=1)).isoformat()
        if groups is None:
            super_targets = [a.region for a in rejected_sorted if a.region in priority_regions]
            normal_targets = [a.region for a in rejected_sorted if a.region not in priority_regions]
        else:
            super_targets, normal_targets = [], [a.region for a in rejected_sorted]
        granted_super_regions: list[str] = []
        granted_regions: list[str] = []
        if super_targets:
            granted_super = self.priorities.grant_super(super_targets, next_date)
            granted_super_regions = [p.region for p in granted_super]
        if normal_targets:
            granted = self.priorities.grant(normal_targets, next_date, slot_cap=team_slots)
            granted_regions = [p.region for p in granted]

        # 추첨 상태 갱신
        self.draw_state.status = DrawStatus.DONE
        self.draw_state.drawn_at = iso_now()
        self.draw_state.save()

        logger.info(
            "추첨 완료 scrim_date=%s selected=%d rejected=%d 우선권발급=%d 최우선발급=%d groups=%s",
            scrim_date,
            len(selected),
            len(rejected),
            len(granted_regions),
            len(granted_super_regions),
            "2" if groups else "1",
        )

        return DrawResult(
            selected=selected,
            rejected=rejected,
            granted_priority_regions=granted_regions,
            groups=groups,
            granted_super_regions=granted_super_regions,
        )

    @staticmethod
    def _pick(
        priority_apps: list[Application], normal_apps: list[Application], slots: int
    ) -> tuple[list[Application], list[Application]]:
        """우선권 팀을 먼저 채우고 남은 자리는 일반 팀 무작위. 우선권 팀이 자리 이상이면 그 팀끼리 무작위."""
        if len(priority_apps) >= slots:
            # 상시와 하루 우선권 구분 없음
            priority_pool = list(priority_apps)
            random.shuffle(priority_pool)
            return priority_pool[:slots], priority_pool[slots:] + normal_apps
        random_pool = list(normal_apps)
        random.shuffle(random_pool)
        remaining = slots - len(priority_apps)
        return priority_apps + random_pool[:remaining], random_pool[remaining:]
