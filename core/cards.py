"""把游戏状态拼成「卡片描述」（spec），交给 core/art.py 画成图片。

每个函数对应一张群内卡片，参数与 core/render.py 里同名的文本卡一致 ——
渲染失败时 main.py 会退回 render.py 那份纯文字，所以两边内容要说得一致。
"""
from __future__ import annotations

from .game import (
    DAY_ORDER_CAP, DURABILITY_MAX, DURABILITY_WARN, GEARS, LEVELS, MEAL_PER_DAY,
    MEAL_PRICE, MEAL_STAMINA, STAMINA_MAX, VEHICLES, WEATHERS, level_name,
    next_level_need, order_income_hint, rating_bucket, repair_price, risk_label,
    task_progress, vehicle_of, Player, Order, Settlement,
)
from .render import BAD_LABEL, BUCKET_TEXT, yuan

RISK_TONE = {"低": "good", "中": "warn", "高": "bad"}


def _weather_chips(weather: str, peak: bool, alert: dict | None) -> list[tuple[str, str]]:
    w = WEATHERS.get(weather, WEATHERS["sunny"])
    chips = [(f"{w['emoji']} {w['name']}天", "info")]
    if w.get("bonus"):
        chips.append((f"补贴 +{int(w['bonus'] * 100)}%", "good"))
    if peak:
        chips.append(("⏰ 高峰", "warn"))
    if alert:
        chips.append((f"{alert['emoji']} {alert['name']} +{int(round(float(alert.get('subsidy') or 0) * 100))}%", "bad"))
    return chips


def rider_chips(p: Player) -> list[tuple[str, str]]:
    v = vehicle_of(p)
    return [
        (f"{v['emoji']} {v['name']}", "accent"),
        (f"⭐ {p.rating:.2f}", "good" if p.rating >= 4.5 else ("warn" if p.rating >= 4.0 else "bad")),
        (f"⚡ {int(p.stamina)}/{STAMINA_MAX}", "info" if p.stamina >= 30 else "bad"),
        (f"💰 {int(p.money)} 元", "accent"),
    ]


# ------------------------------------------------------------------ 帮助
_HELP_GROUPS = [
    ("跑单", [
        ("#接单", "派 3 张单，挑一张跑"),
        ("#接 1", "接第 1 张（也有 #接 2 / #接 3）"),
        ("#跳过", "这一批不要，等下一批（体力 −2）"),
    ]),
    ("账本", [
        ("#我的", "看车、评分、体力、余额、段位"),
        ("#收入榜", "今天的群内排名"),
        ("#周榜", "这一周谁跑得最多"),
        ("#总榜", "职业生涯累计谁赚得多"),
    ]),
    ("群里一起玩", [
        ("#任务", "今天的三件事，达成自动发钱"),
        ("#押 名字 金额", "押他进今日前三，九点开奖，押中赔一倍"),
        ("#打赏 名字 金额", "给跑得辛苦的群友塞点钱"),
    ]),
    ("生意", [
        ("#抢单", "限量单 30–60 元，全群先到先得"),
        ("#商店", "买车买装备（#买 2 下单）"),
        ("#吃饭", f"{MEAL_PRICE} 元 +{MEAL_STAMINA} 体力，每天 {MEAL_PER_DAY} 次"),
        ("#修车", "缺多少修多少，车越烂越贵"),
        ("#签到", "每天一次，连签越久给得越多"),
        ("#抄近道", "白银以上每天 1 次，下一单格外顺"),
    ]),
]


def help_spec() -> dict:
    blocks = []
    for title, rows_ in _HELP_GROUPS:
        items = [{"icon": "▸", "text": cmd, "meta": desc, "tone": "muted"}
                 for cmd, desc in rows_]
        blocks.append({"kind": "section", "title": title, "icon": "•"})
        blocks.append({"kind": "list", "items": items})
    blocks.append({
        "kind": "note",
        "tone": "warn",
        "text": "超时按分钟扣钱：先扣这一单配送费的 15%，每多迟到一分钟再加 5%，最多扣光不欠账。"
                "配送失败没有配送费，还要赔餐费 8–25 元。一天最多 30 单，体力每 6 分钟回 1 点。",
    })
    blocks.append({
        "kind": "note",
        "tone": "info",
        "text": "全群同一天气；偶尔雷暴、大风、大雾，平台临时加价，钱多路也难走。"
                "段位按本周收入升：青铜 → 白银 → 黄金 → 钻石 → 星耀 → 王者。",
    })
    return {
        "emoji": "🛵",
        "eyebrow": "风里雨里 · 外卖骑手",
        "title": "群内小游戏",
        "subtitle": "开车送单、攒钱换车，跑得多的人钱多，跑得稳的人评分高",
        "blocks": blocks,
        "footer": "所有指令都带 # 前缀；不认识哪个，随时 #帮助。",
    }


