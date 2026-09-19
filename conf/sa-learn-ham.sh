#!/bin/bash
# Message on stdin, $1 is the user. Always exits 0: a broken bayes database
# must not fail the user's IMAP move.
out=$(/bin/nice -n 19 /usr/bin/sa-learn --ham - 2>&1)
ret=$?

if [ $ret -gt 0 ]; then
	logger -p mail.err -i -t "${0##*/}" "sa-learn --ham failed for ${1:-unknown}: $out"
fi

exit 0
