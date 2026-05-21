#!/usr/bin/env python3
"""
Revalidation complète — repart du domaine de base pour chaque dealer.
Redécouvre la vraie page d'occasion via find_used_link + Playwright.
Conserve: FERMÉ, non_trouve, manual, autousagee.ca
Usage: python3 revalidate.py [--limit N] [--start N]
"""

import csv, sys, time, os, re
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(__file__))
from scraper import (scrape_static, scrape_playwright, scrape_autousagee,
                     pause, quick, find_used_link, USED_PATHS)
import requests

OUTPUT = os.path.join(os.path.dirname(__file__), "output/results.csv")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
}

KEEP_SOURCES = {"FERMÉ", "non_trouve", "manual", "autousagee.ca", ""}


def base_url(url):
    """Extrait https://domain.com depuis n'importe quelle URL."""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return url


def find_used_url_from_base(base):
    """
    Trouve la vraie URL de la page usagés à partir de la homepage.
    1. Cherche les liens dans le HTML statique
    2. Essaie les chemins courants
    Retourne l'URL de la page usagés, ou base si rien trouvé.
    """
    # 1. Liens dans le HTML statique
    try:
        r = requests.get(base, headers=HEADERS, timeout=12,
                         allow_redirects=True, verify=False)
        if r.status_code == 200:
            link = find_used_link(r.text, base)
            if link and link != base:
                return link
    except Exception:
        pass

    # 2. Probing des chemins usagés — priorité aux plus spécifiques
    priority_paths = [
        "/occasion", "/fr/occasion", "/vehicules-occasion",
        "/fr/vehicules-occasion", "/vehicules-usages", "/fr/vehicules-usages",
        "/inventaire/occasion", "/fr/inventaire/occasion",
        "/inventory/used", "/pre-owned", "/used", "/used-vehicles",
        "/inventaire/usages", "/fr/inventaire/usages",
        "/occasion/recherche.html", "/inventaire/search.html?used=1",
    ]
    for path in priority_paths:
        try:
            url = base.rstrip("/") + path
            r = requests.head(url, headers=HEADERS, timeout=6,
                              allow_redirects=True, verify=False)
            if r.status_code == 200:
                # Vérifier que l'URL finale ne redirige pas vers une page non-usagée
                final = r.url
                if re.search(r"occasion|usag|used|pre.?own|inventaire|inventory",
                             final, re.I):
                    return url
        except Exception:
            pass

    return base


def scrape_from_base(base, name):
    """Scrape en repartant du domaine de base."""
    print(f"  Base: {base}")

    # Trouver la page usagés
    used_url = find_used_url_from_base(base)
    print(f"  Used URL: {used_url}")

    # 1. Scraping statique
    count, src, page_url = scrape_static(used_url)
    if count:
        return count, src, page_url

    # 2. Playwright
    quick()
    count, src, page_url = scrape_playwright(used_url)
    if count:
        return count, src, page_url

    # 3. Autousagee.ca
    city = ""  # on n'a pas la ville ici, pas grave
    count, src = scrape_autousagee(name, city)
    if count:
        return count, src, used_url

    return None, "not_found_js", used_url


def main():
    limit = None
    start = 0
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
        if arg == "--start" and i + 1 < len(sys.argv):
            start = int(sys.argv[i + 1])

    with open(OUTPUT, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    # Dealers à revalider: ceux avec une URL connue, peu importe la source
    to_process = [
        r for r in rows
        if r["Source"] not in KEEP_SOURCES
        and r.get("URL_trouvee", "").strip()
    ]

    if start:
        to_process = to_process[start:]
    if limit:
        to_process = to_process[:limit]

    print(f"{'='*65}")
    print(f"REVALIDATION COMPLÈTE — {len(to_process)} dealers")
    print(f"Logique: redécouverte depuis domaine de base")
    print(f"Début: {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*65}\n")

    changed = 0
    failed = 0
    row_index = {r["Concessionnaire"]: r for r in rows}

    for i, row in enumerate(to_process, 1):
        name = row["Concessionnaire"]
        old_count = row.get("Inventaire_Scraped", "")
        old_url = row.get("URL_trouvee", "")
        old_src = row.get("Source", "")
        base = base_url(old_url)

        print(f"\n[{i+start}/{len(to_process)+start}] {name[:45]}")
        print(f"  Avant: {old_count} ({old_src})")

        try:
            new_count, new_src, new_url = scrape_from_base(base, name)

            if new_count is not None:
                if str(new_count) != str(old_count):
                    print(f"  ✓ {old_count} → {new_count} ({new_src}) → {new_url[:70]}")
                    changed += 1
                else:
                    print(f"  = {new_count} confirmé ({new_src})")

                row_index[name]["Inventaire_Scraped"] = str(new_count)
                row_index[name]["Source"] = new_src
                row_index[name]["URL_trouvee"] = new_url
                row_index[name]["Date_Scrape"] = datetime.now().strftime("%Y-%m-%d")
                row_index[name]["Notes"] = ""
            else:
                print(f"  ✗ Aucun résultat — conserve {old_count}")
                failed += 1

            # Sauvegarde incrémentale toutes les 10
            if i % 10 == 0:
                _save(rows, fieldnames)
                print(f"  → Sauvegarde {i+start}/{len(to_process)+start}")

        except KeyboardInterrupt:
            print(f"\n\nInterrompu à {i}/{len(to_process)}")
            _save(rows, fieldnames)
            sys.exit(0)
        except Exception as e:
            print(f"  ⚠ Erreur: {e}")
            failed += 1

        time.sleep(0.3)

    _save(rows, fieldnames)

    print(f"\n{'='*65}")
    print(f"TERMINÉ — {changed} modifiés | {failed} sans résultat | "
          f"{len(to_process)-failed-changed} confirmés")
    print(f"Fin: {datetime.now().strftime('%H:%M:%S')}")


def _save(rows, fieldnames):
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
