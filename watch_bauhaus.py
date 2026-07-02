#!/usr/bin/env python3
import argparse
import http.server
import json
import logging
import os
import re
import smtplib
import threading
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright

import config

DASHBOARD_PORT = 8765

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / config.STATE_FILE
LOG_PATH = BASE_DIR / config.LOG_FILE
SECRETS_PATH = BASE_DIR / config.SECRETS_FILE
WEB_DIR = BASE_DIR / "docs"
STATUS_PATH = WEB_DIR / "status.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

BAUHAUS_AVAILABILITY_RE = re.compile(r'"availability\\?":\\?"https?://schema\.org/(\w+)')
BAUHAUS_PRICE_RE = re.compile(r'"price\\?":\\?"([0-9.]+)\\?",\\?"priceCurrency')
# Das produktweite (nicht das pro-Verkäufer-) online_purchasability-Flag steht direkt
# hinter dem schließenden "]" des offers-Arrays - getrennt von "online_reservation"
# (= Reservieren & Abholen im Markt), das wir bewusst NICHT auswerten.
BAUHAUS_ONLINE_PURCHASABILITY_RE = re.compile(
    r'\]\\?,\\?"online_purchasability\\?":\\?\{\\?"enabled\\?":(true|false)'
)

OBI_LDJSON_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)

AMAZON_ADD_TO_CART_RE = re.compile(r'data-is-add-to-cart-enabled="(true|false)"')
AMAZON_PRICE_MARKERS = (
    'id="corePriceDisplay_desktop_feature_div"',
    'id="corePrice_desktop"',
    'id="corePrice_feature_div"',
)
AMAZON_OFFSCREEN_PRICE_RE = re.compile(r'class="a-offscreen">([^<]+)</span>')

AVAILABLE_STATUSES = {"InStock", "LimitedAvailability"}


def load_secrets():
    secrets = {}
    if SECRETS_PATH.exists():
        for line in SECRETS_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            secrets[key.strip()] = value.strip()
    # In CI (z.B. GitHub Actions) kommen die Zugangsdaten als Umgebungsvariablen
    # (GitHub Secrets), nicht aus secrets.env - das lokale Mac-Setup nutzt weiter die Datei.
    for key in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "NOTIFY_EMAIL"):
        if os.environ.get(key):
            secrets[key] = os.environ[key]
    return secrets


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def load_status():
    if STATUS_PATH.exists():
        return json.loads(STATUS_PATH.read_text())
    return {
        "watcher_started_at": None,
        "interval_seconds": config.CHECK_INTERVAL_SECONDS,
        "total_runs": 0,
        "last_run_at": None,
        "sites": {},
    }


def save_status(status):
    WEB_DIR.mkdir(exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2))


def extract_bauhaus(html):
    match = BAUHAUS_AVAILABILITY_RE.search(html)
    if not match:
        raise RuntimeError("Verfügbarkeits-Feld nicht gefunden (Seitenstruktur evtl. geändert)")
    purchasability_match = BAUHAUS_ONLINE_PURCHASABILITY_RE.search(html)
    if not purchasability_match:
        raise RuntimeError("online_purchasability-Feld nicht gefunden (Seitenstruktur evtl. geändert)")
    price_match = BAUHAUS_PRICE_RE.search(html)
    # Nur "online bestellbar" zählt als verfügbar - Reservieren&Abholen (online_reservation)
    # wird bewusst ignoriert, siehe BAUHAUS_ONLINE_PURCHASABILITY_RE.
    is_available = (
        match.group(1) in AVAILABLE_STATUSES and purchasability_match.group(1) == "true"
    )
    return {
        "raw_status": f"{match.group(1)}/online_purchasability={purchasability_match.group(1)}",
        "available": is_available,
        "price": price_match.group(1) if price_match else None,
    }


