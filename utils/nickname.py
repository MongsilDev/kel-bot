"""닉네임 `지역) 닉네임` 형식 파싱과 검증."""
from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERN = re.compile(r"^\s*(?P<region>[^)\s][^)]*?)\s*\)\s*(?P<name>\S.*)$")


class NicknameFormatError(ValueError):
    """`지역) 닉네임` 형식 위반."""


@dataclass(frozen=True)
class ParsedNickname:
    region: str
    name: str
    raw: str


def parse(display_name: str) -> ParsedNickname:
    """Discord 표시 닉네임에서 (region, name) 추출.

    형식: `<지역>) <닉네임>`. 지역 뒤 `)` + 공백 + 닉네임.
    """
    if not display_name:
        raise NicknameFormatError("서버 닉네임이 설정되지 않았습니다.")
    m = _PATTERN.match(display_name)
    if not m:
        raise NicknameFormatError(
            "서버 닉네임을 `지역) 닉네임` 형식으로 설정해주세요."
        )
    region = m.group("region").strip()
    name = m.group("name").strip()
    if not region:
        raise NicknameFormatError("지역명이 누락되었습니다.")
    if not name:
        raise NicknameFormatError("닉네임이 누락되었습니다.")
    return ParsedNickname(region=region, name=name, raw=display_name)
