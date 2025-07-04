#!/bin/bash
# HTTP: Turn on a web server serving static files
#################################################

source setup/functions.sh # load our functions
source /etc/mailinabox.conf # load global vars

# Some Ubuntu images start off with Apache. Remove it since we
# will use nginx. Use autoremove to remove any Apache dependencies.
if [ -f /usr/sbin/apache2 ]; then
	echo "Removing apache..."
	hide_output apt-get -y purge apache2 apache2-*
	hide_output apt-get -y --purge autoremove
fi

# Install nginx and a PHP FastCGI daemon.
#
# Turn off nginx's default website.

echo "Installing Nginx (web server)..."

apt_install nginx php"${PHP_VER}"-cli php"${PHP_VER}"-fpm idn2 libnginx-mod-http-geoip2

rm -f /etc/nginx/sites-enabled/default

# Copy in a nginx configuration file for common and best-practices
# SSL settings from @konklone. Replace STORAGE_ROOT so it can find
# the DH params.
rm -f /etc/nginx/nginx-ssl.conf # we used to put it here
sed "s#STORAGE_ROOT#$STORAGE_ROOT#" \
	conf/nginx-ssl.conf > /etc/nginx/conf.d/ssl.conf

# Fix some nginx defaults.
#
# The server_names_hash_bucket_size seems to prevent long domain names!
# The default, according to nginx's docs, depends on "the size of the
# processor’s cache line." It could be as low as 32. We fixed it at
# 64 in 2014 to accommodate a long domain name (20 characters?). But
# even at 64, a 58-character domain name won't work (#93), so now
# we're going up to 128.
#
# Drop TLSv1.0, TLSv1.1, following the Mozilla "Intermediate" recommendations
# at https://ssl-config.mozilla.org/#server=nginx&version=1.18.0&config=intermediate&openssl=3.0.2&guideline=5.7.
management/editconf.py /etc/nginx/nginx.conf -s \
	server_names_hash_bucket_size="128;" \
	ssl_protocols="TLSv1.2 TLSv1.3;"

# Tell PHP not to expose its version number in the X-Powered-By header.
management/editconf.py /etc/php/"$PHP_VER"/fpm/php.ini -c ';' \
	expose_php=Off

# Set PHPs default charset to UTF-8, since we use it. See #367.
management/editconf.py /etc/php/"$PHP_VER"/fpm/php.ini -c ';' \
        default_charset="UTF-8"

# Set higher timeout since fts searches with Roundcube may take longer
# than the default 60 seconds. We will also match Roundcube's timeout to the
# same value
management/editconf.py /etc/php/"$PHP_VER"/fpm/php.ini -c ';' \
        default_socket_timeout=180

# Configure the path environment for php-fpm
management/editconf.py /etc/php/"$PHP_VER"/fpm/pool.d/www.conf -c ';' \
	env[PATH]=/usr/local/bin:/usr/bin:/bin \

# We'll configure a php-fpm worker per php application. First do some generic php worker configuration to obtain a common baseline.
# Then configure specific php workers.

if [ -f /etc/php/"$PHP_VER"/fpm/pool.d/www.conf ]; then
	# Change the extension, so it is not used by php anymore
	mv /etc/php/"$PHP_VER"/fpm/pool.d/www.conf /etc/php/"$PHP_VER"/fpm/pool.d/www.conf.unused
fi

# Configure php-fpm based on the amount of memory the machine has
# This is based on https://spot13.com/pmcalculator/ (referenced by Nextcloud) using RAM Buffer = 10% and Process size = 50 MB
# As there will be multiple pools, these numbers are halved. Also, min and max spare servers are decreased a little to decrease
# memory pressure
# The pm=ondemand setting is used for memory constrained machines < 2GB, this is copied over from PR: 1216
TOTAL_PHYSICAL_MEM=$(head -n 1 /proc/meminfo | awk '{print $2}' || /bin/true)
if [ "$TOTAL_PHYSICAL_MEM" -lt 1000000 ]
then
        management/editconf.py /etc/php/"$PHP_VER"/fpm/pool.d/www.conf.unused -c ';' \
                pm=ondemand \
                pm.max_children=7 \
                pm.process_idle_timeout=10s \
                pm.max_requests=500
