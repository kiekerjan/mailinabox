#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smoke test for this Mail-in-a-Box fork on Ubuntu 26.04 -- python3 stdlib only.

Drives a box from outside over IMAP, submission, ManageSieve, SMTP, HTTP
and DNS and prints a PASS/FAIL/SKIP table plus a JSON report. It complements
tests/verify_2604.py, which inspects the configuration on the box itself.

Ported from the smoke test of https://github.com/LM1337/Mail-in-a-box-26
(CC0, like Mail-in-a-Box itself). Left out: its migration-mode checks (they
need a seeded legacy box and a manifest), its Z-Push/ActiveSync group, its
fail2ban attack group, and the checks for features this fork does not have
(its fail2ban and Munin admin API, NEXTCLOUD_SHARE_SCOPE).

Credentials come from --env-file (default ~/.config/miab-smoke.env), never
from the command line:

    TEST_USER_PW=...        # password of the test accounts this script creates
    ADMIN=you@example.com   # an admin of the box (or pass --admin-user)
    ADMIN_PW=...

Typical run from a workstation that can reach the box:

    tests/smoke.py --host 203.0.113.10 --hostname box.example.com --provision

What needs what
  * Everything needs the box reachable on 443, 993, 465, 587, 4190 and
    53, plus TEST_USER_PW and an admin account.
  * --provision creates alice@<domain>, bob@<seed2>, carol@<domain>, the
    aliases info@, team@ (permitted sender alice), the catch-all @<seed2> and
    the domain alias @<seed3>, and two sieve scripts. Run it once; the mail
    group needs those accounts.
  * The mail group sends over port 25 to the box. Without --peer-ssh the mail
    is sent from this machine, so this machine must be able to open port 25
    outbound (many ISPs block it) and must not be in the box's mynetworks.
    With --peer-ssh HOST the sending snippet runs on HOST over ssh instead,
    so the box sees a real foreign MTA.
  * --ssh HOST (or --ssh local on the box) is optional: six checks read the
    box's logs, its sa-learn counters or systemctl state and SKIP without it
    (services-enabled, mail-log-logins, greylist-bypass-config,
    root-mail-alias, imapsieve-move-run, fts-search). Privileged commands run
    through "sudo -n".
  * --jump HOST forwards all ports through an ssh host, for a box on a
    private network.

Every mail this script sends carries the header "X-Miab-Test: 1" and a
Message-ID @smoketest.invalid, and the cleanup check removes them again
(--no-cleanup keeps them).
"""
import argparse
import atexit
import base64
import hashlib
import http.client
import imaplib
import json
import os
import random
import re
import shlex
import smtplib
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.parse
import uuid
from collections import Counter
from email import message_from_bytes
from email.policy import compat32
from email.utils import formatdate

DEFAULT_ENV_FILE = os.environ.get("MIAB_TEST_ENV") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.env")

SIEVE_FILTER_NAME = "seedfilter"

SIEVE_VACATION_NAME = "seedvacation"

SIEVE_FILTER = (
    'require ["fileinto", "mailbox"];\r\n'
    'if header :contains "subject" "[filtered]" {\r\n'
    '  fileinto :create "Filtered";\r\n'
    '  stop;\r\n'
    '}\r\n'
)

VACATION_SUBJECT = "Abwesend (seed)"

def sieve_vacation(carol):
    return (
        'require ["vacation"];\r\n'
        'vacation :days 1 :subject "%s" :addresses ["%s"]\r\n'
        '  "Carol ist nicht da (seed-vacation).";\r\n' % (VACATION_SUBJECT, carol)
    )

RESULTS = []

SECRETS = set()

class Skip(Exception):
    pass

class Fail(Exception):
    pass

def redact(text):
    text = str(text)
    for s in SECRETS:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    return text

def run_check(name, fn, *a, **kw):
    t0 = time.time()
    value = None
    try:
        r = fn(*a, **kw)
        if isinstance(r, tuple) and len(r) == 2 and r[0] in ("PASS", "FAIL", "SKIP"):
            status, detail = r
        else:
            status, detail = "PASS", (r if r is not None else "")
            value = r
    except Skip as e:
        status, detail = "SKIP", str(e)
    except Fail as e:
        status, detail = "FAIL", str(e)
    except Exception as e:  # unexpected error = FAIL with type
        status, detail = "FAIL", "%s: %s" % (type(e).__name__, e)
    detail = redact(detail)
    RESULTS.append({"check": name, "status": status, "detail": detail,
                    "seconds": round(time.time() - t0, 1)})
    print("  %-4s %-38s %s" % (status, name, detail[:150]), flush=True)
    return status, value

def log(msg):
    print("[smoke] " + redact(msg), file=sys.stderr, flush=True)

def load_env_file(path):
    env = {}
    if not path or not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[len("export "):].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            env[k] = v
    return env

def ssl_ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE  # test boxes use self-signed certificates
    return c

class Net:
    PORTS = [443, 25, 465, 587, 993, 4190, 53]

    def __init__(self, host, hostname, jump=None):
        self.host = host
        self.hostname = hostname
        self.jump = jump
        self.map = {}
        self.proc = None
        if jump:
            self._start_tunnel()

    def _start_tunnel(self):
        argv = ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=15", "-o", "ConnectTimeout=20"]
        for p in self.PORTS:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            lp = s.getsockname()[1]
            s.close()
            argv += ["-L", "127.0.0.1:%d:%s:%d" % (lp, self.host, p)]
            self.map[p] = ("127.0.0.1", lp)
        argv.append(self.jump)
        self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)
        atexit.register(self.close)
        deadline = time.time() + 40
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode(errors="replace")
                raise RuntimeError("ssh tunnel via %s exited: %s" % (self.jump, err.strip()))
            try:
                # probe the HTTPS forward only: a bare connect on 993/4190
                # would be logged as "no auth attempts" and feed fail2ban
                socket.create_connection(self.map[443], 2).close()
                return
            except OSError:
                time.sleep(0.5)
        raise RuntimeError("ssh tunnel via %s did not come up" % self.jump)

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except Exception:
                self.proc.kill()

    def addr(self, port):
        return self.map.get(port, (self.host, port))

    # protocol helpers ----------------------------------------------------
    def imap(self, timeout=30):
        h, p = self.addr(993)
        return imaplib.IMAP4_SSL(h, p, ssl_context=ssl_ctx(), timeout=timeout)

    def smtp_ssl(self, timeout=60):
        h, p = self.addr(465)
        return smtplib.SMTP_SSL(h, p, local_hostname="smoketest.invalid", context=ssl_ctx(), timeout=timeout)

    def smtp_starttls(self, timeout=60):
        h, p = self.addr(587)
        s = smtplib.SMTP(h, p, local_hostname="smoketest.invalid", timeout=timeout)
        s.starttls(context=ssl_ctx())
        s.ehlo()
        return s

    def sieve(self, timeout=30):
        h, p = self.addr(4190)
        return ManageSieve(h, p, self.hostname, timeout)

class _HTTPS(http.client.HTTPSConnection):
    def __init__(self, addr, sni, timeout):
        super().__init__(addr[0], addr[1], context=ssl_ctx(), timeout=timeout)
        self._sni = sni

    def connect(self):
        http.client.HTTPConnection.connect(self)
        self.sock = ssl_ctx().wrap_socket(self.sock, server_hostname=self._sni)

class HTTPResp:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name):
        name = name.lower()
        return [v for k, v in self.headers if k.lower() == name]

    def text(self):
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.body.decode("utf-8"))

def http_req(ctx, method, path, headers=None, body=None, auth=None, form=None, timeout=90):
    h = {"Host": ctx.hostname, "User-Agent": "miab-fork-smoke/1"}
    if headers:
        h.update(headers)
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(("%s:%s" % auth).encode()).decode()
    if form is not None:
        body = urllib.parse.urlencode(form)
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if isinstance(body, str):
        body = body.encode("utf-8")
    c = _HTTPS(ctx.net.addr(443), ctx.hostname, timeout)
    try:
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        return HTTPResp(r.status, r.getheaders(), r.read())
    finally:
        c.close()

class SieveError(Exception):
    pass

class ManageSieve:
    def __init__(self, host, port, sni, timeout=30):
        self.sni = sni
        self.sock = socket.create_connection((host, port), timeout)
        self.f = self.sock.makefile("rb")
        self.caps, _ = self._response()

    def _readline(self):
        line = self.f.readline()
        if not line:
            raise SieveError("connection closed")
        return line.rstrip(b"\r\n")

    def _response(self):
        lines = []
        while True:
            line = self._readline()
            m = re.search(rb"\{(\d+)\+?\}$", line)
            if m:
                data = self.f.read(int(m.group(1)))
                rest = self._readline()
                lines.append(line[:m.start()] + data + rest)
                continue
            if line.startswith(b"OK"):
                return lines, line
            if line.startswith(b"NO") or line.startswith(b"BYE"):
                raise SieveError(line.decode(errors="replace"))
            lines.append(line)

    def cmd(self, raw):
        self.sock.sendall(raw + b"\r\n")
        return self._response()

    def starttls(self):
        self.cmd(b"STARTTLS")
        self.sock = ssl_ctx().wrap_socket(self.sock, server_hostname=self.sni)
        self.f = self.sock.makefile("rb")
        self.caps, _ = self._response()  # server re-announces capabilities

    def login(self, user, pw):
        if not any(c.upper().startswith(b'"STARTTLS"') for c in self.caps) and not isinstance(self.sock, ssl.SSLSocket):
            raise SieveError("server offers no STARTTLS")
        if not isinstance(self.sock, ssl.SSLSocket):
            self.starttls()
        cred = base64.b64encode(b"\0" + user.encode() + b"\0" + pw.encode())
        self.cmd(b'AUTHENTICATE "PLAIN" "' + cred + b'"')

    def listscripts(self):
        lines, _ = self.cmd(b"LISTSCRIPTS")
        out = []
        for line in lines:
            m = re.match(rb'^"((?:[^"\\]|\\.)*)"(\s+ACTIVE)?', line)
            if m:
                out.append((m.group(1).decode(), bool(m.group(2))))
        return out

    def putscript(self, name, text):
        data = text.encode("utf-8")
        self.sock.sendall(b'PUTSCRIPT "%s" {%d+}\r\n' % (name.encode(), len(data)) + data + b"\r\n")
        return self._response()

    def setactive(self, name):
        return self.cmd(b'SETACTIVE "%s"' % name.encode())

    def logout(self):
        try:
            self.cmd(b"LOGOUT")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

QTYPES = {"A": 1, "NS": 2, "SOA": 6, "MX": 15, "TXT": 16, "AAAA": 28, "SSHFP": 44,
          "RRSIG": 46, "DNSKEY": 48}

QTYPE_NAMES = {v: k for k, v in QTYPES.items()}

def _recvn(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise OSError("short DNS read")
        buf += chunk
    return buf

def _read_name(data, off):
    labels = []
    end = None
    hops = 0
    while True:
        ln = data[off]
        if ln & 0xC0 == 0xC0:
            if end is None:
                end = off + 2
            off = ((ln & 0x3F) << 8) | data[off + 1]
            hops += 1
            if hops > 50:
                raise ValueError("DNS name loop")
            continue
        if ln == 0:
            off += 1
            break
        labels.append(data[off + 1:off + 1 + ln].decode("ascii", "replace"))
        off += 1 + ln
    return ".".join(labels), (end if end is not None else off)

def dns_query(ctx, qname, qtype, do=True, timeout=15):
    tid = random.randint(0, 65535)
    msg = struct.pack(">HHHHHH", tid, 0, 1, 0, 0, 1 if do else 0)
    for label in qname.rstrip(".").split("."):
        msg += bytes([len(label)]) + label.encode("ascii")
    msg += b"\0" + struct.pack(">HH", QTYPES[qtype], 1)
    if do:
        msg += b"\0" + struct.pack(">HHIH", 41, 4096, 0x00008000, 0)
    with socket.create_connection(ctx.net.addr(53), timeout) as s:
        s.sendall(struct.pack(">H", len(msg)) + msg)
        ln = struct.unpack(">H", _recvn(s, 2))[0]
        data = _recvn(s, ln)
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    off = 12
    for _ in range(qd):
        _, off = _read_name(data, off)
        off += 4
    answers = []
    for _ in range(an):
        name, off = _read_name(data, off)
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        rdata = data[off:off + rdlen]
        rdoff = off
        off += rdlen
        tname = QTYPE_NAMES.get(rtype, str(rtype))
        if rtype == 16:
            parts, i = [], 0
            while i < len(rdata):
                n = rdata[i]
                parts.append(rdata[i + 1:i + 1 + n].decode("utf-8", "replace"))
                i += 1 + n
            value = "".join(parts)
        elif rtype == 44:
            value = (rdata[0], rdata[1], rdata[2:].hex().lower())
        elif rtype == 46:
            value = QTYPE_NAMES.get(struct.unpack(">H", rdata[:2])[0], "?")
        elif rtype == 15:
            value = _read_name(data, rdoff + 2)[0]
        elif rtype == 1:
            value = socket.inet_ntoa(rdata)
        else:
            value = rdata.hex()
        answers.append((name.lower(), tname, value))
    return {"rcode": flags & 0xF, "aa": bool(flags & 0x0400), "answers": answers}

class Remote:
    def __init__(self, spec):
        self.spec = spec

    def available(self):
        return bool(self.spec)

    def run(self, cmd, sudo=False, timeout=180, check=False):
        if not self.spec:
            raise Skip("needs --ssh")
        if sudo and not (self.spec == "local" and os.geteuid() == 0):
            cmd = "sudo -n " + cmd
        if self.spec == "local":
            argv = ["bash", "-c", cmd]
        else:
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", self.spec, cmd]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise Fail("remote command failed (%d): %s %s" % (p.returncode, cmd, p.stderr.strip()[:300]))
        return p.returncode, p.stdout, p.stderr

def run_snippet(snippet, func, spec, ssh_host=None, timeout=900):
    """Run func(spec) from a self-contained snippet locally or on ssh_host via 'python3 -'."""
    if not ssh_host:
        ns = {"__name__": "smoke_snippet"}
        exec(snippet, ns)
        return ns[func](spec)
    arg = base64.b64encode(json.dumps(spec).encode()).decode()
    stub = ("\nif __name__ == '__main__':\n    import sys, json, base64\n"
            "    print(json.dumps(globals()[sys.argv[1]](json.loads(base64.b64decode(sys.argv[2])))))\n")
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", ssh_host,
                        "python3 - %s %s" % (func, arg)],
                       input=snippet + stub, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise Fail("snippet on %s failed: %s" % (ssh_host, p.stderr.strip()[-400:]))
    return json.loads(p.stdout.strip().splitlines()[-1])

SMTP_SNIPPET = r'''
import base64, os, smtplib, socket

def deliver(spec):
    out = {}
    for m in spec["messages"]:
        data = m.get("data")
        if data is None:
            blob = base64.encodebytes(os.urandom(m["gen_size"] * 3 // 4)).decode().replace("\n", "\r\n")
            data = m["gen_headers"] + "\r\n" + blob
        data = data.encode("utf-8")
        try:
            s = smtplib.SMTP(spec["host"], spec["port"], local_hostname=spec.get("helo", "smoketest.invalid"), timeout=300)
            try:
                s.ehlo()
                s.sendmail(m["from"], [m["rcpt"]], data)
                out[m["id"]] = [250, "accepted"]
            finally:
                try:
                    s.quit()
                except Exception:
                    pass
        except smtplib.SMTPRecipientsRefused as e:
            code, msg = list(e.recipients.values())[0]
            out[m["id"]] = [code, msg.decode("utf-8", "replace")]
        except smtplib.SMTPResponseException as e:
            msg = e.smtp_error.decode("utf-8", "replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            out[m["id"]] = [e.smtp_code, msg]
        except Exception as e:
            out[m["id"]] = [0, "%s: %s" % (type(e).__name__, e)]
    return out

def source_ip(spec):
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        u.connect((spec["host"], spec.get("port", 25)))
        return u.getsockname()[0]
    finally:
        u.close()
'''

class Ctx:
    pass

TEST_HEADER = "X-Miab-Test"

TEST_MSGID_DOMAIN = "smoketest.invalid"

def mkmsg(frm, to, subject, sid, body=None, extra=None):
    lines = ["From: %s" % frm, "To: %s" % to, "Subject: %s" % subject,
             "Date: %s" % formatdate(localtime=True),
             "Message-ID: <%s.%s@%s>" % (sid, uuid.uuid4().hex, TEST_MSGID_DOMAIN),
             "X-Smoke-Id: %s" % sid, "%s: 1" % TEST_HEADER, "MIME-Version: 1.0",
             "Content-Type: text/plain; charset=utf-8"]
    lines += extra or []
    if body is None:
        body = "Smoke test message %s.\r\nThis is a functional test of the mail flow.\r\n" % sid
    return "\r\n".join(lines) + "\r\n\r\n" + body

def qmb(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'

def imap_login(ctx, user):
    M = ctx.net.imap()
    M.login(user, ctx.user_pw)
    return M

LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.+)$')

def list_folders(M):
    typ, data = M.list()
    out = []
    for raw in data or []:
        if raw is None:
            continue
        if isinstance(raw, tuple):  # literal name
            line = raw[0].decode(errors="replace")
            name = raw[1].decode(errors="replace")
            m = LIST_RE.match(line.rsplit(" ", 1)[0] + " x")
            flags = m.group("flags") if m else ""
        else:
            line = raw.decode(errors="replace")
            m = LIST_RE.match(line)
            if not m:
                continue
            flags, name = m.group("flags"), m.group("name")
            if name.startswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if "\\noselect" in flags.lower() or "\\nonexistent" in flags.lower():
            continue
        out.append(name)
    return out

def uid_search(M, folder, *criteria, readonly=True):
    typ, _ = M.select(qmb(folder), readonly=readonly)
    if typ != "OK":
        return []
    typ, data = M.uid("SEARCH", *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()

MSGID_RE = re.compile(r"<([^<>\s]+)>")

def parse_mail(raw):
    """A message (or only its header) as email.message.Message."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    return message_from_bytes(raw, policy=compat32)

