"""海戰棋 Server（2～4 人混戰＋觀戰）

執行：python server.py --host 0.0.0.0 --port 9100 --turn 30 --grace 30
（也可以不開終端機：在 client_gui.py 按「我當主機」，Server 會在 GUI 裡面啟動）

  TCP 9100  遊戲連線（可靠、有順序）
  UDP 9101  區網自動搜尋：Client 廣播「有 Server 嗎？」，Server 回覆自己的 Port

Server 是唯一的裁判（權威伺服器）：
所有人的船艦位置只存在 Server。送給每個人的 STATE 會依身分過濾——
玩家只看得到自己的船；觀戰者誰的船都看不到，只看得到公開的開火結果。
"""
import argparse
import json
import logging
import random
import secrets
import socket
import sys
import threading
import time

from protocol import (BOT_PREFIX, COLORS, DEAD_AFTER, DEFAULT_PORT, DISCOVER_MAGIC, DISCOVERY_PORT, EMOTES, ERRORS, FLEET,
                      HERE_MAGIC, MAX_PLAYERS, MIN_PLAYERS, SIZE, LineReader, coord, random_name, send_msg,
                      validate_fleet)

log = logging.getLogger("battleship")


class Player:
    def __init__(self, name, conn):
        self.name = name
        self.conn = conn
        self.token = secrets.token_hex(8)       # 斷線重連用的憑證
        self.room = None
        self.role = None                        # "player" / "spectator" / None（大廳）
        self.color = None                       # 板子顏色（房間內不重複）
        self.offline_until = 0.0
        self.reset_board()

    def reset_board(self):
        self.ships = {}          # id -> {"cells": [(x,y)], "hits": set()}
        self.incoming = {}       # 被打的格子 (x,y) -> miss/hit/sunk（公開資訊）
        self.ready = False
        self.alive = True
        self.left = False        # 對局中離開／斷線逾時（座位保留顯示，但不再參與）
        self.out_reason = ""

    @property
    def online(self):
        return self.conn is not None and self.conn.alive

    def ships_left(self):
        if not self.ships:
            return len(FLEET)
        return sum(1 for s in self.ships.values() if len(s["hits"]) < len(s["cells"]))

    def sunk_list(self):
        return [{"id": sid, "name": FLEET[sid][0], "cells": s["cells"]}
                for sid, s in self.ships.items() if len(s["hits"]) == len(s["cells"])]

    def send(self, msg):
        if self.online:
            self.conn.send(msg)


class Room:
    def __init__(self, code, capacity, owner):
        self.code = code
        self.capacity = capacity
        self.owner = owner
        self.players = []
        self.spectators = []
        self.phase = "WAITING"       # WAITING -> PLACING -> BATTLE -> OVER
        self.turn = 0
        self.turn_no = 0
        self.deadline = 0.0
        self.paused = False
        self.winner = None
        self.reason = ""
        self.rematch = set()
        self.timeouts = {}
        self.cycle = []           # 開局抽籤的攻擊順序（暱稱），每人只能打下一位

    def active(self):
        """還在場上（沒離開）的玩家"""
        return [p for p in self.players if not p.left]

    def alive(self):
        return [p for p in self.players if p.alive and not p.left]

    def everyone(self):
        return self.players + self.spectators


class Conn:
    def __init__(self, sock, addr):
        self.sock, self.addr = sock, addr
        self.player = None
        self.alive = True
        self.lock = threading.Lock()
        self.last_seen = time.time()

    def kill(self):
        """強制關閉（心跳逾時或被同一人新連線取代）：shutdown 會讓 recv() 立刻返回"""
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @property
    def tag(self):
        return f"{self.addr[0]}:{self.addr[1]} ({self.player.name if self.player else '未登入'})"

    def send(self, msg):
        if not self.alive:
            return
        try:
            with self.lock:
                send_msg(self.sock, msg)
        except OSError:
            self.alive = False


