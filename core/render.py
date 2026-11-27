"""所有群内文本都在这里。改文案只需要动这一个文件。"""
from __future__ import annotations

from .game import (
    DAY_ORDER_CAP, DURABILITY_MAX, DURABILITY_WARN, GEARS, LEVELS, MEAL_PER_DAY,
    MEAL_PRICE, MEAL_STAMINA, RATING_MAX, STAMINA_MAX, VEHICLES, WEATHERS,
    Player, Order, Settlement, level_name, next_level_need,
    order_income_hint, rating_bucket, repair_price, risk_label, vehicle_of,
)

BUCKET_TEXT = {
    "premium": "优质 · 评分 ≥4.8，好单多",
    "good": "良好",
    "normal": "一般",
    "floor": "保底 · 评分 <4.0，只能接保底单",
}

BAD_LABEL = {"late": "迟到", "pack": "洒凉", "damage": "磕碰", "karen": "碰瓷"}


def yuan(v) -> str:
    v = int(v)
    return ("+" if v > 0 else ("−" if v < 0 else "")) + str(abs(v))


def _round1(v: float) -> str:
    """耐久 0.4 这种小数值别显示成 0（看着像已经跑不动的车还在跑）。"""
    v = float(v)
    if v <= 0:
        return "0"
    if v >= 1:
        return f"{v:.0f}"
    return f"{v:.1f}"


def stamina_bar(v: float) -> str:
    # int(x+0.5) 是四舍五入；round() 是银行家舍入，恰好在 5/15/25… 这些常见值上
    # 会往下取，体力条看着「漏一格」。
    n = max(0, min(10, int(max(0.0, v) / STAMINA_MAX * 10 + 0.5)))
    return "▰" * n + "▱" * (10 - n)


def weather_text(weather: str, peak: bool) -> str:
    w = WEATHERS.get(weather, WEATHERS["sunny"])
    parts = [f"{w['emoji']} {w['name']}天"]
    if peak:
        parts.append("⏰ 高峰期（路更堵、钱更多）")
    if w["bonus"]:
        parts.append(f"补贴 +{int(w['bonus'] * 100)}%")
    return " ｜ ".join(parts)


def header(p: Player, weather: str, peak: bool) -> str:
    v = vehicle_of(p)
    return (f"{v['emoji']} {v['name']} ｜ ⭐{p.rating:.2f} ｜ "
            f"⚡{int(p.stamina)}/{STAMINA_MAX} ｜ 💰{int(p.money)} 元 ｜ "
            f"今日 {p.today_orders} 单 {yuan(p.today_income)} 元")


def dispatch_card(p: Player, orders: list[Order], weather: str, peak: bool) -> str:
    lines = [header(p, weather, peak), weather_text(weather, peak), ""]
    for o in orders:
        lift = "有电梯" if o.elevator else "无电梯"
        prob = order_income_hint(p, o, weather, peak)
        tag = "🔥" if o.premium else "📦"
        lines.append(f"{tag} 单 {o.idx}  {o.shop_emoji} {o.shop} → {o.dest}"
                     + (f" · 👤 {o.who}" if o.who else ""))
        if o.item:
            lines.append(f"    {o.shop_emoji} {o.item}" + ("　📦 大件" if o.bulk else ""))
        if o.note:
            lines.append(f"    📝 {o.note}")
        slow = "　🍳 出餐慢（先等 8 分钟）" if o.slow else ""
        lines.append(f"    {o.km}km ｜ {o.pay} 元 ｜ {o.floor} 楼 {lift}{slow} ｜ "
                     f"限时 {o.limit} 分 ｜ 风险 {risk_label(prob)}")
        lines.append("")          # 单与单之间空一行，不然三张单糊成一坨
    lines.append("#接 1 / #接 2 / #接 3 　或者 #跳过")
    return "\n".join(lines)


def depart_line(p: Player, o: Order) -> str:
    v = vehicle_of(p)
    return f"{v['emoji']} 出发：{o.shop_emoji} {o.shop} → {o.dest}，{o.km}km…"


