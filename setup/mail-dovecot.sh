#!/bin/bash
#
# Dovecot (IMAP/POP and LDA)
# ----------------------
#
# Dovecot is *both* the IMAP/POP server (the protocol that email applications
# use to query a mailbox) as well as the local delivery agent (LDA),
# meaning it is responsible for writing emails to mailbox storage on disk.
# You could imagine why these things would be bundled together.
#
# As part of local mail delivery, Dovecot executes actions on incoming
# mail as defined in a "sieve" script.
#
# Dovecot's LDA role comes after spam filtering. Postfix hands mail off
# to Spamassassin which in turn hands it off to Dovecot. This all happens
# using the LMTP protocol.

source setup/functions.sh # load our functions
source /etc/mailinabox.conf # load global vars


# Install packages for dovecot. These are all core dovecot plugins,
# but dovecot-lucene is packaged by *us* in the Mail-in-a-Box PPA,
# not by Ubuntu.

echo "Installing Dovecot (IMAP server)..."
apt_install \
	dovecot-core dovecot-imapd dovecot-pop3d dovecot-lmtpd dovecot-sqlite sqlite3 \
	dovecot-sieve dovecot-managesieved

# The `dovecot-imapd`, `dovecot-pop3d`, and `dovecot-lmtpd` packages automatically
# enable IMAP, POP and LMTP protocols.

# Set basic daemon options.

# The `default_process_limit` is 100, which constrains the total number
# of active IMAP connections (at, say, 5 open connections per user that
# would be 20 users). Set it to 250 times the number of cores this
# machine has, so on a two-core machine that's 500 processes/100 users).
# Set the client_limit to 5 times the process limit.
# The `default_vsz_limit` is the maximum amount of virtual memory that
# can be allocated. It should be set *reasonably high* to avoid allocation
# issues with larger mailboxes. We're setting it to 1/3 of the total
# available memory (physical mem + swap) to be sure.
# See here for discussion:
# - https://www.dovecot.org/list/dovecot/2012-August/137569.html
# - https://www.dovecot.org/list/dovecot/2011-December/132455.html
management/editconf.py /etc/dovecot/conf.d/10-master.conf \
	default_process_limit="$(($(nproc) * 250))" \
	default_client_limit="$(($(nproc) * 1250))" \
	default_vsz_limit="$(($(free -tm  | tail -1 | awk '{print $2}') / 3))M"

# The inotify `max_user_instances` default is 128, which constrains
# the total number of watched (IMAP IDLE push) folders by open connections.
# See http://www.dovecot.org/pipermail/dovecot/2013-March/088834.html.
# Ubuntu 26.04 no longer ships /etc/sysctl.conf, so we drop our setting into
# /etc/sysctl.d/ instead, and apply it right away rather than at the next
# boot. Test with `cat /proc/sys/fs/inotify/max_user_instances`.
cat > /etc/sysctl.d/60-mailinabox.conf << EOF;
fs.inotify.max_user_instances=1024
EOF
hide_output sysctl --system

# Set the location where we'll store user mailboxes. Dovecot 2.4 splits the
# old mail_location setting into mail_driver/mail_path, and the one-letter
# %variables are gone: '%{user|domain}' is the domain name and
# '%{user|username}' the username part of the user's email address. We'll
# ensure that no bad domains or email addresses are created within the
# management daemon.
management/editconf.py /etc/dovecot/conf.d/10-mail.conf \
	mail_driver=maildir \
	mail_path="$STORAGE_ROOT/mail/mailboxes/%{user|domain}/%{user|username}" \
	mail_privileged_group=mail \
	first_valid_uid=0

# The Ubuntu package ships an *active* mbox configuration. mail_driver and
# mail_path are overridden above, but mail_inbox_path would still point INBOX
# at /var/mail/<user>, so clear it.
management/editconf.py -e /etc/dovecot/conf.d/10-mail.conf \
	mail_inbox_path=

# Create, subscribe, and mark as special folders: INBOX, Drafts, Sent, Trash, Spam and Archive.
cp conf/dovecot-mailboxes.conf /etc/dovecot/conf.d/15-mailboxes.conf

