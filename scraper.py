#!/usr/bin/env python3
"""
Scraper d'inventaire de véhicules d'occasion - Concessionnaires QC v4
- Validation du contenu: le site doit contenir des mots auto
- 3 requêtes DDG différentes par dealer
- Détection fermé: domaine parqué, 404, aucun contenu auto
- Patterns étendus pour tous les formats québécois
- Probing d'inventaire sur 12 chemins communs
- Fallback autousagee.ca

Usage:
  python scraper.py <input.csv> <output.csv>          # run complet
  python scraper.py <input.csv> <output.csv> retry    # relance non-trouvés seulement
  python scraper.py <input.csv> <output.csv> test 5   # 5 premiers seulement

Format CSV d'entrée attendu (colonnes minimales):
  Concessionnaire, Ville, Rep, Type, Inventaire, URL (optionnel)
"""

import csv, json, os, re, sys, time, random, unicodedata
from datetime import datetime

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False; print("⚠ Playwright non disponible")

try:
    from ddgs import DDGS
    HAS_DDG = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        HAS_DDG = True
    except ImportError:
        HAS_DDG = False; print("⚠ ddgs non disponible — pip install ddgs")

# ── Config ─────────────────────────────────────────────────────────────────────
DELAY_MIN, DELAY_MAX = 2.0, 3.5

HEADERS_HTTP = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

OUTPUT_FIELDS = [
    "Concessionnaire", "Ville", "Rep", "Type",
    "Inventaire_CRM", "URL_trouvee",
    "Inventaire_Scraped", "Source", "Date_Scrape", "Notes",
]

# Mots-clés de marques automobiles
AUTO_BRANDS = {
    "subaru","honda","toyota","ford","chevrolet","gmc","buick","cadillac",
    "chrysler","dodge","jeep","ram","hyundai","kia","nissan","mazda",
    "volkswagen","vw","audi","bmw","mercedes","volvo","mitsubishi",
    "infiniti","lexus","acura","lincoln","genesis","peugeot","renault",
}

# Mots auto dans le contenu (valide qu'un site est vraiment un dealer)
AUTO_CONTENT_WORDS = [
    "véhicule","vehicule","inventaire","occasion","usagé","usages","concessionnaire",
    "neuf","voiture","automobile","auto","camion","berline","suv","vus",
    "kilométrage","kilometrage","odometer","essai","financement","crédit",
    "dealer","dealership","inventory","used","pre-owned","preowned",
    "carfax","vin","garantie","warranty",
]

# Domaines agrégateurs à ignorer
SKIP_DOMAINS = {
    "autohebdo","autotrader","kijiji","facebook","linkedin","yelp",
    "pagesjaunes","yellowpages","google","wikipedia","mapquest","bing",
    "instagram","twitter","youtube","cargurus","motors.ca","carfax",
    "autousagee","vroom","clutch","carpages","commercant","411","canada411",
    "foursquare","tripadvisor","waze","apple.com","auto123","unhaggle",
    "caa.ca","bbb.org","autoexpert","destinationrouyn","tourisme",
    "jd.com","paul.ca","lord.ca","yvon.com","grammarist.com","oxfordlearner",
    "forosecuador","genealogy.com","go.com","cadillacforums","stlevis.ca",
    "desjardins.ca","wn.com","massawa.com","sept.ca","mathews.ca",
    "ouellet.com","rimar.ca","domainnames.ca","worldnews","actu17",
    # Sites officiels de marques / réseaux mondiaux (pas des dealers locaux)
    "ferraridealers","lamborghini.com","porsche.com","maserati.com",
    "mercedes-benz.com","bmw.com","audi.com","toyota.com","honda.com",
    "ford.com","gm.com","stellantis","hyundai.com","kia.com",
    "nissancanada","toyota.ca","hondacanada",
    # Sites européens / agrégateurs internationaux
    "autoscout24","autoscout","lacentrale","leboncoin","caradisiac",
    "largus","motortrend","automobile-magazine","autoplus","turbo.fr",
    "paruvendu","vivastreet","annoncesauto","spoticar","aramisauto",
    "mandataire","cardoen","autowereld","mobile.de","otomoto",
    "hasznaltauto","bazarauto","olx.","trovit","mitula","nuroa",
}