def settle_card(p: Player, st: Settlement, weather: str, peak: bool,
                story: dict | None = None) -> str:
    o = st.order
    v = vehicle_of(p)
    story = story or {}
    head = f"{v['emoji']} {o.shop_emoji} {o.shop} → {o.dest}（{o.km}km）"
    if o.who:
        head += f" · 👤 {o.who}"
    if o.item:
        head += f"\n{o.shop_emoji} {o.item}"
    lines = [head]
    if st.failed:
        lines.append(f"⏱ 折腾了 {st.actual_min:.1f} 分 ｜ ❌ 配送失败，这单没送到")
    elif st.on_time:
        lines.append(f"⏱ 用时 {st.actual_min:.1f} 分 ｜ 限时 {o.limit} 分 ｜ ✅ 准时送达")
    else:
        # 罚款按超时分钟数算，越拖越狠：把「谁晚多久」直接写在卡上
        lines.append(f"⏱ 用时 {st.actual_min:.1f} 分 ｜ 限时 {o.limit} 分 ｜ "
                     f"❌ 超时 {st.late_min:.1f} 分 → 罚 {abs(st.delivery_fee)} 元")

    # 收入明细
    detail = []
    if st.failed:
        detail.append(f"赔餐费 −{st.compensate}")
    elif st.delivery_fee > 0:
        detail.append(f"配送 +{st.delivery_fee}")
    else:
        detail.append(f"超时 −{abs(st.delivery_fee)}")
    if st.tip:
        detail.append(f"小费 +{st.tip}")
    if st.cost:
        detail.append(f"损耗 −{_round1(st.cost)}")
    if st.bonus:
        # 恶意投诉是 bonus -= 10：写「补贴 -10」会让人以为平台倒扣补贴，
        # 其实是赔给顾客的钱。
        detail.append(f"赔偿 −{abs(st.bonus)}" if st.bonus < 0
                      else f"补贴 +{st.bonus}")
    if st.fine:
        detail.append(f"罚款 −{st.fine}")
    lines.append(f"💸 {yuan(st.income)} 元（{'，'.join(detail)}）")

    if st.bad_review:
        why = BAD_LABEL.get(st.bad_kind, "")
        why = f"（{why}）" if why else ""
        # 一天能挨 5、6 个差评，但上限是 4，显示 5/4 只会让人以为卡了
        lines.append(f"💢 差评 +{st.bad_review}{why}"
                     f"（今日 {min(st.today_bad, 4)}/4，满 4 个扣当日收入 15%）")
    if p.rating > st.rating_before:
        lines.append(f"⭐ 评分 {st.rating_before:.2f} → {st.rating_after:.2f}")
    elif p.rating < st.rating_before:
        lines.append(f"📉 评分 {st.rating_before:.2f} → {st.rating_after:.2f}")

    for e in st.events:
        if e.startswith("❌ 超时") or e.startswith("⏱") or e.startswith("🚫 配送失败"):
            continue
        lines.append(e)

    if st.failed and st.fail_reason:
        lines.append(f"🚫 {st.fail_reason}")

    # 这一单的「故事」：为什么好评/差评、商家怎么出餐、路上遇到什么
    if story.get("merchant"):
        tail = "　→ 出餐慢了，这 8 分钟也算在限时里" if o.slow else ""
        lines.append(f"🏪 商家：{story['merchant']}{tail}")
    # 失败单上面已经用 st.fail_reason 说过「没送到」的原因了，这里再播池子
    # 里随机抽的一句，同一单就有两个不同的原因，群里看着像 bug。
    if story.get("late") and not (st.failed and st.fail_reason):
        if st.failed:
            lines.append(f"🚫 没送到的原因：{story['late']}")
        elif st.on_time:
            lines.append(f"🛣 路上：{story['late']}")
        else:
            lines.append(f"🕳 路上：{story['late']}　→ 就是这耽误的 {st.late_min:.1f} 分")
    if story.get("chat"):
        who = o.who or "顾客"
        lines.append(f"📞 {who}打来电话：「{story['chat']}」")
    if story.get("cust"):
        who = o.who or "顾客"
        lines.append(f"💬 {who}：「{story['cust']}」")

    # 车况：这一单磨掉多少、跑完还剩多少（磨没了要去 #修车）
    dur_note = ""
    if st.durability_after <= 0:
        dur_note = "　🛑 跑不动了，先 #修车"
    elif st.durability_after < DURABILITY_WARN:
        dur_note = "　⚠️ 车况见底"
    lines.append(f"🛠 车损 −{st.wear}（耐久 {_round1(st.durability_after)}/{int(DURABILITY_MAX)}）"
                 f"{dur_note}")
    lines.append(f"⚡ 体力 {int(st.stamina_after)}/{STAMINA_MAX} {stamina_bar(st.stamina_after)}")
    lines.append(f"💰 余额 {int(st.money_after)} 元 ｜ 今日 {st.today_orders} 单 "
                 f"{yuan(st.today_income)} 元 ｜ 本周 {yuan(p.week_income)} 元")
    return "\n".join(lines)


