#!/bin/bash
# This script is run daily (at 3am each night).

# Set character encoding flags to ensure that any non-ASCII
# characters don't cause problems. See setup/start.sh and
# the management daemon startup script.
export LANGUAGE=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export LANG=en_US.UTF-8
export LC_TYPE=en_US.UTF-8

source /etc/mailinabox.conf
source setup/functions.sh

# On Mondays, i.e. once a week, send the administrator a report of total emails
# sent and received so the admin might notice server abuse.
if [ "$(date "+%u")" -eq 1 ]; then
    management/mail_log.py -t week -r -s -l -g -b | management/email_administrator.py "Mail-in-a-Box Usage Report"

    /usr/sbin/pflogsumm -u 5 -h 5 --problems_first /var/log/mail.log.1 | management/email_administrator.py "Postfix log analysis summary"
fi

# Take a backup (ignoring informational output from duplicity for some backends).
export DUPLICITY_VALIDATION_INFO="File size can't be validated, because of missing capabilities of the backend. Please verify the backup separately."
management/backup.py 2>&1 | sed 's/'"$DUPLICITY_VALIDATION_INFO"'//g' | management/email_administrator.py "Backup Status"

# Provision any new certificates for new domains or domains with expiring certificates.
management/ssl_certificates.py -q  2>&1 | management/email_administrator.py "TLS Certificate Provisioning Result"

# Daily package update / upgrade
export DEBIAN_FRONTEND=noninteractive
hide_output apt-get -qq update
hide_output apt-get -qq -y upgrade

# Daily maintenance tasks
management/daily_maintenance.py

# Run status checks and email the administrator if anything changed.
management/status_checks.py --show-changes  2>&1 | management/email_administrator.py "Status Checks Change Notice"

# Check blacklists
if [ -n "$SPAMHAUS_DQS_KEY" ]; then
	tools/check-dnsbl.py --dqs-key $SPAMHAUS_DQS_KEY $PUBLIC_IP $PUBLIC_IPV6 2>&1 | management/email_administrator.py "Blacklist Check Result"
else
	tools/check-dnsbl.py $PUBLIC_IP $PUBLIC_IPV6 2>&1 | management/email_administrator.py "Blacklist Check Result"
fi