def extract_obi(html):
    for block in OBI_LDJSON_RE.findall(html):
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            offers = data.get("offers") or []
            if isinstance(offers, dict):
                offers = [offers]
            if not offers:
                continue
            availability_url = offers[0].get("availability", "")
            raw_status = availability_url.rsplit("/", 1)[-1]
            return {
                "raw_status": raw_status,
                "available": raw_status in AVAILABLE_STATUSES,
                "price": offers[0].get("price"),
            }
    raise RuntimeError("Product-JSON-LD nicht gefunden (Seitenstruktur evtl. geändert)")


def extract_amazon_price(html):
    # Bewusst nur im Haupt-Kaufbereich der Seite suchen, damit nicht versehentlich
    # der Preis eines anderen Produkts aus einem "Ähnliche Artikel"-Karussell landet.
    marker_pos = -1
    for marker in AMAZON_PRICE_MARKERS:
        marker_pos = html.find(marker)
        if marker_pos != -1:
            break
    if marker_pos == -1:
        return None
    snippet = html[marker_pos:marker_pos + 2000]
    price_match = AMAZON_OFFSCREEN_PRICE_RE.search(snippet)
    return price_match.group(1) if price_match else None


def extract_amazon(html):
    match = AMAZON_ADD_TO_CART_RE.search(html)
    if not match:
        raise RuntimeError("add-to-cart-Attribut nicht gefunden (Seitenstruktur evtl. geändert, oder Captcha/Block)")
    return {
        "raw_status": match.group(1),
        "available": match.group(1) == "true",
        "price": extract_amazon_price(html),
    }


EXTRACTORS = {
    "bauhaus": extract_bauhaus,
    "obi": extract_obi,
    "amazon": extract_amazon,
}


def fetch_html(browser, url):
    page = browser.new_page(user_agent=USER_AGENT, locale="de-DE")
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        status_code = response.status if response else None
        page.wait_for_timeout(2000)
        html = page.content()
    finally:
        page.close()
    if status_code != 200:
        raise RuntimeError(f"Unerwarteter HTTP-Status: {status_code}")
    return html


def check_product(browser, product):
    html = fetch_html(browser, product["url"])
    extractor = EXTRACTORS[product["site"]]
    return extractor(html)


def send_email(secrets, subject, body):
    gmail_address = secrets.get("GMAIL_ADDRESS")
    gmail_password = secrets.get("GMAIL_APP_PASSWORD")
    notify_email = secrets.get("NOTIFY_EMAIL") or gmail_address

    if not gmail_address or not gmail_password:
        logging.error("GMAIL_ADDRESS/GMAIL_APP_PASSWORD fehlen in secrets.env - kann keine Mail senden")
        return False

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = notify_email

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls()
        smtp.login(gmail_address, gmail_password)
        smtp.sendmail(gmail_address, [notify_email], msg.as_string())
    return True


def status_site(status, product):
    return status["sites"].setdefault(
        product["id"],
        {"name": product["name"], "url": product["url"], "runs": 0, "last_check_at": None,
         "raw_status": None, "available": False, "price": None, "last_error": None,
         "consecutive_errors": 0, "last_notified": None},
    )


