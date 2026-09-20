#!/usr/bin/env python3
"""平台封印 CLI —— 构建/校验/锁只读。

用法（在 shipin-platform 根目录）：
    python tools/platform_integrity.py --build      # 构建 config/integrity.json
    python tools/platform_integrity.py --verify      # 校验封印（默认人对人）
    python tools/platform_integrity.py --verify --json   # 机器可读
    python tools/platform_integrity.py --lock        # src/config/tools 置只读
    python tools/platform_integrity.py --unlock      # 恢复可写

约束：构建只能由平台所有者执行（备份后重封有审计痕迹）；对外暴露的只有
--verify（API / MCP 除外）——AI 没有任何入口能改封印。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from shipin_platform import integrity  # noqa: E402


def _verdict_text(rep: dict) -> str:
    if rep.get("ok"):
        return (f"[封印通过] {rep['clean']} 个文件与清单一致"
                f"（sealed @ {integrity.MANIFEST_PATH}）")
    lines = [f"[封印 FAIL] clean={rep.get('clean', 0)} "
             f"tampered={rep.get('tampered', 0)} "
             f"missing={rep.get('missing', 0)}"]
    if rep.get("reason") == "MANIFEST_MISSING":
        lines.append(f"  清单不存在：{integrity.MANIFEST_PATH}（先 --build）")
    elif rep.get("reason") == "SELF_TAMPERED":
        lines.append("  清单自身被改动（self 封条不符）")
    else:
        for f in rep.get("files", []):
            if f["status"] != "OK":
                lines.append(f"  [{f['status']}] {f['path']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="shipin-platform 完整性封印 CLI")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--build", action="store_true", help="构建封印清单并写盘")
    g.add_argument("--verify", action="store_true", help="校验封印")
    g.add_argument("--lock", action="store_true", help="受保护文件置只读")
    g.add_argument("--unlock", action="store_true", help="恢复可写")
    ap.add_argument("--json", action="store_true", help="输出 machine-readable JSON")
    args = ap.parse_args()

    if args.build:
        rep = integrity.build_manifest(manifest_path=integrity.MANIFEST_PATH)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False))
        else:
            print(f"[封印] 已构建 {integrity.MANIFEST_PATH}："
                  f"{len(rep['files'])} 个文件，self={rep['self'][:12]}…")
        return 0

    if args.verify:
        rep = integrity.verify()
        if args.json:
            print(json.dumps(rep, ensure_ascii=False))
        else:
            print(_verdict_text(rep))
        return 0 if rep.get("ok") else 1

    rep = integrity.set_lock(on=args.lock)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False))
    else:
        kind = "只读" if args.lock else "可写"
        print(f"[封印] {kind}：{rep['files']} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())