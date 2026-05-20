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

def extract_count(text, require_used_context=False):
    """
    Extrait le nombre de véhicules d'occasion du texte.
    Si require_used_context=True, n'accepte que les patterns qui mentionnent
    explicitement occasion/usagé/used/pre-owned.
    """
    # Patterns avec contexte usagé explicite — priorité maximale
    used_patterns = [
        r"v[ée]hicules?\s+d.occasion\s*[\n\r]+\s*(\d+)",
        r"d.occasion\s*[\n\r]+\s*(\d+)\b",
        r"(?:inventaire\s+(?:complet|usag[ée]s?|d.occasion)|occasion|usag[ée]s?)\s*\((\d+)\)",
        r"(\d+)\s+v[ée]hicules?\s+d.occasion",
        r"\((\d+)\)\s*(?:v[ée]hicules?|usag[ée]s?|occasion)",
        r"(?:usag[ée]s?|occasion)\s+(\d+)\b(?!\d)",
        r"\b(\d+)\s+usag[ée]s?\b",
        r"(\d+)\s+(?:pre.?owned|used\s+vehicle)",
        r"(?:occasion|usag[ée])\s*\((\d+)\)",
    ]
    # Patterns génériques — utilisés seulement si require_used_context=False
    generic_patterns = [
        # Patterns SRP très courants sur les plateformes dealer canadiennes
        r"(\d+)\s+[ée]l[ée]ments?\s+correspondants?",   # "58 éléments correspondants" (FR)
        r"(\d+)\s+[Ii]tems?\s+[Mm]atching",              # "58 Items Matching" (EN)
        r"(\d+)\s+[Mm]atching\s+[Ii]tems?",
        r"(\d+)\s+[Rr]esults?\s+[Ff]ound",
        r"(\d+)\s+[Vv]ehicles?\s+[Ff]ound",
        r"[Ff]ound\s+(\d+)\s+[Vv]ehicles?",
        r"(\d+)\s+v[ée]hicules?\s+(?:disponibles?|correspondants?|trouv[ée]s?|en\s+inventaire)",
        r"(\d+)\s+r[ée]sultats?(?:\s+trouv[ée]s?)?",
        r"(?:of|de)\s+(\d+)\s+(?:vehicle|v[ée]hicule|result|r[ée]sultat)",
        r"inventaire\s*:?\s*(\d+)",
        r"(\d+)\s+annonces?",
        r"showing\s+\d+[–\-]\d+\s+of\s+(\d+)",
        r"\d+\s+[àa]\s+\d+\s+de\s+(\d+)",
        r"total\s*:?\s*(\d+)\s*(?:v[ée]hicule|vehicle|résultat|result)?",
    ]
    patterns = used_patterns if require_used_context else used_patterns + generic_patterns
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            if n < 1 or (1900 <= n <= 2030) or n > 2000:
                continue
            return n
    return None


def _extract_count_from_json(data, depth=0):
    """Cherche récursivement un compte de véhicules dans un objet JSON."""
    if depth > 5:
        return None
    if isinstance(data, dict):
        # Clés communes pour le total de résultats
        for key in ["total", "totalCount", "count", "total_count", "nbResults",
                    "nb_results", "totalResults", "total_results", "totalVehicles",
                    "vehicleCount", "numFound", "hits", "size", "totalHits",
                    "Total", "Count", "ResultCount", "ItemCount"]:
            if key in data:
                v = data[key]
                if isinstance(v, (int, float)) and 1 <= int(v) <= 1000:
                    return int(v)
        # Chercher dans les valeurs
        for k, v in data.items():
            # Priorité aux clés liées aux véhicules
            if any(kw in k.lower() for kw in ["vehicle", "inventory", "result", "listing", "item"]):
                r = _extract_count_from_json(v, depth + 1)
                if r:
                    return r
        for v in data.values():
            r = _extract_count_from_json(v, depth + 1)
            if r:
                return r
    elif isinstance(data, list):
        # Une liste de véhicules — sa longueur est le compte
        if len(data) >= 1 and isinstance(data[0], dict):
            # Vérifier que les éléments ressemblent à des véhicules
            first = data[0]
            vehicle_keys = {"vin", "make", "model", "year", "price", "mileage",
                           "marque", "modele", "annee", "kilometrage", "id", "vehicleId",
                           "stockNumber", "condition", "type"}
            if vehicle_keys & set(first.keys()):
                return len(data)
    return None


