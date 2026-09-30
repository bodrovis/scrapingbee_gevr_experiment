import argparse
import json
import re
import sys

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


def is_hidden(node, boundary):
    current = node.parent
    while isinstance(current, Tag):
        if current.name in {"script", "style", "template", "noscript"}:
            return True
        if current.has_attr("hidden") or current.get("aria-hidden") == "true":
            return True
        if current.name == "input" and current.get("type", "").lower() == "hidden":
            return True
        style = current.get("style", "").lower()
        if re.search(r"(?:^|;)\s*display\s*:\s*none\b", style):
            return True
        if re.search(r"(?:^|;)\s*visibility\s*:\s*(?:hidden|collapse)\b", style):
            return True
        if current is boundary:
            break
        current = current.parent
    return False


def visible_text(element):
    fragments = []
    for node in element.descendants:
        if isinstance(node, NavigableString) and not isinstance(node, Comment):
            if not is_hidden(node, element):
                text = re.sub(r"\s+", " ", str(node)).strip()
                if text:
                    fragments.append(text)
    return re.sub(r"\s+", " ", " ".join(fragments)).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source_url")
    parser.parse_args()

    soup = BeautifulSoup(sys.stdin.read(), "html.parser")
    records = [
        {"position": position, "text": visible_text(item)}
        for position, item in enumerate(soup.select(".feed-item"), start=1)
    ]
    print(json.dumps(records, ensure_ascii=False))


if __name__ == "__main__":
    main()