# TLDs européens/internationaux à rejeter — tous les dealers sont au Québec
SKIP_TLDS = {
    ".fr",".be",".ch",".lu",".de",".at",".nl",".es",".it",".pt",
    ".uk",".co.uk",".ie",".pl",".cz",".sk",".hu",".ro",".se",".no",
    ".dk",".fi",".eu",".ru",".ua",".mx",".ar",".br",".au",".nz",
}

# Mots sur une page qui indiquent domaine parqué / fermé
PARKED_SIGNALS = [
    "domain for sale","buy this domain","this domain is for sale",
    "domaine à vendre","ce domaine est à vendre",
    "coming soon","site en construction","en développement",
    "under construction","page not found","404","domain parking",
    "godaddy","namecheap","sedo.com","hugedomains",
    "this website is for sale","buy now",
]

# Chemins usagés à tenter
USED_PATHS = [
    "/inventaire/search.html?used=1",
    "/vehicules-occasion", "/vehicules-usages", "/usages",
    "/occasion", "/inventaire/usages", "/inventaire/occasion",
    "/inventory/used", "/pre-owned", "/pre-owned-vehicles",
    "/used", "/used-vehicles", "/used-cars",
    "/voitures-occasion", "/search?condition=used",
    "/inventaire", "/inventory",
    "/fr/occasion", "/fr/inventaire", "/fr/vehicules-usages",
    "/fr/vehicules-occasion", "/fr/usages", "/fr/inventaire/usages",
    "/fr/inventaire/occasion", "/fr/voitures-usagees",
    "/en/used", "/en/inventory", "/en/used-vehicles",
    "/en/pre-owned", "/en/pre-owned-vehicles",
]

# ── Utilitaires ────────────────────────────────────────────────────────────────

def norm(s):
    return unicodedata.normalize("NFD", s).encode("ascii","ignore").decode().lower().strip()

def pause():  time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
def quick():  time.sleep(random.uniform(0.6, 1.2))

def get_domain(url):
    return re.sub(r"https?://([^/]+).*", r"\1", url.lower())

# ── Persistance ────────────────────────────────────────────────────────────────

def load_done(output_csv):
    if not os.path.exists(output_csv): return set()
    with open(output_csv, encoding="utf-8") as f:
        return {norm(r["Concessionnaire"]) for r in csv.DictReader(f)}

def load_not_found(output_csv):
    if not os.path.exists(output_csv): return set()
    with open(output_csv, encoding="utf-8") as f:
        return {norm(r["Concessionnaire"]) for r in csv.DictReader(f)
                if r.get("Inventaire_Scraped", "").strip() in ("", "0")
                and r.get("Source","") not in ("FERMÉ","ferme")}

def remove_rows(output_csv, names_norm):
    if not os.path.exists(output_csv): return 0
    with open(output_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    kept = [r for r in rows if norm(r["Concessionnaire"]) not in names_norm]
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept)
    return len(rows) - len(kept)

