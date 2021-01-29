source /etc/mailinabox.conf
source setup/functions.sh

# Cleanup old spam and trash email
cp -f conf/local_clean_mail /etc/cron.weekly/
chmod +x /etc/cron.weekly
sed -i "s/PRIMARY_HOSTNAME/$PRIMARY_HOSTNAME/g" /etc/cron.weekly/local_clean_mail

# Some more munin plugins
ln -s /usr/share/munin/plugins/postfix_mailstats /etc/munin/plugins/
ln -s /usr/share/munin/plugins/spamstats /etc/munin/plugins
ln -s /usr/share/munin/plugins/df_abs /etc/munin/plugins

# rootkit hunter
apt_install rkhunter


## TODO
## file filter.d/nginx-http-auth.conf
## add second line:
#failregex = ^ \[error\] \d+#\d+: \*\d+ user "(?:[^"]+|.*?)":? (?:password mismatch|was not found in "[^\"]*"), client: <HOST>, server: \S*,
#            ^ \[error\] \d+#\d+: \*\d+ no user/password was provided for basic authentication, client: <HOST>, server: \S+, request: "\S+ \S

## reduce logs, replace following line in /etc/rsyslog.d/50-default.conf
#*.*;mail,auth,authpriv.none     -/var/log/syslog
## uncomment last line of /etc/rsyslog.d/20-ufw.conf
#& stop

# decrease time journal is stored
tools/editconf.py /etc/systemd/journald.conf MaxRetentionSec=2month
tools/editconf.py /etc/systemd/journald.conf MaxFileSec=1week

