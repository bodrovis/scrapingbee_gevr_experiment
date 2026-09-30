from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


SUPPORTED_FIELDS = {
    "title",
    "price_gbp",
    "in_stock",
    "rating",
    "product_url",
}

RATING_MAP = {
    "One": 1,
    "Two": 2,
    "Three": 3,
    "Four": 4,
    "Five": 5,
}

PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def validate_contract(contract: dict[str, Any]) -> None:
    if not isinstance(contract, dict):
        raise ValueError("Contract must be a JSON object.")

    target_url = contract.get("target_url")
    if not _is_absolute_http_url(target_url):
        raise ValueError("contract.target_url must be an absolute http(s) URL.")

    expected_items = contract.get("expected_items")
    if isinstance(expected_items, bool) or not isinstance(expected_items, int) or expected_items < 1:
        raise ValueError("contract.expected_items must be a positive integer.")

    if not isinstance(contract.get("exact_fields"), bool):
        raise ValueError("contract.exact_fields must be a boolean.")

    field_requirements = contract.get("field_requirements")
    if not isinstance(field_requirements, dict):
        raise ValueError("contract.field_requirements must be an object.")

    fields = set(field_requirements)
    if fields != SUPPORTED_FIELDS:
        raise ValueError(
            "This test-specific verifier supports exactly these fields: "
            + ", ".join(sorted(SUPPORTED_FIELDS))
            + "."
        )


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _is_absolute_http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def build_reference_payload(html: str, source_url: str) -> list[dict[str, Any]]:
    """Trusted, hand-written oracle for the article's books.toscrape.com test."""
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict[str, Any]] = []

    for product in soup.select("article.product_pod"):
        link = product.select_one("h3 a")
        price_node = product.select_one("p.price_color")
        availability_node = product.select_one("p.instock.availability")
        rating_node = product.select_one("p.star-rating")

        if not all((link, price_node, availability_node, rating_node)):
            raise ValueError("Reference extractor found a product with missing expected markup.")

        price_match = PRICE_RE.search(price_node.get_text(" ", strip=True))
        if not price_match:
            raise ValueError("Reference extractor could not parse a product price.")

        rating = next(
            (RATING_MAP[name] for name in RATING_MAP if name in rating_node.get("class", [])),
            None,
        )
        if rating is None:
            raise ValueError("Reference extractor could not parse a product rating.")

        href = link.get("href")
        if not href:
            raise ValueError("Reference extractor found a product without an href.")

        title = link.get("title") or link.get_text(" ", strip=True)
        title = title.strip()
        if not title:
            raise ValueError("Reference extractor found a product without a title.")

        records.append(
            {
                "title": title,
                "price_gbp": float(price_match.group(1)),
                "in_stock": "In stock" in availability_node.get_text(" ", strip=True),
                "rating": rating,
                "product_url": urljoin(source_url, href),
            }
        )

    if not records:
        raise ValueError("Reference extractor found no product records.")

    url_counts = Counter(item["product_url"] for item in records)
    duplicate_urls = sorted(url for url, count in url_counts.items() if count > 1)
    if duplicate_urls:
        raise ValueError(
            "Reference extractor produced duplicate product URLs, for example: "
            + ", ".join(duplicate_urls[:3])
        )

    return records