def mail_id(msg):
    m = MSGID_RE.search(str(msg.get("Message-ID", "")))
    return m.group(1) if m else ""

def sa_magic(ctx):
    rc, out, err = ctx.remote.run(ctx.args.sa_learn_cmd, sudo=False, timeout=120)
    vals = {}
    for line in out.splitlines():
        m = re.match(r"^\s*\S+\s+\S+\s+(\d+)\s+\S+\s+non-token data: (nspam|nham)\s*$", line)
        if m:
            vals[m.group(2)] = int(m.group(1))
    if "nspam" not in vals:
        raise Fail("could not parse sa-learn --dump magic (rc=%d): %s" % (rc, (err or out).strip()[:300]))
    return vals

def admin_api(ctx, method, path, form=None, auth=None, body=None, headers=None, timeout=90):
    auth = auth or ctx.admin_auth
    if not auth:
        raise Skip("no admin session (admin credentials missing or login failed)")
    return http_req(ctx, method, "/admin/" + path.lstrip("/"), form=form, auth=auth, body=body, headers=headers,
                    timeout=timeout)

def remove_test_mailbox(ctx, email):
    # Adding a user makes Dovecot create its mailbox (quota recalc), which
    # stays after the user is removed and is listed as an archived
    # (inactive) account. Remove it again for the test accounts of this run
    # (needs --ssh). Returns a note.
    local, domain = email.split("@", 1)
    if not re.fullmatch(r"smoke[a-z]*-[0-9a-f]{10}", local) or not re.fullmatch(r"[a-z0-9.-]+", domain) or ctx.tok not in local:
        raise Fail("not a test account of this run: %s" % email)
    if not ctx.remote.available():
        return "its archived mailbox stays (no --ssh)"
    cmd = ('root=$(sed -n "s/^STORAGE_ROOT=//p" /etc/mailinabox.conf | tail -n 1); '
           'd="$root/mail/mailboxes/%s/%s"; [ -n "$root" ] && [ ! -L "$d" ] && rm -rf -- "$d" && [ ! -e "$d" ]') % (domain, local)
    rc, out, err = ctx.remote.run("bash -c %s" % shlex.quote(cmd), sudo=True, timeout=60)
    if rc != 0:
        raise Fail("could not remove the archived mailbox of %s: %s" % (email, err.strip()[:120]))
    return "archived mailbox removed"

def c_imap_login(ctx):
    M = imap_login(ctx, ctx.alice)
    typ, _ = M.select("INBOX", readonly=True)
    caps = " ".join(c if isinstance(c, str) else c.decode() for c in M.capabilities)
    M.logout()
    if typ != "OK":
        raise Fail("SELECT INBOX failed")
    return "login ok; caps incl. MOVE=%s QUOTA=%s" % ("MOVE" in caps, "QUOTA" in caps)

def c_sub465(ctx):
    s = ctx.net.smtp_ssl()
    try:
        s.login(ctx.alice, ctx.user_pw)
    finally:
        s.quit()
    return "AUTH ok"

def c_sub587(ctx):
    s = ctx.net.smtp_starttls()
    try:
        s.login(ctx.alice, ctx.user_pw)
    finally:
        s.quit()
    return "STARTTLS + AUTH ok"

def c_sieve_login(ctx):
    ms = ctx.net.sieve()
    try:
        ms.login(ctx.alice, ctx.user_pw)
        scripts = ms.listscripts()
    finally:
        ms.logout()
    ctx.alice_scripts = scripts
    return "login ok, scripts: %s" % (", ".join("%s%s" % (n, "*" if a else "") for n, a in scripts) or "none")

CORE_UNITS = ["postfix", "dovecot", "nginx", "spampd", "opendkim", "opendmarc", "postgrey",
              "fail2ban", "nsd", "munin-node", "mailinabox", "cron",
              "unbound", "dmarc_report_viewer", "postfix-tlspol"]

UNIT_ENABLED_STATES = ("enabled", "static", "indirect", "generated", "alias")

def c_services_enabled(ctx):
    cmd = ("for u in %s $(cd /lib/systemd/system && ls php*-fpm.service 2>/dev/null); do "
           "printf '%%s ' \"${u%%.service}\"; systemctl is-enabled \"$u\" 2>/dev/null | head -n 1; echo; done"
           % " ".join(CORE_UNITS))
    rc, out, err = ctx.remote.run(cmd, timeout=60)
    states = {}
    for line in out.splitlines():
        unit, _, state = line.strip().partition(" ")
        if unit:
            states[unit] = state.strip()
    missing = [u for u in CORE_UNITS if u not in states]
    if missing:
        raise Fail("no answer for %s: %s" % (" ".join(missing), (err or out).strip()[:200]))
    bad = ["%s=%s" % (u, st) for u, st in sorted(states.items()) if st not in UNIT_ENABLED_STATES]
    if bad:
        raise Fail("not enabled (would not start after a reboot): %s" % ", ".join(bad))
    return "%d units enabled" % len(states)

