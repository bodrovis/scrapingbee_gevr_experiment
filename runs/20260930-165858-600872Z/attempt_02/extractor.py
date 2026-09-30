import json
import re
import sys

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


def is_hidden(node, boundary):
    current = node.parent
    while isinstance(current, Tag):
        if current.name in {"script", "style", "template", "noscript"}:
            return True
        if current.has_attr("hidden"):
            return True
        if str(current.get("aria-hidden", "")).strip().lower() == "true":
            return True
        if current.name == "input" and str(current.get("type", "")).lower() == "hidden":
            return True

        style = str(current.get("style", "")).lower()
        if re.search(r"(?:^|;)\s*display\s*:\s*none(?:\s*!important)?\s*(?:;|$)", style):
            return True
        if re.search(
            r"(?:^|;)\s*visibility\s*:\s*(?:hidden|collapse)(?:\s*!important)?\s*(?:;|$)",
            style,
        ):
            return True

        if current is boundary:
            break
        current = current.parent
    return False


def visible_text(element):
    parts = []
    for node in element.descendants:
        if isinstance(node, NavigableString) and not isinstance(node, Comment):
            if not is_hidden(node, element):
                text = re.sub(r"\s+", " ", str(node)).strip()
                if text:
                    parts.append(text)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def main():
    if len(sys.argv) != 2:
        raise SystemExit(2)

    source_url = sys.argv[1]
    soup = BeautifulSoup(sys.stdin.read(), "html.parser")

    records = []
    for position, item in enumerate(soup.select(".feed-item"), 1):
        records.append({
            "position": position,
            "text": visible_text(item),
        })

    print(json.dumps(records, ensure_ascii=False))


if __name__ == "__main__":
    main()