import sys
import json
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

if len(sys.argv) != 2:
    print("[]")
    sys.exit(0)

source_url = sys.argv[1]
html = sys.stdin.read()
soup = BeautifulSoup(html, "html.parser")

rating_values = {
    "One": 1,
    "Two": 2,
    "Three": 3,
    "Four": 4,
    "Five": 5,
}

items = []

for product in soup.select("article.product_pod"):
    link = product.select_one("h3 a[href]")
    price_element = product.select_one(".price_color")
    rating_element = product.select_one(".star-rating")
    availability_element = product.select_one(".availability")

    if not all((link, price_element, rating_element, availability_element)):
        continue

    title = (link.get("title") or link.get_text(" ", strip=True)).strip()
    product_url = urljoin(source_url, link["href"].strip())
    parsed_url = urlparse(product_url)

    price_match = re.search(r"\d+(?:[.,]\d+)?", price_element.get_text(strip=True))
    if not title or not price_match or parsed_url.scheme not in ("http", "https"):
        continue

    rating = None
    for class_name in rating_element.get("class", []):
        if class_name in rating_values:
            rating = rating_values[class_name]
            break

    if rating is None:
        continue

    availability = availability_element.get_text(" ", strip=True).lower()
    in_stock = "in stock" in availability and "out of stock" not in availability

    items.append({
        "title": title,
        "price_gbp": float(price_match.group(0).replace(",", ".")),
        "in_stock": in_stock,
        "rating": rating,
        "product_url": product_url,
    })

print(json.dumps(items, ensure_ascii=False))