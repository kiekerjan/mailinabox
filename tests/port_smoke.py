#!/usr/bin/env python3
"""Rebuild tests/smoke.py from LM1337/Mail-in-a-box-26's tests/fork/smoke.py.

Usage: python3 port_smoke.py /path/to/their/smoke.py
Writes tests/smoke.py. Delete this script afterwards; it is only the record of
how the port was made.
"""
import ast, os, sys

SRC = sys.argv[1]

HEAD_DOC = '''
Smoke test for this Mail-in-a-Box fork on Ubuntu 26.04 -- python3 stdlib only.

Drives a box from outside over IMAP, POP3, submission, ManageSieve, SMTP, HTTP
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
  * Everything needs the box reachable on 443, 993, 995, 465, 587, 4190 and
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
'''

KEEP = """c_provision c_imap_login c_pop3_login c_sub465 c_sub587 c_sieve_login c_services_enabled
c_mail_log_logins c_admin_login c_admin_tmp_user_alias c_backup_status
c_dnssec c_mx_dkim c_dns_custom_roundtrip
c_nc_status c_nc_dav c_nc_dav_proxy c_roundcube c_mail_config_path
c_mail_flow c_delivery c_submission_delivery c_sender_denied c_no_quota_reject_alias c_overquota
c_quotaroot c_greylist_bypass c_root_mail c_vacation c_vacation_no_phantom_folders
c_imapsieve c_fts c_imapsieve_result c_cleanup c_api_read_only c_api_write_roundtrip
Ctx Net Remote ManageSieve run_check load_env_file send_test_mail RESULTS SECRETS
sieve_vacation SIEVE_FILTER SIEVE_FILTER_NAME SIEVE_VACATION_NAME SMTP_SNIPPET run_snippet
DEFAULT_ENV_FILE""".split()

# routes this fork's management daemon does not serve
DROP_ROUTES = {"/me", "/system/backup/status/saved", "/system/fail2ban",
               "/system/fail2ban/settings", "/system/fail2ban/report",
               "/system/munin/alerts"}

TAIL = r'''

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
        run_check("pop3s-995-login", c_pop3_login, ctx)
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
'''


def main():
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    L = src.split("\n")
    top, order = {}, []
    for node in tree.body:
        names = []
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        for n in names:
            top[n] = node
            order.append(n)

    missing = [n for n in KEEP if n not in top]
    if missing:
        print("not found in %s: %s" % (SRC, " ".join(missing)), file=sys.stderr)
        return 2

    seen, stack = set(), list(KEEP)
    while stack:
        n = stack.pop()
        if n in seen or n not in top:
            continue
        seen.add(n)
        for r in {x.id for x in ast.walk(top[n]) if isinstance(x, ast.Name)}:
            if r in top and r not in seen:
                stack.append(r)
    kept = [n for n in order if n in seen]

    chunks = []
    for n in kept:
        node = top[n]
        text = "\n".join(L[node.lineno - 1:node.end_lineno])
        if n == "API_READ_ONLY":
            text = "\n".join(l for l in text.split("\n")
                             if not any('"%s"' % r in l for r in DROP_ROUTES))
            text = text.replace('    ("GET", "/munin/", "text/html"),',
                                '    ("GET", "/dns/advanced-dns", "application/json"),\n'
                                '    ("GET", "/munin/", "text/html"),')
        elif n == "CORE_UNITS":
            text = text.replace('"named", ', '').replace(
                '"mailinabox", "cron"]',
                '"mailinabox", "cron",\n              "unbound", "dmarc_report_viewer", "postfix-tlspol"]')
        elif n == "api_shape":
            fn = ast.parse(text).body[0]
            cut = set()
            for st in ast.walk(fn):
                if (isinstance(st, ast.If) and isinstance(st.test, ast.Compare)
                        and isinstance(st.test.left, ast.Name) and st.test.left.id == "path"
                        and isinstance(st.test.comparators[0], ast.Constant)
                        and st.test.comparators[0].value in DROP_ROUTES):
                    cut.update(range(st.lineno, st.end_lineno + 1))
            text = "\n".join(l for i, l in enumerate(text.split("\n"), 1) if i not in cut)
        chunks.append(text)

    imports = [l for l in L[:min(top[n].lineno for n in kept) - 1] if l.startswith(("import", "from"))]
    out = ("#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n\"\"\""
           + HEAD_DOC + "\"\"\"\n"
           + "\n".join(imports) + "\n\n"
           + "\n\n".join(chunks) + "\n"
           + TAIL)
    dest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoke.py")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(out)
    os.chmod(dest, 0o755)
    ast.parse(out)
    print("wrote %s: %d objects, %d lines" % (dest, len(kept), out.count("\n") + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
