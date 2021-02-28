source /etc/mailinabox.conf
source setup/functions.sh

# Cleanup old spam and trash email
cp -f conf/cron/local_clean_mail /etc/cron.weekly/
chmod +x /etc/cron.weekly/local_clean_mail

# Explicitly reopen dovecot logfiles after logrotate has run
cp -f conf/cron/logrotate_post_dovecot /etc/cron.daily/
chmod +x /etc/cron.daily/logrotate_post_dovecot

# Reduce logs by not logging mail output in syslog
sed -i "s/\*\.\*;auth,authpriv.none.*\-\/var\/log\/syslog/\*\.\*;mail,auth,authpriv.none    \-\/var\/log\/syslog/g" /etc/rsyslog.d/50-default.conf

# Reduce logs by only logging ufw in ufw.log
sed -i "s/#\& stop/\& stop/g" /etc/rsyslog.d/20-ufw.conf

restart_service rsyslog

# decrease time journal is stored
tools/editconf.py /etc/systemd/journald.conf MaxRetentionSec=2month
tools/editconf.py /etc/systemd/journald.conf MaxFileSec=1week

hide_output systemctl restart systemd-journald.service
