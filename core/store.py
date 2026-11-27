"""aiosqlite 存档：玩家 / 群 / 每日限量单。

只有三张表，所有写入都走一把 asyncio.Lock —— 群机器人并发不高，
但「抢单」必须原子，所以宁可慢一点。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import aiosqlite

from .game import Player, Order

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    umo     TEXT NOT NULL,
    uid     TEXT NOT NULL,
    name    TEXT NOT NULL DEFAULT '',
    data    TEXT NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (umo, uid)
);
CREATE TABLE IF NOT EXISTS groups (
    umo    TEXT PRIMARY KEY,
    joined REAL NOT NULL,
    night  TEXT NOT NULL DEFAULT ''      -- 最近一次发过晚间播报的日期
);
CREATE TABLE IF NOT EXISTS limited (
    umo     TEXT NOT NULL,
    day     TEXT NOT NULL,
    slot    INTEGER NOT NULL,
    data    TEXT NOT NULL,
    claimed_by   TEXT,
    claimed_name TEXT,
    claimed_at   REAL,
    PRIMARY KEY (umo, day, slot)
);
CREATE TABLE IF NOT EXISTS meta (
    umo    TEXT NOT NULL,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL,
    PRIMARY KEY (umo, key)
);
"""


def _loads(data: str):
    """存档里的 JSON 坏了不该把整个插件带崩。"""
    try:
        return json.loads(data)
    except Exception:
        return None


def _is_group(umo: str) -> bool:
    """AstrBot 的 umo 形如 aiocqhttp:GroupMessage:123456；私聊是 FriendMessage。

    私聊的 umo 混进 groups 表，晚间播报就会去单聊里念整群人的收入榜。
    """
    u = str(umo or "")
    return ("GroupMessage" in u) or (":group" in u.lower())


