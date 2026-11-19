"""海戰棋 機器人 Client

機器人是一個「真正的 Client」：它也用 TCP 連到 Server、送一樣的 JSON 指令，
所以 Server 對它和對真人一視同仁。自動佈陣、自動開火（先隨機搜尋，打中後沿周圍追擊）。

  python bot.py                          自動搜尋 Server 並快速配對
  python bot.py --host 192.168.1.20 --join 1234
  python bot.py --create 3               建立 3 人房
  python bot.py --watch 1234             觀戰（終端機文字轉播）
房主在遊戲畫面按「＋ 加入機器人」時，Server 會在背景直接呼叫 run_bot()。
"""
import argparse
import json
import random
import socket
import sys
import threading
import time

from protocol import (BOT_PREFIX, DEFAULT_PORT, HEARTBEAT, SIZE, LineReader, discover, random_name,
                      random_fleet, send_msg)


def choose_target(shots):
    tried = {(x, y) for x, y, _ in shots}
    hits = [(x, y) for x, y, v in shots if v == "hit"]          # 打中但還沒沉
    if hits:
        cand = []
        hs = set(hits)
        for x, y in hits:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < SIZE and 0 <= ny < SIZE and (nx, ny) not in tried:
                    score = 2 if (x - dx, y - dy) in hs else 1   # 與相鄰命中格同一直線的優先
                    cand.append((score, random.random(), (nx, ny)))
        if cand:
            return max(cand)[2]
    free = [(x, y) for x in range(SIZE) for y in range(SIZE) if (x, y) not in tried]
    parity = [c for c in free if (c[0] + c[1]) % 2 == 0]
    return random.choice(parity or free)


def run_bot(host, port, name, join="", create=0, watch="", delay=1.2, rematch=False,
            quiet=False, auto_leave=False):
    say = (lambda *a: None) if quiet else print
    try:
        sock = socket.create_connection((host, port), timeout=5)
    except OSError as e:
        say("連線失敗：", e)
        return
    sock.settimeout(None)
    lock = threading.Lock()

    def send(msg):
        with lock:
            send_msg(sock, msg)

    def pinger():
        while True:
            time.sleep(HEARTBEAT)
            try:
                send({"cmd": "PING"})
            except OSError:
                return
    threading.Thread(target=pinger, daemon=True).start()

    reader = LineReader(sock)
    send({"cmd": "LOGIN", "name": name})
    joined = False
    fired_turn = None
    try:
        while True:
            lines = reader.read_lines()
            if lines is None:
                say("Server 關閉連線")
                return
            for line in lines:
                m = json.loads(line)
                ev = m.get("event")
                if m.get("cmd") == "LOGIN" and not m.get("ok"):
                    say("登入失敗：", m.get("msg"))
                    return
                if ev == "LOBBY":
                    if joined:
                        if auto_leave:
                            return                     # 已經離開房間，機器人結束
                        continue
                    if m.get("matching"):
                        continue
                    joined = True
                    if watch:
                        send({"cmd": "JOIN", "code": watch, "spectate": True})
                    elif join:
                        send({"cmd": "JOIN", "code": join})
                    elif create:
                        send({"cmd": "CREATE", "capacity": create})
                    else:
                        send({"cmd": "MATCH"})
                elif ev == "SHOT":
                    tag = {"miss": "未命中", "hit": "命中", "sunk": f"擊沉 {m.get('ship')}"}[m["result"]]
                    say(f"  {m['by']} → {m['target']} {m['at']}：{tag}")
                    if m["result"] == "sunk" and random.random() < 0.6:
                        if m["by"] == name:
                            send({"cmd": "EMOTE", "kind": random.choice(["cool", "tongue", "laugh"])})
                        elif m["target"] == name:
                            send({"cmd": "EMOTE", "kind": random.choice(["cry", "angry"])})
                elif ev in ("BATTLE_START", "GAME_OVER", "LEFT", "OFFLINE", "TIMEOUT", "REMATCH", "ELIMINATED"):
                    say(f"[{ev}] {m.get('msg') or m.get('winner', '')} {m.get('reason', '')}")
                    if ev == "GAME_OVER":
                        if m.get("winner") == name:
                            send({"cmd": "EMOTE", "kind": "gg"})
                        if rematch:
                            time.sleep(1.5)
                            send({"cmd": "REMATCH"})
                elif ev == "CHAT":
                    say(f"  💬 {m['from']}：{m['text']}")
                elif ev == "STATE":
                    if m.get("role") != "player":
                        continue
                    humans = [b for b in m["boards"] if not b["name"].startswith(BOT_PREFIX) and not b["left"]]
                    if auto_leave and not humans:
                        send({"cmd": "LEAVE"})              # 房裡沒有真人了，機器人自己離開
                        continue
                    if m["phase"] == "PLACING" and not m["me"]["ready"]:
                        time.sleep(0.4)
                        send({"cmd": "PLACE", "ships": random_fleet()})
                    elif m["phase"] == "BATTLE" and m.get("turn") == name and not m.get("paused"):
                        key = m.get("turn_no")
                        if fired_turn != key:
                            fired_turn = key
                            time.sleep(delay * random.uniform(0.7, 1.3))
                            tgt = next((b for b in m["boards"] if b["name"] == m.get("turn_target")), None)
                            if tgt is None:
                                continue
                            x, y = choose_target(tgt["incoming"])
                            send({"cmd": "FIRE", "target": tgt["name"], "x": x, "y": y})
                    elif m["phase"] == "OVER" and rematch and name not in m.get("rematch", []) and m.get("winner"):
                        pass
                    elif m["phase"] == "WAITING":
                        say(f"[{name}] 在房間 {m['code']} 等待其他玩家")
                elif m.get("ok") is False and m.get("code") != "E_COOLDOWN":
                    say("錯誤：", m.get("code"), m.get("msg"))
    except (OSError, ValueError):
        return
    finally:
        try:
            sock.close()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="auto", help="Server IP；auto = 用 UDP 廣播自動尋找")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--name", default="")
    ap.add_argument("--join", default="")
    ap.add_argument("--create", type=int, default=0, help="建立 N 人房間（2~4）")
    ap.add_argument("--watch", default="", help="以觀戰者身分進入房間")
    ap.add_argument("--delay", type=float, default=1.2, help="每次開火前思考秒數")
    ap.add_argument("--rematch", action="store_true")
    a = ap.parse_args()
    if a.host == "auto":
        found = discover()
        if not found:
            print("區網內找不到 Server，請用 --host 指定 IP")
            sys.exit(1)
        a.host, a.port = found[0]["ip"], found[0]["port"]
        print(f"自動找到 Server：{found[0]['name']} {a.host}:{a.port}")
    name = a.name or BOT_PREFIX + random_name()[-2:] + str(random.randint(1, 99))
    print(f"[{name}] 連線到 {a.host}:{a.port}")
    run_bot(a.host, a.port, name, a.join, a.create, a.watch, a.delay, a.rematch)


if __name__ == "__main__":
    main()
