import sys
import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup

if len(sys.argv) != 2:
    sys.exit(2)

source_url = sys.argv[1]
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

    title = link.get("title", "").strip()
    if not title:
        title = link.get_text(" ", strip=True)

    price_match = re.search(r"\d+(?:[.,]\d+)?", price_element.get_text(strip=True))
    price_gbp = float(price_match.group(0).replace(",", "."))

    availability_classes = availability.get("class", [])
    in_stock = "instock" in availability_classes

    rating = 0
    for class_name in rating_element.get("class", []):
        if class_name in rating_values:
            rating = rating_values[class_name]
            break

    results.append({
        "title": title,
        "price_gbp": price_gbp,
        "in_stock": in_stock,
        "rating": rating,
        "product_url": urljoin(source_url, link.get("href", ""))
    })

print(json.dumps(results, ensure_ascii=False))