import argparse
import base64
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import textwrap
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = ROOT / "site"
REPORTS_DIR = SITE_DIR / "reports"
OPENAI_URL = "https://api.openai.com/v1/responses"
GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def now_in(tz: str) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(tz))


def google_news_rss(query: str, hl: str = "zh-TW", gl: str = "TW", ceid: str = "TW:zh-Hant") -> str:
    params = {
        "q": query,
        "hl": hl,
        "gl": gl,
        "ceid": ceid,
    }
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def parse_rss_datetime(value: str):
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


def fetch_rss(url: str, limit: int = 8):
    try:
        response = requests.get(url, timeout=20, headers={"User-Agent": "market-report-bot/1.0"})
        response.raise_for_status()
    except Exception as exc:
        return [{"error": str(exc), "url": url}]

    try:
        root = ET.fromstring(response.content)
    except Exception as exc:
        return [{"error": f"RSS parse failed: {exc}", "url": url}]

    items = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source_el = item.find("{*}source")
        source = source_el.text.strip() if source_el is not None and source_el.text else ""
        pub_date = item.findtext("pubDate") or ""
        parsed = parse_rss_datetime(pub_date)
        items.append(
            {
                "title": title,
                "source": source,
                "published_utc": parsed.isoformat() if parsed else pub_date,
                "url": link,
            }
        )
    return items


def fetch_yahoo_chart(symbols):
    out = {}
    for symbol in symbols:
        encoded = urllib.parse.quote(symbol, safe="")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d"
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "market-report-bot/1.0"})
            r.raise_for_status()
            data = r.json()["chart"]["result"][0]
            meta = data.get("meta", {})
            quote = data.get("indicators", {}).get("quote", [{}])[0]
            closes = [v for v in quote.get("close", []) if v is not None]
            out[symbol] = {
                "regularMarketPrice": meta.get("regularMarketPrice"),
                "previousClose": meta.get("chartPreviousClose"),
                "currency": meta.get("currency"),
                "last_close": closes[-1] if closes else None,
            }
        except Exception as exc:
            out[symbol] = {"error": str(exc)}
    return out


def source_plan(mode: str):
    today_tw = now_in("Asia/Taipei").strftime("%Y/%m/%d")
    today_us = now_in("America/New_York").strftime("%B %d %Y")

    if mode == "tw":
        queries = [
            f"{today_tw} 台股 台積電 外資 投信 三大法人",
            f"{today_tw} 台股 AI ASIC 半導體 記憶體 矽光子 面板",
            f"{today_tw} 上市櫃 重大訊息 法說會 營收 財報",
            f"{today_tw} 金管會 央行 新台幣 匯率 台股",
            f"{today_tw} 美股 費半 TSM ADR NVIDIA AMD Micron 台股 影響",
            "site:mops.twse.com.tw 重大訊息 台積電 聯發科 法說會",
            "site:twse.com.tw 三大法人 買賣超 台股",
            "site:fsc.gov.tw 金管會 台股 外資 金融",
        ]
        symbols = ["^TWII", "2330.TW", "2454.TW", "2308.TW", "TSM", "^SOX", "NVDA", "AMD", "MU"]
    elif mode == "us":
        queries = [
            f"{today_us} US stock futures CPI Treasury yields oil Nasdaq S&P 500",
            f"{today_us} Nvidia AMD Micron Broadcom TSM premarket semiconductor",
            f"{today_us} Fed speakers CPI retail sales dollar oil stock market",
            f"{today_us} Reuters AP MarketWatch stock futures tech AI semiconductor",
            f"{today_us} TSM ADR Taiwan stocks semiconductor supply chain",
            "site:bls.gov CPI latest release United States",
            "site:federalreserve.gov Fed speech interest rates latest",
            "site:treasury.gov Treasury yields latest",
        ]
        symbols = ["^GSPC", "^IXIC", "^DJI", "^SOX", "NVDA", "AMD", "AVGO", "MU", "TSM", "CL=F", "^TNX", "DX-Y.NYB"]
    else:
        queries = [
            "next week Taiwan stocks earnings investor conference economic data",
            "下週 台股 法說會 財報 營收 除權息 經濟數據",
            "next week US economic calendar CPI PCE retail sales Fed earnings",
            "next week Nvidia AMD Broadcom Micron TSM semiconductor earnings events",
            "next week oil dollar treasury yields geopolitical risk stocks",
            "site:twse.com.tw 法說會 下週 上市公司",
            "site:mops.twse.com.tw 法說會 財報 重大訊息 下週",
            "site:investing.com economic calendar next week United States",
        ]
        symbols = ["^TWII", "2330.TW", "^GSPC", "^IXIC", "^SOX", "TSM", "NVDA", "AMD", "MU", "CL=F", "^TNX"]
    return queries, symbols


