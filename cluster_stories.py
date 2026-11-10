#!/usr/bin/env python3
"""Product East Stage 2 v4: conservative event clustering + safe publisher URL handling.

This is a heuristic candidate grouper, NOT fact verification. It is deliberately
biased toward false negatives (splitting uncertain reports) rather than false
positives (merging unrelated events).
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from rapidfuzz import fuzz

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "collector.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH, encoding="utf-8")]
)
log = logging.getLogger("producteast.clustering")

STOPWORDS = set("""
the a an and or but to of in on for as at by from with after amid over under into
about says said new latest how why what when where who its their his her they will
could would should has have had is are was were be been being than more less first
second third day days today report reports according that this these those while also
company companies officials official people update updates according announced
announces reportedly during onto their its which that than amid against across
""".split())

SYNONYMS = {
    "crush": "crowd_incident", "stampede": "crowd_incident",
    "crowd": "crowd_incident", "crowd_incident": "crowd_incident",
    "dies": "death", "dead": "death", "killed": "death", "kill": "death",
    "fatal": "death", "deaths": "death", "died": "death",
    "exports": "export", "exporting": "export", "exported": "export",
    "controls": "control", "restrictions": "restrict", "restriction": "restrict",
    "restricts": "restrict", "restricted": "restrict", "curb": "restrict",
    "curbs": "restrict", "curbed": "restrict",
    "chips": "chip", "semiconductors": "semiconductor",
    "technologies": "technology", "tech": "technology",
    "injured": "injury", "hurts": "injury", "hurt": "injury",
    "launches": "launch", "launched": "launch",
    "investigates": "investigation", "investigating": "investigation",
    "proposes": "propose", "proposed": "propose",
    "criminalise": "criminalize", "criminalises": "criminalize", "criminalised": "criminalize",
    "criminalize": "criminalize", "criminalizes": "criminalize", "criminalized": "criminalize",
    "criminalisation": "criminalize", "criminalization": "criminalize", "crime": "criminalize",
    "smuggling": "smuggle", "smuggled": "smuggle", "smuggles": "smuggle",
    "nvidia's": "nvidia", "chips": "chip", "technologies": "technology",
}

# Words that may be shared by many unrelated stories and cannot identify an event.
GENERIC = set("""
china chinese asia asian technology tech artificial intelligence ai business
industry industries government officials company companies market markets
export exports control controls restriction restrictions restrict news world
international new latest country countries economy economic trade policy
technology technologies says said plans plan amid over under
""".split())

GEOGRAPHY = set("""
india taiwan japan korea korean malaysia indonesia vietnam singapore china chinese
us usa america american britain uk europe european africa kenya canada australia
kolkata west bengal madhya pradesh tokyo
""".split())

EVENT_TERMS = set("""
crowd_incident death earthquake fire crash explosion launch deal agreement election
investigation lawsuit acquisition layoff outage recall attack flood storm export
restrict chip semiconductor injury propose
""".split())

# Distinctive subject terms help distinguish separate policies in the same country.
TOPIC_ANCHORS = set("""
solar photovoltaic wafer panel manufacturing artificial intelligence ai model agent
server semiconductor chip tariff prison sentence visa vaccine satellite rocket
quantum clock software app luxury retail school fuel petrol orangutan haze wildfire
""".split())

GOOGLE_NEWS_HOSTS = {"news.google.com", "consent.google.com", "google.com", "www.google.com"}
GOOGLE_INFRASTRUCTURE_SUFFIXES = (".google.com", ".googleusercontent.com", ".gstatic.com")
USER_AGENT = "ProductEastNewsAutomation/0.3 (+https://producteast.com)"


def clean(value: str | None) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", value).strip()


def latest_json() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = sorted(OUTPUT_DIR.glob("news_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError("No news_*.json found in output/. Run run_collector.bat first.")
    return candidates[0]


def strip_publisher_suffix(title: str) -> str:
    """Strip only a likely publisher suffix, not substantive clauses after a dash."""
    title = clean(title)
    # Publisher suffixes usually appear after the final hyphen and contain a brand/site name.
    match = re.search(r"\s+[—–-]\s+([^—–-]{2,80})$", title)
    if match:
        suffix = match.group(1).strip()
        if (re.search(r"\b(www\.|\.com\b|\.net\b|\.org\b|\.in\b|\.co\b|\.uk\b)", suffix, re.I)
                or suffix.casefold() in {
                    "bbc", "reuters", "associated press", "ap", "cna", "techcrunch",
                    "tom's hardware", "pbs", "ndtv profit", "the verge", "cnbc", "marketbeat", "aol.com", "the motley fool", "motley fool", "growth dragons", "cybernews", "stocktwits", "dealroom", "24/7 wall st.", "trendingtopics.eu", "yahoo finance", "south china morning post", "global south opportunities", "college mentor", "dw.com"
                }):
            title = title[:match.start()].strip()
    return title


def tokens(title: str) -> set[str]:
    title = strip_publisher_suffix(title).casefold()
    # Keep numbers out of the basic similarity score; numbers are handled separately.
    title = re.sub(r"\b\d+(?:[.,]\d+)?\b", " ", title)
    result = set()
    for raw in re.findall(r"[a-z]+", title):
        token = SYNONYMS.get(raw, raw)
        if token not in STOPWORDS and (len(token) > 2 or token == "ai"):
            result.add(token)
    return result


def anchors(title: str) -> set[str]:
    return tokens(title) & TOPIC_ANCHORS


def geography(title: str) -> set[str]:
    return tokens(title) & GEOGRAPHY


def primary_geography(title: str) -> str:
    """Best-effort lead geography: first geography term in headline order."""
    for raw in re.findall(r"[a-z]+", strip_publisher_suffix(title).casefold()):
        token = SYNONYMS.get(raw, raw)
        if token in GEOGRAPHY:
            return token
    return ""


def event_markers(title: str) -> set[str]:
    return tokens(title) & EVENT_TERMS


def number_signature(title: str) -> set[str]:
    return set(re.findall(r"\b\d+(?:[.,]\d+)?\b", clean(title).casefold()))


def stock_transaction_investor(title: str) -> set[str]:
    """Extract distinctive investor tokens from stock-holding transaction headlines."""
    text = strip_publisher_suffix(title).casefold()
    if not re.search(r"\b(?:shares?|stake|holdings?|position)\b", text):
        return set()
    if not re.search(r"\b(?:bought|purchased|sold|lifted|reduced|increased|decreased|adds?|cuts?|raises?)\b", text):
        return set()
    match = re.search(r"\bby\s+(.+)$", text)
    if not match:
        return set()
    investor_text = match.group(1)
    investor_text = re.sub(r"\b(?:llc|inc|incorporated|ltd|limited|lp|llp|plc|corp|corporation)\b", " ", investor_text)
    investor_tokens = set(re.findall(r"[a-z]+", investor_text))
    # These describe the instrument/action, not the investor identity.
    investor_tokens -= {"shares", "share", "stock", "stake", "holdings", "holding", "position",
                        "bought", "purchased", "sold", "lifted", "reduced", "increased", "decreased",
                        "company", "manufacturing", "taiwan", "semiconductor", "tsm", "tsmc", "of", "the", "by",
                        "wealth", "partners", "management", "advisors", "advisor", "asset", "assets",
                        "alternative", "capital", "investment", "investments", "financial", "finance",
                        "fund", "funds", "pension", "trust", "bank", "group", "holdings", "holding"}
    return investor_tokens


def google_infrastructure(host: str) -> bool:
    host = (host or "").lower().strip(".")
    return host == "google.com" or host.endswith(GOOGLE_INFRASTRUCTURE_SUFFIXES)


def same_event_score(a: dict, b: dict) -> tuple[float, str, list[str]]:
    """Score two headlines conservatively. Only strong evidence returns >= 0.80."""
    title_a = strip_publisher_suffix(a.get("title", ""))
    title_b = strip_publisher_suffix(b.get("title", ""))
    # Stock ownership disclosures are separate events when the named investors differ,
    # even if a templated headline shares most words and ticker/company terms.
    investors_a = stock_transaction_investor(title_a)
    investors_b = stock_transaction_investor(title_b)
    if investors_a and investors_b and investors_a.isdisjoint(investors_b):
        return 0.10, "different named investors in stock-transaction headlines", sorted(investors_a | investors_b)
    ta, tb = tokens(title_a), tokens(title_b)
    if not ta or not tb:
        return 0.0, "empty title", []

    shared = ta & tb
    informative_shared = shared - GENERIC
    shared_events = event_markers(title_a) & event_markers(title_b)
    shared_geo = geography(title_a) & geography(title_b)
    primary_geo_a = primary_geography(title_a)
    primary_geo_b = primary_geography(title_b)
    shared_anchors = anchors(title_a) & anchors(title_b)
    union = ta | tb
    jaccard = len(informative_shared) / max(1, len((ta | tb) - GENERIC))
    # Compare the original normalized phrase order, not alphabetically sorted tokens.
    phrase_a = " ".join(re.findall(r"[a-z]+", title_a.casefold()))
    phrase_b = " ".join(re.findall(r"[a-z]+", title_b.casefold()))
    phrase_ratio = fuzz.ratio(phrase_a, phrase_b) / 100.0

    nums_a, nums_b = number_signature(title_a), number_signature(title_b)
    # Conflicting explicit figures are a warning, not proof of different events:
    # reports of the same evolving incident can legitimately have different totals.
    number_conflict = bool(nums_a and nums_b and nums_a.isdisjoint(nums_b))

    # Exact / close paraphrases still need at least two informative shared terms.
    if phrase_ratio >= 0.88 and len(informative_shared) >= 2 and not (number_conflict and phrase_ratio < 0.95):
        return phrase_ratio, "high normalized headline similarity", sorted(shared)

    # If the headline leads with different geographies, shared references to a country
    # (e.g. Taiwan export rules mentioning China) must not merge unrelated national policies.
    primary_geo_conflict = bool(primary_geo_a and primary_geo_b and primary_geo_a != primary_geo_b)

    # Taiwan proposal to criminalize AI-chip smuggling to China: different wording
    # ("criminalise", "crime", "smuggling") should still match when all core entities
    # and the policy action are present. This is intentionally narrow.
    if ({"taiwan", "china"}.issubset(ta & tb) and "chip" in shared
            and "smuggle" in shared and "criminalize" in shared):
        return 0.90, "same Taiwan-China chip-smuggling criminalization proposal", sorted(shared)

    # Cross-publisher paraphrases like "stampede" vs "crush": require same event marker,
    # matching geography, and at least one further distinctive term.
    if (not primary_geo_conflict and len(shared_events) >= 2 and shared_geo
            and len(informative_shared) >= 2 and (shared_anchors or "crowd_incident" in shared_events)):
        return 0.82, "matching event markers, geography and context", sorted(shared)

    # Same concrete subject + action + geography. Generic country/topic words cannot
    # satisfy this rule, and distinct anchors (solar vs AI) do not merge.
    action_overlap = shared & {"restrict", "export", "launch", "investigation", "deal",
                               "agreement", "acquisition", "recall", "outage", "death",
                               "crowd_incident", "fire", "crash", "attack", "propose"}
    if not primary_geo_conflict and shared_anchors and action_overlap and shared_geo:
        return 0.82, "shared concrete subject, action and geography", sorted(shared)

    # Very high overlap may match a paraphrase without a predefined anchor.
    if len(informative_shared) >= 4 and jaccard >= 0.68 and phrase_ratio >= 0.68:
        return jaccard, "high distinctive-token overlap", sorted(shared)

    return max(jaccard, phrase_ratio * 0.5), "insufficient evidence for same event", sorted(shared)


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonical = None
        self.og_url = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): v for k, v in attrs}
        if tag.lower() == "link":
            rel = (attrs.get("rel") or "").lower()
            href = attrs.get("href")
            if href and "canonical" in rel:
                self.canonical = href
        if tag.lower() == "meta":
            prop = (attrs.get("property") or attrs.get("name") or "").lower()
            content = attrs.get("content")
            if content and prop == "og:url":
                self.og_url = content


def hostname(url: str) -> str:
    """Return the normalized host (not the registrable publisher domain)."""
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def publisher_domain(url: str) -> str:
    """Best-effort registrable domain, merging common publisher subdomains."""
    host = hostname(url)
    for prefix in ("m.", "mobile.", "amp.", "edition.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    parts = host.split(".") if host else []
    if len(parts) <= 2:
        return host
    multi_level_suffixes = {
        "co.uk", "org.uk", "ac.uk", "com.au", "net.au", "org.au", "co.in", "com.in",
        "co.jp", "com.sg", "com.my", "com.ph", "com.vn", "com.id", "co.kr", "com.tw",
        "com.hk", "co.nz", "com.br", "com.mx", "com.tr", "co.za", "com.cn"
    }
    suffix2 = ".".join(parts[-2:])
    if suffix2 in multi_level_suffixes and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def resolve_publisher_url(url: str, timeout: int = 8) -> tuple[str, str]:
    """Resolve Google News only when the final host is demonstrably not Google.

    Google consent/redirect pages are NOT publisher URLs. On failure, preserve the
    original RSS link and report unresolved; never count google.com as a publisher.
    """
    if not url:
        return "", "missing_url"
    host = hostname(url)
    if host != "news.google.com":
        return url, "direct_url"
    try:
        response = requests.get(
            url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            timeout=timeout, allow_redirects=True
        )
        final_url = response.url
        final_host = hostname(final_url)
        if final_host and not google_infrastructure(final_host):
            return final_url, "redirect_resolved"
        parser = MetaParser()
        parser.feed(response.text[:1_500_000])
        for candidate in (parser.canonical, parser.og_url):
            if candidate:
                candidate = urljoin(final_url, candidate)
                candidate_host = hostname(candidate)
                if candidate_host and not google_infrastructure(candidate_host):
                    return candidate, "metadata_resolved"
        return url, "unresolved_google_news"
    except Exception as exc:
        log.warning("Google News URL resolution failed (%s): %s", type(exc).__name__, url[:100])
        return url, "resolution_error"


def publisher_hint(title: str) -> str:
    """Return a title-suffix publisher hint, not a verified publisher identity."""
    raw = clean(title)
    match = re.search(r"\s+[—–-]\s+([^—–-]{2,100})$", raw)
    if not match:
        return ""
    suffix = match.group(1).strip()
    # Avoid treating substantive clauses as a source label; only common source-style suffixes.
    if re.search(r"\b(?:www\.|\.com\b|\.net\b|\.org\b|\.eu\b|\.co\b|\.uk\b)", suffix, re.I):
        return suffix
    if suffix.casefold() in {
        "bbc", "reuters", "associated press", "ap", "cna", "techcrunch", "tom's hardware",
        "pbs", "ndtv profit", "the verge", "cnbc", "marketbeat", "aol.com", "the motley fool",
        "motley fool", "growth dragons", "cybernews", "stocktwits", "dealroom", "24/7 wall st.",
        "yahoo finance", "south china morning post", "global south opportunities", "college mentor", "dw.com"
    }:
        return suffix
    return ""


def resolve_stories(items: list[dict], config: dict) -> list[dict]:
    if not config.get("resolve_google_news_urls", True):
        for item in items:
            item["resolved_url"] = item.get("url", "")
            host = hostname(item.get("url", ""))
            item["publisher_domain"] = publisher_domain(item.get("url", "")) if host and not google_infrastructure(host) else ""
            item["publisher_domain_source"] = "direct_article_url" if item["publisher_domain"] else ""
            source_url = item.get("publisher_url", "")
            source_host = hostname(source_url)
            if not item["publisher_domain"] and source_host and not google_infrastructure(source_host):
                item["publisher_domain"] = publisher_domain(source_url)
                item["publisher_domain_source"] = "google_news_rss_source_metadata"
            item["publisher_name_hint"] = item.get("publisher_name") or publisher_hint(item.get("title", ""))
            item["url_resolution_status"] = "resolution_disabled"
        return items

    timeout = int(config.get("url_resolution_timeout_seconds", 8))
    cache: dict[str, tuple[str, str]] = {}
    for index, item in enumerate(items, 1):
        original = item.get("url", "")
        if original not in cache:
            cache[original] = resolve_publisher_url(original, timeout)
            # Gentle pacing to avoid hammering Google News.
            if hostname(original) in GOOGLE_NEWS_HOSTS:
                time.sleep(float(config.get("url_resolution_delay_seconds", 0.15)))
        resolved, status = cache[original]
        item["resolved_url"] = resolved
        item["url_resolution_status"] = status
        item["publisher_domain_source"] = "resolved_article_url" if status in {"direct_url", "redirect_resolved", "metadata_resolved"} and not google_infrastructure(hostname(resolved)) else ""
        item["publisher_domain"] = publisher_domain(resolved) if item["publisher_domain_source"] else ""
        # Google News RSS entries often carry a <source url=...> field. This is a
        # publisher/site URL, not necessarily the article URL, but it is better evidence
        # for publisher attribution than a guessed title suffix.
        source_publisher_url = item.get("publisher_url", "")
        source_host = hostname(source_publisher_url)
        if not item["publisher_domain"] and source_host and not google_infrastructure(source_host):
            item["publisher_domain"] = publisher_domain(source_publisher_url)
            item["publisher_domain_source"] = "google_news_rss_source_metadata"
        item["publisher_name_hint"] = item.get("publisher_name") or publisher_hint(item.get("title", ""))
    return items


def cluster_items(items: list[dict], threshold: float = 0.80) -> list[dict]:
    # More specific titles first. Every match is against the representative; no chaining.
    ordered = sorted(
        items,
        key=lambda x: (len(tokens(x.get("title", ""))), int(x.get("score", 0))),
        reverse=True
    )
    clusters: list[dict] = []
    for item in ordered:
        best_cluster = None
        best_score = 0.0
        best_reason = ""
        for cluster in clusters:
            score, reason, _ = same_event_score(item, cluster["stories"][0])
            if score > best_score:
                best_score, best_reason, best_cluster = score, reason, cluster
        if best_cluster is not None and best_score >= threshold:
            best_cluster["stories"].append(item)
            best_cluster["match_reasons"].append(best_reason)
        else:
            clusters.append({"stories": [item], "match_reasons": []})

    for cluster in clusters:
        stories = cluster["stories"]
        stories.sort(key=lambda x: (x.get("published_at_utc") or x.get("published_at_local") or ""), reverse=True)
        cluster["representative_title"] = stories[0].get("title", "(untitled)")
        domains = {s.get("publisher_domain") for s in stories if s.get("publisher_domain")}
        cluster["publisher_domains"] = sorted(domains)
        cluster["publisher_domain_count"] = len(domains)
        cluster["story_count"] = len(stories)
        cluster["sources"] = sorted(set((s.get("source_name") or "") for s in stories))
        cluster["independent_publisher_count_estimate"] = len(domains)
        cluster["review_flag"] = "POTENTIAL_SAME_EVENT" if len(stories) > 1 else "SINGLE_STORY"
        cluster["verification_status"] = "NOT_VERIFIED"
        cluster["warning"] = (
            "Headline similarity only. Publisher-domain count is an estimate and does not prove "
            "editorial independence. Verify article text and facts before drafting."
        )
    clusters.sort(
        key=lambda c: (c["story_count"], max((int(s.get("score", 0)) for s in c["stories"]), default=0)),
        reverse=True
    )
    for i, cluster in enumerate(clusters, 1):
        cluster["cluster_id"] = f"E{i:03d}"
    return clusters


def render_report(source_path: Path, items: list[dict], clusters: list[dict], stamp: str) -> str:
    multi = sum(1 for c in clusters if c["story_count"] > 1)
    unresolved = sum(1 for s in items if s.get("url_resolution_status", "").startswith("unresolved")
                     or s.get("url_resolution_status") == "resolution_error")
    lines = [
        f"# Product East — Conservative event clustering v4 ({stamp})", "",
        f"- Input: `{source_path.name}`",
        f"- Candidate stories: {len(items)}",
        f"- Potential same-event groups: {multi}",
        f"- Single-story groups: {len(clusters) - multi}",
        f"- Google News URLs unresolved or failed: {unresolved}",
        "",
        "> This is conservative headline matching, not fact verification. A cluster is only a candidate. "
        "Publisher-domain counts are estimates and do not prove independent reporting.",
        ""
    ]
    for cluster in clusters:
        lines.extend([
            f"## {cluster['cluster_id']} — {clean(cluster['representative_title'])}",
            f"- Status: **{cluster['review_flag']}**",
            f"- Items: {cluster['story_count']}",
            f"- Publisher domains identified: {cluster['publisher_domain_count']}",
            f"- Domains: {', '.join(cluster['publisher_domains']) or '(not resolved)'}",
            f"- Feeds: {', '.join(cluster['sources'])}",
            f"- Verification: **{cluster['verification_status']}**",
            ""
        ])
        for story in cluster["stories"]:
            lines.extend([
                f"- **{clean(story.get('title'))}**",
                f"  - Feed: {story.get('source_name', '')}",
                f"  - Published: {story.get('published_at_local', '')}",
                f"  - Original URL: {story.get('url', '')}",
                f"  - Resolved URL: {story.get('resolved_url') or story.get('url', '')}",
                f"  - URL resolution: {story.get('url_resolution_status', 'not_attempted')}",
                f"  - Publisher domain: {story.get('publisher_domain') or '(unverified)'}",
                f"  - Publisher name: {story.get('publisher_name_hint') or '(none)'}",
                f"  - Publisher domain evidence: {story.get('publisher_domain_source') or '(unverified)'}",
                f"  - Publisher source URL: {story.get('publisher_url') or '(not supplied)'}",
                f"  - Summary: {clean(story.get('summary', ''))[:400]}",
            ])
        lines.append("")
    return "\n".join(lines)


def main():
    source_path = latest_json()
    data = json.loads(source_path.read_text(encoding="utf-8"))
    items = data.get("stories", [])
    if not isinstance(items, list):
        raise ValueError("Input JSON must contain a 'stories' list.")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    items = resolve_stories(items, config)
    clusters = cluster_items(items)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M")
    json_path = OUTPUT_DIR / f"clusters_{stamp}.json"
    report_path = OUTPUT_DIR / f"clusters_{stamp}.md"
    multi = sum(1 for c in clusters if c["story_count"] > 1)

    payload = {
        "stage_version": "2-v4",
        "input_file": source_path.name,
        "generated_at": datetime.now().astimezone().isoformat(),
        "candidate_count": len(items),
        "cluster_count": len(clusters),
        "multi_story_clusters": multi,
        "unresolved_google_news_urls": sum(
            1 for s in items if s.get("url_resolution_status", "").startswith("unresolved")
            or s.get("url_resolution_status") == "resolution_error"
        ),
        "clusters": clusters
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(render_report(source_path, items, clusters, stamp), encoding="utf-8")
    log.info("Stage 2 v4 complete: candidates=%d groups=%d multi_story=%d", len(items), len(clusters), multi)
    print(f"Input: {source_path.name}")
    print(f"Candidates: {len(items)} | groups: {len(clusters)} | multi-story groups: {multi}")
    print(f"Report: {report_path}")
    print(f"JSON:   {json_path}")


if __name__ == "__main__":
    main()
