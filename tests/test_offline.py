"""离线自检：不联网、不需要任何 token，也不触碰真实 state。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitor import cloud, rules, run as monitor_run, sources, state as state_mod  # noqa: E402

UTC = timezone.utc
NOW = datetime.now(UTC).replace(microsecond=0)
NOW_ISO = NOW.isoformat()


class TestSources(unittest.TestCase):
    def test_feed_list_is_scope_limited(self):
        self.assertEqual(len(sources.FEEDS), 24)
        names = {src.name for src in sources.FEEDS}
        self.assertIn("eqn_main", names)
        self.assertIn("ns3_ranking", names)
        for name in ("abmedia", "blocktempo", "blockcast", "zombit", "foresight"):
            self.assertIn(name, names)
            self.assertEqual(sources.SOURCE_BY_NAME[name].kind, "rss")
            self.assertTrue(sources.SOURCE_BY_NAME[name].verified_at)
        self.assertNotIn("fed_monetary", names)
        self.assertNotIn("sosovalue", names)
        for src in sources.FEEDS:
            self.assertTrue(src.verified_at)
            self.assertNotIn("status.", src.url)

    def test_rss_parse(self):
        payload = (b'<rss version="2.0"><channel><item>'
                   b'<title>CFTC approves test</title><link>https://example.com/a</link>'
                   b'<pubDate>Tue, 17 Nov 2026 10:00:00 GMT</pubDate>'
                   b'<description>body</description></item></channel></rss>')
        src = next(src for src in sources.FEEDS if src.name == "cftc_general")
        result = http_result(payload)
        items = sources.parse_payload(src, result)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].published_at.startswith("2026-11-17T10:00"))

    def test_blockbeats_parse(self):
        day = datetime.now(sources.CST).strftime("%Y-%m-%d")
        page = (f'<div class="flash-list-today">{day}</div>'
                '<a href="/flash/377840" title="协议被黑损失 100 万美元" target="_blank" class="x">'
                '\n 00:01 <div>协议被黑损失 100 万美元</div></a>')
        src = next(src for src in sources.FEEDS if src.name == "blockbeats")
        items = sources.parse_payload(src, http_result(page.encode()))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://www.theblockbeats.info/flash/377840")
        local = datetime.fromisoformat(items[0].published_at).astimezone(sources.CST)
        self.assertEqual(local.strftime("%Y-%m-%d %H:%M"), f"{day} 00:01")
        midnight_page = page.replace("00:01", "24:01")
        midnight = sources.parse_payload(src, http_result(midnight_page.encode()))
        self.assertTrue(midnight[0].published_at, "24:xx 跨午夜时间不能变成字段缺失")

    def test_odaily_parse(self):
        page = ('<div class="newsflash-item" data-publish-timestamp="1794932107000">'
                '<span class="text-custom">某协议被黑</span>'
                '<a href="/zh-CN/newsflash/535816">详情</a><p>正文</p></div>')
        src = next(src for src in sources.FEEDS if src.name == "odaily")
        items = sources.parse_payload(src, http_result(page.encode()))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "某协议被黑")
        self.assertTrue(items[0].published_at.startswith("2026-11-"))

    def test_techflow_parse(self):
        page = ('<a target="_blank" aria-label="CFTC 暂停某合约上市" '
                'class="absolute inset-0 z-0" href="/newsletter/145263"></a>'
                '<span class="text-[10px] text-gray">11/17 16:36</span>')
        src = next(src for src in sources.FEEDS if src.name == "techflow")
        items = sources.parse_payload(src, http_result(page.encode()))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://www.techflowpost.com/newsletter/145263")

    def test_telegram_preview_parse(self):
        page = ('<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" '
                'data-post="wublockchainenglish/123"></div>'
                '<div class="tgme_widget_message_text">监管消息：某协议被黑</div>'
                '<time datetime="2026-11-18T01:00:00Z"></time></div>')
        src = next(src for src in sources.FEEDS if src.name == "x_wublockchain")
        items = sources.parse_payload(src, http_result(page.encode()))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://t.me/wublockchainenglish/123")

    def test_newest_hours_detects_stale(self):
        item = sources.Item("sec_official", "t", "https://example.com/a",
                            (datetime.now(UTC) - timedelta(days=10)).isoformat())
        self.assertGreater(sources._newest_hours([item], datetime.now(UTC)), 72)


class TestRules(unittest.TestCase):
    def judge(self, title, summary="", **kwargs):
        return rules.judge(title, summary, published_at=kwargs.pop("published_at", NOW_ISO), **kwargs)

    def test_binance_hack_wallet_move_routes_away(self):
        title = ("Binance Hack Wallets Move $137 Million in Ethereum "
                 "After Two Weeks of Silence")
        decision = self.judge(title)
        self.assertIn(decision.route, ("exchange", "whale"))
        self.assertFalse(decision.pushable)

    def test_hack_is_high_and_auditable(self):
        decision = self.judge("某 DeFi 协议遭黑客攻击，损失 1200 万美元")
        self.assertEqual(decision.level, rules.HIGH)
        self.assertTrue(decision.rule_name.startswith("K01"))
        self.assertIn("K01", decision.reason)

    def test_word_variants(self):
        for title in ("协议 suspended withdrawals 引发争议", "协议 halted withdrawals",
                      "协议 pauses withdrawals", "协议 frozen funds"):
            self.assertTrue(rules.SUSPEND in title or rules.HALT in title
                            or rules.PAUSE in title or rules.FREEZE in title
                            or any(word in title for word in ("suspended", "halted", "pauses", "frozen")))

    def test_bare_words_not_high(self):
        for title in ("US government shutdown 继续", "Clarity Act fails Senate vote",
                      "银行 default 风险上升", "公司 collapse 引发讨论"):
            decision = self.judge(title)
            self.assertNotEqual(decision.level, rules.HIGH, title)

    def test_short_token_boundary(self):
        import re
        sec = re.compile(rules.token("sec"), re.I)
        self.assertTrue(sec.search("SEC 起诉"))
        self.assertFalse(sec.search("second quarter"))
        etf = re.compile(rules.token("etf"), re.I)
        self.assertTrue(etf.search("ETFs"))
        self.assertFalse(etf.search("etfsomething"))

    def test_exchange_and_whale_route_away(self):
        self.assertEqual(self.judge("Binance 暂停提现").route, "exchange")
        self.assertEqual(self.judge("某交易所维护延期").route, "exchange")
        self.assertEqual(self.judge("某巨鲸转移 9938 枚 ZEC 至新地址").route, "whale")
        self.assertEqual(self.judge("某协议被盗 100 万美元").route, "")

    def test_medium_signal_needs_accumulation(self):
        self.assertEqual(self.judge("某协议完成 1000 万美元融资").level, rules.MEDIUM)
        self.assertEqual(self.judge("BTC 生态项目完成 1000 万美元融资").level, rules.MEDIUM_HIGH)

    def test_contamination_downgrade(self):
        decision = self.judge("BTC 协议据称遭黑客攻击，损失 100 万美元")
        self.assertGreaterEqual(decision.contamination, 1)
        self.assertIn("污染度", decision.reason)

    def test_half_life_expires(self):
        decision = rules.judge("某协议遭黑客攻击，损失 500 万美元",
                               published_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat())
        self.assertTrue(decision.expired)
        self.assertFalse(decision.pushable)
        self.assertIn("半衰期", decision.reason)

    def test_research_source_title_required(self):
        decision = self.judge("Trail of Bits 发布审计报告", "回顾某协议被黑损失 1 亿美元",
                              source="trailofbits")
        self.assertEqual(decision.level, rules.MEDIUM)
        self.assertIn("标题未命中", decision.reason)

    def test_official_source_limited_promotion(self):
        decision = self.judge("SEC 发布新规征求意见稿", source="sec_official")
        self.assertEqual(decision.level, rules.HIGH)
        self.assertEqual(decision.rule_name, "official_source_promotion")

    def test_speculation_and_roundup_archived(self):
        for title in ("Bitcoin to $1M by 2030? ‘Almost certainly not,’ says Qureshi",
                      "Here's what happened in crypto today"):
            decision = self.judge(title)
            self.assertFalse(decision.pushable)
            self.assertEqual(decision.rule_name, "market_speculation_archived")

    def test_missing_time_not_pushable(self):
        decision = rules.judge("某协议遭黑客攻击", published_at="")
        self.assertFalse(decision.pushable)
        self.assertIn("发布时间缺失", decision.reason)

    def test_message_format(self):
        item = sources.Item("x", "标题 A", "https://example.com/a", NOW_ISO)
        decision = self.judge("某协议遭黑客攻击")
        message = rules.format_message(item, decision)
        parts = message.split("｜")
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0], "标题 A")
        self.assertEqual(parts[2], "https://example.com/a")
        self.assertEqual(parts[1], rules.format_time(NOW_ISO))


class TestLanguageFilter(unittest.TestCase):
    def test_pure_english_blocked(self):
        self.assertTrue(rules.is_non_chinese(
            "Kalshi says it complies with CFTC order halting Trump mention contracts"))
        self.assertTrue(rules.is_non_chinese(
            "Bitcoin Investors Are Capitulating—That's Historically Been a Bottom Signal"))
        self.assertTrue(rules.is_non_chinese(
            "Ethereum Devs Weigh a Scaling Upgrade That Could Break 15,000 Apps"))

    def test_mixed_and_chinese_kept(self):
        self.assertFalse(rules.is_non_chinese("英伟达 NVDA 跌 2%"))
        self.assertFalse(rules.is_non_chinese("美联储 FOMC 会议纪要"))
        self.assertFalse(rules.is_non_chinese("覆盖全美50州，Coinbase开放BTC、ETH和SOL抵押借USDC服务"))
        self.assertFalse(rules.is_non_chinese("ETH突破1800 USDT，24H跌幅收窄至0.98%"))

    def test_traditional_converted_only_in_message(self):
        original = "MakerDAO 創辦人關聯錢包借走 Sky 存款池近半，存戶領不出錢"
        item = sources.Item("abmedia", original, "https://example.com/t", NOW_ISO)
        decision = rules.judge("协议被黑", published_at=NOW_ISO)
        message = rules.format_message(item, decision)
        self.assertIn("创办人关联钱包借走", message)
        self.assertIn("存户领不出钱", message)
        self.assertNotIn("創辦人", message)
        self.assertEqual(item.title, original, "存档原文不能被转换")

    def test_ratio_math(self):
        self.assertEqual(rules.chinese_ratio("英伟达"), 1.0)
        self.assertEqual(rules.chinese_ratio("Nvidia"), 0.0)
        self.assertLess(rules.chinese_ratio("加密 crypto"), 1.0)


class TestState(unittest.TestCase):
    def test_url_dedup_and_title_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = state_mod.State(os.path.join(tmp, "state.json"))
            item = sources.Item("x", "标题", "https://example.com/a?utm_source=x#top", NOW_ISO)
            decision = rules.judge("某协议遭黑客攻击", published_at=NOW_ISO)
            self.assertFalse(st.known_event(item, decision))
            st.mark_seen(item, decision)
            self.assertTrue(st.known_event(item, decision))
            other = sources.Item("x", "标题", "https://example.com/b", NOW_ISO)
            self.assertTrue(st.known_event(other, decision))

    def test_corrupt_state_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                state_mod.State(str(path)).load()


class TestBalance(unittest.TestCase):
    def test_same_source_capped(self):
        decision = rules.judge("某协议遭黑客攻击", published_at=NOW_ISO)
        candidates = [monitor_run.Candidate(
            sources.Item("odaily", f"同源标题 {i}", f"https://example.com/same/{i}", NOW_ISO),
            decision) for i in range(5)]
        selected, deferred = monitor_run.balance_sources(candidates)
        self.assertEqual(len(selected), 2)
        self.assertEqual(len(deferred), 3)
        mixed = candidates[:2] + [monitor_run.Candidate(
            sources.Item("blockbeats", "异源标题", "https://example.com/other", NOW_ISO),
            decision)]
        selected2, deferred2 = monitor_run.balance_sources(mixed)
        self.assertEqual(len(selected2), 3)
        self.assertEqual(len(deferred2), 0)


class TestCloud(unittest.TestCase):
    def test_window_overlap(self):
        last = NOW - timedelta(hours=2)
        start, text = cloud.search_window(last.isoformat(), now=NOW)
        self.assertEqual(start, NOW - timedelta(hours=2, minutes=30))
        self.assertIn((NOW - timedelta(hours=2, minutes=30)).strftime("%Y-%m-%d %H:%M UTC"), text)

    def test_hits_need_time_and_scope(self):
        hit = cloud.SearchHit("q", "exa_public", "https://example.com/a", "某协议被黑", "细节", "")
        self.assertEqual(cloud.hits_to_items([hit], window_start=NOW - timedelta(hours=1),
                                             window_end=NOW), [])
        fresh = cloud.SearchHit("q", "exa_public", "https://example.com/b", "某协议被黑",
                                "细节", NOW_ISO)
        self.assertEqual(len(cloud.hits_to_items([fresh], window_start=NOW - timedelta(hours=1),
                                                 window_end=NOW)), 1)
        exchange = cloud.SearchHit("q", "exa_public", "https://example.com/c", "Binance 暂停提现",
                                   "细节", NOW_ISO)
        self.assertEqual(cloud.hits_to_items([exchange], window_start=NOW - timedelta(hours=1),
                                             window_end=NOW), [])


class TestDryRun(unittest.TestCase):
    def test_fixture_dry_run_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            code = monitor_run.run(
                fixtures=str(ROOT / "tests/fixtures/sample.json"),
                state_path=os.path.join(tmp, "state.json"),
                report_path=os.path.join(tmp, "report.json"),
                send=False, no_cloud=True,
            )
            self.assertEqual(code, 0)
            payload = json.loads((Path(tmp) / "report.json").read_text(encoding="utf-8"))
            titles = [c["title"] for c in payload["candidates"]]
            self.assertTrue(any("SEC" in title for title in titles))
            self.assertTrue(any("黑客" in title for title in titles))
            self.assertFalse(any("暂停提现" in title for title in titles))
            self.assertFalse(any("巨鲸" in title for title in titles))
            self.assertFalse(any("Hack Wallets" in title for title in titles))
            self.assertFalse(payload["telegram_sent"])
            self.assertGreater(payload["archived"]["medium"], 0)
            self.assertGreaterEqual(sum(payload["routed"].values()), 2)
            self.assertGreaterEqual(payload["filtered_non_chinese"], 2)
            self.assertFalse(any("Kalshi" in title for title in titles))
            self.assertFalse(any("Hack Wallets" in title for title in titles))


def http_result(payload: bytes):
    return sources.http.FetchResult("https://example.com/feed", payload, 200)


if __name__ == "__main__":
    unittest.main()
