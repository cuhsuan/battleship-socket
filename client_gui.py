"""海戰棋 Client（圖形介面）：2～4 人混戰＋觀戰

執行：python client_gui.py
      python client_gui.py --host 192.168.1.20 --name 阿明    （帶參數直接連線）

連線方式（三選一）：
  1. 區網自動搜尋：開啟後自動用 UDP 廣播找 Server，點一下就連
  2. 我當主機：在這個視窗裡直接啟動 Server，其他人用自動搜尋就找得到你
  3. 手動輸入 IP

只用 Python 標準函式庫。收訊在背景執行緒，訊息丟進 queue 由 UI 主執行緒處理。
"""
import argparse
import json
import logging
import math
import os
import queue
import random
import re
import socket
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

from art import (CHAR, FIRE_O, INK, PLAYER_COLORS, Geo, Painter, blend, boom, cannon, emote_face, fire, miss_mark,
                 shell, ship_shape, smoke, splash, tray_ships, tray_width)
from protocol import (COLS, DEAD_AFTER, DEFAULT_PORT, EMOTES, FLEET, HEARTBEAT, MAX_PLAYERS, SIZE, LineReader, coord,
                      discover, random_fleet, random_name, send_msg, ship_cells)

# ---------------------------------------------------------------- 配色：深海雷達
BG = "#06121a"
PANEL = "#0b1a24"
CARD = "#10232f"
LINE = "#1a3a4b"
WATER = "#0a2130"
WATER2 = "#0c2636"
TEXT = "#e2f1f5"
MUTED = "#7193a3"
ACCENT = "#36f1c6"
ACCENT_DK = "#0f3b36"
WARN = "#ffb547"
HIT = "#ff4f4f"
HIT_BG = "#3d1419"
SUNK = "#7c1d29"
MISS_BG = "#26495c"     # 未命中格：整格變色
SHIP = "#4c6878"
SHIP_EDGE = "#8db0bf"
SESSION_FILE = os.path.join(os.path.expanduser("~"), ".battleship_session.json")   # 記住登入憑證，程式重開也能回到對局
PHASE_ZH = {"WAITING": "等待玩家", "PLACING": "佈陣中", "BATTLE": "交戰中", "OVER": "已結束"}


def pick_font(root, names, fallback):
    fams = set(tkfont.families(root))
    for n in names:
        if n in fams:
            return n
    return fallback


