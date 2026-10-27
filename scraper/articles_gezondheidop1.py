import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
 
import requests
from bs4 import BeautifulSoup
 
# --------------------------------------------------------------------------- #
# SETTINGS - change these if needed
# --------------------------------------------------------------------------- #
START_URL = "https://gezondheidop1.com/media-categorie/artikel/"
MAX_PAGES = 50                 # safety limit for pagination
CHECK_ARTICLE_PAGES = True     # open each article to check for paywall/video (slower, more accurate)
SKIP_LIKELY_PAYWALL = True     # skip articles from paywall outlets even when the page couldn't be checked
SKIP_AUDIO = True              # also skip podcasts / radio items
DELAY_SECONDS = 1.5            # pause between requests, to be polite to the websites
 
# Only keep items with at least one of these Gezondheid op 1 topic tags, e.g. ["staalslakken", "slakken"].
# Leave empty [] to keep all topics. (Tags seen on the site: staalslakken, slakken, tata, industrie,
# vervuiling, falend-beleid, falend-toezicht, gezondheid, greenwashing, sluiting ...)
ONLY_TAGS = []
 
# Outlets that put (some or all) articles behind a paywall.
# "hard" = nearly everything is paid; "mixed" = some articles free, some paid.
PAYWALL_OUTLETS = {
    # hard
    "fd.nl": "hard", "nrc.nl": "hard", "ftm.nl": "hard", "groene.nl": "hard",
    "volkskrant.nl": "mixed", "trouw.nl": "mixed", "parool.nl": "mixed",
    # DPG Media regional (Plus articles)
    "ad.nl": "mixed", "gelderlander.nl": "mixed", "bndestem.nl": "mixed", "ed.nl": "mixed",
    "tubantia.nl": "mixed", "destentor.nl": "mixed", "pzc.nl": "mixed", "bd.nl": "mixed",
    # Mediahuis
    "telegraaf.nl": "mixed", "ijmuidercourant.nl": "mixed", "haarlemsdagblad.nl": "mixed",
    "noordhollandsdagblad.nl": "mixed", "leidschdagblad.nl": "mixed", "gooieneemlander.nl": "mixed",
    "dvhn.nl": "mixed", "lc.nl": "mixed", "frieschdagblad.nl": "mixed", "limburger.nl": "mixed",
    # other
    "nrc.com": "hard", "ft.com": "hard", "economist.com": "hard",
}
 
VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "npostart.nl", "npo.nl", "dailymotion.com", "tiktok.com")
VIDEO_URL_HINTS = ("/video/", "/videos/", "/uitzending", "/tv/", "/kijk/", "/documentaire")
AUDIO_HOSTS = ("spotify.com", "podcasts.apple.com", "soundcloud.com", "nporadio1.nl")
AUDIO_URL_HINTS = ("/podcast", "/audio/", "/radio/")
TRACKING_PARAMS = re.compile(r"^(utm_|at_|fbclid|gclid|mc_|ref$|source$)", re.I)
 
DUTCH_MONTHS = {m: i for i, m in enumerate(
    ["januari", "februari", "maart", "april", "mei", "juni", "juli",
     "augustus", "september", "oktober", "november", "december"], start=1)}
DUTCH_MONTHS.update({m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)})
DATE_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(DUTCH_MONTHS) + r")\s+(\d{4})\b", re.I)
CTA_RE = re.compile(r"^[\s→>»\-–]*(lees|bekijk|kijk|luister|watch|read|listen)\b", re.I)
ARROWS_RE = re.compile(r"^[\s→>»\-–]+|[\s→>»]+$")
 