class Server:
    def __init__(self, turn_secs=30, grace=30, name=None):
        self.turn_secs = turn_secs
        self.grace = grace
        self.name = name or socket.gethostname()
        self.port = None
        self.lock = threading.RLock()
        self.players = {}        # 暱稱 -> Player
        self.rooms = {}          # 房號 -> Room
        self.match_queue = []
        self.conns = set()
        self.last_emote = {}

    # ------------------------------------------------------------ 給每個人的畫面資料
    def room_summary(self):
        return [{"code": r.code, "owner": r.owner, "capacity": r.capacity, "phase": r.phase,
                 "players": [p.name for p in r.active()], "spectators": len(r.spectators)}
                for r in self.rooms.values()]

    def lobby_state(self, p):
        return {"event": "LOBBY", "you": p.name, "matching": p in self.match_queue,
                "online": sum(1 for q in self.players.values() if q.online),
                "rooms": self.room_summary()}

    def state_for(self, v):
        r = v.room
        if r is None:
            return self.lobby_state(v)
        over = r.phase == "OVER"
        boards = []
        for p in r.players:
            b = {"name": p.name, "color": p.color, "online": p.online, "ready": p.ready, "alive": p.alive,
                 "left": p.left, "out_reason": p.out_reason, "ships_left": p.ships_left(),
                 "incoming": [[x, y, val] for (x, y), val in p.incoming.items()],
                 "sunk": p.sunk_list()}
            if over and p is not v:      # 結束才公開大家的船
                b["reveal"] = [{"id": sid, "name": FLEET[sid][0], "cells": s["cells"]} for sid, s in p.ships.items()]
            boards.append(b)
        st = {"event": "STATE", "code": r.code, "phase": r.phase, "you": v.name, "role": v.role,
              "owner": r.owner, "capacity": r.capacity, "boards": boards,
              "spectators": [s.name for s in r.spectators], "rematch": [q.name for q in r.rematch]}
        if v.role == "player":
            st["me"] = {"ready": v.ready, "alive": v.alive,
                        "ships": [{"id": sid, "name": FLEET[sid][0], "cells": s["cells"],
                                   "hits": sorted(s["hits"]), "sunk": len(s["hits"]) == len(s["cells"])}
                                  for sid, s in v.ships.items()]}
        if r.phase == "BATTLE":
            st["turn"] = r.players[r.turn].name
            st["turn_no"] = r.turn_no
            st["paused"] = r.paused
            st["turn_remain"] = None if r.paused else round(max(0.0, r.deadline - time.time()), 1)
            tt = self.assigned_target(r, r.players[r.turn])
            st["turn_target"] = tt.name if tt else None
        if r.phase in ("BATTLE", "OVER"):
            st["cycle"] = r.cycle
        if r.phase == "BATTLE" and v.role == "player" and v.alive and not v.left:
            mt = self.assigned_target(r, v)
            st["my_target"] = mt.name if mt else None      # 我這局固定要打的人
        if over:
            st["winner"], st["reason"] = r.winner, r.reason
        return st

    def push_room(self, r):
        for v in r.everyone():
            v.send(self.state_for(v))

    def push_lobby(self):
        for p in self.players.values():
            if p.room is None:
                p.send(self.lobby_state(p))

    def room_event(self, r, msg):
        msg.setdefault("ts", time.strftime("%H:%M:%S"))
        for v in r.everyone():
            v.send(msg)

    # ------------------------------------------------------------ 房間管理
    def new_room(self, capacity, owner):
        while True:
            code = f"{random.randint(0, 9999):04d}"
            if code not in self.rooms:
                r = Room(code, capacity, owner)
                self.rooms[code] = r
                return r

    def add_player(self, p, r):
        used = {q.color for q in r.active()}
        p.color = next((c for c in COLORS if c not in used), COLORS[0])     # 自動給一個還沒人用的顏色
        r.players.append(p)
        p.room, p.role = r, "player"
        p.reset_board()
        log.info("[ROOM] %s 進入房間 %s（%d/%d）", p.name, r.code, len(r.players), r.capacity)
        if len(r.players) >= r.capacity:
            self.start_placing(r)
        self.push_room(r)
        self.push_lobby()

    def add_spectator(self, p, r):
        r.spectators.append(p)
        p.room, p.role = r, "spectator"
        log.info("[ROOM] %s 進入房間 %s 觀戰", p.name, r.code)
        self.room_event(r, {"event": "SPECTATOR", "msg": f"{p.name} 進來觀戰了"})
        self.push_room(r)
        self.push_lobby()

    def start_placing(self, r):
        r.players = r.active()
        for q in r.players:
            q.reset_board()
        r.phase, r.winner, r.reason = "PLACING", None, ""
        r.rematch.clear()
        log.info("[ROOM] %s 開始佈陣（%d 人）", r.code, len(r.players))
        self.room_event(r, {"event": "MATCHED", "code": r.code, "players": [q.name for q in r.players]})

    def start_battle(self, r):
        r.phase = "BATTLE"
        # 開局抽籤：打亂順序排成一圈，每人只能打下一位（A→B→C→D→A），回合也照這個順序輪
        order = random.sample(r.players, len(r.players))
        r.cycle = [q.name for q in order]
        r.turn = r.players.index(order[0])
        r.turn_no += 1
        r.deadline = time.time() + self.turn_secs
        r.timeouts = {q.name: 0 for q in r.players}
        first = r.players[r.turn].name
        ring = " → ".join(r.cycle + [r.cycle[0]])
        log.info("[GAME] 房間 %s 開戰（%d 人），抽籤順序 %s", r.code, len(r.players), ring)
        self.room_event(r, {"event": "DRAW", "cycle": r.cycle, "msg": f"抽籤結果：{ring}"})
        self.room_event(r, {"event": "BATTLE_START", "first": first,
                            "msg": f"全員佈陣完成，開戰！由 {first} 先攻"})

    def by_name(self, r, name):
        return next((q for q in r.players if q.name == name), None)

    def next_in_cycle(self, r, name):
        """抽籤圈中，name 之後第一位還活著的玩家（中間有人淘汰就跳過）"""
        n = len(r.cycle)
        i = r.cycle.index(name)
        for k in range(1, n):
            q = self.by_name(r, r.cycle[(i + k) % n])
            if q and q.alive and not q.left:
                return q
        return None

    def assigned_target(self, r, p):
        """每人只能打抽籤圈裡的下一位；例如 C 淘汰後，B 改打 D"""
        if p.name not in r.cycle:
            return None
        return self.next_in_cycle(r, p.name)

    def next_turn(self, r):
        nxt = self.next_in_cycle(r, r.players[r.turn].name)
        if nxt:
            r.turn = r.players.index(nxt)
        r.turn_no += 1
        r.deadline = time.time() + self.turn_secs
        tt = self.assigned_target(r, r.players[r.turn])
        log.info("[TURN] 輪到 %s，這回合攻擊 %s", r.players[r.turn].name, tt.name if tt else "-")

    def eliminate(self, r, p, reason):
        if not p.alive:
            return
        p.alive = False
        p.out_reason = reason
        log.info("[GAME] %s 淘汰：%s", p.name, reason)
        self.room_event(r, {"event": "ELIMINATED", "player": p.name, "msg": f"{p.name} 淘汰（{reason}）"})
        alive = r.alive()
        if len(alive) <= 1:
            w = alive[0] if alive else None
            self.finish(r, w, f"{w.name} 是最後存活的艦隊" if w else "無人存活")
        elif r.players[r.turn] is p:
            self.next_turn(r)

    def finish(self, r, winner, reason):
        r.phase = "OVER"
        r.winner = winner.name if winner else None
        r.reason = reason
        r.paused = False
        r.rematch.clear()
        log.info("[GAME] 房間 %s 結束：%s（%s）", r.code, r.winner, reason)
        self.room_event(r, {"event": "GAME_OVER", "winner": r.winner, "reason": reason})

    def update_pause(self, r):
        if r.phase != "BATTLE":
            r.paused = False
            return
        was = r.paused
        r.paused = any(not p.online for p in r.alive())
        if was and not r.paused:
            r.deadline = time.time() + self.turn_secs

    def remove_from_room(self, p, reason):
        """離開房間：依階段處理"""
        r = p.room
        if r is None:
            if p in self.match_queue:
                self.match_queue.remove(p)
            return
        if p.role == "spectator":
            r.spectators.remove(p)
        elif r.phase == "BATTLE":
            # 對局中離開：座位換成一份「凍結的棋盤」留給其他人看，本人則回到大廳（可以去別的房間）
            idx = r.players.index(p)
            ghost = Player(p.name, None)
            ghost.ships, ghost.incoming, ghost.ready, ghost.color = p.ships, p.incoming, True, p.color
            ghost.left = True
            r.players[idx] = ghost
            if p.alive:
                self.eliminate(r, ghost, reason)
            else:
                ghost.alive, ghost.out_reason = False, p.out_reason
            r.rematch.discard(p)
        else:
            r.players.remove(p)
            if r.phase == "PLACING":
                if len(r.active()) < MIN_PLAYERS:
                    r.phase = "WAITING"
                    for q in r.players:
                        q.reset_board()
                elif all(q.ready for q in r.players):
                    self.start_battle(r)
            r.rematch.discard(p)
            present = [q for q in r.active() if q.online]
            if r.phase == "OVER" and r.rematch and len(present) >= MIN_PLAYERS and all(q in r.rematch for q in present):
                r.players = present
                self.start_placing(r)
        log.info("[ROOM] %s 離開房間 %s（%s）", p.name, r.code, reason)
        p.room, p.role = None, None
        p.reset_board()
        if r.owner == p.name and r.active():
            r.owner = r.active()[0].name
        if not r.active() and not r.spectators:
            self.rooms.pop(r.code, None)
            log.info("[ROOM] 房間 %s 已關閉", r.code)
        else:
            self.room_event(r, {"event": "LEFT", "msg": f"{p.name} {reason}"})
            self.update_pause(r)
            self.push_room(r)
        self.push_lobby()

    # ------------------------------------------------------------ 計時
    def ticker(self):
        while True:
            time.sleep(0.25)
            with self.lock:
                now = time.time()
                for c in list(self.conns):
                    if c.alive and now - c.last_seen > DEAD_AFTER:
                        log.info("[BEAT] %s 超過 %d 秒沒有任何資料（心跳逾時），視為斷線", c.tag, DEAD_AFTER)
                        c.kill()
                for r in list(self.rooms.values()):
                    for p in list(r.players):
                        if not p.online and p.offline_until and now >= p.offline_until and not p.left:
                            p.offline_until = 0
                            log.info("[DROP] %s 未在 %d 秒內重連", p.name, self.grace)
                            self.players.pop(p.name, None)
                            self.remove_from_room(p, "斷線未回來")
                    if r.code not in self.rooms:
                        continue
                    if r.phase == "BATTLE" and not r.paused and now >= r.deadline:
                        p = r.players[r.turn]
                        r.timeouts[p.name] = r.timeouts.get(p.name, 0) + 1
                        log.info("[TIME] %s 回合逾時（第 %d 次）", p.name, r.timeouts[p.name])
                        if r.timeouts[p.name] >= 3:
                            self.eliminate(r, p, "連續 3 回合未出手")
                        else:
                            self.room_event(r, {"event": "TIMEOUT", "player": p.name,
                                                "msg": f"{p.name} 超過 {self.turn_secs} 秒未開火，回合跳過"})
                            self.next_turn(r)
                        self.push_room(r)

    # ------------------------------------------------------------ 指令分派
    def err(self, code, detail=None):
        return {"ok": False, "code": code, "msg": detail or ERRORS.get(code, code)}

    def handle(self, conn, line):
        try:
            req = json.loads(line)
            if not isinstance(req, dict) or "cmd" not in req:
                raise ValueError
        except ValueError:
            log.warning("[BAD ] %s 無法解析：%r", conn.tag, line[:80])
            return self.err("E_BAD_REQUEST")
        cmd = str(req["cmd"]).upper()
        if cmd == "PING":
            return {"ok": True, "cmd": "PING"}
        log.info("[RECV] %s %s", conn.tag, line[:160])
        with self.lock:
            if cmd == "LOGIN":
                resp = self.cmd_login(conn, req)
            elif conn.player is None:
                resp = self.err("E_AUTH")
            else:
                fn = getattr(self, "cmd_" + cmd.lower(), None) if cmd.replace("_", "").isalpha() else None
                resp = fn(conn.player, req) if fn else self.err("E_UNKNOWN_CMD")
        if resp is not None:
            resp["cmd"] = cmd
        return resp

    # ------------------------------------------------------------ 指令
    def cmd_login(self, conn, req):
        name = str(req.get("name", "")).strip()
        if not (1 <= len(name) <= 12):
            return self.err("E_NAME")
        if conn.player:
            return self.err("E_PHASE", "已經登入了")
        p = self.players.get(name)
        if p:
            if req.get("token") != p.token:
                if p.offline_until:
                    left = int(p.offline_until - time.time())
                    return self.err("E_NAME_TAKEN", f"「{name}」斷線保留中（{left} 秒後釋出），請換一個暱稱或稍後再試")
                return self.err("E_NAME_TAKEN", f"「{name}」已經有人在線上使用，請換一個暱稱")
            if p.online:
                # 同一個人（token 相符）從新連線回來：舊連線可能已經失效只是還沒偵測到，直接取代
                log.info("[BACK] %s 用新連線取代舊連線 %s", name, p.conn.tag)
                p.conn.kill()
            p.conn, p.offline_until = conn, 0       # 斷線重連：接回原本的玩家
            conn.player = p
            log.info("[BACK] %s 重新連線%s", name, f"，回到房間 {p.room.code}" if p.room else "")
            conn.send({"ok": True, "cmd": "LOGIN", "name": name, "token": p.token, "resumed": True,
                       "turn_secs": self.turn_secs, "server": self.name})
            if p.room:
                self.room_event(p.room, {"event": "BACK", "msg": f"{name} 已重新連線"})
                self.update_pause(p.room)
                self.push_room(p.room)
            else:
                p.send(self.lobby_state(p))
            return None
        p = Player(name, conn)
        conn.player = p
        self.players[name] = p
        log.info("[AUTH] %s 登入為 %s", conn.tag, name)
        conn.send({"ok": True, "cmd": "LOGIN", "name": name, "token": p.token, "resumed": False,
                   "turn_secs": self.turn_secs, "server": self.name})
        self.push_lobby()
        return None

    def cmd_create(self, p, req):
        if p.room or p in self.match_queue:
            return self.err("E_IN_ROOM")
        try:
            cap = int(req.get("capacity", MAX_PLAYERS))      # 預設 4 人上限，2 人以上房主即可開始
        except (TypeError, ValueError):
            cap = 0
        if not MIN_PLAYERS <= cap <= MAX_PLAYERS:
            return self.err("E_CAPACITY")
        r = self.new_room(cap, p.name)
        self.add_player(p, r)
        return {"ok": True, "code": r.code, "msg": f"已建立 {cap} 人房間 {r.code}"}

    def cmd_join(self, p, req):
        if p.room or p in self.match_queue:
            return self.err("E_IN_ROOM")
        code = str(req.get("code", "")).strip()
        r = self.rooms.get(code)
        if not r:
            return self.err("E_NO_ROOM", f"找不到房間 {code}")
        if req.get("spectate"):
            self.add_spectator(p, r)
            return {"ok": True, "code": code, "msg": f"開始觀戰房間 {code}"}
        if r.phase != "WAITING":
            return self.err("E_PHASE", "對局已開始，可以改用「觀戰」")
        if len(r.players) >= r.capacity:
            return self.err("E_ROOM_FULL")
        self.add_player(p, r)
        return {"ok": True, "code": code, "msg": f"已加入房間 {code}"}

    def cmd_match(self, p, req):
        if p.room or p in self.match_queue:
            return self.err("E_IN_ROOM")
        waiting = [q for q in self.match_queue if q.online]
        if waiting:
            o = waiting[0]
            self.match_queue.remove(o)
            r = self.new_room(2, o.name)
            self.add_player(o, r)
            self.add_player(p, r)
            return {"ok": True, "code": r.code, "msg": f"配對成功！對手：{o.name}"}
        self.match_queue.append(p)
        log.info("[MATCH] %s 進入配對佇列", p.name)
        self.push_lobby()
        return {"ok": True, "msg": "配對中，等待其他玩家…"}

    def cmd_start(self, p, req):
        """房主提前開始（3、4 人房沒坐滿也能開）"""
        r = p.room
        if not r or r.phase != "WAITING":
            return self.err("E_PHASE", "只有等待中的房間可以開始")
        if r.owner != p.name:
            return self.err("E_OWNER")
        if len(r.players) < MIN_PLAYERS:
            return self.err("E_NOT_ENOUGH")
        self.start_placing(r)
        self.push_room(r)
        self.push_lobby()
        return {"ok": True}

    def cmd_leave(self, p, req):
        if not p.room and p not in self.match_queue:
            return self.err("E_NOT_IN_ROOM")
        self.remove_from_room(p, "離開房間")
        p.send(self.lobby_state(p))
        return {"ok": True, "msg": "已回到大廳"}

    def cmd_place(self, p, req):
        r = p.room
        if p.role != "player":
            return self.err("E_SPECTATOR")
        if not r or r.phase != "PLACING":
            return self.err("E_PHASE", "現在不是佈陣階段")
        ok, why, result = validate_fleet(req.get("ships"))
        if not ok:
            return self.err("E_PLACE", why)
        p.ships = {sid: {"cells": [tuple(c) for c in cells], "hits": set()} for sid, cells in result.items()}
        p.ready = True
        log.info("[PLACE] %s 佈陣完成", p.name)
        if all(q.ready for q in r.players) and len(r.players) >= MIN_PLAYERS:
            self.start_battle(r)
        self.push_room(r)
        self.push_lobby()
        return {"ok": True, "msg": "佈陣完成" if r.phase == "BATTLE" else "佈陣完成，等待其他人"}

    def cmd_fire(self, p, req):
        r = p.room
        if p.role != "player":
            return self.err("E_SPECTATOR")
        if not r or r.phase != "BATTLE":
            return self.err("E_PHASE", "現在不是交戰階段")
        if r.players[r.turn] is not p:
            return self.err("E_TURN")
        if r.paused:
            return self.err("E_PHASE", "有玩家斷線中，等待重連")
        assigned = self.assigned_target(r, p)
        want = req.get("target")
        if want is not None and assigned is not None and want != assigned.name:
            return self.err("E_TARGET", f"你只能攻擊 {assigned.name}（開局抽籤決定）")
        target = assigned
        if target is None:
            return self.err("E_TARGET")
        try:
            x, y = int(req["x"]), int(req["y"])
        except (KeyError, TypeError, ValueError):
            return self.err("E_RANGE", "需要整數座標 x、y")
        if not (0 <= x < SIZE and 0 <= y < SIZE):
            return self.err("E_RANGE", f"座標 ({x},{y}) 超出 0~{SIZE - 1}")
        if (x, y) in target.incoming:
            return self.err("E_REPEAT", f"{target.name} 的 {coord(x, y)} 已經被打過了")
        result, sunk = "miss", None
        for sid, s in target.ships.items():
            if (x, y) in s["cells"]:
                s["hits"].add((x, y))
                result = "hit"
                if len(s["hits"]) == len(s["cells"]):
                    result, sunk = "sunk", sid
                    for c in s["cells"]:
                        target.incoming[c] = "sunk"
                break
        target.incoming.setdefault((x, y), result)
        r.timeouts[p.name] = 0
        ev = {"event": "SHOT", "by": p.name, "target": target.name, "x": x, "y": y,
              "at": coord(x, y), "result": result}
        if sunk:
            ev["ship"] = FLEET[sunk][0]
            ev["cells"] = target.ships[sunk]["cells"]
        log.info("[FIRE] %s -> %s %s：%s%s", p.name, target.name, coord(x, y), result,
                 f"（{FLEET[sunk][0]}）" if sunk else "")
        self.room_event(r, ev)
        if target.ships_left() == 0:
            self.eliminate(r, target, f"被 {p.name} 擊沉全部艦隊")
        again = False
        if r.phase == "BATTLE":
            if result == "miss":
                self.next_turn(r)
            else:
                # 規則：命中（或擊沉）可以繼續開火，回合計時重新開始
                again = True
                r.turn_no += 1
                r.deadline = time.time() + self.turn_secs
                self.room_event(r, {"event": "AGAIN", "player": p.name,
                                    "msg": f"{p.name} 命中，繼續開火！"})
        self.push_room(r)
        return {"ok": True, "result": result, "at": coord(x, y), "target": target.name, "again": again}

    def cmd_chat(self, p, req):
        r = p.room
        if not r:
            return self.err("E_NOT_IN_ROOM")
        text = str(req.get("text", "")).strip()
        if not text or len(text) > 80:
            return self.err("E_CHAT")
        self.room_event(r, {"event": "CHAT", "from": p.name, "role": p.role, "text": text})
        return {"ok": True}

    def cmd_surrender(self, p, req):
        r = p.room
        if p.role != "player":
            return self.err("E_SPECTATOR")
        if not r or r.phase != "BATTLE" or not p.alive:
            return self.err("E_PHASE", "交戰中才能投降")
        self.eliminate(r, p, "投降")
        self.push_room(r)
        return {"ok": True}

    def cmd_rematch(self, p, req):
        r = p.room
        if p.role != "player":
            return self.err("E_SPECTATOR")
        if not r or r.phase != "OVER":
            return self.err("E_PHASE", "對局結束後才能再來一局")
        r.rematch.add(p)
        present = [q for q in r.active() if q.online]
        if len(present) >= MIN_PLAYERS and all(q in r.rematch for q in present):
            r.players = present
            log.info("[ROOM] %s 再來一局", r.code)
            self.start_placing(r)
            self.room_event(r, {"event": "REMATCH", "msg": "全員同意，再來一局！請重新佈陣"})
        self.push_room(r)
        self.push_lobby()
        return {"ok": True, "msg": "已送出再戰邀請"}

    def cmd_add_bot(self, p, req):
        """房主加入機器人：Server 開一條執行緒跑 bot，它會像一般 Client 一樣用 TCP 連回本機"""
        r = p.room
        if not r or r.phase != "WAITING":
            return self.err("E_PHASE", "等待中的房間才能加入機器人")
        if r.owner != p.name:
            return self.err("E_OWNER", "只有房主可以加入機器人")
        if len(r.players) >= r.capacity:
            return self.err("E_ROOM_FULL")
        for _ in range(50):
            name = BOT_PREFIX + random_name()[-2:] + str(random.randint(1, 99))
            if name not in self.players:
                break
        from bot import run_bot
        threading.Thread(target=run_bot, args=("127.0.0.1", self.port, name),
                         kwargs={"join": r.code, "delay": 1.0, "rematch": True, "quiet": True, "auto_leave": True},
                         daemon=True).start()
        log.info("[BOT ] %s 在房間 %s 加入機器人 %s", p.name, r.code, name)
        return {"ok": True, "msg": f"機器人 {name} 加入中…"}

    def cmd_color(self, p, req):
        """選擇自己板子的顏色（房間內不能和別人重複）"""
        r = p.room
        if not r or p.role != "player":
            return self.err("E_SPECTATOR", "進入房間的玩家才能選顏色")
        if r.phase in ("BATTLE", "OVER"):
            return self.err("E_PHASE", "遊戲開始後就不能更換顏色（請在等待或佈陣時選好）")
        c = str(req.get("color", "")).lower()
        if c not in COLORS:
            return self.err("E_COLOR", "沒有這個顏色")
        if any(q.color == c for q in r.active() if q is not p):
            owner = next(q.name for q in r.active() if q.color == c and q is not p)
            return self.err("E_COLOR", f"這個顏色 {owner} 已經在用了")
        p.color = c
        log.info("[COLOR] %s 換成 %s", p.name, c)
        self.push_room(r)
        return {"ok": True}

    def cmd_emote(self, p, req):
        r = p.room
        if not r:
            return self.err("E_NOT_IN_ROOM")
        kind = str(req.get("kind", ""))
        if kind not in EMOTES:
            return self.err("E_EMOTE")
        now = time.time()
        if now - self.last_emote.get(p.name, 0) < 1.2:
            return self.err("E_COOLDOWN", "表情太頻繁了，稍等一下")
        self.last_emote[p.name] = now
        self.room_event(r, {"event": "EMOTE", "from": p.name, "kind": kind, "role": p.role})
        return {"ok": True}

    # ------------------------------------------------------------ 連線
    def client_thread(self, conn):
        reader = LineReader(conn.sock)
        with self.lock:
            self.conns.add(conn)
        try:
            while True:
                lines = reader.read_lines()
                if lines is None:
                    break
                conn.last_seen = time.time()
                for line in lines:
                    resp = (self.err("E_BAD_REQUEST", "單則訊息過長") if line == "__TOO_LONG__"
                            else self.handle(conn, line))
                    if resp is not None:
                        conn.send(resp)
                        if not resp.get("ok"):
                            log.info("[SEND] %s 錯誤 %s：%s", conn.tag, resp.get("code"), resp.get("msg"))
        except OSError as e:
            log.warning("[DROP] %s 連線異常：%s", conn.tag, e)
        finally:
            conn.alive = False
            try:
                conn.sock.close()
            except OSError:
                pass
            with self.lock:
                self.conns.discard(conn)
                self.on_disconnect(conn)

    def on_disconnect(self, conn):
        p = conn.player
        if not p or p.conn is not conn:
            log.info("[BYE ] %s 已斷線", conn.tag)
            return
        r = p.room
        if p in self.match_queue:
            self.match_queue.remove(p)
        if r and p.role == "player" and r.phase in ("PLACING", "BATTLE") and not p.left and p.alive:
            p.offline_until = time.time() + self.grace          # 對局中：保留座位等他回來
            log.info("[BYE ] %s 對局中斷線，保留 %d 秒", conn.tag, self.grace)
            self.room_event(r, {"event": "OFFLINE", "player": p.name, "grace": self.grace,
                                "msg": f"{p.name} 斷線了，等待 {self.grace} 秒重連"})
            self.update_pause(r)
            self.push_room(r)
        else:
            log.info("[BYE ] %s 已斷線", conn.tag)
            if r:
                self.remove_from_room(p, "斷線離開")
            self.players.pop(p.name, None)
            self.push_lobby()

    # ------------------------------------------------------------ UDP 自動搜尋
    def discovery(self):
        """回應區網廣播：收到 DISCOVER 就回覆 Server 名稱與 TCP Port"""
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)    # UDP
            u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            u.bind(("", DISCOVERY_PORT))
        except OSError as e:
            log.warning("[UDP ] 無法開啟搜尋 Port %d（%s），只能手動輸入 IP", DISCOVERY_PORT, e)
            return
        log.info("[UDP ] 自動搜尋服務：UDP %d", DISCOVERY_PORT)
        seen = set()
        while True:
            try:
                data, addr = u.recvfrom(1024)
            except OSError:
                continue
            if data.decode(errors="ignore").strip() != DISCOVER_MAGIC:
                continue
            with self.lock:
                info = {"magic": HERE_MAGIC, "name": self.name, "port": self.port,
                        "players": sum(1 for q in self.players.values() if q.online),
                        "rooms": len(self.rooms)}
            try:
                u.sendto(json.dumps(info, ensure_ascii=False).encode(), addr)
                if addr[0] not in seen:            # 每個來源只記一次，避免日誌洗版
                    seen.add(addr[0])
                    log.info("[UDP ] 回應 %s 的自動搜尋（UDP 廣播）", addr[0])
            except OSError:
                pass

    # ------------------------------------------------------------ 啟動
    def start(self, host="0.0.0.0", port=DEFAULT_PORT):
        """綁定 Port（失敗會丟出 OSError），再於背景執行緒接受連線。GUI 的「我當主機」也用這個。"""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)        # IPv4 + TCP
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(32)
        self.port = port
        log.info("海戰棋 Server 啟動：%s:%d（TCP），每回合 %d 秒，斷線保留 %d 秒",
                 host, port, self.turn_secs, self.grace)
        for ip in local_ips():
            log.info("  Client 可連線位址：%s:%d", ip, port)
        threading.Thread(target=self.ticker, daemon=True).start()
        threading.Thread(target=self.discovery, daemon=True).start()
        threading.Thread(target=self.accept_loop, args=(srv,), daemon=True).start()
        return srv

    def accept_loop(self, srv):
        while True:
            try:
                sock, addr = srv.accept()
            except OSError:
                return
            conn = Conn(sock, addr)
            log.info("[CONN] %s:%d 已連線（TCP 三向交握完成）", *addr)
            threading.Thread(target=self.client_thread, args=(conn,), daemon=True).start()


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))         # 不會真的送封包，只用來查對外網卡 IP
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips) or ["127.0.0.1"]


def setup_logging(path=None, handler=None):
    fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    hs = [handler] if handler else [logging.StreamHandler(sys.stdout)]
    if path:
        hs.append(logging.FileHandler(path, encoding="utf-8"))
    for h in hs:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def main():
    ap = argparse.ArgumentParser(description="海戰棋 Server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--turn", type=int, default=30, help="每回合秒數")
    ap.add_argument("--grace", type=int, default=30, help="斷線保留秒數")
    ap.add_argument("--log", default="battleship_server.log")
    a = ap.parse_args()
    setup_logging(a.log)
    try:
        Server(a.turn, a.grace).start(a.host, a.port)
    except OSError as e:
        log.error("無法使用 Port %d：%s（是否已經有 Server 在跑？）", a.port, e)
        sys.exit(1)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("Server 關閉")


if __name__ == "__main__":
    main()
