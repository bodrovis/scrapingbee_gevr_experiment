from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

from bs4 import BeautifulSoup


SUPPORTED_FIELDS = {
    "title",
    "price",
    "rating",
    "reviews",
    "in_stock",
}


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def validate_contract(contract: dict[str, Any]) -> None:
    if not isinstance(contract, dict):
        raise ValueError("Contract must be a JSON object.")

    target_url = contract.get("target_url")
    oracle_url = contract.get("oracle_url")
    if not isinstance(target_url, str) or not target_url.startswith(("http://", "https://")):
        raise ValueError("contract.target_url must be an absolute http(s) URL.")
    if not isinstance(oracle_url, str) or not oracle_url.startswith(("http://", "https://")):
        raise ValueError("contract.oracle_url must be an absolute http(s) URL.")

    rendering = contract.get("rendering")
    if not isinstance(rendering, dict):
        raise ValueError("contract.rendering must be an object.")
    if rendering.get("render_js") is not True:
        raise ValueError("This experiment requires rendering.render_js=true.")
    if not isinstance(rendering.get("wait_for"), str) or not rendering["wait_for"].strip():
        raise ValueError("rendering.wait_for must be a non-empty selector.")

    if not isinstance(contract.get("exact_fields"), bool):
        raise ValueError("contract.exact_fields must be a boolean.")

    field_requirements = contract.get("field_requirements")
    if not isinstance(field_requirements, dict):
        raise ValueError("contract.field_requirements must be an object.")

    fields = set(field_requirements)
    if fields != SUPPORTED_FIELDS:
        raise ValueError(
            "This verifier supports exactly these fields: "
            + ", ".join(sorted(SUPPORTED_FIELDS))
            + "."
        )


def build_reference_payload(oracle_json: Any) -> list[dict[str, Any]]:
    if not isinstance(oracle_json, dict):
        raise ValueError("Oracle response must be a JSON object.")

    products = oracle_json.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError("Oracle response must contain a non-empty products array.")

    records: list[dict[str, Any]] = []

    for index, product in enumerate(products):
        if not isinstance(product, dict):
            raise ValueError(f"Oracle product {index} is not an object.")

        title = product.get("title")
        price = product.get("price")
        rating = product.get("rating")
        reviews = product.get("reviews")
        in_stock = product.get("in_stock")

        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"Oracle product {index} has an invalid title.")
        if not _is_number(price) or not math.isfinite(float(price)) or float(price) <= 0:
            raise ValueError(f"Oracle product {index} has an invalid price.")
        if not _is_number(rating) or not math.isfinite(float(rating)) or not 0 <= float(rating) <= 5:
            raise ValueError(f"Oracle product {index} has an invalid rating.")
        if isinstance(reviews, bool) or not isinstance(reviews, int) or reviews < 0:
            raise ValueError(f"Oracle product {index} has an invalid reviews value.")
        if not isinstance(in_stock, bool):
            raise ValueError(f"Oracle product {index} has an invalid in_stock value.")

        records.append(
            {
                "title": title.strip(),
                "price": float(price),
                "rating": float(rating),
                "reviews": reviews,
                "in_stock": in_stock,
            }
        )

    counts = Counter(item["title"] for item in records)
    duplicates = sorted(title for title, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(
            "Oracle contains duplicate product titles, for example: "
            + ", ".join(duplicates[:3])
        )

    meta = oracle_json.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("count"), int):
        if meta["count"] != len(records):
            raise ValueError(
                f"Oracle meta.count={meta['count']} but products contains {len(records)} records."
            )

    return records


