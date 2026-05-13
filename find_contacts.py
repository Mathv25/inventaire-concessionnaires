#!/usr/bin/env python3
"""
Cherche GM / Directeur général / Used Cars Manager sur les sites des concessionnaires.
Usage: python find_contacts.py [output_contacts.csv]
"""

import csv, re, sys, time, random, unicodedata
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
}

# Pages à chercher pour l'équipe
TEAM_PATHS = [
    "/equipe", "/notre-equipe", "/team", "/our-team",
    "/a-propos", "/about", "/about-us", "/a-propos-de-nous",
    "/contact", "/nous-joindre", "/contactez-nous",
    "/fr/equipe", "/fr/notre-equipe", "/fr/a-propos",
    "/fr/contact", "/fr/nous-joindre",
    "/en/team", "/en/about", "/en/contact",
    "/direction", "/management",
]

# Titres qui nous intéressent (GM / Directeur général / Used Cars)
TITLE_PATTERNS = [
    r"g[ée]n[ée]ral\s+manager",
    r"directeur\s+g[ée]n[ée]ral",
    r"director\s+general",
    r"used\s+(?:cars?|vehicle|inventory)\s+(?:manager|director|director)",
    r"directeur\s+(?:des\s+)?(?:v[ée]hicules?\s+)?(?:d.occasion|usag[ée]s?)",
    r"gestionnaire\s+(?:des\s+)?(?:v[ée]hicules?\s+)?(?:d.occasion|usag[ée]s?)",
    r"manager[,\s]+used",
    r"responsable\s+(?:des\s+)?(?:v[ée]hicules?\s+)?(?:d.occasion|usag[ée]s?)",
    r"chef\s+des\s+v[ée]hicules?\s+d.occasion",
    r"\bGM\b",
    r"president",
    r"pr[ée]sident",
    r"vice.pr[ée]sident",
    r"propri[ée]taire",
    r"owner",
]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

def pause():
    time.sleep(random.uniform(1.5, 3.0))

def quick():
    time.sleep(random.uniform(0.5, 1.0))


def extract_emails(text):
    return list(set(EMAIL_RE.findall(text)))


def find_team_page(base_url):
    """Retourne (url, html) de la meilleure page d'équipe trouvée."""
    for path in TEAM_PATHS:
        try:
            quick()
            r = requests.get(base_url.rstrip("/") + path, headers=HEADERS,
                             timeout=10, allow_redirects=True, verify=False)
            if r.status_code == 200 and len(r.text) > 2000:
                soup = BeautifulSoup(r.text, "html.parser")
                text = soup.get_text(" ", strip=True).lower()
                # Valide que la page parle d'équipe ou de contacts
                hits = sum(1 for kw in ["équipe","team","directeur","manager","contact","about"]
                           if kw in text)
                if hits >= 2:
                    return base_url.rstrip("/") + path, r.text
        except Exception:
            continue
    return None, None


def extract_contacts(html, page_url):
    """
    Extrait les contacts pertinents d'une page HTML.
    Retourne liste de dicts {nom, titre, email, source_url}
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]): tag.decompose()
    text = soup.get_text(" \n", strip=True)
    contacts = []

    # Cherche les emails globalement
    emails_on_page = extract_emails(text)

    # Parcourir le texte ligne par ligne pour trouver titre + nom adjacents
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    for i, line in enumerate(lines):
        line_l = line.lower()
        matched_title = None
        for pat in TITLE_PATTERNS:
            if re.search(pat, line_l, re.I):
                matched_title = line.strip()
                break

        if not matched_title:
            continue

        # Cherche un nom dans les lignes adjacentes (±3 lignes)
        window = lines[max(0, i-3):i+4]
        name = None
        email = None

        for w in window:
            # Cherche email dans la fenêtre
            em = extract_emails(w)
            if em:
                email = em[0]

            # Nom = ligne courte (2-5 mots) qui ressemble à un prénom/nom
            if w == line:
                continue
            words = w.split()
            if 1 <= len(words) <= 5:
                # Filtre les titres et mots clés
                if not re.search(r"directeur|manager|g[ée]n[ée]ral|used|occasion|v[ée]hicule|contact|equipe|team|t[ée]l|email|ext", w, re.I):
                    if all(ww[0].isupper() or not ww[0].isalpha() for ww in words if ww):
                        name = w.strip()

        if matched_title or name:
            contacts.append({
                "nom": name or "",
                "titre": matched_title,
                "email": email or "",
                "source_url": page_url,
            })

    # Dédupliquer
    seen = set()
    unique = []
    for c in contacts:
        key = (c["nom"], c["titre"][:40])
        if key not in seen:
            seen.add(key)
            unique.append(c)

    return unique


def process_dealer(name, city, url):
    if not url or url.strip() in ("", "—"):
        return []

    # Extraire base URL (homepage)
    m = re.match(r"(https?://[^/]+)", url)
    base = m.group(1) if m else url.rstrip("/")

    # 1. Cherche page équipe/contact
    team_url, team_html = find_team_page(base)

    results = []
    if team_html:
        contacts = extract_contacts(team_html, team_url)
        results.extend(contacts)

    # 2. Si rien trouvé, essaie la homepage
    if not results:
        try:
            quick()
            r = requests.get(base, headers=HEADERS, timeout=12,
                             allow_redirects=True, verify=False)
            if r.status_code == 200:
                contacts = extract_contacts(r.text, base)
                results.extend(contacts)
        except Exception:
            pass

    # Ajouter infos dealer à chaque contact
    for c in results:
        c["concessionnaire"] = name
        c["ville"] = city
        c["url_dealer"] = url

    return results


def main():
    out_file = sys.argv[1] if len(sys.argv) > 1 else "output/contacts_gm.csv"

    # Charger les dealers avec URL
    with open("output/results.csv", encoding="utf-8") as f:
        dealers = list(csv.DictReader(f))

    # Filtrer ceux qui ont une URL valide
    with_url = [d for d in dealers
                if d.get("URL_trouvee", "").startswith("http")]
    print(f"Dealers avec URL: {len(with_url)} / {len(dealers)}")

    OUT_FIELDS = ["concessionnaire", "ville", "nom", "titre", "email", "source_url", "url_dealer"]

    with open(out_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS, extrasaction="ignore")
        w.writeheader()

    found_total = 0
    for i, d in enumerate(with_url, 1):
        name  = d["Concessionnaire"]
        city  = d["Ville"]
        url   = d["URL_trouvee"]
        print(f"[{i}/{len(with_url)}] {name} ({city}) → {url[:60]}")

        try:
            contacts = process_dealer(name, city, url)
        except Exception as e:
            print(f"    ✗ Erreur: {e}")
            contacts = []

        if contacts:
            print(f"    ✓ {len(contacts)} contact(s) trouvé(s)")
            for c in contacts:
                print(f"      {c['nom']} | {c['titre'][:50]} | {c['email']}")
            with open(out_file, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=OUT_FIELDS, extrasaction="ignore")
                w.writerows(contacts)
            found_total += len(contacts)
        else:
            print("    — aucun contact trouvé")

        pause()

    print(f"\nTerminé. {found_total} contacts trouvés → {out_file}")


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()
    main()