def process_product(product, state, secrets, browser, now, status):
    pstate = state.setdefault(
        product["id"],
        {"available": False, "last_notified": None, "consecutive_errors": 0, "last_attempt": None},
    )
    site_status = status_site(status, product)

    if pstate.get("consecutive_errors", 0) >= config.MAX_CONSECUTIVE_ERRORS_BEFORE_WARNING:
        last_attempt = pstate.get("last_attempt")
        if last_attempt:
            elapsed = now - datetime.fromisoformat(last_attempt)
            if elapsed < timedelta(minutes=config.BACKOFF_MINUTES_AFTER_WARNING):
                logging.info(
                    "[%s] Im Backoff (seit %d Fehlern), übersprungen", product["id"], pstate["consecutive_errors"]
                )
                return

    pstate["last_attempt"] = now.isoformat()
    site_status["runs"] += 1
    site_status["last_check_at"] = now.isoformat()

    try:
        result = check_product(browser, product)
    except Exception as exc:
        pstate["consecutive_errors"] = pstate.get("consecutive_errors", 0) + 1
        site_status["last_error"] = str(exc)
        site_status["consecutive_errors"] = pstate["consecutive_errors"]
        logging.error(
            "[%s] Fehler beim Prüfen: %s (in Folge: %d)",
            product["id"], exc, pstate["consecutive_errors"],
        )
        if pstate["consecutive_errors"] == config.MAX_CONSECUTIVE_ERRORS_BEFORE_WARNING:
            send_email(
                secrets,
                f"⚠️ Watcher: Prüfung für {product['name']} schlägt wiederholt fehl",
                f"Der Check für {product['name']} schlägt seit "
                f"{pstate['consecutive_errors']} Versuchen fehl.\n\nLetzter Fehler: {exc}\n\n"
                f"URL: {product['url']}",
            )
        return

    site_status["last_error"] = None
    site_status["consecutive_errors"] = 0
    pstate["consecutive_errors"] = 0
    was_available = pstate.get("available", False)
    is_available = result["available"]
    logging.info(
        "[%s] Status: %s (available=%s), Preis=%s",
        product["id"], result["raw_status"], is_available, result["price"],
    )
    site_status["raw_status"] = result["raw_status"]
    site_status["available"] = is_available
    site_status["price"] = result["price"]

    should_notify = False
    if is_available and not was_available:
        should_notify = True
    elif is_available and was_available:
        last_notified = pstate.get("last_notified")
        if last_notified:
            last_notified_dt = datetime.fromisoformat(last_notified)
            if now - last_notified_dt >= timedelta(hours=config.RENOTIFY_HOURS):
                should_notify = True
        else:
            should_notify = True

    if should_notify:
        price_display = result["price"]
        if price_display and "€" not in price_display:
            price_display = f"{price_display} EUR"
        price_line = f"Preis: {price_display}\n" if price_display else ""
        sent = send_email(
            secrets,
            f"✅ {product['name']} ist bestellbar!",
            f"{product['name']} ist jetzt online bestellbar.\n\n"
            f"{price_line}"
            f"Link: {product['url']}\n\n"
            f"Geprüft am: {now.strftime('%d.%m.%Y %H:%M')}",
        )
        if sent:
            pstate["last_notified"] = now.isoformat()
            logging.info("[%s] Benachrichtigungs-Mail gesendet", product["id"])

    pstate["available"] = is_available
    site_status["last_notified"] = pstate.get("last_notified")


def run_cycle():
    secrets = load_secrets()
    state = load_state()
    status = load_status()
    now = datetime.now()

    if status["watcher_started_at"] is None:
        status["watcher_started_at"] = now.isoformat()
    status["interval_seconds"] = config.CHECK_INTERVAL_SECONDS
    status["total_runs"] += 1
    status["last_run_at"] = now.isoformat()
    status["email_configured"] = bool(
        secrets.get("GMAIL_ADDRESS") and secrets.get("GMAIL_APP_PASSWORD")
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            for product in config.PRODUCTS:
                process_product(product, state, secrets, browser, now, status)
        finally:
            browser.close()

    save_state(state)
    save_status(status)


def watcher_loop():
    while True:
        try:
            run_cycle()
        except Exception:
            logging.exception("Unerwarteter Fehler im Watcher-Zyklus")
        time.sleep(config.CHECK_INTERVAL_SECONDS)


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt, *args):
        logging.info("HTTP " + fmt, *args)


def serve_dashboard():
    WEB_DIR.mkdir(exist_ok=True)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", DASHBOARD_PORT), DashboardHandler)
    logging.info("Dashboard läuft auf http://127.0.0.1:%d", DASHBOARD_PORT)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once", action="store_true",
        help="Nur einen einzelnen Durchlauf ausführen (zum Testen), kein Dauerbetrieb/Dashboard-Server",
    )
    args = parser.parse_args()

    if args.once:
        run_cycle()
        return

    threading.Thread(target=watcher_loop, daemon=True).start()
    serve_dashboard()


if __name__ == "__main__":
    main()
