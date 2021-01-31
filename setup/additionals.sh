source /etc/mailinabox.conf
source setup/functions.sh

# Cleanup old spam and trash email
cp -f conf/local_clean_mail /etc/cron.weekly/
chmod +x /etc/cron.weekly
sed -i "s/PRIMARY_HOSTNAME/$PRIMARY_HOSTNAME/g" /etc/cron.weekly/local_clean_mail

# rootkit hunter
apt_install rkhunter


## TODO
## file filter.d/nginx-http-auth.conf
## add second line:
#failregex = ^ \[error\] \d+#\d+: \*\d+ user "(?:[^"]+|.*?)":? (?:password mismatch|was not found in "[^\"]*"), client: <HOST>, server: \S*,
#            ^ \[error\] \d+#\d+: \*\d+ no user/password was provided for basic authentication, client: <HOST>, server: \S+, request: "\S+ \S


# Reduce logs by not logging mail output in syslog
sed -i "s/\*\.\*;auth,authpriv.none.*\-\/var\/log\/syslog//\*\.\*;mail,auth,authpriv.none    \-\/var\/log\/syslog/g" /etc/rsyslog.d/50-default.conf

# Reduce logs by only logging ufw in ufw.log
sed -i "s/#& stop/& stop/g" /etc/rsyslog.d/20-ufw.conf

restart_service rsyslog

# decrease time journal is stored
tools/editconf.py /etc/systemd/journald.conf MaxRetentionSec=2month
tools/editconf.py /etc/systemd/journald.conf MaxFileSec=1week

hide_output systemctl restart systemd-journald.service
