"""风里雨里 · 外卖骑手 —— 大肥鱼 QQ 群里的骑手人生。

接单 → 送单 → 超时/好评 → 攒钱换车 —— 一天两三分钟，聊着天顺手跑两单。

指令用 # 开头（/ 也行），不走 LLM，纯本地状态机：
    #接单 / #接 1 / #跳过 / #我的 / #收入榜 / #抢单 / #抢 2 / #商店 / #买 3
    #吃饭 / #修车 / #抄近道 / #帮助
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core import game, render
from .core.game import (
    DAY_ORDER_CAP, GEARS, LEVELS, MEAL_PER_DAY, MEAL_PRICE, MEAL_STAMINA,
    STAMINA_MAX, VEHICLES, Order, Player, day_key, is_peak,
    make_orders, regen_stamina, roll_alert, roll_day, roll_weather, settle, vehicle_of,
)
from .core.narrator import Narrator
from .core.store import Store

VERBS = (
    "接单", "派单", "抢单", "抄近道", "收入榜", "排行榜", "榜单", "周榜", "总榜",
    "我的", "状态", "钱包", "商店", "吃饭", "修车", "签到", "帮助", "跳过", "任务",
    "押注", "打赏", "接", "抢", "买", "押", "叙事池",
)
_CMD_RE = re.compile(r"^(?P<verb>%s)\s*(?P<arg>.*)$" % "|".join(VERBS))
_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}

PENDING_TTL = 300          # 派单 5 分钟内有效
COOLDOWN = 1.0             # 同一个人两条指令的最小间隔（秒）
SKIP_STAMINA = 2           # 跳过一批，等车也得费点体力


def _clean(text: str) -> str:
    t = (text or "").strip()
    while t and t[0] in "/#／＃":
        t = t[1:].lstrip()
    return t


def _num(arg: str) -> int | None:
    a = (arg or "").strip()
    if not a:
        return None
    # 只认正整数：`#接 -1` 以前会被 re.search 抓成 1，等于替人接了个单
    m = re.search(r"(?<![0-9])-?[0-9]+", a)
    if m:
        v = int(m.group())
        return v if v > 0 else None
    for ch in a:
        if ch in _NUM:
            return _NUM[ch]
    return None


def _parse_hhmm(text: str) -> tuple[int, int, bool]:
    """播报时间。配错（25:00 / 21:70 / 空）以前会把 CronTrigger 抛错带崩 initialize，
    整个插件加载失败——现在一律夹回合法范围并回落 21:00。
    """
    m = re.fullmatch(r"\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*", str(text or ""))
    if not m:
        return 21, 0, False
    hh, mm = int(m.group(1)), int(m.group(2))
    ok = (0 <= hh <= 23) and (0 <= mm <= 59)
    if not (0 <= hh <= 23):
        hh = 21
    if not (0 <= mm <= 59):
        mm = 0
    return hh, mm, ok


@register(
    "astrbot_plugin_dafeiyu_waimai",
    "dsh",
    "风里雨里 · 外卖骑手：群里接单送单，攒钱换车",
    "v0.11.0",
)
class WaimaiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        conf = config or {}
        self.enabled = bool(conf.get("enabled", True))
        self.groups_only = bool(conf.get("groups_only", False))
        self.broadcast_time = str(conf.get("broadcast_time", "21:00") or "21:00")
        self.night_broadcast = bool(conf.get("night_broadcast", True))
        # 每单现编剧情（直接调大肥鱼背后的模型），关掉就只用池子里的素材
        self.live_ai = bool(conf.get("live_ai", True))
        try:
            self.live_timeout = max(1.0, min(15.0, float(conf.get("live_timeout", 10.0))))
        except Exception:
            self.live_timeout = 10.0
        self.data_dir = Path(__file__).parent / "data"
        self.store = Store(self.data_dir / "waimai.db")
        self.narrator = Narrator(str(self.data_dir / "narrator.json"))
        self._rng = random.Random()
        self._last_cmd: dict[str, float] = {}
        self._cool_warn: dict[str, float] = {}
        self._warm: asyncio.Task | None = None
        self._catchup: asyncio.Task | None = None
        self._sched: AsyncIOScheduler | None = None

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        await self.store.open()
        self.narrator.log = lambda msg: logger.info(msg)
        self.narrator.load()
        self._sched = AsyncIOScheduler()
        hh, mm, ok = _parse_hhmm(self.broadcast_time)
        if not ok:
            logger.warning(f"[waimai] broadcast_time={self.broadcast_time!r} 不合法，按 21:00 播报")
        self._sched.add_job(self._night, CronTrigger(hour=hh, minute=mm),
                            id="waimai_night", replace_existing=True,
                            coalesce=True, misfire_grace_time=3600)
        self._sched.add_job(self._weekly, CronTrigger(day_of_week="mon", hour=0, minute=10),
                            id="waimai_weekly", replace_existing=True,
                            coalesce=True, misfire_grace_time=3600 * 6)
        self._sched.start()
        logger.info(f"[waimai] 就绪：{hh:02d}:{mm:02d} 榜单播报，周一结算段位")
        # 启动后自己预热一遍素材池：不等人发 #接单，先把 AI 文案攒起来
        try:
            self._warm = asyncio.create_task(self._warm_narrator())
        except Exception:
            self._warm = None
        # 关机错过播报（笔记本合盖、重启）就补一次，靠 night 标记防重复
        try:
            self._catchup = asyncio.create_task(self._night_catchup(hh, mm))
        except Exception:
            self._catchup = None

    async def _night_catchup(self, hh: int, mm: int) -> None:
        """启动时间已经过了今晚的播报点，就补播一次；没到点、已经播过都不动。"""
        await asyncio.sleep(20)
        now = time.localtime()
        if (now.tm_hour, now.tm_min) < (hh, mm):
            return
        try:
            await self._night()
        except Exception as e:
            logger.warning(f"[waimai] 补播失败 {type(e).__name__}: {e}")

    async def _warm_narrator(self) -> None:
        await asyncio.sleep(8)
        # 模型偶尔会卡一次（实测启动那一下可能 60 秒超时），所以预热最多试三轮
        for attempt in range(3):
            try:
                if not self.narrator.needs():
                    break
                await self._refill_narrator(None)
                logger.info(f"[waimai] 素材池预热：{self.narrator.sizes()}")
            except Exception as e:
                logger.warning(f"[waimai] 素材池预热失败 {type(e).__name__}: {e}")
            if not self.narrator.needs():
                break
            await asyncio.sleep(20 if attempt == 0 else 40)

    async def terminate(self) -> None:
        self.narrator.cancel()
        # 关插件时把还在跑的预热/补播任务收掉，否则重载会叠出两份
        tasks = [t for t in (self._warm, self._catchup) if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except Exception:
                pass
        self._warm = self._catchup = None
        if self._sched:
            try:
                self._sched.shutdown(wait=False)
            except Exception:
                pass
            self._sched = None
        await self.store.close()

    # ------------------------------------------------------------ 上下文工具

    def _day(self, now: float | None = None) -> str:
        return day_key(now or time.time())

    def _weather(self, umo: str, now: float | None = None) -> str:
        return roll_weather(f"{umo}:{self._day(now)}")

    def _alert(self, umo: str, now: float | None = None) -> dict | None:
        """今天的突发天气（雷暴/大风/大雾），没有就是 None。"""
        return roll_alert(f"{umo}:{self._day(now)}")

    def _reply(self, event: AstrMessageEvent, text: str):
        """群里的卡片一张接一张，谁也看不出是在回谁——每条都顶一个「@发起人」。

        同一条指令出第二、三张卡（结算卡后面跟故事卡）时改成「@名字」纯文字：
        看着还是他的单，但不再多打一次提醒。私聊不 @（就两个人，反而碍眼）。
        """
        try:
            if not event.is_private_chat():
                uid = str(event.get_sender_id() or "")
                name = ""
                try:
                    name = (event.get_sender_name() or "").strip()[:16]
                except Exception:
                    pass
                if uid:
                    if getattr(event, "_waimai_at_done", False) and name:
                        return event.plain_result(f"@{name} \n{text}")
                    try:
                        setattr(event, "_waimai_at_done", True)
                    except Exception:
                        pass
                    return event.chain_result([At(qq=uid), Plain("\n" + text)])
        except Exception:
            pass
        return event.plain_result(text)

    async def _player(self, event: AstrMessageEvent) -> Player:
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())
        name = ""
        try:
            name = (event.get_sender_name() or "").strip()[:16]
        except Exception:
            pass
        p = await self.store.get_player(umo, uid, name)
        now = time.time()
        regen_stamina(p, now)
        if roll_day(p, now):
            await self.store.save_player(umo, p)
        return p

    # ------------------------------------------------------------ AI 叙事
    #
    # 判定（超时/差评/出餐慢）仍然走本地概率，保证公平、经济曲线不崩；
    # 这里只把「为什么」编出来：顾客点了什么、为什么差评、商家怎么出餐慢。
    # 素材先由 LLM 批量生成、落盘缓存，用的时候零延迟——所以接单能马上出结果。

    def _story(self, st) -> dict:
        o = st.order
        # 非餐饮单（药/花/快递/数码/衣服）的说法里不许出现汤菜饭
        food = (not o.biz) or o.biz in game.FOOD_BIZ
        return self.narrator.story(
            late=not st.on_time, slow=bool(o.slow),
            bad_kind=getattr(st, "bad_kind", ""), praised=bool(st.tip),
            rng=self._rng, food=food, failed=bool(getattr(st, "failed", False)))

    async def _live_story(self, st, event: AstrMessageEvent | None = None,
                          budget: float | None = None) -> dict:
        """这一单现编：拿模型写，超时/失败立刻退回池子，别让群里等模型。

        budget：这一单最多等模型几秒。结算卡是唯一那条能发出去的消息，
        卡片要等剧情，所以宁可 2 秒拿不到就退回内置池子，也不让群里干等。
        """
        o = st.order
        food = (not o.biz) or o.biz in game.FOOD_BIZ
        failed = bool(getattr(st, "failed", False))
        if self.live_ai:
            prov = await self._provider(event)
            if prov is not None:
                try:
                    got = await asyncio.wait_for(
                        self.narrator.live_story(
                            prov, who=o.who, biz=o.biz, shop=o.shop, item=o.item,
                            food=food, late=not st.on_time, slow=bool(o.slow),
                            bad_kind=getattr(st, "bad_kind", ""), praised=bool(st.tip),
                            timeout=self.live_timeout, failed=failed),
                        timeout=budget or self.live_timeout + 1,
                    )
                except Exception:      # 超时 / 模型抽风 / JSON 不合格：都退回池子
                    got = None
                if got:
                    return got
        return self._story(st)

    async def _provider(self, event: AstrMessageEvent | None = None):
        umo = getattr(event, "unified_msg_origin", None) if event else None
        prov = None
        try:
            prov = await self.context.get_using_provider_async(umo)
        except Exception:
            prov = None
        if prov is None and umo:
            # 会话没绑定模型时退回全局默认（这就是之前池子一直是空的坑）
            try:
                prov = await self.context.get_using_provider_async()
            except Exception:
                prov = None
        return prov

    async def _refill_narrator(self, event: AstrMessageEvent | None = None) -> None:
        """池子不够就在后台补一池，绝不阻塞这一单的结算。"""
        if not self.narrator.needs():
            return
        prov = await self._provider(event)
        if prov is None:
            self.narrator._log("[waimai] 叙事池补货失败：没取到对话模型（provider=None）")
            self.narrator.last_error = "provider=None（会话没绑定模型）"
            return
        try:
            self.narrator.ensure(prov)
        except Exception as e:
            self.narrator._log(f"[waimai] 叙事池补货失败 {type(e).__name__}: {e}")

    # ------------------------------------------------------------ 指令入口

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        if not self.enabled:
            return
        raw = (event.get_message_str() or "").strip()
        if not raw or raw[0] not in "/#／＃":
            return
        if self.groups_only and event.is_private_chat():
            return
        m = _CMD_RE.match(_clean(raw))
        if not m:
            return
        verb, arg = m.group("verb"), m.group("arg")
        key = f"{event.unified_msg_origin}|{event.get_sender_id()}"
        now = time.time()
        if now - self._last_cmd.get(key, 0) < COOLDOWN:
            event.stop_event()
            # 以前这里静默 return：手快的人发两条，第二条没反应，以为插件死了。
            # 但每条都提示也吵，所以同一个人的提示 5 秒内只发一次。
            if now - self._cool_warn.get(key, 0) > 5:
                self._cool_warn[key] = now
                yield self._reply(event, "⏱ 慢一点，一秒钟只认一条指令。")
            return
        self._last_cmd[key] = now
        if len(self._last_cmd) > 512:      # 群里人多，字典别无限长
            for k, t in list(self._last_cmd.items()):
                if now - t > 120:
                    self._last_cmd.pop(k, None)
        if len(self._cool_warn) > 256:
            for k, t in list(self._cool_warn.items()):
                if now - t > 60:
                    self._cool_warn.pop(k, None)

        event.stop_event()
        try:
            async for out in self._dispatch(verb, arg, event):
                yield out
        except Exception as e:            # 出错也要说话，别让群友以为死了
            logger.exception("[waimai] 指令出错")
            yield self._reply(event, f"🛵 系统抽风了（{type(e).__name__}），这单不算，再试一次？")

    async def _dispatch(self, verb: str, arg: str, event: AstrMessageEvent):
        umo = event.unified_msg_origin

        if verb in ("帮助",):
            yield self._reply(event, render.help_card())

        elif verb in ("接单", "派单"):
            async for r in self._dispatch_orders(umo, event):
                yield r

        elif verb == "接":
            async for r in self._take(umo, arg, event):
                yield r

        elif verb == "跳过":
            async for r in self._skip(umo, event):
                yield r

        elif verb in ("我的", "状态", "钱包"):
            p = await self._player(event)
            yield self._reply(event, render.status_card(p, self._weather(umo), is_peak(time.time())))

        elif verb in ("收入榜", "排行榜", "榜单"):
            day = self._day()
            rows = await self.store.leaderboard(umo, 10, day)
            yield self._reply(event, render.leaderboard_card(day, rows))

        elif verb == "周榜":
            rows = await self.store.leaderboard(umo, 10, by="week")
            yield self._reply(event, render.leaderboard_card(self._day(), rows, by="week"))

        elif verb == "总榜":
            rows = await self.store.leaderboard(umo, 10, by="total")
            yield self._reply(event, render.leaderboard_card(self._day(), rows, by="total"))

        elif verb == "任务":
            p = await self._player(event)
            yield self._reply(event, render.tasks_card(p))

        elif verb in ("押", "押注"):
            async for r in self._bet(umo, arg, event):
                yield r

        elif verb == "打赏":
            async for r in self._tip(umo, arg, event):
                yield r

        elif verb == "抢单":
            async for r in self._list_limited(umo, event):
                yield r

        elif verb == "抢":
            async for r in self._grab(umo, arg, event):
                yield r

        elif verb == "商店":
            p = await self._player(event)
            page = "gear" if ("装备" in (arg or "") or "gear" in (arg or "").lower()) else "car"
            yield self._reply(event, render.shop_card(p, page))

        elif verb == "买":
            async for r in self._buy(umo, arg, event):
                yield r

        elif verb == "吃饭":
            async for r in self._eat(umo, event):
                yield r

        elif verb == "修车":
            async for r in self._repair(umo, event):
                yield r

        elif verb == "签到":
            async for r in self._checkin(umo, event):
                yield r

        elif verb == "抄近道":
            async for r in self._shortcut(umo, event):
                yield r

        elif verb == "叙事池":
            async for r in self._narrator_probe(event):
                yield r

    async def _narrator_probe(self, event: AstrMessageEvent):
        """#叙事池：既看一眼现状，也当场试补一池，把失败原因显式打出来。"""
        yield self._reply(event, self._narrator_card())
        try:
            prov = await self._provider(event)
        except Exception as e:
            yield self._reply(event, f"⚠️ 取对话模型报错：{type(e).__name__}: {e}")
            return
        if prov is None:
            yield self._reply(event, "⚠️ 这个会话没绑定对话模型（provider=None），文案只能用内置兜底。")
            return
        sizes = self.narrator.sizes()
        kind = min(self.narrator.KINDS, key=lambda k: sizes.get(k, 0))
        cfg = getattr(prov, "provider_config", None) or {}
        model = cfg.get("model", "") if isinstance(cfg, dict) else ""
        yield self._reply(event, f"⏳ 正在向模型要一池「{kind}」…（模型 {model or '未知'}）")
        await self.narrator._refill(prov, kind)
        after = self.narrator.sizes()
        got = after.get(kind, 0) - sizes.get(kind, 0)
        if got > 0:
            yield self._reply(event, f"✅ 「{kind}」补进了 {got} 条，现在共 {after.get(kind, 0)} 条。")
        else:
            yield self._reply(event, 
                f"❌ 「{kind}」没补上：{self.narrator.last_error or '模型返回空'}\n"
                "（这条报错原文会写进日志，抄给我就能定位）")

    def _narrator_card(self) -> str:
        """#叙事池：看 AI 素材池到底补上了没有，卡在哪一步。"""
        sizes = self.narrator.sizes()
        total = sum(sizes.values())
        lines = ["🗃 叙事素材池（AI 生成 + 内置兜底）", ""]
        lines.append("池子：" + "　".join(f"{k} {n}" for k, n in sizes.items()))
        lines.append(f"合计 {total} 条，低于 6 条的池会自动向模型要新的。")
        if self.narrator.live_ok or self.narrator.live_fail:
            lines.append(f"⚡ 实时现编：成功 {self.narrator.live_ok} 次 / 失败 "
                         f"{self.narrator.live_fail} 次，最近一次 {self.narrator.live_ms} ms")
            if self.narrator.live_err:
                lines.append(f"　最近一次实时失败：{self.narrator.live_err}")
        err = self.narrator.last_error
        if err:
            lines.append(f"⚠️ 最近一次失败：{err}")
        elif self.narrator.last_ok:
            ago = int(time.time() - self.narrator.last_ok)
            lines.append(f"✅ 最近一次成功：{ago} 秒前，拿到 {self.narrator.last_got} 条")
        else:
            lines.append("还没成功补过货（先在群里 #接单 一次，补货是异步的）")
        return "\n".join(lines)

    # ------------------------------------------------------------ 派单 / 接单

    async def _dispatch_orders(self, umo: str, event: AstrMessageEvent):
        p = await self._player(event)
        now = time.time()
        bad = game.can_take_orders(p)
        if bad:
            yield self._reply(event, f"🚫 {bad}")
            return
        if p.pending and now - p.pending_at < PENDING_TTL:
            yield self._reply(event, 
                f"手上还有 {len(p.pending)} 张单没挑（{int(PENDING_TTL - (now - p.pending_at))} 秒后作废）。"
                "先 #接 1 或者 #跳过。")
            return
        weather, peak = self._weather(umo, now), is_peak(now)
        alert = self._alert(umo, now)
        shops = self.narrator.many_pool("shops")
        bulk_items = self.narrator.pick_many("bulk", 2, self._rng)
        notes = self.narrator.many_pool("note")
        notes_goods = self.narrator.many_pool("note_goods")
        orders = make_orders(p, weather, peak, self._rng, 3, shops=shops,
                            bulk_items=bulk_items, notes=notes, notes_goods=notes_goods,
                            whos=self.narrator.pick_many("names", 3, self._rng))
        p.pending = [o.to_dict() for o in orders]
        p.pending_at = now
        await self.store.save_player(umo, p)
        yield self._reply(event, render.dispatch_card(p, orders, weather, peak, alert))
        await self._refill_narrator(event)

    async def _take(self, umo: str, arg: str, event: AstrMessageEvent):
        p = await self._player(event)
        now = time.time()
        idx = _num(arg)
        if not p.pending:
            yield self._reply(event, "还没派单呢，先发 #接单。")
            return
        if now - p.pending_at > PENDING_TTL:
            p.pending = []
            await self.store.save_player(umo, p)
            yield self._reply(event, "⏳ 这几张单太久没人接，被别的骑手抢走了。再 #接单 看看。")
            return
        if idx is None or not (1 <= idx <= len(p.pending)):
            yield self._reply(event, f"要接哪张？回 #接 1 / #接 2 / #接 3。")
            return
        bad = game.can_take_orders(p)
        if bad:
            yield self._reply(event, f"🚫 {bad}")
            return
        order = Order.from_dict(p.pending[idx - 1])
        p.pending = []
        weather, peak = self._weather(umo, now), is_peak(now)
        shortcut = bool(p.shortcut_armed)
        if shortcut:
            p.shortcut_armed = False

        st = settle(p, order, weather, peak, now, self._rng, shortcut=shortcut,
                    alert=self._alert(umo, now))
        await self.store.save_player(umo, p)
        if st.failed:                     # 没送到：原因交给叙事池，卡片里要写清楚
            st.fail_reason = self.narrator.pick("fail", self._rng)
        # 一条消息发完：别的插件在第一条消息发出去之后就把事件 stop 了，
        # 生成器后面的 yield 全会被丢掉（群里只剩半截）。所以钱、时间、
        # 剧情必须拼在同一张卡里，不能拆成两条。
        story = await self._live_story(st, event, budget=2.5)
        try:
            card = render.settle_card(p, st, weather, peak, story=story)
        except Exception:
            # 钱已经落盘了，这里再报「这单不算」就变成数据和说法打架
            logger.exception("[waimai] 结算卡渲染失败")
            card = (f"🛵 这单结算完成（{st.income:+d} 元），卡片没渲染出来，"
                    f"💰 余额 {int(p.money)} 元。")
        yield self._reply(event, card)
        await self._refill_narrator(event)

    async def _skip(self, umo: str, event: AstrMessageEvent):
        p = await self._player(event)
        if not p.pending:
            yield self._reply(event, "本来就没单，直接 #接单 吧。")
            return
        p.pending = []
        p.pending_at = 0.0
        p.stamina = max(0.0, p.stamina - SKIP_STAMINA)
        await self.store.save_player(umo, p)
        yield self._reply(event, 
            f"🔄 这批不接了，掉头回商圈等下一批（⚡ 体力 −{SKIP_STAMINA} → {int(p.stamina)}）。再 #接单。")

    # ------------------------------------------------------------ 限量单

    def _limited_orders(self, umo: str, day: str) -> list[Order]:
        rng = random.Random(f"{umo}|{day}|limited")
        out = []
        for i in range(1, 4):
            km = round(rng.uniform(3.5, 11.5), 1)
            shop, emoji = rng.choice(game.SHOPS)
            dest, base = rng.choice(game.DESTS)
            floor = max(1, min(30, base + rng.randint(-2, 2)))
            elevator = rng.random() < 0.6
            pay = rng.randint(30, 60)
            travel = km / 25.0 * 60 + 4 + (0 if elevator else floor * 0.8)
            triple = game.split_shop(self.narrator.pick("shops", rng)) or ("", shop, "", emoji)
            biz_emoji = triple[3] or emoji
            food = (not triple[0]) or triple[0] in game.FOOD_BIZ
            note_pool = self.narrator.many_pool("note" if food else "note_goods") or [""]
            out.append(Order(
                idx=i, shop=triple[1], shop_emoji=biz_emoji, dest=dest, km=km, floor=floor,
                elevator=elevator, pay=pay, speed=25,
                limit=max(8, int(math.ceil(travel * 1.15))),
                slow=False, premium=True,
                item=triple[2],
                note=rng.choice(note_pool),
                biz=triple[0],
                who=self.narrator.pick("names", rng),
            ))
        return out

    async def _list_limited(self, umo: str, event: AstrMessageEvent):
        p = await self._player(event)
        bad = game.can_take_orders(p)
        if bad:
            yield self._reply(event, f"🚫 {bad}")
            return
        slots = await self.store.ensure_limited(umo, self._day(), self._limited_orders(umo, self._day()))
        yield self._reply(event, render.limited_card(slots, self._weather(umo), p))

    async def _grab(self, umo: str, arg: str, event: AstrMessageEvent):
        p = await self._player(event)
        now = time.time()
        idx = _num(arg)
        if idx is None or idx not in (1, 2, 3):
            yield self._reply(event, "要抢哪一单？先 #抢单 看看，再回 #抢 3。")
            return
        bad = game.can_take_orders(p)
        if bad:
            yield self._reply(event, f"🚫 {bad}")
            return
        day = self._day()
        order = await self.store.get_limited_order(umo, day, idx)
        if order is None:
            await self.store.ensure_limited(umo, day, self._limited_orders(umo, day))
            order = await self.store.get_limited_order(umo, day, idx)
        if order is None:
            yield self._reply(event, "今天没有这一单，发 #抢单 看看。")
            return
        if order.km > vehicle_of(p)["max_km"]:
            yield self._reply(event, 
                f"🚫 这单 {order.km}km，{vehicle_of(p)['name']} 跑不了这么远，换张近的。")
            return
        ok = await self.store.claim_limited(umo, day, idx, p.uid, p.name)
        if not ok:
            yield self._reply(event, "💨 手慢了，这单已经被别的骑手抢走了。")
            return
        # 抢来的单宽限：钻石段位给 2 分钟，新手机再给 2 分钟（两样都有就是 4 分钟）
        if p.level >= 3:
            order.limit += 2
        if game.has_gear(p, "phone"):
            order.limit += 2
        weather, peak = self._weather(umo), is_peak(now)
        # 抢到就意味着这一轮结束了：手上没挑的那批一起作废，
        # 否则 #接 1 会在「已经跑完一单」之后再结算一张旧单。
        p.pending = []
        p.pending_at = 0.0
        try:
            st = settle(p, order, weather, peak, now, self._rng,
                        alert=self._alert(umo, now))
            await self.store.save_player(umo, p)
            if st.failed:
                st.fail_reason = self.narrator.pick("fail", self._rng)
            # 同样一条发完：抢单这条指令只 yield 一次，否则结算卡会被丢掉，
            # 群里就只剩一句「抢到了」没有下文。
            story = await self._live_story(st, event, budget=2.5)
        except Exception:
            # 结算炸了就把名额还回去，别让这单变成「已被抢走」永远消失
            await self.store.release_limited(umo, day, idx)
            raise
        yield self._reply(event, 
            f"⚡ {p.name} 抢到了！\n"
            + render.settle_card(p, st, weather, peak, story=story))
        await self._refill_narrator(event)

    # ------------------------------------------------------------ 商店 / 生活

    async def _buy(self, umo: str, arg: str, event: AstrMessageEvent):
        p = await self._player(event)
        idx = _num(arg)
        if idx is None:
            yield self._reply(event, "买几号？先 #商店 看清编号，再 #买 3。")
            return
        n_veh = len(VEHICLES)
        if 1 <= idx <= n_veh:
            vi = idx - 1
            veh = VEHICLES[vi]
            if vi <= p.vehicle:
                yield self._reply(event, f"你已经比 {veh['name']} 强了，不用买。")
                return
            if p.money < veh["price"]:
                yield self._reply(event, 
                    f"💰 {veh['name']} 要 {veh['price']} 元，你还差 "
                    f"{veh['price'] - int(p.money)} 元。先跑几单。")
                return
            p.money -= veh["price"]
            p.vehicle = vi
            p.durability = float(game.DURABILITY_MAX)   # 新车，车况满格
            p.sick = 0
            p.pending = []          # 换车了，手上那批按旧车速算的单作废
            await self.store.save_player(umo, p)
            yield self._reply(event, 
                f"{veh['emoji']} 换车了！{veh['name']}（{veh['speed']}km/h，能跑 {veh['max_km']}km）。"
                f"💰 余额 {int(p.money)} 元。\n🚦 {veh['desc']}\n"
                f"🛠 新车，车况 {int(game.DURABILITY_MAX)}/{int(game.DURABILITY_MAX)}。")
            return
        gi = idx - n_veh - 1
        if not (0 <= gi < len(GEARS)):
            yield self._reply(event, "没有这个编号，发 #商店 看看。")
            return
        g = GEARS[gi]
        if g["id"] in p.gear:
            yield self._reply(event, f"你已经买了 {g['emoji']} {g['name']}。")
            return
        if p.money < g["price"]:
            yield self._reply(event, f"💰 还差 {g['price'] - int(p.money)} 元。")
            return
        p.money -= g["price"]
        p.gear.append(g["id"])
        await self.store.save_player(umo, p)
        yield self._reply(event, 
            f"{g['emoji']} 买了 {g['name']}（{g['desc']}）。💰 余额 {int(p.money)} 元。")

    async def _eat(self, umo: str, event: AstrMessageEvent):
        p = await self._player(event)
        if p.today_meals >= MEAL_PER_DAY:
            yield self._reply(event, f"🍚 今天已经吃了 {MEAL_PER_DAY} 顿，再吃跑不动了。")
            return
        if p.money < MEAL_PRICE:
            yield self._reply(event, f"💰 饭钱 {MEAL_PRICE} 元都不够…先跑一单。")
            return
        if p.stamina >= STAMINA_MAX - 1:
            yield self._reply(event, "你现在体力满的，别浪费钱。")
            return
        p.money -= MEAL_PRICE
        p.today_meals += 1
        p.stamina = min(float(STAMINA_MAX), p.stamina + MEAL_STAMINA)
        await self.store.save_player(umo, p)
        yield self._reply(event, 
            f"🍚 蹲在路边扒了口饭（−{MEAL_PRICE} 元），⚡ 体力回到 {int(p.stamina)}/{STAMINA_MAX}。"
            f"今天还能吃 {MEAL_PER_DAY - p.today_meals} 次。")

    async def _repair(self, umo: str, event: AstrMessageEvent):
        p = await self._player(event)
        dur, top = p.durability, int(game.DURABILITY_MAX)
        # 什么车况都能修：小毛病就几块钱，烂得厉害才贵；满格又没爆胎才劝退
        if dur >= game.DURABILITY_MAX - 0.05 and not p.sick:
            yield self._reply(event, f"🔧 车况 {dur:.0f}/{top}，好好的，不用花这钱。")
            return
        price = game.repair_price(p)
        if p.money < price:
            yield self._reply(event, f"💰 修车要 {price} 元（车况 {dur:.0f}/{top}），"
                                     f"你只有 {int(p.money)} 元。先跑两单？")
            return
        p.money -= price
        left = p.sick
        p.durability = float(game.DURABILITY_MAX)
        p.sick = 0
        await self.store.save_player(umo, p)
        tail = f"，刚才剩下的 {left} 单也不用带病跑了" if left else ""
        yield self._reply(event, f"🔧 修好了（−{price} 元）：车况 {dur:.0f} → {top}/{top}{tail}。")

    async def _checkin(self, umo: str, event: AstrMessageEvent):
        p = await self._player(event)
        before = p.stamina
        res = game.checkin(p, time.time())
        if not res["ok"]:
            yield self._reply(event, f"📅 今天已经签过了（连签 {res['streak']} 天），"
                                     f"明天再来还接着算。")
            return
        await self.store.save_player(umo, p)
        streak = res["streak"]
        hot = "　🔥 连签还在涨" if streak >= 3 else ""
        # 体力本来就满的时候不该写「+15」，写实际加了多少
        gain = int(round(p.stamina - before))
        stam = f"体力 +{gain}" if gain > 0 else "体力本来就满"
        yield self._reply(event, f"📅 签到成功 ｜ 连签 {streak} 天 ｜ +{res['money']} 元 ｜ "
                                 f"{stam}{hot}\n"
                                 f"💰 余额 {int(p.money)} 元 ｜ 体力 "
                                 f"{int(p.stamina)}/{game.STAMINA_MAX}")

    async def _shortcut(self, umo: str, event: AstrMessageEvent):
        p = await self._player(event)
        if p.level < 1:
            yield self._reply(event, "抄近道是白银骑手才有的本事，先跑出 900 元周收入。")
            return
        if p.shortcut_day == self._day():
            yield self._reply(event, "今天已经抄过一次近道了，明天再来。")
            return
        p.shortcut_day = self._day()
        p.shortcut_armed = True
        await self.store.save_player(umo, p)
        yield self._reply(event, "🗺 把这片小区的后门都摸熟了，下一单不容易超时。")

    # ------------------------------------------------------------ 押注 / 打赏

    async def _find_player(self, umo: str, word: str) -> Player | None:
        """按名字找人：先精确，再前缀，再包含（群昵称常带前缀后缀）。"""
        word = (word or "").strip()
        if not word:
            return None
        ps = await self.store.players(umo)
        for p in ps:
            if p.name == word:
                return p
        for p in ps:
            if p.name.startswith(word):
                return p
        for p in ps:
            if word in p.name:
                return p
        return None

    async def _bet(self, umo: str, arg: str, event: AstrMessageEvent):
        p = await self._player(event)
        day = self._day()
        raw = (arg or "").strip()
        if not raw:
            rows = await self.store.open_bets(umo, day)
            yield self._reply(event, render.bets_card(rows, day))
            return
        m = re.match(r"^\s*(\S+)\s+(\d+)\s*$", raw)
        if not m:
            yield self._reply(event, "押注这样写：#押 小王 100（名字 + 金额）")
            return
        name, stake = m.group(1), int(m.group(2))
        if stake < 10:
            yield self._reply(event, "押多少都行，但至少 10 元。")
            return
        target = await self._find_player(umo, name)
        if target is None:
            yield self._reply(event, f"群里没找到「{name}」这个人，先让他发一次 #我的。")
            return
        if target.uid == p.uid:
            yield self._reply(event, "押自己没意思——你的单得自己跑。")
            return
        if p.money < stake:
            yield self._reply(event, f"💰 你手上只有 {int(p.money)} 元，押不起 {stake} 元。")
            return
        if not await self.store.place_bet(umo, day, p.uid, p.name,
                                          target.uid, target.name, stake):
            yield self._reply(event, "🎟 今天已经押过了，一人一天一注，等晚上开奖。")
            return
        p.money = float(round(p.money - stake))
        await self.store.save_player(umo, p)
        rows = await self.store.open_bets(umo, day)
        yield self._reply(event, 
            f"🎟 {p.name} 押 {target.name} 进今日前三，{stake} 元已进池。\n"
            f"晚上九点开奖：押中平台赔你一倍，{target.name} 进前三还有彩头。\n\n"
            + render.bets_card(rows, day))

    async def _tip(self, umo: str, arg: str, event: AstrMessageEvent):
        p = await self._player(event)
        raw = (arg or "").strip()
        m = re.match(r"^\s*(\S+)\s+(\d+)\s*$", raw)
        if not m:
            yield self._reply(event, "打赏这样写：#打赏 小王 50（名字 + 金额）")
            return
        name, amount = m.group(1), int(m.group(2))
        if amount < 1:
            yield self._reply(event, "打赏金额得是正数。")
            return
        target = await self._find_player(umo, name)
        if target is None:
            yield self._reply(event, f"群里没找到「{name}」这个人，先让他发一次 #我的。")
            return
        if target.uid == p.uid:
            yield self._reply(event, "自己给自己打赏，平台不收这种手续费。")
            return
        if p.money < amount:
            yield self._reply(event, f"💰 你手上只有 {int(p.money)} 元，打赏不起 {amount} 元。")
            return
        p.money = float(round(p.money - amount))
        target.money = float(round(target.money + amount))
        await self.store.save_player(umo, p)
        await self.store.save_player(umo, target)
        yield self._reply(event, 
            f"💝 {p.name} 给 {target.name} 打赏了 {amount} 元（不进今日榜，纯心意）。\n"
            f"{target.name} 现在有 {int(target.money)} 元。")

    async def _settle_bets(self, umo: str, day: str) -> list[str]:
        """开奖：押「进今日前三」的人赢一倍（平台出钱），被押的人拿彩头。

        押注时本金已经扣过了，赢了退两倍；被押的人没跑单/存档没了就原样退钱。
        """
        rows = await self.store.take_bets(umo, day)
        if not rows:
            return []
        board = await self.store.leaderboard(umo, 100, day)
        # 只有今天真的出了车的人才有名次。0 单也排上去的话，
        # 群里就两个人的时候押谁都是「进前三」，那注等于白送钱。
        rank = {p.uid: i for i, p in enumerate(board, start=1) if p.today_orders > 0}
        everyone = {p.uid: p for p in await self.store.players(umo)}
        lines: list[str] = []
        for r in rows:
            stake = int(r.get("stake") or 0)
            bettor = everyone.get(str(r.get("bettor")))
            target = everyone.get(str(r.get("target")))
            bname = (bettor.name if bettor else r.get("bettor_name")) or "某人"
            tname = (target.name if target else r.get("target_name")) or "某人"
            if bettor is None or target is None or str(r.get("target")) not in rank:
                # 没开成：本金原样退回，别让人白扣一笔
                if bettor is not None:
                    bettor.money = float(round(bettor.money + stake))
                    await self.store.save_player(umo, bettor)
                if target is not None and target.today_orders <= 0:
                    why = "今天一单没跑"
                elif target is None:
                    why = "人找不到了"
                else:
                    why = "没赶上榜单"
                lines.append(f"🎟 {bname} 押 {tname}：{why}，这注没开成，{stake} 元退回。")
                continue
            if rank[str(r.get("target"))] <= 3:
                payout = max(1, stake // 2)
                bettor.money = float(round(bettor.money + stake * 2))
                target.money = float(round(target.money + payout))
                lines.append(f"🎟 {bname} 押中 {tname} 进前三，赢 {stake} 元；"
                             f"{tname} 拿彩头 {payout} 元。")
                await self.store.save_player(umo, bettor)
                await self.store.save_player(umo, target)
            else:
                lines.append(f"🎟 {bname} 押 {tname} 没进前三，{stake} 元归平台。")
                await self.store.save_player(umo, bettor)
        return lines

    # ------------------------------------------------------------ 定时任务

    async def _night(self) -> None:
        if not self.night_broadcast:
            return
        day = self._day()
        for umo in await self.store.groups_with_players():
            # 先占位再发：先查再发是 check-then-act，两次定时器/热重载撞一起会播两遍
            if not await self.store.claim_night(umo, day):
                continue
            sent = False
            try:
                rows = await self.store.leaderboard(umo, 10, day)
                if not rows:
                    await self.store.release_night(umo, day)
                    continue
                text = render.leaderboard_card(day, rows)
                # 押注开奖：晚上播榜的时候一起算，押的人当晚就知道结果
                bets = await self._settle_bets(umo, day)
                if bets:
                    text += "\n\n" + "\n".join(bets)
                tomorrow = roll_weather(f"{umo}:{day_key(time.time() + 86400)}")
                w = game.WEATHERS.get(tomorrow) or game.WEATHERS["sunny"]
                text += f"\n\n明天 {w['emoji']} {w['name']}天"
                if w["bonus"]:
                    text += f"，补贴 +{int(w['bonus'] * 100)}%"
                text += "。收车吧，明早接着跑。"
                await self.context.send_message(umo, MessageChain([Plain(text)]))
                sent = True
            except Exception:
                logger.exception(f"[waimai] 晚间播报失败：{umo}")
            finally:
                # 没发出去就把占位还回去，等下一个人/下一次还能播
                if not sent:
                    await self.store.release_night(umo, day)

    async def _weekly(self) -> None:
        for umo in await self.store.groups_with_players():
            try:
                promoted = await self.store.week_rollover(umo)
                if not promoted:
                    continue
                lines = ["🏅 周结算：段位重新评定了"]
                for name, old, new in promoted:
                    lines.append(f"  {name}：{old} → {new}")
                lines.append("新的一周，从青铜到单王，重新跑。")
                await self.context.send_message(umo, MessageChain([Plain("\n".join(lines))]))
            except Exception:
                logger.exception(f"[waimai] 周结算失败：{umo}")
