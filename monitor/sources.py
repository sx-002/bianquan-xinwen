"""第一层：官方 RSS、中文快讯、发现层来源的公开源适配器（零第三方依赖）。

只接币圈新闻：监管/安全一手源优先，中文快讯与免 key 发现层补覆盖。
宏观、ETF、交易所状态/公告、链上巨鲸搬砖一律不在这里建源。

每个源记 verified_at（最近一次公网验证日期）和 fresh_hours（过期判 stale 阈值）。
抓不到就如实记失败类型，绝不把失败伪装成「本来就没有新闻」。
"""
from __future__ import annotations

import email.utils
import html as html_lib
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin

from . import http

UTC = timezone.utc
CST = timezone(timedelta(hours=8))


@dataclass
class Item:
    source: str
    title: str
    url: str
    published_at: str = ""          # UTC ISO 字符串
    summary: str = ""
    source_layer: str = "rss"       # rss / html / api / cloud / fixture
    source_name: str = ""
    extra: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source": self.source, "title": self.title, "url": self.url,
            "published_at": self.published_at, "summary": self.summary[:500],
            "source_layer": self.source_layer, "source_name": self.source_name,
            "extra": self.extra,
        }


@dataclass
class FeedSource:
    name: str
    label: str
    url: str
    kind: str                       # rss / blockbeats / odaily / techflow / telegram
    verified_at: str
    fresh_hours: int = 72
    note: str = ""
    headers: Optional[Dict[str, str]] = None
    channel: str = ""               # telegram 频道名


# 19 个源条目：10 个官方/行业 RSS + 3 个中文快讯 + 2 个免 key 发现层 + 4 个 X 公开预览。
# SEC/CFTC 只走 RSS，不打开可见网页；美联储/SoSoValue/Farside/金十/ForexFactory 不接。
FEEDS: List[FeedSource] = [
    FeedSource("sec_official", "SEC 新闻稿", "https://www.sec.gov/news/pressreleases.rss",
               "rss", "2026-10-11", 168, "监管一手源；可仅凭来源有限提权",
               {"User-Agent": "bianquan-xinwen/1.0 (https://github.com/sx-002/bianquan-xinwen)"}),
    FeedSource("cftc_general", "CFTC 综合", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml",
               "rss", "2026-10-11", 168, "监管一手源；可仅凭来源有限提权"),
    FeedSource("cftc_enforcement", "CFTC 执法", "https://www.cftc.gov/RSS/RSSENF/rssenf.xml",
               "rss", "2026-10-11", 720, "真监管动作，信号强；发文频率低，30 天才算陈旧"),
    FeedSource("ethereum_foundation", "以太坊基金会", "https://blog.ethereum.org/feed.xml",
               "rss", "2026-10-11", 720, "ETH 官方，发文频率低"),
    FeedSource("trailofbits", "Trail of Bits", "https://blog.trailofbits.com/feed/",
               "rss", "2026-10-11", 720, "安全研究；critical 必须标题命中"),
    FeedSource("coindesk", "CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/",
               "rss", "2026-10-11", 72),
    FeedSource("theblock", "The Block", "https://www.theblock.co/rss.xml",
               "rss", "2026-10-11", 72),
    FeedSource("cointelegraph", "Cointelegraph", "https://cointelegraph.com/rss",
               "rss", "2026-10-11", 72),
    FeedSource("decrypt", "Decrypt", "https://decrypt.co/feed",
               "rss", "2026-10-11", 72),
    FeedSource("cointelegraph_btc", "Cointelegraph-BTC", "https://cointelegraph.com/rss/tag/bitcoin",
               "rss", "2026-10-11", 72, "BTC 专用"),
    FeedSource("blockbeats", "BlockBeats 快讯", "https://www.theblockbeats.info/newsflash/1",
               "blockbeats", "2026-11-18", 72, "中文快讯；原 API 端点本机 SSL 不稳定，改用快讯列表页"),
    FeedSource("odaily", "Odaily 星球日报", "https://www.odaily.news/zh-CN/newsflash",
               "odaily", "2026-11-18", 72, "中文快讯；/rss 已 404，读快讯列表页"),
    FeedSource("techflow", "TechFlow 深潮", "https://www.techflowpost.com/newsletter",
               "techflow", "2026-11-18", 72, "中文快讯；/rss 返回 HTML，读 newsletter 列表页"),
    FeedSource("abmedia", "ABMedia 链新闻", "https://abmedia.io/feed",
               "rss", "2026-11-18", 72, "海外繁体中文，不转简体；GitHub 出口对完整浏览器头返回 403，改用最简请求头",
               {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Accept-Language": "",
                "Sec-Fetch-Dest": "", "Sec-Fetch-Mode": "", "Sec-Fetch-Site": "",
                "Sec-Fetch-User": "", "sec-ch-ua": "", "sec-ch-ua-mobile": "",
                "sec-ch-ua-platform": ""}),
    FeedSource("blocktempo", "BlockTempo 动区", "https://www.blocktempo.com/feed/",
               "rss", "2026-11-18", 72, "海外繁体中文，不转简体"),
    FeedSource("blockcast", "区块客", "https://blockcast.it/feed/",
               "rss", "2026-11-18", 72, "海外繁体中文，不转简体"),
    FeedSource("zombit", "桑币网 Zombit", "https://zombit.info/feed/",
               "rss", "2026-11-18", 72, "海外繁体中文，不转简体"),
    FeedSource("foresight", "Foresight News", "https://rsshub.rssforever.com/foresightnews",
               "rss", "2026-11-18", 72, "RSSHub 路由，繁体中文快讯，不转简体"),
    FeedSource("eqn_main", "方程式新闻 main", "https://api.eqn.news/public/rss?ch=main&lang=zh",
               "rss", "2026-11-17", 72, "只接 main（监管/被黑/裁决）；其他频道是行情公告，不接"),
    FeedSource("ns3_ranking", "ns3 重要榜", "https://api.ns3.ai/feed/news-ranking",
               "rss", "2026-11-17", 72, "当天前 10 重要，只作粗筛；忽略 insight 字段"),
    FeedSource("x_wublockchain", "X @wublockchainenglish",
               "https://t.me/s/wublockchainenglish", "telegram", "2026-11-17", 72,
               "亚洲监管；公开预览页", channel="wublockchainenglish"),
    FeedSource("x_investigations", "X @investigations",
               "https://t.me/s/investigations", "telegram", "2026-11-17", 72,
               "被黑追赃；公开预览页", channel="investigations"),
    FeedSource("x_watcherguru", "X @WatcherGuru",
               "https://t.me/s/WatcherGuru", "telegram", "2026-11-17", 72,
               "快讯，当线索不当事实；公开预览页", channel="WatcherGuru"),
    FeedSource("x_cointelegraph", "X @cointelegraph",
               "https://t.me/s/cointelegraph", "telegram", "2026-11-17", 72,
               "综合新闻；公开预览页", channel="cointelegraph"),
]

