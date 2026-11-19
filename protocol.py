"""海戰棋 共用通訊協定

應用層格式：JSON Lines —— 每則訊息一行 JSON，以 '\n' 結尾，UTF-8。
TCP 是位元組串流、不保留訊息邊界，接收端要先累積緩衝區再依換行切開。
"""
import json
import random

DEFAULT_PORT = 9100
DISCOVERY_PORT = 9101          # UDP：區網自動搜尋 Server 用
DISCOVER_MAGIC = "BATTLESHIP_DISCOVER_V1"
HERE_MAGIC = "BATTLESHIP_HERE_V1"
MIN_PLAYERS, MAX_PLAYERS = 2, 4
HEARTBEAT = 5                  # Client 每 5 秒送一次 PING
DEAD_AFTER = 20                # 20 秒沒收到任何資料就視為斷線
BOT_PREFIX = "AI·"             # 機器人暱稱開頭

# 可選的板子顏色（同一房間不能重複；聊天、戰況裡這位玩家的文字也用同一個顏色）
COLORS = ["#ff6b5b", "#38bdf8", "#fbbf24", "#4ade80", "#a78bfa", "#f472b6", "#fb923c", "#2dd4bf"]

# 表情（自己畫的卡通臉，不用 emoji 字元，各平台都能正常顯示）
EMOTES = {"laugh": "笑死", "cool": "太帥", "angry": "生氣", "cry": "哭哭",
          "tongue": "吐舌", "sleep": "想睡", "boom": "爆炸", "gg": "GG"}

_ADJ = ["勇敢的", "迷路的", "愛睡的", "超快的", "害羞的", "暴躁的", "閃亮的", "神秘的", "餓扁的", "淡定的",
        "無敵的", "傲嬌的", "呆萌的", "冷靜的", "熱血的"]
_NOUN = ["鯨魚", "章魚", "海豚", "海鷗", "螃蟹", "企鵝", "鯊魚", "海龜", "水母", "河豚", "海獺", "龍蝦"]


def random_name():
    """隨機暱稱，例如「迷路的章魚」（最多 12 字）"""
    return random.choice(_ADJ) + random.choice(_NOUN)
ENCODING = "utf-8"
MAX_LINE = 64 * 1024
SIZE = 10                      # 10 x 10 棋盤

# 艦隊：id -> (中文名, 長度)
FLEET = {
    "carrier": ("航空母艦", 5),
    "battleship": ("戰艦", 4),
    "cruiser": ("巡洋艦", 3),
    "submarine": ("潛艇", 3),
    "destroyer": ("驅逐艦", 2),
}

COLS = "ABCDEFGHIJ"


def coord(x, y):
    """(0,0) -> 'A1'"""
    return f"{COLS[x]}{y + 1}"


def encode(msg):
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode(ENCODING)


def send_msg(sock, msg):
    sock.sendall(encode(msg))


class LineReader:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def read_lines(self):
        """回傳完整的行；連線關閉回傳 None"""
        data = self.sock.recv(4096)
        if not data:
            return None
        self.buf += data
        if len(self.buf) > MAX_LINE and b"\n" not in self.buf:
            self.buf = b""
            return ["__TOO_LONG__"]
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.strip()
            if line:
                out.append(line.decode(ENCODING, errors="replace"))
        return out


def ship_cells(x, y, size, d):
    return [(x + i, y) if d == "H" else (x, y + i) for i in range(size)]


def validate_fleet(ships):
    """檢查佈陣是否合法，回傳 (ok, 錯誤訊息, {id: cells})"""
    if not isinstance(ships, list):
        return False, "ships 必須是陣列", None
    ids = [s.get("id") for s in ships if isinstance(s, dict)]
    if sorted(ids) != sorted(FLEET):
        return False, f"艦隊必須剛好包含：{', '.join(n for n, _ in FLEET.values())} 各一艘", None
    used = {}
    result = {}
    for s in ships:
        try:
            x, y, d = int(s["x"]), int(s["y"]), str(s.get("dir", "H")).upper()
        except (KeyError, TypeError, ValueError):
            return False, "每艘船需要 x、y、dir", None
        if d not in ("H", "V"):
            return False, "dir 只能是 H（橫）或 V（直）", None
        name, size = FLEET[s["id"]]
        cells = ship_cells(x, y, size, d)
        for cx, cy in cells:
            if not (0 <= cx < SIZE and 0 <= cy < SIZE):
                return False, f"{name} 超出棋盤範圍", None
            if (cx, cy) in used:
                return False, f"{name} 與 {used[(cx, cy)]} 重疊於 {coord(cx, cy)}", None
            used[(cx, cy)] = name
        result[s["id"]] = cells
    return True, "", result


