"""编排：抓源 → 时间窗 → 规则分级 → 去重合并 → 一条消息 → 干跑/推送 → 状态。

下游故障（解析乱、字段缺、登录墙、全源失败）一律暂停推送，绝不转发未审核内容。
「0 条」和「全源失败」在报告里分开写，不能把瞎了当成安静。
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from . import cloud, http, rules, sources, state as state_mod, telegram

UTC = timezone.utc
CST = timezone(timedelta(hours=8))
EXIT_OK, EXIT_PARTIAL, EXIT_ALL_FAILED, EXIT_CONFIG = 0, 1, 2, 5
# 同源均衡：一轮里同一个源最多入选 2 条，避免中文快讯被 Odaily 一家占满。
MAX_PER_SOURCE = 2
# 这些状态说明「没审明白」，宁可不推；stale/missing 只算部分失败。
PAUSE_STATUSES = {"parse_error", "low_quality", "login_wall", "unsafe_xml", "not_rss",
                  "blocked", "config_error"}


@dataclass
class Candidate:
    item: object
    decision: object
    merged: int = 1


def balance_sources(candidates: List[Candidate], max_per_source: int = MAX_PER_SOURCE):
    """同级候选中同一来源最多入选 max_per_source 条，超出的留到下一轮。

    被延后的条目不写 seen，下一轮还会参与，从而实现轮流出。
    """
    selected: List[Candidate] = []
    deferred: List[Candidate] = []
    used: Dict[str, int] = {}
    for candidate in candidates:
        source = candidate.item.source
        if used.get(source, 0) >= max_per_source:
            deferred.append(candidate)
            continue
        selected.append(candidate)
        used[source] = used.get(source, 0) + 1
    return selected, deferred


def _resolve_published(raw: str, now: datetime) -> str:
    marker = (raw or "").strip()
    if marker == "now":
        return now.isoformat()
    if marker.startswith("now-"):
        try:
            minutes = int(marker[4:].removesuffix("m"))
            return (now - timedelta(minutes=minutes)).isoformat()
        except ValueError:
            return ""
    return sources.parse_time(marker)


def _load_fixtures(path: str, now: datetime) -> Tuple[List[sources.SourceReport], List[cloud.SearchHit]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    reports: List[sources.SourceReport] = []
    for row in raw.get("items", []):
        item = sources.to_item(row)
        item.published_at = _resolve_published(row.get("published_at", ""), now)
        reports.append(sources.SourceReport(
            row.get("source", "fixture"), row.get("label", "本地样例"), "fixture", True, "ok",
            "", [item]))
    hits = [cloud.SearchHit(query=row.get("query", "fixture"), engine=row.get("engine", "fixture"),
                            url=row.get("url", ""), title=row.get("title", ""),
                            snippet=row.get("snippet", ""),
                            published_at=_resolve_published(row.get("published_at", ""), now))
            for row in raw.get("cloud", [])]
    return reports, hits


def _source_credibility(item) -> int:
    src = item.source
    if src in sources.OFFICIAL_PROMOTABLE:
        return 5
    if src in ("ethereum_foundation", "trailofbits"):
        return 4
    if src.startswith("x_") or src == "cloud_search":
        return 1
    return 3


def run(*, fixtures: str = "", state_path: str = "state/state.json", report_path: str = "",
        max_queries: int = 8, no_cloud: bool = False, send: bool = False) -> int:
    started_dt = datetime.now(UTC)
    started = started_dt.isoformat()
    st = state_mod.State(state_path).load()
    window_start, since_text = cloud.search_window(st.last_run, now=started_dt)
    gap = st.record_run(started)
    print(f"本轮开始：{started}｜距上轮 {gap if gap >= 0 else '未知'} 分钟｜时间窗起点 {since_text}")

    cloud_report = cloud.CloudReport(since=since_text)
    if fixtures:
        reports, hits = _load_fixtures(fixtures, started_dt)
        print(f"使用本地样例：源条目 {len(reports)}｜云端命中 {len(hits)}")
    else:
        with ThreadPoolExecutor(max_workers=8) as pool:
            reports = list(pool.map(sources.fetch_source, sources.FEEDS))
        cloud_report = cloud.run_cloud(max_queries=max_queries, enabled=not no_cloud,
                                       since_text=since_text)
        hits = cloud_report.hits
        print(f"云端搜索：{cloud_report.to_dict()}")

    all_failed = bool(reports) and all(not report.ok for report in reports)
    push_paused = False
    pause_reasons: List[str] = []
    raw_items: List[object] = []
    windowed: List[object] = []
    for report in reports:
        if not report.ok:
            print(f"源失败：{report.name}｜{report.status}｜{report.error[:200]}", file=sys.stderr)
            if report.expected:
                print(f"源按预期不可直读（GitHub 出口限制）：{report.name}，不暂停推送。",
                      file=sys.stderr)
                continue
            alert = st.add_source_status(report.name, False, report.status, report.error)
            if alert:
                print(f"源健康告警：{alert}", file=sys.stderr)
            if report.status in PAUSE_STATUSES:
                push_paused = True
                pause_reasons.append(f"{report.name}:{report.status}")
            continue
        st.add_source_status(report.name, True)
        raw_items.extend(report.items)

    for item in raw_items:
        if not item.title or not item.url:
            push_paused = True
            pause_reasons.append(f"{item.source}:missing_title_or_url")
            continue
        if not item.published_at:
            push_paused = True
            pause_reasons.append(f"{item.source}:missing_published_at")
            continue
        try:
            published = datetime.fromisoformat(item.published_at)
        except ValueError:
            push_paused = True
            pause_reasons.append(f"{item.source}:bad_published_at")
            continue
        if published < window_start or published > started_dt + timedelta(minutes=10):
            continue
        item.published_at = published.astimezone(UTC).isoformat()
        windowed.append(item)

    if all_failed:
        print("全部源同时失败，单独告警：0 条不等于一切正常。", file=sys.stderr)
        push_paused = True
        pause_reasons.append("all_sources_failed")
    if not raw_items and not hits and not all_failed:
        print("本轮 0 条：可能是真的安静，报告里单独记 empty_round。", file=sys.stderr)

    items = list(windowed)
    items.extend(cloud.hits_to_items(hits, window_start=window_start, window_end=started_dt))

    candidates: List[Candidate] = []
    archived = {"high": 0, "medium_high": 0, "medium": 0, "low": 0}
    routed = {"exchange": 0, "whale": 0}
    filtered_non_chinese = 0
    already_seen = 0
    decisions: List[dict] = []
    for item in items:
        # 去重优先：已经在 state 里处理过的，本轮不再重算、不再计数。
        if st.known_event(item, None):
            already_seen += 1
            continue
        # 中文过滤：纯英文内容挡在推送之前（不是只存档）。
        if rules.is_non_chinese(item.title, item.summary):
            filtered_non_chinese += 1
            st.mark_seen(item, rules.non_chinese_decision(), "非中文")
            continue
        decision = rules.judge(item.title, item.summary, source=item.source,
                               published_at=item.published_at, now=started_dt)
        if decision.route:
            routed[decision.route] += 1
            st.mark_seen(item, decision, "让路")
            continue
        if not decision.pushable:
            archived[decision.level] = archived.get(decision.level, 0) + 1
            st.mark_seen(item, decision, "存档")
            decisions.append({"title": item.title, "source": item.source,
                              "url": item.url, "decision": decision.to_dict()})
            continue
        candidates.append(Candidate(item, decision))

    # 先按时间倒序（新的在前），再按等级/来源credibility 稳定排序，同级内仍是新的优先。
    candidates.sort(key=lambda c: c.item.published_at, reverse=True)
    candidates.sort(key=lambda c: (-rules.LEVEL_RANK[c.decision.level],
                                   -_source_credibility(c.item)))
    unique: List[Candidate] = []
    for candidate in candidates:
        duplicate = next((entry for entry in unique
                          if state_mod.event_id(candidate.item.url, candidate.item.title)
                          == state_mod.event_id(entry.item.url, entry.item.title)
                          or (state_mod.title_bucket(candidate.item.title, candidate.item.published_at)
                              == state_mod.title_bucket(entry.item.title, entry.item.published_at))), None)
        if duplicate:
            duplicate.merged += 1
            continue
        unique.append(candidate)
    unique, deferred = balance_sources(unique)
    print(f"候选 {len(unique)} 条（跨源合并 {sum(c.merged - 1 for c in unique)} 条）｜"
          f"存档 {archived}｜让路 {routed}｜纯英文挡下 {filtered_non_chinese}｜重复跳过 {already_seen}｜"
          f"同源顺延 {len(deferred)} 条")

    lines = [rules.format_message(candidate.item, candidate.decision) for candidate in unique]
    message = "\n".join(lines)
    if len(message) > 3900:
        push_paused = True
        pause_reasons.append("message_too_long")
        print("合并消息超过 Telegram 单条上限，暂停推送，不截断内容。", file=sys.stderr)

    pushed = False
    if send:
        if push_paused:
            print("下游故障未审明白，拒绝推送：" + ", ".join(sorted(set(pause_reasons))[:12]),
                  file=sys.stderr)
            return EXIT_CONFIG
        token, chat_id = telegram.credentials()
        if not token or not chat_id:
            print("缺少 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，拒绝推送。", file=sys.stderr)
            return EXIT_CONFIG
        if lines:
            telegram.send_message(token, chat_id, message)
            pushed = True
    elif lines:
        print("\n--- 干跑：不会发送 Telegram ---\n" + message + "\n")

    for candidate in unique:
        st.mark_seen(candidate.item, candidate.decision, "已推送" if pushed else "干跑候选")
    st.prune()
    st.save()

    report = {
        "started_at": started, "finished_at": datetime.now(UTC).isoformat(),
        "gap_minutes": gap, "mode": "send" if send else "dry-run",
        "window_start": window_start.isoformat(), "overlap_minutes": cloud.OVERLAP_MINUTES,
        "sources": [r.to_dict() for r in reports],
        "source_failures": st.source_failures, "all_sources_failed": all_failed,
        "cloud": cloud_report.to_dict(), "push_paused": push_paused,
        "pause_reasons": sorted(set(pause_reasons))[:20],
        "empty_round": not items, "raw_items": len(raw_items),
        "archived": archived, "routed": routed,
        "filtered_non_chinese": filtered_non_chinese, "already_seen": already_seen,
        "max_per_source": MAX_PER_SOURCE, "source_balanced_deferred": len(deferred),
        "candidates": [{"source": c.item.source, "title": c.item.title, "url": c.item.url,
                        "published_at": c.item.published_at, "merged": c.merged,
                        "decision": c.decision.to_dict()} for c in unique],
        "archived_decisions": decisions[:40],
        "message": message, "telegram_sent": pushed,
    }
    if report_path:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if all_failed or push_paused:
        return EXIT_ALL_FAILED if all_failed else EXIT_PARTIAL
    if any(not r.ok for r in reports) or cloud_report.errors:
        return EXIT_PARTIAL
    return EXIT_OK


def fetch_once(url: str) -> http.FetchResult:
    """调试用：手工抓一个 URL，验证字段，不改变运行状态。"""
    return http.fetch(url, timeout=20)
