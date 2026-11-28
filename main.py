"""币圈新闻监控入口。默认只干跑，不接 Telegram。

python main.py --self-check                 离线自检
python main.py --fixtures tests/fixtures/sample.json --dry-run
python main.py --list-sources               列出全部源与验证日期
python main.py --dry-run                    真实抓源；只写日志与 state，不推送
python main.py --send                       用户说「接推送」后才用
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monitor import cloud, rules, run as monitor_run, sources, state as state_mod  # noqa: E402

BANNED_URL_PARTS = ("sosovalue", "farside", "forexfactory", "federalreserve",
                    "jin10", "eastmoney", "status.", "announcement")


def _self_check() -> int:
    failures: List[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    now = datetime.now(timezone.utc).isoformat()
    check(len(sources.FEEDS) == 21, f"源条目应为 21，实际 {len(sources.FEEDS)}")
    new_sources = ("blocktempo", "blockcast", "zombit", "foresight")
    sample_feed = (b'<rss version="2.0"><channel><item>'
                   b'<title>Test Chinese News</title><link>https://example.com/a</link>'
                   b'<pubDate>Tue, 17 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>')
    for name in new_sources:
        src = sources.SOURCE_BY_NAME.get(name)
        check(src is not None, f"新源缺失: {name}")
        if src is None:
            continue
        items = sources.parse_payload(src, sources.http.FetchResult(src.url, sample_feed, 200))
        check(len(items) == 1, f"新源解析失败: {name}")
    for src in sources.FEEDS:
        check(not any(part in src.url.lower() for part in BANNED_URL_PARTS),
              f"不接的宏观/ETF/交易所源混进来了: {src.name} {src.url}")
        check(bool(src.verified_at), f"源缺少验证日期: {src.name}")
        check(src.headers is None or isinstance(src.headers, dict),
              f"源 headers 配置类型错误: {src.name}")
        if src.kind == "telegram":
            check(bool(src.channel), f"X 预览源缺少频道名: {src.name}")
    check(any(src.name == "eqn_main" and "ch=main" in src.url for src in sources.FEEDS),
          "方程式新闻只接 main 频道")

    decision = rules.judge("某 DeFi 协议遭黑客攻击，损失 1200 万美元", published_at=now)
    check(decision.level == rules.HIGH and decision.rule_name.startswith("K01"),
          "协议被黑未判为高")
    check(rules.judge("US government shutdown 持续", published_at=now).rule_name
          == "not_crypto_related", "裸 shutdown/纯传统金融误判")
    check(rules.judge("Clarity Act fails Senate vote", published_at=now).level != rules.HIGH,
          "裸 fails 把立法新闻误判为高")
    check(rules.is_non_chinese("Kalshi says it complies with CFTC order halting Trump mention contracts"),
          "纯英文标题没有被中文过滤挡下")
    check(not rules.is_non_chinese("英伟达 NVDA 跌 2%", "Nvidia NVDA fell 2 percent after hours"),
          "中英混合标题被误挡")
    check(not rules.is_non_chinese("美联储 FOMC 会议纪要"), "纯中文标题被误挡")
    sec_pattern = re.compile(rules.token("sec"), re.I)
    check(bool(sec_pattern.search("SEC 裁决")) and not sec_pattern.search("second quarter"),
          "SEC 短词 token 边界错误")
    check(rules.judge("Binance 暂停提现", published_at=now).route == "exchange",
          "交易所主体事件未让路")
    check(rules.judge("某巨鲸转移 9938 枚 ZEC 至新地址", published_at=now).route == "whale",
          "链上大额转账未让路")
    funding = rules.judge("某协议完成 1000 万美元融资", published_at=now)
    check(funding.level in (rules.MEDIUM, rules.MEDIUM_HIGH) and not funding.pushable,
          "中等信号未累计就升级")
    research = rules.judge("Trail of Bits 发布审计报告", "回顾某协议被黑损失 1 亿美元",
                           source="trailofbits", published_at=now)
    check(research.level == rules.MEDIUM, "研究类源摘要复盘旧事故误判为高")
    stale = rules.judge("某协议遭黑客攻击，损失 1200 万美元",
                        published_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    check(stale.expired and not stale.pushable, "超过被黑半衰期仍在推")
    rumor = rules.judge("BTC 协议据称遭黑客攻击", published_at=now)
    check(rumor.contamination >= 1 and "污染度" in rumor.reason, "污染度未留痕")
    message = rules.format_message(type("Item", (), {
        "title": "标题", "published_at": now, "url": "https://example.com/a"})(), decision)
    check(message.count("｜") == 2 and "标题" in message, "推送格式不是标题｜时间｜原文链接")
    traditional = type("Item", (), {
        "title": "Hyperliquid 被香港證監會列入「可疑交易平台名單」",
        "published_at": now, "url": "https://example.com/t"})()
    simplified = rules.format_message(traditional, decision)
    check("香港证监会" in simplified and "證監會" not in simplified,
          "繁体标题未在推送消息中转成简体")
    check(cloud.search_window("", now=datetime(2026, 11, 17, 12, tzinfo=timezone.utc))[0]
          == datetime(2026, 11, 14, 11, 30, tzinfo=timezone.utc), "时间窗重叠不是 30 分钟")
    url_a = state_mod.event_id("https://example.com/a?utm_source=x#top", "A")
    url_b = state_mod.event_id("https://example.com/a", "B")
    check(url_a == url_b, "链接归一去重失败")
    same_source = [monitor_run.Candidate(
        sources.Item("odaily", f"同源标题 {i}", f"https://example.com/same/{i}", now),
        decision) for i in range(5)]
    selected, deferred = monitor_run.balance_sources(same_source)
    check(len(selected) == 2 and len(deferred) == 3,
          f"同源上限应为 2，实际入选 {len(selected)}")
    if failures:
        for item in failures:
            print("自检失败：" + item, file=sys.stderr)
        return 5
    print(f"自检通过：{len(sources.FEEDS)} 个源、新源解析/规则分级/让路/污染度/半衰期/格式/时间窗/去重/同源均衡均正常")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="币圈新闻监控")
    parser.add_argument("--dry-run", action="store_true", help="只打印候选消息，不发送（默认）")
    parser.add_argument("--send", action="store_true", help="显式启用 Telegram 推送")
    parser.add_argument("--self-check", action="store_true", help="离线自检，不联网不写状态")
    parser.add_argument("--list-sources", action="store_true", help="列出源、验证日期与地址")
    parser.add_argument("--fixtures", default="", help="本地 JSON 样例，替代真实网络")
    parser.add_argument("--state", default="state/state.json", help="状态文件路径")
    parser.add_argument("--report", default="report.json", help="运行报告路径")
    parser.add_argument("--max-queries", type=int, default=8, help="云端搜索查询上限")
    parser.add_argument("--no-cloud", action="store_true", help="关闭云端搜索（只跑直连源）")
    args = parser.parse_args()

    if args.self_check:
        return _self_check()
    if args.list_sources:
        for src in sources.FEEDS:
            print(f"{src.name}\t{src.kind}\t{src.verified_at}\t{src.label}\t{src.url}")
        return 0
    if args.send and args.dry_run:
        print("--send 与 --dry-run 不能同时用", file=sys.stderr)
        return 5
    return monitor_run.run(fixtures=args.fixtures, state_path=args.state,
                           report_path=args.report, max_queries=args.max_queries,
                           no_cloud=args.no_cloud, send=args.send)


if __name__ == "__main__":
    sys.exit(main())
