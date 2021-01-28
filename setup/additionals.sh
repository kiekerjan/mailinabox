source /etc/mailinabox.conf

cp -f conf/local_clean_mail /etc/cron.weekly/
chmod +x /etc/cron.weekly
sed -i "s/PRIMARY_HOSTNAME/$PRIMARY_HOSTNAME/g" /etc/cron.weekly/local_clean_mail