# ------------------------------------------------------------------ 派单
def dispatch_spec(p: Player, orders: list[Order], weather: str, peak: bool,
                  alert: dict | None = None) -> dict:
    items = []
    for o in orders:
        prob = order_income_hint(p, o, weather, peak)
        risk = risk_label(prob)
        pills = [
            (f"{o.km}km", "info"),
            (f"{o.pay} 元", "accent"),
            (f"{o.floor} 楼 {'有电梯' if o.elevator else '无电梯'}", "muted"),
            (f"限时 {o.limit} 分", "warn" if o.limit < 40 else "muted"),
            (f"风险 {risk}", RISK_TONE.get(risk, "muted")),
        ]
        if o.slow:
            pills.append(("🍳 出餐慢", "bad"))
        items.append({
            "badge": f"单 {o.idx}",
            "accent": "warn" if o.premium else "accent",
            "route": f"{o.shop_emoji} {o.shop} → {o.dest}",
            "who": f"👤 {o.who}" if o.who else "",
            "item": f"{o.shop_emoji} {o.item}" + ("　📦 大件" if o.bulk else "") if o.item else "",
            "note": f"📝 {o.note}" if o.note else "",
            "pills": pills,
        })
    blocks = [
        {"kind": "section", "title": "手上三张单", "icon": "📦"},
        {"kind": "orders", "items": items},
    ]
    if p.stamina < 30:
        blocks.append({"kind": "note", "tone": "bad",
                       "text": f"体力只剩 {int(p.stamina)}，再跑容易出事——先 #吃饭 补 40 点。"})
    return {
        "emoji": "🛵",
        "title": f"派单 · 今日第 {p.today_orders + 1} 单",
        "subtitle": "挑一张跑：距离越远钱越多，楼层、限时、天气都会影响这一单顺不顺",
        "chips": _weather_chips(weather, peak, alert) + rider_chips(p),
        "blocks": blocks,
        "footer": "#接 1 / #接 2 / #接 3 　或者 #跳过 等下一批",
    }


