#!/usr/bin/python3
"""Read-only verification of a Mail-in-a-Box install on Ubuntu 26.04.

Run as root on the box after setup/start.sh completes:

    sudo python3 tests/verify_2604.py            # summary
    sudo python3 tests/verify_2604.py -v         # detail for every check
    sudo python3 tests/verify_2604.py --step 3   # one step only

Changes nothing: no mail is sent, no config is written, no service restarted.
Exit status is 1 if anything FAILed. Checks that cannot be automated safely
(delivering mail, dragging messages between folders, issuing a certificate)
stay in TESTING-26.04.md.
"""

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
CHECKS = []


def check(step, name):
    def deco(fn):
        CHECKS.append((step, name, fn))
        return fn
    return deco


def run(*cmd, timeout=60, stdin=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, input=stdin)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


_cache = {}


def cached(key, producer):
    if key not in _cache:
        _cache[key] = producer()
    return _cache[key]


def miab_conf():
    def load():
        env = {}
        try:
            with open("/etc/mailinabox.conf", encoding="utf-8") as f:
                for line in f:
                    if "=" in line and not line.startswith("#"):
                        k, v = line.strip().split("=", 1)
                        env[k] = v.strip('"')
        except OSError:
            pass
        return env
    return cached("conf", load)


def storage_root():
    return miab_conf().get("STORAGE_ROOT", "/home/user-data")


def doveconf():
    def load():
        rc, out = run("doveconf", "-n")
        return out if rc == 0 else None
    return cached("doveconf", load)


def dc_block(path):
    """Return the body lines of a (possibly nested) doveconf -n block.

    path is a list of block headers, e.g. ["protocol imap", "mail_plugins"].
    """
    text = doveconf()
    if text is None:
        return None
    lines = text.splitlines()
    depth = 0
    for want in path:
        header = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if len(line) - len(line.lstrip()) != depth * 2:
                continue
            if stripped == want + " {":
                header = i
                break
        if header is None:
            return None
        body, j = [], header + 1
        for j in range(header + 1, len(lines)):
            indent = len(lines[j]) - len(lines[j].lstrip())
            if lines[j].strip() == "}" and indent == depth * 2:
                break
            body.append(lines[j])
        lines = body
        depth += 1
    return lines


def dc_keys(path):
    body = dc_block(path)
    if body is None:
        return None
    out = {}
    for line in body:
        if len(line) - len(line.lstrip()) != (len(path)) * 2:
            continue
        if "=" in line:
            k, v = line.strip().split("=", 1)
            out[k.strip()] = v.strip()
    return out


def dc_setting(name):
    text = doveconf()
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith(name + " ="):
            return line.split("=", 1)[1].strip()
    return None


def dpkg_installed(pkg):
    rc, out = run("dpkg-query", "-W", "-f", "${Status}", pkg)
    return rc == 0 and "install ok installed" in out


def listening():
    def load():
        rc, out = run("ss", "-lntuH")
        rows = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                local = parts[4]
                host, _, port = local.rpartition(":")
                rows.append((host.strip("[]"), port))
        return rows
    return cached("ss", load)


def port_open(port, host=None):
    for h, p in listening():
        if p == str(port) and (host is None or h == host):
            return True
    return False


def mail_users():
    def load():
        db = os.path.join(storage_root(), "mail/users.sqlite")
        if not os.path.exists(db):
            return []
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute("SELECT email, quota FROM users").fetchall()
            con.close()
            return rows
        except sqlite3.Error:
            return []
    return cached("users", load)


# ---------------------------------------------------------------- step 1

@check(1, "Running Ubuntu 26.04")
def c_os():
    rel = {}
    with open("/etc/os-release", encoding="utf-8") as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                rel[k] = v.strip('"')
    v = rel.get("VERSION_ID")
    return (PASS if v == "26.04" else FAIL), f"VERSION_ID={v}"


@check(1, "No third-party PHP repository")
def c_no_php_ppa():
    hits = []
    for d in ("/etc/apt/sources.list.d",):
        for name in os.listdir(d) if os.path.isdir(d) else []:
            body = open(os.path.join(d, name), encoding="utf-8", errors="replace").read()
            if "ondrej" in body or "sury" in body:
                hits.append(name)
    return (FAIL if hits else PASS), ", ".join(hits) or "none"


