"""成本记账 —— 平台每次真实生成都登记到项目台账 cost.json。

平台级约束：总成本不再是摆设（TotalCost=0.0 的旧病），每次
成功调用外部生成（image/video/tts）都按 providers.json 的
"pricing" 段计价落账；读到 /api/pipeline/{id}/report 供审计。
定价是平台配置（config/providers.json），不是代码写死；缺省回退
PRICING_DEFAULTS（占位价，运营项）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from shipin_platform import roots

import os
import threading
import uuid

ROOT = roots.data_root()
PROVIDERS_PATH = ROOT / "config" / "providers.json"

# 轮43:账本进程内锁——异步任务 ThreadPoolExecutor 并发 record_cost,
# 旧代码读-改-写无锁,后写覆盖先写(少计费,且两次都真实花了钱)
_LEDGER_LOCK = threading.Lock()

# 平台运营侧缺省价（USD）；真实价格在 config/providers.json 的 pricing 段，
# 这里只保证"任何环境都有价可记"。
PRICING_DEFAULTS = {
    "image": {"usd_per_unit": 0.001},
    "video": {"usd_per_sec": 0.02},
    "tts": {"usd_per_unit": 0.0},
    # 轮42:LLM(文本生成/审片)按次估算入账——此前 text 阶段每阶段最多
    # 6 轮 ×(生成/修复/审片)的付费调用完全不在账内,预算数字只覆盖媒体
    # 生成、与真实账单长期对不上(七审 #2)。estimate 可被 providers.json
    # 的 pricing.llm 覆盖。
    "llm": {"usd_per_unit": 0.002},
}

_pricing_cache: Optional[dict] = None


def pricing() -> dict:
    """读取 providers.json 的 pricing 段（未配则回退缺省）。"""
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache
    cfg = {k: dict(v) for k, v in PRICING_DEFAULTS.items()}
    try:
        data = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
        p = data.get("pricing")
        if isinstance(p, dict):
            for kind, price in p.items():
                if isinstance(price, dict):
                    cfg.setdefault(kind, {}).update(price)
                elif isinstance(price, (int, float)):
                    cfg.setdefault(kind, {})["usd_per_unit"] = float(price)
    except Exception:
        pass  # 配置缺失/损坏 → 缺省价（本地占位不花钱）
    _pricing_cache = cfg
    return _pricing_cache


def unit_usd(kind: str) -> float:
    """单次计价：image/tts 按条，video 按时长秒。"""
    p = pricing().get(kind, {})
    if not isinstance(p, dict):
        return 0.0
    return float(p.get("usd_per_unit", p.get("usd_per_sec", 0.0)))


def cost_file(project_id: str) -> Path:
    d = ROOT / "data" / "projects" / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "cost.json"


class LedgerCorruptError(RuntimeError):
    """轮43:成本账本损坏——调用方必须显式处理,不得静默按空账放行。"""


def _load_rows(project_id: str) -> list[dict]:
    """轮43:损坏不再静默当空账——截断的 cost.json(磁盘满/进程被杀写在
    半路)旧代码返回 [],预算闸判定"没花钱"重新放行,历史账目无声消失
    (七审 #5:账本清零=给预算闸开后门)。现在:备份损坏文件 + 记
    ledger_corrupt 事件 + **抛 LedgerCorruptError** 让调用方显式失败。
    进程内锁见 record_cost。

    轮49(九审 P3-9):「合法 JSON 但结构不是 list」(dict/str/num)同样
    抛——旧代码 `rows if isinstance(rows, list) else []` 对结构损坏
    静默返空账,与 JSONDecodeError 的处理自相矛盾(七审 #5 的设计意图
    是任何损坏都不空账)。"""
    fp = cost_file(project_id)
    if not fp.exists():
        return []
    try:
        rows = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        try:
            bak = fp.with_suffix(f".corrupt-{int(time.time())}.json")
            fp.replace(bak)
        except OSError:
            bak = fp
        raise LedgerCorruptError(
            f"成本账本损坏已备份({bak.name}): {type(e).__name__}——"
            f"拒绝按空账继续(那会让预算闸重新放行);请人工核对后删除备份或修复")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        try:
            bak = fp.with_suffix(f".corrupt-{int(time.time())}.json")
            fp.replace(bak)
        except OSError:
            bak = fp
        raise LedgerCorruptError(
            f"成本账本结构损坏已备份({bak.name}): "
            f"{type(rows).__name__} 非 list[dict]——拒绝按空账继续")
    return rows


def record_cost(project_id: str, kind: str, *,
                model: str = "", units: float = 1.0, note: str = "") -> dict:
    """登记一笔生成成本并落账。kind: image / video / tts / llm。

    轮43(七审 #5):读-改-写全程加进程内锁(异步任务 ThreadPoolExecutor
    并发 record_cost 会丢行),写改 mkstemp+os.replace 原子写(截断式
    write_text 在半路被杀会留下损坏 JSON→旧 _load_rows 静默清零)。

    轮49(九审 P1-3):units 钳到 [0, 1e6]——负 units 会把 total_usd 拉低,
    `used > max` 的预算判据被反向掏空(裸端点曾可注入负 duration 持续
    放行超预算项目,伪造账目方向)。钳制是纵深防御:调用方仍应校验入参。"""
    with _LEDGER_LOCK:
        units = min(max(float(units), 0.0), 1_000_000.0)
        usd = round(unit_usd(kind) * units, 6)
        rows = _load_rows(project_id)
        row = {
            "seq": (rows[-1]["seq"] + 1) if rows else 1,
            "ts": round(time.time(), 3),
            "kind": kind,
            "model": model,
            "units": units,
            "usd": usd,
            "note": note,
        }
        rows.append(row)
        fp = cost_file(project_id)
        tmp = fp.with_suffix(".tmp-" + uuid.uuid4().hex[:8])
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, fp)
        return row


def cost_summary(project_id: str) -> dict:
    """项目成本快照：总额 + 分 kind 汇总 + 明细。未产生成本也返回空骨架。"""
    rows = _load_rows(project_id)
    by_kind: dict[str, float] = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0.0) + float(r["usd"])
    return {
        "total_usd": round(sum(float(r["usd"]) for r in rows), 6),
        "by_kind": {k: round(v, 6) for k, v in by_kind.items()},
        "records": rows,
    }


# ── P3 全局成本（OpenCost 式：跨项目聚合 + 时段窗口 + showback）──

GLOBAL_BUDGET_PATH = ROOT / "config" / "global_budget.json"


def read_global_budget() -> dict:
    """全局月度预算：{max_monthly_usd, updated_at, updated_by}。未配置 → 空。"""
    try:
        data = json.loads(GLOBAL_BUDGET_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def set_global_budget(max_monthly_usd: float, caller: str = "admin") -> dict:
    """设置全局月度预算（admin）。None/负值 → 清除（解除全局硬闸）。"""
    GLOBAL_BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if max_monthly_usd is None or float(max_monthly_usd) < 0:
        GLOBAL_BUDGET_PATH.write_text("{}", encoding="utf-8")
        return {}
    cfg = read_global_budget()
    cfg["max_monthly_usd"] = round(float(max_monthly_usd), 6)
    cfg["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    cfg["updated_by"] = caller or "admin"
    GLOBAL_BUDGET_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return cfg


def _month_key(ts: float) -> str:
    return time.strftime("%Y-%m", time.localtime(ts))


def _day_key(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def global_cost_summary() -> dict:
    """平台级聚合（OpenCost allocation 思想——同一台账按 项目×时段 切分）：
    全项目总额 / 本月 / 近 7 日 / top 项目。"""
    rows_by_project: dict[str, list[dict]] = {}
    projects_dir = ROOT / "data" / "projects"
    if projects_dir.is_dir():
        for child in sorted(projects_dir.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            fp = child / "cost.json"
            if fp.is_file():
                try:
                    rows = json.loads(fp.read_text(encoding="utf-8"))
                    if isinstance(rows, list):
                        rows_by_project[child.name] = rows
                except (json.JSONDecodeError, OSError):
                    pass
    now = time.time()
    month = _month_key(now)
    totals: dict[str, float] = {}
    month_usd, week_usd = 0.0, 0.0
    daily: dict[str, float] = {}
    for pid, rows in rows_by_project.items():
        per = 0.0
        for r in rows:
            usd = float(r.get("usd", 0.0))
            per += usd
            t = float(r.get("ts", 0.0)) if r.get("ts") is not None else 0.0
            if t and _month_key(t) == month:
                month_usd += usd
            if t and now - t <= 7 * 86400:
                week_usd += usd
                dk = _day_key(t)
                daily[dk] = daily.get(dk, 0.0) + usd
        totals[pid] = round(per, 6)
    top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:8]
    return {
        "projects": len(totals),
        "total_usd": round(sum(totals.values()), 6),
        "month_usd": round(month_usd, 6),
        "week_usd": round(week_usd, 6),
        "daily_7d": [{"date": k, "usd": round(v, 6)}
                     for k, v in sorted(daily.items())],
        "top_projects": [{"project_id": pid, "usd": usd}
                         for pid, usd in top],
        "global_budget": read_global_budget(),
    }


def global_budget_exceeded() -> tuple[bool, float, float]:
    """全局硬闸判定：本月已用 > 上限 → (True, used, max)。无上限 → False。"""
    cfg = read_global_budget()
    mx = cfg.get("max_monthly_usd")
    if mx is None:
        return False, 0.0, 0.0
    used = global_cost_summary()["month_usd"]
    return used > float(mx), used, float(mx)


__all__ = ["PRICING_DEFAULTS", "pricing", "unit_usd", "record_cost",
           "cost_summary", "cost_file", "read_global_budget",
           "set_global_budget", "global_cost_summary",
           "global_budget_exceeded"]