def collect_sources(mode: str):
    queries, symbols = source_plan(mode)
    feeds = []
    for query in queries:
        feeds.append({"query": query, "items": fetch_rss(google_news_rss(query), limit=8)})
    market = fetch_yahoo_chart(symbols)
    return {"feeds": feeds, "market": market}


def build_prompt(mode: str, sources: dict):
    tw_now = now_in("Asia/Taipei")
    ny_now = now_in("America/New_York")
    common = """
你是嚴謹的繁體中文金融市場編輯。請根據提供的多源候選資料，整理成可發布到手機網頁的市場報告。

硬性要求：
- 只保留可能影響投資判斷、市場風險、重要產業、權值股或資金面的內容。
- 排除低重要性、重複、未經證實、純市場閒聊。
- 每則必須附來源 URL。若同一事件有多來源，選最權威或最清楚的來源。
- 不要給買賣指令，不要構成投資建議。
- 用繁體中文。
- 請輸出單一 JSON，不要 Markdown code fence。
- JSON 結構：
  {
    "meta": {
      "slug": "...",
      "title": "...",
      "date_label": "...",
      "summary": "...",
      "temperature": "...",
      "risks": "...",
      "themes": "...",
      "button_label": "查看完整報告"
    },
    "report": "完整報告純文字"
  }

報告純文字格式：
【標題】
日期

今日一句話 或 下週一句話
...

市場溫度
...
主要風險：...
主線題材：...

────────────────
🔴 高度重要
每則包含：標題、偏向（↑利多/↓利空/↔中性）、重點、台股影響/美股影響、相關公司或產業、來源。

────────────────
🟡 中度重要
...

────────────────
🔵 觀察中
...

最該盯
1. ...
2. ...
3. ...

提醒：以上為資訊整理，不是投資建議，也不含買賣指令。
"""
    if mode == "tw":
        specific = f"""
任務：台灣金融開盤前快報。
目前時間：台北 {tw_now.isoformat()}；紐約 {ny_now.isoformat()}。
slug: tw-{tw_now.strftime('%Y-%m-%d')}
title: 台灣金融開盤前快報
date_label: {tw_now.strftime('%Y-%m-%d')}｜台北時間 08:00

內容至少涵蓋：
- 台股大盤與權值股，尤其台積電、聯發科、台達電等。
- AI/ASIC/半導體/記憶體/矽光子/伺服器等主線。
- 三大法人、外資/投信、自營商、匯率、台指期。
- MOPS 重大訊息、營收、財報、法說會。
- 美股前一日收盤、費半、TSM ADR、NVIDIA/AMD/Micron/Broadcom、油價、美債、美元、Fed/CPI 的台股連動。
高度重要至少 3 則，中度重要至少 3 則，觀察中至少 2 則。若不足，請明確說明不足。
"""
    elif mode == "us":
        specific = f"""
任務：美股開盤前快報。
目前時間：台北 {tw_now.isoformat()}；紐約 {ny_now.isoformat()}。
slug: us-{ny_now.strftime('%Y-%m-%d')}
title: 美股開盤前快報
date_label: {ny_now.strftime('%Y-%m-%d')}｜美東時間 08:30

內容至少涵蓋：
- Dow、S&P 500、Nasdaq、Russell 2000、費半與主要期貨。
- NVIDIA、AMD、Broadcom、Marvell、Intel、Micron、TSM ADR、Applied Materials、Cisco、雲端資本支出與資料中心。
- Fed、CPI/PCE、就業、零售銷售、美債殖利率、美元、油價。
- 關稅、地緣政治、國際事件。
- 對台股隔日與半導體供應鏈的可能影響。
高度重要至少 3 則，中度重要至少 3 則，觀察中至少 2 則。若不足，請明確說明不足。
"""
    else:
        next_week = (tw_now + dt.timedelta(days=7)).strftime("%Y-%m-%d")
        specific = f"""
任務：台股＋美股下週重要事件展望。
目前時間：台北 {tw_now.isoformat()}；紐約 {ny_now.isoformat()}。
slug: weekly-{tw_now.strftime('%Y-%m-%d')}
title: 台股＋美股下週展望
date_label: {tw_now.strftime('%Y-%m-%d')}｜展望至 {next_week}

內容依日期排序，涵蓋：
- 下週台灣公司法說、營收、財報、除權息、股東會、重大政策、央行/金管會。
- 下週美國經濟數據、Fed、重要財報、科技/半導體事件。
- 油價、美元、美債、關稅、地緣政治。
- 對台股與美股的可能影響。
高度重要至少 3 則，中度重要至少 4 則，觀察中至少 3 則。若不足，請明確說明不足。
"""
    source_text = json.dumps(sources, ensure_ascii=False, indent=2)
    return common + "\n" + specific + "\n候選資料如下：\n" + source_text