elif [ "$TOTAL_PHYSICAL_MEM" -lt 2000000 ]
then
        management/editconf.py /etc/php/"$PHP_VER"/fpm/pool.d/www.conf.unused -c ';' \
                pm=ondemand \
                pm.max_children=14 \
                pm.process_idle_timeout=10s \
                pm.max_requests=500
elif [ "$TOTAL_PHYSICAL_MEM" -lt 3000000 ]
then
        management/editconf.py /etc/php/"$PHP_VER"/fpm/pool.d/www.conf.unused -c ';' \
                pm=dynamic \
                pm.max_children=15 \
                pm.start_servers=4 \
                pm.min_spare_servers=2 \
                pm.max_spare_servers=10 \
                pm.max_requests=1000
elif [ "$TOTAL_PHYSICAL_MEM" -lt 5000000 ]
        management/editconf.py /etc/php/"$PHP_VER"/fpm/pool.d/www.conf.unused -c ';' \
                pm=dynamic \
                pm.max_children=28 \
                pm.start_servers=7 \
                pm.min_spare_servers=4 \
                pm.max_spare_servers=21 \
                pm.max_requests=1000
else
        management/editconf.py /etc/php/"$PHP_VER"/fpm/pool.d/www.conf.unused -c ';' \
                pm=dynamic \
                pm.max_children=56 \
                pm.start_servers=14 \
                pm.min_spare_servers=7 \
                pm.max_spare_servers=42 \
                pm.max_requests=1000
fi

# Configure Nextcloud
cp -f /etc/php/"$PHP_VER"/fpm/pool.d/www.conf.unused /etc/php/"$PHP_VER"/fpm/pool.d/nextcloud.conf

sed -i "s/\[www\]/\[nextcloud\]/" /etc/php/"$PHP_VER"/fpm/pool.d/nextcloud.conf

if [ ! id -u nextcloud_php >/dev/null 2>&1 ]; then
	adduser --system --disabled-login --shell /bin/false --no-create-home nextcloud_php
	usermod -a -G www-data nextcloud_php
fi

management/editconf.py /etc/php/"$PHP_VER"/fpm/pool.d/nextcloud.conf -c ';' \
	user=nextcloud_php \
	listen=/run/php/php8.3-fpm-nextcloud.sock

# Configure Snappymail
if [ -d /usr/local/share/snappymail/snappymail ]; then
	cp -f /etc/php/"$PHP_VER"/fpm/pool.d/www.conf.unused /etc/php/"$PHP_VER"/fpm/pool.d/snappymail.conf

	sed -i "s/\[www\]/\[snappymail\]/" /etc/php/"$PHP_VER"/fpm/pool.d/snappymail.conf

	if [ ! id -u snappymail_php >/dev/null 2>&1 ]; then
		adduser --system --disabled-login --shell /bin/false --no-create-home snappymail_php
		usermod -a -G www-data snappymail_php
	fi

	management/editconf.py /etc/php/"$PHP_VER"/fpm/pool.d/snappymail.conf -c ';' \
		user=snappymail_php \
		listen=/run/php/php8.3-fpm-snappymail.sock
fi

# Configure Roundcube
cp -f /etc/php/"$PHP_VER"/fpm/pool.d/www.conf.unused /etc/php/"$PHP_VER"/fpm/pool.d/roundcube.conf

sed -i "s/\[www\]/\[roundcube\]/" /etc/php/"$PHP_VER"/fpm/pool.d/roundcube.conf

if [ ! id -u roundcube_php >/dev/null 2>&1 ]; then
	adduser --system --disabled-login --shell /bin/false --no-create-home roundcube_php
	usermod -a -G www-data roundcube_php
fi

management/editconf.py /etc/php/"$PHP_VER"/fpm/pool.d/roundcube.conf -c ';' \
	user=roundcube_php \
	listen=/run/php/php8.3-fpm-roundcube.sock