def c_mail_log_logins(ctx):
    # The usage report (management/mail_log.py, run weekly by daily_tasks.sh) must
    # count the IMAP logins just made (Dovecot 2.4 logs "Logged in:").
    rc, miab, _err = ctx.remote.run("awk '$1 == \"cd\" { print $2; exit }' /usr/local/bin/mailinabox")
    miab = miab.strip()
    if rc != 0 or not miab:
        raise Skip("Mail-in-a-Box checkout not found in /usr/local/bin/mailinabox")
    cmd = "cd %s && management/mail_log.py -l -v -t hour -u %s" % (shlex.quote(miab), shlex.quote(ctx.alice))
    rc, out, err = ctx.remote.run("bash -c %s" % shlex.quote(cmd), sudo=True, timeout=300)
    if rc != 0:
        raise Fail("mail_log.py rc=%d: %s" % (rc, (err or out).strip()[-200:]))
    counts = {p: sum(int(n) for n in re.findall(r"\b%s \S+: (\d+) times" % p, out)) for p in ("imap",)}
    missing = [p for p, n in counts.items() if not n]
    if missing:
        raise Fail("no %s logins of %s in the last hour counted" % ("/".join(missing), ctx.alice))
    return "logins of the last hour: imap %d" % counts["imap"]

def c_provision(ctx):
    done = []
    for email, quota in ((ctx.alice, "0"), (ctx.bob, "10M"), (ctx.carol, "0")):
        r = admin_api(ctx, "POST", "mail/users/add", form={"email": email, "password": ctx.user_pw, "quota": quota})
        if r.status == 200:
            done.append("+" + email)
        elif r.status == 400 and "already exists" in r.text():
            r2 = admin_api(ctx, "POST", "mail/users/password", form={"email": email, "password": ctx.user_pw})
            r3 = admin_api(ctx, "POST", "mail/users/quota", form={"email": email, "quota": quota})
            if r2.status != 200 or r3.status != 200:
                raise Fail("cannot reset %s: %s %s" % (email, r2.text()[:100], r3.text()[:100]))
        else:
            raise Fail("users/add %s: HTTP %d %s" % (email, r.status, r.text()[:200]))
    aliases = [
        ("info@" + ctx.domain, ctx.alice, ""),
        ("@" + ctx.seed2, ctx.bob, ""),
        ("@" + ctx.seed3, "@" + ctx.seed2, ""),
        ("team@" + ctx.domain, "%s,%s" % (ctx.alice, ctx.carol), ctx.alice),
    ]
    for addr, fwd, senders in aliases:
        r = admin_api(ctx, "POST", "mail/aliases/add", form={"address": addr, "forwards_to": fwd,
                                                             "permitted_senders": senders, "update_if_exists": "1"})
        if r.status != 200:
            raise Fail("aliases/add %s: HTTP %d %s" % (addr, r.status, r.text()[:200]))
    for user, name, text in ((ctx.alice, SIEVE_FILTER_NAME, SIEVE_FILTER),
                             (ctx.carol, SIEVE_VACATION_NAME, sieve_vacation(ctx.carol))):
        ms = ctx.net.sieve()
        try:
            ms.login(user, ctx.user_pw)
            ms.putscript(name, text)
            ms.setactive(name)
        finally:
            ms.logout()
    return "users/aliases/sieve ensured %s" % " ".join(done)

def nc_userid(ctx, user):
    if user in ctx.nc_ids:
        return ctx.nc_ids[user]
    r = http_req(ctx, "GET", "/cloud/ocs/v1.php/cloud/user?format=json",
                 headers={"OCS-APIRequest": "true"}, auth=(user, ctx.user_pw))
    if r.status != 200:
        raise Fail("OCS user lookup HTTP %d" % r.status)
    uid = r.json()["ocs"]["data"]["id"]
    ctx.nc_ids[user] = uid
    return uid

def roundcube_login(ctx, user):
    """Returns cookie jar dict after a successful login, raises Fail otherwise."""
    jar = {}

    def eat(resp):
        for v in resp.header("Set-Cookie"):
            k, _, rest = v.partition("=")
            jar[k.strip()] = rest.split(";", 1)[0]

    def cookie():
        return "; ".join("%s=%s" % kv for kv in jar.items() if kv[1] not in ("", "deleted"))

    r = http_req(ctx, "GET", "/mail/")
    eat(r)
    if r.status != 200:
        raise Fail("GET /mail/ HTTP %d" % r.status)
    m = re.search(r'name="_token" value="([^"]+)"', r.text())
    if not m:
        raise Fail("no login token on /mail/")
    r = http_req(ctx, "POST", "/mail/?_task=login", headers={"Cookie": cookie()}, form={
        "_token": m.group(1), "_task": "login", "_action": "login", "_timezone": "Europe/Berlin",
        "_url": "", "_user": user, "_pass": ctx.user_pw})
    eat(r)
    loc = (r.header("Location") or [""])[0]
    if r.status not in (301, 302) or "_task=login" in loc:
        raise Fail("login POST HTTP %d Location=%s" % (r.status, loc))
    return jar, cookie

def c_admin_login(ctx):
    if not ctx.admin_user or not ctx.admin_pw:
        raise Skip("admin credentials not given")
    r = http_req(ctx, "POST", "/admin/login", auth=(ctx.admin_user, ctx.admin_pw))
    j = r.json() if r.status == 200 else {}
    if j.get("status") == "missing-totp-token":
        raise Skip("admin has TOTP enabled")
    if j.get("status") != "ok":
        raise Fail("login HTTP %d %s" % (r.status, j.get("reason", r.text()[:100])))
    SECRETS.add(j["api_key"])
    ctx.admin_auth = (ctx.admin_user, j["api_key"])
    return "session for %s, privileges %s" % (j["email"], j.get("privileges"))

def c_admin_tmp_user_alias(ctx):
    tmp = "smoketmp-%s@%s" % (ctx.tok, ctx.domain)
    tmpa = "smokealias-%s@%s" % (ctx.tok, ctx.domain)
    r = admin_api(ctx, "POST", "mail/users/add", form={"email": tmp, "password": ctx.user_pw})
    if r.status != 200:
        raise Fail("users/add HTTP %d %s" % (r.status, r.text()[:200]))
    try:
        r = admin_api(ctx, "POST", "mail/aliases/add", form={"address": tmpa, "forwards_to": tmp, "permitted_senders": ""})
        if r.status != 200:
            raise Fail("aliases/add HTTP %d %s" % (r.status, r.text()[:200]))
        r = admin_api(ctx, "GET", "mail/aliases?format=json")
        if not any(a["address"] == tmpa for d in r.json() for a in d["aliases"]):
            raise Fail("new alias not listed")
        r = admin_api(ctx, "POST", "mail/aliases/remove", form={"address": tmpa})
        if r.status != 200:
            raise Fail("aliases/remove HTTP %d" % r.status)
    finally:
        r = admin_api(ctx, "POST", "mail/users/remove", form={"email": tmp})
    if r.status != 200:
        raise Fail("users/remove HTTP %d %s" % (r.status, r.text()[:200]))
    return "add/list/remove of user and alias ok, %s" % remove_test_mailbox(ctx, tmp)

def c_backup_status(ctx):
    r = admin_api(ctx, "GET", "system/backup/status")
    if r.status != 200:
        raise Fail("HTTP %d" % r.status)
    j = r.json()
    if "error" in j:
        raise Fail("backup status error: %s" % j["error"][:200])
    b = j.get("backups") or []
    cfg = admin_api(ctx, "GET", "system/backup/config").json()
    last = b[0].get("date_str", b[0].get("date")) if b else None
    return "target=%s, %d backups, latest %s%s" % (cfg.get("target"), len(b), last,
        " (saved status as of %s: %s)" % (j.get("saved_time_str"), j["in_progress"][:100]) if j.get("in_progress") else "")

def c_dnssec(ctx, zone):
    res = dns_query(ctx, zone, "SOA", do=True)
    types = [t for _n, t, _v in res["answers"]]
    if res["rcode"] != 0 or "SOA" not in types:
        raise Fail("rcode=%d answers=%s" % (res["rcode"], types))
    sigs = [v for _n, t, v in res["answers"] if t == "RRSIG"]
    if "SOA" not in sigs:
        raise Fail("SOA not signed (no RRSIG); zone unsigned on this box")
    keys = dns_query(ctx, zone, "DNSKEY", do=True)
    if not any(t == "DNSKEY" for _n, t, _v in keys["answers"]):
        raise Fail("no DNSKEY")
    return "SOA+RRSIG, DNSKEY present (aa=%s)" % res["aa"]

def c_mx_dkim(ctx, domain):
    mx = [v for _n, t, v in dns_query(ctx, domain, "MX", do=False)["answers"] if t == "MX"]
    txt = [v for _n, t, v in dns_query(ctx, "mail._domainkey." + domain, "TXT", do=False)["answers"] if t == "TXT"]
    if not mx:
        raise Fail("no MX")
    if not any(v.replace(" ", "").startswith("v=DKIM1") and "p=" in v for v in txt):
        raise Fail("no DKIM TXT at mail._domainkey (have %s)" % txt)
    return "MX %s, DKIM TXT ok" % ",".join(mx)

def c_dns_custom_roundtrip(ctx):
    qname = "smoke-%s.%s" % (ctx.tok, ctx.domain)
    value = "smoke-%s" % ctx.tok
    r = admin_api(ctx, "PUT", "dns/custom/%s/TXT" % qname, body=value, headers={"Content-Type": "text/plain"})
    if r.status != 200:
        raise Fail("PUT HTTP %d %s" % (r.status, r.text()[:200]))
    try:
        deadline = time.time() + 60
        while True:
            vals = [v for _n, t, v in dns_query(ctx, qname, "TXT", do=False)["answers"] if t == "TXT"]
            if value in vals:
                break
            if time.time() > deadline:
                raise Fail("TXT not served by nsd after 60 s")
            time.sleep(3)
    finally:
        admin_api(ctx, "DELETE", "dns/custom/%s/TXT" % qname)
    return "custom TXT set, served, removed"

def c_nc_status(ctx):
    r = http_req(ctx, "GET", "/cloud/status.php")
    if r.status != 200:
        raise Fail("HTTP %d" % r.status)
    j = r.json()
    v = j.get("versionstring", "")
    bad = []
    if not j.get("installed"):
        bad.append("not installed")
    if j.get("maintenance"):
        bad.append("maintenance mode")
    if j.get("needsDbUpgrade"):
        bad.append("needsDbUpgrade")
    if not v.startswith(ctx.args.expect_nc + "."):
        bad.append("version %s, expected %s.x" % (v, ctx.args.expect_nc))
    if bad:
        raise Fail(", ".join(bad))
    return "Nextcloud %s" % v

def c_nc_dav(ctx, kind):
    uid = nc_userid(ctx, ctx.alice)
    path = {"caldav": "/cloud/remote.php/dav/calendars/%s/",
            "carddav": "/cloud/remote.php/dav/addressbooks/users/%s/",
            "webdav": "/cloud/remote.php/dav/files/%s/"}[kind] % urllib.parse.quote(uid)
    body = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:displayname/></d:prop></d:propfind>'
    r = http_req(ctx, "PROPFIND", path, auth=(ctx.alice, ctx.user_pw), body=body,
                 headers={"Depth": "1", "Content-Type": "application/xml"})
    if r.status != 207:
        raise Fail("PROPFIND %s HTTP %d" % (path, r.status))
    n = r.text().count("<d:response>")
    return "207, %d entries (user id %s)" % (n, uid)

