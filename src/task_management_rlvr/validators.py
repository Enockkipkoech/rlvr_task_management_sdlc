"""Parse and validate the structured implementation artifact a model returns.

Expected reply shape::

    <implementation>
    <file path="app/security.py">
    ...complete file contents...
    </file>
    </implementation>

Rules (deterministic, no LLM judge):
* only files the task owns may be written; anything else is a violation and is
  ignored (it is never copied into the sandbox);
* paths are POSIX, relative, normalised; no traversal, no duplicates;
* each file is at most MAX_FILE_BYTES;
* surrounding prose is tolerated, but at most one <implementation> block counts
  (the last complete one).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

MAX_FILE_BYTES = 120_000

_BLOCK = re.compile(r"<implementation>(.*?)</implementation>", re.DOTALL)
_FILE = re.compile(r'<file\s+path\s*=\s*"([^"]+)"\s*>(.*?)</file>', re.DOTALL)
_FENCE = re.compile(r"\A\s*```[A-Za-z0-9_+-]*\n(.*?)\n?```\s*\Z", re.DOTALL)


@dataclass
class ParsedArtifact:
    files: dict[str, str] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    found_block: bool = False

    @property
    def usable(self) -> bool:
        """True when at least one owned file was extracted."""
        return bool(self.files)

    @property
    def clean(self) -> bool:
        return self.found_block and not self.violations


def reply_text(completion) -> str:
    """Best-effort conversion of a Verifiers reply (str / message / list) to text."""
    if completion is None:
        return ""
    if isinstance(completion, str):
        return completion
    if isinstance(completion, dict):
        return str(completion.get("content", ""))
    if isinstance(completion, list):
        return reply_text(completion[-1]) if completion else ""
    content = getattr(completion, "content", None)
    return content if isinstance(content, str) else str(completion)


def normalise_path(raw: str) -> str | None:
    path = raw.strip().replace("\\", "/")
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return None
    norm = posixpath.normpath(path)
    if norm.startswith("..") or "/../" in f"/{norm}/" or norm == ".":
        return None
    return norm


def _strip_fence(content: str) -> str:
    match = _FENCE.match(content)
    body = match.group(1) if match else content
    return body.strip("\n") + "\n"


def parse_artifact(completion, owned_files: set[str] | frozenset[str]) -> ParsedArtifact:
    text = reply_text(completion)
    result = ParsedArtifact()
    blocks = _BLOCK.findall(text)
    if not blocks:
        return result
    result.found_block = True
    seen: set[str] = set()
    for raw_path, content in _FILE.findall(blocks[-1]):
        path = normalise_path(raw_path)
        if path is None:
            result.violations.append(f"unsafe path: {raw_path!r}")
            continue
        if path not in owned_files:
            result.violations.append(f"file not owned by this task: {path}")
            continue
        if path in seen:
            result.violations.append(f"duplicate file: {path}")
        seen.add(path)
        body = _strip_fence(content)
        if len(body.encode("utf-8")) > MAX_FILE_BYTES:
            result.violations.append(f"file too large: {path}")
            continue
        if not body.strip():
            result.violations.append(f"empty file: {path}")
            continue
        result.files[path] = body
    return result


def render_artifact(files: dict[str, str]) -> str:
    """Build a well-formed reply from a {path: content} mapping (used for gold/mutants)."""
    parts = ["<implementation>"]
    for path, content in files.items():
        parts.append(f'<file path="{path}">\n{content.rstrip(chr(10))}\n</file>')
    parts.append("</implementation>")
    return "\n".join(parts)
