source /etc/mailinabox.conf
source setup/functions.sh

# ## Additional modifications

# Add additional packages
apt_install pflogsumm rkhunter

# Cleanup old spam and trash email
hide_output install -m 755 conf/cron/miab_clean_mail /etc/cron.weekly/

# Reduce logs by not logging mail output in syslog
sed -i "s/\*\.\*;auth,authpriv.none.*\-\/var\/log\/syslog/\*\.\*;mail,auth,authpriv.none    \-\/var\/log\/syslog/g" /etc/rsyslog.d/50-default.conf

# Reduce logs by only logging ufw in ufw.log
sed -i "s/#\& stop/\& stop/g" /etc/rsyslog.d/20-ufw.conf

restart_service rsyslog

# Create forward for root emails
cat > /root/.forward << EOF;
administrator@$PRIMARY_HOSTNAME
EOF

# ### rkhunter configuration

# Adapt rkhunter cron job to reduce log file production
sed -i "s/--cronjob --report-warnings-only --appendlog/--cronjob --report-warnings-only --no-verbose-logging --appendlog/g" /etc/cron.daily/rkhunter

# Install fake mail script
if [ ! -f /usr/local/bin/mail ]; then
        hide_output install -m 755 tools/fake_mail /usr/local/bin
        mv -f /usr/local/bin/fake_mail /usr/local/bin/mail
fi

# Adapt rkhunter configuration
management/editconf.py /etc/rkhunter.conf \
        UPDATE_MIRRORS=1 \
        MIRRORS_MODE=0 \
        WEB_CMD='""' \
        APPEND_LOG=1 \
        ALLOWHIDDENDIR=/etc/.java

# Check presence of whitelist
if ! grep -Fxq "SCRIPTWHITELIST=/usr/local/bin/mail" /etc/rkhunter.conf > /dev/null; then
	echo "SCRIPTWHITELIST=/usr/local/bin/mail" >> /etc/rkhunter.conf
fi

management/editconf.py /etc/default/rkhunter \
        CRON_DAILY_RUN='"true"' \
        CRON_DB_UPDATE='"true"' \
        APT_AUTOGEN='"true"'

# Should be last, update expected output
rkhunter --propupd

# ### Install Subnetblocker
# Regularly scan fail2ban log to capture whole subnets to block
hide_output install -m 755 tools/fail2ban-block-ip-range.py /usr/local/bin
cp -f conf/cron/miab-fail2ban-subnet-blocker /etc/cron.d/
# Logrotation is done via generic mail in a box logrotate config

# ### Install additional tools

# Install combine_certs.sh tool
hide_output install -m 755 tools/combine_certs.sh /usr/local/bin