def c_nc_dav_proxy(ctx):
    # B130: the proxy of /caldav, /carddav and /webdav (to Nextcloud's
    # remote.php) passes the path escaped, as the client sent it, and the
    # query string. It decoded the path: a space gave 400, an escaped "?"
    # began a query string (a DELETE of a card whose name began with "?"
    # deleted the whole address book), and query strings were dropped. A
    # card whose name has a space and a "?" is put through Nextcloud's own
    # path, then read through /carddav, found in the address book's export
    # (?export) and deleted through /carddav; the address book must stay.
    # The DELETE is only sent when the reads worked (on a box with the old
    # proxy it would reach another name, never the address book: the name
    # does not begin with "?"). And nginx's dotfile block (the regex location
    # at the end of nginx-alldomains.conf) must not apply to these paths, as
    # it did not with the old regex location (users' own files, #414): a
    # file named ".htmiab-smoke-..." and a card ".gitmiab-smoke-....vcf",
    # put through Nextcloud's own path, are read through /webdav and
    # /carddav (403 with plain prefix locations).
    uid = urllib.parse.quote(nc_userid(ctx, ctx.alice))
    auth = (ctx.alice, ctx.user_pw)
    mark = "miab-smoke-%s" % ctx.tok
    name = urllib.parse.quote("%s proxy?name.vcf" % mark, safe="")
    book = "/cloud/remote.php/dav/addressbooks/users/%s/contacts/" % uid
    proxied_book = "/carddav/addressbooks/%s/contacts/" % uid
    vcf = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:%s-proxy\r\nFN:%s proxy\r\nN:%s;Proxy;;;\r\n"
           "NOTE:X-Miab-Test 1\r\nEND:VCARD\r\n" % (mark, mark, mark))
    propfind = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
    dotfile = ".htmiab-smoke-%s" % ctx.tok
    files = "/cloud/remote.php/dav/files/%s/" % uid
    dotcard = ".gitmiab-smoke-%s.vcf" % ctx.tok
    dotvcf = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:%s-dot\r\nFN:%s dot\r\nN:%s;Dot;;;\r\n"
              "NOTE:X-Miab-Test 1\r\nEND:VCARD\r\n" % (mark, mark, mark))

    def put(path, body, ctype):
        for attempt in range(3):
            # Nextcloud on SQLite answers 500 ("database is locked") now and then
            r = http_req(ctx, "PUT", path, auth=auth, body=body, headers={"Content-Type": ctype})
            if r.status not in (500, 503):
                break
            time.sleep(10)
        if r.status not in (201, 204):
            raise Fail("PUT %s HTTP %d" % (path, r.status))

    try:
        put(book + name, vcf, "text/vcard; charset=utf-8")
        r = http_req(ctx, "GET", proxied_book + name, auth=auth)
        if r.status != 200 or mark not in r.text():
            raise Fail("GET of the card %s through /carddav: HTTP %d (the proxy does not pass the escaped name)"
                       % (name, r.status))
        r = http_req(ctx, "GET", proxied_book + "?export", auth=auth)
        if r.status != 200 or mark not in r.text():
            raise Fail("GET /carddav/.../contacts/?export: HTTP %d (the proxy drops the query string)" % r.status)
        r = http_req(ctx, "DELETE", proxied_book + name, auth=auth)
        if r.status != 204:
            raise Fail("DELETE of the card through /carddav: HTTP %d" % r.status)
        r = http_req(ctx, "PROPFIND", book, auth=auth, body=propfind,
                     headers={"Depth": "0", "Content-Type": "application/xml"})
        if r.status != 207:
            raise Fail("after the DELETE through /carddav the address book answers HTTP %d" % r.status)
        r = http_req(ctx, "GET", book + name, auth=auth)
        if r.status != 404:
            raise Fail("after the DELETE through /carddav the card answers HTTP %d" % r.status)
        out = ["/carddav: card with space and ? read, exported, deleted (the address book stays)"]
        put(files + dotfile, "%s X-Miab-Test 1\n" % mark, "text/plain")
        r = http_req(ctx, "GET", "/webdav/" + dotfile, auth=auth)
        if r.status != 200 or mark not in r.text():
            raise Fail("GET /webdav/%s: HTTP %d (nginx's dotfile block applies to the proxy)" % (dotfile, r.status))
        put(book + dotcard, dotvcf, "text/vcard; charset=utf-8")
        r = http_req(ctx, "GET", proxied_book + dotcard, auth=auth)
        if r.status != 200 or mark not in r.text():
            raise Fail("GET of the card %s through /carddav: HTTP %d (nginx's dotfile block applies to the proxy)"
                       % (dotcard, r.status))
        out.append("dotfiles %s, %s through /webdav, /carddav 200" % (dotfile, dotcard))
        for kind, path in (("caldav", "/caldav/calendars/%s/" % uid), ("webdav", "/webdav/")):
            r = http_req(ctx, "PROPFIND", path, auth=auth, body=propfind,
                         headers={"Depth": "0", "Content-Type": "application/xml"})
            if r.status != 207:
                raise Fail("PROPFIND %s HTTP %d" % (path, r.status))
            out.append("/%s 207" % kind)
        return "; ".join(out)
    finally:
        http_req(ctx, "DELETE", book + name, auth=auth)
        http_req(ctx, "DELETE", book + dotcard, auth=auth)
        r = http_req(ctx, "DELETE", files + dotfile, auth=auth)
        if r.status == 204:
            # and from alice's trash bin (it holds "<name>.d<time>")
            trash = "/cloud/remote.php/dav/trashbin/%s/trash/" % uid
            r = http_req(ctx, "PROPFIND", trash, auth=auth, body=propfind,
                         headers={"Depth": "1", "Content-Type": "application/xml"})
            if r.status == 207:
                for href in re.findall(r"<d:href>([^<]+)</d:href>", r.text()):
                    if urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1]).startswith(dotfile + ".d"):
                        http_req(ctx, "DELETE", href, auth=auth)

def c_mail_config_path(ctx):
    # B134: Roundcube's deny of its config directory was the unanchored regex
    # location "~ /mail/config/.*", which came before the regex locations of
    # Nextcloud and answered 403 for every path with /mail/config/ in it. Now
    # "^/mail/config/": /mail/config/ itself is still denied, a Nextcloud file
    # below a folder "mail/config" is read through /cloud/remote.php and
    # through the /webdav proxy, and a missing static page under such a path
    # is a 404, not a 403.
    uid = urllib.parse.quote(nc_userid(ctx, ctx.alice))
    auth = (ctx.alice, ctx.user_pw)
    mark = "miab-smoke-%s" % ctx.tok
    files = "/cloud/remote.php/dav/files/%s/" % uid
    top = files + mark + "/"
    body = "%s X-Miab-Test 1\n" % mark
    propfind = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
    out = []
    r = http_req(ctx, "GET", "/mail/config/%s.txt" % mark)
    if r.status != 403:
        raise Fail("GET /mail/config/%s.txt: HTTP %d (Roundcube's config directory must answer 403)" % (mark, r.status))
    out.append("/mail/config/ 403")
    r = http_req(ctx, "GET", "/%s/mail/config/x.txt" % mark)
    if r.status != 404:
        raise Fail("GET /%s/mail/config/x.txt (a missing static page): HTTP %d, want 404" % (mark, r.status))
    out.append("static /.../mail/config/ 404")
    try:
        for d in (top, top + "mail/", top + "mail/config/"):
            for attempt in range(3):
                # Nextcloud on SQLite answers 500 ("database is locked") now and then
                r = http_req(ctx, "MKCOL", d, auth=auth)
                if r.status not in (500, 503):
                    break
                time.sleep(10)
            if r.status != 201:
                raise Fail("MKCOL %s HTTP %d" % (d, r.status))
        path = top + "mail/config/x.txt"
        for attempt in range(3):
            r = http_req(ctx, "PUT", path, auth=auth, body=body, headers={"Content-Type": "text/plain"})
            if r.status not in (500, 503):
                break
            time.sleep(10)
        if r.status not in (201, 204):
            raise Fail("PUT %s HTTP %d" % (path, r.status))
        for via in (path, "/webdav/%s/mail/config/x.txt" % mark):
            r = http_req(ctx, "GET", via, auth=auth)
            if r.status != 200 or mark not in r.text():
                raise Fail("GET %s: HTTP %d (a path with /mail/config/ in it must reach Nextcloud)" % (via, r.status))
        out.append("Nextcloud file .../mail/config/x.txt 200 through /cloud/remote.php and /webdav")
        return "; ".join(out)
    finally:
        r = http_req(ctx, "DELETE", top, auth=auth)
        if r.status == 204:
            # and from alice's trash bin (it holds "<name>.d<time>")
            trash = "/cloud/remote.php/dav/trashbin/%s/trash/" % uid
            r = http_req(ctx, "PROPFIND", trash, auth=auth, body=propfind,
                         headers={"Depth": "1", "Content-Type": "application/xml"})
            if r.status == 207:
                for href in re.findall(r"<d:href>([^<]+)</d:href>", r.text()):
                    if urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1]).startswith(mark + ".d"):
                        http_req(ctx, "DELETE", href, auth=auth)

def c_roundcube(ctx):
    r = http_req(ctx, "GET", "/mail/")
    if r.status != 200 or 'name="_user"' not in r.text():
        raise Fail("login page HTTP %d" % r.status)
    _jar, cookie = roundcube_login(ctx, ctx.alice)
    r = http_req(ctx, "GET", "/mail/?_task=mail&_mbox=INBOX", headers={"Cookie": cookie()})
    if r.status != 200 or 'name="_pass"' in r.text():
        raise Fail("after login: HTTP %d, still login form=%s" % (r.status, 'name="_pass"' in r.text()))
    return "login page 200, login + mailbox view ok"

def smtp_batch(ctx, msgs):
    """Send port-25 messages, retrying greylisted (4xx) ones. Returns {id: [code, text, attempts]}."""
    final = {}
    pending = list(msgs)
    where = ctx.args.peer_ssh
    if where:
        target = (ctx.args.host, 25)
    else:
        target = ctx.net.addr(25)
    for attempt in range(ctx.args.greylist_retries + 1):
        res = run_snippet(SMTP_SNIPPET, "deliver",
                          {"host": target[0], "port": target[1], "messages": pending}, ssh_host=where)
        for m in pending:
            code, text = res[m["id"]]
            final[m["id"]] = [code, text, attempt + 1]
        deferred = [m for m in pending if 400 <= res[m["id"]][0] < 500]
        if not deferred:
            break
        if attempt < ctx.args.greylist_retries:
            log("%d message(s) deferred (%s), waiting %d s for greylisting"
                % (len(deferred), res[deferred[0]["id"]][1][:60], ctx.args.greylist_wait))
            time.sleep(ctx.args.greylist_wait)
        pending = deferred
    return final

def submit(ctx, port, login_user, mail_from, rcpt, data):
    s = ctx.net.smtp_ssl() if port == 465 else ctx.net.smtp_starttls()
    try:
        s.login(login_user, ctx.user_pw)
        s.sendmail(mail_from, [rcpt], data.encode())
        return [250, "accepted"]
    except smtplib.SMTPRecipientsRefused as e:
        code, msg = list(e.recipients.values())[0]
        return [code, msg.decode(errors="replace")]
    except smtplib.SMTPResponseException as e:
        return [e.smtp_code, e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)]
    finally:
        try:
            s.quit()
        except Exception:
            pass

def wait_for(ctx, expectations, timeout):
    """expectations: list of dicts {key, user, folders:[...], criteria:(...)}; returns {key: folder or None}."""
    found = {e["key"]: None for e in expectations}
    deadline = time.time() + timeout
    while True:
        by_user = {}
        for e in expectations:
            if found[e["key"]] is None:
                by_user.setdefault(e["user"], []).append(e)
        if not by_user:
            break
        for user, exps in by_user.items():
            M = imap_login(ctx, user)
            try:
                for e in exps:
                    for folder in e["folders"]:
                        if uid_search(M, folder, *e["criteria"]):
                            found[e["key"]] = folder
                            break
            finally:
                M.logout()
        if time.time() > deadline or all(v is not None for v in found.values()):
            break
        time.sleep(5)
    return found

