"""风里雨里 · 外卖骑手 —— 核心数值逻辑。

本模块是纯 stdlib 实现、不 import astrbot，方便用 tools/simulate.py 跑几万单
校准经济曲线、也方便写单测。所有随机性都走显式传入的 random.Random，
方便固定种子复现。
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 基础常量

STAMINA_MAX = 100
STAMINA_PER_ORDER = 6.0          # 每单消耗
STAMINA_REGEN_MIN = 6            # 每 6 分钟回 1 点（≈240/天）
FATIGUE_LINE = 20                # 体力低于此值，超时率 +10%
DAY_ORDER_CAP = 30               # 每人每日有效订单上限
MEAL_PRICE = 15
MEAL_STAMINA = 40
MEAL_PER_DAY = 3
REPAIR_PRICE = 30               # 老价钱（现在按车况算，见 repair_price）
SICK_ORDERS = 3                  # 爆胎后「带病上路」剩余单数
SICK_PENALTY = 0.15              # 带病上路每单超时率 +15%

# 车况（耐久）：跑一单磨一点，磨没了就跑不动，修车按缺多少收钱
DURABILITY_MAX = 100.0
DURABILITY_WARN = 25.0           # 低于这个数，车况开始拖后腿（超时率往上走）
# 每台车磨多少耐久写在 VEHICLES 的 wear 上：越贵的车越耐跑
REPAIR_PER_POINT = 0.20          # 修 1 点耐久 0.2 元（从空修到满 20 元）
REPAIR_MIN = 5                   # 修车起步价
REPAIR_LINE = 50.0               # 车况低于这个数才提示/允许修车
START_MONEY = 50                 # 新人补贴

RATING_MIN, RATING_MAX = 1.0, 5.0
RATING_INIT = 4.5

# 车辆：price 购车价、speed km/h、mod 超时率修正、cost 每单燃料/损耗、max_km 能接的最远距离
# 每台车靠几个小开关做区分：
#   weather_immune  有棚，雨雪不加超时率
#   peak_extra      四轮高峰堵车，额外超时率
#   bulk / bulk_p   能拉大件（整箱水/煤气罐），大件单多给钱
#   meal_p          公司团餐概率（meal_mult / meal_add 决定加价）
#   puncture        会爆胎（两轮/三轮才扎胎）
#   police          没牌/显眼，怕交警
#   wear            每公里磨掉的耐久
VEHICLES = [
    {"id": "bike", "name": "共享单车", "emoji": "🚲", "price": 0,
     "speed": 12, "mod": +0.23, "cost": 0.5, "max_km": 5.0, "wear": 1.25,
     "puncture": True,
     "desc": "起步免费，慢、且只能接 5km 内的单"},
    {"id": "scooter", "name": "电动滑板车", "emoji": "🛴", "price": 350,
     "speed": 20, "mod": +0.08, "cost": 0.3, "max_km": 4.0, "wear": 1.40,
     "puncture": True,
     "desc": "省钱、跑得快，但只敢送 4km 内，轮子细、磨得凶"},
    {"id": "tricycle", "name": "电动三轮车", "emoji": "🛺", "price": 700,
     "speed": 16, "mod": +0.12, "cost": 0.6, "max_km": 6.5, "wear": 1.00,
     "bulk": True, "bulk_p": 0.24, "puncture": True,
     "accident": True, "accident_p": 0.02,
     "desc": "能拉大件（整箱水、煤气罐）多给 8 元，但车斗晃、侧翻风险"},
    {"id": "ebike", "name": "电动车", "emoji": "🛵", "price": 1200,
     "speed": 22, "mod": 0.0, "cost": 1.0, "max_km": 9.0, "wear": 0.85,
     "puncture": True,
     "desc": "均衡，能跑 9km，是绝大多数人的第一台车"},
    {"id": "elder", "name": "老头乐", "emoji": "🚙", "price": 2000,
     "speed": 30, "mod": +0.03, "cost": 1.4, "max_km": 10.5, "wear": 0.75,
     "weather_immune": True, "peak_extra": 0.06, "police": True, "pay_mul": 1.16,
     "desc": "有棚，雨雪淋不着、能跑 10.5km，但车速慢、四轮堵车、没牌照怕交警"},
    {"id": "moto", "name": "摩托车", "emoji": "🏍", "price": 3000,
     "speed": 35, "mod": -0.07, "cost": 1.0, "max_km": 12.0, "wear": 0.95,
     "puncture": True, "accident": True, "police": True, "peak_mod": 0.03,
     "pay_mul": 1.10,
     "desc": "快，能钻缝，但油费高、有交警和摔车风险"},
    {"id": "van", "name": "电动小货车", "emoji": "🛻", "price": 4500,
     "speed": 26, "mod": +0.02, "cost": 1.2, "max_km": 13.0, "wear": 0.80,
     "bulk": True, "bulk_p": 0.22, "weather_immune": True, "peak_extra": 0.04,
     "meal_p": 0.18, "meal_mult": 1.8, "meal_add": 5.0, "pay_mul": 1.00,
     "desc": "带棚能拉货，跑 13km，大件和团餐都接得住；就是跑不快"},
    {"id": "car", "name": "小汽车", "emoji": "🚗", "price": 7000,
     "speed": 50, "mod": -0.10, "cost": 3.0, "max_km": 14.0, "wear": 0.60,
     "weather_immune": True, "peak_extra": 0.10, "accident": True, "police": True,
     "meal_p": 0.25, "meal_mult": 2.2, "meal_add": 8.0, "pay_mul": 1.24,
     "desc": "最快最贵，但高峰期堵起来比摩托还慢"},
    {"id": "truck", "name": "厢式货车", "emoji": "🚚", "price": 11000,
     "speed": 40, "mod": -0.05, "cost": 4.0, "max_km": 16.0, "wear": 0.55,
     "bulk": True, "bulk_p": 0.30, "weather_immune": True, "peak_extra": 0.12,
     "meal_p": 0.30, "meal_mult": 2.4, "meal_add": 10.0, "pay_mul": 1.25,
     "desc": "一车全接：团餐、整箱货、16km 远单；空车跑一趟也费油"},
    {"id": "sport", "name": "性能车", "emoji": "🏎", "price": 16000,
     "speed": 65, "mod": -0.14, "cost": 5.0, "max_km": 18.0, "wear": 0.85,
     "accident": True, "police": True, "peak_extra": 0.08,
     "meal_p": 0.20, "meal_mult": 2.0, "meal_add": 6.0, "pay_mul": 1.62,
     "desc": "全城最快，18km 也敢接；油钱最贵，一脚油门一块五"},
]
# 老存档的 p.vehicle 是旧的四台/六台车索引，插入新车后要迁移，否则电车玩家会变成三轮车
VEHICLE_INDEX_VER = 3
VEHICLE_MIGRATION = {0: 0, 1: 2, 2: 3, 3: 4, 4: 5, 5: 7}   # 六台车 → 现在
VEHICLE_MIGRATION_V1 = {0: 0, 1: 2, 2: 4, 3: 5}            # 四台车 → 六台车

GEARS = [
    {"id": "saddle",   "name": "坐垫",     "emoji": "🪑", "price": 150,  "desc": "软和，跑一单少费点力气"},
    {"id": "helmet",   "name": "头盔",     "emoji": "🪖", "price": 200,  "desc": "摔了更扛得住，磕碰少"},
    {"id": "raincoat", "name": "雨衣",     "emoji": "☂️", "price": 300,  "desc": "雨天雪天不容易超时"},
    {"id": "boots",    "name": "防水鞋",   "emoji": "🥾", "price": 350,  "desc": "雨雪天不泡坏车，磨损小"},
    {"id": "box",      "name": "保温箱",   "emoji": "🧊", "price": 600,  "desc": "保温好，差评少"},
    {"id": "cart",     "name": "折叠推车", "emoji": "🛒", "price": 700,  "desc": "大件单多给钱（+5 元）"},
    {"id": "phone",    "name": "新手机",   "emoji": "📱", "price": 900,  "desc": "手快，抢限量单窗口更长"},
    {"id": "vip",      "name": "修车会员卡", "emoji": "💳", "price": 1000, "desc": "修车打七折"},
    {"id": "permit",   "name": "临时通行证", "emoji": "📄", "price": 1400, "desc": "遇上交警少赔点"},
    {"id": "map",      "name": "熟悉地图", "emoji": "🧭", "price": 1800, "desc": "路熟，一路不容易超时"},
]

LEVELS = [
    {"name": "青铜", "weekly": 0,    "perk": "刚入行，风里雨里"},
    {"name": "白银", "weekly": 900,  "perk": "每日 1 次「抄近道」，下一单格外顺"},
    {"name": "黄金", "weekly": 2700, "perk": "派单金额 +10%"},
    {"name": "钻石", "weekly": 4600, "perk": "抢限量单宽限 2 分钟，远单也敢接"},
    {"name": "星耀", "weekly": 6800, "perk": "平台优先派远单、大单"},
    {"name": "王者", "weekly": 9500, "perk": "平台补贴，车辆损耗全免"},
]

# ---- 签到：一天一次，连签越久给越多，断了从头算
CHECKIN_MONEY = 15
CHECKIN_STEP = 5
CHECKIN_CAP = 60
CHECKIN_STAMINA = 15

WEATHERS = {
    "sunny": {"emoji": "☀️", "name": "晴",   "timeout": 0.0,  "bonus": 0.0},
    "rain":  {"emoji": "🌧", "name": "雨",   "timeout": 0.10, "bonus": 0.5},
    "snow":  {"emoji": "🌨", "name": "雪",   "timeout": 0.18, "bonus": 0.8},
}

SHOPS = [
    ("老王牛肉面", "🍜"), ("沙县小吃", "🥟"), ("张记烧烤", "🍢"), ("蜜雪冰城", "🥤"),
    ("肯德基宅急送", "🍗"), ("兰州拉面", "🍜"), ("川味冒菜", "🌶"), ("粥铺阿姨", "🥣"),
    ("炸鸡汉堡", "🍔"), ("重庆小面", "🍲"), ("便利蜂", "🏪"), ("桃园烧饼", "🥠"),
    ("日料小町", "🍣"), ("阿婆馄饨", "🍲"), ("东北饺子馆", "🥟"),
]

# 品类 → 图标。叙事池给的是「品类|店名|商品」，图标按品类挑，别让花店挂着🍜
BIZ_EMOJI = {
    "餐饮": "🍜", "药房": "💊", "药店": "💊", "超市": "🏪", "生鲜": "🥬",
    "水果": "🍎", "花店": "💐", "宠物": "🐾", "数码": "📱", "五金": "🔧",
    "建材": "🧱", "蛋糕": "🎂", "奶茶": "🥤", "文具": "✏️", "服装": "👕",
    "洗衣": "🧺", "跑腿": "📦", "快递": "📦", "鲜花": "💐",
}

# 送的东西是「吃的」的品类：说话可以带菜饭字眼，差评走「洒凉」
FOOD_BIZ = ("餐饮", "蛋糕", "奶茶", "生鲜", "水果", "超市")

# 其中「有汤水、会洒」的品类：只有这些单子的备注才提「汤别洒」「面别坨」
WET_BIZ = ("餐饮",)


def split_shop(entry: str) -> tuple[str, str, str, str] | None:
    """把「品类|店名|商品」拆成 (品类, 店名, 商品, 图标)；格式不对返回 None。"""
    parts = [x.strip() for x in str(entry or "").split("|")]
    if len(parts) != 3 or not all(parts):
        return None
    kind, shop, item = parts[0][:6], parts[1][:18], parts[2][:32]
    return kind, shop, item, BIZ_EMOJI.get(kind, "🏪")
DESTS = [
    ("阳光小区", 6), ("幸福里", 18), ("环球中心 A 座", 26), ("青年公寓", 12),
    ("世纪花园", 9), ("老城区 7 号院", 2), ("河畔新城", 22), ("科创园 B 栋", 15),
    ("城南医院", 8), ("师范学院宿舍", 6), ("绿地中心", 28), ("温馨家园", 4),
    ("金奥大厦", 30), ("南湖雅苑", 16), ("工人新村", 5),
]


# ---------------------------------------------------------------- 数据结构

@dataclass
class Player:
    uid: str
    name: str = "骑手"
    money: int = START_MONEY
    rating: float = RATING_INIT
    stamina: float = float(STAMINA_MAX)
    stamina_at: float = 0.0
    vehicle: int = 0
    gear: list[str] = field(default_factory=list)
    orders: int = 0                       # 累计接单
    week_income: int = 0
    level: int = 0
    day: str = ""                         # 今日日期（YYYY-MM-DD）
    today_orders: int = 0
    today_income: int = 0
    today_bad: int = 0
    today_meals: int = 0
    today_penalty: bool = False           # 今日已吃「4 差评」罚金
    sick: int = 0                         # 带病上路剩余单
    durability: float = DURABILITY_MAX    # 车况 0–100，跑一单磨一点，修车补回来
    shortcut_day: str = ""                # 抄近道已用日期
    shortcut_armed: bool = False           # 抄近道已备好，下一单生效
    on_time_streak: int = 0               # 连续准时
    pending: list = field(default_factory=list)   # 挂起的订单 dict
    pending_at: float = 0.0
    last_cmd: float = 0.0
    created: float = 0.0
    checkin_day: str = ""                 # 上次签到的日期（YYYY-MM-DD）
    checkin_streak: int = 0               # 连签天数

    # -- 序列化
    def to_dict(self) -> dict:
        return {
            "uid": self.uid, "name": self.name, "money": int(self.money),
            "rating": round(float(self.rating), 2), "stamina": round(float(self.stamina), 1),
            "stamina_at": float(self.stamina_at), "vehicle": int(self.vehicle),
            "veh_ver": VEHICLE_INDEX_VER,
            "gear": list(self.gear), "orders": int(self.orders),
            "week_income": int(self.week_income), "level": int(self.level),
            "day": self.day, "today_orders": int(self.today_orders),
            "today_income": int(self.today_income), "today_bad": int(self.today_bad),
            "today_meals": int(self.today_meals), "today_penalty": bool(self.today_penalty),
            "sick": int(self.sick), "durability": round(float(self.durability), 1),
            "shortcut_day": self.shortcut_day, "shortcut_armed": bool(self.shortcut_armed),
            "on_time_streak": int(self.on_time_streak),
            "checkin_day": self.checkin_day, "checkin_streak": int(self.checkin_streak),
            "pending": self.pending, "pending_at": float(self.pending_at),
            "created": float(self.created),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Player":
        p = cls(uid=str(d.get("uid", "")))
        p.name = d.get("name") or "骑手"
        p.money = int(d.get("money", 0))
        p.rating = float(d.get("rating", RATING_INIT))
        p.stamina = float(d.get("stamina", STAMINA_MAX))
        p.stamina_at = float(d.get("stamina_at", 0) or 0)
        p.vehicle = int(d.get("vehicle", 0))
        # 旧存档的车辆索引按着老车表来的，插了新车要迁移，否则电车玩家会变三轮车。
        # 四台车时代（veh_ver 缺省=1）先升到六台车，再统一升到现在的十台车。
        ver = int(d.get("veh_ver", 1))
        if ver < 2:
            p.vehicle = VEHICLE_MIGRATION_V1.get(p.vehicle, 0)
        if ver < VEHICLE_INDEX_VER:
            p.vehicle = VEHICLE_MIGRATION.get(p.vehicle, 0)
        p.vehicle = max(0, min(p.vehicle, len(VEHICLES) - 1))
        p.gear = list(d.get("gear") or [])
        p.orders = int(d.get("orders", 0))
        p.week_income = int(d.get("week_income", 0))
        p.level = int(d.get("level", 0))
        p.day = d.get("day") or ""
        p.today_orders = int(d.get("today_orders", 0))
        p.today_income = int(d.get("today_income", 0))
        p.today_bad = int(d.get("today_bad", 0))
        p.today_meals = int(d.get("today_meals", 0))
        p.today_penalty = bool(d.get("today_penalty", False))
        p.sick = int(d.get("sick", 0))
        p.durability = max(0.0, min(DURABILITY_MAX,
                                    float(d.get("durability", DURABILITY_MAX))))
        p.shortcut_day = d.get("shortcut_day") or ""
        p.shortcut_armed = bool(d.get("shortcut_armed", False))
        p.on_time_streak = int(d.get("on_time_streak", 0))
        p.checkin_day = d.get("checkin_day") or ""
        p.checkin_streak = int(d.get("checkin_streak", 0))
        p.pending = list(d.get("pending") or [])
        p.pending_at = float(d.get("pending_at", 0) or 0)
        p.created = float(d.get("created", 0) or 0)
        return p


@dataclass
class Order:
    idx: int                 # 1/2/3
    shop: str
    shop_emoji: str
    dest: str
    km: float
    floor: int
    elevator: bool
    pay: int                 # 基础配送费
    speed: int               # 生成时的车速（展示用）
    limit: int               # 限时（分钟）
    slow: bool               # 商家出餐慢
    premium: bool = False    # 高价优质单
    item: str = ""           # 顾客点了什么（由叙事池给出）
    note: str = ""           # 顾客备注（奇怪要求，由叙事池给出）
    bulk: bool = False       # 大件单（整箱水/煤气罐，三轮车专属福利）
    biz: str = ""            # 品类（餐饮/药房/超市/…；空=内置餐饮店）
    who: str = ""            # 收件人称呼（王先生/刘女士，由叙事池给出）

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "idx", "shop", "shop_emoji", "dest", "km", "floor", "elevator",
            "pay", "speed", "limit", "slow", "premium", "item", "note", "bulk",
            "biz", "who")}

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Settlement:
    order: Order
    on_time: bool
    actual_min: float
    late_min: float
    income: int                 # 本单到手（含小费/事件，已扣车辆成本）
    delivery_fee: int           # 配送费部分
    tip: int = 0
    cost: int = 0
    fine: int = 0
    bonus: int = 0
    bad_review: int = 0
    bad_kind: str = ""          # 差评类型：late 迟到 / pack 洒凉 / karen 碰瓷
    rating_before: float = RATING_INIT
    rating_after: float = RATING_INIT
    stamina_after: float = STAMINA_MAX
    money_after: int = 0
    today_income: int = 0
    today_orders: int = 0
    today_bad: int = 0
    events: list[str] = field(default_factory=list)
    broke: bool = False          # 今日 4 差评罚金触发
    accident: bool = False
    failed: bool = False         # 配送失败：没送到，赔付餐费
    fail_reason: str = ""       # 为什么没送到（由叙事池给出，结算卡里播）
    compensate: int = 0         # 失败时赔给商家的钱
    wear: float = 0.0           # 这一单磨掉了多少耐久
    durability_after: float = DURABILITY_MAX   # 跑完这一单的车况


# ---------------------------------------------------------------- 小工具

def vehicle_of(p: Player) -> dict:
    v = VEHICLES[max(0, min(p.vehicle, len(VEHICLES) - 1))]
    return v


def vehicle_speed(p: Player) -> int:
    return int(vehicle_of(p)["speed"])


def vehicle_wear(p: Player, order: Order, weather: str, shortcut: bool = False) -> float:
    """跑这一单磨掉多少耐久：按车、按距离，爬楼/大件/雨雪/烂路各加一点。"""
    vi = max(0, min(p.vehicle, len(VEHICLES) - 1))
    wear = VEHICLES[vi].get("wear", 1.0) * order.km
    if order.floor >= 6 and not order.elevator:
        wear += order.floor * 0.06          # 扛东西爬楼，链条和膝盖一起遭罪
    if order.bulk:
        wear += 1.5                          # 大件压车
    if weather != "sunny":
        wear *= 1.2                          # 雨雪天，刹车片和链条磨得快
        if has_gear(p, "boots"):
            wear *= 0.85                     # 防水鞋：轮子不泡水，少磨一点
    if shortcut:
        wear += 0.8                          # 抄近道走烂路
    if p.sick:
        wear += 1.0                          # 带病上路更伤车
    return round(wear, 1)


def repair_price(p: Player) -> int:
    """修车钱按车况算：缺多少修多少，小毛病也有个起步价。

    什么车况都能修，只是越烂越贵；有修车会员卡再打七折。
    """
    missing = max(0.0, DURABILITY_MAX - p.durability)
    price = max(REPAIR_MIN, int(round(missing * REPAIR_PER_POINT)))
    if has_gear(p, "vip"):
        price = max(REPAIR_MIN, int(round(price * 0.7)))
    return price


def needs_repair(p: Player) -> bool:
    """该不该修：爆过胎，或者车况掉到修车线以下（只是提示，修不修玩家自己定）。"""
    return p.sick > 0 or p.durability < REPAIR_LINE


def checkin(p: Player, now: float) -> dict:
    """每日签到：连签越久给得越多，断一天从头算。"""
    d = day_key(now)
    if p.checkin_day == d:
        return {"ok": False, "streak": p.checkin_streak}
    y = day_key(now - 86400)
    p.checkin_streak = p.checkin_streak + 1 if p.checkin_day == y else 1
    p.checkin_day = d
    money = min(CHECKIN_CAP, CHECKIN_MONEY + (p.checkin_streak - 1) * CHECKIN_STEP)
    p.money += money
    before = p.stamina
    p.stamina = min(STAMINA_MAX, p.stamina + CHECKIN_STAMINA)
    return {"ok": True, "streak": p.checkin_streak, "money": money,
            "stamina": round(p.stamina - before, 1)}


def has_gear(p: Player, gid: str) -> bool:
    return gid in p.gear


def level_name(p: Player) -> str:
    return LEVELS[max(0, min(p.level, len(LEVELS) - 1))]["name"]


def next_level_need(p: Player) -> tuple[int, int] | None:
    """(还差多少周收入, 需要多少) —— 满级返回 None。"""
    if p.level >= len(LEVELS) - 1:
        return None
    need = LEVELS[p.level + 1]["weekly"]
    return max(0, need - p.week_income), need


def is_peak(now: float) -> bool:
    lt = time.localtime(now)
    h, m = lt.tm_hour, lt.tm_min
    mm = h * 60 + m
    return (11 * 60 + 30 <= mm < 13 * 60) or (17 * 60 + 30 <= mm < 19 * 60)


def day_key(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def roll_weather(seed_key: str) -> str:
    """全群同一天气：由 (群, 日期) 决定性生成，不需要存储。"""
    rng = random.Random(hash(seed_key) & 0xFFFFFFFF)
    r = rng.random()
    if r < 0.62:
        return "sunny"
    if r < 0.88:
        return "rain"
    return "snow"


def regen_stamina(p: Player, now: float) -> bool:
    """懒计算体力恢复，返回是否有变化。"""
    if p.stamina_at <= 0:
        p.stamina_at = now
        return False
    if p.stamina >= STAMINA_MAX:
        p.stamina_at = now
        return False
    minutes = (now - p.stamina_at) / 60.0
    if minutes < STAMINA_REGEN_MIN:
        return False
    gain = int(minutes // STAMINA_REGEN_MIN)
    if gain <= 0:
        return False
    p.stamina = min(float(STAMINA_MAX), p.stamina + gain)
    if p.stamina >= STAMINA_MAX:
        p.stamina_at = now
    else:
        p.stamina_at += gain * STAMINA_REGEN_MIN * 60
    return True


def roll_day(p: Player, now: float) -> bool:
    """跨天重置。返回是否发生了重置。"""
    d = day_key(now)
    if p.day == d:
        return False
    if p.day:                                  # 先给「昨天」做个小结算
        if p.today_orders >= 5:
            if p.today_bad <= 1:
                p.rating = min(RATING_MAX, p.rating + 0.04)
            elif p.today_bad >= 4:
                p.rating = max(RATING_MIN, p.rating - 0.02)
        if p.rating < 4.2:                     # 站长兜底：不至于永远爬不起来
            p.rating = min(4.2, p.rating + 0.05)
    p.day = d
    p.today_orders = 0
    p.today_income = 0
    p.today_bad = 0
    p.today_meals = 0
    p.today_penalty = False
    p.pending = []
    p.pending_at = 0.0
    p.shortcut_armed = False
    return True


def rating_bucket(p: Player) -> str:
    if p.rating >= 4.8:
        return "premium"
    if p.rating >= 4.5:
        return "good"
    if p.rating >= 4.0:
        return "normal"
    return "floor"


# ---------------------------------------------------------------- 派单

def _timeout_probability(order: Order, p: Player, weather: str, peak: bool) -> float:
    w = WEATHERS[weather]
    veh = vehicle_of(p)
    # 基准超时率：老手、中等距离、有电梯、天气好这一档，超时约 24%；
    # 再加上配送失败约 6%，一单「跑砸」的概率就在三成上下。
    prob = 0.24
    if order.km > 6:
        prob += (order.km - 6) * 0.020
    if order.floor >= 6 and not order.elevator:
        prob += 0.12
    if order.slow:
        prob += 0.10
    wt = w["timeout"]
    if veh.get("weather_immune"):
        wt = 0.0                 # 有棚（汽车/老头乐），雨雪淋不着
    elif wt > 0 and has_gear(p, "raincoat"):
        wt *= 0.5
    prob += wt
    if peak:
        prob += veh.get("peak_mod", 0.08)     # 摩托能钻缝，只 +3%
        prob += veh.get("peak_extra", 0.0)    # 四轮堵车，汽车 +10% / 老头乐 +6%
    prob += veh["mod"]
    if p.orders < 10:              # 新手保护：头 10 单别一上来就把人劝退
        prob -= 0.20
    if has_gear(p, "map"):
        prob -= 0.05
    # 熟练度：每 100 单 −1%，最多 −8%
    prob -= min(0.08, p.orders / 100.0 * 0.01)
    if p.stamina < FATIGUE_LINE:
        prob += 0.06
    if p.sick > 0:
        prob += SICK_PENALTY
    # 车况差：闸不灵、灯不亮、链条松，跑起来就不稳
    if p.durability < DURABILITY_WARN:
        prob += 0.06
    elif p.durability < 50:
        prob += 0.03
    prob = max(0.02, min(0.85, prob))
    # 30% 以上的部分折半：雨雪+没电梯+出餐慢+老车叠起来原本能到八成，
    # 那不是难，是判死。压成「最悬也就六成」，玩家还有得选。
    if prob > 0.30:
        prob = 0.30 + (prob - 0.30) * 0.5
    return prob


def risk_label(prob: float) -> str:
    if prob < 0.20:
        return "低"
    if prob < 0.34:
        return "中"
    if prob < 0.50:
        return "高"
    return "极高"


def _fail_probability(order: Order, p: Player, weather: str, peak: bool) -> float:
    """配送失败：不是晚了，是压根没送到（联系不上人/门禁进不去/地址错/餐洒了没法交）。

    失败比超时更疼——配送费全没，还要赔餐费，评分掉得也多。所以概率低一档（6% 起），
    但会随距离、楼层、天气、体力累加；远单硬刚能爬到 15%，跟超时叠一起就是这单的三成。
    """
    veh = vehicle_of(p)
    prob = 0.06
    if order.km >= 8:
        prob += 0.02
    if order.floor >= 10 and not order.elevator:
        prob += 0.03
    wt = WEATHERS[weather]["timeout"]
    if veh.get("weather_immune"):
        wt = 0.0
    elif wt > 0 and has_gear(p, "raincoat"):
        wt *= 0.5
    prob += wt * 0.4             # 雨雪里「找不到门/进不去」的概率没那么高
    if p.stamina < FATIGUE_LINE:
        prob += 0.02
    if p.sick > 0:
        prob += 0.02
    if has_gear(p, "map"):
        prob -= 0.02             # 熟悉地图少跑错门
    if p.orders < 10:
        prob -= 0.02             # 新手保护
    return max(0.02, min(0.30, prob))


def distance_rate(km: float) -> float:
    """距离越远，每公里单价越高：2km 内 2.6，5km 内 3.0，8km 内 3.8，再远 4.6。"""
    if km <= 2:
        return 2.6
    if km <= 5:
        return 3.0
    if km <= 8:
        return 3.8
    return 4.6


def make_orders(p: Player, weather: str, peak: bool, rng: random.Random,
                count: int = 3, items: list[str] | None = None,
                bulk_items: list[str] | None = None,
                notes: list[str] | None = None,
                notes_goods: list[str] | None = None,
                shops: list[str] | None = None,
                whos: list[str] | None = None) -> list[Order]:
    """生成 count 张订单（默认 3 张），已按评分档位调整金额。

    shops: 叙事池给出的「品类|店名|商品」——店、货、图标三者配套，不会出现
           肯德基卖水果这种事。空的话退回内置餐饮店表。
    items: 只有拿不到三段的订单行时才用，按顺序给每张单配一条商品描述。
    bulk_items: 大件单专用的「整箱/成件」描述（三轮车才用得上）。
    notes: 餐饮单的顾客备注；notes_goods: 非餐饮单的备注（别混着用）。
    whos: 收件人称呼（王先生/刘女士），一批里不重复。
    """
    veh = vehicle_of(p)
    max_km = veh["max_km"]
    bucket = rating_bucket(p)
    cands = [x for x in (split_shop(s) for s in (shops or [])) if x]
    # 一批里尽量别重复同一家店：先抽 count 个，不够再随机补
    picked = (rng.sample(cands, count) if len(cands) >= count else
              [rng.choice(cands) for _ in range(count)] if cands else [])
    # 收件人称呼同理：一批三张单不要三个人都姓王
    who_pool = [w for w in (whos or []) if str(w).strip()]
    picked_who = (rng.sample(who_pool, count) if len(who_pool) >= count else
                  [rng.choice(who_pool) for _ in range(count)] if who_pool else [""] * count)
    out: list[Order] = []
    # 备注在同一批单里别重复：先打乱，按顺序发
    notes_food = list(notes or [])
    notes_other = list(notes_goods or notes or [])
    rng.shuffle(notes_food)
    rng.shuffle(notes_other)
    nf = no = 0
    for i in range(1, count + 1):
        # 星耀以上平台优先派远单、大单：三角分布的重心往远处推
        mode = max_km * (0.72 if p.level >= 4 else 0.55)
        km = round(rng.triangular(0.8, max_km, mode), 1)
        # 团餐/大额单：带 meal_p 的车才有，按 meal_mult/meal_add 加价
        meal_mult, meal_add = 0.0, 0.0
        if veh.get("meal_p") and rng.random() < veh["meal_p"]:
            meal_mult = veh.get("meal_mult", 1.8)
            meal_add = veh.get("meal_add", 5.0)
        group_meal = meal_mult > 0
        # 大件单：能拉货的车按 bulk_p 抽，整箱水/煤气罐多给 8 元
        bulk = bool(veh.get("bulk")) and rng.random() < veh.get("bulk_p", 0.18)
        if group_meal or bulk:
            km = round(max(km, 4.5), 1)
        shop, shop_emoji = rng.choice(SHOPS)
        ai_item, biz = "", ""
        if picked:
            got = picked[(i - 1) % len(picked)]
            if group_meal or bulk:
                # 团餐得是餐饮店，大件得是超市/生鲜/建材这类，别让花店接公司团餐
                want = ("餐饮",) if group_meal else ("超市", "生鲜", "建材", "五金", "水果")
                alt = [c for c in cands if c[0] in want]
                if alt:
                    got = rng.choice(alt)
            biz, shop, ai_item, shop_emoji = got
        dest, base_floor = rng.choice(DESTS)
        floor = max(1, min(30, base_floor + rng.randint(-2, 2)))
        if floor <= 6:
            elevator = rng.random() < 0.5
        else:
            elevator = rng.random() < 0.85
        pickup = 8 if rng.random() < 0.3 else 3
        slow = pickup == 8
        travel = km / veh["speed"] * 60.0
        stairs = 0.0 if elevator else floor * 0.8
        limit = max(6, int(math.ceil((travel + pickup + stairs) * 1.2)))

        # 按距离分档计价：远单单价高，但路上更久、更容易超时
        pay = km * distance_rate(km) + rng.uniform(-1.5, 4.0)
        if not elevator:
            pay += floor * 0.35
        premium = False
        if group_meal:
            pay = pay * meal_mult + meal_add
            premium = True
        elif bulk:
            pay += 8.0                      # 大件辛苦费
            if has_gear(p, "cart"):
                pay += 5.0                  # 折叠推车：一趟能拉完，多给点
            premium = True
        elif bucket == "premium" and rng.random() < 0.25:
            pay *= 1.5
            premium = True
        elif bucket in ("normal", "floor") and rng.random() < 0.10:
            pay *= 0.85
        # 贵车的「专送/加急」加价：不然跑到 14km 和 9km 的单一个价，白换车
        pay *= veh.get("pay_mul", 1.0)
        # 封顶跟着车走：能跑 18km 的车，单子金额自然比 5km 的车高
        cap = max(60, int(round(veh["max_km"] * 5.5)))
        pay = int(max(5, min(cap, round(pay))))

        # 等级：黄金起 派单金额 +10%
        if p.level >= 2:
            pay = int(round(pay * 1.10))

        # 评分掉到 4.0 以下的保底：金额不低于 9 元，避免死亡螺旋
        if bucket == "floor":
            pay = max(pay, 9)

        # 体力/车况影响不了金额，但天气补贴写在结算里
        # 商品描述：优先用「品类|店名|商品」里配好的那条，店和货才对得上
        item = ai_item
        if bulk:
            # 大件单换个说法：整箱水/煤气罐，别让「生煎包 8 个」顶着「大件」标签
            pool = bulk_items or items
            if pool:
                item = str(pool[(i - 1) % len(pool)] or "").strip()[:32]
        elif not item and items:
            item = str(items[(i - 1) % len(items)] or "").strip()[:32]
        # 顾客备注：奇怪要求的来源（也解释了为什么这单会有服务差评）
        note = ""
        # 「会洒的汤面单」和「其他单」的备注各说各的：别让花束订单出现「汤别洒一滴」
        wet = (not biz) or biz in WET_BIZ
        if wet and notes_food:
            note = str(notes_food[nf % len(notes_food)] or "").strip()[:32]
            nf += 1
        elif not wet and notes_other:
            note = str(notes_other[no % len(notes_other)] or "").strip()[:32]
            no += 1
        order = Order(idx=i, shop=shop, shop_emoji=shop_emoji, dest=dest, km=km,
                      floor=floor, elevator=elevator, pay=pay, speed=veh["speed"],
                      limit=limit, slow=slow, premium=premium, item=item,
                      note=note, bulk=bulk, biz=biz,
                      who=str(picked_who[(i - 1) % len(picked_who)] or ""))
        out.append(order)
    return out


def order_template(order: Order) -> str:
    lift = "有电梯" if order.elevator else "无电梯"
    return (f"{order.km}km ｜ {order.pay} 元 ｜ {order.floor} 楼 {lift} ｜ "
            f"限时 {order.limit} 分")


# ---------------------------------------------------------------- 结算

def settle(p: Player, order: Order, weather: str, peak: bool, now: float,
           rng: random.Random, shortcut: bool = False) -> Settlement:
    """结算一单。会就地修改玩家状态（体力/余额/评分/今日统计）。"""
    veh = vehicle_of(p)
    w = WEATHERS[weather]
    prob = _timeout_probability(order, p, weather, peak)
    if shortcut:
        prob = max(0.02, prob - 0.15)

    forced_late = False
    if order.elevator and order.floor >= 8 and rng.random() < 0.05:
        forced_late = True          # 电梯坏了，18 楼爬上去

    # 配送失败：不是晚了，是没送到（联系不上/门禁/地址错/洒了没法交）
    failed = (not forced_late) and rng.random() < _fail_probability(order, p, weather, peak)
    late = (not failed) and (forced_late or rng.random() < prob)
    st = Settlement(order=order, on_time=not late and not failed,
                    actual_min=0, late_min=0, income=0, delivery_fee=0)
    st.rating_before = round(p.rating, 2)

    if failed:
        st.failed = True
        st.actual_min = round(order.limit * rng.uniform(0.85, 1.3), 1)
        st.delivery_fee = 0
        st.compensate = int(min(25, max(8, round(order.pay * 0.8))))
        st.events.append("🚫 配送失败，这单退回去了")
        p.rating -= 0.07
        p.on_time_streak = 0
        if rng.random() < 0.5:      # 一半人会去投诉
            st.bad_review = 1
            st.bad_kind = "karen"
            p.rating -= 0.04
    elif late:
        st.actual_min = round(order.limit + rng.uniform(1.5, 8.0), 1)
        st.late_min = round(st.actual_min - order.limit, 1)
        # 超时罚款跟现实一样按分钟算：越晚扣得越多，扣的是这一单的钱
        # （起手扣一成半，每多迟到一分钟再加 5%，最多把这单的钱扣光，不倒欠）。
        # 按比例扣是为了让便宜的小单不至于一次超时就白跑一晚上。
        over = max(1, int(round(st.late_min)))
        ratio = min(1.0, 0.15 + 0.05 * over)
        penalty = max(1, min(int(order.pay), int(round(order.pay * ratio))))
        st.delivery_fee = -penalty
        st.events.append(f"❌ 超时 {st.late_min:g} 分，罚 {penalty} 元")
        if rng.random() < 0.20:
            st.bad_review = 1
            st.bad_kind = "late"
            p.rating -= 0.04
        if forced_late:
            st.events.append("🛗 电梯坏了，爬了 %d 楼" % order.floor)
        p.rating -= 0.04
        p.on_time_streak = 0
    else:
        st.actual_min = round(order.limit * rng.uniform(0.68, 0.97), 1)
        st.delivery_fee = order.pay
        p.rating += 0.01
        p.on_time_streak += 1
        # 服务差评（餐凉/磕碰）：准时单也可能挨，保温箱 −30%
        svc = 0.03 + (0.02 if weather != "sunny" else 0.0)
        if has_gear(p, "box"):
            svc *= 0.7
        if rng.random() < svc:
            st.bad_review = 1
            # 餐单是「洒凉」，非餐饮单（药/花/快递/衣服）是「磕碰压坏」
            food_st = (not order.biz) or order.biz in FOOD_BIZ
            st.bad_kind = st.bad_kind or ("pack" if food_st else "damage")
            p.rating -= 0.06
        # 好评（15%，只可能发生在准时单；已经挨了差评就别再夸，自相矛盾）
        if st.bad_review == 0 and rng.random() < 0.15:
            st.tip = rng.randint(2, 8)
            st.events.append(f"⭐ 好评，顾客打赏 {st.tip} 元")
            p.rating += 0.02

    # ---- 随机事件
    # 恶意投诉先判：一旦挨了差评，后面就不许再出现「打赏」这种相反的事
    if rng.random() < 0.03:
        st.bonus -= 10
        st.bad_review += 1
        st.bad_kind = st.bad_kind or "karen"
        st.events.append("😤 恶意投诉，罚 10 元 + 差评")
        p.rating -= 0.03
        if st.tip:
            # 已经夸过又翻脸投诉：把那条好评和打赏一起撤掉，别自相矛盾
            st.tip = 0
            st.events = [e for e in st.events if not e.startswith("⭐ 好评")]
    if st.bad_review == 0 and not st.failed and rng.random() < 0.10:
        st.bonus += 5
        st.events.append("💰 顾客额外打赏 5 元")
    if not st.failed and rng.random() < 0.08:
        st.bonus += 6
        st.events.append("↪️ 顺路捎了一单，额外 +6 元")
    if veh.get("accident"):
        accident_p = veh.get("accident_p", 0.03) * (0.5 if has_gear(p, "helmet") else 1.0)
        if rng.random() < accident_p:
            st.accident = True
            st.fine += 50
            p.stamina -= 20
            crash = "🛺 车斗翻了！" if veh["id"] == "tricycle" else "🚑 摔车了！"
            st.events.append(f"{crash}本单超时 + 罚款 50 元 + 体力 −20")
            if not late and not st.failed:
                late = True
                st.on_time = False
                st.tip = 0
                st.actual_min = round(order.limit + rng.uniform(3, 9), 1)
                st.late_min = round(st.actual_min - order.limit, 1)
                st.delivery_fee = -5
                st.bad_review = max(st.bad_review, 1)
                st.bad_kind = st.bad_kind or "late"
                p.rating -= 0.05
    if veh.get("police"):
        if rng.random() < 0.03:
            fine = 20 if has_gear(p, "permit") else 50
            st.fine += fine
            head = "交警查车（无牌老头乐）" if veh["id"] == "elder" else "交警查车"
            tail = "，罚款 %d 元" % fine + ("（通行证少赔点）" if has_gear(p, "permit") else "")
            st.events.append(f"👮 {head}{tail}")
    if veh.get("puncture") and p.sick == 0 and rng.random() < 0.04:
        p.sick = SICK_ORDERS
        st.events.append(f"🛠 爆胎了！接下来 {SICK_ORDERS} 单车况差、更容易超时，"
                         f"`#修车` 立刻修好")

    # ---- 车况：跑一单磨一点，磨没了就真跑不动了
    st.wear = vehicle_wear(p, order, weather, shortcut)
    p.durability = max(0.0, round(p.durability - st.wear, 1))
    st.durability_after = p.durability
    if p.durability <= 0:
        st.events.append("🛑 车彻底跑不动了，修好才能接单（`#修车`）")

    # ---- 天气补贴（只对正式配送费生效）
    if st.delivery_fee > 0 and w["bonus"] > 0:
        extra = int(round(st.delivery_fee * w["bonus"]))
        st.bonus += extra
        st.events.append(f"{w['emoji']} {w['name']}天补贴 +{extra} 元")

    st.cost = veh["cost"]
    if p.level >= 5:
        st.cost = 0                  # 王者：平台补贴车辆损耗

    st.income = st.delivery_fee + st.tip + st.bonus - st.cost - st.fine - st.compensate

    # ---- 写回玩家
    if st.bad_review:
        p.today_bad += st.bad_review
    p.money += st.income
    if p.money < 0:
        p.money = 0
    burn = STAMINA_PER_ORDER - (1.0 if has_gear(p, "saddle") else 0.0)
    p.stamina = max(0.0, p.stamina - burn)
    if p.sick > 0:
        p.sick -= 1
    p.orders += 1
    p.today_orders += 1
    p.today_income += st.income
    p.week_income += st.income
    p.rating = max(RATING_MIN, min(RATING_MAX, p.rating))

    # 今日 4 个差评 → 当日总收入 −15%（一次性）
    if p.today_bad >= 4 and not p.today_penalty:
        p.today_penalty = True
        fine = int(round(max(0, p.today_income) * 0.15))
        if fine > 0:
            p.money = max(0, p.money - fine)
            p.today_income -= fine
            st.broke = True
            st.events.append(f"📉 今日差评满 4 个，扣当日收入 15%（−{fine} 元）")

    st.rating_after = round(p.rating, 2)
    st.stamina_after = p.stamina
    st.money_after = p.money
    st.today_income = p.today_income
    st.today_orders = p.today_orders
    st.today_bad = p.today_bad
    return st


# ---------------------------------------------------------------- 杂项判定

def can_take_orders(p: Player) -> str | None:
    """返回不能接单的原因，能接返回 None。"""
    if p.today_orders >= DAY_ORDER_CAP:
        return f"今天已经跑了 {p.today_orders} 单，到点了，回去睡吧（每日上限 {DAY_ORDER_CAP} 单）"
    if p.stamina < STAMINA_PER_ORDER:
        return "体力见底了，跑不动。歇一会儿（每 6 分钟回 1 点）或者 `#吃饭` 15 元回 40 体力"
    if p.durability <= 0:
        return "车彻底跑不动了（耐久 0），先 `#修车` 修好再跑。"
    return None


def order_income_hint(p: Player, order: Order, weather: str, peak: bool) -> float:
    """给派单卡片用的「这一单跑砸的概率」：超时或没送到，二选一发生就算砸。

    两个概率是独立判定的，所以合起来用 1-(1-a)(1-b)，别把失败再额外乘个系数
    （那样会把 30% 显示成 38%，群里看着就玄乎）。
    """
    late = _timeout_probability(order, p, weather, peak)
    miss = _fail_probability(order, p, weather, peak)
    return 1.0 - (1.0 - late) * (1.0 - miss)