def _extract_count_from_scripts(html):
    """Parse les balises <script> pour trouver des données JSON d'inventaire.
    Seulement pour des scripts qui contiennent des mots-clés liés à l'inventaire.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Mots-clés requis dans le script pour qu'il soit considéré comme inventaire
    _INVENTORY_KW = re.compile(
        r"vehicle|inventory|occasion|used|listing|catalogue|inventaire|"
        r"vin|stockNumber|mileage|kilometrage|make|model",
        re.IGNORECASE
    )
    # Patterns de variables JS qui contiennent l'inventaire
    js_struct_patterns = [
        r"window\.__(?:STORE|STATE|DATA|INITIAL_STATE|NEXT_DATA|APP_STATE)__\s*=\s*(\{.{200,}\});",
        r"window\.(?:inventory|vehicles|listings|results)\s*=\s*(\[.{50,}\]);",
        r"var\s+(?:inventory|vehicles|initialState|appData)\s*=\s*(\{.{100,}\});",
    ]
    # Patterns directs — exigent un contexte inventaire dans le même script
    count_patterns = [
        (r'"totalCount"\s*:\s*(\d+)', 2),   # (pattern, min_count)
        (r'"totalResults"\s*:\s*(\d+)', 2),
        (r'"numFound"\s*:\s*(\d+)', 2),
        (r'"ResultCount"\s*:\s*(\d+)', 2),
        (r'"total"\s*:\s*(\d+)', 3),         # "total" seul — min 3 pour éviter faux positifs
        (r'"count"\s*:\s*(\d+)', 3),
    ]
    for script in soup.find_all("script"):
        content = script.string or ""
        if not content or len(content) < 100:
            continue
        # Le script doit parler de véhicules/inventaire
        if not _INVENTORY_KW.search(content):
            continue
        # Essayer les patterns de comptage direct
        for pat, min_count in count_patterns:
            for m in re.finditer(pat, content, re.IGNORECASE):
                try:
                    n = int(m.group(1))
                    if n >= min_count and not (1900 <= n <= 2030) and n <= 1000:
                        return n
                except (ValueError, IndexError):
                    pass
        # Essayer de parser du JSON structuré
        for pat in js_struct_patterns:
            m = re.search(pat, content, re.IGNORECASE | re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    count = _extract_count_from_json(data)
                    if count and count >= 2:
                        return count
                except (json.JSONDecodeError, IndexError):
                    pass
    return None


def count_vehicle_cards(html, used_only=False):
    """
    Compte les cartes de véhicules dans le HTML.
    Si used_only=True, ne compte que les cartes avec indicateur d'occasion/usagé.
    """
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
            cards = soup.select(sel)
            n = len(cards)
            if used_only and n > 0:
                # Ne compter que les cartes qui mentionnent usagé/occasion/used
                used_cards = [
                    c for c in cards
                    if re.search(r"usag[ée]|occasion|pre.?owned|used", c.get_text(), re.I)
                    or re.search(r"usag[ée]|occasion|used", str(c.get("class","")) + str(c.get("data-condition","")), re.I)
                ]
                n = len(used_cards)
            if n > best:
                best = n
        except Exception:
            pass
    return best if 1 <= best <= 500 else None


def _verify_used_page_content(text, html):
    """
    Vérifie que le contenu de la page correspond vraiment à des véhicules d'occasion
    (pas tous les véhicules neuf+usagé confondus).
    Retourne un score: >0 = page usagés probable, <0 = suspicion de page globale.
    """
    score = 0
    text_l = text.lower()

    # Signaux positifs — page clairement filtrée sur les usagés
    used_signals = [
        r"v[ée]hicules?\s+d.occasion", r"inventaire\s+d.occasion",
        r"v[ée]hicules?\s+usag[ée]s?", r"inventaire\s+usag[ée]",
        r"pre.?owned\s+vehicles?", r"used\s+vehicles?\s+inventory",
        r"used\s+cars?\s+for\s+sale",
    ]
    for pat in used_signals:
        if re.search(pat, text_l):
            score += 2

    # Signaux négatifs — page qui montre tout (neuf + usagé)
    mixed_signals = [
        r"v[ée]hicules?\s+neufs?", r"neuf\s+et\s+(?:usag[ée]|occasion)",
        r"tous\s+les\s+v[ée]hicules?", r"inventaire\s+complet",
        r"new\s+and\s+used", r"new\s+vehicles?\s+inventory",
    ]
    for pat in mixed_signals:
        if re.search(pat, text_l):
            score -= 1

    return score


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
    Tente d'extraire le compte de véhicules usagés d'une page statique.
    Priorité: 1) JSON embarqué  2) regex texte contexte usagé  3) cartes HTML
    Retourne (count, source_label, page_url) ou (None, ..., page_url)
    """
    is_used_url = bool(re.search(r"occasion|usag[ée]|pre.?owned|used|inventaire|inventory",
                                  page_url, re.I))

    # 1. JSON embarqué dans <script> — très fiable
    script_count = _extract_count_from_scripts(raw_html)
    if script_count and is_used_url:
        return script_count, f"script_json{label_suffix}", page_url

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script","style","noscript","header","footer","nav"]): tag.decompose()
    text = soup.get_text(" \n", strip=True)

    # 2. Regex texte avec contexte usagé explicite
    count = extract_count(text, require_used_context=True)
    if count:
        return count, f"static_used{label_suffix}", page_url

    # 3. Cartes HTML — seulement si URL est une page usagés
    if is_used_url:
        n = count_vehicle_cards(raw_html, used_only=False)
        if n and n <= 150:
            return n, f"cards{label_suffix}", page_url

    # 4. Regex générique — seulement si URL est clairement une page usagés
    if is_used_url:
        count = extract_count(text, require_used_context=False)
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


