"""Expose the local `tastydb dashboard` to other machines on the network.

`tastydb dashboard` binds to 127.0.0.1:8787 by default, so only this machine
can reach it. This script is a plain TCP relay: it listens on every interface
(0.0.0.0) on port 8787 and pipes each connection, byte for byte, to the
dashboard at 127.0.0.1:8787. It doesn't parse HTTP, so everything the
dashboard serves passes through unchanged.

Usage (two terminals):

    .venv/bin/tastydb dashboard        # 1. start the dashboard (127.0.0.1:8787)
    python3 forward.py                 # 2. start the relay; Ctrl-C to stop

Then browse to http://<this-machine's-LAN-IP>:8787 from another device. On
startup the relay prints that URL, using the address of the interface that
carries the default route. If that guess is wrong (VPN, several network
adapters), find the LAN IP (usually 192.168.x.x, 10.x.x.x or 172.16-31.x.x;
not 127.0.0.1) by hand:

    # macOS: en0 is usually Wi-Fi on laptops, Ethernet on desktops; if it
    # prints nothing, try en1, or ask which interface the default route uses:
    ipconfig getifaddr en0
    route -n get default | grep interface

    # Linux:
    hostname -I                  # first address listed
    ip route get 1.1.1.1         # the address after "src"

    # Windows:
    ipconfig                     # "IPv4 Address" under the active adapter

Or on macOS, open System Settings -> Wi-Fi (or Network) -> Details next to the
connected network. The address can change when DHCP renews. If a bookmark
stops working, check the address again, or set a DHCP reservation on the
router.

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


# Best guess at this machine's LAN IP: "connecting" a UDP socket sends no
# packets, but makes the OS pick the outgoing interface for that destination,
# whose address getsockname() then reports. Returns None when offline.
def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(('192.0.2.1', 80))  # TEST-NET-1: never actually contacted
            return s.getsockname()[0]
        except OSError:
            return None

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
    ip = lan_ip()
    if ip:
        print(f"From another device, browse to http://{ip}:{LISTEN_PORT}")
    else:
        print("Couldn't determine the LAN IP; see the docstring to find it by hand")
    while True:
        client_sock, addr = listener.accept()
        threading.Thread(target=handle_client, args=(client_sock,), daemon=True).start()

if __name__ == '__main__':
    main()