SOURCE_BY_NAME = {src.name: src for src in FEEDS}
# 本机可读但 GitHub 出口固定 403 的源：按预期不可直读，不触发暂停推送/健康告警。
KNOWN_BLOCKED = {"abmedia"}
OFFICIAL_PROMOTABLE = {"sec_official", "cftc_general", "cftc_enforcement"}
RESEARCH_SOURCES = {"trailofbits"}


@dataclass
class SourceReport:
    name: str
    label: str
    layer: str
    ok: bool
    status: str = ""
    error: str = ""
    items: List[Item] = field(default_factory=list)
    duration_ms: int = 0
    expected: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name, "label": self.label, "layer": self.layer,
            "status": self.status, "error": self.error[:300], "items": len(self.items),
            "duration_ms": self.duration_ms, "expected": self.expected,
        }


def _first_text(node: ET.Element, tags: Iterable[str]) -> str:
    for tag in tags:
        found = node.find(tag)
        if found is None:
            continue
        if found.text and found.text.strip():
            return found.text.strip()
        href = found.attrib.get("href", "")
        if href:
            return href.strip()
    return ""


def parse_time(raw: str) -> str:
    """把 RSS/页面时间统一成 UTC ISO；解析不出来返回空字符串（后续按字段缺处理）。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{10,13}", text):
        number = int(text)
        if number > 10_000_000_000:
            number //= 1000
        return datetime.fromtimestamp(number, UTC).isoformat()
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    normalized = text.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(normalized[:30], fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat()
        except ValueError:
            continue
    return ""


def _strip(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _rss_items(data: bytes, src: FeedSource) -> List[Item]:
    if re.search(br"<!\s*ENTITY\b", data[:4096], re.I):
        raise http.FetchFailure("XML 含 ENTITY 声明，拒绝解析", "unsafe_xml")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise http.FetchFailure(f"XML 解析失败: {exc}", "parse_error") from exc
    tag = root.tag.split("}")[-1].lower()
    if tag == "rss":
        nodes = root.findall("./channel/item")
    elif tag == "feed":
        nodes = root.findall("./{http://www.w3.org/2005/Atom}entry") or root.findall("./entry")
    else:
        raise http.FetchFailure(f"不是 RSS/Atom（根节点 {tag}）", "not_rss")
    items: List[Item] = []
    for node in nodes[:50]:
        title = _strip(_first_text(node, ("title", "{http://www.w3.org/2005/Atom}title")))
        link = _first_text(node, ("link", "{http://www.w3.org/2005/Atom}link", "guid"))
        if not title or not link:
            continue
        published = parse_time(_first_text(
            node, ("pubDate", "published", "{http://www.w3.org/2005/Atom}published",
                   "updated", "{http://www.w3.org/2005/Atom}updated", "dc:date")))
        summary = _strip(_first_text(
            node, ("description", "summary", "{http://www.w3.org/2005/Atom}summary",
                   "{http://www.w3.org/2005/Atom}content")))[:1200]
        items.append(Item(src.name, title[:500], urljoin(src.url, link), published,
                          summary, "rss", src.label))
    return items


_BLOCK_TITLE_RE = re.compile(
    r'<a\s+href="(/flash/\d+)"\s+title="([^"]{4,300})"[^>]*>\s*(\d{1,2}:\d{2})', re.I | re.S)
_BLOCK_DATE_RE = re.compile(r'flash-list-today[^>]*>(20\d{2}-\d{1,2}-\d{1,2})')


def _blockbeats_items(page: str, src: FeedSource) -> List[Item]:
    date_match = _BLOCK_DATE_RE.search(page)
    day = date_match.group(1) if date_match else datetime.now(CST).strftime("%Y-%m-%d")
    items: List[Item] = []
    seen = set()
    for link, title, clock in _BLOCK_TITLE_RE.findall(page):
        url = urljoin(src.url, link)
        if url in seen:
            continue
        seen.add(url)
        try:
            hour, minute = (int(part) for part in clock.split(":"))
            # 该站跨午夜用 24:xx 表示次日 00:xx，直接 strptime 会失败并造成字段缺失。
            stamp = (datetime.strptime(f"{day} 00:{minute:02d}", "%Y-%m-%d %H:%M")
                     + timedelta(hours=hour))
            published = stamp.replace(tzinfo=CST).astimezone(UTC).isoformat()
            if datetime.fromisoformat(published) > datetime.now(UTC) + timedelta(minutes=10):
                published = (stamp - timedelta(days=1)).replace(tzinfo=CST).astimezone(UTC).isoformat()
        except ValueError:
            published = ""
        items.append(Item(src.name, _strip(title)[:500], url, published, "", "html", src.label))
        if len(items) >= 50:
            break
    return items


_ODAILY_BLOCK_RE = re.compile(r'<div class="newsflash-item[^"]*"\s+data-publish-timestamp="(\d{10,13})"')
_ODAILY_LINK_RE = re.compile(r'href="(/zh-CN/newsflash/\d+)"')


def _odaily_items(page: str, src: FeedSource) -> List[Item]:
    matches = list(_ODAILY_BLOCK_RE.finditer(page))
    items: List[Item] = []
    for index, match in enumerate(matches[:50]):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(page)
        block = page[match.start():end]
        link_match = _ODAILY_LINK_RE.search(block)
        # 页面里时间标签也是 span；跳过纯 HH:MM，避免把 16:15 当标题。
        title = ""
        for span in re.finditer(r'<span class="text[^"]*"[^>]*>(.*?)</span>', block[:5000], re.S):
            candidate = _strip(span.group(1))
            if len(candidate) >= 4 and not re.fullmatch(r"\d{1,2}:\d{2}", candidate):
                title = candidate
                break
        if not link_match or not title:
            continue
        summary_match = re.search(r"<p>(.*?)</p>", block, re.S)
        items.append(Item(
            src.name, title[:500], urljoin(src.url, link_match.group(1)),
            parse_time(match.group(1)), _strip(summary_match.group(1))[:1200] if summary_match else "",
            "html", src.label))
    return items


_TECHFLOW_RE = re.compile(
    r'<a[^>]+aria-label="([^"]{4,300})"[^>]+href="(/newsletter/\d+)"[^>]*></a>(.{0,1200}?)'
    r'<span class="text-\[10px\][^"]*"[^>]*>(\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})</span>', re.S)


def _techflow_items(page: str, src: FeedSource) -> List[Item]:
    items: List[Item] = []
    seen = set()
    now = datetime.now(CST)
    for title, link, _middle, clock in _TECHFLOW_RE.findall(page)[:50]:
        url = urljoin(src.url, link)
        if url in seen:
            continue
        seen.add(url)
        try:
            month, day = (int(part) for part in clock.split()[0].split("/"))
            hour, minute = (int(part) for part in clock.split()[1].split(":"))
            published_dt = datetime(now.year, month, day, hour, minute, tzinfo=CST)
            if published_dt > now + timedelta(minutes=10):
                published_dt = published_dt.replace(year=now.year - 1)
            published = published_dt.astimezone(UTC).isoformat()
        except ValueError:
            published = ""
        items.append(Item(src.name, _strip(title)[:500], url, published, "", "html", src.label))
    return items


def _telegram_messages(page: str) -> List[dict]:
    """t.me 公开预览页拆成 [{text, time, post}]。"""
    messages = []
    for block in re.split(r'<div class="tgme_widget_message_wrap', page):
        text_match = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not text_match:
            continue
        text = re.sub(r"<br\s*/?>", "\n", text_match.group(1))
        text = html_lib.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        time_match = re.search(r'<time datetime="([^"]+)"', block)
        post_match = re.search(r'data-post="([^"]+)"', block)
        messages.append({"text": text, "time": time_match.group(1) if time_match else "",
                         "post": post_match.group(1) if post_match else ""})
    return messages


def _telegram_items(page: str, src: FeedSource) -> List[Item]:
    items: List[Item] = []
    for message in _telegram_messages(page)[:50]:
        text = (message["text"] or "").strip()
        if not text:
            continue
        title = re.split(r"\n", text, maxsplit=1)[0].strip()[:500]
        if len(title) < 6:
            continue
        url = (f"https://t.me/{message['post']}" if message["post"]
               else f"https://t.me/s/{src.channel}")
        items.append(Item(src.name, title, url, parse_time(message["time"]),
                          text[:1200], "html", src.label, {"channel": src.channel}))
    return items


def parse_payload(src: FeedSource, result: http.FetchResult) -> List[Item]:
    if src.kind == "rss":
        return _rss_items(result.data, src)
    page = result.text()
    if src.kind == "blockbeats":
        return _blockbeats_items(page, src)
    if src.kind == "odaily":
        return _odaily_items(page, src)
    if src.kind == "techflow":
        return _techflow_items(page, src)
    if src.kind == "telegram":
        return _telegram_items(page, src)
    raise http.FetchFailure(f"未知源类型: {src.kind}", "config_error")


def _newest_hours(items: List[Item], now: datetime) -> Optional[float]:
    stamps = []
    for item in items:
        try:
            stamps.append(datetime.fromisoformat(item.published_at))
        except ValueError:
            continue
    if not stamps:
        return None
    return (now - max(stamps)).total_seconds() / 3600


def fetch_source(src: FeedSource, *, timeout: float = 20.0,
                 now: Optional[datetime] = None) -> SourceReport:
    started = time.time()
    try:
        result = http.fetch(src.url, timeout=timeout, headers=src.headers,
                            check_login_wall=(src.kind != "rss"))
    except Exception as exc:  # noqa: BLE001 单源配置/网络异常必须隔离，不能拖垮整轮
        return SourceReport(src.name, src.label, src.kind, False, "provider_error",
                            f"{type(exc).__name__}: {exc}",
                            duration_ms=int((time.time() - started) * 1000))
    if not result.ok:
        return SourceReport(src.name, src.label, src.kind, False,
                            result.error_type, result.error, duration_ms=result.duration_ms,
                            expected=(src.name in KNOWN_BLOCKED and result.error_type == "blocked"))
    try:
        items = parse_payload(src, result)
    except http.FetchFailure as exc:
        return SourceReport(src.name, src.label, src.kind, False, exc.error_type, str(exc),
                            duration_ms=int((time.time() - started) * 1000))
    except Exception as exc:  # noqa: BLE001 单源解析异常必须被隔离
        return SourceReport(src.name, src.label, src.kind, False, "low_quality",
                            f"{type(exc).__name__}: {exc}",
                            duration_ms=int((time.time() - started) * 1000))
    if not items:
        return SourceReport(src.name, src.label, src.kind, False, "missing",
                            "解析后 0 条，按缺失处理，不视为正常空轮",
                            duration_ms=int((time.time() - started) * 1000))
    hours = _newest_hours(items, now or datetime.now(UTC))
    if hours is not None and hours > src.fresh_hours:
        return SourceReport(src.name, src.label, src.kind, False, "stale",
                            f"最新条目距今 {hours:.1f} 小时，超过 {src.fresh_hours} 小时阈值",
                            items, int((time.time() - started) * 1000))
    return SourceReport(src.name, src.label, src.kind, True, "ok", "", items,
                        int((time.time() - started) * 1000))


def to_item(row: dict) -> Item:
    """本地样例用：字段白名单构造，避免样例夹带任意字段。"""
    fields = set(Item.__dataclass_fields__)
    return Item(**{key: value for key, value in row.items() if key in fields})