def c_mail_flow(ctx):
    """Sends everything at once (greylisting costs one wait) and records sub-results."""
    tok, P, s2, s3 = ctx.tok, ctx.domain, ctx.seed2, ctx.seed3
    sender = "smoke-%s@%s" % (tok, s2)
    msgs = []

    def add(mid, rcpt, subject, frm=None, extra=None, to_hdr=None):
        sid = "smk-%s-%s" % (tok, mid)
        msgs.append({"id": mid, "from": frm or sender, "rcpt": rcpt,
                     "data": mkmsg(frm or sender, to_hdr or rcpt, subject, sid, extra=extra)})
        return sid

    ids = {}
    ids["direct"] = add("direct", ctx.alice, "smoke direct %s" % tok)
    ids["alias"] = add("alias", "info@" + P, "smoke alias %s" % tok)
    ids["catchall"] = add("catchall", "nobody-%s@%s" % (tok, s2), "smoke catch-all %s" % tok)
    ids["domalias_ca"] = add("domalias_ca", "nobody-%s@%s" % (tok, s3), "smoke domain alias %s" % tok)
    ids["domalias_user"] = add("domalias_user", "bob@" + s3, "smoke domain alias user %s" % tok)
    ids["filtered"] = add("filtered", ctx.alice, "[filtered] smoke %s" % tok)
    vac_sender = "vac-%s@%s" % (tok, s2)
    ids["vacation"] = add("vacation", ctx.carol, "smoke vacation %s" % tok, frm=vac_sender)
    oq_sender = "oq-%s@%s" % (tok, s2)
    oq_sid = "smk-%s-overquota" % tok
    hdr = mkmsg(oq_sender, ctx.bob, "smoke over quota %s" % tok, oq_sid, body="")
    hdr = hdr[:-2] if hdr.endswith("\r\n\r\n") else hdr  # gen_headers ends with one CRLF
    msgs.append({"id": "overquota", "from": oq_sender, "rcpt": ctx.bob, "data": None,
                 "gen_size": int(ctx.args.overquota_mb * 1024 * 1024), "gen_headers": hdr})

    # submission (465 / 587, sender login maps)
    sub = {}
    sub["s465"] = submit(ctx, 465, ctx.alice, ctx.alice, ctx.alice,
                         mkmsg(ctx.alice, ctx.alice, "smoke 465 %s" % tok, "smk-%s-s465" % tok))
    sub["s587"] = submit(ctx, 587, ctx.alice, ctx.alice, ctx.alice,
                         mkmsg(ctx.alice, ctx.alice, "smoke 587 %s" % tok, "smk-%s-s587" % tok))
    sub["team_ok"] = submit(ctx, 587, ctx.alice, "team@" + P, ctx.alice,
                            mkmsg("team@" + P, ctx.alice, "smoke team %s" % tok, "smk-%s-team" % tok))
    sub["team_denied"] = submit(ctx, 587, ctx.bob, "team@" + P, ctx.alice,
                                mkmsg("team@" + P, ctx.alice, "smoke team denied %s" % tok, "smk-%s-teamdeny" % tok))

    smtp = smtp_batch(ctx, msgs)
    ctx.flow = {"smtp": smtp, "sub": sub, "ids": ids, "tok": tok}

    exp = []

    def expect(key, user, folders, criteria):
        exp.append({"key": key, "user": user, "folders": folders, "criteria": criteria})

    allf = ["INBOX", "Spam", "Filtered", "Archive"]
    for k, user in (("direct", ctx.alice), ("alias", ctx.alice), ("catchall", ctx.bob),
                    ("domalias_ca", ctx.bob), ("domalias_user", ctx.bob), ("filtered", ctx.alice),
                    ("vacation", ctx.carol)):
        if smtp[k][0] == 250:
            expect(k, user, allf, ("HEADER", "X-Smoke-Id", ids[k]))
    for k in ("s465", "s587", "team_ok"):
        if sub[k][0] == 250:
            expect(k, ctx.alice, allf, ("HEADER", "X-Smoke-Id", "smk-%s-%s" % (tok, {"team_ok": "team"}.get(k, k))))
    expect("vacation_reply", ctx.bob, ["INBOX", "Spam"], ("TO", '"%s"' % vac_sender))
    if smtp["overquota"][0] == 250:
        expect("overquota_delivered", ctx.bob, ["INBOX", "Spam"], ("HEADER", "X-Smoke-Id", oq_sid))
        expect("overquota_bounce", ctx.bob, ["INBOX", "Spam"], ("TO", '"%s"' % oq_sender))
    # the over-quota "delivered"/"bounce" keys are exclusive; give them the normal window
    ctx.flow["found"] = wait_for(ctx, exp, ctx.args.deliver_timeout)
    return "sent %d via port 25 (%s), 4 submissions; results recorded" % (
        len(msgs), "peer " + ctx.args.peer_ssh if ctx.args.peer_ssh else ("jump " + ctx.args.jump if ctx.args.jump else "direct"))

def _flow(ctx):
    if not getattr(ctx, "flow", None):
        raise Skip("mail flow not run")
    return ctx.flow

def c_delivery(ctx, key, want_folder, label):
    f = _flow(ctx)
    code, text, attempts = f["smtp"][key]
    if code != 250:
        raise Fail("%s rejected at SMTP: %s %s (attempt %d)" % (label, code, text[:120], attempts))
    where = f["found"].get(key)
    if where is None:
        raise Fail("%s accepted but not found in mailbox within %d s" % (label, ctx.args.deliver_timeout))
    if where != want_folder:
        raise Fail("%s landed in %s, expected %s" % (label, where, want_folder))
    return "accepted (attempt %d) and in %s" % (attempts, where)

def c_submission_delivery(ctx, key, label):
    f = _flow(ctx)
    code, text = f["sub"][key]
    if code != 250:
        raise Fail("%s refused: %s %s" % (label, code, text[:150]))
    where = f["found"].get(key)
    if where != "INBOX":
        raise Fail("%s accepted but found in %s" % (label, where))
    return "submitted and delivered to INBOX"

def c_sender_denied(ctx):
    f = _flow(ctx)
    code, text = f["sub"]["team_denied"]
    if 500 <= code < 600:
        return "bob may not send as team@: %d %s" % (code, text[:80])
    raise Fail("bob could send as team@ (not a permitted sender): %s %s" % (code, text[:100]))

def c_no_quota_reject_alias(ctx):
    f = _flow(ctx)
    bad = []
    for k in ("alias", "catchall", "domalias_ca", "domalias_user"):
        code, text, _a = f["smtp"][k]
        if code != 250:
            bad.append("%s: %d %s" % (k, code, text[:60]))
    if bad:
        raise Fail("; ".join(bad))
    if not ctx.args.peer_ssh and not ctx.args.jump and ctx.args.host in ("127.0.0.1", "::1"):
        return ("PASS", "accepted, but sent from mynetworks (policy service not consulted)")
    return "alias, catch-all and domain-alias recipients accepted at RCPT"

def c_overquota(ctx):
    f = _flow(ctx)
    code, text, attempts = f["smtp"]["overquota"]
    if 500 <= code < 600:
        return "%d MB mail rejected at SMTP: %d %s" % (ctx.args.overquota_mb, code, text[:80])
    if code != 250:
        raise Fail("unexpected SMTP result %d %s" % (code, text[:100]))
    if f["found"].get("overquota_delivered"):
        raise Fail("%d MB mail was ACCEPTED and DELIVERED into bob's 10M mailbox "
                   "(check quota_storage_grace: 2.4 takes an absolute size, 22.04 MIAB used 10%%)" % ctx.args.overquota_mb)
    if f["found"].get("overquota_bounce"):
        return ("FAIL" if (ctx.args.peer_ssh or ctx.args.jump) else "PASS",
                "accepted at SMTP, LMTP refused, bounce generated (quota-status policy did not reject at RCPT)")
    raise Fail("accepted at SMTP, neither delivered nor bounced within the wait window")

def c_quotaroot(ctx):
    M = imap_login(ctx, ctx.bob)
    try:
        typ, data = M.getquotaroot("INBOX")
    finally:
        M.logout()
    flat = " ".join(x.decode(errors="replace") if isinstance(x, bytes) else str(x)
                    for part in data for x in (part if isinstance(part, list) else [part]))
    m = re.search(r"STORAGE (\d+) (\d+)", flat)
    if typ != "OK" or not m:
        raise Fail("GETQUOTAROOT: %s %s" % (typ, flat[:200]))
    used, limit = int(m.group(1)), int(m.group(2))
    if limit != 10240:
        raise Fail("bob limit %d KiB, expected 10240 (10M)" % limit)
    return "bob STORAGE %d / %d KiB" % (used, limit)

GREYLIST_BYPASS = "/etc/postfix/greylist_bypass_senders"

def c_greylist_bypass(ctx):
    # prepare-testenv.sh whitelists the peers in postgrey, so greylisting cannot
    # be watched on port 25 here: check what setup/mail-postfix.sh configured.
    has_file = ctx.remote.run("test -f %s" % GREYLIST_BYPASS)[0] == 0
    rc, out, err = ctx.remote.run("postconf -h smtpd_recipient_restrictions", timeout=60)
    items = [i.strip() for i in out.strip().split(",")]
    bypass = "check_sender_access hash:" + GREYLIST_BYPASS
    grey = "check_policy_service inet:127.0.0.1:10023"
    quota = "check_policy_service inet:127.0.0.1:12340"
    if not has_file:
        # The test boxes have no such file: then the restrictions end as upstream's.
        if bypass in items:
            raise Fail("%r is set, but %s is missing: %s" % (bypass, GREYLIST_BYPASS, out.strip()[:200]))
        if items[-2:] != [grey, quota]:
            raise Fail("without %s the restrictions should end with postgrey, then the quota check: %s"
                       % (GREYLIST_BYPASS, out.strip()[:200]))
        return "no %s: postgrey, then the quota check (as upstream)" % GREYLIST_BYPASS
    for item in (bypass, grey, quota):
        if item not in items:
            raise Fail("%r missing in smtpd_recipient_restrictions: %s" % (item, out.strip()[:200]))
    if not items.index(quota) < items.index(bypass) == items.index(grey) - 1:
        raise Fail("order should be quota check, sender bypass, postgrey: %s" % out.strip()[:200])
    rc, key, _err = ctx.remote.run("sed -n 's/^\\([^#[:space:]][^[:space:]]*\\).*/\\1/p' %s" % GREYLIST_BYPASS)
    key = (key.split() or [""])[0]
    if not key:
        return ("PASS", "restrictions ok; the file has no entries")
    rc, val, err = ctx.remote.run("postmap -q %s hash:%s" % (shlex.quote(key), GREYLIST_BYPASS), sudo=True, timeout=60)
    if rc != 0:
        raise Fail("postmap -q %s gives nothing (is the .db older than the file?) %s" % (key, err.strip()[:150]))
    return "quota check, then %s (%s -> %s), then postgrey" % (bypass, key, val.strip()[:20])

