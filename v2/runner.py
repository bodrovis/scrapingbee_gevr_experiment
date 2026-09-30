from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from openai import OpenAI

from prompts import DEVELOPER_INSTRUCTIONS, initial_prompt, refine_prompt
from verifier import (
    build_reference_payload,
    feedback_for_model,
    inspect_unrendered_page,
    validate_contract,
    validate_rendered_page,
    verify_stdout,
)


ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
CONTRACT = json.loads((ROOT / "contract.json").read_text(encoding="utf-8"))

TRACKED_FILES = [
    "runner.py",
    "prompts.py",
    "verifier.py",
    "contract.json",
    "pyproject.toml",
    "uv.lock",
]

ALLOWED_IMPORTS = {
    "__future__",
    "bs4",
    "decimal",
    "html",
    "json",
    "re",
    "sys",
    "typing",
    "urllib.parse",
}

ALLOWED_SYS_ATTRIBUTES = {
    "argv",
    "exit",
    "stdin",
    "stderr",
}

FORBIDDEN_CALL_NAMES = {
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "open",
    "setattr",
    "type",
    "vars",
}

FORBIDDEN_NAMES = {
    "__builtins__",
    "__loader__",
    "__spec__",
}

CODE_FENCE_RE = re.compile(r"^```(?:python)?\s*|\s*```$", re.IGNORECASE)
CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.IGNORECASE)


