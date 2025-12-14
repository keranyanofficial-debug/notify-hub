import os
import json
import time
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml
import feedparser
from dotenv import load_dotenv
import xml.etree.ElementTree as ET

JST = ZoneInfo("Asia/Tokyo")


def now_jst_str() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def yyyymmdd_jst() -> str:
    return datetime.now(JST).strftime("%Y%m%d")


def safe_text(s: Any) -> str:
    if s is None:
        return ""
    return str(s).replace("\r", " ").replace("\n", " ").strip()


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def state_path(target_id: str) -> str:
    ensure_dir(".state")
    return os.path.join(".state", f"{target_id}.json")


def load_state(target_id: str) -> Dict[str, Any]:
    p = state_path(target_id)
    if not os.path.exists(p):
        return {"seen_ids": []}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(target_id: str, state: Dict[str, Any]) -> None:
    p = state_path(target_id)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def http_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 20) -> requests.Response:
    headers = {
        "User-Agent": "watchtower-notifier/1.0 (+https://github.com/)",
        "Accept": "*/*",
    }
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r


# -----------------------
# Fetchers (targets)
# -----------------------

def fetch_rss(url: str, max_items: int) -> List[Dict[str, Any]]:
    # feedparser handles RSS1.0/2.0/Atom
    d = feedparser.parse(url)
    items: List[Dict[str, Any]] = []
    for e in d.entries[:max_items]:
        link = safe_text(getattr(e, "link", ""))
        title = safe_text(getattr(e, "title", ""))
        summary = safe_text(getattr(e, "summary", ""))[:280]
        published = safe_text(getattr(e, "published", "") or getattr(e, "updated", ""))
        entry_id = safe_text(getattr(e, "id", "")) or sha1(link or title)
        items.append({
            "id": entry_id,
            "title": title,
            "url": link,
            "summary": summary,
            "published": published,
            "source": "RSS",
        })
    return items


def fetch_egov_updates(max_items: int) -> List[Dict[str, Any]]:
    # e-Gov Law API v1: https://laws.e-gov.go.jp/api/1/updatelawlists/{yyyyMMdd}
    date_str = yyyymmdd_jst()
    url = f"https://laws.e-gov.go.jp/api/1/updatelawlists/{date_str}"
    xml_text = http_get(url).text

    root = ET.fromstring(xml_text)
    appl = root.find(".//ApplData")
    if appl is None:
        return []

    items: List[Dict[str, Any]] = []
    for info in appl.findall(".//LawNameListInfo"):
        law_name = safe_text(info.findtext("LawName"))
        law_no = safe_text(info.findtext("LawNo"))
        amend = safe_text(info.findtext("AmendName"))
        enforced = safe_text(info.findtext("EnforcementDate"))
        promulg = safe_text(info.findtext("PromulgationDate"))
        # Stable-ish ID
        entry_id = sha1(f"{date_str}|{law_no}|{law_name}|{amend}|{enforced}")

        # Link: best-effort to e-Gov law search site (by name) – user can click and search quickly
        link = "https://laws.e-gov.go.jp/"

        summary = " / ".join([x for x in [law_no, amend, f"施行日:{enforced}", f"公布日:{promulg}"] if x])

        items.append({
            "id": entry_id,
            "title": law_name or "(法令名不明)",
            "url": link,
            "summary": summary[:280],
            "published": date_str,
            "source": "e-Gov法令API",
        })

    return items[:max_items]


def fetch_jgrants(keywords: List[str], acceptance: str, sort: str, order: str, max_items: int) -> List[Dict[str, Any]]:
    # Digital Agency developer site shows public endpoint:
    # https://api.jgrants-portal.go.jp/exp/v1/public/subsidies
    base = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies"

    all_results: Dict[str, Dict[str, Any]] = {}
    for kw in keywords:
        params = {
            "keyword": kw,
            "sort": sort,
            "order": order,
            "acceptance": acceptance,
        }
        r = http_get(base, params=params).json()
        for row in r.get("result", []) or []:
            sid = safe_text(row.get("id"))
            if not sid:
                continue
            all_results[sid] = row

    # Convert to items (sorted by end date if present)
    def end_dt(x: Dict[str, Any]) -> str:
        return safe_text(x.get("acceptance_end_datetime"))

    rows = list(all_results.values())
    rows.sort(key=end_dt)

    items: List[Dict[str, Any]] = []
    for row in rows[:max_items]:
        sid = safe_text(row.get("id"))
        title = safe_text(row.get("title") or row.get("name") or "（補助金）")
        area = safe_text(row.get("target_area_search"))
        max_limit = row.get("subsidy_max_limit", "")
        start = safe_text(row.get("acceptance_start_datetime"))
        end = safe_text(row.get("acceptance_end_datetime"))

        link = f"https://www.jgrants-portal.go.jp/subsidy/{sid}"  # portal page (usually works)
        summary = f"地域:{area} / 上限:{max_limit} / 期間:{start} → {end}"

        items.append({
            "id": sid,
            "title": title,
            "url": link,
            "summary": summary[:280],
            "published": end or start,
            "source": "JグランツAPI",
        })

    return items


# -----------------------
# Importance scoring (rule-based)
# -----------------------