# Quota support. Dovecot 2.4 removed the plugin {} block: a quota root is now
# a named `quota` filter and the quota-status replies are global settings.
# We keep the Maildir++ driver rather than the new default 'count' driver
# because the management daemon reads each mailbox's `maildirsize` file to
# report usage in the admin panel (see management/mailconfig.py).
#
# Per-user limits come from the userdb as `userdb_quota_storage_size`, see
# setup/mail-users.sh, so the quota root itself sets no size.
cat > /etc/dovecot/conf.d/99-local-quota.conf << EOF;
quota miab {
  driver = maildir
}

# Let a single delivery exceed the quota by this much. Dovecot 2.4 only
# accepts an absolute size here; in 2.3 this was quota_grace = 10%%.
quota_storage_grace = 100 M

quota_status_success = DUNNO
quota_status_nouser = DUNNO
quota_status_overquota = "522 5.2.2 Mailbox is full"

service quota-status {
  executable = quota-status -p postfix
  inet_listener quota-status {
    port = 12340
  }
}
EOF

# ### IMAP/POP

# Require that passwords are sent over SSL only, and allow the usual IMAP authentication mechanisms.
# The LOGIN mechanism is supposedly for Microsoft products like Outlook to do SMTP login (I guess
# since we're using Dovecot to handle SMTP authentication?).
management/editconf.py /etc/dovecot/conf.d/10-auth.conf \
	auth_allow_cleartext=no \
	"auth_mechanisms=plain login"

# Enable SSL, specify the location of the SSL certificate and private key files.
# Use Mozilla's "Intermediate" recommendations at https://ssl-config.mozilla.org/#server=dovecot&config=intermediate
# Dovecot 2.4 renamed these: ssl_cert -> ssl_server_cert_file, ssl_key ->
# ssl_server_key_file, ssl_dh -> ssl_server_dh_file, and the '<' file-read
# prefix is gone (the *_file settings take a path directly).
management/editconf.py /etc/dovecot/conf.d/10-ssl.conf \
	ssl=required \
	"ssl_server_cert_file=$STORAGE_ROOT/ssl/ssl_certificate.pem" \
	"ssl_server_key_file=$STORAGE_ROOT/ssl/ssl_private_key.pem" \
	"ssl_min_protocol=TLSv1.2" \
	"ssl_cipher_list=ALL:!kRSA:!SRP:!kDHd:!DSS:!aNULL:!eNULL:!EXPORT:!DES:!3DES:!MD5:!PSK:!RC4:!ADH:!CAMELLIA:!ARIA:!CBC:!AESCCM:!LOW@STRENGTH" \
	"ssl_curve_list=X25519:prime256v1:secp384r1" \
	"ssl_server_prefer_ciphers=client" \
	"ssl_server_dh_file=$STORAGE_ROOT/ssl/dh4096.pem"

# Disable in-the-clear IMAP/POP because there is no reason for a user to transmit
# login credentials outside of an encrypted connection. Only the over-TLS versions
# are made available (IMAPS on port 993; POP3S on port 995).
sed -i "s/#port = 143/port = 0/" /etc/dovecot/conf.d/10-master.conf
sed -i "s/#port = 110/port = 0/" /etc/dovecot/conf.d/10-master.conf

# Dovecot 2.4 no longer ships conf.d/20-imap.conf, 20-pop3.conf or 20-lmtp.conf;
# the per-protocol settings that used to live there are set in 99-local.conf
# below. pop3_uidl_format is dropped entirely: its value used the one-letter
# %variables that 2.4 removed, and the 2.4 default is already IMAP's
# UIDVALIDITY/UID, which is what we wanted in the first place.

# ### LDA (LMTP)

# Enable Dovecot's LDA service with the LMTP protocol. It will listen
# on port 10026, and Spamassassin will be configured to pass mail there.
#
# The disabled unix socket listener is normally how Postfix and Dovecot
# would communicate (see the Postfix setup script for the corresponding
# setting also commented out).
#
# Also increase the number of allowed IMAP connections per mailbox because
# we all have so many devices lately.
#
# Dovecot 2.4 notes: inet_listener's `address` is now `listen`, and
# mail_plugins is a boolean map instead of a space separated string.
cat > /etc/dovecot/conf.d/99-local.conf << EOF;
# Load the quota plugin for every service, and its IMAP counterpart for IMAP.
mail_plugins {
  quota = yes
}

service lmtp {
  #unix_listener /var/spool/postfix/private/dovecot-lmtp {
  #  user = postfix
  #  group = postfix
  #}
  inet_listener lmtp {
    listen = 127.0.0.1
    port = 10026
  }
}

