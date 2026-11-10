#!/usr/bin/env python3
"""Product East News Collector — improved Stage 1 (v4 metadata support).

Collects RSS metadata, applies word-boundary keyword matching, uses a rolling
lookback window, deduplicates normalized URLs, stores feed health, and exports
JSON/CSV/Markdown. This stage gathers a broad candidate pool; it does NOT yet
cluster multiple reports of the same event or publish to WordPress.
"""
from __future__ import annotations

import csv
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from zoneinfo import ZoneInfo

import feedparser
import requests
from dateutil import parser as date_parser

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "collector.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOG_PATH, encoding="utf-8")]
)
log = logging.getLogger("producteast.collector")


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        ignored = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                   "utm_content", "fbclid", "gclid", "ocid", "ref"}
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() not in ignored]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                           parts.path.rstrip("/"), urlencode(query), ""))
    except Exception:
        return url.strip()


def entry_datetime(entry: dict):
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            try:
                parsed = date_parser.parse(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except (ValueError, TypeError, OverflowError):
                pass
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(time.mktime(value), tz=timezone.utc)
            except (ValueError, TypeError, OverflowError):
                pass
    return None


def keyword_score(title: str, summary: str, keywords: list[str]) -> tuple[int, list[str]]:
    """Match whole words/phrases; prevents 'ai' matching inside 'Malaysia'."""
    text = f" {title} {summary} ".casefold()
    matched = []
    for keyword in keywords:
        kw = keyword.strip().casefold()
        if not kw:
            continue
        # Treat spaces in a phrase flexibly, but require word boundaries around it.
        pattern = r"(?<!\w)" + re.escape(kw).replace(r"\ ", r"\s+") + r"(?!\w)"
        if re.search(pattern, text, flags=re.IGNORECASE):
            matched.append(keyword.strip())
    matched = sorted(set(matched), key=str.casefold)
    return len(matched), matched


def init_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                published_at_utc TEXT,
                published_at_local TEXT,
                summary TEXT,
                publisher_name TEXT NOT NULL DEFAULT '',
                publisher_url TEXT NOT NULL DEFAULT '',
                score INTEGER NOT NULL DEFAULT 0,
                matched_keywords TEXT NOT NULL DEFAULT '[]',
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feed_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at_local TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                status TEXT NOT NULL,
                entry_count INTEGER NOT NULL DEFAULT 0,
                error TEXT
            )
        """)
        # Safe migration for databases created by older versions.
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(stories)").fetchall()}
        if "publisher_name" not in existing_columns:
            conn.execute("ALTER TABLE stories ADD COLUMN publisher_name TEXT NOT NULL DEFAULT ''")
        if "publisher_url" not in existing_columns:
            conn.execute("ALTER TABLE stories ADD COLUMN publisher_url TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stories_local ON stories(published_at_local)")
        conn.commit()


def fetch_feed(feed: dict, config: dict) -> tuple[list[dict], str | None]:
    headers = {"User-Agent": config.get(
        "user_agent", "ProductEastNewsCollector/0.2 (RSS reader)"
    )}
    timeout = int(config.get("request_timeout_seconds", 20))
    try:
        response = requests.get(feed["url"], headers=headers, timeout=timeout)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"RSS parse error: {parsed.bozo_exception}")
        entries = parsed.entries[:int(config.get("max_items_per_feed", 80))]
        return entries, None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def collect():
    config = load_config()
    tz = ZoneInfo(config.get("timezone", "Europe/Bucharest"))
    now_local = datetime.now(tz)
    cutoff_local = now_local - timedelta(hours=int(config.get("lookback_hours", 36)))
    keywords = config.get("topic_keywords", [])
    db_path = BASE_DIR / config.get("database_path", "data/news.sqlite3")
    output_dir = BASE_DIR / config.get("output_directory", "output")
    output_dir.mkdir(parents=True, exist_ok=True)
    init_db(db_path)

    feeds = [f for f in config.get("feeds", []) if f.get("enabled", True)]
    seen_this_run = set()
    fetched_count = 0
    inserted_count = 0
    feed_health = []

    with sqlite3.connect(db_path) as conn:
        for feed in feeds:
            entries, error = fetch_feed(feed, config)
            fetched_count += len(entries)
            status = "error" if error else ("ok" if entries else "empty")
            feed_health.append({
                "source_name": feed["name"], "source_url": feed["url"],
                "status": status, "entry_count": len(entries), "error": error
            })
            log.info("Feed %-26s status=%s entries=%d%s",
                     feed["name"], status, len(entries), f" error={error}" if error else "")
            conn.execute("""
                INSERT INTO feed_runs(run_at_local, source_name, source_url, status, entry_count, error)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (now_local.isoformat(), feed["name"], feed["url"], status, len(entries), error))
            if error:
                continue

            for entry in entries:
                title = clean_text(entry.get("title"))
                url = canonicalize_url(entry.get("link", ""))
                if not title or not url or url in seen_this_run:
                    continue
                seen_this_run.add(url)

                published_utc = entry_datetime(entry)
                if published_utc is None:
                    continue
                published_local = published_utc.astimezone(tz)
                if not (cutoff_local <= published_local <= now_local):
                    continue

                summary = clean_text(entry.get("summary") or entry.get("description") or "")
                source_meta = entry.get("source") or {}
                publisher_name = clean_text(source_meta.get("title", "")) if isinstance(source_meta, dict) else ""
                publisher_url = canonicalize_url(source_meta.get("href", "")) if isinstance(source_meta, dict) else ""
                score, matched = keyword_score(title, summary, keywords)
                if score == 0:
                    continue

                now_utc = datetime.now(timezone.utc).isoformat()
                values = (
                    url, title, feed["name"], feed["url"], published_utc.isoformat(),
                    published_local.isoformat(), summary[:3000], publisher_name, publisher_url, score,
                    json.dumps(matched, ensure_ascii=False), now_utc, now_utc
                )
                cursor = conn.execute("""
                    INSERT INTO stories
                    (url, title, source_name, source_url, published_at_utc,
                     published_at_local, summary, publisher_name, publisher_url, score, matched_keywords,
                     first_seen_utc, last_seen_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        last_seen_utc=excluded.last_seen_utc,
                        title=CASE WHEN excluded.title <> '' THEN excluded.title ELSE stories.title END,
                        summary=CASE WHEN excluded.summary <> '' THEN excluded.summary ELSE stories.summary END,
                        publisher_name=CASE WHEN excluded.publisher_name <> '' THEN excluded.publisher_name ELSE stories.publisher_name END,
                        publisher_url=CASE WHEN excluded.publisher_url <> '' THEN excluded.publisher_url ELSE stories.publisher_url END,
                        published_at_utc=COALESCE(excluded.published_at_utc, stories.published_at_utc),
                        published_at_local=COALESCE(excluded.published_at_local, stories.published_at_local),
                        score=MAX(stories.score, excluded.score),
                        matched_keywords=CASE WHEN excluded.score > stories.score
                            THEN excluded.matched_keywords ELSE stories.matched_keywords END
                """, values)
                if cursor.rowcount:
                    inserted_count += 1
        conn.commit()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT url, title, source_name, source_url, published_at_utc,
                   published_at_local, summary, publisher_name, publisher_url, score, matched_keywords
            FROM stories
            WHERE published_at_local >= ? AND published_at_local <= ?
            ORDER BY score DESC, published_at_local DESC
        """, (cutoff_local.isoformat(), now_local.isoformat())).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item["matched_keywords"] = json.loads(item["matched_keywords"])
        items.append(item)

    stamp = now_local.strftime("%Y-%m-%d_%H-%M")
    json_path = output_dir / f"news_{stamp}.json"
    csv_path = output_dir / f"news_{stamp}.csv"
    report_path = output_dir / f"report_{stamp}.md"

    json_path.write_text(json.dumps({
        "generated_at": now_local.isoformat(),
        "timezone": str(tz),
        "lookback_hours": int(config.get("lookback_hours", 36)),
        "cutoff": cutoff_local.isoformat(),
        "candidate_count": len(items),
        "stories": items,
        "feed_health": feed_health
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["title", "source_name", "published_at_local", "score",
                  "matched_keywords", "url", "summary"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in items:
            row = item.copy()
            row["matched_keywords"] = ", ".join(row["matched_keywords"])
            writer.writerow({k: row.get(k, "") for k in fields})

    report = [
        f"# Product East — News Collector report ({now_local.strftime('%Y-%m-%d %H:%M %Z')})",
        "",
        f"- Lookback window: {config.get('lookback_hours', 36)} hours",
        f"- RSS entries fetched: {fetched_count}",
        f"- Candidate stories after date/topic filters: {len(items)}",
        f"- Feeds: {len(feed_health)} total; {sum(1 for f in feed_health if f['status'] == 'ok')} successful; "
        f"{sum(1 for f in feed_health if f['status'] == 'error')} errors; "
        f"{sum(1 for f in feed_health if f['status'] == 'empty')} empty",
        "- This is a candidate pool, not the final 3–5 event selection.",
        "- Related stories are not clustered yet; that is Stage 2.",
        "",
        "## Feed health",
        ""
    ]
    for f in feed_health:
        suffix = f" — {f['error']}" if f["error"] else ""
        report.append(f"- **{f['source_name']}**: {f['status']}, {f['entry_count']} entries{suffix}")
    report.extend(["", "## Candidate stories", ""])
    if not items:
        report.append("No matching stories found in the lookback window. Review feed health and keywords.")
    for idx, item in enumerate(items, 1):
        report.extend([
            f"### {idx}. {item['title']}",
            f"- Source: {item['source_name']}",
            f"- Published: {item['published_at_local']}",
            f"- Relevance score: {item['score']}",
            f"- Matched keywords: {', '.join(item['matched_keywords'])}",
            f"- URL: {item['url']}",
            f"- Publisher (RSS metadata): {item.get('publisher_name') or '(not supplied)'}",
            f"- Publisher URL (RSS metadata): {item.get('publisher_url') or '(not supplied)'}",
            f"- Summary: {item['summary'] or '(No RSS summary supplied)'}",
            ""
        ])
    report_path.write_text("\n".join(report), encoding="utf-8")

    log.info("Finished. fetched=%d candidates=%d new/upserted=%d", fetched_count, len(items), inserted_count)
    print(f"\nCandidates in lookback window: {len(items)}")
    print(f"Report: {report_path}\nJSON:   {json_path}\nCSV:    {csv_path}")


if __name__ == "__main__":
    collect()
