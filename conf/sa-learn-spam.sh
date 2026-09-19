#!/bin/bash
# Message on stdin, $1 is the user. Always exits 0: a broken bayes database
# must not fail the user's IMAP move.
out=$(/bin/nice -n 19 /usr/bin/sa-learn --spam - 2>&1)
ret=$?

if [ $ret -gt 0 ]; then
	logger -p mail.err -i -t "${0##*/}" "sa-learn --spam failed for ${1:-unknown}: $out"
fi

exit 0
