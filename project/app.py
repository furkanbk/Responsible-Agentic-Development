"""project.app — the HTTP surface. Run with: python -m project.app"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

from project import config
from project import store

PAGE = """<!doctype html>
<html>
<head>
  <title>{title}</title>
  <style>
    body {{ font-family: sans-serif; margin: 3rem auto; max-width: 30rem; }}
    h1 {{ text-align: center; }}
    li {{ padding: 0.2rem 0; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <ul>{items}</ul>
  <form method="post" action="/add">
    <input name="title" placeholder="new task" autofocus>
    <button type="submit">add</button>
  </form>
</body>
</html>
"""


def render():
    """The whole page, as HTML."""
    items = "".join(f"<li>{t['id']}. {t['title']}</li>" for t in store.list_tasks())
    return PAGE.format(title=config.TITLE, items=items or "<li><i>nothing yet</i></li>")


class Handler(BaseHTTPRequestHandler):

    def _send(self, status, body, content_type):
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, status, body):
        self._send(status, body, "text/html; charset=utf-8")

    def _json(self, status, payload):
        self._send(status, json.dumps(payload), "application/json")

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_GET(self):
        if self.path == "/":
            self._html(200, render())
        elif self.path == "/tasks":
            self._json(200, {"tasks": store.list_tasks()})
        else:
            self._html(404, "<h1>not found</h1>")

    def do_POST(self):
        if self.path == "/add":
            form = parse_qs(self._body().decode())
            store.add_task(form.get("title", [""])[0])
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        elif self.path == "/tasks":
            body = json.loads(self._body() or b"{}")
            try:
                self._json(201, {"task": store.add_task(body.get("title", ""))})
            except ValueError as exc:
                self._json(409, {"error": str(exc)})
        else:
            self._html(404, "<h1>not found</h1>")


def run():
    print(f"serving on http://{config.HOST}:{config.PORT}")
    HTTPServer((config.HOST, config.PORT), Handler).serve_forever()


if __name__ == "__main__":
    run()
