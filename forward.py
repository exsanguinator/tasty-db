"""Expose the local `tastydb dashboard` to other machines on the network.

`tastydb dashboard` binds to 127.0.0.1:8787 by default, so only this machine
can reach it. This script is a plain TCP relay: it listens on every interface
(0.0.0.0) on port 8787 and pipes each connection, byte for byte, to the
dashboard at 127.0.0.1:8787. It doesn't parse HTTP, so everything the
dashboard serves passes through unchanged.

Usage (two terminals):

    .venv/bin/tastydb dashboard        # 1. start the dashboard (127.0.0.1:8787)
    python3 forward.py                 # 2. start the relay; Ctrl-C to stop

Then browse to http://<this-machine's-LAN-IP>:8787 from another device.

Notes:
- Listening and target ports are both 8787. That works on macOS because the
  more specific 127.0.0.1 binding (the dashboard) wins for loopback traffic,
  while the 0.0.0.0 binding (this relay) gets everything else. On Linux the
  second bind usually fails with "Address already in use". In that case,
  change LISTEN_PORT to something else, e.g. 8788.
- The dashboard has no authentication, and POST /marks/refresh is reachable
  through the relay. Only run this on a network you trust.
- Needs only the standard library. It doesn't import tastydb.
"""
import socket
import threading

LISTEN_HOST = '0.0.0.0'    # all interfaces: reachable from the LAN
LISTEN_PORT = 8787
TARGET_HOST = '127.0.0.1'  # where `tastydb dashboard` listens by default
TARGET_PORT = 8787


# Copy bytes one way until either side closes, then close both sockets so the
# thread running the opposite direction also ends.
def forward(src, dst):
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        src.close()
        dst.close()

# Each incoming connection gets its own connection to the dashboard and two
# pump threads, one for each direction.
def handle_client(client_sock):
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.connect((TARGET_HOST, TARGET_PORT))
    threading.Thread(target=forward, args=(client_sock, server_sock), daemon=True).start()
    threading.Thread(target=forward, args=(server_sock, client_sock), daemon=True).start()

# Accept connections forever. Threads are daemons, so Ctrl-C exits right away.
def main():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((LISTEN_HOST, LISTEN_PORT))
    listener.listen(5)
    print(f"Forwarding 0.0.0.0:{LISTEN_PORT} -> {TARGET_HOST}:{TARGET_PORT}")
    while True:
        client_sock, addr = listener.accept()
        threading.Thread(target=handle_client, args=(client_sock,), daemon=True).start()

if __name__ == '__main__':
    main()