def fetch_with_scrapingbee(
    target_url: str,
    api_key: str,
    *,
    render_js: bool,
    wait_for: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    params: dict[str, str] = {
        "url": target_url,
        "render_js": str(render_js).lower(),
    }
    if wait_for:
        params["wait_for"] = wait_for

    response = requests.get(
        "https://app.scrapingbee.com/api/v1/",
        params=params,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=90,
    )
    response.raise_for_status()

    headers = {k.lower(): v for k, v in response.headers.items()}
    meta = {
        "status_code": response.status_code,
        "scrapingbee_cost": headers.get("spb-cost"),
        "scrapingbee_request_id": headers.get("spb-request-id"),
        "resolved_url": headers.get("spb-resolved-url"),
        "content_type": headers.get("content-type"),
        "render_js": render_js,
        "wait_for": wait_for,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return response.content, meta


def fetch_oracle(oracle_url: str) -> tuple[Any, dict[str, Any]]:
    response = requests.get(
        oracle_url,
        headers={"User-Agent": "scrapingbee-gevr-js-rendered/0.1"},
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    return payload, {
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
        "url": response.url,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def decode_html(raw_html: bytes, content_type: str | None) -> tuple[str, str]:
    candidates: list[str] = []

    if content_type:
        match = CHARSET_RE.search(content_type)
        if match:
            candidates.append(match.group(1))

    head = raw_html[:8192].decode("ascii", errors="ignore")
    match = CHARSET_RE.search(head)
    if match:
        candidates.append(match.group(1))

    candidates.append("utf-8")

    seen: set[str] = set()
    for encoding in candidates:
        normalized = encoding.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return raw_html.decode(encoding), encoding
        except (LookupError, UnicodeDecodeError):
            continue

    return raw_html.decode("utf-8", errors="replace"), "utf-8-with-replacement"


def strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = CODE_FENCE_RE.sub("", text).strip()
    return text


def _import_allowed(module_name: str | None) -> bool:
    return bool(module_name) and module_name in ALLOWED_IMPORTS


def static_preflight(code: str) -> list[str]:
    """Basic policy check. This reduces risk but is not a security sandbox."""
    errors: list[str] = []

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"Generated code has a syntax error: {exc}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _import_allowed(alias.name):
                    errors.append(f"Disallowed import: {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or not _import_allowed(node.module):
                errors.append(f"Disallowed import: {node.module or '<relative import>'}")

        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALL_NAMES:
                errors.append(f"Disallowed call: {node.func.id}()")

        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            errors.append(f"Disallowed name: {node.id}")

        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                errors.append(f"Disallowed dunder attribute access: .{node.attr}")

            if isinstance(node.value, ast.Name) and node.value.id == "sys":
                if node.attr not in ALLOWED_SYS_ATTRIBUTES:
                    errors.append(f"Disallowed sys attribute: sys.{node.attr}")

    return sorted(set(errors))


def restricted_subprocess_env() -> dict[str, str]:
    keep = {
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
    }
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def _read_capture(stream: Any, limit_bytes: int) -> tuple[str, int, bool]:
    stream.flush()
    stream.seek(0, os.SEEK_END)
    total_bytes = stream.tell()
    stream.seek(0)
    data = stream.read(limit_bytes)
    truncated = total_bytes > limit_bytes
    text = data.decode("utf-8", errors="replace")
    if truncated:
        text += f"\n[HARNESS TRUNCATED OUTPUT AFTER {limit_bytes} BYTES]"
    return text, total_bytes, truncated


def execute_extractor(
    code: str,
    html_text: str,
    source_url: str,
    output_limit_bytes: int,
) -> tuple[str, str, int, dict[str, Any]]:
    """Run generated code in isolated Python mode and a disposable working directory.

    This is containment for a controlled experiment, not a security sandbox.
    """
    with tempfile.TemporaryDirectory(prefix="gevr-extractor-") as tmp:
        tmp_dir = Path(tmp)
        tmp_code = tmp_dir / "extractor.py"
        tmp_code.write_text(code, encoding="utf-8")

        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            timed_out = False
            try:
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-X",
                        "utf8",
                        "-B",
                        str(tmp_code),
                        source_url,
                    ],
                    cwd=str(tmp_dir),
                    env=restricted_subprocess_env(),
                    input=html_text.encode("utf-8"),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=10,
                )
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                returncode = 124

            stdout, stdout_bytes, stdout_truncated = _read_capture(
                stdout_file, output_limit_bytes
            )
            stderr, stderr_bytes, stderr_truncated = _read_capture(
                stderr_file, output_limit_bytes
            )

            if timed_out:
                stderr += "\nExecution timed out after 10 seconds."

            execution_meta = {
                "stdout_bytes": stdout_bytes,
                "stderr_bytes": stderr_bytes,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "timed_out": timed_out,
                "output_limit_bytes": output_limit_bytes,
            }
            return stdout, stderr, returncode, execution_meta


def _sdk_object_to_dict(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


def call_llm(
    client: OpenAI,
    model: str,
    prompt: str,
) -> tuple[str, dict[str, Any], str]:
    response = client.responses.create(
        model=model,
        instructions=DEVELOPER_INSTRUCTIONS,
        input=prompt,
    )

    response_meta = {
        "response_id": getattr(response, "id", None),
        "requested_model": model,
        "returned_model": getattr(response, "model", None),
        "usage": _sdk_object_to_dict(getattr(response, "usage", None)),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "developer_instructions_sha256": hashlib.sha256(
            DEVELOPER_INSTRUCTIONS.encode("utf-8")
        ).hexdigest(),
    }

    if hasattr(response, "model_dump_json"):
        response_json = response.model_dump_json(indent=2)
    else:
        response_json = json.dumps(
            _sdk_object_to_dict(response),
            indent=2,
            default=str,
        )

    return strip_code_fences(response.output_text), response_meta, response_json


def html_for_prompt(html_text: str, limit: int) -> tuple[str, bool]:
    if len(html_text) <= limit:
        return html_text, False
    return html_text[:limit] + "\n<!-- HTML CONTEXT TRUNCATED BY HARNESS -->", True


def make_diff(old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile="previous_extractor.py",
            tofile="revised_extractor.py",
        )
    )


def _package_version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_metadata(model: str) -> dict[str, Any]:
    return {
        "python_version": sys.version,
        "platform": platform.platform(),
        "requested_model": model,
        "packages": {
            "beautifulsoup4": _package_version("beautifulsoup4"),
            "openai": _package_version("openai"),
            "python-dotenv": _package_version("python-dotenv"),
            "requests": _package_version("requests"),
        },
        "harness_sha256": {
            name: _sha256(ROOT / name)
            for name in TRACKED_FILES
            if (ROOT / name).exists()
        },
    }


def snapshot_harness(run_dir: Path) -> None:
    snapshot_dir = run_dir / "harness"
    snapshot_dir.mkdir()
    for name in TRACKED_FILES:
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, snapshot_dir / name)


def write_error(path: Path, exc: BaseException, extra: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {
        "exception_type": type(exc).__name__,
        "message": str(exc),
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _cost_as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def main() -> int:
    load_dotenv(ROOT / ".env")

    try:
        validate_contract(CONTRACT)
    except ValueError as exc:
        raise SystemExit(f"Invalid contract.json: {exc}") from exc

    sb_key = os.environ.get("SCRAPINGBEE_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", "gpt-5.6")
    max_attempts = int(os.environ.get("MAX_ATTEMPTS", "3"))
    html_prompt_limit = int(os.environ.get("HTML_PROMPT_LIMIT", "120000"))
    output_limit_bytes = int(os.environ.get("EXTRACTOR_OUTPUT_LIMIT_BYTES", "1000000"))

    if not sb_key:
        raise SystemExit("Missing SCRAPINGBEE_API_KEY.")
    if not openai_key:
        raise SystemExit("Missing OPENAI_API_KEY.")
    if max_attempts < 1:
        raise SystemExit("MAX_ATTEMPTS must be >= 1.")
    if html_prompt_limit < 1000:
        raise SystemExit("HTML_PROMPT_LIMIT must be >= 1000.")
    if output_limit_bytes < 1024:
        raise SystemExit("EXTRACTOR_OUTPUT_LIMIT_BYTES must be >= 1024.")

    target_url = CONTRACT["target_url"]
    oracle_url = CONTRACT["oracle_url"]
    wait_for = CONTRACT["rendering"]["wait_for"]

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    snapshot_harness(run_dir)
    (run_dir / "developer_instructions.txt").write_text(
        DEVELOPER_INSTRUCTIONS,
        encoding="utf-8",
    )
    _write_json(run_dir / "runtime_meta.json", runtime_metadata(model))

    print(f"[fetch/raw] {target_url}")
    try:
        raw_initial_bytes, raw_fetch_meta = fetch_with_scrapingbee(
            target_url,
            sb_key,
            render_js=False,
        )
    except requests.RequestException as exc:
        write_error(run_dir / "raw_fetch_error.json", exc)
        print(f"[fetch/raw] ERROR: {exc}")
        print(f"[done] evidence saved to {run_dir}")
        return 2

    raw_initial_html, raw_encoding = decode_html(
        raw_initial_bytes,
        raw_fetch_meta.get("content_type"),
    )
    raw_fetch_meta["normalized_encoding"] = raw_encoding
    raw_fetch_meta["raw_bytes"] = len(raw_initial_bytes)
    raw_fetch_meta["normalized_characters"] = len(raw_initial_html)
    raw_dom_meta = inspect_unrendered_page(raw_initial_html)

    (run_dir / "page.initial.raw.html").write_bytes(raw_initial_bytes)
    (run_dir / "page.initial.html").write_text(raw_initial_html, encoding="utf-8")
    _write_json(run_dir / "fetch_initial_meta.json", raw_fetch_meta)
    _write_json(run_dir / "initial_dom_meta.json", raw_dom_meta)

    print(
        "[fetch/raw] ok"
        f" status={raw_fetch_meta['status_code']}"
        f" credits={raw_fetch_meta['scrapingbee_cost']}"
        f" product_cards={raw_dom_meta['product_card_count']}"
    )

    print(f"[oracle] {oracle_url}")
    try:
        oracle_json, oracle_fetch_meta = fetch_oracle(oracle_url)
        reference = build_reference_payload(oracle_json)
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        write_error(run_dir / "oracle_error.json", exc)
        print(f"[oracle] ERROR: {exc}")
        print(f"[done] evidence saved to {run_dir}")
        return 2

    _write_json(run_dir / "oracle_response.json", oracle_json)
    _write_json(run_dir / "oracle_fetch_meta.json", oracle_fetch_meta)
    print(f"[oracle] ok reference_items={len(reference)}")

    print(f"[fetch/js] {target_url}")
    try:
        rendered_bytes, rendered_fetch_meta = fetch_with_scrapingbee(
            target_url,
            sb_key,
            render_js=True,
            wait_for=wait_for,
        )
    except requests.RequestException as exc:
        write_error(run_dir / "rendered_fetch_error.json", exc)
        print(f"[fetch/js] ERROR: {exc}")
        print(f"[done] evidence saved to {run_dir}")
        return 2

    rendered_html, rendered_encoding = decode_html(
        rendered_bytes,
        rendered_fetch_meta.get("content_type"),
    )
    rendered_fetch_meta["normalized_encoding"] = rendered_encoding
    rendered_fetch_meta["raw_bytes"] = len(rendered_bytes)
    rendered_fetch_meta["normalized_characters"] = len(rendered_html)

    try:
        rendered_dom_meta = validate_rendered_page(rendered_html, reference)
    except ValueError as exc:
        write_error(
            run_dir / "rendered_dom_error.json",
            exc,
            extra={"wait_for": wait_for},
        )
        (run_dir / "page.rendered.raw.html").write_bytes(rendered_bytes)
        (run_dir / "page.rendered.html").write_text(rendered_html, encoding="utf-8")
        _write_json(run_dir / "fetch_rendered_meta.json", rendered_fetch_meta)
        print(f"[fetch/js] HARNESS ERROR: {exc}")
        print(f"[done] evidence saved to {run_dir}")
        return 2

    (run_dir / "page.rendered.raw.html").write_bytes(rendered_bytes)
    (run_dir / "page.rendered.html").write_text(rendered_html, encoding="utf-8")
    _write_json(run_dir / "fetch_rendered_meta.json", rendered_fetch_meta)
    _write_json(run_dir / "rendered_dom_meta.json", rendered_dom_meta)

    initial_cost = _cost_as_int(raw_fetch_meta.get("scrapingbee_cost"))
    rendered_cost = _cost_as_int(rendered_fetch_meta.get("scrapingbee_cost"))
    total_cost = (
        initial_cost + rendered_cost
        if initial_cost is not None and rendered_cost is not None
        else None
    )

    fetch_summary = {
        "initial_html_product_cards": raw_dom_meta["product_card_count"],
        "rendered_html_product_cards": rendered_dom_meta["rendered_product_cards"],
        "oracle_product_count": len(reference),
        "wait_for": wait_for,
        "initial_fetch_credits": initial_cost,
        "rendered_fetch_credits": rendered_cost,
        "scrapingbee_total_credits": total_cost,
    }
    _write_json(run_dir / "fetch_comparison.json", fetch_summary)

    print(
        "[fetch/js] ok"
        f" status={rendered_fetch_meta['status_code']}"
        f" credits={rendered_fetch_meta['scrapingbee_cost']}"
        f" request_id={rendered_fetch_meta['scrapingbee_request_id']}"
        f" product_cards={rendered_dom_meta['rendered_product_cards']}"
        f" data_ready={rendered_dom_meta['data_ready']}"
    )

    prompt_html, prompt_html_truncated = html_for_prompt(
        rendered_html,
        html_prompt_limit,
    )
    _write_json(
        run_dir / "prompt_context_meta.json",
        {
            "full_rendered_html_characters": len(rendered_html),
            "prompt_html_characters": len(prompt_html),
            "html_prompt_limit": html_prompt_limit,
            "truncated": prompt_html_truncated,
            "oracle_url_exposed_to_model": False,
            "unrendered_html_exposed_to_model": False,
        },
    )

    client = OpenAI(api_key=openai_key)
    previous_code: str | None = None
    feedback: dict[str, Any] | None = None

    for attempt in range(1, max_attempts + 1):
        attempt_dir = run_dir / f"attempt_{attempt:02d}"
        attempt_dir.mkdir()

        if attempt == 1:
            prompt = initial_prompt(
                target_url=target_url,
                contract=CONTRACT,
                html_context=prompt_html,
            )
        else:
            assert previous_code is not None
            assert feedback is not None
            prompt = refine_prompt(
                contract=CONTRACT,
                previous_code=previous_code,
                stdout=feedback["stdout"],
                stderr=feedback["stderr"],
                verification_feedback=feedback["verification_feedback"],
                html_context=prompt_html,
            )

        (attempt_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

        print(f"[generate] attempt {attempt}")
        try:
            code, llm_meta, llm_response_json = call_llm(client, model, prompt)
        except Exception as exc:
            write_error(attempt_dir / "generation_error.json", exc)
            print(f"[generate] ERROR: {exc}")
            print(f"[done] evidence saved to {run_dir}")
            return 2

        (attempt_dir / "extractor.py").write_text(code, encoding="utf-8")
        _write_json(attempt_dir / "llm_meta.json", llm_meta)
        (attempt_dir / "llm_response.json").write_text(
            llm_response_json,
            encoding="utf-8",
        )

        if previous_code is not None:
            (attempt_dir / "diff.patch").write_text(
                make_diff(previous_code, code),
                encoding="utf-8",
            )

        preflight_errors = static_preflight(code)
        execution_meta: dict[str, Any] = {
            "preflight_passed": not preflight_errors,
        }

        if preflight_errors:
            stdout = ""
            stderr = "\n".join(preflight_errors)
            returncode = 126
            verification = {
                "passed": False,
                "harness_error": False,
                "phase": "preflight",
                "errors": preflight_errors,
                "feedback": preflight_errors,
                "warnings": [
                    "Static preflight is a policy check, not a security sandbox."
                ],
                "stats": {},
            }
        else:
            print(f"[execute] attempt {attempt}")
            stdout, stderr, returncode, capture_meta = execute_extractor(
                code,
                rendered_html,
                target_url,
                output_limit_bytes,
            )
            execution_meta.update(capture_meta)

            if capture_meta["stdout_truncated"]:
                message = (
                    "Extractor stdout exceeded the harness output limit of "
                    f"{output_limit_bytes} bytes."
                )
                verification = {
                    "passed": False,
                    "harness_error": False,
                    "errors": [message],
                    "feedback": [message],
                    "warnings": [],
                    "stats": {},
                }
            else:
                _, verification = verify_stdout(
                    stdout=stdout,
                    contract=CONTRACT,
                    reference=reference,
                )

            if capture_meta["stderr_truncated"]:
                verification.setdefault("warnings", []).append(
                    f"Extractor stderr exceeded {output_limit_bytes} bytes and was truncated."
                )

            if returncode != 0:
                message = f"Extractor exited with code {returncode}."
                verification["passed"] = False
                verification.setdefault("errors", []).insert(0, message)
                verification.setdefault("feedback", []).insert(0, message)

        execution_meta["returncode"] = returncode
        (attempt_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
        (attempt_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
        _write_json(attempt_dir / "execution.json", execution_meta)
        _write_json(attempt_dir / "verification.json", verification)

        if verification.get("harness_error"):
            print("[verify] HARNESS ERROR")
            for error in verification["errors"][:8]:
                print(f"  - {error}")
            print(f"[done] evidence saved to {run_dir}")
            return 2

        if verification["passed"]:
            print(f"[verify] PASS on attempt {attempt}")
            print(f"[done] evidence saved to {run_dir}")
            return 0

        print(f"[verify] FAIL on attempt {attempt}")
        for error in verification["errors"][:8]:
            print(f"  - {error}")

        feedback = {
            "stdout": stdout,
            "stderr": stderr,
            "verification_feedback": feedback_for_model(verification),
        }
        previous_code = code

    print(f"[done] retry limit hit after {max_attempts} attempts")
    print(f"[done] evidence saved to {run_dir}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
