import re
from pathlib import Path


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
