import sys
import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup

_, source_url = sys.argv
html = sys.stdin.read()
soup = BeautifulSoup(html, "html.parser")

rating_values = {
    "One": 1,
    "Two": 2,
    "Three": 3,
    "Four": 4,
    "Five": 5
}

results = []

for product in soup.select("article.product_pod"):
    link = product.select_one("h3 a")
    price_element = product.select_one(".price_color")
    availability = product.select_one(".availability")
    rating_element = product.select_one(".star-rating")

    if not link or not price_element or not availability or not rating_element:
        continue

    title = str(link.get("title", "")).strip()
    if not title:
        title = link.get_text(" ", strip=True)

    price_match = re.search(r"\d+(?:[.,]\d+)?", price_element.get_text(" ", strip=True))
    if not title or not price_match:
        continue

    rating = None
    for class_name in rating_element.get("class", []):
        if class_name in rating_values:
            rating = rating_values[class_name]
            break

    href = str(link.get("href", "")).strip()
    if rating is None or not href:
        continue

    results.append({
        "title": title,
        "price_gbp": float(price_match.group(0).replace(",", ".")),
        "in_stock": "instock" in availability.get("class", []),
        "rating": rating,
        "product_url": urljoin(source_url, href)
    })

print(json.dumps(results, ensure_ascii=False))