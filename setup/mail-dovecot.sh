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
# Drop our setting into /etc/sysctl.d/, and apply it right away rather than
# at the next boot. Test with `cat /proc/sys/fs/inotify/max_user_instances`.
cat > /etc/sysctl.d/60-mailinabox.conf << EOF;
fs.inotify.max_user_instances=1024
EOF
hide_output sysctl --system

# Set the location where we'll store user mailboxes. 2.4 split mail_location
# into mail_driver/mail_path and replaced %d/%n with %{user|domain} and
# %{user|username}. The management daemon validates domains and addresses.
management/editconf.py /etc/dovecot/conf.d/10-mail.conf \
	mail_driver=maildir \
	mail_path="$STORAGE_ROOT/mail/mailboxes/%{user|domain}/%{user|username}" \
	mail_privileged_group=mail \
	first_valid_uid=0

# The package ships an active mbox config; mail_inbox_path would still point
# INBOX at /var/mail, so clear it.
management/editconf.py /etc/dovecot/conf.d/10-mail.conf -e \
	mail_inbox_path=

# Create, subscribe, and mark as special folders: INBOX, Drafts, Sent, Trash, Spam and Archive.
cp conf/dovecot-mailboxes.conf /etc/dovecot/conf.d/15-mailboxes.conf

# Configure quota support
cat > /etc/dovecot/conf.d/99-local-quota.conf << EOF;
quota miab {
  driver = maildir
}

# Let a single delivery exceed the quota by this much.
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
# Mozilla "Intermediate": https://ssl-config.mozilla.org/#server=dovecot&config=intermediate
# 2.4 renamed ssl_cert/ssl_key/ssl_dh to ssl_server_*_file and dropped the '<'
# file-read prefix.
management/editconf.py /etc/dovecot/conf.d/10-ssl.conf \
	ssl=required \
	"ssl_server_cert_file=$STORAGE_ROOT/ssl/ssl_certificate.pem" \
	"ssl_server_key_file=$STORAGE_ROOT/ssl/ssl_private_key.pem" \
	"ssl_min_protocol=TLSv1.3" \
	"ssl_curve_list=X25519MLKEM768:X25519:prime256v1:secp384r1" \
	"ssl_server_prefer_ciphers=client" \
	"ssl_server_dh_file=$STORAGE_ROOT/ssl/dh4096.pem"

# Disable in-the-clear IMAP/POP because there is no reason for a user to transmit
# login credentials outside of an encrypted connection. Only the over-TLS versions
# are made available (IMAPS on port 993; POP3S on port 995).
sed -i "s/#port = 143/port = 0/" /etc/dovecot/conf.d/10-master.conf
sed -i "s/#port = 110/port = 0/" /etc/dovecot/conf.d/10-master.conf

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
    # Drives the spam/ham learning scripts, see setup/spamassassin.sh.
    imap_sieve = yes
  }

  # Halve IMAP IDLE keepalive chatter (default 2 mins). See
  # https://github.com/mail-in-a-box/mailinabox/issues/129
  imap_idle_notify_interval = 4 mins

  mail_max_userip_connections = 40
}

protocol lmtp {
  mail_plugins {
    sieve = yes
  }

  auth_username_format = %{user | lower}
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
# 2.4 replaced sieve_before/sieve_after/sieve/sieve_dir with named
# sieve_script filters. Same-type scripts run in definition order, so
# spam-global stays ahead of global-before. ManageSieve writes the user's
# scripts under the personal `path` and symlinks the active one to
# `active_path`, which is kept out of both the mailbox dir and `path`.
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

# Fail here rather than at first delivery if the config does not parse.
hide_output doveconf -n