def c_root_mail(ctx):
    # Mail of cron and root goes to root@ + /etc/mailname (myorigin); setup sets
    # /etc/mailname to PRIMARY_HOSTNAME and makes root@ an automatic alias.
    rc, out, err = ctx.remote.run("cat /etc/mailname")
    if rc != 0 or out.strip() != ctx.hostname:
        raise Fail("/etc/mailname is %r, expected %s" % (out.strip()[:80], ctx.hostname))
    root = "root@" + ctx.hostname
    rc, out, err = ctx.remote.run("postmap -q %s sqlite:/etc/postfix/virtual-alias-maps.cf" % shlex.quote(root),
                                  sudo=True, timeout=60)
    if rc != 0 or not out.strip():
        raise Fail("%s is no alias, system mail would bounce (rc=%d %s)" % (root, rc, err.strip()[:120]))
    return "/etc/mailname ok, %s -> %s" % (root, out.strip()[:60])

def c_vacation(ctx):
    f = _flow(ctx)
    if f["smtp"]["vacation"][0] != 250:
        raise Fail("mail to carol rejected: %s" % f["smtp"]["vacation"][:2])
    if not f["found"].get("vacation"):
        raise Fail("mail to carol not delivered")
    if not f["found"].get("vacation_reply"):
        raise Fail("no vacation auto-reply to %s within %d s (carol's active script: vacation?)"
                   % ("vac-%s@%s" % (ctx.tok, ctx.seed2), ctx.args.deliver_timeout))
    return "auto-reply arrived via catch-all in bob's %s" % f["found"]["vacation_reply"]

def c_vacation_no_phantom_folders(ctx):
    # Dovecot 2.4 kept the lock files of the duplicate database (vacation,
    # redirect) in .dovecot.lda-dupes.locks in the Maildir, which IMAP listed as
    # folders "dovecot", "dovecot.lda-dupes" and "dovecot.lda-dupes.locks" (B66).
    # \Noselect entries count too. The parent "dovecot" alone is not counted:
    # a user may have a real folder of that name.
    f = _flow(ctx)
    if not f["found"].get("vacation_reply"):
        raise Skip("no vacation reply, so the duplicate database was not used")
    M = imap_login(ctx, ctx.carol)
    try:
        typ, data = M.list()
    finally:
        M.logout()
    names = []
    for raw in data or []:
        if raw is None:
            continue
        if isinstance(raw, tuple):
            names.append(raw[1].decode(errors="replace"))
            continue
        m = LIST_RE.match(raw.decode(errors="replace"))
        if m:
            name = m.group("name")
            names.append(name[1:-1] if name.startswith('"') else name)
    phantom = [n for n in names if re.match(r"(?i)^dovecot[./]lda-dupes($|[./]locks$)", n)]
    if phantom:
        raise Fail("%s has the folders %s" % (ctx.carol, ", ".join(sorted(phantom))))
    return "%d folders of %s, none from Dovecot's duplicate database" % (len(names), ctx.carol)

def c_imapsieve(ctx):
    before = sa_magic(ctx)
    sid = "smk-%s-imapsieve" % ctx.tok
    words = " ".join(random.choice(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
                                    "hotel", "india", "juliet", "kilo", "lima", "mike", "november"])
                     + str(random.randint(0, 99999)) for _ in range(150))
    msg = mkmsg("sender-%s@%s" % (ctx.tok, ctx.seed2), ctx.alice, "smoke imapsieve %s" % ctx.tok, sid,
                body=words + "\r\n")
    M = imap_login(ctx, ctx.alice)
    out = {}
    try:
        typ, _ = M.append("INBOX", None, None, msg.encode())
        if typ != "OK":
            raise Fail("APPEND failed")
        uids = uid_search(M, "INBOX", "HEADER", "X-Smoke-Id", sid, readonly=False)
        if not uids:
            raise Fail("appended message not found")
        typ, data = M.uid("MOVE", uids[-1].decode(), "Spam")
        if typ != "OK":
            raise Fail("MOVE to Spam: %s %s" % (typ, data))
        out["spam"] = _poll_magic(ctx, lambda m: m["nspam"] > before["nspam"])
        after_spam = out["spam"] or sa_magic(ctx)
        uids = uid_search(M, "Spam", "HEADER", "X-Smoke-Id", sid, readonly=False)
        if not uids:
            raise Fail("message not in Spam after MOVE")
        typ, data = M.uid("MOVE", uids[-1].decode(), "INBOX")
        if typ != "OK":
            raise Fail("MOVE out of Spam: %s %s" % (typ, data))
        out["ham"] = _poll_magic(ctx, lambda m: m.get("nham", 0) > after_spam.get("nham", 0))
    finally:
        M.logout()
    ctx.imapsieve = {"before": before, **out}
    return "before %s, after spam-move %s, after ham-move %s" % (before, out.get("spam"), out.get("ham"))

def c_fts(ctx):
    """Full-text search: a body-only word must be found by IMAP SEARCH and by the fts_xapian index."""
    word = "ftsprobe%s%d" % (ctx.tok.replace("-", ""), random.randint(1000, 9999))
    sid = "smk-%s-fts" % ctx.tok
    msg = mkmsg("sender-%s@%s" % (ctx.tok, ctx.seed2), ctx.alice, "smoke fts %s" % ctx.tok, sid,
                body="The quick brown fox mentions %s only in the body.\r\n" % word)
    M = imap_login(ctx, ctx.alice)
    try:
        typ, _ = M.append("INBOX", None, None, msg.encode())
        if typ != "OK":
            raise Fail("APPEND failed")
        uids = []
        for _ in range(15):
            uids = uid_search(M, "INBOX", "BODY", word)
            if uids:
                break
            time.sleep(2)
        if not uids:
            raise Fail("IMAP SEARCH BODY %s found nothing" % word)
    finally:
        M.logout()
    # Make sure the hit came from the Xapian index and not from a slow scan.
    # Indexing runs asynchronously in the indexer, so give it a moment.
    for _ in range(15):
        rc, out, err = ctx.remote.run("doveadm fts lookup -u %s mailbox INBOX body %s" % (ctx.alice, word),
                                      sudo=True, timeout=120)
        if rc == 0 and re.search(r"INBOX: \d", out):
            break
        time.sleep(2)
    if rc != 0 or not re.search(r"INBOX: \d", out):
        raise Fail("doveadm fts lookup rc=%d: %s" % (rc, (err or out).strip()[:300]))
    return "IMAP SEARCH found uid %s, fts index: %s" % (uids[-1].decode(), out.strip().splitlines()[0][:80])

