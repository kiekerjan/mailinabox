#!/usr/local/lib/mailinabox/env/bin/python

# Run daily maintenance tasks
########################################################################

from editconf import do_editconf
import json, logging, os, sqlite3, subprocess, time, utils

DAILY_CHECKS_FILE = "/var/lib/mailinabox/daily_checks.json"


def _run(*cmd, timeout=60):
	try:
		p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
			env={"PATH": "/sbin:/bin:/usr/sbin:/usr/bin", "LC_ALL": "C"})
		return p.returncode, (p.stdout or "") + (p.stderr or "")
	except FileNotFoundError:
		return 127, f"{cmd[0]}: not found"
	except subprocess.TimeoutExpired:
		return 124, f"{cmd[0]}: timed out"


def check_webmail_paths(env):
	host = env["PRIMARY_HOSTNAME"]
	want = [
		("/mail/static.php/program/js/app.min.js", {"200"}),
		("/mail/static.php/skins/elastic/styles/styles.min.css", {"200"}),
		("/mail/installer.php", {"404"}),
		("/mail/config/config.inc.php", {"403"}),
		("/mail/logs/errors.log", {"403", "404"}),
	]
	bad = []
	for path, ok in want:
		rc, out = _run("curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "15", f"https://{host}{path}")
		code = out.strip()[-3:] if rc == 0 else "no response"
		if code not in ok:
			bad.append(f"{path} gave {code}, expected {'/'.join(sorted(ok))}")
	return "; ".join(bad) or None


def check_quota_enforced(env):
	db = os.path.join(env["STORAGE_ROOT"], "mail/users.sqlite")
	con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
	try:
		users = con.execute("SELECT email, quota FROM users WHERE quota IS NOT NULL AND quota NOT IN ('', '0')").fetchall()
	finally:
		con.close()
	bad = []
	for email, quota in users:
		rc, out = _run("doveadm", "quota", "get", "-u", email)
		if rc != 0:
			bad.append(f"{email}: doveadm quota get failed")
			continue
		limit = ""
		for line in out.splitlines():
			parts = line.split()
			if len(parts) >= 4 and parts[1] == "STORAGE":
				limit = parts[3]
		if limit in ("", "-", "0"):
			bad.append(f"{email} has quota {quota} but Dovecot enforces none")
	return "; ".join(bad) or None


DAILY_CHECKS = [
	("webmail serves its assets and blocks its private paths", check_webmail_paths),
	("mailbox quotas are enforced by Dovecot", check_quota_enforced),
]


def do_daily_checks(env):
	logging.debug("Do daily checks")
	results = []
	for name, fn in DAILY_CHECKS:
		try:
			detail = fn(env)
		except Exception as e:
			detail = f"check raised {type(e).__name__}: {e}"
		results.append({"check": name, "ok": detail is None, "detail": detail or ""})
	failed = [r["check"] for r in results if not r["ok"]]
	tmp = DAILY_CHECKS_FILE + ".new"
	with open(tmp, "w", encoding="utf-8") as f:
		json.dump({"finished": int(time.time()), "results": results, "failed": failed}, f)
	os.chmod(tmp, 0o644)
	os.replace(tmp, DAILY_CHECKS_FILE)


def do_daily_maintenance(env):
	# This function collects all other maintenance functions

	logging.debug("Do daily maintenance")

	try:
		do_webmail_maintenance(env)
	finally:
		do_daily_checks(env)


def do_webmail_maintenance(env):
	logging.debug("Do webmail maintenance")
	config = utils.load_settings(env)

	# Handle custom front logo of the Roundcube webmail
	skin_logo = config.get("webmail", {}).get("skin_logo", None)
	webmail_file = "/usr/local/lib/roundcubemail/config/config.inc.php"
	remove = True

	if skin_logo:
		if isinstance(skin_logo, str):
			do_editconf([webmail_file, f"$config['skin_logo']='{skin_logo}';"])
			remove = False
		else:
			logging.debug("webmail.skin_logo configured but not a string")

	if remove:
		do_editconf([webmail_file, "-e", "$config['skin_logo']="])


if __name__ == "__main__":
	env = utils.load_environment()

	do_daily_maintenance(env)