def _sum_body_type_counts(text):
    """
    Somme les comptes de types de carrosserie dans le sidebar du SRP.
    Ex: "Convertible (1) Minivan (6) SUV (38)" → 58
    Utile quand le total n'est pas affiché explicitement.
    """
    body_types = re.findall(
        r'(?:Convertible|Minivan|Sedan|SUV|Truck|Coupe|Hatchback|Wagon|Other|'
        r'Crossover|Pickup|Cabriolet|Camion|Familiale|Berline|Utilitaire|Fourgonnette)'
        r'\s*\((\d+)\)',
        text, re.IGNORECASE
    )
    if len(body_types) >= 2:  # Au moins 2 types pour éviter les faux positifs
        total = sum(int(n) for n in body_types)
        if 2 <= total <= 500:
            return total
    return None


def scrape_playwright(url):
    """Retourne (count, source_label, page_url)."""
    if not HAS_PLAYWRIGHT: return None, "playwright_unavailable", url

    intercepted_counts = []  # (count, api_url) capturés depuis les réponses réseau

    def _handle_response(response):
        try:
            if response.status != 200:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            resp_url = response.url.lower()
            # Ne s'intéresser qu'aux URLs qui ressemblent à de l'inventaire
            if not any(k in resp_url for k in [
                "vehicle", "inventory", "occasion", "used", "search",
                "listing", "stock", "catalogue", "inventaire", "vehicul"
            ]):
                return
            data = response.json()
            count = _extract_count_from_json(data)
            if count and 1 <= count <= 1000:
                intercepted_counts.append((count, response.url))
        except Exception:
            pass

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
            page.on("response", _handle_response)

            # Trouver l'URL de la page usagés via scraping statique d'abord
            used_url = url
            try:
                r_static = requests.get(url, headers=HEADERS_HTTP, timeout=10, verify=False)
                link = find_used_link(r_static.text, url)
                if link:
                    used_url = link
            except Exception:
                pass

            # Si pas trouvé statiquement, essayer les chemins courants
            if used_url == url:
                base = url.rstrip("/")
                # Tenter les chemins les plus spécifiques en premier
                priority_paths = [
                    "/fr/vehicules-usages", "/fr/vehicules-occasion", "/fr/occasion",
                    "/fr/inventaire/usages", "/vehicules-occasion", "/vehicules-usages",
                    "/occasion", "/inventory/used", "/pre-owned", "/used-vehicles",
                    "/used", "/pre-owned-vehicles",
                ]
                for path in priority_paths:
                    try:
                        probe = base + path
                        r2 = requests.head(probe, headers=HEADERS_HTTP, timeout=6,
                                           allow_redirects=True, verify=False)
                        if r2.status_code == 200:
                            used_url = probe
                            break
                    except Exception:
                        pass

            try:
                page.goto(used_url, wait_until="networkidle", timeout=35000)
            except Exception:
                try:
                    page.goto(used_url, wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    pass
            time.sleep(3.5)

            final_url = page.url

            # Si on est encore sur la homepage, chercher et cliquer sur le lien usagés
            _used_url_kw = re.compile(
                r"occasion|usag[ée]|pre.?owned|used|inventaire|inventory", re.I
            )
            if not _used_url_kw.search(final_url):
                for selector in [
                    'a:text-matches("usag", "i")',
                    "a:text-matches(\"occasion\", \"i\")",
                    'a:text-matches("used", "i")',
                    'a:text-matches("pre-owned", "i")',
                    'a:text-matches("pre owned", "i")',
                ]:
                    try:
                        links = page.locator(selector).all()
                        for lnk in links:
                            href = lnk.get_attribute("href") or ""
                            if re.search(r"neuf|new|demo|command|order|specials?", href, re.I):
                                continue
                            target = href if href.startswith("http") else url.rstrip("/") + href
                            if not target:
                                continue
                            intercepted_counts.clear()  # reset pour la nouvelle navigation
                            try:
                                page.goto(target, wait_until="networkidle", timeout=25000)
                            except Exception:
                                try:
                                    page.goto(target, wait_until="domcontentloaded", timeout=20000)
                                except Exception:
                                    pass
                            time.sleep(2.5)
                            final_url = page.url
                            if _used_url_kw.search(final_url):
                                break
                        if _used_url_kw.search(final_url):
                            break
                    except Exception:
                        pass

            page_text = page.inner_text("body")
            page_html = page.content()
            browser.close()

        on_used_page = bool(_used_url_kw.search(final_url))
        content_score = _verify_used_page_content(page_text, page_html)

        # ── Priorité 1: Réponses API interceptées (si page usagés) ───────────────
        api_count = None
        if intercepted_counts and (on_used_page or content_score > 0):
            intercepted_counts.sort(key=lambda x: x[0])
            api_count, best_api_url = intercepted_counts[0]
            print(f"    [API] Compte intercepté: {api_count} depuis {best_api_url[:80]}")

        # ── Priorité 2: Regex sur le texte — contexte usagé explicite ────────────
        count = extract_count(page_text, require_used_context=True)
        if count:
            return count, "website_js_text", final_url

        # ── Priorité 3: Regex générique (Items Matching, éléments correspondants…)
        #    Si on est sur une page inventaire, le SRP count est fiable
        if on_used_page or content_score > 0:
            count = extract_count(page_text, require_used_context=False)
            if count:
                return count, "website_js", final_url

        # ── Priorité 4: Somme des body types dans le sidebar ─────────────────────
        body_type_total = _sum_body_type_counts(page_text)
        if body_type_total and (on_used_page or content_score > 0):
            print(f"    [BodyTypes] Somme: {body_type_total}")
            return body_type_total, "website_js_body_sum", final_url

        # ── Priorité 5: JSON embarqué dans les balises <script> ──────────────────
        script_count = _extract_count_from_scripts(page_html)
        if script_count and (on_used_page or content_score > 0):
            print(f"    [Script JSON] Compte trouvé: {script_count}")
            # Cross-valider avec API si disponible
            if api_count and abs(script_count - api_count) / max(api_count, 1) > 0.5:
                print(f"    [CV] Script={script_count} vs API={api_count} — préfère API")
                return api_count, "api_intercepted", final_url
            return script_count, "script_json", final_url

        # ── Priorité 6: Retourner API intercepté si disponible ───────────────────
        if api_count:
            return api_count, "api_intercepted", final_url

        # ── Priorité 7: Cartes HTML — seulement si page usagés confirmée ─────────
        if on_used_page and content_score >= 0:
            n = count_vehicle_cards(page_html, used_only=True)
            if n:
                return n, "website_js_used_cards", final_url
            if content_score > 0:
                n = count_vehicle_cards(page_html, used_only=False)
                if n and n <= 150:
                    return n, "website_js_cards", final_url
                if n:
                    print(f"    ⚠ {n} cartes — trop élevé, suspect")

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
