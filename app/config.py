"""Minimal configuration adapter for the public logic lab."""
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

@dataclass(frozen=True)
class Settings:
    timezone: str = os.getenv("LOGIC_TIMEZONE", "UTC")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

settings = Settings()

def apply_timezone(name: str):
    ZoneInfo(name)
    object.__setattr__(settings, "timezone", name)