def story_card(st: Settlement, story: dict | None = None) -> str:
    """单独播「这一单到底发生了什么」——实时生成的剧情慢几秒，先出结果再出故事。"""
    o = st.order
    story = story or {}
    you = o.who or "顾客"
    lines = []
    if story.get("merchant"):
        tail = "　→ 出餐慢了，这 8 分钟也算在限时里" if o.slow else ""
        lines.append(f"🏪 商家：{story['merchant']}{tail}")
    if story.get("late") and not (st.failed and st.fail_reason):
        # 失败的原因结算卡里已经播过一句了，故事卡再说一句，群里看着像
        # 「同一单有两个原因」。上面有原因就不再重复。
        if st.failed:
            lines.append(f"🚫 没送到的原因：{story['late']}")
        elif st.on_time:
            lines.append(f"🛣 路上：{story['late']}")
        else:
            lines.append(f"🕳 路上：{story['late']}　→ 就是这耽误的 {st.late_min:.1f} 分")
    if story.get("chat"):
        lines.append(f"📞 {you}打来电话：「{story['chat']}」")
    if story.get("cust"):
        lines.append(f"💬 {you}：「{story['cust']}」")
    return "\n".join(lines)


def status_card(p: Player, weather: str, peak: bool) -> str:
    v = vehicle_of(p)
    geo = "、".join(f"{g['emoji']} {g['name']}" for g in GEARS if g["id"] in p.gear)
    lines = [
        f"{v['emoji']} {v['name']} ｜ 🏅 {level_name(p)}骑手",
        f"⭐ 评分 {p.rating:.2f}（{BUCKET_TEXT[rating_bucket(p)]}） ｜ 累计 {p.orders} 单",
        f"💰 余额 {int(p.money)} 元 ｜ 今日 {p.today_orders} 单 {yuan(p.today_income)} 元",
        f"⚡ 体力 {int(p.stamina)}/{STAMINA_MAX} {stamina_bar(p.stamina)} ｜ "
        f"差评 {min(p.today_bad, 4)}/4 ｜ 今日上限 {DAY_ORDER_CAP} 单",
        weather_text(weather, peak),
    ]
    nxt = next_level_need(p)
    if nxt:
        lines.append(f"🏆 本周 {yuan(p.week_income)} 元 ｜ 距 {LEVELS[p.level + 1]['name']} "
                     f"还差 {nxt[0]} 元（门槛 {nxt[1]}）")
    else:
        lines.append(f"🏆 已是王者，本周 {yuan(p.week_income)} 元")
    perks = []
    if v.get("bulk"):
        perks.append("能拉大件（大件单 +8 元）")
    if v.get("meal_p"):
        perks.append("接公司团餐（大额单）")
    if v.get("weather_immune"):
        perks.append("有棚，雨雪不加超时")
    if v.get("peak_extra"):
        perks.append("四轮堵车，高峰期更慢")
    if v.get("accident"):
        perks.append("有翻车/摔车风险")
    if v.get("police"):
        perks.append("怕交警查车")
    if v.get("puncture"):
        perks.append("会扎胎")
    if v.get("wear", 1.0) >= 1.2:
        perks.append("跑起来费车")
    if perks:
        lines.append(f"🚦 车辆特性：{'、'.join(perks)}")
    lines.append(f"🎒 装备：{geo or '还什么都没有，去 #商店 看看'}")
    # 车况：一单一单磨下来的耐久，磨到底就得修
    dur = max(0.0, min(DURABILITY_MAX, p.durability))
    tail = ""
    if dur <= 0:
        tail = "　🛑 跑不动了"
    elif dur < DURABILITY_WARN:
        tail = "　⚠️ 见底，容易出事"
    if p.sick:
        tail += f" ｜ 🛞 带病上路剩 {p.sick} 单"
    # 什么车况都能修，所以价钱一直挂着；车况好的时候就是个几块钱的小修
    tail += f" ｜ #修车 {repair_price(p)} 元"
    lines.append(f"🛠 车况 {_round1(dur)}/{int(DURABILITY_MAX)} {stamina_bar(dur)}{tail}")
    lines.append(f"📅 连签 {p.checkin_streak} 天 ｜ #签到 领钱（连签越久给越多）")
    return "\n".join(lines)