def item_time(item):
    value = item.get("published_utc") or ""
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def flatten_items(sources: dict):
    items = []
    seen = set()
    for feed in sources.get("feeds", []):
        query = feed.get("query", "")
        for item in feed.get("items", []):
            if item.get("error"):
                continue
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            if not title or not url:
                continue
            key = re.sub(r"\s+", " ", title.lower())
            if key in seen:
                continue
            seen.add(key)
            item = dict(item)
            item["query"] = query
            items.append(item)
    items.sort(key=item_time, reverse=True)
    return items


IMPORTANT_KEYWORDS = [
    "台積電", "tsmc", "tsm", "輝達", "nvidia", "nvda", "amd", "micron", "美光",
    "broadcom", "博通", "費半", "半導體", "ai", "asic", "伺服器", "資料中心",
    "cpi", "pce", "fed", "fomc", "利率", "殖利率", "美債", "美元", "油價",
    "外資", "投信", "三大法人", "新台幣", "營收", "財報", "法說", "重大訊息",
    "關稅", "川普", "伊朗", "地緣", "央行", "金管會",
]

MEDIUM_KEYWORDS = [
    "記憶體", "矽光子", "面板", "航運", "金融", "塑化", "電源", "散熱", "pcb",
    "雲端", "零售銷售", "就業", "失業", "pmi", "美元", "期貨", "adr", "除權息",
]


def score_item(item):
    text = ((item.get("title") or "") + " " + (item.get("query") or "") + " " + (item.get("source") or "")).lower()
    score = 0
    for keyword in IMPORTANT_KEYWORDS:
        if keyword.lower() in text:
            score += 3
    for keyword in MEDIUM_KEYWORDS:
        if keyword.lower() in text:
            score += 1
    source = (item.get("source") or "").lower()
    for trusted in ["reuters", "ap", "中央社", "經濟日報", "鉅亨", "工商", "marketwatch", "yahoo", "investing"]:
        if trusted.lower() in source:
            score += 1
    age = item_time(item)
    if age > dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=36):
        score += 2
    return score


def direction_for(title):
    lower = title.lower()
    negative = ["下跌", "賣超", "利空", "衰退", "風險", "戰", "油價", "通膨", "殖利率", "關稅", "跌", "弱"]
    positive = ["上漲", "買超", "利多", "創高", "成長", "增", "ai", "訂單", "營收", "漲", "強"]
    if any(word in lower for word in negative):
        return "↓ 偏向：可能利空/風險"
    if any(word in lower for word in positive):
        return "↑ 偏向：可能利多"
    return "↔ 偏向：中性/待確認"