def importance_level(item: Dict[str, Any], target: Dict[str, Any]) -> Tuple[str, str]:
    title = safe_text(item.get("title")).lower()
    summary = safe_text(item.get("summary")).lower()
    text = f"{title} {summary}"

    # Defaults
    level = "C"
    comment = "チェック対象に変化あり。念のため確認推奨。"

    # Security spikes
    if any(k in text for k in ["緊急", "critical", "rce", "remote code", "権限昇格", "ゼロデイ", "0day"]):
        level = "S"
        comment = "危険度高めの匂い。社内IT/委託先に即共有が安全。"
    elif any(k in text for k in ["脆弱性", "xss", "csrf", "sql", "認証", "漏えい", "パストラバーサル"]):
        level = "A"
        comment = "セキュリティ系。影響範囲の棚卸し（該当製品/バージョン）を先にやると早い。"

    # Law updates: boost by keywords
    if target.get("kind") == "egov_law_updates":
        kws = target.get("important_keywords") or []
        if any(str(k).lower() in text for k in kws):
            level = "A" if level != "S" else level
            comment = "法改正/更新の可能性。対象業務（契約書/規程/労務）に影響あるか確認。"

    # Grants: deadline soon / big money
    if target.get("kind") == "jgrants":
        if any(k in text for k in ["it", "dx", "設備", "省力化"]):
            level = "B" if level == "C" else level
        # crude check: if "上限" large
        if "上限:" in safe_text(item.get("summary")) and any(x in safe_text(item.get("summary")) for x in ["10000000", "5000000", "3000000"]):
            level = "A" if level != "S" else level
            comment = "補助金チャンス。締切と要件を先に確認→当てはまるなら最短で動ける。"

    emoji = {"S": "🟥", "A": "🟧", "B": "🟨", "C": "🟦"}[level]
    return level, f"{emoji} {comment}"


# -----------------------
# Notifiers
# -----------------------

def post_slack(webhook_url: str, text: str) -> None:
    payload = {
        "text": text,
        "mrkdwn": True,
    }
    requests.post(webhook_url, json=payload, timeout=20).raise_for_status()


def post_discord(webhook_url: str, title: str, description: str, url: str) -> None:
    payload = {
        "embeds": [{
            "title": title[:256],
            "description": description[:4096],
            "url": url,
        }]
    }
    requests.post(webhook_url, json=payload, timeout=20).raise_for_status()


def notify_all(cfg: Dict[str, Any], headline: str, body: str, url: str) -> None:
    slack_url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    if cfg.get("notifiers", {}).get("slack") and slack_url:
        post_slack(slack_url, f"{headline}\n{body}\n{url}")

    if cfg.get("notifiers", {}).get("discord") and discord_url:
        post_discord(discord_url, headline, f"{body}\n\n{url}", url)


# -----------------------
# Core
# -----------------------

def run_once(cfg: Dict[str, Any]) -> None:
    print(f"[{now_jst_str()}] run_once start")

    for target in cfg.get("targets", []):
        tid = target["id"]
        kind = target["kind"]
        title = target.get("title", tid)
        max_items = int(target.get("max_items", 20))

        try:
            if kind == "rss":
                items = fetch_rss(target["url"], max_items)
            elif kind == "egov_law_updates":
                items = fetch_egov_updates(max_items)
            elif kind == "jgrants":
                items = fetch_jgrants(
                    keywords=target.get("keywords", []),
                    acceptance=str(target.get("acceptance", "1")),
                    sort=str(target.get("sort", "acceptance_end_datetime")),
                    order=str(target.get("order", "ASC")),
                    max_items=max_items,
                )
            else:
                print(f"  - {tid}: unknown kind={kind} (skip)")
                continue

            st = load_state(tid)
            seen = set(st.get("seen_ids", []))

            new_items = [it for it in items if it["id"] not in seen]

            if not new_items:
                print(f"  - {tid}: no changes")
                continue

            # Update state (keep latest 300 ids)
            merged = [it["id"] for it in items] + list(seen)
            dedup = []
            for x in merged:
                if x not in dedup:
                    dedup.append(x)
            st["seen_ids"] = dedup[:300]
            st["last_run"] = now_jst_str()
            save_state(tid, st)

            # Notify (send up to 3 items, summarize rest)
            top = new_items[:3]
            for it in top:
                level, ai_comment = importance_level(it, target)
                headline = f"🚨 更新検知 [{title}]（重要度:{level}）"
                body = f"{ai_comment}\n・タイトル: {safe_text(it.get('title'))}\n・概要: {safe_text(it.get('summary'))}\n・日時: {safe_text(it.get('published'))}\n・ソース: {safe_text(it.get('source'))}"
                notify_all(cfg, headline, body, safe_text(it.get("url")))

            if len(new_items) > 3:
                headline = f"📌 追加更新 [{title}]"
                body = f"他 {len(new_items)-3} 件の新規/更新がありました。必要なら max_items を上げて追跡できます。"
                notify_all(cfg, headline, body, "（リンクは各通知参照）")

            print(f"  - {tid}: notified {len(new_items)} change(s)")

        except Exception as e:
            # Error notification (keeps readable, avoids mojibake)
            msg = safe_text(e)
            headline = f"⚠️ 取得失敗 [{title}]"
            body = f"時刻: {now_jst_str()}\n種別: {kind}\nエラー: {msg}\n対処: URL/パラメータ/ネットワークを確認"
            notify_all(cfg, headline, body, target.get("url", ""))
            print(f"  - {tid}: ERROR {msg}")

    print(f"[{now_jst_str()}] run_once end")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=None)
    args = parser.parse_args()

    load_dotenv()

    cfg = load_yaml(args.config)
    interval = args.interval or int(cfg.get("watch", {}).get("interval_sec", 300))

    if args.watch:
        while True:
            run_once(cfg)
            time.sleep(interval)
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