def shop_card(p: Player, page: str = "car") -> str:
    """补给站。车和装备分两页发 —— 20 行一次发出去在手机上要翻两屏半，
    发下来没人看得到底部的「#买 编号」怎么用。
    """
    v = vehicle_of(p)
    left = max(0, MEAL_PER_DAY - p.today_meals)
    if page == "gear":
        lines = [f"🏪 补给站 · 装备 ｜ 💰 {int(p.money)} 元", "", "【装备】"]
        for i, g in enumerate(GEARS, start=len(VEHICLES) + 1):
            if g["id"] in p.gear:
                lines.append(f"  {i}. {g['emoji']} {g['name']} — 已拥有")
            else:
                lines.append(f"  {i}. {g['emoji']} {g['name']} {g['price']} 元 ｜ {g['desc']}")
        lines.append("")
        lines.append(f"#买 编号 下单 ｜ #商店 回车 ｜ 今天还能 #吃饭 {left} 次")
        return "\n".join(lines)

    lines = [f"🏪 补给站 · 车 ｜ 当前 {v['emoji']} {v['name']} ｜ 💰 {int(p.money)} 元", "", "【车】"]
    for i, veh in enumerate(VEHICLES, start=1):
        if i - 1 == p.vehicle:
            lines.append(f"  {i}. {veh['emoji']} {veh['name']}（当前）—")
        elif i - 1 < p.vehicle:
            lines.append(f"  {i}. {veh['emoji']} {veh['name']} — 已拥有")
        else:
            lines.append(f"  {i}. {veh['emoji']} {veh['name']} {veh['price']} 元 ｜ {veh['desc']}")
    lines.append("")
    lines.append(f"#买 编号 下单 ｜ #商店 装备 看装备 ｜ 每天 {MEAL_PER_DAY} 次 #吃饭")
    lines.append(f"今天还能 #吃饭 {left} 次（{MEAL_PRICE} 元 +{MEAL_STAMINA} 体力）")
    return "\n".join(lines)