# ------------------------------------------------------------------ 结算
def settle_spec(p: Player, st: Settlement, weather: str, peak: bool,
                story: dict | None = None) -> dict:
    o = st.order
    v = vehicle_of(p)
    story = story or {}
    if st.failed:
        verdict, tone = "配送失败", "bad"
        timing = f"折腾 {st.actual_min:.1f} 分，这单没送到"
    elif st.on_time:
        verdict, tone = "准时送达", "good"
        timing = f"用时 {st.actual_min:.1f} 分 ｜ 限时 {o.limit} 分"
    else:
        verdict, tone = f"超时 {st.late_min:.1f} 分", "warn"
        timing = f"用时 {st.actual_min:.1f} 分 ｜ 限时 {o.limit} 分 ｜ 罚 {abs(st.delivery_fee)} 元"

    detail = []
    if st.failed:
        detail.append(("赔餐费", f"−{st.compensate}", "bad"))
    elif st.delivery_fee > 0:
        detail.append(("配送费", f"+{st.delivery_fee}", "good"))
    else:
        detail.append(("超时扣", f"−{abs(st.delivery_fee)}", "bad"))
    if st.tip:
        detail.append(("小费", f"+{st.tip}", "good"))
    if st.cost:
        detail.append(("损耗", f"−{st.cost:g}", "muted"))
    if st.bonus:
        if st.bonus < 0:
            detail.append(("赔给顾客", f"−{abs(st.bonus)}", "bad"))
        else:
            detail.append(("补贴", f"+{st.bonus}", "good"))
    if st.fine:
        detail.append(("罚款", f"−{st.fine}", "bad"))
    if st.task_bonus:
        detail.append(("任务奖励", f"+{st.task_bonus}", "good"))

    blocks = [
        {"kind": "section", "title": "这一单", "icon": "🧾"},
        {"kind": "rows", "items": [
            {"k": "行程", "v": f"{o.shop_emoji} {o.shop} → {o.dest}（{o.km}km）", "tone": "accent"},
            {"k": "结果", "v": verdict, "tone": tone},
            {"k": "时间", "v": timing, "tone": "muted"},
        ]},
        {"kind": "table", "head": ["项目", "金额"], "rows": [[k, val] for k, val, _ in detail]},
    ]

    if st.bad_review:
        why = BAD_LABEL.get(st.bad_kind, "")
        blocks.append({"kind": "note", "tone": "bad",
                       "text": f"💢 差评 +{st.bad_review}{'（' + why + '）' if why else ''}"
                               f"　今日 {min(st.today_bad, 4)}/4，满 4 个扣当日收入 15%"})
    events = [e for e in st.events if not (e.startswith("❌ 超时") or e.startswith("⏱")
                                          or e.startswith("🚫 配送失败"))]
    if events:
        blocks.append({"kind": "section", "title": "路上", "icon": "🛣"})
        blocks.append({"kind": "list", "items": [{"icon": "·", "text": e} for e in events]})
    if st.failed and st.fail_reason:
        blocks.append({"kind": "note", "tone": "bad", "text": f"🚫 {st.fail_reason}"})

    lines = []
    if story.get("merchant"):
        lines.append(f"🏪 商家：{story['merchant']}" + ("　→ 出餐慢了，算在限时里" if o.slow else ""))
    if story.get("late") and not (st.failed and st.fail_reason):
        prefix = "🚫 没送到" if st.failed else ("🛣 路上" if st.on_time else "🕳 路上")
        lines.append(f"{prefix}：{story['late']}")
    if story.get("chat"):
        lines.append(f"📞 {o.who or '顾客'}：「{story['chat']}」")
    if story.get("cust"):
        lines.append(f"💬 {o.who or '顾客'}：「{story['cust']}」")
    if lines:
        blocks.append({"kind": "section", "title": "这一单的经过", "icon": "📖"})
        blocks.append({"kind": "list", "items": [{"icon": "·", "text": t} for t in lines]})

    blocks.append({"kind": "bars", "items": [
        {"label": "体力", "cur": st.stamina_after, "max": STAMINA_MAX,
         "note": f"{int(st.stamina_after)}/{STAMINA_MAX}",
         "color": "#ff8b3d" if st.stamina_after >= 30 else "#ff6b6b"},
    ]})
    blocks.append({"kind": "rows", "items": [
        {"k": "余额", "v": f"{int(st.money_after)} 元", "tone": "accent"},
        {"k": "今日", "v": f"{st.today_orders} 单 {yuan(st.today_income)} 元", "tone": "info"},
        {"k": "本周", "v": f"{yuan(p.week_income)} 元", "tone": "purple"},
        {"k": "车况", "v": f"{st.durability_after:g}/{int(DURABILITY_MAX)}（车损 −{st.wear}）",
         "tone": "bad" if st.durability_after < DURABILITY_WARN else "muted"},
    ]})

    income = int(st.income)
    hero_emoji = "💸" if income >= 0 else "🩹"
    return {
        "emoji": "🛵",
        "title": f"{o.shop_emoji} {o.shop} → {o.dest}",
        "subtitle": f"{v['emoji']} {v['name']}" + (f" ｜ 收件人 {o.who}" if o.who else ""),
        "hero": {"emoji": hero_emoji, "text": f"{yuan(income)} 元 ｜ {verdict}",
                 "sub": timing},
        "blocks": blocks,
        "footer": "车况见底就 #修车；体力不够就 #吃饭。",
    }


