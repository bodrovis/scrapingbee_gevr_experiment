import sys
import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup

def main():
    if len(sys.argv) != 2:
        print("[]")
        return

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

    products = []

    for card in soup.select("article.product_pod"):
        link = card.select_one("h3 a[href]")
        price_element = card.select_one(".price_color")
        rating_element = card.select_one(".star-rating")
        availability_element = card.select_one(".availability")

        if not link or not price_element or not rating_element or not availability_element:
            continue

        title = (link.get("title") or link.get_text(" ", strip=True)).strip()
        href = link.get("href", "").strip()

        price_match = re.search(r"(\d+(?:\.\d+)?)", price_element.get_text(" ", strip=True))
        if not title or not href or not price_match:
            continue

        rating = None
        for class_name in rating_element.get("class", []):
            if class_name in rating_values:
                rating = rating_values[class_name]
                break

        if rating is None:
            continue

        availability_text = " ".join(
            availability_element.get_text(" ", strip=True).lower().split()
        )
        in_stock = (
            "in stock" in availability_text
            and "out of stock" not in availability_text
        )

        products.append({
            "title": title,
            "price_gbp": float(price_match.group(1)),
            "in_stock": in_stock,
            "rating": rating,
            "product_url": urljoin(source_url, href)
        })

    print(json.dumps(products, ensure_ascii=False))

if __name__ == "__main__":
    main()