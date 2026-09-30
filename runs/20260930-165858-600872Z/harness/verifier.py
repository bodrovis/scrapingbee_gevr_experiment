from __future__ import annotations

import json
from typing import Any

from bs4 import BeautifulSoup


SUPPORTED_FIELDS = {"position", "text"}


def validate_contract(contract: dict[str, Any]) -> None:
    if not isinstance(contract, dict):
        raise ValueError("Contract must be a JSON object.")

    target_url = contract.get("target_url")
    if not isinstance(target_url, str) or not target_url.startswith(("http://", "https://")):
        raise ValueError("contract.target_url must be an absolute http(s) URL.")

    expected_total = contract.get("expected_total_items")
    if isinstance(expected_total, bool) or not isinstance(expected_total, int) or expected_total <= 0:
        raise ValueError("contract.expected_total_items must be a positive integer.")

    selectors = contract.get("selectors")
    if not isinstance(selectors, dict):
        raise ValueError("contract.selectors must be an object.")
    for key in ("list", "item", "complete"):
        if not isinstance(selectors.get(key), str) or not selectors[key].strip():
            raise ValueError(f"contract.selectors.{key} must be a non-empty string.")

    if not isinstance(contract.get("exact_fields"), bool):
        raise ValueError("contract.exact_fields must be a boolean.")

    fields = contract.get("field_requirements")
    if not isinstance(fields, dict) or set(fields) != SUPPORTED_FIELDS:
        raise ValueError(
            "This verifier supports exactly these fields: "
            + ", ".join(sorted(SUPPORTED_FIELDS))
            + "."
        )


def inspect_scroll_dom(html: str, contract: dict[str, Any]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    list_node = soup.select_one(contract["selectors"]["list"])
    items = soup.select(contract["selectors"]["item"])

    data_complete = None
    if list_node is not None:
        data_complete = list_node.get("data-complete")

    status_node = soup.select_one("#scroll-status")
    status_text = status_node.get_text(" ", strip=True) if status_node else None

    expected_total = int(contract["expected_total_items"])
    complete = data_complete == "true" and len(items) == expected_total

    return {
        "list_present": list_node is not None,
        "item_count": len(items),
        "expected_total_items": expected_total,
        "data_complete": data_complete,
        "status_text": status_text,
        "complete": complete,
    }


def build_reference_payload(html: str, contract: dict[str, Any]) -> list[dict[str, Any]]:
    validate_contract(contract)
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(contract["selectors"]["item"])

    return [
        {
            "position": index,
            "text": node.get_text(" ", strip=True),
        }
        for index, node in enumerate(items, start=1)
    ]


def validate_complete_dom(html: str, contract: dict[str, Any]) -> dict[str, Any]:
    meta = inspect_scroll_dom(html, contract)

    if not meta["list_present"]:
        raise ValueError("Rendered page is missing the infinite-scroll list.")
    if meta["data_complete"] != "true":
        raise ValueError(
            "Infinite-scroll list did not reach data-complete=true. "
            "The fetch layer returned an incomplete DOM."
        )
    if meta["item_count"] != meta["expected_total_items"]:
        raise ValueError(
            f"Rendered DOM contains {meta['item_count']} feed items, but the challenge "
            f"declares {meta['expected_total_items']} total items."
        )

    return meta


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

        if "position" in item:
            position = item["position"]
            if isinstance(position, bool) or not isinstance(position, int) or position <= 0:
                errors.append(f"{prefix}.position: must be a positive integer.")

        if "text" in item:
            text = item["text"]
            if not isinstance(text, str) or not text.strip():
                errors.append(f"{prefix}.text: must be a non-empty string.")

    return errors


def verify(
    payload: Any,
    contract: dict[str, Any],
    reference: list[dict[str, Any]],
) -> dict[str, Any]:
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
            f"The fully scrolled DOM contains {len(reference)} feed items, but the extractor "
            f"returned {len(payload)}. Re-check the feed-item selector and skip conditions."
        )

    mismatch_count = 0
    compare_count = min(len(payload), len(reference))

    for index in range(compare_count):
        actual = payload[index]
        expected = reference[index]

        if not isinstance(actual, dict):
            continue
        if {"position", "text"} - set(actual):
            continue

        mismatches: list[str] = []

        if actual["position"] != expected["position"]:
            mismatches.append(
                f"position expected {expected['position']!r}, got {actual['position']!r}"
            )
        if actual["text"] != expected["text"]:
            mismatches.append("text does not match normalized visible text from the DOM")

        if mismatches:
            mismatch_count += 1
            if mismatch_count <= 8:
                errors.append(
                    f"Value mismatch at output index {index}: " + "; ".join(mismatches)
                )

    if mismatch_count > 8:
        errors.append(f"...and {mismatch_count - 8} more item(s) with value mismatches.")

    if mismatch_count:
        feedback.append(
            f"{mismatch_count} output item(s) do not match the DOM order and/or normalized visible "
            "text. Iterate over every .feed-item in DOM order and derive values at runtime."
        )

    return {
        "passed": not errors,
        "harness_error": False,
        "errors": errors,
        "feedback": feedback,
        "warnings": warnings,
        "stats": {
            "item_count": len(payload),
            "reference_item_count": len(reference),
            "value_mismatch_count": mismatch_count,
        },
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