# ------------------------------------------------------------------ 我的
def status_spec(p: Player, weather: str, peak: bool) -> dict:
    v = vehicle_of(p)
    nxt = next_level_need(p)
    blocks = [
        {"kind": "bars", "items": [
            {"label": "体力", "cur": p.stamina, "max": STAMINA_MAX,
             "note": f"{int(p.stamina)}/{STAMINA_MAX}",
             "color": "#ff8b3d" if p.stamina >= 30 else "#ff6b6b"},
            {"label": "车况", "cur": max(0.0, p.durability), "max": DURABILITY_MAX,
             "note": f"{p.durability:g}/{int(DURABILITY_MAX)} ｜ 修车 {repair_price(p)} 元",
             "color": "#3ddc84" if p.durability >= DURABILITY_WARN else "#ffcc55"},
        ]},
        {"kind": "section", "title": "账本", "icon": "💰"},
        {"kind": "rows", "items": [
            {"k": "余额", "v": f"{int(p.money)} 元", "tone": "accent"},
            {"k": "今日", "v": f"{p.today_orders}/{DAY_ORDER_CAP} 单 {yuan(p.today_income)} 元", "tone": "info"},
            {"k": "本周收入", "v": f"{yuan(p.week_income)} 元", "tone": "purple"},
            {"k": "职业生涯", "v": f"{yuan(p.total_income)} 元", "tone": "good"},
            {"k": "评分", "v": f"{p.rating:.2f}（{BUCKET_TEXT[rating_bucket(p)]}）", "tone": "good"},
            {"k": "累计单量", "v": f"{p.orders} 单", "tone": "muted"},
            {"k": "差评", "v": f"{min(p.today_bad, 4)}/4", "tone": "bad" if p.today_bad else "muted"},
        ]},
        {"kind": "section", "title": "段位", "icon": "🏅"},
        {"kind": "list", "items": [{
            "icon": "🏆",
            "text": (f"距 {LEVELS[p.level + 1]['name']} 还差 {nxt[0]} 元（门槛 {nxt[1]}）"
                     if nxt else f"已是王者，本周 {yuan(p.week_income)} 元"),
            "meta": level_name(p), "tone": "accent",
        }]},
    ]

    perks = []
    if v.get("bulk"):
        perks.append("能拉大件（+8 元）")
    if v.get("meal_p"):
        perks.append("接公司团餐")
    if v.get("weather_immune"):
        perks.append("有棚，雨雪不加超时")
    if v.get("peak_extra"):
        perks.append("四轮堵车，高峰更慢")
    if v.get("accident"):
        perks.append("可能翻车")
    if v.get("police"):
        perks.append("怕交警")
    if v.get("puncture"):
        perks.append("会扎胎")
    if v.get("wear", 1.0) >= 1.2:
        perks.append("跑起来费车")
    if perks:
        blocks.append({"kind": "note", "tone": "warn", "text": "🚦 车辆脾气：" + "、".join(perks)})

    gear = [f"{g['emoji']} {g['name']}" for g in GEARS if g["id"] in p.gear]
    blocks.append({"kind": "section", "title": "装备与签到", "icon": "🎒"})
    blocks.append({"kind": "list", "items": [
        {"icon": "🎒", "text": "、".join(gear) if gear else "还什么都没有，去 #商店 看看"},
        {"icon": "📅", "text": f"连签 {p.checkin_streak} 天 ｜ #签到 领钱（连签越久给越多）", "tone": "good"},
    ]})
    return {
        "emoji": "🛵",
        "eyebrow": f"{level_name(p)}骑手",
        "title": p.name or "骑手",
        "subtitle": f"{v['emoji']} {v['name']}",
        "chips": _weather_chips(weather, peak, None),
        "blocks": blocks,
        "footer": "今日上限 30 单，体力每 6 分钟回 1 点。",
    }


# ------------------------------------------------------------------ 榜单
def leaderboard_spec(day: str, players: list[Player], limit: int = 10,
                     by: str = "day") -> dict:
    if by == "week":
        title, unit, tone = "本周收入榜", "周收入，周一清零", "purple"
    elif by == "total":
        title, unit, tone = "总榜", "职业生涯累计", "accent"
    else:
        title, unit, tone = "今日收入榜", day, "accent"
    items = []
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, p in enumerate(players[:limit], start=1):
        v = vehicle_of(p)
        if by == "week":
            money, sub = p.week_income, f"⭐{p.rating:.2f} ｜ {v['emoji']} {v['name']}"
        elif by == "total":
            money, sub = p.total_income, f"⭐{p.rating:.2f} ｜ {v['emoji']} {v['name']}"
        else:
            money, sub = p.today_income, f"{p.today_orders} 单 ｜ ⭐{p.rating:.2f} ｜ {v['emoji']} {v['name']}"
        items.append({
            "medal": medals.get(i, f"{i}"),
            "pos": i,
            "name": p.name or f"骑手{i}",
            "sub": sub,
            "value": f"{yuan(money)} 元",
            "tone": "accent" if i <= 3 else "muted",
        })
    blocks = []
    if not items:
        blocks.append({"kind": "note", "tone": "muted",
                       "text": "这周还没人出车。" if by == "week"
                               else ("还没人跑过单。" if by == "total" else "今天还没人出车。")})
    else:
        blocks.append({"kind": "rank", "items": items})
    return {
        "emoji": "🏆",
        "eyebrow": unit,
        "title": title,
        "blocks": blocks,
        "footer": ("明天早上再排一次，跑得多的人钱多，跑得稳的人评分高。"
                   if by == "day" else "跑到哪一天，这一栏都算数。"),
    }