def blend(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class FlatButton(tk.Label):
    def __init__(self, master, text, command, color=ACCENT, fg="#04110e", font=None, padx=14, pady=7):
        super().__init__(master, text=text, bg=color, fg=fg, font=font, padx=padx, pady=pady, cursor="hand2")
        self.color, self.fg0, self.command, self.enabled = color, fg, command, True
        self.bind("<Button-1>", lambda e: self.enabled and self.command())
        self.bind("<Enter>", lambda e: self.enabled and self.config(bg=blend(self.color, "#ffffff", 0.15)))
        self.bind("<Leave>", lambda e: self.config(bg=self.color if self.enabled else LINE))

    def set_enabled(self, on):
        self.enabled = on
        self.config(bg=self.color if on else LINE, fg=self.fg0 if on else MUTED, cursor="hand2" if on else "")


class QueueLogHandler(logging.Handler):
    """「我當主機」時，把 Server 的日誌送到 GUI 顯示"""

    def __init__(self, inbox):
        super().__init__()
        self.inbox = inbox

    def emit(self, record):
        self.inbox.put(("srvlog", self.format(record)))


# ================================================================ 網路層
class Net:
    def __init__(self, inbox):
        self.inbox = inbox
        self.sock = None
        self.lock = threading.Lock()
        self.connected = False

    def connect(self, host, port, attempts=3):
        last = None
        for i in range(1, attempts + 1):
            try:
                s = socket.create_connection((host, port), timeout=4)   # TCP 三向交握
                s.settimeout(None)
                self.sock = s
                self.connected = True
                threading.Thread(target=self._reader, args=(s,), daemon=True).start()
                return True, s.getsockname()
            except ConnectionRefusedError:
                last = "連線被拒：Server 沒有啟動，或 Port 不對"
            except socket.timeout:
                last = "連線逾時：IP 錯誤、不在同一網路，或被防火牆擋下"
            except socket.gaierror:
                return False, "IP 位址格式錯誤或找不到主機"
            except OSError as e:
                last = f"連線失敗：{e}"
            self.inbox.put(("retry", f"{last}（第 {i}/{attempts} 次）"))
            if i < attempts:
                time.sleep(1.5)
        return False, last

    def _reader(self, s):
        r = LineReader(s)
        while True:
            try:
                lines = r.read_lines()
            except OSError:
                lines = None
            if lines is None:
                if self.sock is s:
                    self.connected = False
                    self.inbox.put(("closed", None))
                return
            for line in lines:
                try:
                    self.inbox.put(("msg", json.loads(line)))
                except json.JSONDecodeError:
                    self.inbox.put(("raw", line))

    def send(self, msg):
        if not self.sock:
            return False
        try:
            with self.lock:
                send_msg(self.sock, msg)
            return True
        except OSError:
            self.connected = False
            self.inbox.put(("closed", None))
            return False

    def close(self):
        s, self.sock = self.sock, None
        self.connected = False
        if s:
            try:
                s.close()
            except OSError:
                pass


# ================================================================ 主程式
class App:
    def __init__(self, root, args):
        self.root, self.args = root, args
        root.title("海戰棋 BATTLESHIP · Socket Client")
        root.configure(bg=BG)
        root.geometry("1360x920")
        root.minsize(1300, 860)
        sans = pick_font(root, ["Microsoft JhengHei UI", "Microsoft JhengHei", "PingFang TC", "Heiti TC",
                                "Noto Sans CJK TC", "Noto Sans TC"], "TkDefaultFont")
        mono = pick_font(root, ["Cascadia Mono", "Consolas", "Menlo", "SF Mono", "DejaVu Sans Mono"], "TkFixedFont")
        self.f = {"title": (sans, 22, "bold"), "h1": (sans, 18, "bold"), "h2": (sans, 13, "bold"),
                  "b": (sans, 11), "bb": (sans, 11, "bold"), "s": (sans, 9), "sb": (sans, 9, "bold"),
                  "xs": (mono, 7), "m": (mono, 9), "mb": (mono, 10, "bold"), "mbig": (mono, 16, "bold")}
        self.inbox = queue.Queue()
        self.net = Net(self.inbox)
        self.name = self.token = self.host = self.port = self.local = None
        self.screen = None
        self.st = None
        self.st_at = 0.0
        self.reconnecting = False
        self.rtt = None
        self.ping_t = None
        self.anim = 0.0
        self.turn_secs = 30
        self.again_until = 0.0
        self.show_packets = True
        self.hosting = None          # 我當主機時的 Server 物件
        self.found = []
        self.scanning = False
        self.capacity = 2
        self.views = {}
        self.flights = []            # 飛行中／剛落地的砲彈（動畫用）
        self.manual_view = None
        self.last_target = None
        self.target = None
        self.over_at = 0.0
        self.colors = {}
        self.cover = False           # 遮住我的船（防偷看）
        self.show_side = True        # 戰況欄開／關
        self.side_w = 260            # 戰況欄寬度（可拖曳分隔線調整）
        self.emotes = []             # (from, kind, t0)
        self.last_rx = time.time()
        self.reset_placement()

        self.build_shell()
        self.show_connect()
        root.bind("<KeyPress-r>", lambda e: self.rotate() if self.focus_ok() else None)
        root.bind("<KeyPress-R>", lambda e: self.rotate() if self.focus_ok() else None)
        self.root.after(40, self.pump)
        self.root.after(5000, self.heartbeat)
        if args.host and args.name:
            self.root.after(300, lambda: self.connect_to(args.host, args.port))

    def focus_ok(self):
        return not isinstance(self.root.focus_get(), tk.Entry)

    def reset_placement(self):
        self.place = {}
        self.sel = "carrier"
        self.dir = "H"

    # ------------------------------------------------------------ 外框
    def build_shell(self):
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=24, pady=(12, 4))
        logo = tk.Canvas(top, width=40, height=40, bg=BG, highlightthickness=0)
        logo.pack(side="left")
        for r in (18, 12, 6):
            logo.create_oval(20 - r, 20 - r, 20 + r, 20 + r, outline=ACCENT_DK if r != 18 else ACCENT, width=2)
        logo.create_line(20, 20, 34, 9, fill=ACCENT, width=2)
        logo.create_oval(27, 23, 31, 27, fill=HIT, outline="")
        tk.Label(top, text="海戰棋", font=self.f["title"], fg=TEXT, bg=BG).pack(side="left", padx=(10, 6))
        tk.Label(top, text="BATTLESHIP · TCP", font=self.f["mb"], fg=ACCENT, bg=BG).pack(side="left", pady=(8, 0))
        self.host_chip = tk.Label(top, text="", font=self.f["sb"], fg="#04110e", bg=WARN, padx=8, pady=3)

        self.btn_pkt = FlatButton(top, "封包監看 ▾", self.toggle_packets, color=CARD, fg=TEXT, font=self.f["sb"])
        self.btn_pkt.pack(side="right", padx=(10, 0))
        self.conn_box = tk.Frame(top, bg=PANEL, padx=12, pady=5, highlightthickness=1, highlightbackground=LINE)
        self.conn_box.pack(side="right")
        self.conn_dot = tk.Label(self.conn_box, text="●", font=self.f["b"], fg=MUTED, bg=PANEL)
        self.conn_dot.pack(side="left")
        self.conn_lbl = tk.Label(self.conn_box, text="尚未連線", font=self.f["m"], fg=MUTED, bg=PANEL, justify="left")
        self.conn_lbl.pack(side="left", padx=(6, 0))

        self.banner = tk.Frame(self.root, bg=HIT)
        self.banner_lbl = tk.Label(self.banner, text="", font=self.f["bb"], fg="#140606", bg=HIT, pady=6)
        self.banner_lbl.pack(side="left", padx=12)
        FlatButton(self.banner, "放棄並回首頁", self.abandon, color="#140606", fg=TEXT, font=self.f["sb"], pady=4).pack(side="right", padx=8, pady=4)
        self.top = top

        # 底部：封包監看（主機模式再多一欄 Server 日誌）
        self.pkt_frame = tk.Frame(self.root, bg=BG)
        left = tk.Frame(self.pkt_frame, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="TCP 封包監看   → Client 送出   ← Server 回應／推播   （JSON Lines：一行一則，以 \\n 分隔）",
                 font=self.f["s"], fg=MUTED, bg=PANEL, anchor="w").pack(fill="x", padx=12, pady=(6, 0))
        self.pkt = self.mk_text(left)
        self.srv_wrap = tk.Frame(self.pkt_frame, bg=PANEL, highlightthickness=1, highlightbackground=WARN)
        tk.Label(self.srv_wrap, text="Server 日誌（你是主機）", font=self.f["sb"], fg=WARN, bg=PANEL,
                 anchor="w").pack(fill="x", padx=12, pady=(6, 0))
        self.srvlog = self.mk_text(self.srv_wrap)
        self.pkt_frame.pack(side="bottom", fill="x", padx=24, pady=(0, 12))

        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(fill="both", expand=True, padx=24, pady=4)

        self.toast = tk.Frame(self.root, bg=CARD, highlightthickness=2, highlightbackground=ACCENT)
        self.toast_t = tk.Label(self.toast, text="", font=self.f["h2"], fg=ACCENT, bg=CARD, anchor="w")
        self.toast_t.pack(fill="x", padx=16, pady=(10, 0))
        self.toast_m = tk.Label(self.toast, text="", font=self.f["b"], fg=TEXT, bg=CARD, anchor="w",
                                wraplength=320, justify="left")
        self.toast_m.pack(fill="x", padx=16, pady=(2, 12))
        self.toast_after = None

    def mk_text(self, parent):
        t = tk.Text(parent, height=6, bg=PANEL, fg=MUTED, font=self.f["m"], relief="flat",
                    highlightthickness=0, wrap="none", padx=12, pady=4)
        t.pack(fill="both", expand=True)
        for tag, col in (("out", WARN), ("in", "#7fd3ff"), ("push", ACCENT), ("err", HIT), ("t", "#3c5a69"),
                         ("srv", "#b8cdd6")):
            t.tag_config(tag, foreground=col)
        t.config(state="disabled")
        return t

    def toggle_packets(self):
        self.show_packets = not self.show_packets
        if self.show_packets:
            self.pkt_frame.pack(side="bottom", fill="x", padx=24, pady=(0, 12), before=self.content)
            self.btn_pkt.config(text="封包監看 ▾")
        else:
            self.pkt_frame.pack_forget()
            self.btn_pkt.config(text="封包監看 ▸")

    def clear(self):
        for w in self.content.winfo_children():
            w.destroy()
        self.screen = None
        self.views = {}

    def entry(self, parent, font=None, **kw):
        return tk.Entry(parent, font=font or self.f["mbig"], bg=CARD, fg=TEXT, insertbackground=ACCENT, relief="flat",
                        highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT, **kw)

    # ------------------------------------------------------------ 畫面 1：連線
    def show_connect(self):
        self.clear()
        self.screen = "connect"
        box = tk.Frame(self.content, bg=PANEL, padx=34, pady=26, highlightthickness=1, highlightbackground=LINE)
        box.place(relx=0.5, rely=0.47, anchor="center")
        tk.Label(box, text="連線到指揮中心", font=self.f["h1"], fg=TEXT, bg=PANEL).pack(anchor="w")
        nm = tk.Frame(box, bg=PANEL)
        nm.pack(fill="x", pady=(12, 16))
        tk.Label(nm, text="你的暱稱", font=self.f["sb"], fg=ACCENT, bg=PANEL).pack(side="left")
        self.e_name = self.entry(nm, width=16)
        self.e_name.insert(0, self.args.name or self.name or random_name())
        self.e_name.pack(side="left", padx=(12, 0), ipady=4)
        FlatButton(nm, "隨機", self.reroll_name, color=CARD, fg=TEXT, font=self.f["sb"], pady=6).pack(side="left", padx=6)
        tk.Label(nm, text="1~12 個字", font=self.f["s"], fg="#4d6b7a", bg=PANEL).pack(side="left", padx=4)

        cols = tk.Frame(box, bg=PANEL)
        cols.pack(fill="x")
        # 左：自動搜尋
        lf = tk.Frame(cols, bg=CARD, padx=16, pady=14, width=400, height=330)
        lf.pack(side="left", fill="y")
        lf.pack_propagate(False)
        hd = tk.Frame(lf, bg=CARD)
        hd.pack(fill="x")
        tk.Label(hd, text="① 區網中的 Server", font=self.f["h2"], fg=TEXT, bg=CARD).pack(side="left")
        self.scan_lbl = tk.Label(hd, text="", font=self.f["s"], fg=MUTED, bg=CARD)
        self.scan_lbl.pack(side="right")
        tk.Label(lf, text="UDP 廣播自動搜尋，點一下就連線，不用輸入 IP", font=self.f["s"], fg=MUTED, bg=CARD,
                 anchor="w").pack(fill="x", pady=(2, 8))
        self.found_box = tk.Frame(lf, bg=CARD)
        self.found_box.pack(fill="both", expand=True)
        # 右：當主機 / 手動
        rf = tk.Frame(cols, bg=PANEL, padx=18, width=330)
        rf.pack(side="left", fill="y")
        tk.Label(rf, text="② 我當主機", font=self.f["h2"], fg=TEXT, bg=PANEL, anchor="w").pack(fill="x")
        tk.Label(rf, text="在這台電腦直接開 Server，\n其他人用「自動搜尋」就會看到你", font=self.f["s"], fg=MUTED,
                 bg=PANEL, anchor="w", justify="left").pack(fill="x", pady=(2, 8))
        FlatButton(rf, "★ 開 Server 並進入", self.host_game, color=WARN, font=self.f["bb"], pady=9).pack(fill="x")
        tk.Frame(rf, bg=LINE, height=1).pack(fill="x", pady=16)
        tk.Label(rf, text="③ 手動輸入 IP", font=self.f["h2"], fg=TEXT, bg=PANEL, anchor="w").pack(fill="x")
        row = tk.Frame(rf, bg=PANEL)
        row.pack(fill="x", pady=(8, 8))
        self.e_host = self.entry(row, font=self.f["mb"], width=16)
        self.e_host.insert(0, self.args.host or "")
        self.e_host.pack(side="left", ipady=6, fill="x", expand=True)
        tk.Label(row, text=":", font=self.f["mb"], fg=MUTED, bg=PANEL).pack(side="left")
        self.e_port = self.entry(row, font=self.f["mb"], width=6)
        self.e_port.insert(0, str(self.args.port))
        self.e_port.pack(side="left", ipady=6)
        self.e_host.bind("<Return>", lambda e: self.manual_connect())
        FlatButton(rf, "連線", self.manual_connect, color="#7fd3ff", font=self.f["bb"], pady=8).pack(fill="x")
        self.conn_msg = tk.Label(box, text="", font=self.f["s"], fg=HIT, bg=PANEL, wraplength=700, justify="left", anchor="w")
        self.conn_msg.pack(fill="x", pady=(12, 0))
        self.render_found()
        self.scan()

    def scan(self):
        if self.screen != "connect" or self.scanning:
            return
        self.scanning = True
        self.scan_lbl.config(text="⟳ 搜尋中…", fg=ACCENT)

        def work():
            res = discover(1.2)
            self.inbox.put(("found", res))
        threading.Thread(target=work, daemon=True).start()

    def render_found(self):
        if self.screen != "connect":
            return
        for w in self.found_box.winfo_children():
            w.destroy()
        if not self.found:
            tk.Label(self.found_box, text="還沒找到 Server\n\n‧ 確認對方已開 Server、和你在同一個網路\n‧ 或自己當主機、改用手動輸入 IP",
                     font=self.f["s"], fg=MUTED, bg=CARD, justify="left", anchor="w").pack(fill="x", pady=10)
        for s in self.found[:5]:
            row = tk.Frame(self.found_box, bg=PANEL, padx=12, pady=8, cursor="hand2",
                           highlightthickness=1, highlightbackground=LINE)
            row.pack(fill="x", pady=3)
            a = tk.Label(row, text=f"◉ {s.get('name', '?')}", font=self.f["bb"], fg=ACCENT, bg=PANEL, anchor="w")
            a.pack(fill="x")
            b = tk.Label(row, text=f"{s['ip']}:{s['port']}　·　線上 {s.get('players', 0)} 人　·　{s.get('rooms', 0)} 個房間",
                         font=self.f["m"], fg=MUTED, bg=PANEL, anchor="w")
            b.pack(fill="x")
            for w in (row, a, b):
                w.bind("<Button-1>", lambda e, s=s: self.connect_to(s["ip"], s["port"]))

    def reroll_name(self):
        self.e_name.delete(0, "end")
        self.e_name.insert(0, random_name())

    # ---- 登入憑證：存在本機，程式當掉或被關掉後重開，用同暱稱就能回到原本的對局
    def load_tokens(self):
        try:
            with open(SESSION_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_token(self, name, token):
        d = self.load_tokens()
        d[name] = token
        try:
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
        except OSError:
            pass

    def get_name(self):
        n = self.e_name.get().strip() if self.screen == "connect" else (self.args.name or "")
        if not 1 <= len(n) <= 12:
            self.conn_msg.config(text="請先輸入暱稱（1~12 個字）", fg=HIT)
            return None
        return n

    def manual_connect(self):
        try:
            port = int(self.e_port.get())
            assert 1 <= port <= 65535
        except (ValueError, AssertionError):
            return self.conn_msg.config(text="Port 必須是 1~65535 的數字", fg=HIT)
        host = self.e_host.get().strip()
        if not host:
            return self.conn_msg.config(text="請輸入 Server IP", fg=HIT)
        self.connect_to(host, port)

    def host_game(self):
        if not self.get_name():
            return
        if not self.hosting:
            import server as srvmod
            try:
                port = int(self.e_port.get())
            except ValueError:
                port = DEFAULT_PORT
            h = QueueLogHandler(self.inbox)
            srvmod.setup_logging("battleship_server.log", handler=h)
            s = srvmod.Server(30, 30)
            try:
                s.start("0.0.0.0", port)
            except OSError as e:
                srvmod.log.removeHandler(h)
                return self.conn_msg.config(text=f"無法在 Port {port} 開 Server：{e}\n（可能已經有 Server 在跑了，直接用自動搜尋連它即可）", fg=HIT)
            self.hosting = s
            self.srv_wrap.pack(side="left", fill="both", expand=True, padx=(10, 0))
            ips = srvmod.local_ips()
            self.host_chip.config(text=f"★ 主機  {ips[0]}:{port}")
            self.host_chip.pack(side="left", padx=14, pady=(6, 0))
        self.connect_to("127.0.0.1", self.hosting.port)

    def connect_to(self, host, port):
        name = self.get_name() if self.screen == "connect" else self.args.name
        if not name:
            return
        self.name = name
        if self.screen == "connect":
            self.conn_msg.config(text=f"正在連線 {host}:{port} …", fg=MUTED)

        def work():
            ok, info = self.net.connect(host, port)
            self.inbox.put(("connected" if ok else "fail", (host, port, info)))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 畫面 2：大廳
    def show_lobby(self, lb):
        if self.screen != "lobby":
            self.clear()
            self.screen = "lobby"
            wrap = tk.Frame(self.content, bg=BG)
            wrap.pack(fill="both", expand=True, pady=10)
            hd = tk.Frame(wrap, bg=BG)
            hd.pack(fill="x")
            tk.Label(hd, text=f"艦長 {self.name}，歡迎登艦", font=self.f["h1"], fg=TEXT, bg=BG).pack(side="left")
            self.lb_info = tk.Label(hd, text="", font=self.f["b"], fg=MUTED, bg=BG)
            self.lb_info.pack(side="left", padx=16, pady=(6, 0))
            FlatButton(hd, "← 返回伺服器選擇", self.back_to_servers, color=CARD, fg=TEXT,
                       font=self.f["sb"], pady=7).pack(side="right")
            body = tk.Frame(wrap, bg=BG)
            body.pack(fill="both", expand=True, pady=14)

            left = tk.Frame(body, bg=BG, width=360)
            left.pack(side="left", fill="y")
            left.pack_propagate(False)

            def card(title, desc):
                c = tk.Frame(left, bg=PANEL, padx=20, pady=14, highlightthickness=1, highlightbackground=LINE)
                c.pack(fill="x", pady=(0, 12))
                tk.Label(c, text=title, font=self.f["h2"], fg=TEXT, bg=PANEL, anchor="w").pack(fill="x")
                tk.Label(c, text=desc, font=self.f["s"], fg=MUTED, bg=PANEL, anchor="w").pack(fill="x", pady=(2, 10))
                return c
            c = card("建立房間", f"最多 {MAX_PLAYERS} 人；滿 2 人房主就能直接開始")
            FlatButton(c, "建立房間", lambda: self.send({"cmd": "CREATE", "capacity": MAX_PLAYERS}),
                       color=WARN, font=self.f["bb"], pady=8).pack(fill="x")
            c = card("快速配對", "和其他正在配對的玩家 1 對 1")
            self.btn_match = FlatButton(c, "開始配對", lambda: None, font=self.f["bb"], pady=8)
            self.btn_match.pack(fill="x")
            c = card("用房號加入", "輸入朋友給你的 4 位數房號")
            row = tk.Frame(c, bg=PANEL)
            row.pack(fill="x")
            self.e_code = self.entry(row, width=6, justify="center")
            self.e_code.pack(side="left", ipady=3)
            FlatButton(row, "加入", lambda: self.send({"cmd": "JOIN", "code": self.e_code.get().strip()}),
                       color="#7fd3ff", font=self.f["sb"], pady=6).pack(side="left", padx=(8, 4), fill="x", expand=True)
            FlatButton(row, "觀戰", lambda: self.send({"cmd": "JOIN", "code": self.e_code.get().strip(), "spectate": True}),
                       color=CARD, fg=TEXT, font=self.f["sb"], pady=6).pack(side="left", fill="x", expand=True)

            right = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
            right.pack(side="left", fill="both", expand=True, padx=(16, 0))
            tk.Label(right, text="房間列表（即時更新）", font=self.f["h2"], fg=TEXT, bg=PANEL, anchor="w").pack(fill="x", padx=18, pady=(14, 8))
            self.room_box = tk.Frame(right, bg=PANEL)
            self.room_box.pack(fill="both", expand=True, padx=14)
        if lb.get("matching"):
            self.lb_info.config(text=f"⟳ 配對中，等待其他玩家…　線上 {lb['online']} 人", fg=ACCENT)
            self.btn_match.config(text="取消配對")
            self.btn_match.command = lambda: self.send({"cmd": "LEAVE"})
        else:
            self.lb_info.config(text=f"線上 {lb['online']} 人", fg=MUTED)
            self.btn_match.config(text="開始配對")
            self.btn_match.command = lambda: self.send({"cmd": "MATCH"})
        self.render_rooms(lb.get("rooms", []))

    def render_rooms(self, rooms):
        for w in self.room_box.winfo_children():
            w.destroy()
        if not rooms:
            tk.Label(self.room_box, text="目前沒有房間，建立一個吧！", font=self.f["b"], fg=MUTED, bg=PANEL).pack(pady=30)
            return
        for r in rooms[:9]:
            row = tk.Frame(self.room_box, bg=CARD, padx=14, pady=8)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=r["code"], font=self.f["mbig"], fg=ACCENT, bg=CARD).pack(side="left")
            info = tk.Frame(row, bg=CARD)
            info.pack(side="left", padx=14, fill="x", expand=True)
            col = {"WAITING": ACCENT, "PLACING": WARN, "BATTLE": HIT, "OVER": MUTED}[r["phase"]]
            tk.Label(info, text=f"{PHASE_ZH[r['phase']]}　{len(r['players'])}/{r['capacity']} 人"
                                f"{'　👁 ' + str(r['spectators']) if r['spectators'] else ''}",
                     font=self.f["bb"], fg=col, bg=CARD, anchor="w").pack(fill="x")
            tk.Label(info, text="、".join(r["players"]) or "—", font=self.f["s"], fg=MUTED, bg=CARD, anchor="w").pack(fill="x")
            FlatButton(row, "觀戰", lambda c=r["code"]: self.send({"cmd": "JOIN", "code": c, "spectate": True}),
                       color=PANEL, fg=TEXT, font=self.f["sb"], pady=6).pack(side="right", padx=(6, 0))
            if r["phase"] == "WAITING" and len(r["players"]) < r["capacity"]:
                FlatButton(row, "加入", lambda c=r["code"]: self.send({"cmd": "JOIN", "code": c}),
                           font=self.f["sb"], pady=6).pack(side="right")

    # ------------------------------------------------------------ 畫面 3：對戰／觀戰
    BIG_W, BIG_H = 560, 640
    FLIGHT = 0.6                  # 砲彈飛行秒數

    def build_game(self):
        """左：全員海域縮圖　中：目前檢視（攻擊目標）的大圖＋砲台　右：戰況與聊天"""
        st = self.st
        side_old = getattr(self, "side_pane", None)
        if self.show_side and side_old is not None:
            try:
                if side_old.winfo_exists() and side_old.winfo_width() > 50:
                    self.side_w = side_old.winfo_width()
            except tk.TclError:
                pass
        self.clear()
        self.screen = "game"
        self.layout_key = self.game_layout_key(st)
        self.ui_key = None
        self.big_static = None
        self.shake_off = (0, 0)
        self.hover_big = None
        self.tray_boxes = {}
        head = tk.Frame(self.content, bg=BG)
        head.pack(fill="x", pady=(2, 6))
        self.g_room = tk.Label(head, text="", font=self.f["mb"], fg=ACCENT, bg=ACCENT_DK, padx=10, pady=4)
        self.g_room.pack(side="left")
        self.g_spec = tk.Label(head, text="", font=self.f["s"], fg=MUTED, bg=BG)
        self.g_spec.pack(side="left", padx=10)
        self.btn_side = FlatButton(head, "", self.toggle_side, color=CARD, fg=TEXT, font=self.f["sb"], pady=5)
        self.btn_side.pack(side="left", padx=(4, 0))
        self.btn_side.config(text="◧ 戰況：開" if self.show_side else "◧ 戰況：關")
        self.g_cycle = tk.Label(head, text="", font=self.f["sb"], fg=WARN, bg=BG)
        self.g_cycle.pack(side="left", padx=12)
        self.g_timer = tk.Canvas(head, width=46, height=46, bg=BG, highlightthickness=0)
        self.g_timer.pack(side="right")
        self.g_status = tk.Label(head, text="", font=self.f["h1"], fg=TEXT, bg=BG)
        self.g_status.pack(side="right", padx=10)

        # 三欄用 PanedWindow：中間與戰況欄之間的分隔線可以拖曳調整寬度
        body = tk.PanedWindow(self.content, orient="horizontal", bg="#163545", sashwidth=7, bd=0,
                              sashrelief="flat", opaqueresize=True, sashcursor="sb_h_double_arrow")
        body.pack(fill="both", expand=True)
        self.body_pw = body
        left = tk.Frame(body, bg=BG, width=300)
        left.pack_propagate(False)
        body.add(left, minsize=300, width=300, stretch="never")
        tk.Label(left, text="全員海域　點縮圖切換大圖", font=self.f["sb"], fg=MUTED, bg=BG, anchor="w").pack(fill="x")
        tb = tk.Frame(left, bg=BG)
        tb.pack(fill="x", pady=(4, 6))
        self.thumbs = {}
        for i, b in enumerate(st["boards"]):
            nm = b["name"]
            f = tk.Frame(tb, bg=PANEL, highlightthickness=2, highlightbackground=LINE, cursor="hand2")
            f.grid(row=i // 2, column=i % 2, padx=(0, 6), pady=(0, 6))
            title = tk.Label(f, text=nm, font=self.f["sb"], fg=TEXT, bg=PANEL, anchor="w", width=16)
            title.pack(fill="x", padx=5, pady=(3, 0))
            cv = tk.Canvas(f, width=128, height=142, bg=PANEL, highlightthickness=0, cursor="hand2")
            cv.pack(padx=5, pady=(2, 5))
            for w in (f, title, cv):
                w.bind("<Button-1>", lambda e, n=nm: self.pick_view(n))
            self.thumbs[nm] = {"frame": f, "title": title, "cv": cv, "static": None, "hl": None, "tt": None}
        self.ctrl = tk.Frame(left, bg=BG)
        self.ctrl.pack(side="bottom", fill="x", pady=(0, 2))
        self.swatches = {}
        if st["role"] == "player":
            cr = tk.Frame(left, bg=BG)          # 選擇自己板子的顏色（房間內不能重複）
            cr.pack(side="bottom", fill="x", pady=(0, 4))
            self.color_lbl = tk.Label(cr, text="顏色", font=self.f["s"], fg=MUTED, bg=BG, width=5, anchor="w")
            self.color_lbl.pack(side="left")
            for c in PLAYER_COLORS:
                sw = tk.Canvas(cr, width=31, height=30, bg=BG, highlightthickness=0, cursor="hand2")
                sw.pack(side="left", padx=1)
                sw.bind("<Button-1>", lambda e, c=c: self.pick_color(c))
                self.swatches[c] = sw
            self.swatch_key = None
        eb = tk.Frame(left, bg=BG)              # 表情列放左欄，關掉戰況欄也能用
        eb.pack(side="bottom", fill="x", pady=(0, 2))
        tk.Label(eb, text="表情", font=self.f["s"], fg=MUTED, bg=BG, width=4, anchor="w").pack(side="left")
        er = eb
        for kind, label in EMOTES.items():
            c = tk.Canvas(er, width=33, height=36, bg=BG, highlightthickness=0, cursor="hand2")
            c.pack(side="left", padx=0)
            emote_face(Painter(c, "e"), kind, 16, 19, 12, font=self.f["sb"])
            c.bind("<Button-1>", lambda e, k=kind: self.send({"cmd": "EMOTE", "kind": k}))
            c.bind("<Enter>", lambda e, c=c: c.config(bg=CARD))
            c.bind("<Leave>", lambda e, c=c: c.config(bg=BG))

        center = tk.Frame(body, bg=BG)
        body.add(center, minsize=self.BIG_W + 20, stretch="always")
        self.big = tk.Canvas(center, width=self.BIG_W, height=self.BIG_H, bg=BG, highlightthickness=0)
        self.big.pack(anchor="n", padx=10)
        self.big.bind("<Motion>", lambda e: setattr(self, "hover_big", (e.x, e.y)))
        self.big.bind("<Leave>", lambda e: setattr(self, "hover_big", None))
        self.big.bind("<Button-1>", self.on_big_click)
        self.big.bind("<Button-3>", lambda e: self.rotate())
        self.big.bind("<Button-2>", lambda e: self.rotate())

        side = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        self.side_pane = side
        if self.show_side:
            body.add(side, minsize=180, width=self.side_w, stretch="never")
        tk.Label(side, text="戰況", font=self.f["h2"], fg=TEXT, bg=PANEL, anchor="w").pack(fill="x", padx=14, pady=(10, 2))
        self.logbox = tk.Text(side, bg=PANEL, fg=TEXT, font=self.f["b"], relief="flat", wrap="word",
                              highlightthickness=0, padx=14, pady=2, spacing1=2, spacing3=3, height=8, width=10)
        self.logbox.pack(fill="both", expand=True)
        for tag, col in (("t", "#3c5a69"), ("me", ACCENT), ("op", WARN), ("hit", HIT), ("muted", MUTED),
                         ("err", HIT), ("chat", "#7fd3ff"), ("sys", TEXT)):
            self.logbox.tag_config(tag, foreground=col)
        self.logbox.config(state="disabled")
        chat = tk.Frame(side, bg=PANEL)
        chat.pack(fill="x", padx=12, pady=(4, 12))
        self.e_chat = self.entry(chat, font=self.f["b"])
        self.e_chat.pack(side="left", fill="x", expand=True, ipady=5)
        self.e_chat.bind("<Return>", lambda e: self.chat())
        FlatButton(chat, "傳送", self.chat, color=CARD, fg=TEXT, font=self.f["sb"], pady=5).pack(side="left", padx=(6, 0))
        if self.log_room == st["code"]:          # 版面重建時保留戰況紀錄
            hist, self.log_hist = self.log_hist, []
            for ts, parts in hist:
                self.log(*parts, ts=ts)
        else:
            self.log_hist = []
        self.log_room = st["code"]
        for line in self.pending_log:
            self.log(*line)
        self.pending_log = []

    def game_layout_key(self, st):
        return (st["code"], st["role"], tuple(b["name"] for b in st["boards"]))

    # ---- 目前大圖要看誰
    def boards_map(self):
        return {b["name"]: b for b in self.st["boards"]}

    def alive_foes(self):
        st = self.st
        return [b["name"] for b in st["boards"] if b["name"] != st["you"] and b["alive"] and not b["left"]]

    def valid_target(self):
        foes = self.alive_foes()
        if self.target not in foes:
            self.target = foes[0] if foes else None
        return self.target

    def active_flight(self):
        now = time.time()
        live = [f for f in self.flights if now < f["t0"] + self.FLIGHT + 0.6]
        return live[-1] if live else None

    def current_view(self):
        st = self.st
        bm = self.boards_map()
        names = list(bm)
        me = st["you"]
        if self.manual_view in bm:
            return self.manual_view
        if st["role"] == "player" and st["phase"] == "BATTLE" and st.get("my_target") in bm:
            return st["my_target"]            # 玩家：大圖固定在自己唯一能打的目標，不會被別人的砲彈拉走
        fl = self.active_flight()
        if fl and fl["target"] in bm and st["phase"] in ("BATTLE", "OVER"):
            return fl["target"]               # 觀戰者／已淘汰：跟隨每一發砲彈
        if st["phase"] == "BATTLE" and st.get("turn_target") in bm:
            return st["turn_target"]          # 輪流攻擊：大圖顯示這回合被攻擊的人
        if st["role"] == "player":
            if st["phase"] == "OVER" and self.last_target in bm:
                return self.last_target
            return me
        if self.last_target in bm:
            return self.last_target
        return names[0]

    def pick_view(self, name):
        st = self.st
        if not st:
            return
        fl = self.active_flight()
        if fl and time.time() < fl["t0"] + self.FLIGHT:
            return                                  # 砲彈飛行中先不切換
        self.flights = [f for f in self.flights if time.time() >= f["t0"] + self.FLIGHT + 0.6]
        home = st.get("my_target") if st["role"] == "player" else st.get("turn_target")
        self.manual_view = None if name == home else name     # 點回自己的目標就恢復預設

    # ---- 控制區（左下）與縮圖標題，只在狀態變化時重建
    def update_game_widgets(self):
        st = self.st
        is_player = st["role"] == "player"
        me = st.get("me", {})
        view = self.current_view()
        self.g_room.config(text=f"房間 {st['code']}" + ("　觀戰中" if not is_player else ""))
        self.g_spec.config(text=f"👁 觀戰 {len(st['spectators'])} 人" if st["spectators"] else "")
        if self.swatches:
            owners = {b["color"]: b["name"] for b in st["boards"] if not b["left"] and b.get("color")}
            locked = st["phase"] in ("BATTLE", "OVER")
            sk = (tuple(sorted(owners.items())), locked)
            if sk != self.swatch_key:
                self.color_lbl.config(text="已鎖定" if locked else "顏色", fg=WARN if locked else MUTED)
                self.swatch_key = sk
                for c, sw in self.swatches.items():
                    sw.delete("all")
                    who = owners.get(c)
                    mine = who == st["you"]
                    if mine:
                        sw.create_oval(2, 1, 29, 28, outline="#ffffff", width=3)
                    sw.create_oval(6, 5, 25, 24, fill=c if (mine or not who) else blend(c, BG, 0.65), outline="")
                    if who and not mine:          # 別人在用：變暗並畫叉
                        sw.create_line(9, 8, 22, 21, fill=INK, width=2)
                        sw.create_line(9, 21, 22, 8, fill=INK, width=2)
                    if locked and not mine:
                        sw.delete("all")              # 開戰後鎖定：只留自己的顏色
                    sw.config(cursor="" if (who or locked) else "hand2")
        cyc = st.get("cycle") or []
        if cyc and len(cyc) > 2:
            bmap = self.boards_map()
            names = [("✕" + n) if (n in bmap and (not bmap[n]["alive"] or bmap[n]["left"])) else n for n in cyc]
            ctext = "抽籤：" + " → ".join(names + [names[0]])
        else:
            ctext = ""
        if self.g_cycle.cget("text") != ctext:
            self.g_cycle.config(text=ctext)
        for name, th in self.thumbs.items():
            b = self.boards_map().get(name)
            if not b:
                continue
            turn = st["phase"] == "BATTLE" and st.get("turn") == name
            targeted = st["phase"] == "BATTLE" and st.get("turn_target") == name
            hl = self.colors.get(name, LINE) if turn else (HIT if targeted else (TEXT if name == view else LINE))
            now_ = time.time()
            if any(f["target"] == name and f["result"] != "miss" and 0 <= now_ - f["t0"] - self.FLIGHT < 0.8
                   and int((now_ - f["t0"]) * 8) % 2 == 0 for f in self.flights):
                hl = FIRE_O                   # 被打中時縮圖外框閃爍
            if hl != th["hl"]:
                th["frame"].config(highlightbackground=hl)
                th["hl"] = hl
            tag = ""
            if st["phase"] in ("BATTLE", "OVER"):
                tag = f"　{b['ships_left']} 艘"
            if b["left"]:
                tag = "　離開"
            elif not b["alive"]:
                tag = "　淘汰"
            elif not b["online"]:
                tag = "　斷線"
            elif st["phase"] == "PLACING":
                tag = "　✓" if b["ready"] else "　佈陣中"
            tt = (("▶ " if turn else ("◎ " if targeted else "")) + name + ("（你）" if name == st["you"] else "") + tag)
            if tt != th["tt"]:
                th["title"].config(text=tt, fg=MUTED if (b["left"] or not b["alive"]) else self.colors.get(name, TEXT))
                th["tt"] = tt

        key = (st["phase"], me.get("ready"), me.get("alive"), tuple(st.get("rematch", [])),
               len(self.place) == len(FLEET), st.get("owner"), self.dir, self.cover,
               tuple((b["name"], b["online"], b["alive"], b["left"]) for b in st["boards"]))
        if key == self.ui_key:
            return
        self.ui_key = key
        for w in self.ctrl.winfo_children():
            w.destroy()

        def btn(parent, text, cmd, color=CARD, fg=TEXT, side="left", **kw):
            b = FlatButton(parent, text, cmd, color=color, fg=fg, font=self.f["sb"], pady=8, **kw)
            b.pack(side=side, expand=True, fill="x", padx=2)
            return b
        ph = st["phase"]
        if is_player and ph == "PLACING" and not me["ready"]:
            tk.Label(self.ctrl, text=f"點格子放船（{'橫' if self.dir == 'H' else '直'}）· 右鍵/R 旋轉 · 點上方船切換",
                     font=self.f["s"], fg=MUTED, bg=BG, justify="left", anchor="w").pack(fill="x", pady=(0, 4))
            r1 = tk.Frame(self.ctrl, bg=BG)
            r1.pack(fill="x", pady=2)
            btn(r1, "⟳ 旋轉", self.rotate)
            btn(r1, "隨機佈陣", self.random_place)
            btn(r1, "清除", self.reset_placement)
            r2 = tk.Frame(self.ctrl, bg=BG)
            r2.pack(fill="x", pady=2)
            b = btn(r2, "準備完成 ▶", self.submit_place, ACCENT, "#04110e")
            b.set_enabled(len(self.place) == len(FLEET))
        active = [b for b in st["boards"] if not b["left"]]
        if is_player and not (ph == "PLACING" and not me["ready"]):
            r0 = tk.Frame(self.ctrl, bg=BG)
            r0.pack(fill="x", pady=2)
            btn(r0, "◐ 顯示我的船" if self.cover else "◑ 遮住我的船（防偷看）", self.toggle_cover,
                WARN if self.cover else CARD, "#04110e" if self.cover else TEXT)
        if is_player and ph == "WAITING" and st["owner"] == st["you"] and len(active) < st["capacity"]:
            rb = tk.Frame(self.ctrl, bg=BG)
            rb.pack(fill="x", pady=2)
            btn(rb, f"＋ 加入機器人（{len(active)}/{st['capacity']}）", lambda: self.send({"cmd": "ADD_BOT"}), "#7fd3ff", "#04110e")
        row = tk.Frame(self.ctrl, bg=BG)
        row.pack(fill="x", pady=2)
        if is_player:
            if ph == "WAITING" and st["owner"] == st["you"] and len(active) >= 2:
                btn(row, f"開始遊戲（{len(active)} 人）", lambda: self.send({"cmd": "START"}), ACCENT, "#04110e")
            if ph == "OVER":
                if st["you"] in st.get("rematch", []):
                    btn(row, f"等待其他人（{len(st['rematch'])}/{len([b for b in active if b['online']])}）", lambda: None)
                else:
                    btn(row, "再來一局" + (f"（{len(st['rematch'])} 人同意）" if st.get("rematch") else ""),
                        lambda: self.send({"cmd": "REMATCH"}), ACCENT, "#04110e")
            if ph == "BATTLE" and me.get("alive"):
                btn(row, "投降", lambda: self.send({"cmd": "SURRENDER"}))
        btn(row, "離開房間", lambda: self.send({"cmd": "LEAVE"}))

    def pick_color(self, c):
        if self.st and self.st["phase"] in ("BATTLE", "OVER"):
            self.show_toast("顏色已鎖定", "遊戲開始後就不能更換顏色", WARN)
            return
        self.send({"cmd": "COLOR", "color": c})

    def toggle_side(self):
        """戰況欄開／關（關掉時記住目前寬度，打開時還原）"""
        side = self.side_pane
        if self.show_side:
            if side.winfo_width() > 50:
                self.side_w = side.winfo_width()
            self.body_pw.forget(side)
        else:
            self.body_pw.add(side, minsize=180, width=self.side_w, stretch="never")
        self.show_side = not self.show_side
        self.btn_side.config(text="◧ 戰況：開" if self.show_side else "◧ 戰況：關")

    def toggle_cover(self):
        self.cover = not self.cover

    # ---- 佈陣操作
    def select_ship(self, sid):
        if sid in self.place:
            self.dir = self.place.pop(sid)[2]
        self.sel = sid

    def rotate(self):
        self.dir = "V" if self.dir == "H" else "H"

    def random_place(self):
        self.place = {s["id"]: (s["x"], s["y"], s["dir"]) for s in random_fleet()}
        self.sel = None

    def submit_place(self):
        self.send({"cmd": "PLACE", "ships": [{"id": k, "x": x, "y": y, "dir": d} for k, (x, y, d) in self.place.items()]})

    def placed_cells(self):
        used = {}
        for sid, (x, y, d) in self.place.items():
            for c in ship_cells(x, y, FLEET[sid][1], d):
                used[c] = sid
        return used

    def preview(self, x, y):
        cells = ship_cells(x, y, FLEET[self.sel][1], self.dir)
        used = self.placed_cells()
        return cells, all(0 <= cx < SIZE and 0 <= cy < SIZE and (cx, cy) not in used for cx, cy in cells)

    def big_geo(self):
        return Geo((self.BIG_W - 42 * SIZE) / 2, 106, 42, gap=4)

    def on_big_click(self, e):
        st = self.st
        if not st:
            return
        ox, oy = self.shake_off
        px, py = e.x - ox, e.y - oy
        view = self.current_view()
        c = self.big_geo().cell_at(px, py)
        if st["role"] == "player" and st["phase"] == "PLACING" and not st["me"]["ready"] and view == st["you"]:
            for sid, (x1, y1, x2, y2) in self.tray_boxes.items():
                if x1 - 3 <= px <= x2 + 3 and y1 - 6 <= py <= y2 + 6:
                    self.select_ship(sid)
                    return
            if not c:
                return
            used = self.placed_cells()
            if self.sel:
                cells, ok = self.preview(*c)
                if ok:
                    self.place[self.sel] = (c[0], c[1], self.dir)
                    rest = [s for s in FLEET if s not in self.place]
                    self.sel = rest[0] if rest else None
                elif c in used:
                    self.select_ship(used[c])
            elif c in used:
                self.select_ship(used[c])
        elif c and st["phase"] == "BATTLE" and st["role"] == "player" and view != st["you"]:
            # 不在本地擋「沒輪到你」或「重複」，交給 Server 判定，影片才看得到 Server 的驗證
            self.send({"cmd": "FIRE", "target": view, "x": c[0], "y": c[1]})

    def chat(self):
        t = self.e_chat.get().strip()
        if t:
            self.send({"cmd": "CHAT", "text": t})
            self.e_chat.delete(0, "end")

    # ------------------------------------------------------------ 繪製
    def visible_board(self, name, now):
        """砲彈還在空中時，先藏起這一發的結果（落地才揭曉）"""
        b = self.boards_map()[name]
        flying = [f for f in self.flights if f["target"] == name and now < f["t0"] + self.FLIGHT]
        hide = {(f["x"], f["y"]) for f in flying}
        pend_sunk = set()
        for f in flying:
            if f["result"] == "sunk" and f.get("cells"):
                pend_sunk.update(tuple(c) for c in f["cells"])
        inc = {}
        for x, y, v in b["incoming"]:
            if (x, y) in hide:
                continue
            inc[(x, y)] = "hit" if (v == "sunk" and (x, y) in pend_sunk) else v
        sunk = [s for s in b["sunk"] if not (set(tuple(c) for c in s["cells"]) & pend_sunk)]
        return b, inc, sunk, pend_sunk

    def draw_game(self):
        st = self.st
        now = time.time()
        self.colors = {b["name"]: b.get("color") or PLAYER_COLORS[i % len(PLAYER_COLORS)] for i, b in enumerate(st["boards"])}
        for name in self.thumbs:
            if name in self.boards_map():
                self.draw_thumb(name, now)
        self.draw_big(now)

    def draw_thumb(self, name, now):
        th = self.thumbs[name]
        cv = th["cv"]
        color = self.colors[name]
        geo = Geo(4, 4, 12, gap=1)
        if th["static"] != color:
            cv.delete("all")
            S = Painter(cv, "static")
            for x in range(SIZE):
                for y in range(SIZE):
                    S.rect(*geo.box(x, y), fill=blend(color, BG, 0.62 if (x + y) % 2 else 0.68), outline="")
            th["static"] = color
        cv.delete("dyn")
        P = Painter(cv, "dyn")
        st = self.st
        b, inc, sunk, pend = self.visible_board(name, now)
        light = blend(color, "#ffffff", 0.35)
        mine = name == st["you"] and st["role"] == "player"
        if mine:
            if st["phase"] == "PLACING" and not st["me"]["ready"]:
                cells = list(self.placed_cells())
            elif self.cover:
                cells = []
            else:
                cells = [tuple(c) for s in st["me"]["ships"] for c in s["cells"]]
            for c in cells:
                P.rect(*geo.box(*c), fill=light, outline="")
        # 縮圖下方：這位玩家的剩餘艦艇
        if mine:
            sunk_ids = {s_["id"] for s_ in st["me"]["ships"] if s_["sunk"]} - {
                s_["id"] for s_ in st["me"]["ships"] if set(tuple(c) for c in s_["cells"]) & pend}
        else:
            sunk_ids = {s_["id"] for s_ in sunk}
        tray_ships(P, 6, 129, sunk_ids, color, cs=6, gap=3, dim=not b["alive"] or b["left"])
        if st["phase"] == "OVER":
            for s in b.get("reveal", []):
                for c in s["cells"]:
                    P.rect(*geo.box(*c), fill=light, outline="", stipple="gray50")
        for s in sunk:
            for c in s["cells"]:
                P.rect(*geo.box(*c), fill=CHAR, outline="")
        for (x, y), v in inc.items():
            x1, y1, x2, y2 = geo.box(x, y)
            if v == "miss":
                P.line(x1 + 3, y1 + 3, x2 - 3, y2 - 3, fill=INK, width=2)
                P.line(x1 + 3, y2 - 3, x2 - 3, y1 + 3, fill=INK, width=2)
            else:
                P.rect(x1 + 2, y1 + 2, x2 - 2, y2 - 2, fill=FIRE_O if v == "hit" else "#b02c3e", outline="")
        msg = None
        if b["left"]:
            msg = "離開"
        elif not b["alive"]:
            msg = "淘汰"
        elif not b["online"]:
            msg = "斷線"
        elif st["phase"] == "PLACING" and not (name == st["you"] and not b["ready"]):
            msg = "✓ 就緒" if b["ready"] else "佈陣中"
        if msg:
            P.rect(4, 4, 124, 124, fill=BG, outline="", stipple="gray50")
            P.text(64, 64, text=msg, fill=TEXT if msg != "淘汰" else HIT, font=self.f["bb"])
        for f in self.flights:            # 縮圖上的落點效果（被打時看得到）
            dt = now - f["t0"] - self.FLIGHT
            if f["target"] == name and 0 <= dt < 0.9:
                cx, cy = geo.center(f["x"], f["y"])
                rr = 4 + 16 * dt / 0.9
                P.oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=FIRE_O if f["result"] != "miss" else "#ffffff", width=3)
        for frm, kind, t0 in self.emotes:
            dt = now - t0
            if frm == name and dt < 2.4:
                k = min(1.0, dt / 0.15) * (1 + 0.18 * math.sin(dt * 12) * math.exp(-dt * 3))
                emote_face(P, kind, 64, 62 - dt * 6, 34 * k, font=self.f["sb"])

    def draw_big(self, now):
        st = self.st
        cv = self.big
        view = self.current_view()
        bm = self.boards_map()
        if view not in bm:
            return
        color = self.colors[view]
        geo = self.big_geo()
        me = st["you"]
        is_me = view == me and st["role"] == "player"
        x0, y0, size = geo.x0, geo.y0, geo.size
        skey = (view, color)
        if self.big_static != skey:
            cv.delete("all")
            S = Painter(cv, "static")
            S.rrect(x0 - 24, y0 - 24, x0 + size + 24, y0 + size + 24, 22, fill=blend(color, BG, 0.45), outline="")
            for i in range(SIZE):
                S.text(x0 + (i + 0.5) * geo.cs, y0 - 12, text=COLS[i], fill=blend(color, "#ffffff", 0.55), font=self.f["sb"])
                S.text(x0 - 12, y0 + (i + 0.5) * geo.cs, text=str(i + 1), fill=blend(color, "#ffffff", 0.55), font=self.f["sb"])
            for x in range(SIZE):
                for y in range(SIZE):
                    S.rrect(*geo.box(x, y), 6, fill=blend(color, BG, 0.74 if (x + y) % 2 else 0.7), outline="")
            self.big_static = skey
            self.shake_off = (0, 0)
        else:
            ox, oy = self.shake_off
            if ox or oy:
                cv.move("static", -ox, -oy)
            cv.delete("dyn")
        P = Painter(cv, "dyn")
        b, inc, sunk, pend = self.visible_board(view, now)
        ph = st["phase"]
        placing = st["role"] == "player" and ph == "PLACING" and not st["me"]["ready"] and is_me

        # 標題
        turn = st.get("turn")
        if placing:
            title = "佈陣：把 5 艘船放進你的海域"
        elif is_me:
            title = "你的海域" + (f"　·　{turn} 正在攻擊你！" if ph == "BATTLE" and st.get("turn_target") == me else
                                  (f"　·　{turn} 的回合" if ph == "BATTLE" and turn != me else ""))
        else:
            tt = st.get("turn_target")
            if ph == "BATTLE" and turn == me and st["role"] == "player" and view == tt:
                title = f"攻擊目標：{view}　·　點格子開火！"
            elif ph == "BATTLE" and turn == me and st["role"] == "player":
                title = f"{view} 的海域　·　你的目標是 {tt}（點它的縮圖）"
            elif ph == "BATTLE" and st["role"] == "player" and view == st.get("my_target"):
                who = "你" if tt == me else tt
                title = f"你的目標：{view}　·　{turn} 正在攻擊 {who}"
            elif ph == "BATTLE" and view == tt:
                title = f"{turn} → 攻擊 {view}"
            else:
                title = f"{view} 的海域"
        fl0 = self.active_flight()
        if fl0 and fl0["target"] == view and now < fl0["t0"] + self.FLIGHT + 0.4:
            who = "你" if fl0["shooter"] == me else fl0["shooter"]
            title = f"{who} → 攻擊 {'你' if view == me else view}"      # 砲彈飛行中：標題顯示這一發是誰打誰
        P.text(self.BIG_W / 2, 14, text=title, fill=TEXT, font=self.f["h2"])

        # 艦艇列
        if is_me:
            sunk_ids = {s["id"] for s in st["me"]["ships"] if s["sunk"]} if not placing else set()
            sunk_ids -= {s["id"] for s in st["me"]["ships"] if set(tuple(c) for c in s["cells"]) & pend}
        else:
            sunk_ids = {s["id"] for s in sunk}
        TS = 21
        tw = tray_width(TS, 10)
        tx0 = (self.BIG_W - tw) / 2
        if placing:
            boxes = {}
            x = tx0
            for sid, (name, sz) in FLEET.items():
                g = Geo(x, 30, TS, gap=0)
                on, done = sid == self.sel, sid in self.place
                if on:
                    P.rrect(x - 6, 26, x + sz * TS + 6, 30 + TS + 4, 8, fill=blend(color, BG, 0.2), outline="")
                ship_shape(P, g, sid, [(i, 0) for i in range(sz)],
                           blend(color, BG, 0.55) if done and not on else color, INK, width=2)
                boxes[sid] = (x, 30, x + sz * TS, 30 + TS)
                x += sz * TS + 10
            self.tray_boxes = boxes
            P.text(self.BIG_W / 2, 64, text=f"{FLEET[self.sel][0]}（{FLEET[self.sel][1]} 格）· {'橫放' if self.dir == 'H' else '直放'}"
                   if self.sel else "全部放好了！按左下「準備完成」", fill=MUTED, font=self.f["s"])
        else:
            self.tray_boxes = {}
            tray_ships(P, tx0, 30, sunk_ids, color, cs=TS, gap=10, dim=not b["alive"] or b["left"])

        # 被打過的格子：整格變色
        for (x, y), v in inc.items():
            if v == "sunk":
                continue                    # 擊沉：直接畫出整艘船，不加底色
            fill = blend(blend(color, BG, 0.7), "#ffffff", 0.25) if v == "miss" else "#15171c"
            P.rrect(*geo.box(x, y), 6, fill=fill, outline="")
        sunk_fill = blend(color, "#1a1d23", 0.5)

        # 船
        light = blend(color, "#ffffff", 0.25)
        if placing:
            for sid, (x, y, d) in self.place.items():
                ship_shape(P, geo, sid, ship_cells(x, y, FLEET[sid][1], d), light)
            h = geo.cell_at(*self.hover_big) if self.hover_big else None
            if h and self.sel:
                cells, ok = self.preview(*h)
                cells = [c for c in cells if 0 <= c[0] < SIZE and 0 <= c[1] < SIZE]
                if cells:
                    ship_shape(P, geo, self.sel, cells, "#ffffff" if ok else HIT, INK, stipple="gray50")
                    for c in cells:
                        P.rrect(*geo.box(*c), 6, fill="", outline="#ffffff" if ok else HIT, width=2)
        elif is_me:
            for s in st["me"]["ships"]:
                dead = s["sunk"] and not (set(tuple(c) for c in s["cells"]) & pend)
                if self.cover and not dead:
                    continue                  # 遮住模式：沒沉的船不畫（被擊沉的本來就是公開資訊）
                ship_shape(P, geo, s["id"], s["cells"], sunk_fill if dead else light)
            if self.cover and ph != "WAITING":
                P.rrect(x0 + size / 2 - 120, y0 + size + 4, x0 + size / 2 + 120, y0 + size + 22, 8, fill=WARN, outline="")
                P.text(x0 + size / 2, y0 + size + 13, text="已遮住你的船（左下按鈕可顯示）", fill=INK, font=self.f["sb"])
        else:
            if ph == "OVER":
                sunk_set = {s["id"] for s in sunk}
                for s in b.get("reveal", []):
                    if s["id"] not in sunk_set:
                        ship_shape(P, geo, s["id"], s["cells"], light, INK, stipple="gray50")
            for s in sunk:
                ship_shape(P, geo, s["id"], s["cells"], sunk_fill)

        # 記號：沒中 = X；命中 = 火焰；擊沉 = 冒煙
        ink = blend(color, INK, 0.75)
        for (x, y), v in inc.items():
            if v == "miss":
                miss_mark(P, geo, x, y, ink)
            elif v == "hit":                # 只有「打中但還沒沉」的格子有火焰
                fire(P, geo, x, y, self.anim * 3, seed=x * 7 + y)

        # 瞄準準星
        my_turn = (st["role"] == "player" and ph == "BATTLE" and turn == me and st["me"]["alive"]
                   and not st.get("paused") and not self.reconnecting)
        h = geo.cell_at(*self.hover_big) if self.hover_big else None
        aim_ok = my_turn and not is_me and b["alive"] and not b["left"] and view == st.get("turn_target")
        if h and ph == "BATTLE" and st["role"] == "player" and not is_me and b["alive"] and not b["left"]:
            cx, cy = geo.center(*h)
            done = h in inc
            mc = self.colors.get(me, ACCENT)
            col = HIT if done else (mc if my_turn else MUTED)
            r = geo.cs * 0.42
            P.oval(cx - r, cy - r, cx + r, cy + r, outline=col, width=3)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                P.line(cx + dx * r * 0.45, cy + dy * r * 0.45, cx + dx * r * 1.25, cy + dy * r * 1.25, fill=col, width=3)
            lab = f"{coord(*h)} 已打過" if done else coord(*h)
            P.text(cx, cy - r - 14, text=lab, fill=col, font=self.f["mb"])

        # 遮罩
        msg = None
        if ph == "WAITING":
            act = [x for x in st["boards"] if not x["left"]]
            msg, mcol = f"等待玩家　{len(act)}/{st['capacity']}", MUTED
        elif ph == "PLACING" and not is_me:
            msg, mcol = ("✓ 已就緒" if b["ready"] else "佈陣中…"), TEXT
        elif ph == "PLACING" and is_me and st["me"]["ready"]:
            msg, mcol = "✓ 已就緒，等待其他人", ACCENT
        elif b["left"]:
            msg, mcol = "已離開", MUTED
        elif not b["alive"] and ph == "BATTLE":
            msg, mcol = "已淘汰", HIT
        elif not b["online"] and ph in ("PLACING", "BATTLE"):
            msg, mcol = "斷線中…", WARN
        if msg:
            P.rect(x0, y0, x0 + size, y0 + size, fill=BG, outline="", stipple="gray50")
            P.rrect(self.BIG_W / 2 - 130, y0 + size / 2 - 30, self.BIG_W / 2 + 130, y0 + size / 2 + 30, 14, fill=PANEL, outline=mcol, width=2)
            P.text(self.BIG_W / 2, y0 + size / 2, text=msg, fill=mcol, font=self.f["h2"])

        # 砲台與砲彈
        fl = self.active_flight()
        shooter = None
        if fl and fl["target"] == view and now < fl["t0"] + self.FLIGHT + 0.35:
            shooter = fl["shooter"]
        elif ph == "BATTLE" and st["role"] == "player" and view == st.get("my_target"):
            shooter = me                      # 我的目標海域下方永遠是我的砲台
        elif ph == "BATTLE" and view == st.get("turn_target"):
            shooter = turn
        ccx, ccy = self.BIG_W / 2 + 50, self.BIG_H - 50
        if shooter and shooter != view and shooter in self.colors:
            if fl and fl["target"] == view and now < fl["t0"] + self.FLIGHT + 0.35:
                ax, ay = geo.center(fl["x"], fl["y"])
                dt = now - fl["t0"]
                recoil = max(0.0, 1 - dt / 0.25) if dt < 0.25 else 0.0
                flash = max(0.0, 1 - dt / 0.12)
            elif aim_ok and h and shooter == me:
                ax, ay = geo.center(*h)
                recoil = flash = 0.0
            else:
                a = -math.pi / 2 + 0.35 * math.sin(self.anim * 0.9)
                ax, ay = ccx + math.cos(a) * 100, ccy + math.sin(a) * 100
                recoil = flash = 0.0
            ang = math.atan2(ay - ccy, ax - ccx)
            mx, my = cannon(P, ccx, ccy, ang, self.colors[shooter], recoil=recoil, flash=flash)
            P.text(ccx + 48, ccy + 22, text=f"{shooter}{'（你）' if shooter == me else ''} 的砲台",
                   fill=self.colors[shooter], font=self.f["sb"], anchor="w")
            for f in self.flights:
                t = (now - f["t0"]) / self.FLIGHT
                if f["target"] == view and 0 <= t < 1:
                    ex, ey = geo.center(f["x"], f["y"])
                    shell(P, mx, my, ex, ey, t)

        # 落點特效＋畫面震動
        shake = 0.0
        for f in self.flights:
            if f["target"] != view:
                continue
            dt = now - f["t0"] - self.FLIGHT
            if 0 <= dt < 0.8:
                cx, cy = geo.center(f["x"], f["y"])
                if f["result"] == "miss":
                    splash(P, cx, cy, dt / 0.8, geo.cs, blend(color, BG, 0.5))
                else:
                    boom(P, cx, cy, dt / 0.8, geo.cs, big=f["result"] == "sunk")
                    if dt < 0.3:
                        shake = max(shake, (10 if f["result"] == "sunk" else 6) * (1 - dt / 0.3))

        # 左下角：其他玩家的剩餘艦艇
        others = [x for x in st["boards"] if x["name"] != view]
        zy = y0 + size + 34
        P.text(8, zy, text="其他艦隊剩餘", fill=MUTED, font=self.f["s"], anchor="w")
        for i, ob in enumerate(others[:3]):
            yy = zy + 12 + i * 19
            _, _, osunk, opend = self.visible_board(ob["name"], now)
            if ob["name"] == me and st["role"] == "player":
                ids = {s_["id"] for s_ in st["me"]["ships"] if s_["sunk"]} - {
                    s_["id"] for s_ in st["me"]["ships"] if set(tuple(c) for c in s_["cells"]) & opend}
            else:
                ids = {s_["id"] for s_ in osunk}
            oc = self.colors.get(ob["name"], TEXT)
            label = "你" if ob["name"] == me else ob["name"]
            P.text(8, yy + 5, text=label[:7], fill=MUTED if (not ob["alive"] or ob["left"]) else oc,
                   font=self.f["sb"], anchor="w")
            tray_ships(P, 96, yy, ids, oc, cs=10, gap=3, dim=not ob["alive"] or ob["left"])

        # 表情：大圖右側浮出
        live = [e for e in self.emotes if now - e[2] < 2.4][-4:]
        for i, (frm, kind, t0) in enumerate(live):
            dt = now - t0
            k = min(1.0, dt / 0.15) * (1 + 0.18 * math.sin(dt * 12) * math.exp(-dt * 3))
            ex, ey = self.BIG_W - 40, y0 + 40 + i * 86 - dt * 8
            emote_face(P, kind, ex, ey, 28 * k, font=self.f["bb"])
            P.text(ex, ey + 40, text=frm[:6], fill=self.colors.get(frm, MUTED), font=self.f["sb"])

        # 結束時的大橫幅
        if ph == "OVER" and self.over_at and now > self.over_at + self.FLIGHT:
            win = st.get("winner") == me and st["role"] == "player"
            bc = ACCENT if win else (HIT if st["role"] == "player" else WARN)
            txt = "勝利！" if win else f"{st.get('winner') or '無人'} 獲勝"
            P.rrect(self.BIG_W / 2 - 170, y0 + size / 2 - 44, self.BIG_W / 2 + 170, y0 + size / 2 + 44, 18,
                    fill=bc, outline=INK, width=4)
            P.text(self.BIG_W / 2, y0 + size / 2 - 8, text=txt, fill=INK, font=self.f["h1"])
            P.text(self.BIG_W / 2, y0 + size / 2 + 22, text=st.get("reason", ""), fill=INK, font=self.f["s"])

        if shake:
            ox, oy = random.uniform(-shake, shake), random.uniform(-shake, shake)
            cv.move("all", ox, oy)
            self.shake_off = (ox, oy)
        else:
            self.shake_off = (0, 0)

    def draw_header(self):
        st = self.st
        ph = st["phase"]
        cv = self.g_timer
        cv.delete("all")
        spectator = st["role"] == "spectator"
        active = [b for b in st["boards"] if not b["left"]]
        if ph == "WAITING":
            txt = f"等待玩家 {len(active)}/{st['capacity']}"
            if st["owner"] == st["you"] and len(active) >= 2:
                txt += "　（可按「開始遊戲」）"
            col = MUTED
        elif ph == "PLACING":
            if spectator:
                txt, col = "玩家佈陣中…", MUTED
            elif not st["me"]["ready"]:
                txt, col = "佈陣階段：放好 5 艘船", TEXT
            else:
                txt, col = f"已就緒，等待其他人（{sum(b['ready'] for b in active)}/{len(active)}）", MUTED
        elif ph == "BATTLE":
            turn = st.get("turn")
            if self.reconnecting:
                txt, col = "連線中斷，重新連線中…", HIT
            elif st.get("paused"):
                txt, col = "有玩家斷線，等待重連…", WARN
            elif spectator:
                txt, col = f"{turn} → {st.get('turn_target')}", self.colors.get(turn, TEXT)
            elif not st["me"]["alive"]:
                txt, col = f"你已淘汰，觀戰中（{turn} 的回合）", MUTED
            elif turn == st["you"]:
                txt = "命中！再打一次" if time.time() < self.again_until else f"輪到你開火！目標：{st.get('turn_target')}"
                col = ACCENT
            elif st.get("turn_target") == st["you"]:
                txt, col = f"{turn} 正在瞄準你！", HIT
            else:
                txt, col = f"{turn} → {st.get('turn_target')}", self.colors.get(turn, MUTED)
            rem = st.get("turn_remain")
            if rem is not None and not self.reconnecting:
                rem = max(0.0, rem - (time.time() - self.st_at))
                frac = rem / max(1, self.turn_secs)
                c = ACCENT if frac > 0.33 else HIT
                cv.create_oval(4, 4, 42, 42, outline=LINE, width=5)
                cv.create_arc(4, 4, 42, 42, start=90, extent=359.9 * frac, style="arc", outline=c, width=5)
                cv.create_text(23, 23, text=str(int(math.ceil(rem))), fill=TEXT, font=self.f["mb"])
        else:
            if st.get("winner") == st["you"]:
                txt, col = "勝利！", ACCENT
            elif spectator or not st.get("winner"):
                txt, col = f"結束：{st.get('winner') or '無人'} 獲勝", ACCENT
            else:
                txt, col = f"落敗　{st.get('winner')} 獲勝", HIT
        self.g_status.config(text=txt, fg=col)

    # ------------------------------------------------------------ 訊息處理
    def pump(self):
        try:
            while True:
                kind, data = self.inbox.get_nowait()
                self.handle(kind, data)
        except queue.Empty:
            pass
        self.anim += 0.05
        if self.screen == "game" and self.st:
            self.update_game_widgets()
            self.draw_header()
            self.draw_game()
        self.update_conn()
        self.root.after(50, self.pump)

    def handle(self, kind, data):
        if kind == "srvlog":
            self.write(self.srvlog, "srv", data)
        elif kind == "found":
            self.scanning = False
            self.found = data
            if self.screen == "connect":
                self.scan_lbl.config(text=f"找到 {len(data)} 台", fg=ACCENT if data else MUTED)
                self.render_found()
                self.root.after(2500, self.scan)
        elif kind == "retry":
            if self.reconnecting:
                self.banner_lbl.config(text=f"與 Server 斷線，自動重新連線中… {data}")
            elif self.screen == "connect":
                self.conn_msg.config(text=data, fg=HIT)
        elif kind == "fail":
            host, port, info = data
            if self.reconnecting:
                self.root.after(2500, self.try_reconnect)
            elif self.screen == "connect":
                self.conn_msg.config(text=f"無法連線 {host}:{port}　{info}\n請確認同一網路、防火牆已開放 TCP {port}", fg=HIT)
        elif kind == "connected":
            self.host, self.port, self.local = data[0], data[1], data[2]
            msg = {"cmd": "LOGIN", "name": self.name}
            tok = self.token or self.load_tokens().get(self.name)
            if tok:
                msg["token"] = tok         # 帶著憑證：Server 會把我接回原本的對局（或取代失效的舊連線）
            self.last_rx = time.time()
            self.send(msg)
        elif kind == "closed":
            self.on_closed()
        elif kind == "raw":
            self.log_pkt("in", data)
        elif kind == "msg":
            self.last_rx = time.time()
            self.on_msg(data)

    def send(self, msg):
        if msg.get("cmd") != "PING":
            self.log_pkt("out", json.dumps(msg, ensure_ascii=False))
        if self.reconnecting and msg.get("cmd") != "LOGIN":
            self.show_toast("尚未連線", "等重新連線後再操作", WARN)
            return False
        return self.net.send(msg)

    def on_msg(self, m):
        ev, cmd = m.get("event"), m.get("cmd")
        if cmd == "PING":
            if self.ping_t:
                self.rtt = (time.time() - self.ping_t) * 1000
            return
        if ev == "STATE":
            self.log_pkt("push", f'{{"event":"STATE","phase":"{m["phase"]}","turn":{json.dumps(m.get("turn"), ensure_ascii=False)}, …棋盤資料}}')
        elif ev == "LOBBY":
            self.log_pkt("push", f'{{"event":"LOBBY","online":{m["online"]},"rooms":[…{len(m["rooms"])} 間]}}')
        else:
            self.log_pkt("push" if ev else ("in" if m.get("ok") else "err"), json.dumps(m, ensure_ascii=False))

        if cmd == "LOGIN":
            if m.get("ok"):
                self.token = m["token"]
                self.save_token(self.name, self.token)
                self.turn_secs = m.get("turn_secs", 30)
                if m.get("resumed") and not self.reconnecting:
                    self.show_toast("歡迎回來", "已用上次的登入憑證回到原本的狀態", ACCENT)
                if self.reconnecting:
                    self.reconnecting = False
                    self.banner.pack_forget()
                    self.show_toast("已重新連線", "對局狀態已由 Server 恢復" if m.get("resumed") else "回到大廳", ACCENT)
                    self.log("sys", "已重新連線" + ("，回到對局" if m.get("resumed") else ""))
            else:
                if self.reconnecting:
                    self.net.close()
                    self.root.after(2500, self.try_reconnect)
                elif self.screen == "connect":
                    extra = ""
                    if m.get("code") == "E_NAME_TAKEN":
                        new = random_name()
                        self.e_name.delete(0, "end")
                        self.e_name.insert(0, new)
                        extra = f"\n已幫你換成「{new}」，再點一次 Server 或按連線即可"
                    self.conn_msg.config(text=f"[{m.get('code')}] {m.get('msg')}{extra}", fg=HIT)
                    self.net.close()
            return
        if ev == "LOBBY":
            self.st = None
            self.reset_placement()
            self.show_lobby(m)
            return
        if ev == "STATE":
            prev = self.st
            self.st, self.st_at = m, time.time()
            self.colors = {b["name"]: b.get("color") or PLAYER_COLORS[i % len(PLAYER_COLORS)] for i, b in enumerate(m["boards"])}
            if prev and prev["phase"] != m["phase"]:
                self.manual_view = None
                if m["phase"] != "OVER":
                    self.over_at = 0.0
            if (m["phase"] == "BATTLE" and m.get("turn") == m["you"]
                    and (not prev or prev.get("turn") != m["you"])):
                self.manual_view = None       # 輪到我時自動回到我的目標
            if self.screen != "game" or self.layout_key != self.game_layout_key(m):
                if self.screen != "game":
                    self.pending_log = [("sys", f"進入房間 {m['code']}" + ("（觀戰）" if m["role"] == "spectator" else ""))]
                else:
                    self.pending_log = []
                self.build_game()
            if m["phase"] == "PLACING" and (not prev or prev["phase"] != "PLACING"):
                self.reset_placement()
            if (m["role"] == "player" and m["phase"] == "BATTLE" and m.get("turn") == m["you"]
                    and (not prev or prev.get("turn") != m["you"])):
                self.bell()
            return
        if ev:
            self.on_event(m)
            return
        if m.get("ok") is False:
            self.show_toast("操作被拒絕", f"{m.get('msg')}  [{m.get('code')}]", HIT)
            self.log("err", f"✕ {m.get('msg')}  [{m.get('code')}]")
            if self.screen == "connect":
                self.conn_msg.config(text=m.get("msg"), fg=HIT)

    def on_event(self, m):
        ev = m["event"]
        me = self.name
        if ev == "SHOT":
            mine, hit_me = m["by"] == me, m["target"] == me
            res = {"miss": "未命中", "hit": "命中！", "sunk": f"擊沉 {m.get('ship')}！"}[m["result"]]
            who = "你" if mine else m["by"]
            tgt = "你" if hit_me else m["target"]
            # 先播砲彈動畫，落地後才在戰況顯示結果
            self.flights = [f for f in self.flights if time.time() < f["t0"] + 3] + [
                {"shooter": m["by"], "target": m["target"], "x": m["x"], "y": m["y"], "result": m["result"],
                 "cells": m.get("cells"), "t0": time.time()}]
            if not (self.st and self.st["role"] == "player" and self.st.get("my_target")):
                self.manual_view = None
            self.last_target = m["target"]

            def land():
                self.log(self.pc(m["by"]), f"{who} ", "muted", "→ ", self.pc(m["target"]), f"{tgt} ", "muted", f"{m['at']}  ",
                         "hit" if m["result"] != "miss" else "muted", res)
                if m["result"] == "sunk" and (mine or hit_me):
                    self.show_toast("擊沉！" if mine else "我方艦艇被擊沉", f"{m['target']} 的 {m.get('ship')} 沉沒了",
                                    ACCENT if mine else HIT)
            self.root.after(int(self.FLIGHT * 1000), land)
        elif ev == "AGAIN":
            if m["player"] == me:
                self.again_until = time.time() + 2.5
                self.log("me", "命中！可以繼續開火")
            else:
                self.log(self.pc(m["player"]), m["player"], "muted", " 命中，繼續開火")
        elif ev == "DRAW":
            self.log("sys", m["msg"])
            self.show_toast("開局抽籤", m["msg"] + "\n每人只能攻擊箭頭指向的下一位", WARN)
        elif ev == "EMOTE":
            self.emotes = [e for e in self.emotes if time.time() - e[2] < 3] + [(m["from"], m["kind"], time.time())]
            self.log(self.pc(m["from"]), f"{m['from']}：[{EMOTES.get(m['kind'], m['kind'])}]")
        elif ev == "CHAT":
            tag = self.pc(m["from"]) if m.get("role") == "player" else "muted"
            self.log(tag, f"{m['from']}{'（觀戰）' if m.get('role') == 'spectator' else ''}：{m['text']}")
        elif ev == "GAME_OVER":
            self.over_at = time.time()
            win = m["winner"] == me

            def done():          # 等最後一發砲彈落地再宣布
                self.log("me" if win else "err", f"★ {m['winner']} 獲勝", "muted", f"  {m['reason']}")
                self.show_toast("勝利！" if win else "對局結束", f"{m['winner']} 獲勝：{m['reason']}", ACCENT if win else HIT)
                self.bell()
            self.root.after(int(self.FLIGHT * 1000) + 80, done)
        elif ev == "ELIMINATED":
            def out():
                self.log("err", f"✕ {m['msg']}")
                if m["player"] == me:
                    self.show_toast("你被淘汰了", m["msg"], HIT)
            self.root.after(int(self.FLIGHT * 1000) + 40, out)
        else:
            text = m.get("msg") or ""
            if ev == "MATCHED":
                text = f"開始佈陣：{'、'.join(m['players'])}"
            col = {"BATTLE_START": ACCENT, "TIMEOUT": WARN, "OFFLINE": WARN, "BACK": ACCENT, "LEFT": WARN,
                   "REMATCH": ACCENT, "MATCHED": ACCENT, "SPECTATOR": MUTED}.get(ev, TEXT)
            self.log("sys", text)
            if ev != "SPECTATOR":
                self.show_toast({"BATTLE_START": "開戰", "TIMEOUT": "回合逾時", "OFFLINE": "玩家斷線", "BACK": "玩家回來了",
                                 "LEFT": "有人離開", "REMATCH": "再來一局", "MATCHED": "人數到齊"}.get(ev, ev), text, col)

    def bell(self):
        try:
            self.root.bell()
        except tk.TclError:
            pass

    # ------------------------------------------------------------ 斷線 / 重連 / 心跳
    def on_closed(self):
        if not self.token:
            if self.screen == "connect":
                self.conn_msg.config(text="連線被 Server 關閉", fg=HIT)
            return
        if self.reconnecting:
            return
        self.reconnecting = True
        self.net.close()
        self.banner_lbl.config(text="與 Server 斷線，自動重新連線中…（對局中斷線，Server 會保留你的位置 30 秒）")
        self.banner.pack(fill="x", padx=24, after=self.top)
        self.log("err", "與 Server 的 TCP 連線中斷，正在重新連線…")
        self.root.after(1200, self.try_reconnect)

    def try_reconnect(self):
        if not self.reconnecting:
            return

        def work():
            ok, info = self.net.connect(self.host, self.port, attempts=1)
            self.inbox.put(("connected" if ok else "fail", (self.host, self.port, info)))
        threading.Thread(target=work, daemon=True).start()

    def heartbeat(self):
        if self.net.connected and not self.reconnecting and self.token:
            if time.time() - self.last_rx > DEAD_AFTER - 5:
                # 很久沒收到 Server 任何資料（連 PING 回應都沒有）：網路可能已斷，主動重連
                self.log("err", "超過 15 秒沒收到 Server 回應，判定連線中斷")
                self.net.close()
                self.on_closed()
            else:
                self.ping_t = time.time()
                self.net.send({"cmd": "PING"})
        self.root.after(HEARTBEAT * 1000, self.heartbeat)

    def back_to_servers(self):
        """離開這台 Server，回到第一個畫面重新選擇（大廳裡沒有對局，Server 會立刻釋放暱稱）"""
        self.send({"cmd": "LEAVE"}) if self.st else None
        self.abandon()
        if self.hosting:
            self.conn_msg.config(text=f"你的 Server 仍在背景執行（Port {self.hosting.port}），其他玩家不受影響；"
                                      "按「開 Server 並進入」可以回到自己的 Server", fg=MUTED)

    def abandon(self):
        """放棄重連，回到首頁（憑證仍保存在本機，之後用同暱稱連回同一台 Server 還能回到對局）"""
        self.reconnecting = False
        self.net.close()
        self.banner.pack_forget()
        self.st = None
        self.token = None
        self.host = None
        self.conn_dot.config(fg=MUTED)
        self.conn_lbl.config(text="尚未連線", fg=MUTED)
        self.show_connect()

    def update_conn(self):
        if not self.host or self.screen == "connect":
            return
        if self.reconnecting or not self.net.connected:
            self.conn_dot.config(fg=HIT)
            self.conn_lbl.config(text=f"已斷線  Server {self.host}:{self.port}", fg=HIT)
            return
        rtt = f"{self.rtt:.0f} ms" if self.rtt is not None else "— ms"
        lo = f"{self.local[0]}:{self.local[1]}" if self.local else ""
        self.conn_dot.config(fg=ACCENT if int(time.time() * 2) % 2 else ACCENT_DK)
        self.conn_lbl.config(text=f"TCP  Server {self.host}:{self.port}   RTT {rtt}\n     本機   {lo}", fg=TEXT)

    # ------------------------------------------------------------ 紀錄 / 提示
    pending_log = []
    log_hist = []
    log_room = None

    def pc(self, name):
        """這位玩家顏色的文字標籤（戰況、聊天都用和板子一樣的顏色）"""
        c = self.colors.get(name)
        return f"pc_{c[1:]}" if c else "chat"

    def log(self, *parts, ts=None):
        if self.screen != "game":
            return
        for i in range(0, len(parts), 2):
            if parts[i].startswith("pc_"):
                self.logbox.tag_config(parts[i], foreground="#" + parts[i][3:])
        ts = ts or time.strftime("%H:%M:%S  ")
        self.log_hist.append((ts, parts))
        self.logbox.config(state="normal")
        self.logbox.insert("end", ts, "t")
        for i in range(0, len(parts), 2):
            self.logbox.insert("end", parts[i + 1], parts[i])
        self.logbox.insert("end", "\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def write(self, widget, tag, text, prefix=""):
        widget.config(state="normal")
        widget.insert("end", text + "\n", tag)
        n = int(widget.index("end-1c").split(".")[0])
        if n > 400:
            widget.delete("1.0", f"{n - 400}.0")
        widget.see("end")
        widget.config(state="disabled")

    def log_pkt(self, tag, text):
        text = re.sub(r'"token": "[0-9a-f]+"', '"token": "••••••"', text)    # 影片中不露出憑證
        self.pkt.config(state="normal")
        self.pkt.insert("end", time.strftime("%H:%M:%S "), "t")
        self.pkt.config(state="disabled")
        self.write(self.pkt, tag, ("→ " if tag == "out" else "← ") + text[:230])

    def show_toast(self, title, text, color):
        self.toast.config(highlightbackground=color)
        self.toast_t.config(text=title, fg=color)
        self.toast_m.config(text=text)
        self.toast.place(relx=1.0, x=-28, y=124, anchor="ne", width=380)
        self.toast.lift()
        if self.toast_after:
            self.root.after_cancel(self.toast_after)
        self.toast_after = self.root.after(3000, self.toast.place_forget)


def main():
    ap = argparse.ArgumentParser(description="海戰棋 GUI Client")
    ap.add_argument("--host", default="")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--name", default="")
    args = ap.parse_args()
    root = tk.Tk()
    App(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
