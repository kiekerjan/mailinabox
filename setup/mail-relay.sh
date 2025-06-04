#!/bin/bash

source /etc/mailinabox.conf
source setup/functions.sh

# ### Install stunnel for postfix relay

apt_install stunnel4

# Install combine_certs.sh tool
hide_output install -m 755 tools/combine_certs.sh /usr/local/bin

# stunnel certification creation
# ssl certificates have already been created (might have been self signed)

RELAYDOMAIN=$(get_miab_setting mail.relay.relaydomain)

if [ -n $RELAYDOMAIN ]; then

	cat > /etc/cron.daily/miab_stunnel_cert << EOF;
#!/bin/bash
# Mail-in-a-Box
# Update stunnel SSL certificate
if [ $STORAGE_ROOT/ssl/ssl_certificate.pem -nt /etc/stunnel/cert.pem ]; then
        combine_certs.sh $STORAGE_ROOT/ssl ssl_certificate.pem ssl_private_key.pem /etc/stunnel cert.pem > /dev/null
        chmod 700 /etc/stunnel/cert.pem
fi
EOF
	chmod +x /etc/cron.daily/miab_stunnel_cert
	hide_output /etc/cron.daily/miab_stunnel_cert
	
	RELAYPORT=$(get_miab_setting mail.relay.port)
	
	if [ -z $RELAYPORT ]; then
		RELAYPORT=465
	fi
	
	cat > /etc/stunnel/miabrelay.conf << EOF;
# stunnel configuration for Postfix relay

pid = /var/lib/stunnel4/mxrelay.pid
output = /var/log/stunnel4/mxrelay.log

setuid = stunnel4
setgid = stunnel4

# debug = 7
debug = 5
syslog = no

#  run as a background service
foreground = yes

# Connect to local TLS
[smtp-tls-server]
accept = 127.0.0.1:11001
connect = 127.0.0.1:11002
protocol = smtp
cert = /etc/stunnel/cert.pem

# Service to forward SMTP to remote Postfix server with implicit TLS
[smtp-tls-wrapper]
client = yes
# Local port to listen for unencrypted SMTP
accept = 127.0.0.1:11002
# Remote server with implicit TLS
connect = $RELAYDOMAIN:$RELAYPORT
CApath = /etc/ssl/certs
verifyChain = yes
EOF

	systemctl enable stunnel@miabrelay.service
	systemctl start stunnel@miabrelay.service
	
	# Create reply filter
	cp -f conf/postfix/reply_filter /etc/postfix/
	
	# Create password map
	RELAYUSER=$(get_miab_setting mail.relay.user)
	RELAYPASSWORD=$(get_miab_setting mail.relay.password)
	
	cat > /etc/postfix/sasl_passwd << EOF;
\[$$RELAYDOMAIN\]:$RELAYPORT $RELAYUSER:$RELAYPASSWORD
EOF
	chmod 600 /etc/postfix/sasl_passwd
	postmap /etc/postfix/sasl_passwd
	
	# Configuration of fallback relay
	management/editconf.py /etc/postfix/main.cf \
		smtp_reply_filter=pcre:/etc/postfix/reply_filter \
		smtp_sasl_password_maps="hash:/etc/postfix/sasl_passwd" \
		smtp_sasl_auth_enable=yes \
		smtp_sasl_tls_security_options=noanonymous \
		smtp_fallback_relay = "[127.0.0.1]:11001"

	systemctl restart postfix
else
	systemctl stop stunnel@miabrelay.service
	systemctl disable stunnel@miabrelay.service
	
	rm /etc/cron.daily/miab_stunnel_cert
	rm /etc/stunnel/miabrelay.conf
	rm /etc/postfix/reply_filter
	rm /etc/postfix/sasl_passwd
	rm /etc/postfix/sasl_passwd.db
	
	# Clear configuration	
	management/editconf.py /etc/postfix/main.cf -e \
		smtp_fallback_relay= \
		smtp_reply_filter= \
		smtp_sasl_password_maps= \
		smtp_sasl_auth_enable= \
		smtp_sasl_tls_security_options=
	
	systemctl restart postfix
fi