# Other nginx settings will be configured by the management service
# since it depends on what domains we're serving, which we don't know
# until mail accounts have been created.

# Create the iOS/OS X Mobile Configuration file which is exposed via the
# nginx configuration at /mailinabox-mobileconfig.
mkdir -p /var/lib/mailinabox
chmod a+rx /var/lib/mailinabox
cat conf/ios-profile.xml \
	| sed "s/PRIMARY_HOSTNAME/$PRIMARY_HOSTNAME/" \
	| sed "s/UUID1/$(cat /proc/sys/kernel/random/uuid)/" \
	| sed "s/UUID2/$(cat /proc/sys/kernel/random/uuid)/" \
	| sed "s/UUID3/$(cat /proc/sys/kernel/random/uuid)/" \
	| sed "s/UUID4/$(cat /proc/sys/kernel/random/uuid)/" \
	 > /var/lib/mailinabox/mobileconfig.xml
chmod a+r /var/lib/mailinabox/mobileconfig.xml

# Create the Mozilla Auto-configuration file which is exposed via the
# nginx configuration at /.well-known/autoconfig/mail/config-v1.1.xml.
# The format of the file is documented at:
# https://wiki.mozilla.org/Thunderbird:Autoconfiguration:ConfigFileFormat
# and https://developer.mozilla.org/en-US/docs/Mozilla/Thunderbird/Autoconfiguration/FileFormat/HowTo.
cat conf/mozilla-autoconfig.xml \
	| sed "s/PRIMARY_HOSTNAME/$PRIMARY_HOSTNAME/" \
	 > /var/lib/mailinabox/mozilla-autoconfig.xml
chmod a+r /var/lib/mailinabox/mozilla-autoconfig.xml

# Create a generic mta-sts.txt file which is exposed via the
# nginx configuration at /.well-known/mta-sts.txt
# more documentation is available on:
# https://www.uriports.com/blog/mta-sts-explained/
# default mode is "enforce". In /etc/mailinabox.conf change
# "MTA_STS_MODE=testing" which means "Messages will be delivered
# as though there was no failure but a report will be sent if
# TLS-RPT is configured" if you are not sure you want this yet. Or "none".
PUNY_PRIMARY_HOSTNAME=$(echo "$PRIMARY_HOSTNAME" | idn2)
cat conf/mta-sts.txt \
        | sed "s/MODE/${MTA_STS_MODE}/" \
        | sed "s/PRIMARY_HOSTNAME/$PUNY_PRIMARY_HOSTNAME/" \
         > /var/lib/mailinabox/mta-sts-long.txt
chmod a+r /var/lib/mailinabox/mta-sts-long.txt

# Also create a mta-sts file with a short period, to be used when e.g. moving
# the box. This will be coupled to the short ttl option in the DNS configuration.
cat conf/mta-sts-short.txt \
        | sed "s/PRIMARY_HOSTNAME/$PUNY_PRIMARY_HOSTNAME/" \
         > /var/lib/mailinabox/mta-sts-short.txt
chmod a+r /var/lib/mailinabox/mta-sts-short.txt

# make a default homepage
if [ -d "$STORAGE_ROOT/www/static" ]; then mv "$STORAGE_ROOT/www/static" "$STORAGE_ROOT/www/default"; fi # migration #NODOC
mkdir -p "$STORAGE_ROOT/www/default"
if [ ! -f "$STORAGE_ROOT/www/default/index.html" ]; then
	cp conf/www_default.html "$STORAGE_ROOT/www/default/index.html"
fi
chown -R "$STORAGE_USER" "$STORAGE_ROOT/www"

# Copy geoblock config file, but only if it does not exist to keep user config
if [ ! -f /etc/nginx/conf.d/10-geoblock.conf ]; then
    cp -f conf/nginx/conf.d/10-geoblock.conf /etc/nginx/conf.d/
fi

# touch logfiles that might not exist
touch /var/log/nginx/geoipblock.log
chown www-data /var/log/nginx/geoipblock.log

# Start services.
restart_service nginx
restart_service php"$PHP_VER"-fpm

# Open ports.
ufw_allow http
ufw_allow https