class Store:
    def __init__(self, db_path: str | Path):
        self.path = str(db_path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        # 库坏了（断电、磁盘满）会让 execute 直接抛，插件在 initialize 就挂掉，
        # 群里表现成「怎么 @ 都没反应」。这里先自检，坏了就把旧库改名留档重建。
        try:
            cur = await self._db.execute("PRAGMA integrity_check")
            row = await cur.fetchone()
            ok = bool(row and row[0] == "ok")
        except Exception:
            ok = False
        if not ok:
            await self._db.close()
            self._db = None
            stamp = time.strftime("%Y%m%d-%H%M%S")
            broken = f"{self.path}.corrupt-{stamp}"
            try:
                Path(self.path).rename(broken)
            except OSError:
                pass
            self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store 还没 open()")
        return self._db

    # ------------------------------------------------------------ 玩家

    async def get_player(self, umo: str, uid: str, name: str = "") -> Player:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT data, name FROM players WHERE umo=? AND uid=?", (umo, str(uid))
            )
            row = await cur.fetchone()
            if row is None:
                p = Player(uid=str(uid), name=name or "骑手", created=time.time())
                await self.db.execute(
                    "INSERT INTO players(umo,uid,name,data,updated) VALUES(?,?,?,?,?)",
                    (umo, str(uid), p.name, json.dumps(p.to_dict(), ensure_ascii=False),
                     time.time()),
                )
                # 第一次有人上车就把这个群登记下来，否则周一结算、晚间播报
                # 只能靠「三天内有存档」这个窗口去猜哪些群还活着。
                # 私聊的 umo 不是群，登记进去会让播报去给单聊发别人的榜单。
                if _is_group(umo):
                    await self.db.execute(
                        "INSERT OR IGNORE INTO groups(umo, joined) VALUES(?,?)", (umo, time.time())
                    )
                await self.db.commit()
                return p
        data, old_name = row
        raw = _loads(data)
        if raw is None:
            p = Player(uid=str(uid), name=name or "骑手", created=time.time())
        else:
            try:
                p = Player.from_dict(raw)
            except Exception:
                p = Player(uid=str(uid), name=name or "骑手", created=time.time())
        if not p.created:
            p.created = time.time()
        if name and name != old_name:
            p.name = name
            async with self._lock:
                await self.db.execute(
                    "UPDATE players SET name=? WHERE umo=? AND uid=?", (name, umo, str(uid))
                )
                await self.db.commit()
        return p

    async def save_player(self, umo: str, p: Player) -> None:
        async with self._lock:
            data = json.dumps(p.to_dict(), ensure_ascii=False)
            cur = await self.db.execute(
                "UPDATE players SET name=?, data=?, updated=? WHERE umo=? AND uid=?",
                (p.name, data, time.time(), umo, p.uid),
            )
            if cur.rowcount == 0:
                # 存档行被人工删过（或并发下刚被清库），UPDATE 空转会把这一局整个吞掉
                await self.db.execute(
                    "INSERT OR REPLACE INTO players(umo,uid,name,data,updated) "
                    "VALUES(?,?,?,?,?)",
                    (umo, p.uid, p.name, data, time.time()),
                )
            await self.db.commit()

    async def players(self, umo: str) -> list[Player]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT data FROM players WHERE umo=? ORDER BY updated DESC", (umo,)
            )
            rows = await cur.fetchall()
        out = []
        for (data,) in rows:
            raw = _loads(data)
            if raw is None:
                continue
            try:
                out.append(Player.from_dict(raw))
            except Exception:
                continue
        return out

    async def leaderboard(self, umo: str, limit: int = 10,
                          day: str = "") -> list[Player]:
        """今日收入榜。

        必须按 p.day 过滤：存档里的 today_income 是「那一天的」，昨天跑完没再上线的
        人若不过滤，会带着昨天的钱挂在今天的榜上（标题却写着今天）。
        """
        day = day or time.strftime("%Y-%m-%d")
        ps = [p for p in await self.players(umo) if p.day == day]
        return sorted(ps, key=lambda x: -x.today_income)[:limit]

    async def week_rollover(self, umo: str, week: str = "") -> list[tuple[str, str, str]]:
        """周一结算：按周收入升段位，返回 [(名字, 旧段位, 新段位)]。

        带「本周已结算」标记 + 单次提交：手册版是「快照 → 逐人 save」，中途被打断
        或周一播报重入会把同一周结算两次（第二次全是 0 收入，等于把人白降一级）。
        """
        from .game import LEVELS

        week = week or time.strftime("%G-W%V")
        async with self._lock:
            cur = await self.db.execute(
                "SELECT value FROM meta WHERE umo=? AND key='week_rollover'", (umo,)
            )
            row = await cur.fetchone()
            if row and row[0] == week:
                return []
            cur = await self.db.execute("SELECT uid, data FROM players WHERE umo=?", (umo,))
            rows = await cur.fetchall()
            promoted: list[tuple[str, str, str]] = []
            now = time.time()
            for uid, data in rows:
                raw = _loads(data)
                if raw is None:
                    continue
                try:
                    p = Player.from_dict(raw)
                except Exception:
                    continue
                old = p.level
                new = 0
                for i, lv in enumerate(LEVELS):
                    if p.week_income >= lv["weekly"]:
                        new = i
                if new > old:
                    p.level = new
                    promoted.append((p.name, LEVELS[old]["name"], LEVELS[new]["name"]))
                p.week_income = 0
                await self.db.execute(
                    "UPDATE players SET data=?, updated=? WHERE umo=? AND uid=?",
                    (json.dumps(p.to_dict(), ensure_ascii=False), now, umo, uid),
                )
            await self.db.execute(
                "INSERT INTO meta(umo,key,value) VALUES(?,?,?) "
                "ON CONFLICT(umo,key) DO UPDATE SET value=excluded.value",
                (umo, "week_rollover", week),
            )
            await self.db.commit()
        return promoted

    # ------------------------------------------------------------ 群

    async def join_group(self, umo: str) -> None:
        async with self._lock:
            await self.db.execute(
                "INSERT OR IGNORE INTO groups(umo, joined) VALUES(?,?)", (umo, time.time())
            )
            await self.db.commit()

    async def groups(self) -> list[str]:
        async with self._lock:
            cur = await self.db.execute("SELECT umo FROM groups")
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def groups_with_players(self) -> list[str]:
        """需要结算/播报的群：登记过的群 ∪ 最近一个月还有存档的群。

        原来只看「三天内有存档」，一个群要是周中没人跑单，周一结算时就被漏掉，
        周收入不清零、段位不结算，等于这一周白干。
        """
        async with self._lock:
            cur = await self.db.execute(
                "SELECT umo FROM groups UNION "
                "SELECT DISTINCT umo FROM players WHERE updated > ?",
                (time.time() - 30 * 86400,),
            )
            rows = await cur.fetchall()
        return [r[0] for r in rows if _is_group(r[0])]

    async def night_done(self, umo: str, day: str) -> bool:
        async with self._lock:
            cur = await self.db.execute("SELECT night FROM groups WHERE umo=?", (umo,))
            row = await cur.fetchone()
            return bool(row and row[0] == day)

    async def mark_night(self, umo: str, day: str) -> None:
        async with self._lock:
            await self.db.execute(
                "INSERT INTO groups(umo, joined, night) VALUES(?,?,?) "
                "ON CONFLICT(umo) DO UPDATE SET night=excluded.night",
                (umo, time.time(), day),
            )
            await self.db.commit()

    async def claim_night(self, umo: str, day: str) -> bool:
        """原子占位：只有把 night 从「不是今天」改成今天的那一次返回 True。

        「先 send 再 mark_night」在热重载/两次触发叠在一起时会重复播一遍；
        反过来先占位又不保险（发送失败就再也不播了）。所以让调用方在发送前
        抢占位，失败再 release_night 放回去。
        """
        async with self._lock:
            await self.db.execute(
                "INSERT OR IGNORE INTO groups(umo, joined) VALUES(?,?)", (umo, time.time())
            )
            cur = await self.db.execute(
                "UPDATE groups SET night=? WHERE umo=? AND night<>?", (day, umo, day)
            )
            await self.db.commit()
            return cur.rowcount == 1

    async def release_night(self, umo: str, day: str) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE groups SET night='' WHERE umo=? AND night=?", (umo, day)
            )
            await self.db.commit()

    # ------------------------------------------------------------ 限量单

    async def ensure_limited(self, umo: str, day: str, orders: list[Order]) -> list[dict]:
        """给 (群, 日期) 建好限量单，返回全部槽位。"""
        async with self._lock:
            for i, o in enumerate(orders, start=1):
                await self.db.execute(
                    "INSERT OR IGNORE INTO limited(umo,day,slot,data) VALUES(?,?,?,?)",
                    (umo, day, i, json.dumps(o.to_dict(), ensure_ascii=False)),
                )
            await self.db.commit()
            cur = await self.db.execute(
                "SELECT slot, data, claimed_by, claimed_name FROM limited "
                "WHERE umo=? AND day=? ORDER BY slot", (umo, day),
            )
            rows = await cur.fetchall()
        return [
            {"slot": s, "order": _loads(d) or {}, "claimed_by": cby, "claimed_name": cn}
            for s, d, cby, cn in rows
        ]

    async def claim_limited(self, umo: str, day: str, slot: int, uid: str,
                            name: str) -> bool:
        """原子抢单：只有把 claimed_by 从 NULL 改成自己的那一个成功。"""
        async with self._lock:
            cur = await self.db.execute(
                "UPDATE limited SET claimed_by=?, claimed_name=?, claimed_at=? "
                "WHERE umo=? AND day=? AND slot=? AND claimed_by IS NULL",
                (str(uid), name, time.time(), umo, day, slot),
            )
            await self.db.commit()
            return cur.rowcount == 1

    async def release_limited(self, umo: str, day: str, slot: int) -> None:
        """抢到的单结算炸了就把名额还回去，否则这单永远显示「已被抢走」。"""
        async with self._lock:
            await self.db.execute(
                "UPDATE limited SET claimed_by=NULL, claimed_name=NULL, claimed_at=NULL "
                "WHERE umo=? AND day=? AND slot=?", (umo, day, slot),
            )
            await self.db.commit()

    async def get_limited_order(self, umo: str, day: str, slot: int) -> Order | None:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT data FROM limited WHERE umo=? AND day=? AND slot=?",
                (umo, day, slot),
            )
            row = await cur.fetchone()
        raw = _loads(row[0]) if row else None
        if raw is None:
            return None
        try:
            return Order.from_dict(raw)
        except Exception:
            return None