#!/bin/bash
#
# IMAP full text search with flatcurve
# ------------------------------------
#
# flatcurve is Dovecot's own Xapian-backed FTS engine, packaged by Ubuntu as
# dovecot-flatcurve. It replaces the third-party fts-xapian plugin we used on
# 24.04.

source setup/functions.sh # load our functions
source /etc/mailinabox.conf # load global vars

echo "Installing flatcurve (full text search)..."

apt_install dovecot-flatcurve

# Text extraction from attachments.
apt_install poppler-utils catdoc unzip
install -m 0755 conf/dovecot-decode2text.sh /usr/lib/dovecot/decode2text.sh

# The dovecot-flatcurve package's own conf.d/90-fts-flatcurve.conf already
# loads the fts and fts_flatcurve plugins and sets up language handling.
#
# 2.4 dropped fts_autoindex_exclude in favour of per-mailbox filters, and
# renamed fts_enforced to fts_search_add_missing.
cat > /etc/dovecot/conf.d/99-local-fts.conf << EOF;
fts_autoindex = yes
fts_search_add_missing = yes

mailbox Trash {
  fts_autoindex = no
}
mailbox Junk {
  fts_autoindex = no
}
mailbox Spam {
  fts_autoindex = no
}

fts_decoder_driver = script
fts_decoder_script_socket_path = decode2text

service decode2text {
  executable = script /usr/lib/dovecot/decode2text.sh
  user = dovecot
  unix_listener decode2text {
    mode = 0666
  }
}

service indexer-worker {
  vsz_limit = 2G
}
EOF

# The package's 90-fts-flatcurve.conf and our 99-local.conf each set a
# global mail_plugins map. Restate the complete list in a file that sorts
# after both, so it holds whether 2.4 merges or replaces same-scope maps.
cat > /etc/dovecot/conf.d/99-miab-plugins.conf << EOF;
mail_plugins {
  quota = yes
  fts = yes
  fts_flatcurve = yes
}
EOF

# Install cronjobs to keep FTS up to date.
hide_output install -m 755 conf/cron/miab_dovecot /etc/cron.daily/

restart_service dovecot
hide_output doveconf -n

# Drop index entries for expunged mail, then queue indexing of everything that
# is missing. -q hands the work to the indexer process in the background.
hide_output doveadm fts rescan -A
doveadm index -A -q \*
