import sys
import json
import re
from bs4 import BeautifulSoup

if len(sys.argv) != 2:
    raise SystemExit(2)

source_url = sys.argv[1]
html = sys.stdin.read()
soup = BeautifulSoup(html, "html.parser")

container = soup.select_one("#js-product-list")
cards = container.select(".product-card") if container else soup.select(".product-card")
results = []

for card in cards:
    title_node = card.select_one(".product-title")
    price_node = card.select_one(".product-price")
    rating_node = card.select_one(".product-rating")

    title = title_node.get_text(" ", strip=True)

    price_text = price_node.get_text(" ", strip=True)
    price_match = re.search(r"\d[\d,]*(?:\.\d+)?", price_text)
    price = float(price_match.group(0).replace(",", ""))

    rating_text = rating_node.get_text(" ", strip=True)
    rating_raw = rating_node.get("data-rating")
    if rating_raw is None:
        rating_match = re.search(r"\d+(?:\.\d+)?", rating_text)
        rating_raw = rating_match.group(0)
    rating = float(str(rating_raw).replace(",", ""))

    reviews_match = re.search(r"\(\s*([\d,]+)\s*\)", rating_text)
    reviews = int(reviews_match.group(1).replace(",", ""))

    stock_raw = card.get("data-in-stock")
    if stock_raw is not None:
        in_stock = str(stock_raw).strip().lower() == "true"
    else:
        stock_node = card.select_one(".product-stock")
        stock_text = stock_node.get_text(" ", strip=True).lower()
        in_stock = not stock_text.startswith("out of stock")

    results.append({
        "title": title,
        "price": price,
        "rating": rating,
        "reviews": reviews,
        "in_stock": in_stock
    })

print(json.dumps(results, ensure_ascii=False))