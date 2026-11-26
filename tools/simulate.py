"""跑模拟，校准经济曲线。用法：

    python tools/simulate.py [--days 40] [--orders 25]

输出：各车辆的日均收入、超时率、差评率、以及买车/换车需要几天。
纯 stdlib，不需要在 venv 里跑（但用 venv 的 python 也行）。
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import game  # noqa: E402
from core.game import LEVELS  # noqa: E402


def simulate_day(p: game.Player, rng: random.Random, weather: str,
                 max_orders: int, meals: bool = True) -> dict:
    """模拟玩家跑一天：接单 → 结算 → 体力不够就吃饭 → 到上限收工。"""
    taken = late = bad = 0
    gross = 0
    peak_flags = []
    while True:
        if game.can_take_orders(p) is not None:
            if meals and p.today_meals < game.MEAL_PER_DAY and p.money >= game.MEAL_PRICE:
                p.money -= game.MEAL_PRICE
                p.today_meals += 1
                p.stamina = min(game.STAMINA_MAX, p.stamina + game.MEAL_STAMINA)
                continue
            break
        if taken >= max_orders:
            break
        peak = rng.random() < 0.4
        peak_flags.append(peak)
        orders = game.make_orders(p, weather, peak, rng)
        o = rng.choice(orders)
        st = game.settle(p, o, weather, peak, 0, rng)
        taken += 1
        if not st.on_time:
            late += 1
        if st.bad_review:
            bad += st.bad_review
        gross += st.income
        # 车况见底就花钱修（真人也会这么干），维修费直接从当日净收入里扣
        if game.needs_repair(p) and p.money >= game.repair_price(p):
            fee = game.repair_price(p)
            p.money -= fee
            p.durability = float(game.DURABILITY_MAX)
            p.sick = 0
            gross -= fee
        # 每次接单消耗一点时间，身体恢复不重要（一天里体力是主要瓶颈）
    return {"orders": taken, "late": late, "bad": bad, "gross": gross,
            "peak_ratio": (sum(peak_flags) / len(peak_flags)) if peak_flags else 0.0}


def run(vehicle_idx: int, days: int, orders_per_day: int, seed: int) -> dict:
    rng = random.Random(seed)
    p = game.Player(uid="sim", name="sim", created=0)
    p.vehicle = vehicle_idx
    incomes, late_rates, bad_rates = [], [], []
    for d in range(days):
        p.day = ""          # 强制跨天重置
        game.roll_day(p, 1700000000 + d * 86400)
        p.stamina = game.STAMINA_MAX
        p.stamina_at = 1700000000 + d * 86400
        weather = game.roll_weather(f"grp{d}")
        r = simulate_day(p, rng, weather, orders_per_day)
        incomes.append(r["gross"])
        if r["orders"]:
            late_rates.append(r["late"] / r["orders"])
            bad_rates.append(r["bad"] / r["orders"])
    v = game.VEHICLES[vehicle_idx]
    avg = statistics.mean(incomes)
    return {
        "vehicle": v["name"], "price": v["price"], "avg_income": avg,
        "avg_orders": statistics.mean([1 for _ in incomes]),
        "late": statistics.mean(late_rates) if late_rates else 0,
        "bad": statistics.mean(bad_rates) if bad_rates else 0,
        "payback_days": (v["price"] / avg) if avg > 0 else float("inf"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--orders", type=int, default=25)
    args = ap.parse_args()

    print(f"每车 {args.days} 天 × 每日 {args.orders} 单上限\n")
    print(f"{'车辆':<10}{'车价':>7}{'日均收入':>10}{'超时率':>9}{'差评率':>9}"
          f"{'车价/日收':>11}")
    print("-" * 58)
    results = []
    for i in range(len(game.VEHICLES)):
        r = run(i, args.days, args.orders, 20261124 + i)
        results.append(r)
        print(f"{r['vehicle']:<10}{r['price']:>7.0f}{r['avg_income']:>10.1f}"
              f"{r['late']*100:>8.1f}%{r['bad']*100:>8.1f}%"
              f"{r['payback_days']:>10.1f}天")

    print()
    print("攒钱节奏（按当前车的日收入算「攒够目标车价要几天」，不做以旧换新）：")
    by_id = {r["vehicle"]: r for r in results}

    def days_to(cur: str, target: str) -> float:
        return by_id[target]["price"] / by_id[cur]["avg_income"]

    routes = {
        "路线A 求稳": ["共享单车", "电动三轮车", "电动车", "老头乐", "电动小货车", "小汽车"],
        "路线B 求快": ["共享单车", "电动滑板车", "电动三轮车", "电动车", "摩托车", "性能车"],
    }
    for name, chain in routes.items():
        total = 0.0
        parts = []
        for a, b in zip(chain, chain[1:]):
            d = days_to(a, b)
            total += d
            parts.append(f"{b} {d:.0f}天")
        print(f"  {name}：{' → '.join(parts)}　合计 {total:.0f} 天到小汽车")

    print()
    print("电车之后两条路：多花的钱多久赚回来：")
    eb = by_id["电动车"]
    for key in ("老头乐", "摩托车"):
        r = by_id[key]
        gap = r["avg_income"] - eb["avg_income"]
        extra = r["price"] - eb["price"]
        print(f"  电动车 → {key}：多花 {extra:.0f} 元、日均多赚 {gap:.0f} 元"
              f" ⇒ 回本约 {extra / gap:.0f} 天" if gap > 0 else f"  电动车 → {key}：不划算")

    print()
    print("周收入（7 天，与等级门槛对照）：")
    for r in results:
        print(f"  {r['vehicle']:<10} ≈ {r['avg_income']*7:.0f} 元/周  "
              f"（" + " / ".join(f"{lv['name']} {lv['weekly']}" for lv in LEVELS) + "）")


if __name__ == "__main__":
    main()