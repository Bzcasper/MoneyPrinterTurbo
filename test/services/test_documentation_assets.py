import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def test_documentation_sponsor_images_resolve():
    root = Path(__file__).resolve().parents[2]
    references = []
    pattern = re.compile(r"(?:\.{2}/)*(?:docs/)?sponsors/[^\s\"'<>)]*")
    documents = [*root.glob("README*"), *(root / "docs").rglob("*.md")]
    for document in documents:
        if not document.is_file():
            continue
        with document.open(encoding="utf-8") as source:
            for line in source:
                for match in pattern.finditer(line):
                    reference = match.group()
                    base = root if reference.startswith("docs/") else document.parent
                    references.append((document.relative_to(root), reference))
                    assert (base / reference).is_file(), references[-1]
    assert references, "No sponsor image references were checked"


def test_firefly_webhook_examples_use_localhost():
    root = Path(__file__).resolve().parents[2]
    samples = []
    pattern = re.compile(r"https?://[^\s\"'<>]+/webhook/firefly-provider-generate")
    for path in (root / "config.example.toml", root / "webui/Main.py"):
        with path.open(encoding="utf-8") as source:
            urls = [match.group() for line in source for match in pattern.finditer(line)]
        assert urls, path.name
        samples.extend((path.name, url) for url in urls)
    for locale in (root / "webui/i18n").glob("*.json"):
        messages = json.loads(locale.read_text(encoding="utf-8"))["Translation"]
        match = pattern.search(messages["Firefly Webhook URL Help"])
        assert match, locale.name
        samples.append((locale.name, match.group()))
    for name, url in samples:
        parsed = urlsplit(url)
        assert parsed.hostname == "localhost", name
        assert parsed.port == 5678, name
