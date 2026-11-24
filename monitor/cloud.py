"""兜底层：Exa 公共 MCP（免 key）→ Anysearch（可选密钥），只找新闻线索。

搜索起点复用 state.last_run 与 30 分钟重叠，不另起字段。
没有明确发布时间、过旧、非币圈、交易所主体、链上转账的命中一律不进候选。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import http, rules, sources

UTC = timezone.utc
EXA_MCP = "https://mcp.exa.ai/mcp"
ANYSEARCH_DEFAULT_BASE_URL = "https://api.anysearch.com"
OVERLAP_MINUTES = 30
MAX_LOOKBACK_DAYS = 3
JSONRPC_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "User-Agent": http.BROWSER_UA,
    "Origin": "https://mcp.exa.ai",
}

DISCOVERY_QUERIES = [
    ("中文", "加密货币 监管 执法 最新"),
    ("中文", "协议 被黑 漏洞 损失"),
    ("中文", "稳定币 脱锚 裁决"),
    ("中文", "加密项目 融资 并购"),
    ("英文", "crypto protocol exploit hack latest"),
    ("英文", "SEC CFTC crypto enforcement action"),
    ("英文", "stablecoin depeg court ruling"),
    ("英文", "Bitcoin Ethereum mainnet upgrade announcement"),
]


@dataclass
class SearchHit:
    query: str
    engine: str
    url: str
    title: str
    snippet: str = ""
    published_at: str = ""

    def to_dict(self) -> dict:
        return {"query": self.query, "engine": self.engine, "url": self.url,
                "title": self.title[:300], "snippet": self.snippet[:300],
                "published_at": self.published_at}


@dataclass
class CloudReport:
    hits: List[SearchHit] = field(default_factory=list)
    queries_used: List[str] = field(default_factory=list)
    engines: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    since: str = ""

    def to_dict(self) -> dict:
        return {"since": self.since, "queries": self.queries_used, "engine_counts": self.engines,
                "hits": len(self.hits), "errors": self.errors[:8]}


def search_window(last_run: str, *, now: Optional[datetime] = None,
                  overlap_minutes: int = OVERLAP_MINUTES,
                  max_lookback_days: int = MAX_LOOKBACK_DAYS) -> Tuple[datetime, str]:
    """上次运行往前重叠 30 分钟；首次或停摆超过 3 天自动回看 3 天。"""
    now = now or datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat((last_run or "").replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if (parsed is None or parsed.tzinfo is None or parsed > now + timedelta(minutes=5)
            or (now - parsed) > timedelta(days=max_lookback_days)):
        parsed = now - timedelta(days=max_lookback_days)
    start = parsed - timedelta(minutes=overlap_minutes)
    return start, start.strftime("%Y-%m-%d %H:%M UTC")


def _objective(since_text: str) -> str:
    return (f"只返回 {since_text} 之后新发布或新更新的原文新闻；"
            f"不要返回 {since_text} 之前的旧文章、帮助页、科普文、历史回顾和常驻页面；"
            "每条必须有明确发布时间；排除价格预测、行情讨论、交易信号与交易所公告。")


def _parse_time(raw: str, now: datetime) -> Optional[datetime]:
    text = (raw or "").strip()
    if not text:
        return None
    parsed = sources.parse_time(text)
    if parsed:
        return datetime.fromisoformat(parsed)
    relative = re.search(r"(\d+)\s*(minute|hour|day)s?\s*ago", text, re.I)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        delta = {"minute": timedelta(minutes=amount), "hour": timedelta(hours=amount),
                 "day": timedelta(days=amount)}[unit]
        return now - delta
    return None


def _sse_or_json(payload: str) -> dict:
    if payload.lstrip().startswith("{"):
        return json.loads(payload)
    messages = []
    for line in payload.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            if raw and raw != "[DONE]":
                try:
                    messages.append(json.loads(raw))
                except ValueError:
                    continue
    return messages[-1] if messages else {}


def mcp_call(endpoint: str, tool_name: str, arguments: dict, *, timeout: float = 25.0,
             origin: str = "https://mcp.exa.ai") -> str:
    headers = dict(JSONRPC_HEADERS)
    headers["Origin"] = origin
    init = http.fetch(endpoint, method="POST",
                      body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2025-03-26",
                                                  "capabilities": {},
                                                  "clientInfo": {"name": "bianquan-xinwen",
                                                                 "version": "1.0.0"}}}).encode(),
                      headers=headers, timeout=timeout, retries=1, max_bytes=1_000_000)
    if not init.ok:
        raise RuntimeError(f"MCP 初始化失败: {init.error_type} {init.error[:120]}")
    try:
        _sse_or_json(init.text())
    except ValueError:
        pass
    call = http.fetch(endpoint, method="POST",
                      body=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                       "params": {"name": tool_name, "arguments": arguments}}).encode(),
                      headers=headers, timeout=timeout, retries=1, max_bytes=2_000_000)
    if not call.ok:
        raise RuntimeError(f"MCP {tool_name} 失败: {call.error_type} {call.error[:120]}")
    try:
        data = _sse_or_json(call.text())
    except ValueError as exc:
        raise RuntimeError(f"MCP {tool_name} 返回非 JSON") from exc
    if isinstance(data, list):
        data = next((item for item in data if isinstance(item, dict) and item.get("id") == 2),
                    data[-1] if data and isinstance(data[-1], dict) else {})
    if not isinstance(data, dict) or "error" in data:
        raise RuntimeError(f"MCP {tool_name} 报错: {str(data)[:200]}")
    block = (data.get("result") or {}).get("content") or []
    return "\n".join(str(part.get("text") or "") for part in block if isinstance(part, dict))


_MONTHS = {name: idx for idx, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
    start=1)}


def _json_date(*texts: str) -> str:
    """Anysearch 的 JSON 结果没有发布时间字段，从标题/摘要里补一个明确日期。"""
    blob = " ".join(str(item or "") for item in texts)
    found = re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", blob)
    if found:
        year, month, day = re.split(r"[-/.]", found.group(0))
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}T00:00:00+00:00"
    found = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(20\d{2})\b", blob)
    if found:
        month = _MONTHS.get(found.group(1)[:3].lower())
        if month:
            return f"{found.group(3)}-{month:02d}-{int(found.group(2)):02d}T00:00:00+00:00"
    found = re.search(r"\d+\s*(?:minute|hour|day)s?\s*ago", blob, re.I)
    return found.group(0).lower() if found else ""


def _hits_from_text(text: str, query: str, engine: str) -> List[SearchHit]:
    hits: List[SearchHit] = []
    for chunk in re.split(r"\n(?=Title:)", text):
        url_match = re.search(r"^URL:\s*(https?://\S+)", chunk, re.M)
        if not url_match:
            continue
        title = re.search(r"^Title:\s*(.+)$", chunk, re.M)
        published = re.search(r"^Published:\s*(.+)$", chunk, re.M)
        snippet = re.search(r"^Highlights:\s*(.*)$", chunk, re.M | re.S)
        raw_date = ""
        if published and published.group(1).strip().upper() != "N/A":
            raw_date = published.group(1).strip()[:60]
        if not raw_date and snippet:
            found = re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", snippet.group(1))
            raw_date = found.group(0) if found else ""
        hits.append(SearchHit(query, engine, url_match.group(1).rstrip(".,;)"),
                              (title.group(1).strip() if title else "")[:300],
                              (snippet.group(1).strip() if snippet else "")[:800], raw_date))
    if hits:
        return hits[:8]
    seen = set()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if parsed is not None:
        def walk(value):
            if isinstance(value, dict):
                url = str(value.get("url") or value.get("link") or "")
                if url.startswith("http") and url not in seen:
                    seen.add(url)
                    title = str(value.get("title") or "")
                    snippet = str(value.get("snippet") or value.get("text")
                                  or value.get("content") or "")
                    published = (str(value.get("published") or value.get("date") or "")
                                 or _json_date(title, snippet))
                    hits.append(SearchHit(query, engine, url, title[:300],
                                          snippet[:800], published[:60]))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(parsed)
    return hits[:8]


def exa_search(query: str, since_text: str) -> List[SearchHit]:
    text = mcp_call(EXA_MCP, "web_search_exa",
                    {"query": query, "objective": _objective(since_text), "numResults": 5})
    return _hits_from_text(text, query, "exa_public")


class AllKeysFailed(RuntimeError):
    """所有 Anysearch key 都失败（含配额耗尽）。"""


def _try_keys(search_fn, keys, *args, label="Anysearch"):
    """按顺序尝试多把 key：402/配额耗尽自动换下一把，其他错误也依次尝试。

    背景（2026-11-25）：第一把 key 当日配额耗尽返回 402，但代码只取 keys[0]，
    导致六仓同时召回归零；#2~#4 明明可用却被闲置。
    """
    failures = []
    for index, key in enumerate(keys):
        try:
            return search_fn(key, *args), index
        except Exception as exc:  # noqa: BLE001 单把失败继续试下一把
            failures.append(f"key{index + 1}: {type(exc).__name__} {str(exc)[:100]}")
    raise AllKeysFailed("；".join(failures))


def anysearch_search(query: str, key: str, since_text: str) -> List[SearchHit]:
    url = f"{os.environ.get('ANYSEARCH_BASE_URL', ANYSEARCH_DEFAULT_BASE_URL).rstrip('/')}/v1/search"
    scoped = f"{query}；只返回 {since_text} 之后新发布的原文新闻，排除旧文章与交易所公告"
    result = http.fetch(url, method="POST",
                        body=json.dumps({"query": scoped, "max_results": 5}).encode(),
                        headers={"Content-Type": "application/json", "User-Agent": http.BROWSER_UA,
                                 "Authorization": f"Bearer {key}", "X-API-Key": key},
                        timeout=25, retries=1)
    if not result.ok:
        raise RuntimeError(f"Anysearch {result.error_type}: {result.error[:120]}")
    return _hits_from_text(result.text(), scoped, "anysearch")


def _dedup(hits: List[SearchHit]) -> List[SearchHit]:
    seen = set()
    unique = []
    for hit in hits:
        key = re.sub(r"^https?://(www\.)?", "", hit.url).split("?")[0].lower()
        if key in seen or len(hit.url) > 500:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


def run_cloud(daily_used: Optional[Dict[str, str]] = None, *, max_queries: int = 8,
              enabled: bool = True, since_text: str = "") -> CloudReport:
    daily_used = daily_used or {}
    report = CloudReport(since=since_text or search_window("")[1])
    if not enabled:
        return report
    keys = [key.strip() for key in os.environ.get("ANYSEARCH_KEYS", "")
            .replace(";", ",").split(",") if key.strip()]
    for _kind, query in DISCOVERY_QUERIES:
        if len(report.queries_used) >= max_queries:
            break
        if query in daily_used:
            continue
        try:
            hits = exa_search(query, report.since)
        except Exception as exc:  # noqa: BLE001
            message = f"{query}: {type(exc).__name__} {str(exc)[:120]}"
            if not keys:
                report.errors.append(message + " → 未配置 ANYSEARCH_KEYS，无法兜底")
                continue
            try:
                hits, _used = _try_keys(anysearch_search, keys, query, report.since)
                report.errors.append(message + " → Anysearch 兜底成功")
            except Exception as exc2:  # noqa: BLE001
                report.errors.append(message + f" → Anysearch 也失败: {str(exc2)[:120]}")
                continue
        if not hits:
            continue
        report.hits.extend(hits)
        report.queries_used.append(query)
        report.engines[hits[0].engine] = report.engines.get(hits[0].engine, 0) + 1
    report.hits = _dedup(report.hits)
    return report


def hits_to_items(hits: List[SearchHit], *, window_start: Optional[datetime] = None,
                  window_end: Optional[datetime] = None) -> List[sources.Item]:
    now = window_end or datetime.now(UTC)
    items: List[sources.Item] = []
    for hit in hits:
        if not hit.url.startswith(("http://", "https://")):
            continue
        published = _parse_time(hit.published_at, now)
        if published is None:
            continue
        if window_start is not None and published < window_start:
            continue
        if published > now + timedelta(minutes=10):
            continue
        if re.search(r"/(help|learn|faq)/|/help$|support/.*/articles", hit.url, re.I):
            continue
        title = hit.title or hit.snippet[:120]
        decision = rules.judge(title, hit.snippet, published_at=published.isoformat())
        if not decision.pushable or decision.route:
            continue
        items.append(sources.Item(
            "cloud_search", title[:500], hit.url, published.astimezone(UTC).isoformat(),
            hit.snippet[:1200], "cloud", f"cloud:{hit.engine}", {"query": hit.query}))
    return items
