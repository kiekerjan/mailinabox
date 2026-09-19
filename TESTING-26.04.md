# Ubuntu 26.04 branch — manual test plan

Running list of things to verify by hand on a throwaway VPS after a full
`setup/start.sh` run. Grouped by the migration step that introduced the risk.
Items marked **[unverified]** are things that could not be checked against
anything but documentation while porting — check these first.

Suggested order: run the whole checklist top to bottom on a fresh box, then
restore a backup from the 24.04 box and run the mail/quota/sieve sections again.

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

## Steps 4-9

To be filled in as those steps land.

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
