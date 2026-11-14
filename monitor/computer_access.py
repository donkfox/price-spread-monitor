from __future__ import annotations

import json
import os
import re
import shutil
import threading
import zipfile
import csv
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree


ACCESS_MODES = {"none", "read", "manage", "full"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".jsonl", ".log", ".ini", ".cfg",
    ".yaml", ".yml", ".xml", ".html", ".css", ".js", ".ts", ".py",
    ".java", ".c", ".cpp", ".h", ".hpp", ".sql", ".ps1", ".bat",
}
ANALYZABLE_EXTENSIONS = {".xlsx", ".csv"}


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


class ComputerAccessManager:
    def __init__(self, root: Path, runtime: Path, settings: dict, logger):
        self.root = root.resolve()
        self.runtime = runtime.resolve()
        self.settings = settings
        self.log = logger
        self.state_path = self.root / str(
            settings.get("state_file", "runtime/computer-access.json")
        )
        self.audit_path = self.root / str(
            settings.get("audit_file", "runtime/computer-access-audit.jsonl")
        )
        self.recycle_root = self.root / str(
            settings.get("recycle_dir", "runtime/computer-recycle")
        )
        self._lock = threading.RLock()
        self._state = self._load_state()

    @staticmethod
    def _default_state() -> dict[str, object]:
        return {
            "mode": "none",
            "fallback_mode": "none",
            "allowed_roots": [],
            "full_until": "",
            "updated_at": _iso(),
        }

    def _load_state(self) -> dict[str, object]:
        state = self._default_state()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                state.update(payload)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        mode = str(state.get("mode", "none"))
        state["mode"] = mode if mode in ACCESS_MODES else "none"
        fallback = str(state.get("fallback_mode", "none"))
        state["fallback_mode"] = fallback if fallback in {"none", "read", "manage"} else "none"
        roots = state.get("allowed_roots", [])
        state["allowed_roots"] = [str(value) for value in roots if str(value).strip()] if isinstance(roots, list) else []
        return state

    def _save_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def _audit(self, action: str, *, target: str = "", result: str = "ok", detail: str = "") -> None:
        entry = {
            "created_at": _iso(),
            "action": action[:80],
            "target": target[:1000],
            "result": result[:40],
            "detail": detail[:500],
        }
        try:
            with self._lock:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as error:
            self.log.warning("电脑权限审计写入失败：%s", error)

    def _expire_full_locked(self) -> None:
        if self._state.get("mode") != "full":
            return
        try:
            deadline = datetime.fromisoformat(str(self._state.get("full_until", "")))
        except ValueError:
            deadline = _now() - timedelta(seconds=1)
        if deadline > _now():
            return
        self._state["mode"] = self._state.get("fallback_mode", "none")
        self._state["full_until"] = ""
        self._state["updated_at"] = _iso()
        self._save_locked()
        self._audit("full_access_expired")

    def status(self) -> dict[str, object]:
        with self._lock:
            self._expire_full_locked()
            mode = str(self._state["mode"])
            return {
                "mode": mode,
                "allowed_roots": list(self._state["allowed_roots"]),
                "full_until": str(self._state.get("full_until", "")),
                "can_read": mode in {"read", "manage", "full"},
                "can_manage": mode in {"manage", "full"},
                "full_control": mode == "full",
                "updated_at": str(self._state.get("updated_at", "")),
            }

    def set_mode(self, mode: str, *, confirmed: bool = False, minutes: int = 30) -> dict[str, object]:
        value = str(mode).strip().lower()
        if value not in ACCESS_MODES:
            raise ValueError("不支持的电脑权限模式")
        if value != "none" and confirmed is not True:
            raise PermissionError("请在权限确认框中点击确认")
        with self._lock:
            self._expire_full_locked()
            if value == "full":
                duration = max(5, min(int(minutes), 120))
                fallback = str(self._state.get("mode", "none"))
                if fallback == "full":
                    fallback = str(self._state.get("fallback_mode", "none"))
                self._state["fallback_mode"] = fallback
                self._state["full_until"] = _iso(_now() + timedelta(minutes=duration))
            else:
                self._state["fallback_mode"] = value
                self._state["full_until"] = ""
            self._state["mode"] = value
            self._state["updated_at"] = _iso()
            self._save_locked()
        self._audit("set_mode", target=value, detail=f"minutes={minutes}")
        return self.status()

    def revoke(self) -> dict[str, object]:
        return self.set_mode("none")

    def grant_root(self, path: str, *, confirmed: bool = False) -> dict[str, object]:
        if confirmed is not True:
            raise PermissionError("请在文件夹授权确认框中点击确认")
        target = Path(path).expanduser().resolve(strict=True)
        if not target.is_dir():
            raise ValueError("授权路径必须是存在的文件夹")
        with self._lock:
            roots = [Path(value) for value in self._state["allowed_roots"]]
            if target not in roots:
                roots.append(target)
            self._state["allowed_roots"] = [str(value) for value in roots]
            self._state["updated_at"] = _iso()
            self._save_locked()
        self._audit("grant_root", target=str(target))
        return self.status()

    def revoke_root(self, path: str) -> dict[str, object]:
        target = Path(path).expanduser().resolve(strict=False)
        with self._lock:
            self._state["allowed_roots"] = [
                value
                for value in self._state["allowed_roots"]
                if Path(value).resolve(strict=False) != target
            ]
            self._state["updated_at"] = _iso()
            self._save_locked()
        self._audit("revoke_root", target=str(target))
        return self.status()

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    def _assert_allowed(self, path: Path, *, write: bool = False) -> Path:
        target = path.expanduser().resolve(strict=False)
        status = self.status()
        mode = str(status["mode"])
        if mode == "none" or (write and mode == "read"):
            raise PermissionError("当前电脑权限不足")
        if mode == "full":
            return target
        roots = [Path(value).resolve(strict=False) for value in status["allowed_roots"]]
        if not any(self._inside(target, root) for root in roots):
            raise PermissionError("这个路径没有获得主人授权")
        return target

    @staticmethod
    def _entry(path: Path) -> dict[str, object]:
        stat = path.stat()
        return {
            "name": path.name,
            "path": str(path),
            "kind": "folder" if path.is_dir() else "file",
            "suffix": path.suffix.lower() if path.is_file() else "",
            "size": stat.st_size if path.is_file() else 0,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        }

    def list_directory(self, path: str = "") -> dict[str, object]:
        if not path:
            status = self.status()
            if status["mode"] == "full":
                target = Path.home().resolve()
            elif status["allowed_roots"]:
                target = Path(str(status["allowed_roots"][0])).resolve()
            else:
                raise PermissionError("还没有授权任何文件夹")
        else:
            target = Path(path)
        target = self._assert_allowed(target)
        if not target.is_dir():
            raise ValueError("目标不是文件夹")
        entries = []
        for item in target.iterdir():
            try:
                entries.append(self._entry(item))
            except OSError:
                continue
        entries.sort(key=lambda item: (item["kind"] != "folder", str(item["name"]).casefold()))
        self._audit("list_directory", target=str(target), detail=f"items={len(entries)}")
        return {"path": str(target), "entries": entries[:500]}

    def search(self, query: str, path: str = "", limit: int = 50) -> dict[str, object]:
        keyword = str(query).strip().casefold()
        if not keyword:
            raise ValueError("搜索关键词不能为空")
        status = self.status()
        if path:
            roots = [self._assert_allowed(Path(path))]
        elif status["mode"] == "full":
            roots = [Path.home().resolve()]
        else:
            roots = [Path(value).resolve() for value in status["allowed_roots"]]
        if not roots:
            raise PermissionError("还没有授权任何文件夹")
        maximum = max(1, min(int(limit), 100))
        scanned = 0
        max_entries = int(self.settings.get("max_search_entries", 30000))
        results = []
        for root in roots:
            for current, directories, files in os.walk(root, followlinks=False):
                directories[:] = [name for name in directories if not name.startswith(".")]
                for name in [*directories, *files]:
                    scanned += 1
                    if keyword in name.casefold():
                        try:
                            results.append(self._entry(Path(current) / name))
                        except OSError:
                            pass
                    if len(results) >= maximum or scanned >= max_entries:
                        break
                if len(results) >= maximum or scanned >= max_entries:
                    break
            if len(results) >= maximum or scanned >= max_entries:
                break
        self._audit("search", target=path, detail=f"query={query[:80]}; results={len(results)}")
        return {"query": query, "results": results, "scanned": scanned, "truncated": scanned >= max_entries}

    def inspect_file(self, path: str) -> dict[str, object]:
        target = self._assert_allowed(Path(path))
        if not target.is_file():
            raise ValueError("目标不是文件")
        result = self._entry(target)
        result["preview"] = self._preview(target)
        self._audit("inspect_file", target=str(target))
        return result

    @staticmethod
    def _number(value: object) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float, Decimal)):
            return Decimal(str(value))
        text = str(value).strip().replace(",", "")
        text = re.sub(r"(?:人民币|RMB|CNY|¥|￥|元)", "", text, flags=re.IGNORECASE)
        match = re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text.strip())
        if not match:
            return None
        try:
            return Decimal(match.group())
        except InvalidOperation:
            return None

    @staticmethod
    def _field_for_header(value: object) -> str:
        header = re.sub(r"\s+", "", str(value or "")).lower()
        if not header:
            return ""
        if any(word in header for word in ("尺码", "规格", "码数", "容量", "颜色")):
            return "specification"
        if "利润率" in header or "毛利率" in header:
            return ""
        if any(word in header for word in ("已实现利润", "实际利润", "实收利润")):
            return "realized_profit"
        if any(word in header for word in ("预计利润", "预估利润")):
            return "expected_profit"
        if any(word in header for word in ("利润", "毛利")):
            return "profit"
        if any(word in header for word in ("运费", "快递费", "物流费")):
            return "shipping"
        if any(word in header for word in ("成本", "进货价", "采购价", "买入价")):
            return "cost"
        if any(word in header for word in ("已售流水", "销售额", "总流水", "流水")):
            return "revenue"
        if any(word in header for word in ("到手价", "售价", "卖出价", "预计收入", "预估收入")):
            return "sale_income"
        return ""

    def _analyze_rows(self, rows, source_name: str, maximum_rows: int) -> dict[str, object]:
        buffered = []
        for row in rows:
            values = list(row)
            if any(value not in (None, "") for value in values):
                buffered.append(values)
            if len(buffered) >= maximum_rows + 10:
                break
        if not buffered:
            return {"source": source_name, "rows": 0, "totals": {}, "specifications": []}

        best_index = 0
        best_fields: dict[int, str] = {}
        for index, row in enumerate(buffered[:10]):
            fields = {
                column: field
                for column, value in enumerate(row)
                if (field := self._field_for_header(value))
            }
            if len(fields) > len(best_fields):
                best_index = index
                best_fields = fields
        if not best_fields:
            return {"source": source_name, "rows": 0, "totals": {}, "specifications": []}

        totals: dict[str, Decimal] = {}
        specifications: set[str] = set()
        data_rows = 0
        for row in buffered[best_index + 1:best_index + 1 + maximum_rows]:
            first_text = "".join(str(value or "") for value in row[:2])
            if any(label in first_text for label in ("合计", "总计", "汇总")):
                continue
            row_values: dict[str, Decimal] = {}
            has_data = False
            for column, field in best_fields.items():
                value = row[column] if column < len(row) else None
                if field == "specification":
                    specification = str(value or "").strip()
                    if specification:
                        specifications.add(specification)
                        has_data = True
                    continue
                number = self._number(value)
                if number is not None:
                    row_values[field] = number
                    totals[field] = totals.get(field, Decimal("0")) + number
                    has_data = True
            if not has_data:
                continue
            data_rows += 1
            if not any(key in row_values for key in ("profit", "expected_profit", "realized_profit")):
                if "sale_income" in row_values and "cost" in row_values:
                    derived = row_values["sale_income"] - row_values["cost"] - row_values.get("shipping", Decimal("0"))
                    totals["derived_profit"] = totals.get("derived_profit", Decimal("0")) + derived
        return {
            "source": source_name,
            "rows": data_rows,
            "totals": {key: str(value.quantize(Decimal("0.01"))) for key, value in totals.items()},
            "specifications": sorted(specifications)[:80],
        }

    def _analyze_file(self, path: Path, maximum_rows: int) -> list[dict[str, object]]:
        if path.suffix.lower() == ".xlsx":
            from openpyxl import load_workbook
            workbook = load_workbook(path, read_only=True, data_only=True)
            results = []
            try:
                for sheet in workbook.worksheets[:10]:
                    result = self._analyze_rows(
                        sheet.iter_rows(values_only=True),
                        f"{path.name}/{sheet.title}",
                        maximum_rows,
                    )
                    if result["rows"]:
                        results.append(result)
            finally:
                workbook.close()
            return results
        if path.suffix.lower() == ".csv":
            rows = None
            for encoding in ("utf-8-sig", "gb18030"):
                try:
                    with path.open("r", encoding=encoding, newline="") as source:
                        rows = list(csv.reader(source))
                    break
                except UnicodeDecodeError:
                    continue
            return [self._analyze_rows(rows or [], path.name, maximum_rows)]
        return []

    def analyze_path(self, path: str) -> dict[str, object]:
        target = self._assert_allowed(Path(path))
        maximum_files = max(1, min(int(self.settings.get("max_analysis_files", 30)), 100))
        maximum_rows = max(50, min(int(self.settings.get("max_analysis_rows_per_sheet", 5000)), 20000))
        if target.is_file():
            files = [target] if target.suffix.lower() in ANALYZABLE_EXTENSIONS else []
        elif target.is_dir():
            files = []
            for current, directories, names in os.walk(target, followlinks=False):
                directories[:] = [name for name in directories if not name.startswith(".")]
                for name in names:
                    candidate = Path(current) / name
                    if candidate.suffix.lower() in ANALYZABLE_EXTENSIONS:
                        files.append(self._assert_allowed(candidate))
                        if len(files) >= maximum_files:
                            break
                if len(files) >= maximum_files:
                    break
        else:
            raise FileNotFoundError("要分析的路径不存在")
        if not files:
            raise ValueError("这个位置没有可分析的 Excel 或 CSV 文件")

        sections = []
        for file in files:
            try:
                sections.extend(self._analyze_file(file, maximum_rows))
            except (OSError, ValueError, zipfile.BadZipFile) as error:
                self.log.warning("跳过无法分析的文件 %s：%s", file, error)
        totals: dict[str, Decimal] = {}
        specifications: set[str] = set()
        rows = 0
        for section in sections:
            rows += int(section["rows"])
            specifications.update(str(value) for value in section["specifications"])
            for key, value in section["totals"].items():
                totals[key] = totals.get(key, Decimal("0")) + Decimal(str(value))
        self._audit("analyze_path", target=str(target), detail=f"files={len(files)}; rows={rows}")
        return {
            "path": str(target),
            "files": len(files),
            "sheets": len(sections),
            "rows": rows,
            "totals": {key: str(value.quantize(Decimal("0.01"))) for key, value in totals.items()},
            "specifications": sorted(specifications)[:100],
            "truncated": len(files) >= maximum_files,
        }

    def _preview(self, path: Path) -> dict[str, object]:
        suffix = path.suffix.lower()
        max_chars = int(self.settings.get("max_preview_characters", 20000))
        try:
            if suffix in TEXT_EXTENSIONS:
                max_bytes = max(4096, max_chars * 4)
                with path.open("rb") as source:
                    data = source.read(max_bytes + 1)
                text = data[:max_bytes].decode("utf-8-sig", errors="replace")
                return {
                    "type": "text",
                    "content": text[:max_chars],
                    "truncated": len(data) > max_bytes or len(text) > max_chars,
                }
            max_file_bytes = int(self.settings.get("max_preview_file_bytes", 25 * 1024 * 1024))
            if path.stat().st_size > max_file_bytes:
                return {
                    "type": "too_large",
                    "content": "",
                    "error": "文件过大，只显示基本信息",
                }
            if suffix == ".docx":
                with zipfile.ZipFile(path) as archive:
                    document_info = archive.getinfo("word/document.xml")
                    if document_info.file_size > max_file_bytes:
                        return {"type": "too_large", "content": "", "error": "文档内容过大"}
                    xml = archive.read("word/document.xml")
                root = ElementTree.fromstring(xml)
                text = "\n".join(value.strip() for value in root.itertext() if value.strip())
                return {"type": "document", "content": text[:max_chars], "truncated": len(text) > max_chars}
            if suffix == ".xlsx":
                from openpyxl import load_workbook
                workbook = load_workbook(path, read_only=True, data_only=True)
                rows = []
                for sheet in workbook.worksheets[:5]:
                    rows.append(f"[{sheet.title}]")
                    for row in sheet.iter_rows(max_row=80, values_only=True):
                        rows.append("\t".join("" if value is None else str(value) for value in row[:20]))
                workbook.close()
                text = "\n".join(rows)
                return {"type": "spreadsheet", "content": text[:max_chars], "truncated": len(text) > max_chars}
            if suffix == ".pdf":
                from pypdf import PdfReader
                reader = PdfReader(path)
                text = "\n".join((page.extract_text() or "") for page in reader.pages[:12])
                return {"type": "pdf", "content": text[:max_chars], "truncated": len(reader.pages) > 12 or len(text) > max_chars}
            if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
                from PIL import Image
                with Image.open(path) as image:
                    return {"type": "image", "width": image.width, "height": image.height, "format": image.format}
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
            return {"type": "unreadable", "error": str(error)[:200]}
        return {"type": "binary", "content": ""}

    def perform_action(self, payload: dict[str, object]) -> dict[str, object]:
        if payload.get("confirmed") is not True:
            raise PermissionError("请在文件操作确认框中点击确认")
        action = str(payload.get("action", "")).strip().lower()
        source_text = str(payload.get("path", "")).strip()
        if not source_text:
            raise ValueError("文件操作路径不能为空")
        source = self._assert_allowed(Path(source_text), write=True)
        try:
            if action == "mkdir":
                source.mkdir(parents=False, exist_ok=False)
                result = self._entry(source)
            elif action == "write_text":
                content = str(payload.get("content", ""))
                if len(content.encode("utf-8")) > 1024 * 1024:
                    raise ValueError("单次写入内容不能超过 1MB")
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(content, encoding="utf-8")
                result = self._entry(source)
            elif action == "move":
                destination_text = str(payload.get("destination", "")).strip()
                if not destination_text:
                    raise ValueError("移动目标不能为空")
                destination = self._assert_allowed(Path(destination_text), write=True)
                if destination.exists():
                    raise FileExistsError("移动目标已经存在")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                result = self._entry(destination)
            elif action == "recycle":
                if not source.exists():
                    raise FileNotFoundError("要回收的文件不存在")
                self.recycle_root.mkdir(parents=True, exist_ok=True)
                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", source.name)[:100] or "item"
                destination = self.recycle_root / f"{_now().strftime('%Y%m%d-%H%M%S-%f')}-{safe_name}"
                shutil.move(str(source), str(destination))
                result = {"recycled_from": str(source), "recycle_path": str(destination)}
            else:
                raise ValueError("不支持的文件操作")
        except Exception as error:
            self._audit(action or "unknown_action", target=str(source), result="error", detail=str(error))
            raise
        self._audit(action, target=str(source))
        return {"action": action, "result": result}

    def recent_audit(self, limit: int = 50) -> list[dict[str, object]]:
        maximum = max(1, min(int(limit), 200))
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()[-maximum:]
        except FileNotFoundError:
            return []
        entries = []
        for line in reversed(lines):
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    entries.append(value)
            except json.JSONDecodeError:
                continue
        return entries
