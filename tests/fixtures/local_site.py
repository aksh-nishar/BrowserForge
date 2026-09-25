"""A tiny local website for integration tests: deterministic, offline, no third-party server.

Pages: /login (+ /secure), /table?page=N (3 pages), /table-endless, /download (+ file),
/upload, /form, /delayed, /slow-table?page=N (3 pages that stream in slowly), /register
(a full registration form that records what it receives; read back at /submissions),
/register-late (form built by JavaScript), /register-captcha (CAPTCHA widget and an iframe).
"""

import json
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

REGISTER_PAGE = _html(
    "Register",
    """<form method="post" action="/register" enctype="multipart/form-data">
      <label for="first_name">First name *</label>
      <input id="first_name" name="first_name" required>
      <label for="email">E-mail</label>
      <input id="email" name="email" type="email">
      <label for="dob">Date of birth</label>
      <input id="dob" name="dob" type="date">
      <label for="country">Country</label>
      <select id="country" name="country">
        <option value="">--</option><option value="IN">India</option>
        <option value="US">USA</option>
      </select>
      <fieldset><legend>Plan</legend>
        <label><input type="radio" name="plan" value="basic"> Basic</label>
        <label><input type="radio" name="plan" value="pro"> Pro</label>
      </fieldset>
      <label for="notes">Notes</label>
      <textarea id="notes" name="notes"></textarea>
      <label for="cv">CV</label>
      <input id="cv" name="cv" type="file">
      <label>
        <input id="agree" name="agree" type="checkbox" value="yes"> I agree to the terms
      </label>
      <input type="hidden" name="token" value="abc123">
      <input id="locked" name="locked" disabled>
      <input placeholder="Nickname">
      <button type="submit" id="send">Register</button>
    </form>""",
)

# A form that JavaScript builds after the page has loaded.
REGISTER_LATE_PAGE = _html(
    "Late form",
    """<div id="mount"></div>
    <script>setTimeout(() => {
      document.getElementById('mount').innerHTML =
        '<form method="post" action="/register"><label for="late_name">Late name</label>' +
        '<input id="late_name" name="late_name">' +
        '<button id="late_send" type="submit">Go</button></form>';
    }, 400);</script>""",
)

# A page with things BrowserForge cannot handle: a CAPTCHA widget and an embedded frame.
REGISTER_CAPTCHA_PAGE = _html(
    "Captcha form",
    """<div class="g-recaptcha" data-sitekey="test"></div>
    <iframe src="/form" width="120" height="60"></iframe>
    <input id="q" name="q">""",
)

# What the /register form received, oldest first. Cleared each time the site starts.
SUBMISSIONS: list[dict[str, str]] = []

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


def _parse_form(content_type: str, body: bytes) -> dict[str, str]:
    """Fields of a multipart or urlencoded form post. Uploaded files show as `[file name: text]`."""
    if "multipart" not in content_type:
        return {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
    raw = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    message = BytesParser(policy=policy.default).parsebytes(raw)
    fields: dict[str, str] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        payload = bytes(part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        filename = part.get_filename()
        if filename is None:
            fields[name] = payload
        elif filename:  # an empty file input arrives with an empty name: ignore it
            fields[name] = f"[file {filename}: {payload}]"
    return fields


def _thanks_page(fields: dict[str, str]) -> str:
    items = "".join(f"<li>{key}={value}</li>" for key, value in fields.items())
    who = fields.get("first_name") or fields.get("late_name") or "there"
    return _html("Thanks", f'<div id="thanks">Thanks, {who}</div><ul id="received">{items}</ul>')


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
        elif parsed.path == "/register":
            self._send(REGISTER_PAGE)
        elif parsed.path == "/register-late":
            self._send(REGISTER_LATE_PAGE)
        elif parsed.path == "/register-captcha":
            self._send(REGISTER_CAPTCHA_PAGE)
        elif parsed.path == "/submissions":
            self._send(json.dumps(SUBMISSIONS), content_type="application/json")
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
        elif self.path == "/register":
            fields = _parse_form(self.headers.get("Content-Type", ""), body)
            SUBMISSIONS.append(fields)
            self._send(_thanks_page(fields))
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
    SUBMISSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
