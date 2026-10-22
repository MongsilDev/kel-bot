"""LayoutView 빌더 헬퍼."""
from __future__ import annotations

import discord
from discord.ui import Container, LayoutView, TextDisplay


def _build(body: str, accent: discord.Color) -> LayoutView:
    view = LayoutView()
    view.add_item(Container(TextDisplay(content=body), accent_colour=accent))
    return view


def info_view(body: str) -> LayoutView:
    return _build(body, discord.Color.blurple())


def success_view(body: str) -> LayoutView:
    return _build(body, discord.Color.green())


def error_view(body: str) -> LayoutView:
    return _build(body, discord.Color.red())