HEADERS = {
    "User-Agent": "Mozilla/5.0 (UvA student research project; non-commercial) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}
 
SCRIPT_DIR = Path(__file__).resolve().parent
OUT_DIR = SCRIPT_DIR / "data"
SITE_HOST = urlparse(START_URL).netloc.replace("www.", "")
 
 
# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
 
session = requests.Session()
session.headers.update(HEADERS)
 
 
def fetch(url):
    """Return (final_url, html) or (None, None) on failure."""
    try:
        r = session.get(url, timeout=25, allow_redirects=True)
    except requests.RequestException as e:
        print(f"    ! could not open {url[:80]} ({type(e).__name__})")
        return None, None
    if r.status_code != 200:
        return None, None
    return r.url, r.text
 
 
def host_of(url):
    return urlparse(url).netloc.lower().replace("www.", "")
 
 
def matches_host(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)
 
 
def clean_url(url):
    """Remove tracking parameters like ?at_medium=sharing so duplicates match."""
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    return urlunparse(p._replace(query=urlencode(q), fragment=""))
 
 
def dutch_date_to_iso(text):
    m = DATE_RE.search(text or "")
    if not m:
        return "", ""
    d, mon, y = int(m.group(1)), DUTCH_MONTHS[m.group(2).lower()], int(m.group(3))
    return m.group(0), f"{y:04d}-{mon:02d}-{d:02d}"
 
 
# --------------------------------------------------------------------------- #
# Step 1: read the listing pages
# --------------------------------------------------------------------------- #
 
def find_container(heading):
    """Walk up from a heading to the smallest element that also holds the item's button/date."""
    node = heading
    for _ in range(6):
        parent = node.parent
        if parent is None or parent.name in ("body", "html", "main"):
            break
        # stop if this parent already holds several headings (= it's the whole list)
        if len(parent.find_all(["h2", "h3", "h4", "h5"])) > 1:
            break
        node = parent
        if DATE_RE.search(node.get_text(" ", strip=True)) and any(
                CTA_RE.match(a.get_text(" ", strip=True) or "") for a in node.find_all("a")):
            break
    return node
 
 
def parse_loop_items(soup, page_url):
    """Gezondheid op 1 layout (Elementor loop grid): one div.e-loop-item per article."""
    items = []
    for box in soup.select("div.e-loop-item"):
        h = box.find(["h2", "h3", "h4", "h5"])
        a = h.find("a", href=True) if h else None
        if a is None:
            continue
        url = urljoin(page_url, a["href"])
        if host_of(url).endswith(SITE_HOST):
            continue
        meta = box.select_one(".elementor-shortcode")
        meta_text = meta.get_text(" ", strip=True) if meta else ""
        date_text, date_iso = dutch_date_to_iso(meta_text or box.get_text(" ", strip=True))
        outlet = meta_text.split("|")[0].strip() if "|" in meta_text else ""
        cta = ""
        for link in box.find_all("a"):
            t = link.get_text(" ", strip=True)
            if CTA_RE.match(t):
                cta = ARROWS_RE.sub("", t)
        classes = box.get("class", [])
        items.append({
            "title": h.get_text(" ", strip=True), "outlet": outlet, "date": date_text, "date_iso": date_iso,
            "tags": ", ".join(c.replace("media_tag-", "") for c in classes if c.startswith("media_tag-")),
            "url": url, "clean_url": clean_url(url), "button_text": cta, "listing_page": page_url,
        })
    return items
 
 
def parse_listing(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup.select("nav, header, footer, script, style, noscript"):
        junk.decompose()
 
    items = parse_loop_items(soup, page_url)
    if items:
        return items, soup
 
    # Fallback for other layouts: any heading that links to an outside site.
    for h in soup.find_all(["h2", "h3", "h4", "h5"]):
        a = h.find("a", href=True) or (h.parent.find("a", href=True) if h.parent and h.parent.name == "a" else None)
        if a is None and h.parent is not None and h.parent.name == "a":
            a = h.parent
        if a is None:
            continue
        url = urljoin(page_url, a["href"])
        if host_of(url).endswith(SITE_HOST) or not url.startswith("http"):
            continue  # internal link (menu, category...), not an article
        title = h.get_text(" ", strip=True)
        box = find_container(h)
        text = box.get_text(" | ", strip=True)
 
        cta = ""
        for link in box.find_all("a"):
            t = link.get_text(" ", strip=True)
            if CTA_RE.match(t):
                cta = ARROWS_RE.sub("", t)
                break
 
        date_text, date_iso = dutch_date_to_iso(text)
        # outlet = the short text bit right before the date (e.g. "NH Nieuws")
        outlet = ""
        rest = [p.strip() for p in text.replace(title, "", 1).split("|") if p.strip()]
        for i, part in enumerate(rest):
            if date_text and date_text in part:
                before = part.split(date_text)[0].strip(" -–·,")
                outlet = before or (rest[i - 1] if i > 0 else "")
                break
        if outlet and CTA_RE.match(outlet):
            outlet = ""
 
        items.append({
            "title": title, "outlet": outlet, "date": date_text, "date_iso": date_iso,
            "tags": "", "url": url, "clean_url": clean_url(url), "button_text": cta, "listing_page": page_url,
        })
    return items, soup
 
 
def next_page_url(soup, current_url, page_no):
    # Infinite scroll / "load more" (Elementor): the next page is written in the HTML.
    anchor = soup.select_one(".e-load-more-anchor[data-next-page]")
    if anchor is not None:
        try:
            if int(anchor.get("data-page", 0)) >= int(anchor.get("data-max-page", 0)):
                return None
        except ValueError:
            pass
        return urljoin(current_url, anchor["data-next-page"])
    link = soup.find("a", rel=lambda r: r and "next" in r) or \
        soup.find("a", string=re.compile(r"volgende|next|ouder|older", re.I))
    if link and link.get("href"):
        return urljoin(current_url, link["href"])
    # WordPress-style fallback: /page/2/, /page/3/ ...
    base = START_URL.rstrip("/")
    return f"{base}/page/{page_no + 1}/"
 
 
def scrape_listing():
    all_items, seen, url = [], set(), START_URL
    for page_no in range(1, MAX_PAGES + 1):
        if not url:
            break
        final, html = fetch(url)
        if not html:
            if page_no == 1:
                sys.exit(f"Could not open {START_URL}. Check your internet connection.")
            break
        items, soup = parse_listing(html, final)
        new = [it for it in items if it["clean_url"] not in seen]
        if not new:
            break
        for it in new:
            seen.add(it["clean_url"])
        all_items += new
        anchor = soup.select_one(".e-load-more-anchor[data-max-page]")
        total = f" of {anchor['data-max-page']}" if anchor is not None else ""
        print(f"Page {page_no}{total}: {len(new)} items")
        url = next_page_url(soup, final, page_no)
        time.sleep(DELAY_SECONDS)
    if not all_items:
        sys.exit("Found no article items. The page may load its list with JavaScript; "
                 "send the error/HTML to whoever wrote this script.")
    return all_items
 
 
# --------------------------------------------------------------------------- #
# Step 2: classify each item (article / video / audio, free / paywall)
# --------------------------------------------------------------------------- #
 
def type_from_listing(item):
    host, path, cta = host_of(item["url"]), urlparse(item["url"]).path.lower(), item["button_text"].lower()
    if matches_host(host, VIDEO_HOSTS) or any(h in path for h in VIDEO_URL_HINTS):
        return "video", "video link"
    if matches_host(host, AUDIO_HOSTS) or any(h in path for h in AUDIO_URL_HINTS):
        return "audio", "podcast/audio link"
    has_read = "lees" in cta or "read" in cta
    if not has_read and re.search(r"\b(bekijk|kijk|watch)\b", cta):
        return "video", f"button says '{item['button_text']}'"
    if not has_read and re.search(r"\b(luister|listen)\b", cta):
        return "audio", f"button says '{item['button_text']}'"
    if has_read and re.search(r"\b(bekijk|kijk)\b", cta):
        return "article_with_video", f"button says '{item['button_text']}' (text article with a video in it)"
    return "article", ""
 
 
def iter_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                yield d
                for k in ("@graph", "mainEntity", "hasPart"):
                    v = d.get(k)
                    if isinstance(v, list):
                        stack += v
                    elif isinstance(v, dict):
                        stack.append(v)
            elif isinstance(d, list):
                stack += d
 
 
def check_article_page(url):
    """Return (paywall: True/False/None, is_video: bool, note)."""
    final, html = fetch(url)
    if not html:
        return None, False, "page could not be opened"
    if host_of(final) != host_of(url) and any(w in host_of(final) for w in ("privacy", "consent", "cookie", "myprivacy")):
        return None, False, "cookie wall, could not check"
    soup = BeautifulSoup(html, "html.parser")
    paywall, is_video, types = None, False, set()
    for d in iter_jsonld(soup):
        t = d.get("@type")
        types.update(t if isinstance(t, list) else [t])
        free = d.get("isAccessibleForFree")
        if free is not None:
            if str(free).lower() in ("false", "0", "no"):
                paywall = True
            elif paywall is None:
                paywall = False
    if types & {"VideoObject"} and not types & {"NewsArticle", "Article", "ReportageNewsArticle", "AnalysisNewsArticle"}:
        is_video = True
    meta_tier = soup.find("meta", attrs={"property": "article:content_tier"})
    if meta_tier and meta_tier.get("content", "").lower() in ("locked", "metered"):
        paywall = True
    if paywall is None and soup.select_one("[class*='paywall'], [id*='paywall'], [class*='premium-content']"):
        paywall = True
        return paywall, is_video, "paywall element found on page"
    if paywall is True:
        return True, is_video, "page marks itself as not free (isAccessibleForFree=false)"
    if paywall is False:
        return False, is_video, "page marks itself as free"
    return None, is_video, "page gives no paywall information"
 
 
def classify(item):
    kind, why = type_from_listing(item)
    item["type"] = kind
    item_tags = [t.strip() for t in item.get("tags", "").split(",") if t.strip()]
    if ONLY_TAGS and not set(item_tags) & set(ONLY_TAGS):
        item.update(paywall_status="", paywall_note="", kept="no", reason="topic tag not in ONLY_TAGS")
        return item
    paywall, note = None, ""
 
    if kind in ("article", "article_with_video") and CHECK_ARTICLE_PAGES:
        paywall, page_video, note = check_article_page(item["url"])
        if page_video and kind == "article":
            kind, why = "video", "article page is a video page"
            item["type"] = kind
        time.sleep(DELAY_SECONDS)
 
    tier = PAYWALL_OUTLETS.get(next((d for d in PAYWALL_OUTLETS if matches_host(host_of(item["url"]), [d])), ""), "")
    if paywall is True:
        status = "paywall"
    elif paywall is False:
        status = "free"
    elif tier == "hard":
        status = "likely_paywall"
        note = (note + "; " if note else "") + "outlet is (almost) fully paywalled"
    elif tier == "mixed":
        status = "possible_paywall"
        note = (note + "; " if note else "") + "outlet paywalls some articles"
    else:
        status = "free" if not note or "no paywall information" in note else "unknown"
        if status == "free" and not note:
            note = "outlet has no known paywall"
    item["paywall_status"] = status
    item["paywall_note"] = note
 
    if kind == "video":
        keep, reason = False, f"video ({why})"
    elif kind == "audio" and SKIP_AUDIO:
        keep, reason = False, f"audio ({why})"
    elif status == "paywall":
        keep, reason = False, "paywalled (" + note + ")"
    elif status == "likely_paywall" and SKIP_LIKELY_PAYWALL:
        keep, reason = False, "likely paywalled (" + note + ")"
    elif status == "possible_paywall" and SKIP_LIKELY_PAYWALL:
        keep, reason = False, "possibly paywalled, could not confirm free (" + note + ")"
    else:
        keep, reason = True, why
    item["kept"] = "yes" if keep else "no"
    item["reason"] = reason
    return item
 
 
# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
 
def main():
    print(f"Reading {START_URL}")
    items = scrape_listing()
    print(f"\n{len(items)} items found. Checking each one" +
          (f" (opening every article page: roughly {len(items) * DELAY_SECONDS / 60 + len(items) / 60:.0f} minutes)..." if CHECK_ARTICLE_PAGES else "..."))
 
    for n, it in enumerate(items, 1):
        classify(it)
        mark = "KEEP" if it["kept"] == "yes" else "skip"
        print(f"  [{mark}] {it['outlet'] or host_of(it['url'])}: {it['title'][:60]}"
              + ("" if it["kept"] == "yes" else f"  -> {it['reason']}"))
 
    OUT_DIR.mkdir(exist_ok=True)
    fields = ["kept", "reason", "title", "outlet", "date", "date_iso", "tags", "type", "paywall_status",
              "paywall_note", "button_text", "url", "clean_url", "listing_page"]
    with open(OUT_DIR / "articles_all.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(items)
    kept = [it for it in items if it["kept"] == "yes"]
    (OUT_DIR / "articles_links.txt").write_text("\n".join(it["clean_url"] for it in kept) + "\n", encoding="utf-8")
 
    skipped = [it for it in items if it["kept"] == "no"]
    print(f"\nDone: {len(kept)} kept, {len(skipped)} skipped "
          f"({sum(1 for i in skipped if i['reason'].startswith(('video', 'audio')))} video/audio, "
          f"{sum(1 for i in skipped if i['reason'].startswith(('paywall', 'likely', 'possibly')))} paywall, "
          f"{sum(1 for i in skipped if i['reason'].startswith('topic'))} other topic).")
    print(f"Saved to {OUT_DIR / 'articles_all.csv'} and {OUT_DIR / 'articles_links.txt'}")
 
 
if __name__ == "__main__":
    main()