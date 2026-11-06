#!/usr/bin/python3
import base64, fcntl, imaplib, json, os, re, secrets, shutil, smtplib, socket, ssl, sys, time, traceback
import urllib.error, urllib.parse, urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import dkim

STATUS = "/var/lib/mailinabox/mail_roundtrip.json"
LOCK = "/run/mailinabox-mail-roundtrip.lock"
MGMT = "http://127.0.0.1:10222"
PORTS = (25, 465, 587, 993)


def load_env():
    with open("/etc/mailinabox.conf") as f:
        return dict(l.rstrip("\n").split("=", 1) for l in f if "=" in l)


def greylist_delay():
    try:
        with open("/etc/default/postgrey") as f:
            m = re.search(r"--delay=(\d+)", f.read())
        return int(m.group(1)) + 15 if m else 315
    except OSError:
        return 315


def mgmt(path, data=None):
    with open("/var/lib/mailinabox/api.key") as f:
        key = f.read().strip()
    req = urllib.request.Request(MGMT + path, urllib.parse.urlencode(data).encode() if data is not None else None)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{key}:".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def smtp_code(exc):
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return next(iter(exc.recipients.values()))[0]
    if isinstance(exc, smtplib.SMTPResponseException):
        return exc.smtp_code
    return None


class Run:
    def __init__(self, env):
        self.env = env
        self.ip = env["PUBLIC_IP"]
        self.ip6 = env.get("PUBLIC_IPV6", "").strip()
        self.host = env["PRIMARY_HOSTNAME"]
        self.box = f"mailtest@{self.host}"
        self.password = secrets.token_urlsafe(24)
        self.tag = secrets.token_hex(6)
        self.grey = greylist_delay()
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.results = []

    def check(self, name, fn):
        try:
            detail = fn()
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
        self.results.append({"check": name, "ok": detail is None, "detail": detail or ""})
        print(("ok   " if detail is None else "FAIL ") + name + (f": {detail}" if detail else ""), flush=True)

    def wait_ready(self, seconds=180):
        end = time.time() + seconds
        pending = set(PORTS)
        while pending and time.time() < end:
            for port in list(pending):
                try:
                    socket.create_connection((self.ip, port), timeout=5).close()
                    pending.discard(port)
                except OSError:
                    pass
            if pending:
                time.sleep(5)
        if pending:
            raise RuntimeError(f"ports not answering after {seconds}s: {sorted(pending)}")

    def letter(self, frm, to, subject, body="Mail-in-a-Box nightly round-trip test"):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = frm, to, f"{subject} {self.tag}"
        m["Date"], m["Message-ID"] = formatdate(localtime=True), make_msgid(domain=self.host)
        m.set_content(body)
        return m

    def s25(self, ip=None):
        s = smtplib.SMTP(ip or self.ip, 25, self.host, timeout=30)
        s.starttls(context=self.ctx)
        return s

    def s587(self):
        s = smtplib.SMTP(self.ip, 587, self.host, timeout=30)
        s.starttls(context=self.ctx)
        return s

    def s465(self):
        return smtplib.SMTP_SSL(self.ip, 465, self.host, timeout=30, context=self.ctx)

    def find(self, subject, seconds=60):
        end = time.time() + seconds
        while time.time() < end:
            with imaplib.IMAP4_SSL(self.ip, 993, ssl_context=self.ctx, timeout=30) as m:
                m.login(self.box, self.password)
                for folder in ("INBOX", "Spam"):
                    if m.select(folder, readonly=True)[0] != "OK":
                        continue
                    _, hit = m.search(None, "SUBJECT", f'"{subject} {self.tag}"')
                    if hit[0]:
                        _, data = m.fetch(hit[0].split()[-1], "(BODY.PEEK[])")
                        return folder, data[0][1]
            time.sleep(3)
        return None, None

    def expect_5xx(self, fn, exact=None):
        try:
            fn()
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPResponseException) as e:
            c = smtp_code(e)
            if c is None or not 500 <= c <= 599:
                return f"answered {c} (temporary), not a permanent refusal"
            if exact and c != exact:
                return f"refused with {c}, expected {exact}"
            return None
        return "it was accepted"

    def send25(self, msg, ip=None):
        for attempt in (1, 2):
            try:
                with self.s25(ip) as s:
                    s.send_message(msg)
                return
            except smtplib.SMTPRecipientsRefused as e:
                code, text = next(iter(e.recipients.values()))
                if attempt == 1 and code in (450, 451) and b"reylist" in text:
                    time.sleep(self.grey)
                    continue
                raise

    def dkim_result(self, raw):
        sigs = re.findall(rb"^DKIM-Signature:.*?(?=^\S)", raw, re.M | re.S)
        if not sigs:
            return "not DKIM-signed"
        algos, bad = [], []
        for i, sig in enumerate(sigs):
            a = re.search(rb"\ba=\s*([\w-]+)", sig)
            algo = a.group(1).decode() if a else "?"
            algos.append(algo)
            try:
                ok = dkim.DKIM(raw).verify(idx=i)
            except Exception as e:
                ok, algo = False, f"{algo} ({type(e).__name__}: {e})"
            if not ok:
                bad.append(algo)
        missing = [a for a in ("rsa-sha256", "ed25519-sha256") if a not in algos]
        if bad:
            return "signature doesn't verify against DNS: " + ", ".join(bad)
        if missing:
            return "missing signature: " + ", ".join(missing)
        return None

    def listed(self):
        code, text = mgmt("/mail/users")
        if code != 200:
            raise RuntimeError(f"listing users failed: {code} {text.strip()[:200]}")
        return self.box in text.split()

    def imap_login_ok(self):
        try:
            with imaplib.IMAP4_SSL(self.ip, 993, ssl_context=self.ctx, timeout=30) as m:
                m.login(self.box, self.password)
            return True
        except imaplib.IMAP4.error:
            return False

    def create_box(self):
        code, text = mgmt("/mail/users/add", {"email": self.box, "password": self.password})
        if code != 200:
            return f"API answered {code}: {text.strip()[:200]}"
        if not self.listed():
            return "API said ok, but the account isn't listed"
        if not self.imap_login_ok():
            return "account is listed, but IMAP login fails"
        return None

    def remove_box(self):
        code, text = mgmt("/mail/users/remove", {"email": self.box})
        self.wipe_maildir()
        if code != 200:
            return f"API answered {code}: {text.strip()[:200]}"
        if self.listed():
            return "API said ok, but the account is still listed"
        if self.imap_login_ok():
            return "account is removed, but IMAP login still works"
        return None

    def wipe_maildir(self):
        user, domain = self.box.split("@")
        root = os.path.realpath(os.path.join(self.env["STORAGE_ROOT"], "mail/mailboxes"))
        path = os.path.realpath(os.path.join(root, domain, user))
        if path.startswith(root + os.sep) and os.path.isdir(path):
            shutil.rmtree(path)

    def run(self):
        self.wait_ready()
        if self.listed():
            mgmt("/mail/users/remove", {"email": self.box})
        self.wipe_maildir()
        self.check("account is created through the admin API", self.create_box)
        created = self.results[-1]["ok"]
        try:
            if not created:
                return
            def inbound(ip, subject):
                def f():
                    self.send25(self.letter(self.box, self.box, subject), ip)
                    where, raw = self.find(subject)
                    if where is None:
                        return "didn't arrive"
                    if where != "INBOX":
                        score = re.search(rb"X-Spam-Status: \w+, score=(\S+)", raw)
                        return f"landed in {where} (score {score.group(1).decode() if score else '?'})"
                    return None
                return f
            self.check("mail from outside arrives in the inbox (port 25, IPv4)", inbound(self.ip, "in4"))
            if self.ip6:
                self.check("mail from outside arrives in the inbox (port 25, IPv6)", inbound(self.ip6, "in6"))

            def outbound(connect, subject):
                def f():
                    with connect() as s:
                        s.login(self.box, self.password)
                        s.send_message(self.letter(self.box, self.box, subject))
                    where, raw = self.find(subject)
                    if where != "INBOX":
                        return f"landed in {where}" if where else "didn't arrive"
                    return self.dkim_result(raw)
                return f
            self.check("submission on 587 delivers, RSA and Ed25519 DKIM verify", outbound(self.s587, "out587"))
            self.check("submission on 465 delivers, RSA and Ed25519 DKIM verify", outbound(self.s465, "out465"))

            def wrong_password():
                with self.s587() as s:
                    s.login(self.box, self.password + "x")
            self.check("a wrong password is refused", lambda: self.expect_5xx(wrong_password, exact=535))

            def spoof():
                with self.s587() as s:
                    s.login(self.box, self.password)
                    s.send_message(self.letter(f"postmaster@{self.host}", self.box, "spoof"))
            self.check("sending as another address is refused", lambda: self.expect_5xx(spoof))

            def no_login():
                with self.s587() as s:
                    s.send_message(self.letter(self.box, "relay-check@example.com", "nologin"))
            self.check("submission without login is refused", lambda: self.expect_5xx(no_login))

            def relay():
                with self.s25() as s:
                    s.send_message(self.letter(self.box, "relay-check@example.com", "relay"))
            self.check("relaying from the public address is refused", lambda: self.expect_5xx(relay))

            def root_external():
                with self.s25() as s:
                    s.send_message(self.letter(self.box, f"root@{self.host}", "root"))
            self.check("mail to root@ from outside is refused", lambda: self.expect_5xx(root_external))

            def gtube():
                body = "XJS*C4JDBQADN1.NSBN3*2IDNEN*GTUBE-STANDARD-ANTI-UBE-TEST-EMAIL*C.34X"
                self.send25(self.letter(self.box, self.box, "gtube", body))
                where, raw = self.find("gtube")
                if where != "Spam":
                    return f"landed in {where}" if where else "didn't arrive"
                return None if b"X-Spam-Flag: YES" in raw else "in Spam but not flagged by SpamAssassin"
            self.check("spam is filed in Spam", gtube)
        finally:
            if created or self.listed():
                self.check("account is removed through the admin API", self.remove_box)
            else:
                self.wipe_maildir()


def write_status(results, crashed=None):
    failed = [r["check"] for r in results if not r["ok"]]
    if crashed:
        failed.append(f"test run crashed: {crashed}")
    tmp = STATUS + ".new"
    with open(tmp, "w") as f:
        json.dump({"finished": int(time.time()), "results": results, "failed": failed}, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATUS)
    return failed


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("already running")
        return 0
    run = None
    try:
        run = Run(load_env())
        run.run()
        failed = write_status(run.results)
    except Exception as e:
        traceback.print_exc()
        failed = write_status(run.results if run else [], crashed=f"{type(e).__name__}: {e}")
    print("FAIL: " + "; ".join(failed) if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
