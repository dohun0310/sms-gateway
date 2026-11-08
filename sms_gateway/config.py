"""Configuration loading and validation."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

NUMBER_RE = re.compile(r"^\+?[0-9]{3,20}$")
PLACEHOLDER_PASSPHRASE = "CHANGE-ME"
DEFAULT_INTRO = "긴급 요청 전달 서비스입니다. 다른 번호로 들어온 요청을 전달합니다."


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Contact:
    name: str
    number: str


@dataclass(frozen=True)
class EmergencyConfig:
    passphrase: str
    contacts: tuple[Contact, ...]
    intro: str = DEFAULT_INTRO
    reply_to_requester: bool = True
    max_requests_per_hour: int = 3


@dataclass(frozen=True)
class Config:
    database_path: Path
    socket_path: Path
    allowed_uids: frozenset[int]
    poll_interval: float = 5.0
    emergency: EmergencyConfig | None = None


def validate_number(number: str) -> str:
    number = number.strip().replace("-", "")
    if not NUMBER_RE.fullmatch(number):
        raise ConfigError("invalid phone number")
    return number


def _emergency(raw: dict) -> EmergencyConfig:
    passphrase = str(raw.get("passphrase", "")).strip()
    if passphrase == PLACEHOLDER_PASSPHRASE:
        raise ConfigError("set emergency.passphrase in the config file")
    if len(passphrase) < 6 or any(ch.isspace() for ch in passphrase):
        raise ConfigError("emergency.passphrase must be at least 6 characters without spaces")

    contacts = tuple(
        Contact(name=str(c["name"]).strip(), number=validate_number(str(c["number"])))
        for c in raw.get("contacts", [])
    )
    if not contacts:
        raise ConfigError("emergency.contacts must list at least one contact")
    if len({c.number for c in contacts}) != len(contacts):
        raise ConfigError("emergency.contacts must not repeat a number")

    limit = int(raw.get("max_requests_per_hour", 3))
    if limit < 1:
        raise ConfigError("emergency.max_requests_per_hour must be at least 1")

    return EmergencyConfig(
        passphrase=passphrase,
        contacts=contacts,
        intro=str(raw.get("intro", DEFAULT_INTRO)).strip() or DEFAULT_INTRO,
        reply_to_requester=bool(raw.get("reply_to_requester", True)),
        max_requests_per_hour=limit,
    )


def load(path: Path) -> Config:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    uids = raw.get("allowed_uids", [])
    if not uids:
        raise ConfigError("allowed_uids must list at least one uid")

    return Config(
        database_path=Path(raw.get("database_path", "/var/lib/sms-gateway/sms.db")),
        socket_path=Path(raw.get("socket_path", "/run/sms-gateway/sms.sock")),
        allowed_uids=frozenset(int(u) for u in uids),
        poll_interval=float(raw.get("poll_interval", 5.0)),
        emergency=_emergency(raw["emergency"]) if "emergency" in raw else None,
    )