def section_items(items, min_high=3, min_medium=3, min_watch=2):
    ranked = sorted(items, key=lambda item: (score_item(item), item_time(item)), reverse=True)
    high = ranked[: max(min_high, min(5, len(ranked)))]
    remaining = [x for x in ranked if x not in high]
    medium = remaining[: max(min_medium, min(5, len(remaining)))]
    remaining = [x for x in remaining if x not in medium]
    watch = remaining[: max(min_watch, min(4, len(remaining)))]
    return high, medium, watch


def market_snapshot(mode, market):
    lines = []
    preferred = {
        "tw": ["^TWII", "2330.TW", "2454.TW", "TSM", "^SOX", "NVDA", "AMD", "MU"],
        "us": ["^GSPC", "^IXIC", "^DJI", "^SOX", "NVDA", "AMD", "AVGO", "MU", "TSM", "CL=F", "^TNX"],
        "weekly": ["^TWII", "^GSPC", "^IXIC", "^SOX", "TSM", "NVDA", "CL=F", "^TNX"],
    }.get(mode, [])
    for symbol in preferred:
        data = market.get(symbol)
        if not data or data.get("error"):
            continue
        price = data.get("regularMarketPrice") or data.get("last_close")
        prev = data.get("previousClose")
        if price is None:
            continue
        change = ""
        if prev:
            try:
                pct = (float(price) - float(prev)) / float(prev) * 100
                change = f" ({pct:+.2f}%)"
            except Exception:
                pass
        lines.append(f"{symbol}: {price}{change}")
    return "；".join(lines[:10]) if lines else "行情資料暫缺"