# ------------------------------------------------------------------ 任务
def tasks_spec(p: Player) -> dict:
    tasks = [t for t in p.tasks if isinstance(t, dict)] if isinstance(p.tasks, list) else []
    claimed = set(x for x in (p.tasks_claimed or []) if isinstance(x, str))
    items = []
    for i, t in enumerate(tasks, start=1):
        done = t.get("id") in claimed
        item = {"icon": "✅" if done else "⬜", "text": str(t.get("text") or ""),
                "meta": f"+{int(t.get('reward') or 0)} 元", "tone": "good" if done else "accent"}
        if not done:
            cur, need = task_progress(t, p)
            cur = max(0.0, float(cur))
            ratio = 0.0 if need <= 0 else max(0.0, min(1.0, cur / float(need)))
            item["bar"] = {"ratio": ratio, "tone": "accent"}
            item["text"] = f"{item['text']}　{cur:.0f}/{need}"
        items.append(item)
    blocks = []
    if items:
        blocks.append({"kind": "list", "items": items})
        left = [t for t in tasks if t.get("id") not in claimed]
        blocks.append({"kind": "note", "tone": "good" if not left else "info",
                       "text": "今天三个任务全清了，厉害。" if not left
                               else "达成了下一单结算时自动发钱，不用手领。"})
    else:
        blocks.append({"kind": "note", "tone": "muted",
                       "text": "今天还没派任务，先 #接单 跑一单，任务就来了。"})
    return {
        "emoji": "📋",
        "eyebrow": p.day or "今天",
        "title": "今日任务",
        "blocks": blocks,
        "footer": "任务每天随天气重排；差评、超时都会影响达成。",
    }


# ------------------------------------------------------------------ 商店
def shop_spec(p: Player, page: str = "car") -> dict:
    v = vehicle_of(p)
    left = max(0, MEAL_PER_DAY - p.today_meals)
    if page == "gear":
        rows = []
        for i, g in enumerate(GEARS, start=len(VEHICLES) + 1):
            owned = g["id"] in p.gear
            rows.append([str(i), f"{g['emoji']} {g['name']}", "已拥有" if owned else f"{g['price']} 元"])
        return {
            "emoji": "🏪", "eyebrow": f"💰 {int(p.money)} 元",
            "title": "补给站 · 装备",
            "chips": [(f"今天还能 #吃饭 {left} 次", "info")],
            "blocks": [{"kind": "table", "head": ["#", "装备", "价格"], "rows": rows}],
            "footer": "#买 编号 下单 ｜ #商店 回车 ｜ " + f"{MEAL_PRICE} 元 #吃饭 +{MEAL_STAMINA} 体力",
        }
    rows = []
    for i, veh in enumerate(VEHICLES, start=1):
        if i - 1 == p.vehicle:
            price = "当前"
        elif i - 1 < p.vehicle:
            price = "已拥有"
        else:
            price = f"{veh['price']} 元"
        rows.append([str(i), f"{veh['emoji']} {veh['name']}", price])
    return {
        "emoji": "🏪", "eyebrow": f"💰 {int(p.money)} 元",
        "title": "补给站 · 车",
        "subtitle": f"当前 {v['emoji']} {v['name']}",
        "chips": [(f"#吃饭 还剩 {left}/{MEAL_PER_DAY} 次", "info")],
        "blocks": [
            {"kind": "table", "head": ["#", "车", "价格"], "rows": rows},
            {"kind": "note", "tone": "info",
             "text": "换车以后手上没跑完的单作废；车越贵越快，油钱也越贵。"},
        ],
        "footer": "#买 编号 下单 ｜ #商店 装备 看装备 ｜ 每天 3 次 #吃饭",
    }


