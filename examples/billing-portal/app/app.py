"""Billing Portal: a deliberately small demo app for RestoreProof.

It stores invoices in PostgreSQL and the invoice PDFs on disk. That split is the
point of the demo: a database backup can restore cleanly while the files it
points at never made it into the uploads backup.
"""

import html
import json
import os
import posixpath
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "")
UPLOAD_DIR = os.path.realpath(os.environ.get("UPLOAD_DIR", "/data/uploads"))
PORT = int(os.environ.get("PORT", "8080"))


def db():
    return psycopg.connect(DATABASE_URL, connect_timeout=3)


# The customer's own product, in its own plain look: nothing here is RestoreProof or Hermes branding.
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · Billing Portal</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f4f5f7;color:#111827;font:15px/1.5 Inter,-apple-system,"Segoe UI",Roboto,sans-serif}}
nav{{display:flex;justify-content:space-between;align-items:center;padding:16px 24px;background:#fff;
  border-bottom:1px solid #e5e7eb}}
nav b{{font-size:15px;letter-spacing:-.01em}}nav a,nav span{{color:#6b7280;text-decoration:none;font-size:14px}}
main{{width:calc(100% - 32px);max-width:880px;margin:32px auto;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden}}
main.note{{max-width:560px;padding:28px}}
.head{{padding:24px 24px 12px}}h1{{margin:0;font-size:22px;font-weight:600;line-height:1.3}}
p{{margin:6px 0 0}}.dim,.k,dt{{color:#6b7280}}.k,dt{{font-size:12px;margin:0}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th{{text-align:left;font-size:12px;font-weight:500;color:#6b7280;background:#f9fafb;padding:10px 24px;
  border-bottom:1px solid #e5e7eb}}
td{{padding:11px 24px;border-bottom:1px solid #f0f1f3}}tr:last-child td{{border-bottom:0}}
td a{{color:#111827;font-weight:500;text-decoration:none}}td a:hover{{text-decoration:underline}}
.r{{text-align:right}}
.row{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}}
.amt{{font-size:22px;font-weight:600;margin:14px 0 0;font-variant-numeric:tabular-nums}}
dl{{display:grid;grid-template-columns:1fr 1fr;gap:12px 24px;margin:20px 0 24px}}dd{{margin:2px 0 0}}
.btn{{display:inline-flex;align-items:center;min-height:44px;padding:0 16px;border-radius:6px;background:#111827;
  color:#fff;text-decoration:none;font-weight:500}}
.code{{font:12px/1.5 ui-monospace,Consolas,monospace;color:#6b7280;margin-top:14px}}
@media (max-width:600px){{main{{width:100%;margin:0;border-radius:0;border-width:0 0 1px}}th,td{{padding:10px 14px}}
  td:nth-child(3),th:nth-child(3){{display:none}}}}
</style></head><body>{body}</body></html>"""


def nav(label, href=None):
    right = f"<a href='{href}'>{html.escape(label)}</a>" if href else f"<span>{html.escape(label)}</span>"
    return f"<nav><b>Billing Portal</b>{right}</nav>"


def money(cents):
    return f"${cents / 100:,.2f}"


def day(d):
    return f"{d.day} {d:%b %Y}"


def safe_upload_path(rel: str):
    rel = posixpath.normpath("/" + rel).lstrip("/")
    full = os.path.realpath(os.path.join(UPLOAD_DIR, rel))
    if not full.startswith(UPLOAD_DIR + os.sep):
        return None
    return full


class Handler(BaseHTTPRequestHandler):
    server_version = "BillingPortal/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, fmt % args))

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])
        try:
            if path == "/health":
                with db() as conn:
                    conn.execute("select 1")
                return self.send_json(200, {"status": "ok"})
            if path.startswith("/api/invoices/"):
                return self.invoice(path[len("/api/invoices/"):])
            if path.startswith("/files/"):
                return self.file(path[len("/files/"):])
            if path.startswith("/invoices/"):
                return self.invoice_page(path[len("/invoices/"):])
            if path == "/":
                return self.index()
            return self.send_json(404, {"error": "not found"})
        except psycopg.Error as exc:
            return self.send_json(503, {"error": "database unavailable", "detail": type(exc).__name__})

    def invoice(self, number):
        with db() as conn:
            row = conn.execute(
                "select i.number, c.name, i.amount_cents, i.currency, i.issued_on, i.pdf_path "
                "from invoices i join customers c on c.id = i.customer_id where i.number = %s",
                (number,),
            ).fetchone()
        if not row:
            return self.send_json(404, {"error": "invoice not found"})
        num, customer, amount, currency, issued, pdf = row
        return self.send_json(200, {
            "number": num, "customer": customer, "amount_cents": amount,
            "currency": currency, "issued_on": issued.isoformat(),
            "pdf_url": "/files/" + pdf if pdf else None,
        })

    def wants_html(self):
        # Browsers ask for text/html; API clients and RestoreProof's probe do not, so they get JSON.
        return "text/html" in (self.headers.get("Accept") or "")

    def send_page(self, status, title, body):
        page = PAGE.format(title=html.escape(title), body=body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def file(self, rel):
        full = safe_upload_path(rel)
        if not full or not os.path.isfile(full):
            if self.wants_html():
                name = html.escape(posixpath.basename(rel) or rel)
                return self.send_page(404, "File not found", (
                    f"{nav('Invoices', '/')}<main class='note'><p class='k'>Download</p>"
                    f"<h1>This PDF couldn't be found.</h1>"
                    f"<p>{name} is missing from storage. The invoice record exists, but its file does not.</p>"
                    f"<p class='code'>404 · /files/{html.escape(rel)}</p></main>"))
            return self.send_json(404, {"error": "file not found", "path": rel})
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def index(self):
        with db() as conn:
            rows = conn.execute(
                "select i.number, c.name, i.issued_on, i.amount_cents from invoices i "
                "join customers c on c.id = i.customer_id order by i.issued_on desc, i.number desc limit 15"
            ).fetchall()
            customers = conn.execute("select count(*) from customers").fetchone()[0]
        items = "".join(
            f"<tr><td><a href='/invoices/{html.escape(n)}'>{html.escape(n)}</a></td><td>{html.escape(c)}</td>"
            f"<td class='dim'>{day(d)}</td><td class='r'>{money(a)}</td></tr>"
            for n, c, d, a in rows
        )
        return self.send_page(200, "Invoices", (
            f"{nav('Invoices')}<main><div class='head'><h1>Invoices</h1>"
            f"<p class='dim'>{customers:,} customers · newest first</p></div>"
            f"<table><thead><tr><th>Invoice</th><th>Customer</th><th>Issued</th><th class='r'>Amount</th></tr>"
            f"</thead><tbody>{items}</tbody></table></main>"))

    def invoice_page(self, number):
        with db() as conn:
            row = conn.execute(
                "select i.number, c.name, i.amount_cents, i.issued_on, i.pdf_path "
                "from invoices i join customers c on c.id = i.customer_id where i.number = %s",
                (number,),
            ).fetchone()
        if not row:
            return self.send_page(404, "Invoice not found", (
                f"{nav('Invoices', '/')}<main class='note'><h1>No invoice {html.escape(number)}.</h1>"
                f"<p><a href='/'>Back to invoices</a></p></main>"))
        num, customer, amount, issued, pdf = row
        download = (f"<a class='btn' href='/files/{html.escape(pdf)}'>Download PDF</a>" if pdf
                    else "<p class='dim'>No PDF on file.</p>")
        return self.send_page(200, num, (
            f"{nav('← Invoices', '/')}<main class='note'><div class='row'><div><p class='k'>Invoice</p>"
            f"<h1>{html.escape(num)}</h1></div><p class='amt'>{money(amount)}</p></div>"
            f"<dl><div><dt>Customer</dt><dd>{html.escape(customer)}</dd></div>"
            f"<div><dt>Issued</dt><dd>{day(issued)}</dd></div></dl>{download}</main>"))


if __name__ == "__main__":
    print(f"Billing Portal listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