@check(1, "chrony active, timesyncd gone")
def c_chrony():
    rc, _ = run("systemctl", "is-active", "--quiet", "chrony")
    tsd = dpkg_installed("systemd-timesyncd")
    if rc != 0:
        return FAIL, "chrony is not active"
    if tsd:
        return WARN, "chrony active but systemd-timesyncd still installed"
    return PASS, "chrony active, timesyncd removed"


@check(1, "duplicity can import b2sdk and boto3")
def c_duplicity_sdks():
    rc, out = run("python3", "-c",
                  "import b2sdk, boto3; print(b2sdk.__version__, boto3.__version__)")
    return (PASS if rc == 0 else FAIL), out.strip()


@check(1, "Nothing pip-installed into system python")
def c_no_system_pip():
    import glob
    names = {"b2sdk", "boto3", "email_validator"}
    bad = []
    for d in glob.glob("/usr/local/lib/python3*/dist-packages/*"):
        base = os.path.basename(d).split("-")[0].lower()
        if base in names:
            bad.append(os.path.basename(d))
    return (WARN if bad else PASS), ", ".join(sorted(set(bad))) or "clean"


# ---------------------------------------------------------------- step 2

@check(2, "Roundcube 1.7.x installed with public_html docroot")
def c_rc_version():
    d = "/usr/local/lib/roundcubemail"
    if not os.path.isdir(d):
        return FAIL, "not installed"
    if not os.path.isfile(os.path.join(d, "public_html/index.php")):
        return FAIL, "public_html/index.php missing"
    ver = ""
    try:
        body = open(os.path.join(d, "program/include/iniset.php"), encoding="utf-8",
                    errors="replace").read()
        m = re.search(r"RCMAIL_VERSION',\s*'([^']+)'", body)
        ver = m.group(1) if m else ""
    except OSError:
        pass
    if not ver:
        try:
            ver = open(os.path.join(d, "version"), encoding="utf-8").read().split(":")[0]
        except OSError:
            ver = "unknown"
    ok = ver.startswith("1.7")
    return (PASS if ok else FAIL), f"version {ver}"


@check(2, "Roundcube web installer removed")
def c_rc_installer():
    gone = not os.path.isdir("/usr/local/lib/roundcubemail/installer")
    return (PASS if gone else FAIL), "installer/ absent" if gone else "installer/ still present"


@check(2, "html5_notifier replaced by bundled newmail_notifier")
def c_rc_plugins():
    cfg = "/usr/local/lib/roundcubemail/config/config.inc.php"
    if not os.path.isfile(cfg):
        return SKIP, "no config.inc.php"
    body = open(cfg, encoding="utf-8", errors="replace").read()
    bad = "html5_notifier" in body
    good = "newmail_notifier" in body
    if bad:
        return FAIL, "html5_notifier still enabled"
    return (PASS if good else WARN), "newmail_notifier enabled" if good else "neither plugin enabled"


@check(2, "php-imap not installed")
def c_no_php_imap():
    rc, out = run("bash", "-c", "dpkg-query -W -f '${Package}\\n' 'php*-imap' 2>/dev/null")
    hits = [l for l in out.split() if l]
    return (FAIL if hits else PASS), ", ".join(hits) or "absent"


@check(2, "nginx config parses and http2 is on")
def c_nginx():
    rc, out = run("nginx", "-t")
    if rc != 0:
        return FAIL, out.strip().splitlines()[-1] if out else "nginx -t failed"
    rc2, out2 = run("bash", "-c", "grep -rl 'http2 on;' /etc/nginx/ 2>/dev/null | head -1")
    return (PASS if out2.strip() else WARN), "nginx -t ok; http2 directive " + ("found" if out2.strip() else "not found")