def make_aggregate_report(mode: str, sources: dict):
    tw_now = now_in("Asia/Taipei")
    ny_now = now_in("America/New_York")
    items = flatten_items(sources)
    high, medium, watch = section_items(
        items,
        min_high=3 if mode != "weekly" else 3,
        min_medium=3 if mode != "weekly" else 4,
        min_watch=2 if mode != "weekly" else 3,
    )
    snapshot = market_snapshot(mode, sources.get("market", {}))

    if mode == "tw":
        title = "台灣金融開盤前快報"
        slug = f"tw-{tw_now.strftime('%Y-%m-%d')}"
        date_label = f"{tw_now.strftime('%Y-%m-%d')}｜台北時間 08:00"
        one_liner = "免費新聞聚合版：依多源新聞、官方/市場資料與行情快照整理，供你快速掌握台股開盤前重點。"
        temp = "台股：依新聞熱度與行情快照判讀，請點完整報告查看來源"
        risks = "風險：台積電/權值股、外資籌碼、美股半導體、匯率、油價與利率"
        themes = "主線：AI、半導體、ASIC、記憶體、法說/營收、政策與法人籌碼"
        impact_a = "台股影響"
        impact_b = "國際連動"
    elif mode == "us":
        title = "美股開盤前快報"
        slug = f"us-{ny_now.strftime('%Y-%m-%d')}"
        date_label = f"{ny_now.strftime('%Y-%m-%d')}｜美東時間 08:30"
        one_liner = "免費新聞聚合版：依多源新聞、行情快照與總經事件整理，供你快速掌握美股開盤前重點。"
        temp = "美股：依新聞熱度與行情快照判讀，請點完整報告查看來源"
        risks = "風險：CPI/Fed、美債殖利率、美元、油價、科技股評價與地緣政治"
        themes = "主線：AI、半導體、Mag 7、雲端資本支出、費半與 TSM ADR"
        impact_a = "美股影響"
        impact_b = "台股影響"
    else:
        title = "台股＋美股下週展望"
        slug = f"weekly-{tw_now.strftime('%Y-%m-%d')}"
        date_label = f"{tw_now.strftime('%Y-%m-%d')}｜週日展望"
        one_liner = "免費新聞聚合版：彙整下週台股與美股重要事件、總經數據、財報與產業焦點。"
        temp = "下週：依事件密度與新聞熱度判讀，請點完整報告查看來源"
        risks = "風險：總經數據、Fed/利率、油價、美元、美債、台股法說與外資籌碼"
        themes = "主線：AI、半導體、重要財報、政策事件、經濟數據"
        impact_a = "台股影響"
        impact_b = "美股影響"

    def render_item(item):
        title_text = item.get("title", "").replace(" - ", "｜")
        source = item.get("source") or "Google News"
        published = item.get("published_utc") or ""
        url = item.get("url") or ""
        direction = direction_for(title_text)
        return textwrap.dedent(
            f"""\
            {title_text}
            {direction}
            重點：{title_text}
            {impact_a}：請優先確認是否涉及權值股、半導體、AI、利率/匯率、政策或法人籌碼。
            {impact_b}：若涉及美股科技股、油價、美元、美債或地緣政治，需觀察隔日連動。
            來源：{source}｜{published}
            連結：{url}
            """
        ).strip()

    def render_section(name, rows):
        if not rows:
            return f"{name}\n目前未篩出足夠高重要性新聞。"
        return name + "\n\n" + "\n\n".join(render_item(row) for row in rows)

    report = "\n\n".join(
        [
            f"【{title}】",
            date_label,
            "今日一句話" if mode != "weekly" else "下週一句話",
            one_liner,
            "市場溫度",
            temp,
            f"行情快照：{snapshot}",
            f"主要風險：{risks}",
            f"主線題材：{themes}",
            "────────────────",
            render_section("🔴 高度重要", high),
            "────────────────",
            render_section("🟡 中度重要", medium),
            "────────────────",
            render_section("🔵 觀察中", watch),
            "────────────────",
            "最該盯" if mode != "weekly" else "下週最該盯",
            "1. 是否有官方公告、財報/法說或總經數據改變市場預期\n2. 半導體/AI 主線是否獲得美股與台股同步支撐\n3. 美債殖利率、美元、油價與外資資金是否轉向",
            "提醒：以上為自動新聞聚合與來源整理，不是投資建議，也不含買賣指令。",
        ]
    )
    meta = {
        "slug": slug,
        "title": title,
        "date_label": date_label,
        "summary": one_liner,
        "temperature": temp,
        "risks": f"風險：{risks}",
        "themes": f"主線：{themes}",
        "button_label": "查看完整報告",
    }
    return {"meta": meta, "report": report}


