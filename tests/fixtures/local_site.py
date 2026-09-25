"""A tiny local website for integration tests: deterministic, offline, no third-party server.

Pages: /login (+ /secure), /table?page=N (3 pages), /table-endless, /download (+ file),
/upload, /form, /delayed, /slow-table?page=N (3 pages that stream in slowly).
"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

USERNAME = "tomsmith"
PASSWORD = "SuperSecretPassword!"

EMPLOYEES = [
    ("1", "Ann", "Sales"),
    ("2", "Bob", "IT"),
    ("3", "Cy", "HR"),
    ("4", "Di", "IT"),
    ("5", "Ed", "Sales"),
    ("6", "Flo", "HR"),
]
PAGE_SIZE = 2
LAST_PAGE = len(EMPLOYEES) // PAGE_SIZE
CSV_BODY = "id,name\n1,Ann\n2,Bob\n"
SLOW_LAST_PAGE = 3
SLOW_ROWS = 4
SLOW_PAUSE_SECONDS = 0.7


def _html(title: str, body: str) -> str:
    return f"<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>"


def _login_page(error: str = "") -> str:
    flash = f'<div id="flash" class="error">{error}</div>' if error else ""
    return _html(
        "Login",
        f"""{flash}
        <form method="post" action="/login">
          <input id="username" name="username">
          <input id="password" name="password" type="password">
          <button id="submit" type="submit">Login</button>
        </form>""",
    )


def _table_page(page: int) -> str:
    rows = EMPLOYEES[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    body_rows = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in rows)
    if page < LAST_PAGE:
        nxt = f'<a id="next" href="/table?page={page + 1}">Next</a>'
    else:
        nxt = '<button id="next" disabled>Next</button>'
    return _html(
        f"Employees {page}",
        f"""<table id="employees">
          <thead><tr><th>ID</th><th>Name</th><th>Dept</th></tr></thead>
          <tbody>{body_rows}</tbody>
        </table>{nxt}""",
    )


ENDLESS_PAGE = _html(
    "Endless",
    """<table id="employees"><tr><th>id</th></tr><tr><td>x</td></tr></table>
    <a id="next" href="/table-endless">Next</a>""",
)

DOWNLOAD_PAGE = _html("Download", '<a id="export" href="/files/report.csv">Export</a>')

UPLOAD_PAGE = _html(
    "Upload",
    """<form method="post" action="/upload" enctype="multipart/form-data">
      <input id="file" name="file" type="file" multiple>
      <button id="submit-upload" type="submit">Upload</button>
    </form>""",
)

FORM_PAGE = _html(
    "Form",
    """<input id="name">
    <select id="country"><option value="">--</option><option value="IN">India</option>
      <option value="US">USA</option></select>
    <input id="agree" type="checkbox">
    <div id="hidden-note" style="display:none">secret</div>
    <div id="chart" style="width:120px;height:60px;background:#39c">Chart</div>""",
)

DELAYED_PAGE = _html(
    "Delayed",
    """<script>setTimeout(() => {
      const el = document.createElement('div'); el.id = 'late'; el.textContent = 'ready';
      document.body.appendChild(el);
    }, 300);</script>""",
)

SECURE_PAGE = _html("Secure", '<h1 id="dashboard">Secure Area</h1>')


def _uploaded_page(files: list[tuple[str, str]]) -> str:
    items = "".join(f"<li>{name}: {content}</li>" for name, content in files)
    return _html("Uploaded", f'<ul id="uploaded">{items}</ul>')


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # keep test output quiet
        pass

    def _send(
        self,
        body: str,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _stream_slow_table(self, page: int) -> None:
        """A table that arrives in two chunks, like a slow real site.

        The first chunk holds the header and the first row; the rest (and the Next link)
        follows after a pause. A reader that looks at the page too early sees a half-parsed
        table with no Next link and wrongly concludes it is the last page.
        """
        rows = [(str(page * 10 + i), f"Row {page}.{i}") for i in range(1, SLOW_ROWS + 1)]

        def cells(row: tuple[str, str]) -> str:
            return f"<tr><td>{row[0]}</td><td>{row[1]}</td></tr>"

        if page < SLOW_LAST_PAGE:
            nxt = f'<a id="next" href="/slow-table?page={page + 1}">Next</a>'
        else:
            nxt = '<button id="next" disabled>Next</button>'
        first = (
            "<!doctype html><html><head><title>Slow</title></head><body>"
            '<table id="slow"><thead><tr><th>ID</th><th>Name</th></tr></thead><tbody>'
            + cells(rows[0])
        )
        rest = "".join(cells(row) for row in rows[1:]) + f"</tbody></table>{nxt}</body></html>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()  # no Content-Length: the connection closing ends the body
        self.wfile.write(first.encode("utf-8"))
        self.wfile.flush()
        time.sleep(SLOW_PAUSE_SECONDS)
        self.wfile.write(rest.encode("utf-8"))

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self._send(_login_page())
        elif parsed.path == "/secure":
            self._send(SECURE_PAGE)
        elif parsed.path == "/table":
            page = int(parse_qs(parsed.query).get("page", ["1"])[0])
            self._send(_table_page(min(max(page, 1), LAST_PAGE)))
        elif parsed.path == "/table-endless":
            self._send(ENDLESS_PAGE)
        elif parsed.path == "/download":
            self._send(DOWNLOAD_PAGE)
        elif parsed.path == "/files/report.csv":
            self._send(
                CSV_BODY,
                content_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="report.csv"'},
            )
        elif parsed.path == "/upload":
            self._send(UPLOAD_PAGE)
        elif parsed.path == "/form":
            self._send(FORM_PAGE)
        elif parsed.path == "/delayed":
            self._send(DELAYED_PAGE)
        elif parsed.path == "/slow-table":
            page = int(parse_qs(parsed.query).get("page", ["1"])[0])
            self._stream_slow_table(min(max(page, 1), SLOW_LAST_PAGE))
        else:
            self._send(_html("Not found", "<h1>404</h1>"), status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path == "/login":
            form = parse_qs(body.decode("utf-8"))
            if form.get("username") == [USERNAME] and form.get("password") == [PASSWORD]:
                self._redirect("/secure")
            else:
                self._send(_login_page("Your username or password is invalid!"))
        elif self.path == "/upload":
            content_type = self.headers.get("Content-Type", "")
            raw = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
            message = BytesParser(policy=policy.default).parsebytes(raw)
            files = []
            for part in message.iter_parts():
                filename = part.get_filename()
                if filename:
                    payload = bytes(part.get_payload(decode=True) or b"")
                    files.append((filename, payload.decode("utf-8", "replace")))
            self._send(_uploaded_page(files))
        else:
            self._send(_html("Not found", "<h1>404</h1>"), status=404)


@contextmanager
def run_local_site() -> Iterator[str]:
    """Serve the site on a free localhost port; yields the base URL (no trailing slash)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