@check(2, "Roundcube static assets and blocked paths")
def c_rc_http():
    host = miab_conf().get("PRIMARY_HOSTNAME")
    if not host or not shutil.which("curl"):
        return SKIP, "no PRIMARY_HOSTNAME or curl"
    want = [
        ("/mail/static.php/program/js/app.min.js", {"200"}),
        ("/mail/static.php/skins/elastic/styles/styles.min.css", {"200"}),
        ("/mail/installer.php", {"404"}),
        ("/mail/config/config.inc.php", {"403"}),
        ("/mail/logs/errors.log", {"403", "404"}),
    ]
    bad = []
    for path, ok in want:
        rc, out = run("curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                      "--max-time", "15", f"https://{host}{path}")
        code = out.strip()[-3:]
        if code not in ok:
            bad.append(f"{path}={code} (want {'/'.join(sorted(ok))})")
    return (FAIL if bad else PASS), "; ".join(bad) or "all 5 paths as expected"


# ---------------------------------------------------------------- step 3

@check(3, "doveconf -n parses")
def c_doveconf():
    rc, out = run("doveconf", "-n")
    if rc != 0:
        return FAIL, out.strip().splitlines()[-1] if out else "doveconf failed"
    warns = [l for l in out.splitlines() if "Warning" in l or "Deprecated" in l]
    return (WARN if warns else PASS), warns[0] if warns else "clean"


@check(3, "Dovecot is 2.4.x")
def c_dovecot_version():
    rc, out = run("dovecot", "--version")
    v = out.strip().split()[0] if out.strip() else "?"
    return (PASS if v.startswith("2.4") else FAIL), v


@check(3, "Global mail_plugins has quota, fts and fts_flatcurve")
def c_global_plugins():
    keys = dc_keys(["mail_plugins"])
    if keys is None:
        return FAIL, "no global mail_plugins block in doveconf -n"
    want = {"quota", "fts", "fts_flatcurve"}
    have = {k for k, v in keys.items() if v == "yes"}
    missing = want - have
    if missing:
        return FAIL, ("missing " + ", ".join(sorted(missing)) +
                      " -- same-scope boolean maps are NOT merging, see TESTING-26.04.md")
    return PASS, "quota, fts, fts_flatcurve all yes"


@check(3, "protocol imap has imap_quota and imap_sieve")
def c_imap_plugins():
    keys = dc_keys(["protocol imap", "mail_plugins"])
    if keys is None:
        return FAIL, "no mail_plugins under protocol imap"
    have = {k for k, v in keys.items() if v == "yes"}
    missing = {"imap_quota", "imap_sieve"} - have
    return (FAIL if missing else PASS), ("missing " + ", ".join(sorted(missing))) if missing else "both yes"


@check(3, "protocol lmtp has sieve")
def c_lmtp_plugins():
    keys = dc_keys(["protocol lmtp", "mail_plugins"])
    if keys is None:
        return FAIL, "no mail_plugins under protocol lmtp"
    return (PASS if keys.get("sieve") == "yes" else FAIL), str(keys)


@check(3, "Maildir storage under STORAGE_ROOT, inbox not in /var/mail")
def c_mail_location():
    driver = dc_setting("mail_driver")
    path = dc_setting("mail_path")
    inbox = dc_setting("mail_inbox_path")
    problems = []
    if driver != "maildir":
        problems.append(f"mail_driver={driver}")
    if not path or storage_root() not in path:
        problems.append(f"mail_path={path}")
    if inbox:
        problems.append(f"mail_inbox_path={inbox} (should be unset)")
    if os.path.isdir("/var/mail") and os.listdir("/var/mail"):
        problems.append("/var/mail is not empty")
    return (FAIL if problems else PASS), "; ".join(problems) or f"{driver}:{path}"


@check(3, "TLS settings use the 2.4 names")
def c_dovecot_ssl():
    wanted = {
        "ssl": "required",
        "ssl_min_protocol": "TLSv1.2",
    }
    problems = []
    for k, v in wanted.items():
        got = dc_setting(k)
        if got != v:
            problems.append(f"{k}={got}")
    for k in ("ssl_server_cert_file", "ssl_server_key_file", "ssl_server_dh_file"):
        got = dc_setting(k)
        if not got or not os.path.exists(got):
            problems.append(f"{k}={got}")
    return (FAIL if problems else PASS), "; ".join(problems) or "cert/key/dh present, ssl=required"


@check(3, "Cleartext auth disabled")
def c_auth_cleartext():
    v = dc_setting("auth_allow_cleartext")
    return (PASS if v in (None, "no") else FAIL), f"auth_allow_cleartext={v or 'default (no)'}"


@check(3, "Sieve scripts declared, spam before global_before")
def c_sieve_scripts():
    text = doveconf()
    if text is None:
        return SKIP, "doveconf unavailable"
    order = re.findall(r"^sieve_script (\S+) \{", text, re.M)
    want = {"spam-global", "global-before", "global-after", "personal"}
    missing = want - set(order)
    if missing:
        return FAIL, "missing sieve_script " + ", ".join(sorted(missing))
    if "spam-global" in order and "global-before" in order:
        if order.index("spam-global") > order.index("global-before"):
            return WARN, "global-before is declared before spam-global; check precedence"
    return PASS, " -> ".join(order)


@check(3, "Quota root uses the maildir driver")
def c_quota_root():
    text = doveconf()
    if text is None:
        return SKIP, "doveconf unavailable"
    m = re.search(r"^quota (\S+) \{", text, re.M)
    if not m:
        return FAIL, "no quota root in doveconf -n"
    keys = dc_keys([f"quota {m.group(1)}"]) or {}
    drv = keys.get("driver", "count (default)")
    return (PASS if drv == "maildir" else WARN), f"quota root '{m.group(1)}' driver={drv}"


@check(3, "Per-user quota is actually enforced")
def c_quota_enforced():
    users = [(e, q) for e, q in mail_users() if q and q != "0"]
    if not users:
        return SKIP, "no user has a quota set; set one and re-run"
    bad = []
    for email, quota in users[:5]:
        rc, out = run("doveadm", "quota", "get", "-u", email)
        if rc != 0:
            bad.append(f"{email}: doveadm failed")
            continue
        limit = ""
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "User" and parts[1] == "quota":
                limit = parts[3]
        if limit in ("", "-", "0"):
            bad.append(f"{email}: db quota={quota} but dovecot limit={limit or 'none'}")
    if bad:
        return FAIL, ("; ".join(bad) +
                      " -- userdb_quota_storage_size is not taking effect")
    return PASS, f"{len(users[:5])} user(s) checked, limits present"


@check(3, "IMAPS/POP3S up, cleartext only on loopback")
def c_dovecot_ports():
    problems = []
    if not port_open(993):
        problems.append("993 not listening")
    if not port_open(995):
        problems.append("995 not listening")
    ext143 = [h for h, p in listening() if p == "143" and h not in ("127.0.0.1", "::1")]
    if ext143:
        problems.append("143 listening on " + ", ".join(ext143))
    if not port_open(143, "127.0.0.1"):
        problems.append("143 not on 127.0.0.1 (Nextcloud login needs it)")
    if port_open(110):
        problems.append("110 is listening")
    return (FAIL if problems else PASS), "; ".join(problems) or "993+995 public, 143 loopback only, 110 closed"


@check(3, "inotify max_user_instances raised")
def c_inotify():
    try:
        v = int(open("/proc/sys/fs/inotify/max_user_instances", encoding="utf-8").read())
    except OSError:
        return SKIP, "unreadable"
    return (PASS if v >= 1024 else FAIL), str(v)


@check(3, "auth-sql.conf.ext is not world readable")
def c_authsql_mode():
    f = "/etc/dovecot/conf.d/auth-sql.conf.ext"
    if not os.path.exists(f):
        return FAIL, "missing"
    mode = os.stat(f).st_mode & 0o777
    return (PASS if mode & 0o007 == 0 else FAIL), oct(mode)


# ---------------------------------------------------------------- step 4

@check(4, "dovecot-antispam gone")
def c_no_antispam():
    return (FAIL if dpkg_installed("dovecot-antispam") else PASS), "absent"


@check(4, "spampd configured in /etc/spampd.cfg")
def c_spampd_cfg():
    f = "/etc/spampd.cfg"
    if not os.path.isfile(f):
        return FAIL, "missing"
    body = open(f, encoding="utf-8", errors="replace").read()
    want = {"relayport": "10026", "maxsize": "2000", "local-only": "0"}
    problems = []
    for k, v in want.items():
        m = re.search(rf"^{re.escape(k)}\s*[=\s]\s*(\S+)", body, re.M)
        if not m or m.group(1) != v:
            problems.append(f"{k}={m.group(1) if m else 'unset'}")
    if not port_open(10025):
        problems.append("not listening on 10025")
    return (FAIL if problems else PASS), "; ".join(problems) or "relayport/maxsize/local-only ok"


@check(4, "spamassassin rule updates scheduled")
def c_sa_timer():
    rc, _ = run("systemctl", "is-enabled", "--quiet", "spamassassin-maintenance.timer")
    return (PASS if rc == 0 else WARN), "timer enabled" if rc == 0 else "timer not enabled"


@check(4, "IMAPSieve scripts compiled and wrappers executable")
def c_imapsieve_files():
    problems = []
    for f in ("/usr/lib/dovecot/sieve/report-spam.sieve",
              "/usr/lib/dovecot/sieve/report-ham.sieve"):
        if not os.path.isfile(f):
            problems.append(f"missing {f}")
        elif not os.path.isfile(f.replace(".sieve", ".svbin")):
            problems.append(f"not compiled: {os.path.basename(f)}")
    for f in ("/usr/lib/dovecot/sieve-pipe/sa-learn-spam.sh",
              "/usr/lib/dovecot/sieve-pipe/sa-learn-ham.sh"):
        if not os.access(f, os.X_OK):
            problems.append(f"not executable: {f}")
    return (FAIL if problems else PASS), "; ".join(problems) or "4 files present and compiled"


@check(4, "IMAPSieve wired up in dovecot config")
def c_imapsieve_conf():
    text = doveconf()
    if text is None:
        return SKIP, "doveconf unavailable"
    problems = []
    for want in ("mailbox Spam {", "mailbox Junk {",
                 "imapsieve_from Spam {", "imapsieve_from Junk {"):
        if want not in text:
            problems.append("no " + want.rstrip(" {"))
    if "sieve_pipe_bin_dir" not in text:
        problems.append("sieve_pipe_bin_dir unset")
    return (FAIL if problems else PASS), "; ".join(problems) or "spam/ham triggers present"


@check(4, "Bayes database reachable by the mail process")
def c_bayes_perms():
    d = os.path.join(storage_root(), "mail/spamassassin")
    if not os.path.isdir(d):
        return FAIL, "missing " + d
    st = os.stat(d)
    import grp
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    mode = st.st_mode & 0o777
    ok = group == "spampd" and mode & 0o070 == 0o070
    return (PASS if ok else WARN), f"dir {oct(mode)} group={group}"


# ---------------------------------------------------------------- step 5

@check(5, "flatcurve installed, fts-xapian gone")
def c_flatcurve_pkgs():
    problems = []
    if not dpkg_installed("dovecot-flatcurve"):
        problems.append("dovecot-flatcurve not installed")
    if dpkg_installed("dovecot-fts-xapian"):
        problems.append("dovecot-fts-xapian still installed")
    if os.path.exists("/etc/dovecot/conf.d/90-plugin-fts.conf"):
        problems.append("stale 90-plugin-fts.conf")
    return (FAIL if problems else PASS), "; ".join(problems) or "flatcurve only"


@check(5, "Trash/Junk/Spam excluded from autoindexing")
def c_fts_exclusions():
    text = doveconf()
    if text is None:
        return SKIP, "doveconf unavailable"
    missing = [m for m in ("Trash", "Junk", "Spam")
               if not re.search(rf"mailbox {m} \{{[^}}]*fts_autoindex = no", text, re.S)]
    return (WARN if missing else PASS), ("no exclusion for " + ", ".join(missing)) if missing else "all three excluded"


@check(5, "Attachment decoder works")
def c_decode2text():
    f = "/usr/lib/dovecot/decode2text.sh"
    if not os.access(f, os.X_OK):
        return FAIL, "missing or not executable"
    if not os.path.exists("/usr/lib/dovecot/xml2text"):
        return WARN, "decode2text present but xml2text missing (OOXML/ODF will fail)"
    rc, out = run(f)
    ok = rc == 0 and "application/pdf" in out
    missing = [t for t in ("pdftotext", "catdoc", "unzip") if not shutil.which(t)]
    if missing:
        return WARN, "decoder ok but missing tools: " + ", ".join(missing)
    return (PASS if ok else FAIL), "format table returned" if ok else out.strip()[:120]


# ---------------------------------------------------------------- step 6

@check(6, "PHP 8.5 with OPcache")
def c_php():
    rc, out = run("php", "-v")
    ver = out.split()[1] if rc == 0 and len(out.split()) > 1 else "?"
    rc2, mods = run("php", "-m")
    opcache = "Zend OPcache" in mods or "opcache" in mods.lower()
    problems = []
    if not ver.startswith("8.5"):
        problems.append(f"php {ver}")
    if not opcache:
        problems.append("OPcache not loaded")
    return (FAIL if problems else PASS), "; ".join(problems) or f"php {ver} with OPcache"


@check(6, "php-fpm pools are running and their users exist")
def c_fpm_pools():
    import glob
    problems = []
    pools = glob.glob("/etc/php/*/fpm/pool.d/*.conf")
    if not pools:
        return FAIL, "no pool.d/*.conf found"
    names = []
    for f in pools:
        user = None
        try:
            for line in open(f, encoding="utf-8"):
                line = line.strip()
                if line.startswith("user") and "=" in line:
                    user = line.split("=", 1)[1].strip()
        except OSError as e:
            problems.append(f"{os.path.basename(f)}: {e}")
            continue
        if not user:
            problems.append(f"{os.path.basename(f)}: no user setting")
        elif run("id", "-u", user)[0] != 0:
            problems.append(f"{os.path.basename(f)}: user {user} does not exist")
        else:
            names.append(user)
    ver = pools[0].split("/etc/php/", 1)[1].split("/", 1)[0]
    rc, out = run("systemctl", "is-active", "php%s-fpm" % ver)
    if out.strip() != "active":
        problems.append("php-fpm " + (out.strip() or "state unknown"))
    return (FAIL if problems else PASS), "; ".join(problems) or ", ".join(sorted(set(names)))


@check(6, "Nextcloud 33 or newer, user_external enabled")
def c_nextcloud():
    occ = "/usr/local/lib/nextcloud/cloud/occ"
    if not os.path.exists(occ):
        occ = "/usr/local/lib/owncloud/occ"
    if not os.path.exists(occ):
        return SKIP, "occ not found"
    rc, out = run("sudo", "-u", "nextcloud_php", "php", occ, "status", "--no-warnings")
    m = re.search(r"versionstring:\s*(\S+)", out)
    ver = m.group(1) if m else "?"
    major = int(ver.split(".")[0]) if ver[:1].isdigit() else 0
    rc2, apps = run("sudo", "-u", "nextcloud_php", "php", occ, "app:list")
    ue = "user_external" in apps.split("Disabled:")[0]
    problems = []
    if major < 33:
        problems.append(f"version {ver}")
    if not ue:
        problems.append("user_external not enabled")
    return (FAIL if problems else PASS), "; ".join(problems) or f"Nextcloud {ver}, user_external enabled"


@check(6, "No php8.0 leftovers or apt holds")
def c_no_php80():
    rc, out = run("bash", "-c", "dpkg-query -W -f '${Package}\\n' 'php8.0*' 2>/dev/null")
    holds_rc, holds = run("apt-mark", "showhold")
    problems = [l for l in out.split() if l]
    if holds.strip():
        problems.append("held: " + holds.strip().replace("\n", " "))
    return (WARN if problems else PASS), "; ".join(problems) or "clean"


# ---------------------------------------------------------------- step 7

@check(7, "Management virtualenv on Python 3.14 with its wheels")
def c_venv():
    py = "/usr/local/lib/mailinabox/env/bin/python"
    if not os.path.exists(py):
        return FAIL, "virtualenv missing"
    rc, out = run(py, "-c",
                  "import sys, cryptography, psutil, PIL;"
                  "print('.'.join(map(str,sys.version_info[:2])), cryptography.__version__,"
                  " psutil.__version__, PIL.__version__)")
    if rc != 0:
        return FAIL, out.strip().splitlines()[-1] if out else "import failed"
    parts = out.split()
    ok = parts[0] == "3.14"
    return (PASS if ok else WARN), "python " + " ".join(parts)


@check(7, "Management daemon running")
def c_miab_service():
    rc, _ = run("systemctl", "is-active", "--quiet", "mailinabox")
    return (PASS if rc == 0 else FAIL), "active" if rc == 0 else "not active"


@check(7, "certbot present")
def c_certbot():
    rc, out = run("certbot", "--version")
    return (PASS if rc == 0 else FAIL), out.strip()


@check(7, "SSHFP uses IANA algorithm numbers and no DSA")
def c_sshfp():
    host = miab_conf().get("PRIMARY_HOSTNAME")
    if not host or not shutil.which("dig"):
        return SKIP, "no PRIMARY_HOSTNAME or dig"
    rc, out = run("dig", "+short", "SSHFP", host, "@127.0.0.1")
    algs = sorted({line.split()[0] for line in out.splitlines() if line.split()})
    if not algs:
        return WARN, "no SSHFP records returned by the local resolver"
    if "2" in algs:
        return FAIL, "DSA (2) record present: " + " ".join(algs)
    unexpected = [a for a in algs if a not in ("1", "3", "4")]
    if unexpected:
        return WARN, "unexpected algorithm numbers: " + " ".join(algs)
    return PASS, "algorithms " + " ".join(algs)


# ---------------------------------------------------------------- step 8

@check(8, "SSH geo-filter is wired up and sshd-session links libwrap")
def c_geoip_ssh():
    problems = []
    for f in ("/etc/hosts.allow", "/etc/hosts.deny"):
        if not os.path.exists(f):
            problems.append(f"missing {f}")
    try:
        if "sshd: ALL" not in open("/etc/hosts.deny", encoding="utf-8").read():
            problems.append("no sshd line in hosts.deny")
        if "geoipfilter.sh" not in open("/etc/hosts.allow", encoding="utf-8").read():
            problems.append("no geoipfilter line in hosts.allow")
    except OSError:
        pass
    session = "/usr/lib/openssh/sshd-session"
    if os.path.exists(session):
        rc, out = run("ldd", session)
        if "libwrap" not in out:
            problems.append("sshd-session is NOT linked against libwrap: filter is inert")
    else:
        rc, out = run("ldd", "/usr/sbin/sshd")
        if "libwrap" not in out:
            problems.append("neither sshd nor sshd-session links libwrap")
    return (FAIL if problems else PASS), "; ".join(problems) or "hosts.allow + libwrap ok"


@check(8, "postfix-tlspol built, running and reachable")
def c_tlspol():
    problems = []
    if not os.path.exists("/usr/bin/postfix-tlspol"):
        problems.append("binary missing (Go build did not run)")
    rc, out = run("systemctl", "is-active", "postfix-tlspol.service")
    if out.strip() != "active":
        problems.append("service " + (out.strip() or "state unknown"))
    if not port_open(8642, "127.0.0.1"):
        problems.append("nothing listening on 127.0.0.1:8642")
    rc, out = run("postconf", "-h", "smtp_tls_policy_maps")
    maps = out.strip()
    if "8642" not in maps:
        problems.append("smtp_tls_policy_maps=" + (maps or "<empty>"))
    elif problems:
        problems.append("policy maps point at a dead socketmap, outbound mail will defer")
    return (FAIL if problems else PASS), "; ".join(problems) or "socketmap on 127.0.0.1:8642"


@check(8, "ucf-created /etc/default files exist")
def c_etc_default():
    missing = [f for f in ("/etc/default/postgrey", "/etc/default/rkhunter")
               if not os.path.exists(f)]
    return (FAIL if missing else PASS), ", ".join(missing) or "postgrey + rkhunter present"


@check(8, "mail.log and syslog are being written")
def c_logs():
    import time
    problems = []
    for f in ("/var/log/mail.log", "/var/log/syslog"):
        if not os.path.exists(f):
            problems.append(f"missing {f}")
        elif time.time() - os.stat(f).st_mtime > 86400:
            problems.append(f"{f} stale (>24h)")
    return (FAIL if problems else PASS), "; ".join(problems) or "both fresh"


@check(8, "ipset blacklist loaded")
def c_ipset():
    rc, out = run("ipset", "list", "-n")
    if rc != 0:
        return SKIP, "ipset unavailable"
    sets = [s for s in out.split() if s]
    return (PASS if sets else WARN), ", ".join(sets) or "no sets loaded"


@check(8, "fail2ban running with jails")
def c_fail2ban():
    rc, out = run("fail2ban-client", "status")
    if rc != 0:
        return FAIL, "fail2ban-client status failed"
    m = re.search(r"Jail list:\s*(.*)", out)
    jails = [j.strip() for j in m.group(1).split(",")] if m else []
    return (PASS if jails else FAIL), f"{len(jails)} jails: " + ", ".join(jails[:8])


# ---------------------------------------------------------------- step 9

@check(9, "Timezone set via systemd, /etc/timezone not relied on")
def c_timezone():
    rc, out = run("timedatectl", "show", "-p", "Timezone", "--value")
    tz = out.strip()
    note = "" if not os.path.exists("/etc/timezone") else " (/etc/timezone still present)"
    return (PASS if rc == 0 and tz else FAIL), tz + note


@check(9, "Core services active")
def c_services():
    required = ["dovecot", "postfix", "nginx", "nsd", "unbound", "opendmarc",
                "spampd", "postgrey", "fail2ban", "mailinabox", "php8.5-fpm"]
    optional = ["stunnel@miabrelay", "dmarc_report_viewer", "munin-node", "chrony"]
    inactive, missing, opt_down = [], [], []
    for s in required + optional:
        rc, _ = run("systemctl", "is-active", "--quiet", s)
        if rc == 0:
            continue
        rc2, out2 = run("systemctl", "list-unit-files", s + ".service")
        known = s in out2
        if s in optional:
            opt_down.append(s)
        elif known:
            inactive.append(s)
        else:
            missing.append(s)
    if inactive or missing:
        parts = []
        if inactive:
            parts.append("inactive: " + ", ".join(inactive))
        if missing:
            parts.append("no unit: " + ", ".join(missing))
        return FAIL, "; ".join(parts)
    note = f"{len(required)} required services active"
    if opt_down:
        note += "; optional not running: " + ", ".join(opt_down)
        return WARN, note
    return PASS, note


# ----------------------------------------------------------------- main

COLOR = {PASS: "\033[32m", FAIL: "\033[31m", WARN: "\033[33m", SKIP: "\033[90m"}
RESET = "\033[0m"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show the detail line for passing checks too")
    ap.add_argument("--step", type=int, action="append",
                    help="only run checks for this step (repeatable)")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("warning: not running as root; several checks will fail or skip\n",
              file=sys.stderr)

    tally = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0}
    last_step = None
    for step, name, fn in CHECKS:
        if args.step and step not in args.step:
            continue
        try:
            status, detail = fn()
        except Exception as e:  # a broken check must not hide the others
            status, detail = FAIL, f"check raised {type(e).__name__}: {e}"
        tally[status] += 1
        detail = " ".join(str(detail).split())[:400]
        if step != last_step:
            print(f"\n--- step {step} ---")
            last_step = step
        c = "" if args.no_color else COLOR[status]
        r = "" if args.no_color else RESET
        line = f"  [{c}{status}{r}] {name}"
        if detail and (args.verbose or status != PASS):
            line += f"\n         {detail}"
        print(line)

    print(f"\n{tally[PASS]} passed, {tally[FAIL]} failed, "
          f"{tally[WARN]} warnings, {tally[SKIP]} skipped")
    if tally[FAIL] or tally[WARN]:
        print("See TESTING-26.04.md for the manual checks this script cannot do:")
        print("  spam/ham learning round trip, sieve ordering, certificate issuance,")
        print("  attachment search, backup restore, reboot survival.")
    return 1 if tally[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
