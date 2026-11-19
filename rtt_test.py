"""RTT 量測工具：連到 Server，送 N 次 PING，統計來回時間（Round-Trip Time）

  python rtt_test.py                         自動搜尋區網中的 Server
  python rtt_test.py --host 192.168.43.20    指定 Server IP
  python rtt_test.py --host 127.0.0.1        同一台電腦（對照組）

量測方式：記錄送出 PING 的時間，收到 Server 回應的時間相減，即為一次 RTT。
另外記錄 TCP 三向交握（connect）花費的時間。結果可直接填入計畫書的效能比較表。
"""
import argparse
import json
import random
import socket
import statistics
import sys
import time

from protocol import DEFAULT_PORT, LineReader, discover, send_msg


def main():
    ap = argparse.ArgumentParser(description="海戰棋 RTT 量測")
    ap.add_argument("--host", default="auto")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("-n", "--count", type=int, default=20)
    a = ap.parse_args()
    if a.host == "auto":
        found = discover()
        if not found:
            print("找不到 Server，請用 --host 指定 IP")
            sys.exit(1)
        a.host, a.port = found[0]["ip"], found[0]["port"]

    t0 = time.perf_counter()
    sock = socket.create_connection((a.host, a.port), timeout=5)       # 含 TCP 三向交握
    handshake = (time.perf_counter() - t0) * 1000
    sock.settimeout(5)
    reader = LineReader(sock)
    pending = []

    def recv_one():
        while not pending:
            lines = reader.read_lines()
            if lines is None:
                raise ConnectionError("Server 關閉連線")
            pending.extend(json.loads(x) for x in lines)
        return pending.pop(0)

    send_msg(sock, {"cmd": "LOGIN", "name": f"RTT測試{random.randint(100, 999)}"})
    while recv_one().get("cmd") != "LOGIN":
        pass
    local = sock.getsockname()
    print(f"Server {a.host}:{a.port}　本機 {local[0]}:{local[1]}")
    print(f"TCP 三向交握（connect）：{handshake:.2f} ms")

    rtts = []
    for i in range(a.count):
        t = time.perf_counter()
        send_msg(sock, {"cmd": "PING"})
        while True:
            m = recv_one()
            if m.get("cmd") == "PING":
                break
        rtts.append((time.perf_counter() - t) * 1000)
        print(f"  第 {i + 1:2d} 次 RTT = {rtts[-1]:.2f} ms")
        time.sleep(0.1)
    sock.close()
    print(f"\n共 {len(rtts)} 次　最小 {min(rtts):.2f} ms　平均 {statistics.mean(rtts):.2f} ms　"
          f"最大 {max(rtts):.2f} ms　標準差 {statistics.pstdev(rtts):.2f} ms")


if __name__ == "__main__":
    main()
