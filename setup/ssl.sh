#!/bin/bash
#
# Private key, SSL certificate, Diffie-Hellman bits files
# -------------------------------------------

# Create a private key, a self-signed SSL certificate, and some
# Diffie-Hellman cipher bits, if they have not yet been created.
#
# The private key and certificate are used for:
#
#  * DNSSEC DANE TLSA records
#  * IMAP
#  * SMTP (opportunistic TLS for port 25 and submission on ports 465/587)
#  * HTTPS
#
# The certificate is created with its CN set to the PRIMARY_HOSTNAME. It is
# also used for other domains served over HTTPS until the user installs a
# better certificate for those domains.
#
# The Diffie-Hellman cipher bits are used for SMTP and HTTPS, when a
# Diffie-Hellman cipher is selected during TLS negotiation. Diffie-Hellman
# provides Perfect Forward Secrecy.

source setup/functions.sh # load our functions
source /etc/mailinabox.conf # load global vars

# Show a status line if we are going to take any action in this file.
if  [ ! -f /usr/bin/openssl ] \
 || [ ! -f "$STORAGE_ROOT/ssl/ssl_private_key.pem" ] \
 || [ ! -f "$STORAGE_ROOT/ssl/ssl_certificate.pem" ] \
 || [ ! -f "$STORAGE_ROOT/ssl/dh4096.pem" ]; then
	echo "Creating initial SSL certificate and perfect forward secrecy Diffie-Hellman parameters..."
fi

# Install openssl.

apt_install openssl

# Create a directory to store TLS-related things like "SSL" certificates.

mkdir -p "$STORAGE_ROOT/ssl"

# make directory readable
chmod 755 $STORAGE_ROOT/ssl

# Generate a new private key. Every certificate this box ever gets is issued
# against this one key, and the DANE TLSA record is its public key's hash.
#
# The key is only as good as the entropy available to openssl; see system.sh
# for how /dev/urandom is seeded before this runs.
if [ ! -f "$STORAGE_ROOT/ssl/ssl_private_key.pem" ]; then
	# Set the umask so the key file is never world-readable.
	(umask 077; hide_output \
		openssl ecparam -name prime256v1 -genkey -noout -out "$STORAGE_ROOT/ssl/ssl_private_key.pem")
fi

# Generate a self-signed SSL certificate because things like nginx, dovecot,
# etc. won't even start without some certificate in place, and we need nginx
# so we can offer the user a control panel to install a better certificate.
if [ ! -f "$STORAGE_ROOT/ssl/ssl_certificate.pem" ]; then
	# Generate a certificate signing request.
	CSR=/tmp/ssl_cert_sign_req-$$.csr
	hide_output \
	openssl req -new -key "$STORAGE_ROOT/ssl/ssl_private_key.pem" -out $CSR \
	  -sha256 -subj "/CN=$PRIMARY_HOSTNAME"

	# Generate the self-signed certificate.
	CERT=$STORAGE_ROOT/ssl/$PRIMARY_HOSTNAME-selfsigned-$(date --rfc-3339=date | sed s/-//g).pem
	hide_output \
	openssl x509 -req -days 365 \
	  -in $CSR -signkey "$STORAGE_ROOT/ssl/ssl_private_key.pem" -out "$CERT"

	# Delete the certificate signing request because it has no other purpose.
	rm -f $CSR

	# Symlink the certificate into the system certificate path, so system services
	# can find it.
	ln -s "$CERT" "$STORAGE_ROOT/ssl/ssl_certificate.pem"
fi

# We no longer generate Diffie-Hellman cipher bits. Following rfc7919 we use
# a predefined finite field group, in this case ffdhe4096 from
# https://raw.githubusercontent.com/internetstandards/dhe_groups/master/ffdhe4096.pem
cp -f conf/dh4096.pem $STORAGE_ROOT/ssl/

# Cleanup expired SSL certificates from $STORAGE_ROOT/ssl daily
cat > /etc/cron.daily/mailinabox-ssl-cleanup << EOF;
#!/bin/bash
# Mail-in-a-Box
# Cleanup expired SSL certificates
$(pwd)/tools/ssl_cleanup
EOF
chmod +x /etc/cron.daily/mailinabox-ssl-cleanup