# ------------------------------------------------------------------ 限量单
def limited_spec(slots: list[dict], weather: str, p: Player | None = None) -> dict:
    w = WEATHERS.get(weather, WEATHERS["sunny"])
    items = []
    for s in slots:
        o = s.get("order") or {}
        if not o.get("shop"):
            items.append({"icon": "⚠️", "text": f"{s.get('slot', '?')} 号单的存档读不出来，换一张吧", "tone": "bad"})
            continue
        if s.get("claimed_by"):
            items.append({"icon": "❌", "text": f"{s.get('slot', '?')} 号单已被 {s.get('claimed_name') or '别人'} 抢走",
                          "tone": "muted"})
            continue
        pills = [(f"{o.get('km', 0)}km", "info"), (f"{o.get('pay', 0)} 元", "accent"),
                 (f"限时 {o.get('limit', 0)} 分", "warn"),
                 (f"{o.get('floor', 0)} 楼 {'有电梯' if o.get('elevator') else '无电梯'}", "muted")]
        items.append({
            "badge": f"{s.get('slot', '?')} 号",
            "accent": "accent",
            "route": f"{o.get('shop')} → {o.get('dest', '?')}",
            "who": f"👤 {o['who']}" if o.get("who") else "",
            "item": f"{o.get('shop_emoji') or '🛍'} {o['item']}" if o.get("item") else "",
            "note": f"📝 {o['note']}" if o.get("note") else "",
            "pills": pills,
        })
    has_open = any(not s.get("claimed_by") for s in slots)
    return {
        "emoji": "⚡",
        "eyebrow": f"{w['emoji']} {w['name']}天 ｜ 全群先到先得",
        "title": "今日限量单",
        "subtitle": "钱多，但也更难跑——手快有，手慢无",
        "blocks": [{"kind": "orders", "items": items}],
        "footer": "#抢 编号 抢 ｜ 抢了就得跑，不能退" if has_open else "今天的限量单都被人抢走了，明天早点来。",
    }


# ------------------------------------------------------------------ 押注
def bets_spec(rows: list[dict], day: str) -> dict:
    items = [{
        "icon": "🎟",
        "text": f"{r.get('bettor_name') or '某人'} → 押 {r.get('target_name') or '某人'}",
        "meta": f"{int(r.get('stake') or 0)} 元",
        "tone": "accent",
    } for r in rows]
    blocks = []
    if items:
        blocks.append({"kind": "list", "items": items})
        blocks.append({"kind": "note", "tone": "info",
                       "text": "晚上九点开奖：押中的人赢平台一倍，被押的人进前三还有彩头。"})
    else:
        blocks.append({"kind": "note", "tone": "muted",
                       "text": "还没人押注。试试 #押 小王 100 —— 押他今天能进前三。"})
    return {
        "emoji": "🎟",
        "eyebrow": day,
        "title": "今日押注",
        "blocks": blocks,
        "footer": "一人一天一注，最低 10 元；被押的人今天一单没跑，本金原样退回。",
    }


# ------------------------------------------------------------------ 任意一句话

# 这些词出现就换个语气色，让「成了」和「没成」一眼就分开
_TONE_WORDS = [
    ("bad", ("不能", "不够", "不起", "没有", "没找到", "别", "失败", "抽风", "太贵",
             "抢走", "作废", "扣", "罚", "没钱", "先跑", "只有")),
    ("good", ("成功", "搞定", "到手", "完成", "送达", "+", "赢", "修好", "签到")),
    ("warn", ("注意", "小心", "风险", "超时", "晚了", "快没")),
]


def _guess_tone(text: str, default: str = "accent") -> str:
    for tone, words in _TONE_WORDS:
        if any(w in text for w in words):
            return tone
    return default


def _split_emoji(line: str) -> tuple[str, str]:
    """把行首的 emoji 摘出来当卡片图标（剩下的当标题）。"""
    from .art import is_emoji

    chars = list(line)
    i = 0
    while i < len(chars) and (chars[i] in ("\ufe0f", "\u200d") or is_emoji(chars[i])):
        i += 1
    icon = "".join(c for c in chars[:i] if c not in ("\ufe0f", "\u200d")).strip()
    rest = "".join(chars[i:]).strip()
    if not rest:
        return "", icon or line
    return icon, rest


def message_spec(text: str, tone: str | None = None) -> dict:
    """把任意一句群内文案做成卡片：第一行当标题，剩下的行当正文。"""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        return {"emoji": "🛵", "title": "……", "accent": "accent"}

    icon, title = _split_emoji(lines[0])
    body = lines[1:]
    tone_name = tone or _guess_tone("\n".join(lines))

    blocks: list[dict] = []
    if body:
        bullets = all(ln.startswith(("·", "•", "-", "▪")) for ln in body)
        if bullets and len(body) > 1:
            blocks.append({"kind": "list", "items": [
                {"icon": "·", "text": ln.lstrip("·•-▪ ").strip()} for ln in body
            ]})
        else:
            blocks.append({"kind": "text", "text": "\n".join(body)})

    spec = {
        "emoji": icon or "🛵",
        "title": title,
        "accent": tone_name,
        "blocks": blocks,
    }
    if tone_name == "bad":
        spec["eyebrow"] = "这条没走通"
    elif tone_name == "good":
        spec["eyebrow"] = "办成了"
    return spec