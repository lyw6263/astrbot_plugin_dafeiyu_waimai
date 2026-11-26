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
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.path = str(db_path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
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
                await self.db.commit()
                return p
        data, old_name = row
        p = Player.from_dict(json.loads(data))
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
            await self.db.execute(
                "UPDATE players SET name=?, data=?, updated=? WHERE umo=? AND uid=?",
                (p.name, json.dumps(p.to_dict(), ensure_ascii=False), time.time(),
                 umo, p.uid),
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
            try:
                out.append(Player.from_dict(json.loads(data)))
            except Exception:
                continue
        return out

    async def leaderboard(self, umo: str, limit: int = 10) -> list[Player]:
        ps = await self.players(umo)
        return sorted(ps, key=lambda x: -x.today_income)[:limit]

    async def week_rollover(self, umo: str) -> list[tuple[str, str, str]]:
        """周一结算：按周收入升段位，返回 [(名字, 旧段位, 新段位)]。"""
        from .game import LEVELS

        promoted: list[tuple[str, str, str]] = []
        for p in await self.players(umo):
            old = p.level
            new = 0
            for i, lv in enumerate(LEVELS):
                if p.week_income >= lv["weekly"]:
                    new = i
            if new > old:
                p.level = new
                promoted.append((p.name, LEVELS[old]["name"], LEVELS[new]["name"]))
            p.week_income = 0
            await self.save_player(umo, p)
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
        async with self._lock:
            cur = await self.db.execute(
                "SELECT DISTINCT umo FROM players WHERE updated > ?",
                (time.time() - 3 * 86400,),
            )
            rows = await cur.fetchall()
        return [r[0] for r in rows]

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
            {"slot": s, "order": json.loads(d), "claimed_by": cby, "claimed_name": cn}
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

    async def get_limited_order(self, umo: str, day: str, slot: int) -> Order | None:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT data FROM limited WHERE umo=? AND day=? AND slot=?",
                (umo, day, slot),
            )
            row = await cur.fetchone()
        return Order.from_dict(json.loads(row[0])) if row else None