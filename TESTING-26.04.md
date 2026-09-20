# Ubuntu 26.04 branch — manual test plan

Running list of things to verify by hand on a throwaway VPS after a full
`setup/start.sh` run. Grouped by the migration step that introduced the risk.
Items marked **[unverified]** are things that could not be checked against
anything but documentation while porting — check these first.

Suggested order: run the whole checklist top to bottom on a fresh box, then
restore a backup from the 24.04 box and run the mail/quota/sieve sections again.

Run `sudo python3 tests/verify_2604.py` first. It automates about forty of the
checks below and is read-only -- it sends no mail, writes no config and
restarts nothing. `-v` shows detail for passing checks, `--step N` limits it to
one step. Exit status is 1 if anything FAILs. Work through the items it cannot
do by hand: anything involving delivering mail, moving messages between
folders, issuing a certificate, restoring a backup or rebooting.

---

## Step 1 — base plumbing

- [ ] `setup/start.sh` refuses to run on anything but 26.04
      (`sed -i s/26.04/25.10/ /etc/os-release` on a scratch box, or just read
      the preflight output).
- [ ] No third-party PHP repo present: `ls /etc/apt/sources.list.d/` shows no
      ondrej/sury entry, and `apt-cache policy php8.5-cli` resolves to
      `resolute/main`.
- [ ] `apt-get update` produces no `releaseinfo-change` prompt now that
      `--allow-releaseinfo-change` is gone.
- [ ] chrony replaced systemd-timesyncd:
      `systemctl is-active chrony` → active;
      `dpkg -l systemd-timesyncd` → not installed;
      `chronyc tracking` shows a sane offset;
      `chronyc -N sources` shows the Ubuntu NTS pool.
- [ ] Entropy seeding does not abort setup: `sudo -u pollinate /usr/bin/pollinate -q -r`
      exits 0. 26.04 added an AppArmor profile for pollinate and a unit that
      runs it as the pollinate user; as root it fails its own writability check
      on /var/cache/pollinate. A failure here is now only a warning.
- [ ] duplicity can import the distro-packaged SDKs:
      `python3 -c "import b2sdk, boto3; print(b2sdk.__version__, boto3.__version__)"`
- [ ] Actually run a backup to B2 and to S3 from the admin panel, not just the
      import check.
- [ ] `nsenter`-free sanity: `pip3 list 2>/dev/null | grep -Ei "b2sdk|boto3|email"`
      should be empty — nothing installed into system python any more.
- [ ] Setup questions still validate an email address (python3-email-validator
      is doing the work now, not a pip install).
- [ ] **Create the `v77` tag** before testing a curl bootstrap; until it exists
      `setup/bootstrap.sh` will 404 on 26.04.

## Step 2 — Roundcube 1.7.4 / nginx 1.28

- [ ] `nginx -t` passes. **[unverified]** `http2 on;` could not be tested
      locally (only nginx 1.24 available); confirm with
      `curl -sI --http2 https://$HOST/ | head -1` → `HTTP/2 200`.
- [ ] `/mail` redirects to `/mail/`, login works, inbox renders.
- [ ] **No 404s in the browser devtools Network tab on first load** — this is
      the main risk of the `public_html` move. Spot check by hand:
      `curl -sI "https://$HOST/mail/static.php/skins/elastic/styles/styles.min.css" | head -1` → 200
      `curl -sI "https://$HOST/mail/static.php/program/js/app.min.js" | head -1` → 200
- [ ] Paths that must NOT be served any more:
      `/mail/installer.php` → 404 (installer/ removed)
      `/mail/config/config.inc.php` → 403
      `/mail/logs/errors.log` → 404
      `/mail/temp/` → 404
      `/mail/vendor/autoload.php` → 404
- [ ] Every bundled plugin loads without a PHP error in
      `/var/log/roundcubemail/errors.log`:
      archive, zipdownload, password, managesieve, jqueryui, markasjunk.
- [ ] **persistent_login** (unmaintained upstream since Jan 2022) — tick "stay
      logged in", close the browser, reopen. First suspect if 1.7 misbehaves.
