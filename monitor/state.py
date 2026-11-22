"""跨轮状态：链接去重 + 标题归一/6 小时时间桶 + 跨源合并 + 源健康 + cron 间隔。

状态提交在 state 分支，不靠 Actions cache。干跑也写 seen，避免下一轮重复。
推送失败不写 seen，下一轮重试。中了/低了也写存档，方便审计但不再推。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from . import rules

UTC = timezone.utc
RETENTION_DAYS = 7
LEVEL_RANK = rules.LEVEL_RANK


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_url(url: str) -> str:
    raw = (url or "").strip().split("#", 1)[0]
    raw = re.sub(r"^https?://(www\.)?", "", raw, flags=re.I)
    if "?" in raw:
        path, query = raw.split("?", 1)
        kept = [part for part in query.split("&")
                if part and not part.lower().startswith(("utm_", "ref=", "spm="))]
        raw = path + ("?" + "&".join(kept) if kept else "")
    return raw.rstrip("/")


def event_id(url: str, title: str) -> str:
    key = normalize_url(url)
    if not key:
        key = "title|" + rules.normalize_title(title)
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def title_bucket(title: str, published_at: str) -> str:
    stamp = _parse(published_at)
    bucket = int(stamp.timestamp() // 21600) if stamp else 0   # 6 小时桶
    return f"{rules.normalize_title(title)}|{bucket}"


class State:
    def __init__(self, path: str = "state/state.json"):
        self.path = Path(path)
        self.seen: Dict[str, dict] = {}
        self.title_buckets: Dict[str, str] = {}
        self.source_failures: Dict[str, dict] = {}
        self.runs: List[dict] = []
        self.last_run: str = ""
        self.updated_at: str = ""

    def load(self) -> "State":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"状态文件损坏，拒绝静默跳过（会造成重复推送）: {exc}") from exc
        self.seen = dict(raw.get("seen") or {})
        self.title_buckets = dict(raw.get("title_buckets") or {})
        self.source_failures = dict(raw.get("source_failures") or {})
        self.runs = list(raw.get("runs") or [])[-48:]
        self.last_run = str(raw.get("last_run") or "")
        return self

    def save(self) -> None:
        self.updated_at = _now()
        payload = {
            "schema_version": "1.0.0", "updated_at": self.updated_at,
            "last_run": self.last_run, "seen": self.seen,
            "title_buckets": self.title_buckets, "source_failures": self.source_failures,
            "runs": self.runs[-48:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def record_run(self, started_at: str) -> float:
        prior = _parse(self.last_run)
        current = _parse(started_at)
        gap = round((current - prior).total_seconds() / 60, 1) if prior and current else -1
        self.runs.append({"at": started_at, "gap_minutes": gap})
        self.last_run = started_at
        return gap

    def add_source_status(self, name: str, ok: bool, error_type: str = "",
                          error: str = "") -> Optional[str]:
        entry = dict(self.source_failures.get(name) or {})
        if ok:
            if entry.get("count", 0) >= 3:
                self.source_failures[name] = {"count": 0, "recovered_at": _now(), "last_error": ""}
                return f"来源恢复：{name}"
            self.source_failures.pop(name, None)
            return None
        count = int(entry.get("count") or 0) + 1
        self.source_failures[name] = {
            "count": count, "error_type": error_type, "last_error": error[:300], "at": _now(),
        }
        if count == 3:
            return f"来源连续 3 轮失败：{name}（{error_type}）"
        return None

    def known_event(self, item, decision) -> bool:
        """链接已见，或同标题同一 6 小时桶已见，都不再推。"""
        if event_id(item.url, item.title) in self.seen:
            return True
        return title_bucket(item.title, item.published_at) in self.title_buckets

    def mark_seen(self, item, decision, status: str = "新增") -> None:
        key = event_id(item.url, item.title)
        previous = self.seen.get(key, {})
        self.seen[key] = {
            "source": item.source, "title": item.title[:300], "url": item.url,
            "level": decision.level, "rule": decision.rule_name, "status": status,
            "published_at": item.published_at,
            "first_seen": previous.get("first_seen") or _now(),
            "updated_at": _now(),
        }
        self.title_buckets[title_bucket(item.title, item.published_at)] = key

    def prune(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
        removed = 0
        for key, meta in list(self.seen.items()):
            stamp = _parse(str(meta.get("first_seen") or ""))
            if stamp is not None and stamp < cutoff:
                self.seen.pop(key, None)
                removed += 1
        for bucket, key in list(self.title_buckets.items()):
            if key not in self.seen:
                self.title_buckets.pop(bucket, None)
        return removed
