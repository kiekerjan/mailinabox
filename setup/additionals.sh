#!/bin/bash

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

# ### Install Subnetblocker
# Regularly scan fail2ban log to capture whole subnets to block
hide_output install -m 755 tools/fail2ban-block-ip-range.py /usr/local/bin
cp -f conf/cron/miab-fail2ban-subnet-blocker /etc/cron.d/
# Logrotation is done via generic mail in a box logrotate config

# ### Install ipset blacklist 

# Dependencies
apt_install ipset iptables

wget_verify "https://github.com/zhanhb/cidr-merger/releases/download/v1.1.3/cidr-merger-linux-amd64" b7d0a54514653c609509095f13c702f9ab2f32dc /tmp/cidr-merger
hide_output install -m 755 /tmp/cidr-merger /usr/local/bin
rm -f /tmp/cidr-merger

# Configuration files
mkdir -p /etc/ipset-blacklist

if [ ! -f /etc/ipset-blacklist/ipset-blacklist.conf ]; then
	cp conf/ipset-blacklist.conf /etc/ipset-blacklist/
fi
if [ ! -f /etc/ipset-blacklist/ipset-blacklist-custom.list ]; then
	touch /etc/ipset-blacklist/ipset-blacklist-custom.list
fi

# Install update scripts
mkdir -p /usr/local/lib/ipset-blacklist

hide_output install -m 755 tools/update-blacklist.sh /usr/local/lib/ipset-blacklist
hide_output install -m 755 tools/ipset-at-boot /usr/local/lib/ipset-blacklist
hide_output install -m 755 tools/ipset-update /usr/local/lib/ipset-blacklist

# Install cron job
hide_output install -m 755 conf/cron/miab-ipset-blacklist /etc/cron.d

# Initial creation
hide_output /usr/local/lib/ipset-blacklist/ipset-update

# Fail2ban actions cause fail2ban rules to be added after ipset blacklist rules
cp -f conf/fail2ban/action.d/iptables-allports.local /etc/fail2ban/action.d/
cp -f conf/fail2ban/action.d/iptables-multiport.local /etc/fail2ban/action.d/

# ### DMARC Report Viewer
wget_verify "https://github.com/cry-inc/dmarc-report-viewer/releases/download/2.2.1/linux-x86_64.zip" 126c2341a5df475c3b9f1ac0b3b9c5680c38c6fb /tmp/dmarc.zip
unzip -q /tmp/dmarc.zip -d /tmp/dmarc
hide_output install -m 755 /tmp/dmarc/linux-x86_64/dmarc-report-viewer /usr/local/bin/
rm -f /tmp/dmarc.zip
rm -rf /tmp/dmarc

# On first installation,create configuration
if [ ! -f /etc/default/dmarc_report ]; then
        cp conf/dmarc_report /etc/default/
        management/editconf.py /etc/default/dmarc_report IMAP_HOST=$PRIMARY_HOSTNAME
        
        cp conf/dmarc_report_viewer.service /etc/systemd/system
        systemctl daemon-reload
        
        systemctl start dmarc-report-viewer.service
else
	systemctl restart dmarc-report-viewer.service
fi

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

