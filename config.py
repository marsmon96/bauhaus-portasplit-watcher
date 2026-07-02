# "automated": False bedeutet - wird NICHT automatisch geprüft (z.B. weil die Seite
# Cloud-/Rechenzentrums-IPs wie die von GitHub Actions blockt), taucht im Dashboard
# aber trotzdem mit direktem Link auf, damit man selbst manuell nachschauen kann.
PRODUCTS = [
    {
        "id": "bauhaus",
        "name": "Midea PortaSplit 12000 BTU (Bauhaus)",
        "url": "https://www.bauhaus.info/klimaanlagen/midea-klimasplitgeraet-portasplit-12000-btu/p/31934233",
        "site": "bauhaus",
        "automated": False,
        "manual_reason": "Cloudflare blockt GitHub-Actions-IPs (403)",
    },
    {
        "id": "obi",
        "name": "Midea PortaSplit (OBI)",
        "url": "https://www.obi.de/p/8620890/midea-mobile-split-klimaanlage-portasplit",
        "site": "obi",
        "automated": True,
    },
    {
        "id": "amazon",
        "name": "Midea Klimaanlage PortaSplit (Amazon)",
        "url": "https://www.amazon.de/Midea-Klimaanlage-Entfeuchten-Ventilieren-Silent-Modus/dp/B0D3PP64JS?th=1",
        "site": "amazon",
        "automated": False,
        "manual_reason": "Amazon blockt GitHub-Actions-IPs (Captcha/Block)",
    },
]

# Wie oft (in Stunden) eine erneute Erinnerungs-Mail geschickt wird,
# solange ein Produkt verfügbar bleibt (verhindert Mail-Spam bei jedem Check)
RENOTIFY_HOURS = 24

# Nach so vielen aufeinanderfolgenden Fehlern *pro Seite* wird eine Warn-Mail
# geschickt, damit auffällt, dass der Checker für diese Seite nicht mehr funktioniert.
# Bei 5-Minuten-Takt (GitHub Actions Cron) entsprechen 12 Fehlern ca. 1 Stunde.
MAX_CONSECUTIVE_ERRORS_BEFORE_WARNING = 12

# Ab MAX_CONSECUTIVE_ERRORS_BEFORE_WARNING wird diese Seite geschont: statt alle
# 5 Minuten wird sie nur noch in diesem Abstand (Minuten) versucht, bis es wieder
# klappt. Reduziert die Last auf eine Seite, die gerade blockt.
BACKOFF_MINUTES_AFTER_WARNING = 30

STATE_FILE = "state.json"
LOG_FILE = "watcher.log"
SECRETS_FILE = "secrets.env"

# Nur fürs Dashboard (Countdown-Anzeige) - muss zum Cron-Schedule in
# .github/workflows/check.yml übereinstimmen, ändert das eigentliche Intervall nicht.
CHECK_INTERVAL_SECONDS = 300
