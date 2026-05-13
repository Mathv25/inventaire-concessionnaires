#!/usr/bin/env python3
"""
Cherche GM / Directeur général / Used Cars Manager via Playwright (pages rendues JS).
Usage: python find_contacts.py [output_contacts.csv]
"""

import csv, re, sys, time, random, unicodedata
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ── Config ─────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
}

# Chemins de pages équipe/contact à tenter (ordre de priorité)
TEAM_PATHS = [
    "/fr/equipe", "/fr/notre-equipe", "/fr/a-propos/equipe",
    "/fr/a-propos", "/fr/contact", "/fr/nous-joindre",
    "/equipe", "/notre-equipe", "/team", "/our-team",
    "/a-propos", "/about", "/about-us", "/about/team",
    "/contact", "/contactez-nous", "/nous-joindre",
    "/direction", "/management", "/leadership",
    "/en/team", "/en/about", "/en/our-team",
    "/en/contact",
]

# Titres ciblés — GM, Directeur général, Directeur occasion
TITLE_RE = re.compile(
    r"g[ée]n[ée]ral\s+manager"
    r"|directeur\s+g[ée]n[ée]ral"
    r"|director\s+general"
    r"|used\s+(?:car|vehicle|inventory)s?\s+(?:manager|director)"
    r"|directeur\s+(?:des\s+)?(?:v[ée]hicules?\s+)?(?:d.occasion|usag[ée]s?)"
    r"|gestionnaire\s+(?:des\s+)?(?:v[ée]hicules?\s+)?(?:d.occasion|usag[ée]s?)"
    r"|responsable\s+(?:des\s+)?(?:v[ée]hicules?\s+)?(?:d.occasion|usag[ée]s?)"
    r"|chef\s+(?:des\s+)?(?:v[ée]hicules?\s+)?(?:d.occasion|usag[ée]s?)"
    r"|manager[,\s]+used"
    r"|pr[ée]sident(?:\s*(?:et|&|/)\s*propri[ée]taire)?"
    r"|president(?:\s*(?:and|&|/)\s*owner)?"
    r"|propri[ée]taire"
    r"|owner",
    re.I,
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Mots à exclure d'un "nom" (pour éviter de capturer du bruit)
NOT_NAME_RE = re.compile(
    r"directeur|manager|g[ée]n[ée]ral|occasion|v[ée]hicule|usag|contact"
    r"|[ée]quipe|team|t[ée]l[ée]phone|courriel|email|ext\.|poste|dept"
    r"|bienvenue|welcome|service|vente|finance|assurance|garantie"
    r"|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|\d{3}[-.\s]\d{3}|\d{4}",
    re.I,
)


def pause():
    time.sleep(random.uniform(2.0, 3.5))

def quick():
    time.sleep(random.uniform(0.8, 1.5))


def base_url(url):
    m = re.match(r"(https?://[^/]+)", url)
    return m.group(1) if m else url.rstrip("/")


def extract_contacts_from_text(text, page_url):
    """
    Parse le texte brut ligne par ligne.
    Cherche un titre cible puis le nom dans les lignes adjacentes.
    Retourne liste de dicts.
    """
    lines = [l.strip() for l in text.replace("\t", "\n").split("\n") if l.strip()]
    contacts = []

    for i, line in enumerate(lines):
        if not TITLE_RE.search(line):
            continue

        title = line.strip()
        # Fenêtre: 4 lignes avant + 4 après
        window = lines[max(0, i - 4): i + 5]
        name = None
        email = None

        for w in window:
            # Email
            em = EMAIL_RE.findall(w)
            if em and not email:
                email = em[0]

            if w == line:
                continue

            # Nom : 2–4 mots, commence par majuscules, pas de bruit
            words = w.split()
            if 2 <= len(words) <= 4 and not NOT_NAME_RE.search(w):
                # Chaque mot commence par majuscule ou apostrophe/tiret
                if all(
                    ww[0].isupper() or ww[0] in ("'", "-", "L", "D")
                    for ww in words if ww
                ):
                    if not name:
                        name = w.strip()

        contacts.append({
            "nom": name or "",
            "titre": title,
            "email": email or "",
            "source_url": page_url,
        })

    # Dédupliquer sur (nom, titre tronqué)
    seen, unique = set(), []
    for c in contacts:
        key = (c["nom"].lower(), c["titre"][:50].lower())
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def scrape_page_playwright(pw_browser, url):
    """
    Charge une URL avec Playwright et retourne le texte rendu.
    """
    try:
        page = pw_browser.new_page(
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8"},
        )
        page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        try:
            page.goto(url, wait_until="networkidle", timeout=25000)
        except Exception:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                page.close()
                return None, None
        time.sleep(2.0)
        text = page.inner_text("body")
        final_url = page.url
        page.close()
        return text, final_url
    except Exception:
        return None, None


def find_team_link_in_page(pw_browser, base):
    """
    Charge la homepage et cherche un lien vers la page d'équipe dans le menu.
    Retourne l'URL trouvée ou None.
    """
    try:
        page = pw_browser.new_page(user_agent=HEADERS["User-Agent"])
        page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        try:
            page.goto(base, wait_until="networkidle", timeout=20000)
        except Exception:
            page.goto(base, wait_until="domcontentloaded", timeout=15000)
        time.sleep(1.5)

        for sel in [
            'a:text-matches("équipe", "i")',
            'a:text-matches("notre équipe", "i")',
            'a:text-matches("team", "i")',
            'a:text-matches("our team", "i")',
            'a:text-matches("à propos", "i")',
            'a:text-matches("about", "i")',
            'a:text-matches("direction", "i")',
            'a:text-matches("management", "i")',
        ]:
            try:
                locs = page.locator(sel).all()
                for loc in locs:
                    href = loc.get_attribute("href")
                    if href and not re.search(r"#|javascript|mailto", href, re.I):
                        if not href.startswith("http"):
                            href = base.rstrip("/") + href
                        page.close()
                        return href
            except Exception:
                continue
        page.close()
    except Exception:
        pass
    return None


def process_dealer(name, city, url, pw_browser):
    burl = base_url(url)
    all_contacts = []

    # 1. Essayer les chemins connus en ordre
    found_team_url = None
    for path in TEAM_PATHS:
        candidate = burl + path
        text, final_url = scrape_page_playwright(pw_browser, candidate)
        if text and len(text) > 500:
            # Vérifier que la page parle d'équipe/contact/personnel
            kw_hits = sum(1 for kw in ["équipe", "team", "directeur", "manager",
                                        "contact", "about", "direction", "personnel",
                                        "présiden", "propri"]
                          if kw in text.lower())
            if kw_hits >= 2:
                contacts = extract_contacts_from_text(text, final_url)
                if contacts:
                    all_contacts.extend(contacts)
                    found_team_url = final_url
                    break
        quick()

    # 2. Si rien trouvé via paths directs, chercher le lien dans la homepage
    if not all_contacts:
        team_link = find_team_link_in_page(pw_browser, burl)
        if team_link and team_link != burl:
            text, final_url = scrape_page_playwright(pw_browser, team_link)
            if text:
                contacts = extract_contacts_from_text(text, final_url)
                all_contacts.extend(contacts)

    # 3. Fallback: homepage elle-même (certains petits dealers mettent tout là)
    if not all_contacts:
        text, final_url = scrape_page_playwright(pw_browser, burl)
        if text:
            contacts = extract_contacts_from_text(text, burl)
            all_contacts.extend(contacts)

    for c in all_contacts:
        c["concessionnaire"] = name
        c["ville"] = city
        c["url_dealer"] = url

    return all_contacts


def main():
    out_file = sys.argv[1] if len(sys.argv) > 1 else "output/contacts_gm.csv"

    with open("output/results.csv", encoding="utf-8") as f:
        dealers = list(csv.DictReader(f))

    # Seulement les dealers avec une URL valide et pas fermés
    with_url = [
        d for d in dealers
        if d.get("URL_trouvee", "").startswith("http")
        and d.get("Source", "") not in ("FERMÉ", "ferme")
    ]
    print(f"Dealers à traiter: {len(with_url)}")

    OUT_FIELDS = [
        "concessionnaire", "ville", "rep", "nom", "titre", "email",
        "source_url", "url_dealer",
    ]

    with open(out_file, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=OUT_FIELDS).writeheader()

    found_total = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        for i, d in enumerate(with_url, 1):
            name = d["Concessionnaire"]
            city = d["Ville"]
            rep  = d.get("Rep", "")
            url  = d["URL_trouvee"]
            print(f"[{i}/{len(with_url)}] {name} ({city})")

            try:
                contacts = process_dealer(name, city, url, browser)
            except Exception as e:
                print(f"    ✗ {e}")
                contacts = []

            if contacts:
                print(f"    ✓ {len(contacts)} contact(s)")
                for c in contacts:
                    print(f"      {c['nom'] or '?'} | {c['titre'][:55]} | {c['email'] or '—'}")
                for c in contacts:
                    c["rep"] = rep
                with open(out_file, "a", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=OUT_FIELDS, extrasaction="ignore")
                    w.writerows(contacts)
                found_total += len(contacts)
            else:
                print("    — aucun contact")

            pause()

        browser.close()

    print(f"\nTerminé — {found_total} contacts → {out_file}")


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()
    main()
