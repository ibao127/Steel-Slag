import csv
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
INPUT_FILE = DATA_DIR / "articles_links.txt"
OUTPUT_FILE = DATA_DIR / "article_content.csv"
SCRAPED_FILE = DATA_DIR / "scraped_articles.csv"
DELAY_SECONDS = 1.5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Steel-Slag research scraper)",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}

ARTICLE_SELECTORS = (
    "[itemprop='articleBody']",
    "article",
    "main",
    "[role='main']",
    "[class*='article-body']",
    "[class*='articleBody']",
    "[class*='article-content']",
    "[class*='article__body']",
    "[class*='story-body']",
    "[class*='entry-content']",
    "[class*='content-body']",
    "body",
)

UNWANTED_SELECTORS = (
    "nav, header, footer, aside, script, style, noscript, "
    "[role='navigation'], [class*='related'], [class*='share'], "
    "[class*='advert'], [class*='newsletter'], [class*='comment']"
)


def get_title(soup):
    """Get the article title from common page metadata or headings."""
    meta = soup.find("meta", attrs={"property": "og:title"})
    if meta and meta.get("content"):
        return meta["content"].strip()

    heading = soup.find("h1")
    if heading:
        return heading.get_text(" ", strip=True)

    return soup.title.get_text(" ", strip=True) if soup.title else ""


def text_from_markup(markup):
    """Extract paragraph and subheading text from an HTML fragment."""
    fragment = BeautifulSoup(markup, "html.parser")
    for unwanted in fragment.select(UNWANTED_SELECTORS):
        unwanted.decompose()

    parts = []
    seen = set()
    for element in fragment.find_all(["p", "h2", "h3"]):
        text = element.get_text(" ", strip=True)
        if text and text not in seen:
            parts.append(text)
            seen.add(text)
    if parts:
        return "\n\n".join(parts)

    return fragment.get_text(" ", strip=True)


def jsonld_article_text(soup):
    """Find articleBody text in JSON-LD, if the page provides it."""
    bodies = []

    def visit(value):
        if isinstance(value, dict):
            body = value.get("articleBody")
            if isinstance(body, str) and body.strip():
                bodies.append(body)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            visit(json.loads(script.string or script.get_text()))
        except json.JSONDecodeError:
            continue

    return max((text_from_markup(body) for body in bodies), key=len, default="")


def extract_article(soup):
    """Return (title, text, extraction method) using several page patterns."""
    title = get_title(soup)
    candidates = []

    for selector in ARTICLE_SELECTORS:
        for element in soup.select(selector):
            text = text_from_markup(str(element))
            if text:
                candidates.append(text)

    html_text = max(candidates, key=len, default="")
    structured_text = jsonld_article_text(soup)
    if len(structured_text) > len(html_text):
        return title, structured_text, "json-ld"
    if html_text:
        return title, html_text, "html"
    if structured_text:
        return title, structured_text, "json-ld"
    return title, "", ""


def scrape_url(session, url):
    """Fetch one public page and return a CSV-ready result row."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {
            "url": url,
            "final_url": "",
            "title": "",
            "text": "",
            "extraction_method": "",
            "status": "error",
            "error": "Invalid URL; expected an http or https link",
        }

    try:
        response = session.get(url, timeout=30, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException as error:
        return {
            "url": url,
            "final_url": "",
            "title": "",
            "text": "",
            "extraction_method": "",
            "status": "error",
            "error": f"{type(error).__name__}: {error}",
        }

    content_type = response.headers.get("Content-Type", "").lower()
    if content_type and "html" not in content_type:
        return {
            "url": url,
            "final_url": response.url,
            "title": "",
            "text": "",
            "extraction_method": "",
            "status": "not_html",
            "error": f"Expected an HTML page, got {content_type}",
        }

    soup = BeautifulSoup(response.text, "html.parser")
    title, text, method = extract_article(soup)
    status = "ok" if text else "no_text"
    error = "" if text else "No article text found in the returned HTML"

    return {
        "url": url,
        "final_url": response.url,
        "title": title,
        "text": text,
        "extraction_method": method,
        "status": status,
        "error": error,
    }


def main():
    if not INPUT_FILE.exists():
        raise SystemExit(f"Input link file not found: {INPUT_FILE}")

    urls = []
    seen = set()
    for line in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        url = line.strip()
        if url and not url.startswith("#") and url not in seen:
            urls.append(url)
            seen.add(url)

    DATA_DIR.mkdir(exist_ok=True)
    fields = [
        "url",
        "final_url",
        "title",
        "text",
        "extraction_method",
        "status",
        "error",
    ]
    scraped_fields = ["url", "title", "status"]

    with requests.Session() as session:
        session.headers.update(HEADERS)
        with OUTPUT_FILE.open("w", newline="", encoding="utf-8-sig") as output, \
                SCRAPED_FILE.open("w", newline="", encoding="utf-8-sig") as scraped_output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            scraped_writer = csv.DictWriter(scraped_output, fieldnames=scraped_fields)
            scraped_writer.writeheader()

            for index, url in enumerate(urls, start=1):
                result = scrape_url(session, url)
                writer.writerow(result)
                if result["status"] == "ok":
                    scraped_writer.writerow({
                        "url": result["url"],
                        "title": result["title"],
                        "status": result["status"],
                    })
                output.flush()
                print(
                    f"[{index}/{len(urls)}] {result['status']}: "
                    f"{result['title'] or url}"
                )
                if index < len(urls):
                    time.sleep(DELAY_SECONDS)

    print(f"\nSaved results to {OUTPUT_FILE} and successful extractions to {SCRAPED_FILE}")


if __name__ == "__main__":
    main()