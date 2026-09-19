#!/bin/bash
# Called by the IMAPSieve report-ham script when a user copies a message back
# out of the Spam folder. The message arrives on stdin; $1 is the user.
#
# Installed by setup/spamassassin.sh in sieve_pipe_bin_dir.
#
# We always exit 0: a broken bayes database must never make the user's IMAP
# move fail. Failures are logged to mail.err instead.
out=$(/bin/nice -n 19 /usr/bin/sa-learn --ham - 2>&1)
ret=$?

if [ $ret -gt 0 ]; then
	logger -p mail.err -i -t "${0##*/}" "sa-learn --ham failed for ${1:-unknown}: $out"
fi

exit 0