def random_fleet():
    """隨機佈陣（Client 的「隨機」按鈕與機器人共用）"""
    while True:
        used, ships = set(), []
        ok = True
        for sid, (_, size) in FLEET.items():
            for _ in range(200):
                d = random.choice("HV")
                x = random.randrange(SIZE - (size - 1 if d == "H" else 0))
                y = random.randrange(SIZE - (size - 1 if d == "V" else 0))
                cells = ship_cells(x, y, size, d)
                if not used.intersection(cells):
                    used.update(cells)
                    ships.append({"id": sid, "x": x, "y": y, "dir": d})
                    break
            else:
                ok = False
                break
        if ok:
            return ships


ERRORS = {
    "E_BAD_REQUEST": "訊息格式錯誤（不是合法 JSON）",
    "E_UNKNOWN_CMD": "未知的指令",
    "E_AUTH": "請先登入",
    "E_NAME": "暱稱需為 1~12 個字",
    "E_NAME_TAKEN": "這個暱稱已有人在線上使用",
    "E_IN_ROOM": "你已經在房間裡了",
    "E_NO_ROOM": "找不到這個房間",
    "E_ROOM_FULL": "房間已滿",
    "E_NOT_IN_ROOM": "你不在任何房間",
    "E_PHASE": "現在不能做這個操作",
    "E_PLACE": "佈陣不合法",
    "E_TURN": "還沒輪到你",
    "E_RANGE": "座標超出棋盤",
    "E_REPEAT": "這格已經打過了",
    "E_CHAT": "訊息不可空白或超過 80 字",
    "E_TARGET": "目標不存在或已被淘汰",
    "E_OWNER": "只有房主可以開始",
    "E_NOT_ENOUGH": "至少要 2 位玩家才能開始",
    "E_SPECTATOR": "觀戰者不能做這個操作",
    "E_CAPACITY": "人數需為 2~4",
    "E_COOLDOWN": "太頻繁了，稍等一下",
    "E_EMOTE": "沒有這個表情",
    "E_COLOR": "這個顏色已經有人用了",
}


def _local_ipv4():
    import socket
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def discover(timeout=1.2):
    """用 UDP 廣播在區網裡找 Server（不用手動輸入 IP）

    送出：DISCOVER_MAGIC 到 255.255.255.255:9101（以及各網段的廣播位址）
    收到：{"magic": HERE_MAGIC, "name": ..., "port": 9100, ...}，對方 IP 就是封包來源位址
    UDP 不需要先建立連線，很適合「對整個區網喊一聲」這種用途；真正的遊戲資料仍走 TCP。
    """
    import socket
    import time
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    u.settimeout(0.2)
    targets = {"255.255.255.255", "127.0.0.1"}
    for ip in _local_ipv4():
        a = ip.split(".")
        targets.add(".".join(a[:3] + ["255"]))                 # /24 網段廣播
        if ip.startswith("172.20.10."):
            targets.add("172.20.10.15")                       # iPhone 熱點是 /28
    found = {}
    msg = DISCOVER_MAGIC.encode()
    for t in targets:
        try:
            u.sendto(msg, (t, DISCOVERY_PORT))
        except OSError:
            pass
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = u.recvfrom(2048)
            info = json.loads(data.decode())
        except (OSError, ValueError):
            continue
        if info.get("magic") != HERE_MAGIC:
            continue
        info["ip"] = addr[0]
        key = (info.get("name"), info.get("port"))
        old = found.get(key)
        if old is None or old["ip"].startswith("127."):         # 同一台優先顯示區網 IP
            found[key] = info
    u.close()
    return sorted(found.values(), key=lambda d: d["ip"])