# Enable imap-login on localhost to allow the user_external plugin
# for Nextcloud to do imap authentication. (See #1577)
service imap-login {
  inet_listener imap {
    listen = 127.0.0.1
    port = 143
  }
}

protocol imap {
  mail_plugins {
    imap_quota = yes
  }

  # Make IMAP IDLE slightly more efficient. By default, Dovecot says "still
  # here" every two minutes. With K-9 mail, the bandwidth and battery usage
  # due to this are minimal. But for good measure, let's go to 4 minutes to
  # halve the bandwidth and number of times the device's networking might be
  # woken up. The risk is that if the connection is silent for too long it
  # might be reset by a peer. See
  # https://github.com/mail-in-a-box/mailinabox/issues/129 and
  # http://razor.occams.info/blog/2014/08/09/how-bad-is-imap-idle/
  imap_idle_notify_interval = 4 mins

  mail_max_userip_connections = 40
}

protocol lmtp {
  mail_plugins {
    sieve = yes
  }
}
EOF

# Setting a `postmaster_address` is required or LMTP won't start. An alias
# will be created automatically by our management daemon.
management/editconf.py /etc/dovecot/conf.d/15-lda.conf \
	"postmaster_address=postmaster@$PRIMARY_HOSTNAME"

# ### Sieve

# The sieve plugin is enabled for LMTP in 99-local.conf above.
#
# Configure sieve. We'll create a global script that moves mail marked
# as spam by Spamassassin into the user's Spam folder.
#
# Dovecot 2.4 replaced sieve_before/sieve_before2/sieve_after/sieve/sieve_dir
# with named `sieve_script` filters carrying a `type`. Scripts of the same
# type run in the order they are defined here (sieve_script_precedence can
# override that), so spam-global keeps running before global_before, as
# sieve_before did before sieve_before2 under 2.3.
#
# * `spam-global`: our global sieve which moves spam to the Spam folder.
#
# * `global-before` / `global-after`: directories of .sieve files that run
# globally for every user before resp. after their own sieve files run.
#
# * `personal`: the user's own scripts. ManageSieve stores them under `path`
# and symlinks the active one to `active_path`. `path` should not be in the
# mailbox directory (because then it might appear as a folder) and
# `active_path` should not be inside `path` (because then it might appear to
# the user as one of their scripts).
cat > /etc/dovecot/conf.d/99-local-sieve.conf << EOF;
sieve_script spam-global {
  type = before
  driver = file
  path = /etc/dovecot/sieve-spam.sieve
}
sieve_script global-before {
  type = before
  driver = file
  path = $STORAGE_ROOT/mail/sieve/global_before
}
sieve_script global-after {
  type = after
  driver = file
  path = $STORAGE_ROOT/mail/sieve/global_after
}
sieve_script personal {
  type = personal
  driver = file
  path = $STORAGE_ROOT/mail/sieve/%{user|domain}/%{user|username}
  active_path = $STORAGE_ROOT/mail/sieve/%{user|domain}/%{user|username}.sieve
}

sieve_redirect_envelope_from = recipient
EOF

# Copy the global sieve script into where we've told Dovecot to look for it. Then
# compile it. Global scripts must be compiled now because Dovecot won't have
# permission later.
cp conf/sieve-spam.txt /etc/dovecot/sieve-spam.sieve
sievec /etc/dovecot/sieve-spam.sieve

# ### Permissions

# Ensure configuration files are owned by dovecot and not world readable.
chown -R mail:dovecot /etc/dovecot
chmod -R o-rwx /etc/dovecot

# Ensure mailbox files have a directory that exists and are owned by the mail user.
mkdir -p "$STORAGE_ROOT/mail/mailboxes"
mkdir -p "$STORAGE_ROOT/mail/homes"
chown -R mail:mail "$STORAGE_ROOT/mail/mailboxes"
chown -R mail:mail "$STORAGE_ROOT/mail/homes"

# Same for the sieve scripts.
mkdir -p "$STORAGE_ROOT/mail/sieve"
mkdir -p "$STORAGE_ROOT/mail/sieve/global_before"
mkdir -p "$STORAGE_ROOT/mail/sieve/global_after"
chown -R mail:mail "$STORAGE_ROOT/mail/sieve"

# Allow the IMAP/POP ports in the firewall.
ufw_allow imaps
ufw_allow pop3s

# Allow the Sieve port in the firewall.
ufw_allow sieve

# Restart services.
restart_service dovecot
