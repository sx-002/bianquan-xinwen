"""0 模型规则分级：只报事实，不推断涨跌。

继承旧方案的关键点：词形变体写全、裸词不判危、短词完整 token 边界、
高置信/中等信号两层、污染度降权、事件半衰期、官方窄流有限提权、
研究类源 critical 必须标题命中、每条判定记规则名可审计。
交易所主体事件与链上大额转账在这里直接让路，不重复推。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from . import sources

# 繁转简只用于生成推送消息；未装依赖时保留原文，自检会直接报错提示补装。
try:
    from opencc import OpenCC
    _TO_SIMPLIFIED = OpenCC("t2s")
except Exception:  # noqa: BLE001 依赖缺失不应让采集/干跑整体崩掉
    _TO_SIMPLIFIED = None


def to_simplified(text: str) -> str:
    return _TO_SIMPLIFIED.convert(text) if _TO_SIMPLIFIED and text else (text or "")

UTC = timezone.utc
CST = timezone(timedelta(hours=8))

HIGH = "high"            # 高
MEDIUM_HIGH = "medium_high"  # 中高
MEDIUM = "medium"        # 中（只存档）
LOW = "low"              # 低（只存档）
LEVEL_CN = {HIGH: "高", MEDIUM_HIGH: "中高", MEDIUM: "中", LOW: "低"}
LEVEL_RANK = {LOW: 0, MEDIUM: 1, MEDIUM_HIGH: 2, HIGH: 3}
PUSHABLE = {HIGH, MEDIUM_HIGH}

#: 中文过滤：标题里没有汉字、且标题+摘要的中文占比低于这个值，就判为纯英文不推。
MIN_CHINESE_RATIO = 0.05
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def token(word: str, *, plural: bool = True) -> str:
    """短词必须完整 token 匹配：sec 不能命中 second，etf 可以是 etfs。"""
    return r"(?<![A-Za-z0-9])" + re.escape(word) + (r"(?:s)?" if plural else "") + r"(?![A-Za-z0-9])"


def variants(stem: str, forms: Tuple[str, ...]) -> str:
    return r"(?:" + "|".join(stem + form for form in forms) + r")"


SUSPEND = variants("suspend", ("", "s", "ed", "ing")) + r"|suspension"
HALT = variants("halt", ("", "s", "ed", "ing"))
PAUSE = variants("pause", ("", "s", "d", "ing"))
FREEZE = r"freez(?:e|es|ing)|frozen"
EXPLOIT = r"exploit(?:s|ed|ing|ation)?|hack(?:s|ed|ing|er|ers)?|breach(?:es|ed)?|" \
          r"stolen|stole|theft|drain(?:s|ed|ing)?|compromised|vulnerabilit(?:y|ies)|" \
          r"被黑|黑客|攻击|入侵|漏洞|被盗|盗取|窃取|损失.{0,12}(?:美元|USDT|BTC|ETH)"
DEPEG = r"depeg(?:ged|ging)?|loses? peg|脱锚|脱钩|稳定币.*跌破"
ENFORCE = r"起诉|指控|罚款|裁决|判决|逮捕|制裁|禁令|没收|冻结|立案|批准|通过|" \
          r"charged|sues?|lawsuit|fined|ruling|verdict|arrested|sanctioned|" \
          r"banned|approves?|approved|order(?:ed)?|judgment|settlement"
REGULATOR = r"(?:" + token("sec") + r"|" + token("cftc") + r"|" + token("doj") + r"|" \
            r"(?:" + token("fbi") + r")|" + token("ftc") + r"|OFAC|" \
            r"监管|监管机构|证监会|司法部|法院|法官|陪审团|参议院|国会|" \
            r"regulator|regulators|regulatory|court|judge|senate|congress)"

CRYPTO_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:BTC|ETH|USDT|USDC|DeFi|NFT|Web3|SEC|CFTC|ETF|DAO)(?:s)?(?![A-Za-z0-9])|"
    r"bitcoin|ethereum|crypto(?:currency)?|blockchain|stablecoin|altcoin|token|"
    r"比特币|以太坊|区块链|加密|代币|链上|协议|挖矿|矿工|稳定币|Defi|主网|快讯",
    re.I)
TRADFI_RE = re.compile(r"股市|美股|外汇|黄金|原油|大宗商品|美联储利率|CPI|非农|"
                       r"stock market|forex|commodity|treasury yield", re.I)
MAJOR_ASSET_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:BTC|ETH|Bitcoin|Ethereum|比特币|以太坊)(?![A-Za-z0-9])", re.I)

# 交易所主体事件 → 交易所风险监控仓；链上大额转账 → 巨鲸仓。
EXCHANGE_NAMES = (
    "Binance|币安|OKX|欧易|Bybit|Bitget|Gate|Coinbase|Kraken|KuCoin|MEXC|HTX|"
    "Upbit|Bithumb|Bitfinex|Crypto\\.com|XT|CoinW|BitMart|WEEX|Deepcoin|Hotcoin"
)
EXCHANGE_RE = re.compile(r"(?:" + EXCHANGE_NAMES + r")|交易所|交易平台", re.I)
EXCHANGE_OPERATIONAL_RE = re.compile(
    r"(?:暂停|停止|恢复|限制|延迟).{0,12}(?:提币|提现|充值|充提|出入金)|"
    r"(?:提币|提现|充值).{0,12}(?:暂停|停止|限制|延迟|失败|不到账)|"
    r"(?:维护|系统升级|登录不了|无法登录|登不上|客服失联|跑路|卷款|破产|清盘|监管冻结|资产冻结)|"
    r"withdraw(?:al|als)?\s+(?:" + SUSPEND + r"|" + HALT + r"|" + PAUSE + r"|delayed|failed)|"
    r"deposits?\s+(?:" + SUSPEND + r"|" + HALT + r"|" + PAUSE + r"|disabled)|"
    r"maintenance|(?:site|service)\s+unavailable|login failed|exit scam|insolvency|"
    r"funds?\s+frozen|被黑|被盗|" + EXPLOIT + r"|" + DEPEG,
    re.I)
WHALE_RE = re.compile(
    r"巨鲸|whale\s+alert|大额转账|转入.{0,6}(?:交易所|地址)|转出.{0,6}(?:交易所|地址)|"
    r"(?:转移|转出|转入|存入|提走)\s*[\d,]+|"
    r"(?:transferred|moved|sent|deposited|withdrew)\s+[\d,]+|"
    # 英文快讯常写 wallets move $137 Million，动词是原形、金额带货币符号。
    r"(?:move[sd]?|transfer(?:red|s)?|sent|send)\s+[$¥€]\s*[\d,]+|"
    r"wallet(?:s)?\s+(?:move[sd]?|transfer(?:red)?|sent)|0x[0-9a-fA-F]{8,}",
    re.I)

# 有序规则：首个高置信命中决定类别；中等信号可多类累计。
RULES: List[Tuple[str, str, str, str, int]] = [
    ("K01_security_exploit", "security", "high", EXPLOIT, 45),
    ("K02_stablecoin_depeg", "security", "high", DEPEG, 45),
    ("K03_regulatory_enforcement", "regulatory", "high",
     REGULATOR + r".{0,40}(?:" + ENFORCE + r")|(?:" + ENFORCE + r").{0,40}" + REGULATOR, 240),
    ("K04_regulatory_policy", "regulatory", "medium",
     r"法案|条例|新规|监管框架|立法|提议|草案|听证|" + token("clarity") + r"|MiCA|"
     r"GENIUS Act|bill|regulation|rule proposal|framework|hearing", 240),
    ("K05_funding_ma", "funding", "medium",
     r"融资|完成.{0,8}轮|收购|并购|注资|投资|" + token("funding") + r"|raised|"
     r"series\s+[A-D]|acquired|acquisition|merger", 240),
    ("K06_project_upgrade", "project", "medium",
     r"主网|硬分叉|升级| testnet|mainnet|hard[- ]?fork|upgrade|launch(?:ed|es)?|release", 360),
    ("K07_listing", "project", "medium",
     r"上币|下币|交易对|开放交易|listing|delisting|listed on|added to", 90),
    ("K08_person_statement", "statement", "medium",
     r"称|表示|表态|宣布|警告|官员|CEO|创始人|"
     r"said|says|announced|warned|stated|spokesperson", 240),
    ("K09_market", "market", "low",
     r"暴涨|暴跌|涨幅|跌幅|爆仓|清算|创新高|跌破.{0,8}美元|价格|行情|"
     r"rally|sell[- ]?off|liquidated|price surge|market cap", 45),
]
COMPILED = [(name, category, confidence, re.compile(pattern, re.I), half_life)
            for name, category, confidence, pattern, half_life in RULES]

CONTAMINATION_RULES: List[Tuple[str, re.Pattern]] = [
    ("rumor_wording", re.compile(r"rumor|reportedly|sources say|unconfirmed|"
                                 r"传闻|据称|消息人士|未经证实|网传", re.I)),
    ("anonymous_source", re.compile(r"anonymous|知情人士|匿名消息|不愿具名", re.I)),
    ("social_reshare", re.compile(r"retweet|viral|widely shared|转发|刷屏|社媒热传", re.I)),
]
SPECULATION_RE = re.compile(
    r"by 20\d{2}|到 20\d{2}|预测|目标价|涨到|跌到|almost certainly|"
    r"will (?:reach|hit|surge)|could (?:reach|hit)|price prediction|"
    r"what happened in .{0,20} today|daily recap|market wrap|今日回顾|一周回顾",
    re.I)
TICKER_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,6})(?![A-Za-z0-9])")
KNOWN_TICKERS = {"BTC", "ETH", "USDT", "USDC", "BNB", "SOL", "XRP", "DOGE", "TRX",
                 "ADA", "DOT", "LTC", "BCH", "ARB", "OP", "TON", "APT", "SUI", "ZEC"}


@dataclass
class Decision:
    level: str
    category: str
    rule_name: str
    reason: str = ""
    signals: List[str] = field(default_factory=list)
    contamination: int = 0
    half_life: int = 0
    expired: bool = False
    pushable: bool = False
    route: str = ""          # exchange / whale，非空表示让别的仓报
    archived: bool = False

    def to_dict(self) -> dict:
        return {
            "level": LEVEL_CN[self.level], "level_key": self.level,
            "category": self.category, "rule": self.rule_name, "reason": self.reason,
            "signals": self.signals, "contamination": self.contamination,
            "half_life_minutes": self.half_life, "expired": self.expired,
            "pushable": self.pushable, "route": self.route, "archived": self.archived,
        }


def chinese_ratio(text: str) -> float:
    """标题+摘要里汉字占「有意义字符（字母、数字、汉字）」的比例。"""
    chars = [ch for ch in (text or "") if ch.isalnum() or CJK_RE.match(ch)]
    if not chars:
        return 0.0
    cjk = sum(1 for ch in chars if CJK_RE.match(ch))
    return cjk / len(chars)


def is_non_chinese(title: str, summary: str = "") -> bool:
    """纯英文（读不懂）判 True，挡在推送之前。

    标题里只要有汉字就保留：中文标题配一段英文摘要的条目仍然看得懂，
    不能因为摘要长就把中文条目挡掉；专有名词、代码、币种符号是英文没关系。
    """
    if CJK_RE.search(title or ""):
        return False
    return chinese_ratio(f"{title or ''} {summary or ''}") < MIN_CHINESE_RATIO


def non_chinese_decision() -> Decision:
    return Decision(LOW, "language", "non_chinese_filter",
                    "纯英文内容，用户看不懂，挡在推送之前", archived=True)


def _downgrade(level: str, steps: int = 1) -> str:
    order = [LOW, MEDIUM, MEDIUM_HIGH, HIGH]
    return order[max(0, order.index(level) - steps)]


def _parse(raw: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat((raw or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def judge(title: str, summary: str = "", *, source: str = "",
          published_at: str = "", now: Optional[datetime] = None) -> Decision:
    text = unicodedata.normalize("NFKC", " ".join(part for part in (title, summary) if part)).strip()
    if not text:
        return Decision(LOW, "unknown", "empty_text", "标题与摘要都为空", pushable=False, archived=True)

    # 让路规则优先：谁的主场谁报。
    if WHALE_RE.search(text):
        return Decision(LOW, "transfer", "route_whale_transfer",
                        "链上大额转账归巨鲸转账监控仓，本仓不重复", route="whale", archived=True)
    if EXCHANGE_RE.search(text) and EXCHANGE_OPERATIONAL_RE.search(text):
        return Decision(LOW, "exchange", "route_exchange_monitor",
                        "交易所主体事件归交易所风险监控仓，本仓不重复", route="exchange", archived=True)

    if not CRYPTO_RE.search(text) or (TRADFI_RE.search(text) and not CRYPTO_RE.search(title)):
        return Decision(LOW, "unknown", "not_crypto_related",
                        "与币圈无高度关联或纯传统金融，只存档", archived=True)

    matched = [(name, category, confidence, m.group(0)[:60], half_life)
               for name, category, confidence, pattern, half_life in COMPILED
               if (m := pattern.search(text))]
    title_norm = unicodedata.normalize("NFKC", title or "")
    high_hits = [row for row in matched if row[2] == "high"]
    if not high_hits and SPECULATION_RE.search(title_norm):
        return Decision(LOW, "market", "market_speculation_archived",
                        "价格预测/每日回顾/行情盘点，不是可核实的事实，只存档",
                        signals=[row[0] for row in matched], archived=True)
    if not matched:
        return Decision(LOW, "unknown", "category_unverified",
                        "未命中任何事件类别，分类未确认，只存档", archived=True)

    medium_hits = [row for row in matched if row[2] == "medium"]
    categories = sorted({row[1] for row in matched})
    reasons = [f"命中规则 {row[0]}：{row[3]}" for row in matched[:4]]
    half_life = min(row[4] for row in matched)

    if high_hits:
        level = HIGH
        category = high_hits[0][1]
        rule_name = high_hits[0][0]
        signals = [row[0] for row in high_hits]
        reasons.append("高置信信号单独命中即达高")
    elif medium_hits:
        major = bool(MAJOR_ASSET_RE.search(text))
        enough = len({row[1] for row in medium_hits}) >= 2 or major
        level = MEDIUM_HIGH if enough else MEDIUM
        category = medium_hits[0][1]
        rule_name = medium_hits[0][0] if enough else "medium_signal_not_accumulated"
        signals = [row[0] for row in medium_hits]
        reasons.append("中等信号累计 2 类或含 BTC/ETH" if enough else "中等信号仅 1 类且无 BTC/ETH，只存档")
    else:
        level, category, rule_name, signals = LOW, "market", "K09_market_only", ["K09_market"]
        reasons.append("纯行情类只存档")

    # 污染度：传闻措辞、匿名信源、社交反复转、堆代币名，最高权重降权。
    contamination = 0
    for label, pattern in CONTAMINATION_RULES:
        if pattern.search(text):
            contamination += 1
            reasons.append(f"污染度命中 {label}")
    tickers = set(TICKER_RE.findall(text)) & KNOWN_TICKERS
    if len(tickers) >= 5:
        contamination += 1
        reasons.append(f"污染度命中 token_spam（{len(tickers)} 个代币名）")
    if contamination >= 3:
        level = MEDIUM
        reasons.append("污染度 >=3，降到中，只存档")
    elif contamination == 2:
        level = _downgrade(level)
        reasons.append("污染度 =2，降一级")

    # 研究类源：critical 必须标题本身命中，摘要复盘旧事故不算。
    if source in sources.RESEARCH_SOURCES and level == HIGH and high_hits:
        title_hit = any(re.compile(pattern, re.I).search(title_norm)
                        for pattern in (EXPLOIT, DEPEG))
        if not title_hit:
            level = MEDIUM
            reasons.append("研究类源仅摘要命中 critical，按标题未命中降到中")

    # 官方窄流有限提权：只有 SEC/CFTC 在监管类别上可凭来源提级。
    if (source in sources.OFFICIAL_PROMOTABLE and category == "regulatory"
            and level in (MEDIUM, MEDIUM_HIGH) and contamination == 0):
        level = HIGH
        rule_name = "official_source_promotion"
        reasons.append("SEC/CFTC 官方窄流，监管类别有限提权")

    # 事件半衰期：超时降权不推。
    expired = False
    parsed = _parse(published_at)
    if parsed is None:
        level = _downgrade(level, 2)
        reasons.append("发布时间缺失或无法解析，字段缺失按低质处理")
    else:
        age = ((now or datetime.now(UTC)) - parsed).total_seconds() / 60
        if age < -10:
            level = _downgrade(level, 2)
            reasons.append("发布时间明显超前，按低质处理")
        elif age > half_life:
            expired = True
            level = MEDIUM
            reasons.append(f"超过 {half_life} 分钟半衰期，降权不推")

    pushable = level in PUSHABLE and not expired and not TRADFI_RE.search(category)
    return Decision(
        level=level, category=category, rule_name=rule_name, reason="；".join(reasons),
        signals=signals, contamination=contamination, half_life=half_life,
        expired=expired, pushable=pushable, archived=not pushable,
    )


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title or "").lower()
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)[:200]


def format_time(published_at: str) -> str:
    parsed = _parse(published_at)
    return parsed.astimezone(CST).strftime("%Y-%m-%d %H:%M") if parsed else "原文未给时间"


def format_message(item, decision: Decision) -> str:
    """严格按用户口径：标题｜时间｜原文链接，一条一行，不堆背景不写解读。

    只有推送文本做繁转简；item 本身与 state/归档保留原始标题，方便对照原文。
    """
    title = to_simplified(item.title.strip())
    return f"{title}｜{format_time(item.published_at)}｜{item.url.strip()}"