def _schema_errors(payload: Any, contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_fields = set(contract["field_requirements"])
    exact_fields = bool(contract.get("exact_fields", True))

    if not isinstance(payload, list):
        return [f"Top-level output must be a JSON array, got {type(payload).__name__}."]

    for i, item in enumerate(payload):
        prefix = f"item[{i}]"

        if not isinstance(item, dict):
            errors.append(f"{prefix}: expected an object, got {type(item).__name__}.")
            continue

        actual_fields = set(item)
        missing = expected_fields - actual_fields
        extra = actual_fields - expected_fields

        if missing:
            errors.append(f"{prefix}: missing fields: {', '.join(sorted(missing))}.")
        if exact_fields and extra:
            errors.append(f"{prefix}: unexpected fields: {', '.join(sorted(extra))}.")

        if "title" in item:
            title = item["title"]
            if not isinstance(title, str) or not title.strip():
                errors.append(f"{prefix}.title: must be a non-empty string.")

        if "price_gbp" in item:
            price = item["price_gbp"]
            if not _is_number(price):
                errors.append(f"{prefix}.price_gbp: must be a JSON number.")
            elif not math.isfinite(float(price)) or not (0 < float(price) < 1000):
                errors.append(f"{prefix}.price_gbp: implausible value {price!r}.")

        if "in_stock" in item and not isinstance(item["in_stock"], bool):
            errors.append(f"{prefix}.in_stock: must be true or false.")

        if "rating" in item:
            rating = item["rating"]
            if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
                errors.append(f"{prefix}.rating: must be an integer from 1 to 5.")

        if "product_url" in item and not _is_absolute_http_url(item["product_url"]):
            errors.append(f"{prefix}.product_url: must be an absolute http(s) URL.")

    return errors


def _harness_failure(message: str, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "passed": False,
        "harness_error": True,
        "errors": [message],
        "feedback": ["The verification harness failed; do not attempt to revise the extractor."],
        "warnings": [],
        "stats": stats or {},
    }


def verify(
    payload: Any,
    contract: dict[str, Any],
    source_html: str,
    source_url: str,
) -> dict[str, Any]:
    try:
        validate_contract(contract)
    except ValueError as exc:
        return _harness_failure(f"Invalid verification contract: {exc}")

    try:
        reference = build_reference_payload(source_html, source_url)
    except ValueError as exc:
        return _harness_failure(f"Reference verifier failed: {exc}")

    expected_items = int(contract["expected_items"])
    if len(reference) != expected_items:
        return _harness_failure(
            "Reference verifier extracted "
            f"{len(reference)} items, but the contract expects {expected_items}. "
            "The target page or test contract has changed.",
            stats={"reference_item_count": len(reference)},
        )

    errors = _schema_errors(payload, contract)
    feedback = list(errors)
    warnings: list[str] = []

    if not isinstance(payload, list):
        return {
            "passed": False,
            "harness_error": False,
            "errors": errors,
            "feedback": feedback,
            "warnings": warnings,
            "stats": {"reference_item_count": len(reference)},
        }

    if len(payload) != expected_items:
        message = f"Expected {expected_items} items, got {len(payload)}."
        errors.append(message)
        feedback.append(message)

    valid_payload_items = [
        item
        for item in payload
        if isinstance(item, dict) and _is_absolute_http_url(item.get("product_url"))
    ]

    payload_by_url: dict[str, dict[str, Any]] = {}
    duplicate_urls: set[str] = set()
    for item in valid_payload_items:
        url = item["product_url"]
        if url in payload_by_url:
            duplicate_urls.add(url)
        payload_by_url[url] = item

    if duplicate_urls:
        message = "Duplicate product_url values: " + ", ".join(sorted(duplicate_urls)[:5])
        errors.append(message)
        feedback.append(message)

    reference_by_url = {item["product_url"]: item for item in reference}
    matched_urls = sorted(set(reference_by_url) & set(payload_by_url))
    missing_urls = sorted(set(reference_by_url) - set(payload_by_url))
    extra_urls = sorted(set(payload_by_url) - set(reference_by_url))

    if missing_urls:
        errors.append(
            f"Missing {len(missing_urls)} expected product URL(s), for example: {missing_urls[:3]}"
        )
        feedback.append(
            f"The output is missing {len(missing_urls)} product URL(s) that are present in the page. "
            "Re-check link selection and URL resolution."
        )
    if extra_urls:
        errors.append(
            f"Found {len(extra_urls)} unexpected product URL(s), for example: {extra_urls[:3]}"
        )
        feedback.append(
            f"The output contains {len(extra_urls)} unexpected product URL(s). "
            "Re-check product container selection and URL resolution."
        )

    field_mismatch_counts: Counter[str] = Counter()
    detailed_mismatch_count = 0

    for url in matched_urls:
        expected = reference_by_url[url]
        actual = payload_by_url[url]

        required_fields = set(contract["field_requirements"])
        if required_fields - set(actual):
            continue

        field_mismatches: list[str] = []

        if actual["title"] != expected["title"]:
            field_mismatch_counts["title"] += 1
            field_mismatches.append(
                f"title expected {expected['title']!r}, got {actual['title']!r}"
            )

        if not _is_number(actual["price_gbp"]) or not math.isclose(
            float(actual["price_gbp"]),
            expected["price_gbp"],
            rel_tol=0.0,
            abs_tol=0.001,
        ):
            field_mismatch_counts["price_gbp"] += 1
            field_mismatches.append(
                f"price_gbp expected {expected['price_gbp']!r}, got {actual['price_gbp']!r}"
            )

        if actual["in_stock"] is not expected["in_stock"]:
            field_mismatch_counts["in_stock"] += 1
            field_mismatches.append(
                f"in_stock expected {expected['in_stock']!r}, got {actual['in_stock']!r}"
            )

        if actual["rating"] != expected["rating"]:
            field_mismatch_counts["rating"] += 1
            field_mismatches.append(
                f"rating expected {expected['rating']!r}, got {actual['rating']!r}"
            )

        if field_mismatches:
            detailed_mismatch_count += 1
            if detailed_mismatch_count <= 8:
                errors.append(f"Value mismatch for {url}: " + "; ".join(field_mismatches))

    if detailed_mismatch_count > 8:
        errors.append(
            f"...and {detailed_mismatch_count - 8} more product(s) with value mismatches."
        )

    for field, count in sorted(field_mismatch_counts.items()):
        feedback.append(
            f"{count} matched product(s) have an incorrect {field} value. "
            "Re-read that field from the supplied HTML at runtime; do not hard-code reference values."
        )

    stats = {
        "item_count": len(payload),
        "reference_item_count": len(reference),
        "matched_product_urls": len(matched_urls),
        "missing_product_urls": len(missing_urls),
        "unexpected_product_urls": len(extra_urls),
        "value_mismatch_count": detailed_mismatch_count,
        "field_mismatch_counts": dict(sorted(field_mismatch_counts.items())),
    }

    return {
        "passed": not errors,
        "harness_error": False,
        "errors": errors,
        "feedback": feedback,
        "warnings": warnings,
        "stats": stats,
    }


def feedback_for_model(verification: dict[str, Any]) -> dict[str, Any]:
    return {
        "passed": bool(verification.get("passed", False)),
        "errors": list(verification.get("feedback", verification.get("errors", []))),
        "warnings": list(verification.get("warnings", [])),
        "stats": dict(verification.get("stats", {})),
    }


def verify_stdout(
    stdout: str,
    contract: dict[str, Any],
    source_html: str,
    source_url: str,
) -> tuple[Any | None, dict[str, Any]]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        message = f"stdout is not valid JSON: {exc}"
        return None, {
            "passed": False,
            "harness_error": False,
            "errors": [message],
            "feedback": [message],
            "warnings": [],
            "stats": {},
        }

    return payload, verify(payload, contract, source_html, source_url)