- [ ] **newmail_notifier** replaces html5_notifier — enable it in Settings,
      confirm the desktop notification fires on new mail.
- [ ] carddav plugin still discovers the Nextcloud addressbook.
- [ ] contextmenu (right-click on a message) and twofactor_gauthenticator.
- [ ] Password change from Roundcube actually updates `users.sqlite` and the
      new password works for IMAP.
- [ ] Send a mail with a ~50 MB attachment (checks `client_max_body_size 128M`
      survived the location rewrite).
- [ ] fail2ban still parses `/var/log/roundcubemail/errors.log`:
      `fail2ban-client status roundcube` (or whatever the jail is named).
- [ ] Roundcube sqlite WAL patch still applied:
      `grep -n "PRAGMA journal_mode" /usr/local/lib/roundcubemail/program/lib/Roundcube/db/sqlite.php`
      should show the line commented out, and
      `sqlite3 $STORAGE_ROOT/mail/roundcube/roundcube.sqlite 'PRAGMA journal_mode;'` → `wal`.

## Step 3 — Dovecot 2.4

- [ ] `doveconf -n` runs clean — **no "unknown setting" and no deprecation
      warnings**. This is the single most important check of the whole port.
      Diff it against a saved copy after any later change.
      Setup now runs `doveconf -n` itself after each dovecot restart in
      mail-dovecot.sh and dovecot-fts-flatcurve.sh, so a config that does
      not parse aborts the run at the script that caused it. If setup
      completes the config at least parsed; everything below is about
      whether it parsed into what we meant.
- [ ] `journalctl -u dovecot -b` has no config complaints at startup.
- [ ] Mail lands in the right place: send a message and confirm it appears in
      `$STORAGE_ROOT/mail/mailboxes/<domain>/<user>/new/`, and that
      `/var/mail/` stays empty (checks `mail_inbox_path` was cleared).
