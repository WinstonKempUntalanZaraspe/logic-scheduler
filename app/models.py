from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, time
from typing import Optional

from .session_titles import task_base_title


NOTE_KINDS = {"NOTE"}
ACTIONABLE_KINDS = {"TASK", "TEXT", "CHECKLIST", ""}


@dataclass
class Task:
    id: str
    project_id: str
    title: str
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    priority: int = 0
    tags: list[str] = field(default_factory=list)
    content: str = ""
    status: int = 0
    is_all_day: bool = False
    created_at: Optional[datetime] = None
    column_id: Optional[str] = None
    reminders: list[str] = field(default_factory=list)
    repeat_flag: Optional[str] = None
    desc: str = ""
    sort_order: Optional[int] = None
    items: list[dict] = field(default_factory=list)
    kind: str = "TEXT"
    parent_id: Optional[str] = None
    etag: Optional[str] = None

    @property
    def is_note(self) -> bool:
        return (self.kind or "").upper() in NOTE_KINDS

    @property
    def is_actionable(self) -> bool:
        """Whether this TickTick item is an open, writable task.

        TickTick uses status -1 for abandoned / "Won't Do", 0 for open, and
        2 for completed.  A task that is not open must never enter the scheduler
        or a native write path, even if a stale cache happens to contain it.
        """
        return not self.is_note and self.status == 0

    def is_expired(self, now: datetime) -> bool:
        """Whether a timed task's entire scheduled window has already elapsed."""
        if self.is_all_day or not self.start or not self.end:
            return False
        return self.end <= now

    @property
    def duration_minutes(self) -> Optional[int]:
        # An all-day TickTick date is a date/deadline marker, not a work-duration estimate.
        if self.is_all_day:
            return None
        if self.start and self.end:
            return max(1, int((self.end - self.start).total_seconds() // 60))
        return None


@dataclass
class TaskMeta:
    task_id: str
    duration_minutes: Optional[int] = None
    remaining_minutes: Optional[int] = None
    deadline: Optional[datetime] = None
    earliest: Optional[datetime] = None
    latest_end: Optional[datetime] = None
    energy: str = "auto"  # auto/high/medium/low
    confidence: str = "medium"  # high/medium/low
    splittable: bool = True
    min_chunk: int = 25
    max_chunk: int = 90
    preferred_window_start: Optional[time] = None
    preferred_window_end: Optional[time] = None
    dependencies: list[str] = field(default_factory=list)
    category: Optional[str] = None
    hard_stop: Optional[datetime] = None
    autoschedule: bool = True
    location: Optional[str] = None
    context: Optional[str] = None
    weekly_bucket: Optional[str] = None
    transition_minutes: int = 0
    must_finish: bool = False
    unknown_duration: bool = False
    timing: str = "balanced"  # balanced/asap/late
    allowed_weekdays: list[int] = field(default_factory=lambda: list(range(7)))  # Mon=0
    explicit_activity_minutes: Optional[int] = None  # plan-only actual outing time


@dataclass
class BusyBlock:
    start: datetime
    end: datetime
    label: str
    source: str = "fixed"


@dataclass
class Segment:
    task_id: str
    project_id: str
    title: str
    start: datetime
    end: datetime
    score: float
    reason: str
    source_task: Task
    segment_index: int = 1
    segment_count: int = 1
    location: Optional[str] = None
    category: Optional[str] = None

    def dict(self):
        d = asdict(self)
        if self.title == self.source_task.title:
            d['title'] = task_base_title(self.source_task)
        if self.source_task.content.startswith('ProjectIntelligenceCampaign:'):
            d['project_instructions'] = '\n'.join(line for line in self.source_task.content.splitlines()
                if not line.startswith(('ProjectIntelligenceCampaign:', 'ProjectIntelligenceWorkPackage:', 'Generated from the rolling project blueprint.')))
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        d["source_task"] = {
            "id": self.source_task.id,
            "project_id": self.source_task.project_id,
            "title": self.source_task.title,
            "priority": self.source_task.priority,
            "tags": self.source_task.tags,
            "kind": self.source_task.kind,
            "repeat_flag": self.source_task.repeat_flag,
            "etag": self.source_task.etag,
            "original_start": self.source_task.start.isoformat() if self.source_task.start else None,
            "original_end": self.source_task.end.isoformat() if self.source_task.end else None,
        }
        return d