def validate_rendered_page(html: str, reference: list[dict[str, Any]]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("#js-product-list")
    if container is None:
        raise ValueError("Rendered page is missing #js-product-list.")
    if container.get("data-ready") != "true":
        raise ValueError(
            "Rendered page did not reach data-ready=true. "
            "The fetch layer may have returned before JavaScript finished."
        )

    cards = container.select(".product-card")
    if not cards:
        raise ValueError("Rendered page contains no .product-card elements.")
    if len(cards) != len(reference):
        raise ValueError(
            f"Rendered DOM contains {len(cards)} product cards but the independent oracle "
            f"contains {len(reference)} products."
        )

    return {
        "rendered_product_cards": len(cards),
        "oracle_product_count": len(reference),
        "data_ready": True,
    }


def inspect_unrendered_page(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("#js-product-list")
    cards = soup.select("#js-product-list .product-card")
    return {
        "container_present": container is not None,
        "data_ready": container.get("data-ready") if container is not None else None,
        "product_card_count": len(cards),
    }


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

        if "price" in item:
            price = item["price"]
            if not _is_number(price):
                errors.append(f"{prefix}.price: must be a JSON number.")
            elif not math.isfinite(float(price)) or float(price) <= 0:
                errors.append(f"{prefix}.price: must be a positive finite number.")

        if "rating" in item:
            rating = item["rating"]
            if not _is_number(rating):
                errors.append(f"{prefix}.rating: must be a JSON number.")
            elif not math.isfinite(float(rating)) or not 0 <= float(rating) <= 5:
                errors.append(f"{prefix}.rating: must be between 0 and 5.")

        if "reviews" in item:
            reviews = item["reviews"]
            if isinstance(reviews, bool) or not isinstance(reviews, int) or reviews < 0:
                errors.append(f"{prefix}.reviews: must be a non-negative integer.")

        if "in_stock" in item and not isinstance(item["in_stock"], bool):
            errors.append(f"{prefix}.in_stock: must be true or false.")

    return errors


def _harness_failure(message: str, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "passed": False,
        "harness_error": True,
        "errors": [message],
        "feedback": [],
        "warnings": [],
        "stats": stats or {},
    }


def verify(
    payload: Any,
    contract: dict[str, Any],
    reference: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        validate_contract(contract)
    except ValueError as exc:
        return _harness_failure(f"Invalid verification contract: {exc}")

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

    if len(payload) != len(reference):
        errors.append(f"Expected {len(reference)} items, got {len(payload)}.")
        feedback.append(
            f"The rendered page contains {len(reference)} product records according to the "
            f"independent oracle, but the extractor returned {len(payload)}. Re-check the product "
            "container selector and any conditions that skip records."
        )

    valid_items = [
        item
        for item in payload
        if isinstance(item, dict)
        and isinstance(item.get("title"), str)
        and item["title"].strip()
    ]

    payload_by_title: dict[str, dict[str, Any]] = {}
    duplicate_titles: set[str] = set()
    for item in valid_items:
        title = item["title"].strip()
        if title in payload_by_title:
            duplicate_titles.add(title)
        payload_by_title[title] = item

    if duplicate_titles:
        errors.append(
            "Duplicate title values: " + ", ".join(sorted(duplicate_titles)[:5])
        )
        feedback.append(
            f"The extractor returned {len(duplicate_titles)} duplicate title(s). "
            "Re-check product selection."
        )

    reference_by_title = {item["title"]: item for item in reference}
    matched_titles = sorted(set(reference_by_title) & set(payload_by_title))
    missing_titles = sorted(set(reference_by_title) - set(payload_by_title))
    extra_titles = sorted(set(payload_by_title) - set(reference_by_title))

    if missing_titles:
        errors.append(
            f"Missing {len(missing_titles)} expected product title(s), for example: "
            f"{missing_titles[:3]}"
        )
        feedback.append(
            f"The output is missing {len(missing_titles)} product(s) present in the rendered page. "
            "Re-check product-card selection and title extraction."
        )

    if extra_titles:
        errors.append(
            f"Found {len(extra_titles)} unexpected product title(s), for example: "
            f"{extra_titles[:3]}"
        )
        feedback.append(
            f"The output contains {len(extra_titles)} unexpected product(s). "
            "Re-check product-card selection and title extraction."
        )

    mismatch_counts: Counter[str] = Counter()
    detailed_mismatch_count = 0

    for title in matched_titles:
        expected = reference_by_title[title]
        actual = payload_by_title[title]

        if set(contract["field_requirements"]) - set(actual):
            continue

        mismatches: list[str] = []

        for field in ("price", "rating"):
            value = actual[field]
            if not _is_number(value) or not math.isclose(
                float(value),
                float(expected[field]),
                rel_tol=0.0,
                abs_tol=0.001,
            ):
                mismatch_counts[field] += 1
                mismatches.append(
                    f"{field} expected {expected[field]!r}, got {value!r}"
                )

        if actual["reviews"] != expected["reviews"]:
            mismatch_counts["reviews"] += 1
            mismatches.append(
                f"reviews expected {expected['reviews']!r}, got {actual['reviews']!r}"
            )

        if actual["in_stock"] is not expected["in_stock"]:
            mismatch_counts["in_stock"] += 1
            mismatches.append(
                f"in_stock expected {expected['in_stock']!r}, got {actual['in_stock']!r}"
            )

        if mismatches:
            detailed_mismatch_count += 1
            if detailed_mismatch_count <= 8:
                errors.append(f"Value mismatch for {title!r}: " + "; ".join(mismatches))

    if detailed_mismatch_count > 8:
        errors.append(
            f"...and {detailed_mismatch_count - 8} more product(s) with value mismatches."
        )

    for field, count in sorted(mismatch_counts.items()):
        feedback.append(
            f"{count} matched product(s) have an incorrect {field} value. "
            "Re-read that field from the rendered HTML at runtime; do not hard-code oracle values."
        )

    stats = {
        "item_count": len(payload),
        "reference_item_count": len(reference),
        "matched_products": len(matched_titles),
        "missing_products": len(missing_titles),
        "unexpected_products": len(extra_titles),
        "value_mismatch_count": detailed_mismatch_count,
        "field_mismatch_counts": dict(sorted(mismatch_counts.items())),
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
    reference: list[dict[str, Any]],
) -> tuple[Any | None, dict[str, Any]]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, {
            "passed": False,
            "harness_error": False,
            "errors": [f"stdout is not valid JSON: {exc}"],
            "feedback": ["The extractor did not print valid JSON to stdout."],
            "warnings": [],
            "stats": {},
        }

    return payload, verify(payload, contract, reference)
