from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import LicenseStore


def main() -> int:
    parser = argparse.ArgumentParser(description="价差监控授权管理")
    parser.add_argument("--database", type=Path, default=Path("license-data/licenses.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="批量生成链动小铺发货激活码")
    generate.add_argument("--days", type=int, choices=(1, 30, 90, 365), required=True)
    generate.add_argument("--count", type=int, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--batch", default="")

    commands.add_parser("list", help="列出最近授权")
    for name in ("revoke", "enable", "unbind"):
        command = commands.add_parser(name)
        command.add_argument("--license-id", type=int, required=True)
    extend = commands.add_parser("extend")
    extend.add_argument("--license-id", type=int, required=True)
    extend.add_argument("--days", type=int, required=True)

    args = parser.parse_args()
    store = LicenseStore(args.database)
    if args.command == "generate":
        codes = store.create_codes(args.days, args.count, args.batch)
        store.export_codes(args.output, codes, args.days)
        print(f"已生成 {len(codes)} 个 {args.days} 天激活码：{args.output.resolve()}")
    elif args.command == "list":
        print(json.dumps(store.list_licenses(), ensure_ascii=False, indent=2))
    elif args.command == "revoke":
        store.set_enabled(args.license_id, False)
        print("授权已停用")
    elif args.command == "enable":
        store.set_enabled(args.license_id, True)
        print("授权已恢复")
    elif args.command == "extend":
        print(f"新到期时间：{store.extend(args.license_id, args.days)}")
    elif args.command == "unbind":
        code, days = store.unbind(args.license_id)
        print(f"原设备已停用；补发 {days} 天迁移码：{code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