def save_row(output_csv, row):
    exists = os.path.exists(output_csv)
    with open(output_csv, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        if not exists: w.writeheader()
        w.writerow(row)

# ── Extraction du nombre de véhicules d'occasion ──────────────────────────────

def extract_count(text):
    patterns = [
        r"v[ée]hicules?\s+d.occasion\s*[\n\r]+\s*(\d+)",
        r"d.occasion\s*[\n\r]+\s*(\d+)\b",
        r"(?:inventaire\s+(?:complet|usag[ée]s?|d.occasion)|occasion|usag[ée]s?)\s*\((\d+)\)",
        r"(\d+)\s+v[ée]hicules?\s+d.occasion",
        r"\((\d+)\)\s*(?:v[ée]hicules?|usag[ée]s?|occasion)",
        r"(?:usag[ée]s?|occasion)\s+(\d+)\b(?!\d)",
        r"\b(\d+)\s+usag[ée]s?\b",
        r"(\d+)\s+v[ée]hicules?\s+(?:disponibles?|correspondants?|trouv[ée]s?|en\s+inventaire)",
        r"(\d+)\s+r[ée]sultats?(?:\s+trouv[ée]s?)?",
        r"(?:occasion|usag[ée])\s*\((\d+)\)",
        r"(?:of|de)\s+(\d+)\s+(?:vehicle|v[ée]hicule|result|r[ée]sultat)",
        r"(\d+)\s+(?:pre.?owned|used\s+vehicle)",
        r"inventaire\s*:?\s*(\d+)",
        r"(\d+)\s+annonces?",
        r"showing\s+\d+[–\-]\d+\s+of\s+(\d+)",
        r"\d+\s+[àa]\s+\d+\s+de\s+(\d+)",
        r"total\s*:?\s*(\d+)\s*(?:v[ée]hicule|vehicle|résultat|result)?",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            if n < 1 or (1900 <= n <= 2030) or n > 2000:
                continue
            return n
    return None


def count_vehicle_cards(html):
    soup = BeautifulSoup(html, "html.parser")
    selectors = [
        "[class*='vehicle-card']", "[class*='inventory-item']",
        "[class*='listing-item']", "[class*='car-tile']",
        "[class*='vehicle-tile']", "[class*='srp-item']",
        "[data-vehicle]", "[data-listing-id]",
        "[class*='VehicleCard']", "[class*='InventoryItem']",
        "[class*='vehicle-listing']", "[class*='car-card']",
        "[class*='vehicle-result']", "[class*='result-item']",
        "[class*='item-vehicle']", "[class*='auto-card']",
        "[data-vin]", "[data-stock]", "[data-vehicle-id]",
        "article[class*='vehicle']", "article[class*='car']",
        "li[class*='vehicle']", "li[class*='inventory']",
    ]
    best = 0
    for sel in selectors:
        try:
            n = len(soup.select(sel))
            if n > best: best = n
        except Exception:
            pass
    return best if 1 <= best <= 500 else None


# ── Validation du contenu auto ─────────────────────────────────────────────────

def is_auto_content(text, min_hits=2):
    text_l = text.lower()
    hits = sum(1 for w in AUTO_CONTENT_WORDS if w in text_l)
    return hits >= min_hits

def is_parked_or_closed(text):
    text_l = text.lower()
    return any(s in text_l for s in PARKED_SIGNALS)


# ── Recherche d'URL ────────────────────────────────────────────────────────────

# Mots trop génériques pour identifier un dealer spécifique
_GENERIC_WORDS = {
    "auto","autos","automobile","automobiles","groupe","group","direct",
    "selection","inc","ltee","enr","le","la","les","de","du","des","et",
    "the","concessionnaire","dealer","2000","2006","2010","2015","canada",
    "quebec","qc","saint","sainte","ste","st","nord","sud","est","ouest",
}

def url_relevance_score(url, dealer_name, city):
    score = 0
    url_l = url.lower()
    domain = get_domain(url)

    if any(s in domain for s in SKIP_DOMAINS): return -99
    if any(domain.endswith(tld) for tld in SKIP_TLDS): return -99
    gov_signals = ["portail", ".gouv.", ".gc.ca", ".ville.", "ville-", "mairie", "municipal",
                   "forum","dictio","grammar","genealogy","worldnews","actualite"]
    if any(s in url_l for s in gov_signals): return -99

    city_norm    = norm(city)
    dealer_words = [w for w in norm(dealer_name).split() if len(w) > 2]
    city_words   = [w for w in city_norm.split() if len(w) > 2]

    # Mots UNIQUES au dealer (ni ville, ni génériques) — doivent apparaître dans le domaine
    unique_words = [w for w in dealer_words
                    if w not in city_words and w not in _GENERIC_WORDS]

    brand_found = any(brand in domain for brand in AUTO_BRANDS)
    if brand_found: score += 4

    if unique_words:
        matches = sum(1 for w in unique_words if w in domain)
        if matches == 0:
            # Rejet absolu — le domaine ne contient aucun mot distinctif du nom du dealer
            return -99
        score += matches * 3
    else:
        # Nom 100% générique — exiger au moins un mot du nom dans le domaine
        generic_match = sum(1 for w in dealer_words if w in domain)
        if generic_match == 0:
            return -99
        score += generic_match

    if any(w in domain for w in city_words): score += 1
    for kw in ["auto","autos","cars","vehicule","concessionnaire","dealer","groupe","moto"]:
        if kw in domain: score += 1
    if domain.endswith(".ca"): score += 1

    return score


def find_url_ddg(name, city):
    if not HAS_DDG: return None

    # Requêtes avec nom + ville pour maximiser la précision
    queries = [
        f'"{name}" "{city}"',
        f'"{name}" {city} concessionnaire usagés',
        f'{name} {city} véhicules occasion',
    ]

    best_score = 0
    best_url = None

    for query in queries:
        try:
            results = DDGS().text(query, max_results=10, region="ca-fr")
            for r in results:
                url = r.get("href", "")
                if not url.startswith("http"): continue
                score = url_relevance_score(url, name, city)
                if score > best_score:
                    best_score = score
                    best_url = url
            if best_url and best_score >= 4:
                break
        except Exception as e:
            short = str(e)[:60]
            if "protocol" not in short.lower() and "dns" not in short.lower():
                print(f"    ⚠ DDG: {short}")
            time.sleep(1)
            continue

    return best_url if best_score >= 4 else None


def guess_urls(name, city):
    stop = {"inc","ltee","auto","autos","automobile","automobiles",
            "le","la","les","de","du","des","et","the","groupe","group",
            "concessionnaire","dealer","2000","2006","2010","2015"}
    words_full = norm(name).replace("'","").replace("-"," ").split()
    words = [w for w in words_full if w not in stop and len(w) > 1]
    if not words: words = words_full[:2]

    city_n    = norm(city).replace(" ","").replace("-","")
    city_slug = norm(city).replace(" ","-")

    slugs = set()
    for n_words in range(1, min(4, len(words)+1)):
        combo = words[:n_words]
        slugs.add("".join(combo))
        slugs.add("-".join(combo))
    if len(words) >= 2:
        slugs.add(words[0] + words[-1])
        slugs.add(words[0] + "-" + words[-1])
    slugs = sorted(slugs, key=len, reverse=True)

    candidates = []
    for slug in slugs:
        if slug in (city_n, city_slug, norm(city).replace(" ","")):
            continue
        for tld in [".ca", ".com"]:
            candidates += [
                f"https://www.{slug}{tld}",
                f"https://{slug}{tld}",
                f"https://www.{city_n}{slug}{tld}",
                f"https://www.{slug}{city_n}{tld}",
                f"https://www.groupe{slug}{tld}",
            ]
    seen = set(); out = []
    for u in candidates:
        if u not in seen: seen.add(u); out.append(u)
    return out


def try_url(url):
    try:
        r = requests.head(url, headers=HEADERS_HTTP, timeout=8, allow_redirects=True)
        return r.status_code < 400
    except Exception:
        return False


def validate_url_content(url):
    domain = get_domain(url)
    if any(domain.endswith(tld) for tld in SKIP_TLDS): return 'not_auto'
    if any(s in domain for s in SKIP_DOMAINS): return 'not_auto'

    if any(brand in domain for brand in AUTO_BRANDS):
        try:
            r = requests.get(url, headers=HEADERS_HTTP, timeout=10,
                             allow_redirects=True, verify=False)
            if r.status_code in (404, 410): return 'closed'
            if r.status_code >= 400: return 'error'
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script","style","noscript"]): tag.decompose()
            text = soup.get_text(" ", strip=True)
            if is_parked_or_closed(text): return 'parked'
            if any(s in domain for s in ["forums","forum","wiki","news","blog","parts"]): return 'not_auto'
            return 'ok'
        except Exception:
            return 'error'

    try:
        r = requests.get(url, headers=HEADERS_HTTP, timeout=12,
                         allow_redirects=True, verify=False)
        if r.status_code >= 400:
            return 'closed' if r.status_code in (404, 410) else 'error'
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script","style","noscript"]): tag.decompose()
        text = soup.get_text(" ", strip=True)

        if is_parked_or_closed(text):
            return 'parked'
        if is_auto_content(text):
            return 'ok'
        auto_domain_kw = ["auto","moto","car","veh","voiture","camion","dealer","garage"]
        if any(kw in domain for kw in auto_domain_kw):
            return 'ok'
        return 'not_auto'
    except Exception:
        return 'error'


# ── Scraping ───────────────────────────────────────────────────────────────────

def find_used_link(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    best = None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True).lower()
        if re.search(r"occasion|usag[ée]|pre.?owned|used", href + " " + text, re.I):
            if re.search(r"neuf|new|demo|demonst|specials?|incentive", href + " " + text, re.I):
                continue
            if href.startswith("http"):
                best = href; break
            elif href.startswith("/"):
                best = base_url.rstrip("/") + href; break
    return best


def _scrape_page(html, raw_html, page_url, label_suffix=""):
    """
    Tente d'extraire le compte de véhicules usagés d'une page.
    Priorité: 1) cartes HTML  2) regex texte (restreint)
    Retourne (count, source_label, page_url) ou (None, ..., page_url)
    """
    # 1. Compter les cartes de véhicules — méthode la plus fiable
    n = count_vehicle_cards(raw_html)
    if n:
        return n, f"cards{label_suffix}", page_url

    # 2. Regex sur le texte — patterns très ciblés seulement
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script","style","noscript","header","footer","nav"]): tag.decompose()
    text = soup.get_text(" \n", strip=True)
    count = extract_count(text)
    if count:
        return count, f"static{label_suffix}", page_url

    return None, f"not_found{label_suffix}", page_url


def scrape_static(url):
    """Retourne (count, source_label, page_url)."""
    try:
        r = requests.get(url, headers=HEADERS_HTTP, timeout=15,
                         allow_redirects=True, verify=False)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}", url

        # Essayer d'abord le lien usagés dans le HTML
        used_link = find_used_link(r.text, url)
        if used_link and used_link != url:
            try:
                quick()
                r2 = requests.get(used_link, headers=HEADERS_HTTP, timeout=12,
                                  allow_redirects=True, verify=False)
                if r2.status_code == 200:
                    count, src, pu = _scrape_page(None, r2.text, used_link, "_used_link")
                    if count: return count, src, pu
            except Exception:
                pass

        # Probing des chemins usagés courants
        base = url.rstrip("/")
        for path in USED_PATHS:
            try:
                quick()
                probe_url = base + path
                r2 = requests.get(probe_url, headers=HEADERS_HTTP, timeout=10,
                                  allow_redirects=True, verify=False)
                if r2.status_code == 200:
                    count, src, pu = _scrape_page(None, r2.text, probe_url, path)
                    if count: return count, src, pu
            except Exception:
                pass

        # Fallback: page d'accueil
        count, src, pu = _scrape_page(None, r.text, url, "")
        return count, src, pu

    except Exception as e:
        return None, str(e)[:60], url


def scrape_playwright(url):
    """Retourne (count, source_label, page_url)."""
    if not HAS_PLAYWRIGHT: return None, "playwright_unavailable", url
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            page = browser.new_page(
                user_agent=HEADERS_HTTP["User-Agent"],
                extra_http_headers={
                    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )

            used_url = url
            try:
                r_static = requests.get(url, headers=HEADERS_HTTP, timeout=10, verify=False)
                link = find_used_link(r_static.text, url)
                if link: used_url = link
            except Exception:
                pass

            try:
                page.goto(used_url, wait_until="networkidle", timeout=30000)
            except Exception:
                page.goto(used_url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(3.0)

            final_url = page.url  # URL réelle après navigation

            if used_url == url:
                for selector in [
                    'a:text-matches("usagé", "i")',
                    "a:text-matches(\"d'occasion\", \"i\")",
                    'a:text-matches("occasion", "i")',
                    'a:text-matches("used", "i")',
                    'a:text-matches("pre-owned", "i")',
                ]:
                    try:
                        link = page.locator(selector).first
                        if link.count() > 0:
                            href = link.get_attribute("href")
                            if href and not re.search(r"neuf|new|demo", href, re.I):
                                target = href if href.startswith("http") else url.rstrip("/")+href
                                try:
                                    page.goto(target, wait_until="networkidle", timeout=20000)
                                except Exception:
                                    page.goto(target, wait_until="domcontentloaded", timeout=20000)
                                time.sleep(2.5)
                                final_url = page.url
                                break
                    except Exception:
                        pass

            text = page.inner_text("body")
            html = page.content()
            browser.close()

        # Vérifier que l'URL finale est bien une page d'inventaire usagé
        # Si on est toujours sur la homepage, les cartes comptées incluent neufs + usagés
        _used_url_kw = re.compile(
            r"occasion|usag[ée]|pre.?owned|used|inventaire|inventory", re.I
        )
        on_used_page = bool(_used_url_kw.search(final_url))

        count = extract_count(text)
        if count: return count, "website_js", final_url

        n = count_vehicle_cards(html)
        if n:
            # Rejeter si on n'est PAS sur une page usagés — trop de faux positifs
            if not on_used_page:
                return None, "js_cards_homepage_skip", final_url
            return n, "website_js_cards", final_url

        return None, "not_found_js", url
    except Exception as e:
        return None, f"pw_err:{str(e)[:40]}", url


# ── autousagee.ca fallback ─────────────────────────────────────────────────────

AUTOUSAGEE_INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autousagee_index.json")
_autousagee_index = None

def _load_autousagee_index():
    global _autousagee_index
    if _autousagee_index is None:
        if os.path.exists(AUTOUSAGEE_INDEX_PATH):
            with open(AUTOUSAGEE_INDEX_PATH, encoding="utf-8") as f:
                _autousagee_index = json.load(f)
        else:
            _autousagee_index = {}
    return _autousagee_index


def _dealer_to_slug(s):
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _find_autousagee_profile(name):
    index = _load_autousagee_index()
    if not index:
        return None
    s = _dealer_to_slug(name)
    if s in index:
        return index[s]["path"]
    for suf in ["-ltee", "-inc", "-incorporee", "-incorporated", "-enr", "-limitee",
                "-2000", "-2006", "-2010", "-2015", "-1998", "-1999"]:
        s2 = s.replace(suf, "").strip("-")
        if s2 and s2 in index:
            return index[s2]["path"]
    _GENERIC = {"auto", "autos", "automobile", "automobiles", "groupe", "group",
                "ltee", "inc", "enrg", "limitee", "canada", "quebec", "automobil"}
    words = [w for w in s.split("-") if len(w) >= 4 and w not in _GENERIC]
    if len(words) >= 1:
        for k, v in index.items():
            if all(w in k for w in words):
                return v["path"]
    return None


def scrape_autousagee(name, city):
    profile_url = _find_autousagee_profile(name)
    if not profile_url:
        return None, ""
    try:
        r = requests.get(profile_url, headers=HEADERS_HTTP, timeout=12, allow_redirects=True)
        if r.status_code != 200:
            return None, ""
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style"]): tag.decompose()
        text = soup.get_text(" \n", strip=True)
        m = re.search(r"(\d+)\s+r[ée]sultats?", text, re.I)
        if not m:
            m = re.search(r"voiture[^:]*:\s*(\d+)", text, re.I)
        if not m:
            m = re.search(r"(\d+)\s+v[ée]hicules?", text, re.I)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 2000:
                print(f"    autousagee.ca profil: {n} véhicules ({profile_url})")
                return n, "autousagee.ca"
    except Exception as e:
        print(f"    autousagee.ca erreur: {e}")
    return None, ""


# ── Traitement d'un dealer ─────────────────────────────────────────────────────

def process(dealer, output_csv):
    name = dealer["Concessionnaire"].strip()
    city = dealer["Ville"].strip()

    row = {
        "Concessionnaire":    name,
        "Ville":              city,
        "Rep":                dealer.get("Rep",""),
        "Type":               dealer.get("Type",""),
        "Inventaire_CRM":     dealer.get("Inventaire",""),
        "URL_trouvee":        "",
        "Inventaire_Scraped": "",
        "Source":             "non_trouve",
        "Date_Scrape":        datetime.now().strftime("%Y-%m-%d"),
        "Notes":              "",
    }

    url = dealer.get("URL","").strip()

    if not url:
        print(f"    Recherche: \"{name}\" + \"{city}\"")
        url = find_url_ddg(name, city)
        if url:
            print(f"    URL DDG: {url}")
        else:
            # Tenter des URLs construites à partir du nom — seulement si elles matchent le nom
            for guess in guess_urls(name, city):
                domain = get_domain(guess)
                score = url_relevance_score(guess, name, city)
                if score < 0:
                    continue  # Rejet — domaine ne contient pas le nom
                if try_url(guess):
                    url = guess
                    print(f"    URL devinée: {url}")
                    break
            if not url:
                print("    URL non trouvée")

    if url:
        domain = get_domain(url)
        if any(s in domain for s in SKIP_DOMAINS):
            print(f"    URL rejetée (domaine non-auto): {url}")
            url = None
        else:
            status = validate_url_content(url)
            if status == 'parked':
                print(f"    Site parqué/fermé: {url}")
                row["URL_trouvee"] = url
                row["Source"] = "FERMÉ"
                row["Notes"] = "site_parque"
                return row
            elif status == 'closed':
                print(f"    Site fermé (404): {url}")
                row["URL_trouvee"] = url
                row["Source"] = "FERMÉ"
                row["Notes"] = "HTTP 404"
                return row
            elif status == 'not_auto':
                print(f"    URL non-auto: {url}")
                better = find_url_ddg(name, city)
                if better and better != url:
                    status2 = validate_url_content(better)
                    if status2 == 'ok':
                        print(f"    Meilleure URL: {better}")
                        url = better
                    else:
                        url = None
                else:
                    url = None

    row["URL_trouvee"] = url or ""

    if url:
        pause()
        print("    Scraping statique...")
        count, src, page_url = scrape_static(url)

        if count is None:
            print(f"    Playwright ({src})...")
            pause()
            count, src, page_url = scrape_playwright(url)

        if count is not None:
            # Sanity check — aucun dealer solo QC n'a plus de 300 usagés
            if count > 300:
                print(f"    ⚠ {count} rejeté (trop élevé — probablement faux positif)")
                count = None
            else:
                print(f"    ✓ {count} véhicules ({src}) → {page_url}")
                row["Inventaire_Scraped"] = count
                row["Source"] = src
                row["URL_trouvee"] = page_url
                return row

        row["Notes"] = f"site:{src}"
    else:
        row["Notes"] = "url_introuvable"

    print("    Tentative autousagee.ca...")
    count, src = scrape_autousagee(name, city)
    if count is not None:
        print(f"    ✓ {count} véhicules ({src})")
        row["Inventaire_Scraped"] = count
        row["Source"] = src
        return row

    print("    ✗ Non trouvé")
    return row


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: python scraper.py <input.csv> <output.csv> [retry|test [n]]")
        sys.exit(1)

    input_csv  = sys.argv[1]
    output_csv = sys.argv[2]
    mode       = sys.argv[3] if len(sys.argv) > 3 else "run"
    n_test     = int(sys.argv[4]) if len(sys.argv) > 4 else 5

    retry_mode = mode == "retry"
    test_mode  = mode == "test"

    # Créer le dossier de sortie si nécessaire
    out_dir = os.path.dirname(output_csv)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    print("=" * 65)
    print("SCRAPER INVENTAIRE CONCESSIONNAIRES QC v4")
    print(f"Input:  {input_csv}")
    print(f"Output: {output_csv}")
    if test_mode:  print(f"MODE TEST — {n_test} premiers dealers")
    if retry_mode: print("MODE RETRY — relance uniquement les non-trouvés")
    print("=" * 65)

    with open(input_csv, encoding="utf-8") as f:
        dealers = list(csv.DictReader(f))

    if test_mode:
        dealers = dealers[:n_test]

    if retry_mode:
        not_found_names = load_not_found(output_csv)
        removed = remove_rows(output_csv, not_found_names)
        print(f"Retiré {removed} entrées non-trouvées du CSV pour retry")
        done = load_done(output_csv)
        todo = [d for d in dealers if norm(d["Concessionnaire"]) not in done]
        print(f"Total: {len(dealers)} | Déjà trouvés/fermés: {len(done)} | À retenter: {len(todo)}\n")
    else:
        done = load_done(output_csv)
        todo = [d for d in dealers if norm(d["Concessionnaire"]) not in done]
        print(f"Total: {len(dealers)} | Déjà traités: {len(done)} | À faire: {len(todo)}\n")

    errors = 0
    for d in todo:
        num = dealers.index(d) + 1
        print(f"[{num}/{len(dealers)}] {d['Concessionnaire']} | {d['Ville']}")
        try:
            result = process(d, output_csv)
            save_row(output_csv, result)
            inv = result["Inventaire_Scraped"]
            src = result["Source"]
            print(f"  → {inv if inv != '' else 'non trouvé'} ({src})")
        except KeyboardInterrupt:
            print(f"\n\nInterrompu — reprendre avec: python3 scraper.py {input_csv} {output_csv} retry")
            sys.exit(0)
        except Exception as e:
            print(f"  ⚠ Erreur: {e}")
            errors += 1
            save_row(output_csv, {
                "Concessionnaire": d["Concessionnaire"], "Ville": d.get("Ville",""),
                "Rep": d.get("Rep",""), "Type": d.get("Type",""),
                "Inventaire_CRM": d.get("Inventaire",""), "URL_trouvee": "",
                "Inventaire_Scraped": "", "Source": "erreur",
                "Date_Scrape": datetime.now().strftime("%Y-%m-%d"),
                "Notes": str(e)[:100],
            })

    print()
    print("=" * 65)
    print(f"TERMINÉ — {len(todo)-errors} OK | {errors} erreurs")
    print(f"Résultats: {output_csv}")


if __name__ == "__main__":
    main()
