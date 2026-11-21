"""Open a recorded replay (logs/replay-*.jsonl) in the official 3D viewer.

    python3 scripts/view.py                      # newest replay
    python3 scripts/view.py logs/replay-XXXX.jsonl

Serves logs/ on http://localhost:8765 with CORS, then prints the viewer URL
(https://play.joinbuilders.org/?replay=http://localhost:8765/<file>). Browsers allow an https page
to fetch from http://localhost. Ctrl-C to stop.
"""
import glob, http.server, os, socketserver, sys, urllib.parse, webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
PORT = 8765


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=LOGS, **k)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204); self.end_headers()

    def log_message(self, *a):
        pass


path = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(os.path.join(LOGS, "replay-*.jsonl")))[-1]
name = os.path.basename(path)
url = f"https://play.joinbuilders.org/?replay={urllib.parse.quote(f'http://localhost:{PORT}/{name}', safe='')}"
print(f"viewer: {url}", flush=True)
try:
    webbrowser.open(url)
except Exception:
    pass
with socketserver.TCPServer(("127.0.0.1", PORT), H) as srv:
    srv.serve_forever()