def _poll_magic(ctx, cond, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        m = sa_magic(ctx)
        if cond(m):
            return m
        time.sleep(3)
    return None

def c_imapsieve_result(ctx, which):
    r = getattr(ctx, "imapsieve", None)
    if r is None:
        raise Skip("IMAPSieve test did not run")
    if not r.get(which):
        raise Fail("n%s did not increase after MOVE %s Spam (before %s)" % (which, "into" if which == "spam" else "out of", r["before"]))
    return "n%s %d -> %d" % (which, r["before"].get("n" + which, 0), r[which]["n" + which])

def c_cleanup(ctx):
    n = 0
    for user in (ctx.alice, ctx.bob, ctx.carol):
        try:
            M = imap_login(ctx, user)
        except Exception:
            continue
        try:
            for folder in list_folders(M):
                uids = uid_search(M, folder, "TEXT", '"%s"' % ctx.tok, readonly=False)
                if uids:
                    M.uid("STORE", b",".join(uids).decode(), "+FLAGS.SILENT", "(\\Deleted)")
                    M.expunge()
                    n += len(uids)
        finally:
            M.logout()
    return "removed %d test messages" % n

def send_test_mail(ctx):
    """--send-test-mail FROM TO: one marked test mail, for the testers' own mails
    (vacation triggers, delivery tests after a reboot)."""
    frm, to = ctx.args.send_test_mail
    sid = "test-%s" % ctx.tok
    data = mkmsg(frm, to, ctx.args.test_subject or "test mail %s" % ctx.tok, sid)
    msgid = mail_id(parse_mail(data))
    if ctx.args.via == "25":
        res = smtp_batch(ctx, [{"id": "t", "from": frm, "rcpt": to, "data": data}])
        code, text = res["t"][0], res["t"][1]
        how = "port 25 from " + (ctx.args.peer_ssh or "here")
    else:
        code, text = submit(ctx, int(ctx.args.via), frm, frm, to, data)
        how = "submission %s as %s" % (ctx.args.via, frm)
    print("%s: %s %s, Message-ID <%s>" % (how, code, redact(str(text))[:120], msgid))
    return 0 if code == 250 else 1

API_READ_ONLY = [
    ("GET", "/system/version", "text/html"),
    ("POST", "/system/latest-upstream-version", "text/html"),
    ("POST", "/system/status", "application/json"),
    ("GET", "/system/updates", "text/html"),
    ("GET", "/system/privacy", "application/json"),
    ("GET", "/system/reboot", "application/json"),
    ("GET", "/system/backup/status", "application/json"),
    ("GET", "/system/backup/config", "application/json"),
    ("GET", "/ssl/status", "application/json"),
    ("POST", "/ssl/csr/{domain}", "text/html"),
    ("GET", "/dns/zones", "application/json"),
    ("GET", "/dns/zonefile/{zone}", "text/plain"),
    ("GET", "/dns/custom", "application/json"),
    ("GET", "/dns/custom/{qname}", "application/json"),
    ("GET", "/dns/custom/{qname}/{rtype}", "application/json"),
    ("GET", "/dns/dump", "application/json"),
    ("GET", "/dns/secondary-nameserver", "application/json"),
    ("GET", "/mail/users", "application/json"),
    ("GET", "/mail/users/privileges", "text/html"),
    ("GET", "/mail/users/quota", "application/json"),
    ("GET", "/mail/domains", "text/html"),
    ("GET", "/mail/aliases", "application/json"),
    ("GET", "/web/domains", "application/json"),
    ("POST", "/mfa/status", "application/json"),
    ("GET", "/dns/advanced-dns", "application/json"),
    ("GET", "/munin/", "text/html"),
]

def api_ctype(r):
    v = r.header("Content-Type")
    return v[0].split(";")[0].strip().lower() if v else ""

def api_expect(r, status, ctype, what):
    # status and content type, or Fail with a short excerpt (never the body
    # of an answer that can hold secrets: the caller passes what=... only)
    if r.status != status:
        raise Fail("%s: HTTP %d, expected %d: %s" % (what, r.status, status, r.text()[:120].strip()))
    if ctype and api_ctype(r) != ctype:
        raise Fail("%s: content type %s, expected %s" % (what, api_ctype(r) or "none", ctype))

def _is_str_list(x):
    return isinstance(x, list) and all(isinstance(v, str) for v in x)

def _need(cond, what):
    if not cond:
        raise Fail(what)

def api_shape(ctx, path, r):
    # The documented shape of each read-only answer (api/mailinabox.yml).
    # Returns a short note. Never puts the body of /mfa/status into a message.
    if api_ctype(r) == "application/json":
        j = r.json()
    t = r.text()
    if path == "/system/version":
        _need(t.strip() and "\n" not in t.strip(), "version: %r" % t[:60])
        return t.strip()
    if path == "/system/latest-upstream-version":
        _need(t.strip() and "\n" not in t.strip(), "latest version: %r" % t[:60])
        return "latest " + t.strip()
    if path == "/system/status":
        _need(isinstance(j, list) and j, "status: not a non-empty list")
        for e in j:
            _need(isinstance(e, dict) and e.get("type") in ("heading", "ok", "error", "warning")
                  and isinstance(e.get("text"), str) and isinstance(e.get("extra"), list), "status entry %r" % str(e)[:80])
        c = Counter(e["type"] for e in j)
        return "%d ok, %d warnings, %d errors" % (c["ok"], c["warning"], c["error"])
    if path == "/system/updates":
        bad = [x for x in t.splitlines() if x and not re.match(r"^\S+ \(.+\)$", x)]
        _need(not bad, "updates: line %r" % (bad[:1],))
        return "%d updates" % len(t.splitlines())
    if path in ("/system/privacy", "/system/reboot"):
        _need(isinstance(j, bool), "%s: not a boolean" % path)
        return str(j).lower()
    if path == "/system/backup/status":
        _need(isinstance(j, dict) and (isinstance(j.get("backups"), list) or isinstance(j.get("error"), str)),
              "backup status: no backups list and no error")
        return "error" if "error" in j else "%d backups" % len(j["backups"])
    if path == "/system/backup/config":
        _need(isinstance(j, dict) and isinstance(j.get("target"), str), "backup config: no target")
        return "target " + j["target"].split(":", 1)[0]
    if path == "/ssl/status":
        _need(_is_str_list(j.get("can_provision")) and isinstance(j.get("status"), list)
              and all({"domain", "status", "text"} <= set(e) for e in j["status"]), "ssl status: keys missing")
        return "%d domains" % len(j["status"])
    if path == "/ssl/csr/{domain}":
        _need(t.startswith("-----BEGIN CERTIFICATE REQUEST-----"), "csr: not a PEM certificate request")
        return "CSR"
    if path == "/dns/zones":
        _need(_is_str_list(j) and j, "zones: not a non-empty list of names")
        return "%d zones" % len(j)
    if path == "/dns/zonefile/{zone}":
        _need(re.search(r"\sSOA\s", t), "zone file without SOA")
        return "%d lines" % len(t.splitlines())
    if path.startswith("/dns/custom"):
        _need(isinstance(j, list) and all({"qname", "rtype", "value", "sort-order"} <= set(e) for e in j), "custom records: keys missing")
        return "%d records" % len(j)
    if path == "/dns/dump":
        _need(isinstance(j, list) and j and all(isinstance(z, list) and len(z) == 2 and isinstance(z[1], list)
                                              and all({"qname", "rtype", "value", "explanation"} <= set(x) for x in z[1]) for z in j),
              "dns dump: not a list of [zone, records]")
        return "%d zones" % len(j)
    if path == "/dns/secondary-nameserver":
        _need(_is_str_list(j.get("hostnames")), "secondary nameserver: no hostnames list")
        return "%d hostnames" % len(j["hostnames"])
    if path == "/mail/users":
        _need(isinstance(j, list) and all(isinstance(d.get("domain"), str) and isinstance(d.get("users"), list) for d in j),
              "users: not a list of domains with users")
        users = {u.get("email"): u for d in j for u in d["users"]}
        _need(all({"email", "privileges", "status"} <= set(u) for u in users.values()), "users: keys missing")
        _need("admin" in (users.get(ctx.admin_user) or {}).get("privileges", []), "users: %s is not listed as admin" % ctx.admin_user)
        return "%d users" % len(users)
    if path == "/mail/users/privileges":
        _need("admin" in t.split("\n"), "privileges of the admin: %r" % t[:60])
        return "admin"
    if path == "/mail/users/quota":
        _need(j.get("email") == ctx.admin_user and isinstance(j.get("quota"), str), "quota: %r" % str(j)[:80])
        return "quota " + j["quota"]
    if path == "/mail/domains":
        _need(ctx.domain in t.split("\n"), "domains: %s missing" % ctx.domain)
        return "%d domains" % len(t.split())
    if path == "/mail/aliases":
        _need(isinstance(j, list) and all(isinstance(d.get("domain"), str) and isinstance(d.get("aliases"), list) for d in j)
              and all({"address", "forwards_to", "permitted_senders", "auto"} <= set(a) for d in j for a in d["aliases"]),
              "aliases: not a list of domains with aliases")
        return "%d aliases" % sum(len(d["aliases"]) for d in j)
    if path == "/web/domains":
        _need(isinstance(j, list) and j and all({"domain", "root", "custom_root", "ssl_certificate"} <= set(d) for d in j),
              "web domains: keys missing")
        return "%d domains" % len(j)
    if path == "/mfa/status":
        _need(isinstance(j.get("enabled_mfa"), list) and isinstance((j.get("new_mfa") or {}).get("totp"), dict),
              "mfa status: enabled_mfa or new_mfa.totp missing")
        SECRETS.add(j["new_mfa"]["totp"].get("secret", ""))
        return "%d MFA" % len(j["enabled_mfa"])
    if path == "/munin/":
        _need(t.strip() == "OK" and any("session=" in c for c in r.header("Set-Cookie")), "munin start: no session cookie")
        return "cookie"
    raise Fail("no shape check for %s" % path)

def api_request(ctx, method, path):
    # The request for a documented read-only path, with real parameters.
    zones = getattr(ctx, "api_zones", None) or [ctx.domain]
    real = (path.replace("{domain}", ctx.hostname).replace("{zone}", zones[0])
            .replace("{qname}", ctx.hostname).replace("{rtype}", "A"))
    form = None
    if path == "/mail/users" or path == "/mail/aliases":
        real += "?format=json"
    elif path in ("/mail/users/privileges", "/mail/users/quota"):
        real += "?" + urllib.parse.urlencode({"email": ctx.admin_user})
    elif path == "/ssl/csr/{domain}":
        form = {"countrycode": "US"}
    elif method == "POST":
        form = {}
    # system/status runs all status checks, system/updates apt-get update
    timeout = 300 if path in ("/system/status", "/system/updates") else 90
    return admin_api(ctx, method, real, form=form, timeout=timeout)

def c_api_read_only(ctx):
    notes, fails = [], []
    r = admin_api(ctx, "GET", "dns/zones")
    ctx.api_zones = r.json() if r.status == 200 else None
    for method, path, ctype in API_READ_ONLY:
        try:
            r = api_request(ctx, method, path)
            if path == "/system/latest-upstream-version" and r.status == 503:
                notes.append("%s 503 (documented: not reachable)" % path)
                continue
            api_expect(r, 200, ctype, "%s %s" % (method, path))
            notes.append("%s %s" % (path, api_shape(ctx, path, r)))
        except Fail as e:
            fails.append(str(e))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            fails.append("%s %s: %s %s" % (method, path, type(e).__name__, str(e)[:80]))
    # The plain text forms of the user and alias lists.
    r = admin_api(ctx, "GET", "mail/users")
    if r.status != 200 or api_ctype(r) != "text/html" or ctx.admin_user not in r.text().split("\n"):
        fails.append("GET /mail/users (text): HTTP %d %s" % (r.status, api_ctype(r)))
    r = admin_api(ctx, "GET", "mail/aliases")
    if r.status != 200 or api_ctype(r) != "text/html":
        fails.append("GET /mail/aliases (text): HTTP %d %s" % (r.status, api_ctype(r)))
    if fails:
        raise Fail("%d of %d: %s" % (len(fails), len(API_READ_ONLY), "; ".join(fails)))
    return "%d endpoints: %s" % (len(API_READ_ONLY), ", ".join(n for n in notes))

def api_snapshot(ctx):
    # The lists that the write round trip must leave as they were.
    users = admin_api(ctx, "GET", "mail/users?format=json")
    aliases = admin_api(ctx, "GET", "mail/aliases?format=json")
    custom = admin_api(ctx, "GET", "dns/custom")
    for r, what in ((users, "users"), (aliases, "aliases"), (custom, "custom records")):
        api_expect(r, 200, "application/json", what)
    # mailbox sizes change with deliveries; compare the accounts only
    u = sorted((x["email"], tuple(x["privileges"]), x["status"], x.get("quota")) for d in users.json() for x in d["users"])
    return {"users": u, "aliases": aliases.json(), "custom": custom.json(),
            "privacy": admin_api(ctx, "GET", "system/privacy").json()}

def c_api_write_roundtrip(ctx):
    if not ctx.user_pw:
        raise Skip("TEST_USER_PW missing")
    before = api_snapshot(ctx)
    user = "smokeapi-%s@%s" % (ctx.tok, ctx.domain)
    alias = "smokeapialias-%s@%s" % (ctx.tok, ctx.domain)
    qname = "smokeapi-%s.%s" % (ctx.tok, ctx.domain)
    v1, v2 = "smokeapi-%s-1" % ctx.tok, "smokeapi-%s-2" % ctx.tok
    pw2 = "Smk-%s" % uuid.uuid4().hex
    SECRETS.add(pw2)
    steps = []
    txt = {"Content-Type": "text/plain"}

    def records():
        r = admin_api(ctx, "GET", "dns/custom/%s/TXT" % qname)
        api_expect(r, 200, "application/json", "GET custom TXT")
        return sorted(x["value"] for x in r.json())

    try:
        api_expect(admin_api(ctx, "POST", "mail/users/add", form={"email": user, "password": ctx.user_pw}), 200, "text/html", "users/add")
        api_expect(admin_api(ctx, "POST", "mail/users/password", form={"email": user, "password": pw2}), 200, "text/html", "users/password")
        for form in ({"email": user, "quota": "1.5G"}, {"email": user}):
            r = admin_api(ctx, "POST", "mail/users/quota", form=form)
            _need(r.status == 400, "users/quota %s: HTTP %d, expected 400" % (form.get("quota", "(none)"), r.status))
        api_expect(admin_api(ctx, "POST", "mail/users/quota", form={"email": user, "quota": "1G"}), 200, "text/html", "users/quota")
        r = admin_api(ctx, "GET", "mail/users/quota?" + urllib.parse.urlencode({"email": user}))
        api_expect(r, 200, "application/json", "GET quota")
        _need(r.json().get("quota") == "1G", "quota after set: %s" % r.json().get("quota"))
        r = admin_api(ctx, "GET", "mail/users/quota?" + urllib.parse.urlencode({"email": "nobody-%s@%s" % (ctx.tok, ctx.domain)}))
        _need(r.status == 400, "GET quota of a non-user: HTTP %d, expected 400" % r.status)
        steps.append("user, password, quota (and two refused)")
        api_expect(admin_api(ctx, "POST", "mail/users/privileges/add", form={"email": user, "privilege": "admin"}), 200, "text/html", "privileges/add")
        _need("admin" in admin_api(ctx, "GET", "mail/users/privileges?" + urllib.parse.urlencode({"email": user})).text().split("\n"),
              "admin privilege not listed")
        api_expect(admin_api(ctx, "POST", "mail/users/privileges/remove", form={"email": user, "privilege": "admin"}), 200, "text/html", "privileges/remove")
        _need("admin" not in admin_api(ctx, "GET", "mail/users/privileges?" + urllib.parse.urlencode({"email": user})).text().split("\n"),
              "admin privilege still listed")
        steps.append("admin privilege")
        api_expect(admin_api(ctx, "POST", "mail/aliases/add", form={"address": alias, "forwards_to": user, "permitted_senders": ""}),
                   200, "text/html", "aliases/add")
        r = admin_api(ctx, "GET", "mail/aliases?format=json")
        _need(any(a["address"] == alias and a["forwards_to"] == [user] for d in r.json() for a in d["aliases"]), "new alias not listed")
        api_expect(admin_api(ctx, "POST", "mail/aliases/remove", form={"address": alias}), 200, "text/html", "aliases/remove")
        steps.append("alias")
        api_expect(admin_api(ctx, "POST", "dns/custom/%s/TXT" % qname, body=v1, headers=txt), 200, "text/html", "POST custom TXT")
        _need(records() == [v1], "after POST: %s" % records())
        api_expect(admin_api(ctx, "POST", "dns/custom/%s/TXT" % qname, body=v2, headers=txt), 200, "text/html", "POST second TXT")
        _need(records() == sorted([v1, v2]), "after second POST: %s" % records())
        api_expect(admin_api(ctx, "PUT", "dns/custom/%s/TXT" % qname, body=v1, headers=txt), 200, "text/html", "PUT custom TXT")
        _need(records() == [v1], "after PUT: %s" % records())
        api_expect(admin_api(ctx, "DELETE", "dns/custom/%s/TXT" % qname, body=v1, headers=txt), 200, "text/html", "DELETE custom TXT")
        _need(records() == [], "after DELETE: %s" % records())
        steps.append("custom TXT post/put/delete")
        cur = before["privacy"]
        api_expect(admin_api(ctx, "POST", "system/privacy", form={"value": "private" if cur else "off"}), 200, "text/html", "privacy")
        steps.append("privacy (same value)")
    finally:
        admin_api(ctx, "DELETE", "dns/custom/%s/TXT" % qname)
        admin_api(ctx, "POST", "mail/aliases/remove", form={"address": alias})
        r = admin_api(ctx, "POST", "mail/users/remove", form={"email": user})
        # Also when a step above failed: its archived mailbox would stay as an
        # inactive account. An error here must not hide the step's own.
        try:
            mailbox = remove_test_mailbox(ctx, user)
        except Exception as e:
            mailbox = e
    api_expect(r, 200, "text/html", "users/remove")
    if isinstance(mailbox, Exception):
        raise mailbox
    steps.append("user removed, " + mailbox)
    after = api_snapshot(ctx)
    if not ctx.remote.available():
        # without --ssh the archived mailbox of the test user stays
        after["users"] = [u for u in after["users"] if u[0] != user]
    diff = [k for k in before if before[k] != after[k]]
    if diff:
        raise Fail("not the same afterwards: %s" % ", ".join(diff))
    return "%s; users, aliases, custom records and privacy the same afterwards" % ", ".join(steps)


GROUPS = ["provision", "login", "admin", "api", "dns", "web", "mail", "imapsieve"]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="groups for --only/--skip: " + ",".join(GROUPS))
    ap.add_argument("--host", required=True, help="IP or name of the box as reachable from here")
    ap.add_argument("--hostname", required=True, help="PRIMARY_HOSTNAME (TLS SNI, HTTP Host)")
    ap.add_argument("--domain", help="primary mail domain (default: hostname without its first label)")
    ap.add_argument("--seed2", help="second mail domain for bob/catch-all (default SEED2_DOMAIN, else seed2.example.net)")
    ap.add_argument("--seed3", help="third mail domain for the domain alias (default SEED3_DOMAIN, else seed3.example.net)")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="file with TEST_USER_PW, ADMIN, ADMIN_PW")
    ap.add_argument("--admin-user", help="admin e-mail (default: ADMIN from the env file)")
    ap.add_argument("--admin-pw-var", default="ADMIN_PW", help="env var holding the admin password")
    ap.add_argument("--user-pw-var", default="TEST_USER_PW")
    ap.add_argument("--jump", help="ssh host to forward all ports through")
    ap.add_argument("--ssh", help="optional ssh host of the box ('local' on the box) for the six log/sa-learn checks")
    ap.add_argument("--peer", help="address of the sending host as the box sees it (informational)")
    ap.add_argument("--peer-ssh", help="ssh host that sends the port-25 mail (default: from here)")
    ap.add_argument("--provision", action="store_true", help="create the test accounts, aliases and sieve scripts via the API")
    ap.add_argument("--greylist-wait", type=int, default=200)
    ap.add_argument("--greylist-retries", type=int, default=2)
    ap.add_argument("--deliver-timeout", type=int, default=150)
    ap.add_argument("--overquota-mb", type=float, default=12)
    ap.add_argument("--expect-nc", default="34", help="expected Nextcloud major version")
    ap.add_argument("--sa-learn-cmd", default="cd / && sudo -n -u spampd -H /usr/bin/sa-learn --dump magic")
    ap.add_argument("--skip", default="", help="comma list of groups to skip")
    ap.add_argument("--only", default="", help="comma list of groups to run")
    ap.add_argument("--no-cleanup", action="store_true", help="keep the test mail")
    ap.add_argument("--report", help="also write the JSON report to this file")
    ap.add_argument("--send-test-mail", nargs=2, metavar=("FROM", "TO"),
                    help="send one marked test mail and exit")
    ap.add_argument("--via", choices=["25", "465", "587"], default="25",
                    help="--send-test-mail: port to send through")
    ap.add_argument("--test-subject", help="--send-test-mail: the subject")
    args = ap.parse_args()

    skip = {g for g in args.skip.split(",") if g}
    only = {g for g in args.only.split(",") if g}

    def want(group):
        if group == "provision" and not args.provision:
            return False
        return group not in skip and (not only or group in only)

    env = load_env_file(args.env_file)
    env.update({k: v for k, v in os.environ.items()
                if k in ("TEST_USER_PW", "ADMIN", "ADMIN_PW", "SEED2_DOMAIN", "SEED3_DOMAIN")})

    ctx = Ctx()
    ctx.args = args
    ctx.hostname = args.hostname
    ctx.domain = args.domain or args.hostname.split(".", 1)[1]
    ctx.seed2 = args.seed2 or env.get("SEED2_DOMAIN") or "seed2.example.net"
    ctx.seed3 = args.seed3 or env.get("SEED3_DOMAIN") or "seed3.example.net"
    ctx.alice = "alice@" + ctx.domain
    ctx.bob = "bob@" + ctx.seed2
    ctx.carol = "carol@" + ctx.domain
    ctx.user_pw = env.get(args.user_pw_var, "")
    ctx.admin_user = args.admin_user or env.get("ADMIN")
    ctx.admin_pw = env.get(args.admin_pw_var, "")
    ctx.admin_auth = None
    ctx.nc_ids = {}
    ctx.tok = uuid.uuid4().hex[:10]
    ctx.t0 = time.time()
    ctx.remote = Remote(args.ssh)
    SECRETS.update({ctx.user_pw, ctx.admin_pw or ""})
    if not ctx.user_pw and not (args.send_test_mail and args.via == "25"):
        print("TEST_USER_PW missing (env or --env-file %s)" % args.env_file, file=sys.stderr)
        return 2

    print("smoke test: host=%s hostname=%s domain=%s token=%s"
          % (args.host, args.hostname, ctx.domain, ctx.tok))
    try:
        ctx.net = Net(args.host, args.hostname, args.jump)
    except Exception as e:
        print("cannot set up network path: %s" % redact(e), file=sys.stderr)
        return 2
    if args.send_test_mail:
        try:
            return send_test_mail(ctx)
        finally:
            ctx.net.close()

    if want("admin") or want("api") or want("provision") or want("dns") or want("web") or want("mail"):
        print("== admin")
        run_check("admin-api-login", c_admin_login, ctx)
    if want("provision"):
        print("== provision")
        run_check("provision-test-accounts", c_provision, ctx)
    if want("login"):
        print("== login")
        run_check("imaps-993-login", c_imap_login, ctx)
        run_check("submission-465-login", c_sub465, ctx)
        run_check("submission-587-login", c_sub587, ctx)
        run_check("managesieve-4190-login", c_sieve_login, ctx)
        run_check("mail-log-logins", c_mail_log_logins, ctx)
        run_check("services-enabled", c_services_enabled, ctx)
    if want("admin"):
        run_check("admin-api-tmp-user-alias", c_admin_tmp_user_alias, ctx)
        run_check("admin-backup-status", c_backup_status, ctx)
    if want("api"):
        print("== api (management API)")
        run_check("api-read-only", c_api_read_only, ctx)
        run_check("api-write-roundtrip", c_api_write_roundtrip, ctx)
    if want("dns"):
        print("== dns")
        for zone in (ctx.domain, ctx.seed2, ctx.seed3):
            run_check("dnssec-" + zone, c_dnssec, ctx, zone)
            run_check("mx-dkim-" + zone, c_mx_dkim, ctx, zone)
        run_check("dns-custom-roundtrip", c_dns_custom_roundtrip, ctx)
    if want("web"):
        print("== web")
        run_check("nextcloud-status", c_nc_status, ctx)
        run_check("nextcloud-caldav-propfind", c_nc_dav, ctx, "caldav")
        run_check("nextcloud-carddav-propfind", c_nc_dav, ctx, "carddav")
        run_check("nextcloud-webdav-propfind", c_nc_dav, ctx, "webdav")
        run_check("nextcloud-dav-proxy", c_nc_dav_proxy, ctx)
        run_check("nextcloud-mail-config-path", c_mail_config_path, ctx)
        run_check("roundcube-login", c_roundcube, ctx)
    if want("mail"):
        print("== mail flow")
        run_check("mail-flow-send", c_mail_flow, ctx)
        run_check("smtp25-direct-delivery", c_delivery, ctx, "direct", "INBOX", "direct mail")
        run_check("smtp25-alias", c_delivery, ctx, "alias", "INBOX", "info@ alias")
        run_check("smtp25-catch-all", c_delivery, ctx, "catchall", "INBOX", "catch-all @seed2")
        run_check("smtp25-domain-alias", c_delivery, ctx, "domalias_ca", "INBOX", "domain alias @seed3")
        run_check("smtp25-domain-alias-user", c_delivery, ctx, "domalias_user", "INBOX", "bob@seed3")
        run_check("no-quota-reject-for-aliases", c_no_quota_reject_alias, ctx)
        run_check("overquota-reject-bob", c_overquota, ctx)
        run_check("imap-getquotaroot-bob", c_quotaroot, ctx)
        run_check("greylist-bypass-config", c_greylist_bypass, ctx)
        run_check("root-mail-alias", c_root_mail, ctx)
        run_check("sieve-filter-filtered", c_delivery, ctx, "filtered", "Filtered", "[filtered] mail")
        run_check("sieve-vacation-reply", c_vacation, ctx)
        run_check("sieve-vacation-no-phantom-folders", c_vacation_no_phantom_folders, ctx)
        run_check("submission-465-delivery", c_submission_delivery, ctx, "s465", "465 submission")
        run_check("submission-587-delivery", c_submission_delivery, ctx, "s587", "587 submission")
        run_check("permitted-sender-allowed", c_submission_delivery, ctx, "team_ok", "alice as team@")
        run_check("permitted-sender-denied", c_sender_denied, ctx)
    if want("imapsieve"):
        print("== imapsieve")
        run_check("imapsieve-move-run", c_imapsieve, ctx)
        run_check("imapsieve-spam-learn", c_imapsieve_result, ctx, "spam")
        run_check("imapsieve-ham-learn", c_imapsieve_result, ctx, "ham")
        run_check("fts-search", c_fts, ctx)
    if not args.no_cleanup and (want("mail") or want("imapsieve")):
        run_check("cleanup-test-mail", c_cleanup, ctx)

    ctx.net.close()
    summary = {}
    for r in RESULTS:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    report = {"meta": {"host": args.host, "hostname": args.hostname, "domain": ctx.domain,
                       "token": ctx.tok, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "path": "peer " + args.peer_ssh if args.peer_ssh else ("jump " + args.jump if args.jump else "direct")},
              "summary": summary, "results": RESULTS}
    print()
    print("%-4s  %-38s %s" % ("STAT", "CHECK", "DETAIL"))
    print("-" * 110)
    for r in RESULTS:
        print("%-4s  %-38s %s" % (r["status"], r["check"], r["detail"][:200]))
    print("-" * 110)
    print("summary: " + ", ".join("%s=%d" % kv for kv in sorted(summary.items())))
    text = redact(json.dumps(report, indent=2, ensure_ascii=False))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print("JSON-REPORT-BEGIN")
    print(text)
    print("JSON-REPORT-END")
    return 1 if summary.get("FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
