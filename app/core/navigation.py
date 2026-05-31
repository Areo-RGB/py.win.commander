from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NavItem:
    label: str
    route: str
    icon: str
    description: str = ""


@dataclass(frozen=True)
class NavGroup:
    label: str
    icon: str
    value: str
    items: tuple[NavItem, ...] = field(default_factory=tuple)


NAV_GROUPS: tuple[NavGroup, ...] = (
    NavGroup(
        label="Home",
        icon="dashboard",
        value="home",
        items=(
            NavItem("Dashboard", "/", "space_dashboard", "Project status and shortcuts"),
        ),
    ),
    NavGroup(
        label="Tools",
        icon="terminal",
        value="tools",
        items=(
            NavItem("Script Runner", "/scripts", "terminal", "Run clipboard scripts with arguments"),
            NavItem("Run History", "/scripts#history", "history", "Recent local script runs"),
        ),
    ),
    NavGroup(
        label="System",
        icon="settings",
        value="system",
        items=(
            NavItem("Settings", "/settings", "tune", "Execution defaults"),
            NavItem("Environment", "/settings#environment", "computer", "Detected runtimes and paths"),
        ),
    ),
    NavGroup(
        label="Help",
        icon="help",
        value="help",
        items=(
            NavItem("About", "/about", "info", "Project notes and docs references"),
        ),
    ),
)