- [ ] IMAPS 993 and POP3S 995 work from outside.
- [ ] Plaintext is closed from outside: `nc -vz $HOST 143` and `nc -vz $HOST 110`
      both refused, while `nc -vz 127.0.0.1 143` succeeds on the box
      (Nextcloud's user_external needs the loopback listener).
- [ ] TLS: `openssl s_client -connect $HOST:993 -tls1_2` and `-tls1_3` both
      work, `-tls1_1` fails. **[unverified]** `ssl_server_prefer_ciphers = client`
      is a behaviour change from the old `ssl_prefer_server_ciphers = yes` —
      confirm the negotiated cipher is acceptable to you
      (`openssl s_client -connect $HOST:993 </dev/null 2>/dev/null | grep Cipher`).
- [ ] Cleartext auth refused before STARTTLS (`auth_allow_cleartext = no`).
- [ ] **[unverified] Quota — the highest-risk item.** `quota_rule` no longer
      exists in 2.4; limits now come back from the userdb as
      `userdb_quota_storage_size`. If this is wrong, quotas silently stop being
      enforced rather than erroring.
      - `doveadm quota get -u user@domain` shows the limit from `users.sqlite`.
      - Change the quota in the admin panel, `doveadm quota recalc -u ...`,
        re-check that `doveadm quota get` reflects the new value.
      - Set a tiny quota (e.g. 1M), deliver mail until it's exceeded, confirm
        the sender gets `522 5.2.2 Mailbox is full` and not a silent accept.
      - A user with quota `0` is unlimited.
- [ ] **[unverified]** quota-status service answers Postfix on 127.0.0.1:12340
      (`nc -vz 127.0.0.1 12340`), and `quota_status_*` being global rather than
      inside `service quota-status {}` actually takes effect — the over-quota
      test above is the real proof.
- [ ] Admin panel user list shows box size and percentage (this reads each
      mailbox's `maildirsize`, which is why the Maildir++ driver was kept).
      `ls $STORAGE_ROOT/mail/mailboxes/<domain>/<user>/maildirsize` must exist.
- [ ] **[unverified] Sieve.** 2.4 replaced sieve_before/after/dir with named
      `sieve_script` blocks.
      - Spam sieve fires: deliver a GTUBE test message, confirm it lands in
        `Spam` and not `INBOX`.
      - Drop a `.sieve` file in `$STORAGE_ROOT/mail/sieve/global_before/`,
        `sievec` it, confirm it runs.
      - Same for `global_after/`.
      - Ordering: spam-global must run before global_before (2.3 ran
        sieve_before ahead of sieve_before2). Easiest test is a global_before
        script that files everything into a folder and confirming spam still
        wins. If the order is wrong, use `sieve_script_precedence`.
      - ManageSieve: create a filter in Roundcube, confirm it appears under
        `$STORAGE_ROOT/mail/sieve/<domain>/<user>/` with the active symlink at
        `<user>.sieve`, and that it actually runs on delivery.
- [ ] IMAP IDLE: connection stays up, `imap_idle_notify_interval = 4 mins`
      visible in `doveconf -n`.
- [ ] Open 40+ IMAP connections from one IP for one user, confirm
      `mail_max_userip_connections = 40` is what bites.
- [ ] `cat /proc/sys/fs/inotify/max_user_instances` → 1024 without a reboot
      (the setting moved to `/etc/sysctl.d/60-mailinabox.conf`).
- [ ] Postfix SASL through Dovecot: submission on 587 authenticates
      (`/var/spool/postfix/private/auth` socket).
- [ ] Nextcloud login works — user_external authenticates against
      127.0.0.1:143 over cURL's IMAP (it does *not* use ext/imap, which no
      longer exists in PHP 8.5).
- [ ] `roundcube_php` system user exists (the `if [ ! id -u ... ]` test in
      web.sh was never actually running `adduser` before this branch).

## Step 4 - SpamAssassin / spampd / IMAPSieve learning

- [ ] spampd starts and is reachable: `systemctl status spampd`,
      `nc -vz 127.0.0.1 10025`.
- [ ] `/etc/spampd.cfg` ended up with `local-only 0`, `maxsize 2000` and
      `relayport 10026` (the old `/etc/default/spampd` is gone in this version).
- [ ] Network checks are actually running, i.e. local-only really is off: send
      a GTUBE message and confirm the `X-Spam-Status` header lists network
      rules (Pyzor / DNSBL / DKIM), not just local ones.
- [ ] A >64 KB message still gets scanned (proves `maxsize` took effect).
- [ ] Rule updates: `systemctl list-timers spamassassin-maintenance.timer`
      shows it enabled, and `systemctl start spamassassin-maintenance.service`
      completes without error.
- [ ] **[unverified] IMAPSieve spam learning.** dovecot-antispam is gone; this
      is an entirely new mechanism, so test all four directions:
      - `doveconf -n` shows `imap_sieve = yes` **and** `imap_quota = yes` under
        `protocol imap` -- if the second `mail_plugins` block replaced rather
        than merged, quota silently stops working.
      - `doveconf -n` shows the `mailbox Spam`/`mailbox Junk` and
        `imapsieve_from Spam`/`imapsieve_from Junk` blocks.
      - Move a message from INBOX to Spam. `/var/log/mail.log` should show the
        sieve pipe running; `sa-learn --dump magic` (as the spampd user) should
        show nspam incrementing.
      - Move it back from Spam to INBOX -> nham increments.
      - Move a message from Spam to **Trash** -> nham must NOT increment
        (report-ham.sieve stops on Trash and `Deleted*`).
      - APPEND directly into Spam (what Roundcube's markasjunk does) also
        learns as spam.
- [ ] Roundcube's markasjunk button ends up doing the same thing as a manual
      drag to Spam.
- [ ] Compiled scripts exist and the mail process is not recompiling them on
      every run: `ls -l /usr/lib/dovecot/sieve/report-*.svbin`, and no
      "failed to compile" lines in `/var/log/mail.log`.
- [ ] Bayes file permissions survive a learn cycle. The wrappers run as the
      `mail` user, which only reaches the files through the `spampd`
      supplementary group (`mail_access_groups = spampd`):
      `ls -l $STORAGE_ROOT/mail/spamassassin/` -> `spampd:spampd`, files 0660,
      directory 0770, and still so **after** a learn.
- [ ] sa-learn as the `mail` user does not try to create `~mail/.spamassassin`
      (bayes_path in `/etc/spamassassin/local.cf` should keep everything under
      `$STORAGE_ROOT`).
- [ ] Deliberately break it -- e.g. `chmod 000` the bayes files -- and confirm
      the IMAP move still succeeds and an error appears in mail.err. The
      wrappers exit 0 on purpose so a broken bayes DB can never fail a user's
      IMAP operation.
- [ ] Spamhaus DQS path still works if `SPAMHAUS_DQS_KEY` is set.

## Step 5 - flatcurve full text search

- [ ] **[unverified] The global plugin list is complete.** `doveconf -n` must
      show **all three** of `quota`, `fts` and `fts_flatcurve` under the global
      `mail_plugins`. Three files set that map: the flatcurve package's
      90-fts-flatcurve.conf, our 99-local.conf and our 99-miab-plugins.conf.
      conf.d is parsed in ASCII order and '-' sorts before '.', so
      99-miab-plugins.conf is parsed last and restates the full list. That is
      deliberate: it is not established whether 2.4 merges or replaces two
      same-scope boolean maps, and restating makes it correct either way.
      If only `quota` appears, maps do NOT merge, and the protocol-scoped maps
      in step 4 need the same treatment.
      Then prove it is not cosmetic: run a body search and get a hit, and
      re-check quota enforcement from step 3.
- [ ] Indexing actually runs: after setup, `doveadm index -A -q '*'` queues
      work and `/var/log/mail.log` shows indexer-worker activity. Index files
      appear under each mailbox.
- [ ] Search in Roundcube and in an IMAP client returns hits on message
      bodies, and hits appear for mail received *after* indexing (autoindex).
- [ ] **[unverified]** Exclusions: `mailbox Trash/Junk/Spam { fts_autoindex = no }`
      replaced 2.3's `fts_autoindex_exclude`. Confirm in `doveconf -n`, and
      confirm no index files are created for those folders.
- [ ] **[unverified]** `fts_search_add_missing = yes` replaced `fts_enforced`.
      Search a mailbox that was never indexed and confirm results still come
      back rather than an empty result set.
- [ ] **[unverified] Attachment decoding.** Dovecot 2.4 stopped shipping
      decode2text.sh, so conf/dovecot-decode2text.sh is our own copy of the
      2.3 one; `xml2text` still ships in dovecot-core.
      - `echo | /usr/lib/dovecot/decode2text.sh` lists the supported formats.
      - Mail yourself a PDF and a .docx, reindex, then search for a word that
        only occurs inside the attachment.
      - `/var/log/mail.log` has no decode2text socket errors.
- [ ] `/etc/cron.daily/miab_dovecot` runs clean: `doveadm fts optimize -A`.
- [ ] Old xapian leftovers are gone: no `dovecot-fts-xapian` package,
      no `/etc/dovecot/conf.d/90-plugin-fts.conf`. On a box restored from a
      24.04 backup the old xapian index files under each mailbox are dead
      weight and can be removed.

## Step 6 - PHP 8.5 / Nextcloud

- [ ] Fresh install lands on Nextcloud 34.0.4 and the web UI works.
- [ ] Restore a 24.04 backup whose Nextcloud is 33.x and confirm the upgrade
      to 34 runs (this is the only supported upgrade path now).
- [ ] Restore/fake a config.php with an older version (e.g. 31) and confirm
      setup prints the "too old, upgrade on 24.04 first" message and continues
      rather than failing.
- [ ] contacts and calendar are enabled and usable. They now come from
      Nextcloud core rather than being downloaded, so check the app list shows
      them and CardDAV/CalDAV still sync.
- [ ] `occ app:list` shows user_external 4.0.0 enabled, and logging in to
      Nextcloud with a mail password works -- it authenticates over cURL's
      IMAP against 127.0.0.1:143, not ext/imap.
- [ ] `php -m | grep -i opcache` shows Zend OPcache even though there is no
      php8.5-opcache package and no conf.d/10-opcache.ini (it is compiled
      into the binary on 26.04).
- [ ] `occ` and the Nextcloud cron job run without OPcache or APCu warnings
      in Administration > Overview.
- [ ] Nextcloud's timezone is right: `grep logtimezone $STORAGE_ROOT/owncloud/config.php`
      matches `timedatectl show -p Timezone --value`.
- [ ] Sending mail from Nextcloud works (sendmail mode).
- [ ] No php8.0 packages are installed and nothing is held:
      `dpkg -l 'php8.0*'`, `apt-mark showhold`.

## Step 7 - management daemon on Python 3.14

- [ ] The virtualenv builds: `/usr/local/lib/mailinabox/env/bin/python -V`
      reports 3.14.x and `pip list` shows every package from system.sh.
      Watch for anything falling back to a source build.
- [ ] Binary wheels resolve rather than compile (all three publish
      forward-compatible abi3 wheels, so this should be clean):
      `env/bin/python -c "import cryptography, psutil, PIL; print(cryptography.__version__, psutil.__version__, PIL.__version__)"`
- [ ] `systemctl status mailinabox` is running and the admin panel loads.
- [ ] Nightly job works: run `management/daily_tasks.sh` (or whatever
      cron.daily entry MiaB installs) by hand and confirm backup + status
      checks + the admin email all complete.
- [ ] Status checks page is clean, or at least only shows things you expect.
- [ ] **[unverified] certbot 4.0.** MiaB calls `certbot certonly` with
      `--csr/--cert-path/--chain-path/--fullchain-path/--webroot` and
      `--register-unsafely-without-email`. Those flags have been stable, but
      4.0 is a major bump and this was not verified against its changelog.
      - `certbot --version`
      - `certbot register --register-unsafely-without-email --agree-tos --config-dir $STORAGE_ROOT/ssl/lets_encrypt` (setup does this)
      - Provision a real certificate from the admin panel for a real domain
        and confirm it installs, not just that the command exits 0.
- [ ] SSHFP records: `dig SSHFP $PRIMARY_HOSTNAME` returns records for RSA (1),
      ECDSA (3) and Ed25519 (4), with no DSA (2). Verify the fingerprints match
      `ssh-keyscan -D localhost`, and that `ssh -o VerifyHostKeyDNS=yes` is
      happy. The algorithm numbers are IANA-assigned, so 3 and 4 must NOT be
      renumbered when DSA is dropped.
- [ ] `ssh-keyscan -t rsa,ecdsa,ed25519` returns keys at all on OpenSSH 10.x.

## Step 8 - fork-specific extras

Most of this step turned out to be verification rather than change. Every
package the setup scripts install exists in 26.04, and several things that
looked like they would break do not (see the notes at the end).

- [ ] Setup gets past `geoiptoolssetup.sh`. On a fresh 26.04 box neither
      /etc/hosts.allow nor /etc/hosts.deny exists, and the `sed -i` there
      aborted the whole run under `set -e`. Confirm both files exist
      afterwards and contain the sshd lines.
- [ ] SSH geo-filtering still works. OpenSSH 9.8+ split sshd, so the TCP
      Wrappers check now lives in sshd-session:
      `ldd /usr/lib/openssh/sshd-session | grep -i wrap` (expect libwrap.so.0).
      Then connect from a blocked country and confirm a
      "DENY geoipblocked connection from <ip>" line in syslog and a
      geoipblockssh ban in `fail2ban-client status geoipblockssh`.
- [ ] ipset blacklist: `ipset list -n`, `iptables -L -n | head`, and the
      cron job in /etc/cron.d/miab-ipset-blacklist runs clean.
- [ ] fail2ban 1.1.0 starts and all jails load: `fail2ban-client status`.
      Check for `allowipv6` notices in the log; it is not set in jails.conf.
- [ ] fail2ban subnet blocker: `/usr/local/bin/fail2ban-block-ip-range.py`
      runs under Python 3.14.
- [ ] rkhunter: `/etc/default/rkhunter` exists (ucf creates it, the deb does
      not ship it), `rkhunter --propupd` and the daily cron run without
      warnings.
- [ ] postgrey: `/etc/default/postgrey` exists (also ucf), greylisting works,
      `systemctl status postgrey`.
- [ ] Logging: `/var/log/mail.log` and `/var/log/syslog` are being written
      (rsyslog still ucf-installs the mail.* routing), and the additionals.sh
      seds against 50-default.conf and 20-ufw.conf took effect.
- [ ] stunnel relay, if you use it: `systemctl status stunnel@miabrelay`,
      and the daily cert-combining cron job.
- [ ] dmarc-report-viewer service starts and the UI loads.
- [ ] munin graphs render (munin 2.0.76, same series as 24.04).
- [ ] smartmontools: `/etc/default/smartmontools` handling still applies.

## Step 9 - full run

- [ ] `setup/start.sh` completes end to end with no manual intervention.
- [ ] **[unverified]** `vagrant up` works. The box name
      `cloud-image/ubuntu-26.04` follows Canonical's current naming but could
      not be checked from here (Vagrant Cloud is unreachable). If it 404s,
      `vagrant box search ubuntu` for the right name.
- [ ] Timezone: on first setup you are asked (interactive) or it is set to
      Etc/UTC (NONINTERACTIVE), `timedatectl` agrees, and re-running
      `mailinabox` does NOT ask again. tzdata deletes /etc/timezone on 26.04,
      so that file is no longer the marker.
- [ ] `management/status_checks.py` is clean, or only shows expected items.
- [ ] Round trip: send mail in, read over IMAP, reply out, and check
      DKIM/SPF/DMARC pass at an external checker.
- [ ] Backup runs and, more importantly, a restore from it works.
- [ ] Reboot and confirm every service comes back, including the ipset
      blacklist (ipset-at-boot) and stunnel if used.
- [ ] Re-run `setup/start.sh` on the finished box: it must be idempotent and
      must not undo anything or ask questions again.
- [ ] `tests/` suite: test_mail.py, test_dns.py, test_smtp_server.py.

---

## Notes carried out of step 8

**SSH geo-filtering keeps working, but the binary to check moved.** Debian
and Ubuntu carry a TCP Wrappers patch for OpenSSH. In 24.04 /usr/sbin/sshd
itself linked libwrap.so.0; since the OpenSSH 9.8 split the per-connection
work happens in /usr/lib/openssh/sshd-session, and that is the binary linking
libwrap and importing hosts_access on 26.04. openssh-server still depends on
libwrap0 (>= 7.6-4~). So hosts.allow/aclexec, geoipfilter.sh and the
geoipblockssh fail2ban jail all still function; only the diagnostic command
changes. Note that this rests on a distro patch, not upstream OpenSSH, so it
is worth re-checking at the next LTS.

**Things that looked broken but are not**: /etc/default/postgrey and
/etc/default/rkhunter are not shipped in their .debs but are created by ucf at
install time, so the editconf.py calls still work. rsyslog still ucf-installs
50-default.conf with `mail.* -/var/log/mail.log`, so mail.log and syslog still
exist. /etc/rsyslog.d/20-ufw.conf still ships with ufw.

---

## Known unknowns carried into testing

1. **Quota userdb field name.** `userdb_quota_storage_size` is from the 2.4
   docs, not from a running server. Verify before trusting quota enforcement.
2. **`quota_storage_grace = 100 M`.** 2.4 dropped percentage grace values, so
   the old `quota_grace = 10%` had no direct translation.
3. **`sieve_script` execution order** is documented as config order; confirm.
4. **`ssl_server_prefer_ciphers = client`** — deliberate change from the old
   `yes`, following Dovecot's 2.4 SSL guidance and Mozilla Intermediate.
5. **`http2 on;`** could not be syntax-checked against nginx 1.28 locally.
6. **`quota miab { driver = maildir }`** — root name is arbitrary; confirm
   `doveadm quota get` reports against it.