def call_openai(prompt: str):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing")
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
    payload = {
        "model": model,
        "input": prompt,
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    r = requests.post(OPENAI_URL, headers=headers, json=payload, timeout=180)
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI API failed {r.status_code}: {r.text[:1000]}")
    data = r.json()
    text = data.get("output_text")
    if not text:
        parts = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in ("output_text", "text"):
                    parts.append(content.get("text", ""))
        text = "\n".join(parts).strip()
    if not text:
        raise RuntimeError("OpenAI response had no text")
    return text


def call_gemini(prompt: str):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = GEMINI_URL_TEMPLATE.format(model=model)
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    last_error = None
    for attempt in range(4):
        r = requests.post(
            url,
            params={"key": api_key},
            json=payload,
            timeout=180,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code < 400:
            break
        last_error = f"Gemini API failed {r.status_code}: {r.text[:1000]}"
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            sleep_seconds = [20, 45, 90][attempt]
            print(f"{last_error}\nRetrying Gemini in {sleep_seconds}s...")
            time.sleep(sleep_seconds)
            continue
        raise RuntimeError(last_error)
    data = r.json()
    parts = []
    for candidate in data.get("candidates", []):
        content = candidate.get("content", {})
        for part in content.get("parts", []):
            if "text" in part:
                parts.append(part["text"])
    text = "\n".join(parts).strip()
    if not text:
        raise RuntimeError("Gemini response had no text")
    return text


def extract_json(text: str):
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def html_report(report: str, meta: dict):
    def esc(s):
        return html.escape(str(s), quote=True)

    blocks = []
    in_list = False
    for raw in report.splitlines():
        line = raw.strip()
        if not line:
            if in_list:
                blocks.append("</ul>")
                in_list = False
            continue
        if re.match(r"^\d+\.\s+", line):
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            list_text = re.sub(r"^\d+\.\s+", "", line)
            blocks.append(f"<li>{esc(list_text)}</li>")
            continue
        if in_list:
            blocks.append("</ul>")
            in_list = False
        if line.startswith("【") and line.endswith("】"):
            blocks.append(f"<h2>{esc(line)}</h2>")
        elif line.startswith(("🔴", "🟡", "🔵")):
            blocks.append(f"<h3>{esc(line)}</h3>")
        elif set(line) == {"─"}:
            blocks.append("<hr>")
        elif "http://" in line or "https://" in line:
            urls = re.findall(r"https?://\S+", line)
            if urls:
                safe = esc(urls[0])
                label = esc(line.replace(urls[0], "").strip() or "來源")
                blocks.append(f'<p class="source">{label} <a href="{safe}">{safe}</a></p>')
            else:
                blocks.append(f"<p>{esc(line)}</p>")
        else:
            blocks.append(f"<p>{esc(line)}</p>")
    if in_list:
        blocks.append("</ul>")

    title = esc(meta.get("title", "Market Report"))
    date_label = esc(meta.get("date_label", ""))
    summary = esc(meta.get("summary", ""))
    body = "\n".join(blocks)
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light; --bg:#f6f7fb; --ink:#111827; --muted:#5b6472; --line:#dfe3ea; --card:#fff; --accent:#111827; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",Arial,sans-serif; background:var(--bg); color:var(--ink); line-height:1.68; }}
    header {{ background:var(--accent); color:white; padding:28px 18px 24px; }}
    .hero, main {{ max-width:780px; margin:0 auto; }}
    main {{ padding:18px; }}
    h1 {{ margin:0; font-size:clamp(1.8rem,5vw,2.7rem); line-height:1.15; letter-spacing:0; }}
    .date {{ margin-top:8px; color:#d1d5db; font-size:1.05rem; }}
    .summary {{ margin-top:18px; padding:14px 16px; background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.16); border-radius:8px; font-size:1.08rem; }}
    article {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:18px; box-shadow:0 6px 18px rgba(15,23,42,.06); }}
    h2 {{ margin:26px 0 10px; font-size:1.36rem; line-height:1.25; border-top:1px solid var(--line); padding-top:18px; }}
    h2:first-child {{ margin-top:0; border-top:0; padding-top:0; }}
    h3 {{ margin:20px 0 8px; font-size:1.15rem; line-height:1.35; }}
    p, li {{ font-size:1.03rem; }}
    p {{ margin:8px 0; }}
    ul {{ margin:8px 0 14px; padding-left:1.3rem; }}
    li {{ margin:6px 0; }}
    hr {{ border:0; border-top:1px solid var(--line); margin:20px 0; }}
    a {{ color:#2563eb; overflow-wrap:anywhere; }}
    .source {{ color:var(--muted); font-size:.95rem; }}
    footer {{ color:var(--muted); font-size:.9rem; text-align:center; padding:18px 0 8px; }}
  </style>
</head>
<body>
  <header><div class="hero"><h1>{title}</h1><div class="date">{date_label}</div><div class="summary">{summary}</div></div></header>
  <main><article>{body}</article><footer>資訊整理，不是投資建議，也不含買賣指令。</footer></main>
</body>
</html>
"""


def send_line_card(meta: dict, report_url: str):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN is missing")
    token = "".join(token.split())
    card = {
        "type": "flex",
        "altText": meta.get("title", "市場快報"),
        "contents": {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "18px",
                "backgroundColor": "#111827",
                "contents": [
                    {"type": "text", "text": meta.get("title", "市場快報"), "color": "#FFFFFF", "weight": "bold", "size": "xl", "wrap": True},
                    {"type": "text", "text": meta.get("date_label", ""), "color": "#D1D5DB", "size": "md", "margin": "sm", "wrap": True},
                ],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "16px",
                "spacing": "md",
                "contents": [
                    {"type": "text", "text": meta.get("summary", ""), "size": "md", "color": "#374151", "wrap": True},
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": meta.get("temperature", ""), "size": "md", "color": "#111827", "wrap": True},
                    {"type": "text", "text": meta.get("risks", ""), "size": "sm", "color": "#4B5563", "wrap": True},
                    {"type": "text", "text": meta.get("themes", ""), "size": "sm", "color": "#4B5563", "wrap": True},
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "12px",
                "backgroundColor": "#F8FAFC",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "color": "#111827",
                        "action": {"type": "uri", "label": meta.get("button_label", "查看完整報告"), "uri": report_url},
                    }
                ],
            },
        },
    }
    r = requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={"messages": [card]},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"LINE failed {r.status_code}: {r.text}")


def should_run(mode: str):
    if os.environ.get("FORCE_RUN") == "1":
        return True
    if mode != "us":
        return True
    ny = now_in("America/New_York")
    return ny.weekday() < 5 and ny.hour == 8 and 20 <= ny.minute <= 40


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["tw", "us", "weekly"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-line", action="store_true")
    args = parser.parse_args()

    if not should_run(args.mode):
        print(f"Skip mode={args.mode}; not scheduled local market time.")
        return

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    sources = collect_sources(args.mode)
    if os.environ.get("USE_GEMINI", "0") == "1":
        prompt = build_prompt(args.mode, sources)
        try:
            result_text = call_gemini(prompt)
            result = extract_json(result_text)
        except Exception as exc:
            if os.environ.get("FALLBACK_AGGREGATE", "1") == "1":
                print(f"Gemini failed, falling back to aggregate report: {exc}")
                result = make_aggregate_report(args.mode, sources)
                result["meta"]["summary"] = "Gemini 暫時忙碌，本次先改用免費新聞聚合版；請點完整報告查看來源清單。"
            else:
                raise
    elif os.environ.get("FREE_AGGREGATE", "1") == "1" and not os.environ.get("OPENAI_API_KEY"):
        result = make_aggregate_report(args.mode, sources)
    elif os.environ.get("FREE_AGGREGATE", "1") == "1":
        result = make_aggregate_report(args.mode, sources)
    else:
        prompt = build_prompt(args.mode, sources)
        result_text = call_openai(prompt)
        result = extract_json(result_text)
    meta = result["meta"]
    report = result["report"]

    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", meta.get("slug") or f"{args.mode}-{dt.datetime.utcnow().strftime('%Y-%m-%d-%H%M')}")
    meta["slug"] = slug
    report_path = REPORTS_DIR / f"{slug}.html"
    report_path.write_text(html_report(report, meta), encoding="utf-8")
    (REPORTS_DIR / f"{slug}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    index = SITE_DIR / "index.html"
    index.write_text(
        f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Market Reports</title><style>body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",Arial,sans-serif;background:#f6f7fb;color:#111827;margin:0}}main{{max-width:760px;margin:0 auto;padding:24px 18px}}a{{display:block;padding:14px 16px;margin:12px 0;background:white;border:1px solid #dfe3ea;border-radius:8px;color:#1d4ed8;text-decoration:none}}</style></head><body><main><h1>Market Reports</h1><a href="reports/{slug}.html">{html.escape(meta.get("title",""))}<br><small>{html.escape(meta.get("date_label",""))}</small></a></main></body></html>""",
        encoding="utf-8",
    )

    base_url = os.environ.get("MARKET_REPORT_BASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("MARKET_REPORT_BASE_URL is missing")
    report_url = f"{base_url}/reports/{slug}.html"

    (ROOT / "last-run.json").write_text(
        json.dumps({"mode": args.mode, "slug": slug, "url": report_url, "generated_at_utc": dt.datetime.utcnow().isoformat()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not args.dry_run and not args.no_line:
        send_line_card(meta, report_url)
    print(report_url)


if __name__ == "__main__":
    main()
