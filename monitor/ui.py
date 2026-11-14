from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import Bounds


BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
RISK_WORDS = ("验证码", "安全验证", "请完成验证", "操作过于频繁", "账号存在风险", "滑动验证")


class UiTree:
    def __init__(self, root: ET.Element):
        self.root = root
        self.parent = {child: node for node in root.iter() for child in node}

    @classmethod
    def from_file(cls, path: Path) -> "UiTree":
        return cls(ET.fromstring(path.read_bytes()))

    def nodes(self) -> list[ET.Element]:
        return list(self.root.iter())

    @staticmethod
    def text(node: ET.Element) -> str:
        return (node.get("text") or "").strip()

    @staticmethod
    def desc(node: ET.Element) -> str:
        return (node.get("content-desc") or "").strip()

    @staticmethod
    def label(node: ET.Element) -> str:
        return " ".join(part for part in (UiTree.text(node), UiTree.desc(node)) if part)

    @staticmethod
    def bounds(node: ET.Element) -> Bounds | None:
        match = BOUNDS_RE.fullmatch(node.get("bounds") or "")
        if not match:
            return None
        return Bounds(*(int(value) for value in match.groups()))

    def find(self, *, text: str | None = None, resource_id: str | None = None) -> list[ET.Element]:
        result = []
        for node in self.root.iter():
            if text is not None and self.text(node) != text:
                continue
            if resource_id is not None and node.get("resource-id") != resource_id:
                continue
            result.append(node)
        return result

    def nearest_clickable(self, node: ET.Element) -> ET.Element | None:
        current: ET.Element | None = node
        while current is not None:
            if current.get("clickable") == "true" and self.bounds(current):
                return current
            current = self.parent.get(current)
        return None

    def descendant_labels(self, node: ET.Element) -> list[str]:
        return [self.label(child) for child in node.iter() if self.label(child)]

    def page_text(self) -> str:
        return "\n".join(self.label(node) for node in self.root.iter() if self.label(node))

    def risk_reason(self) -> str | None:
        page = self.page_text()
        return next((word for word in RISK_WORDS if word in page), None)

