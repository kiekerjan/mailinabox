source setup/functions.sh

echo Installing geoip packages...

# Install some packages
apt_install geoip-database-extra libgeoip1 libnginx-mod-http-geoip

# geo ip filtering of ssh entries, based on https://www.axllent.org/docs/ssh-geoip/#disqus_thread

# Install geo ip lookup tool
gunzip -c tools/goiplookup.gz > /usr/local/bin/goiplookup
chmod +x /usr/local/bin/goiplookup
goiplookup db-update

# Install geo ip filter script
cp -f setup/geoipfilter.sh /usr/local/bin/

# Install only if not yet exists, to keep user config
if [ ! -f /etc/geoiplookup.conf ]; then
    cp -f conf/geoiplookup.conf /etc/
fi

# Add sshd entries for hosts.deny and hosts.allow
if grep -Fxq "sshd: ALL" /etc/hosts.deny
then
    echo hosts.deny already configured
else
    sed -i '/sshd: /d' /etc/hosts.deny
    echo "sshd: ALL" >> /etc/hosts.deny
fi

if grep -Fxq "sshd: ALL: aclexec /usr/local/bin/geoipfilter.sh %a %s" /etc/hosts.allow
then
    echo hosts.allow already configured
else
    # Make sure all sshd lines are removed
    sed -i '/sshd: /d' /etc/hosts.allow
    echo "sshd: ALL: aclexec /usr/local/bin/geoipfilter.sh %a %s" >> /etc/hosts.allow
fi

# geo ip filtering of nginx access log, based on 
# https://guides.wp-bullet.com/blocking-country-and-continent-with-nginx-geoip-on-ubuntu-18-04/

## Install geo ip lookup files

# Move old file away if it exists
if [ -f "/usr/share/GeoIP/GeoIP.dat" ]; then
    mv -f /usr/share/GeoIP/GeoIP.dat /usr/share/GeoIP/GeoIP.dat.bak
fi

hide_output wget -P /usr/share/GeoIP/ https://dl.miyuru.lk/geoip/maxmind/country/maxmind.dat.gz

if [ -f "/usr/share/GeoIP/maxmind.dat.gz" ]; then
    gunzip -c /usr/share/GeoIP/maxmind.dat.gz > /usr/share/GeoIP/GeoIP.dat
else
    echo Did not correctly download maxmind geoip database
fi

# If new file is not created, move the old file back
if [ ! -f "/usr/share/GeoIP/GeoIP.dat" ]; then
    echo GeoIP.dat was not created
    
    if [ -f "/usr/share/GeoIP/GeoIP.dat.bak" ]; then
        mv /usr/share/GeoIP/GeoIP.dat.bak /usr/share/GeoIP/GeoIP.dat
    fi
fi