def leaderboard_card(day: str, players: list[Player], limit: int = 10) -> str:
    lines = [f"🏆 今日收入榜（{day}）"]
    if not players:
        lines.append("今天还没人出车。")
        return "\n".join(lines)
    for i, p in enumerate(players[:limit], start=1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        v = vehicle_of(p)
        lines.append(f"{medal} {p.name} {yuan(p.today_income)} 元 · {p.today_orders} 单 "
                     f"· ⭐{p.rating:.2f} {v['emoji']}")
    lines.append("明天早上再排一次，跑得多的人钱多，跑得稳的人评分高。")
    return "\n".join(lines)


def help_card() -> str:
    return "\n".join([
        "🛵 风里雨里 · 外卖骑手（群内小游戏）",
        "",
        "#接单    派 3 张单，挑一张跑",
        "#接 1    接第 1 张（#接 2 / #接 3）",
        "#跳过    这一批不要，等下一批（⚡ 体力 −2）",
        "#我的    看车、评分、体力、余额、段位",
        "#收入榜  今天的群内排名",
        "#抢单    限量单（30–60 元，全群先到先得）",
        "#抢 2    抢 2 号限量单（手快有手慢无）",
        "#商店    买车 / 买装备（10 台车 + 10 件装备）",
        "#买 2    买 2 号商品",
        "#吃饭    15 元 +40 体力（每天 3 次）",
        "#修车    缺多少修多少，什么车况都能修，越烂越贵",
        "#签到    每天一次，连签越久给的钱越多，还回体力",
        "#抄近道  白银骑手每日 1 次，下一单格外顺、不容易超时",
        "#叙事池  看 AI 素材池补到多少条了（排障用）",
        "",
        "规则：超时按分钟扣钱，越拖越狠——先扣这一单配送费的 15%，每多迟到一分钟再加 5%，",
        "最多把这单的钱扣光，不倒欠；配送失败（联系不上人 / 门禁进不去 / 餐洒了）",
        "配送费全没，还要赔餐费 8–25 元，评分掉得更狠。准时送到达成好评有小费；",
        "一天最多 30 单，体力每 6 分钟回 1 点——别硬撑，先 #吃饭。",
        "全群同一天气，天气会影响路上的顺不顺和补贴。",
        "段位按本周收入升：青铜 → 白银 → 黄金 → 钻石 → 星耀 → 王者。",
        "10 台车各有脾气：🛴 滑板车省钱但只敢跑近单，🛺 三轮车能拉大件（+8 元），",
        "🚙 老头乐有棚不淋雨但怕交警，🏍 摩托能钻缝、跑单加价，🛻 小货车专接大件团餐，",
        "🚚 厢式货车一车全接，🏎 性能车全城最快、油钱也最贵。换车后旧单作废。",
    ])


def limited_card(slots: list[dict], weather: str, p: Player | None = None) -> str:
    w = WEATHERS.get(weather, WEATHERS["sunny"])
    lines = [f"⚡ 今日限量单 ｜ {w['emoji']} {w['name']}天 ｜ 全群先到先得"]
    for s in slots:
        o = s.get("order") or {}
        # 限量单的存档是一段 JSON：库里那条坏了的话 o 会是空 dict，
        # 这里硬取键会把整条指令炸掉，什么都发不出去。
        if not o.get("shop"):
            lines.append(f"  ⚠️ {s.get('slot', '?')}. 这一单的存档读不出来，换一张吧")
            continue
        if s.get("claimed_by"):
            lines.append(f"  ❌ 已被人抢走（{s.get('claimed_name') or '别人'}）")
            continue
        lines.append(f"  🔥 {s.get('slot', '?')}. {o.get('shop')} → {o.get('dest', '?')} ｜ "
                     f"{o.get('km', 0)}km ｜ {o.get('pay', 0)} 元 ｜ 限时 {o.get('limit', 0)} 分")
        floor = o.get("floor")
        if floor is not None:
            lift = "有电梯" if o.get("elevator") else "无电梯"
            lines.append(f"     {floor} 楼 {lift}")
        extra = []
        if o.get("item"):
            extra.append(f"{o.get('shop_emoji') or '🛍'} {o['item']}")
        if o.get("note"):
            extra.append(f"📝 {o['note']}")
        if extra:
            lines.append("     " + " ｜ ".join(extra))
    if any(not s.get("claimed_by") for s in slots):
        lines.append("")
        lines.append("#抢 编号 抢（手快有手慢无）")
    return "\n".join(lines)