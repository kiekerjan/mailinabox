#!/bin/bash
# # Nextcloud
##########################

source setup/functions.sh # load our functions
source /etc/mailinabox.conf # load global vars

# ### Installing Nextcloud

echo "Installing Nextcloud (contacts/calendar)..."

INSTALL_DIR=/usr/local/lib/nextcloud
CLOUD_DIR=$INSTALL_DIR/cloud

# Nextcloud core and app (plugin) versions to install.
# With each version we store a hash to ensure we install what we expect.

# Nextcloud core
# --------------
# * See https://nextcloud.com/changelog for the latest version.
# * Check https://docs.nextcloud.com/server/latest/admin_manual/installation/system_requirements.html
#   for whether it supports the version of PHP available on this machine.
# * Since Nextcloud only supports upgrades from consecutive major versions,
#   we automatically install intermediate versions as needed.
# * The hash is the SHA1 hash of the ZIP package, which you can find by just running this script and
#   copying it from the error message when it doesn't match what is below.
nextcloud_ver=29.0.10
nextcloud_hash=171dec99881e959bb8ed5956fd7430c52c6ae5c7

# Nextcloud apps
# --------------
# * Find the most recent tag that is compatible with the Nextcloud version above by:
#   https://github.com/nextcloud-releases/contacts/tags
#   https://github.com/nextcloud-releases/calendar/tags
#   https://github.com/nextcloud/user_external/tags
#
# * For these three packages, contact, calendar and user_external, the hash is the SHA1 hash of
# the ZIP package, which you can find by just running this script and copying it from
# the error message when it doesn't match what is below:

# Always ensure the versions are supported, see https://apps.nextcloud.com/apps/contacts
contacts_ver=6.0.1
contacts_hash=5ce799cce6943e7fa531ce53aa1cbe01b7b6f1be

# Always ensure the versions are supported, see https://apps.nextcloud.com/apps/calendar
calendar_ver=4.7.16
calendar_hash=1c39ce674027a8710800d056a7cdd0c5c974781d

# Always ensure the versions are supported, see https://apps.nextcloud.com/apps/user_external
user_external_ver=3.4.0
user_external_hash=7f9d8f4dd6adb85a0e3d7622d85eeb7bfe53f3b4

# Developer advice (test plan)
# ----------------------------
# When upgrading above versions, how to test?
#
# 1. Enter your server instance (or on the Vagrant image)
# 1. Git clone <your fork>
# 2. Git checkout <your fork>
# 3. Run `sudo ./setup/nextcloud.sh`
# 4. Ensure the installation completes. If any hashes mismatch, correct them.
# 5. Enter nextcloud web, run following tests:
# 5.1 You still can create, edit and delete contacts
# 5.2 You still can create, edit and delete calendar events
# 5.3 You still can create, edit and delete users
# 5.4 Go to Administration > Logs and ensure no new errors are shown

# Clear prior packages and install dependencies from apt.
apt-get purge -qq -y owncloud* # we used to use the package manager

apt_install curl php"${PHP_VER}" php"${PHP_VER}"-fpm \
	php"${PHP_VER}"-cli php"${PHP_VER}"-sqlite3 php"${PHP_VER}"-gd php"${PHP_VER}"-imap php"${PHP_VER}"-curl \
	php"${PHP_VER}"-dev php"${PHP_VER}"-xml php"${PHP_VER}"-mbstring php"${PHP_VER}"-zip php"${PHP_VER}"-apcu \
	php"${PHP_VER}"-intl php"${PHP_VER}"-imagick php"${PHP_VER}"-gmp php"${PHP_VER}"-bcmath

# Enable APC before Nextcloud tools are run.
management/editconf.py /etc/php/$PHP_VER/mods-available/apcu.ini -c ';' \
    apc.enabled=1 \
    apc.enable_cli=1

InstallNextcloud() {

	version=$1
	hash=$2
	version_contacts=$3
	hash_contacts=$4
	version_calendar=$5
	hash_calendar=$6
	version_user_external=${7:-}
	hash_user_external=${8:-}

	echo
	echo "Upgrading to Nextcloud version $version"
	echo

	# Download and verify
	wget_verify "https://download.nextcloud.com/server/releases/nextcloud-$version.zip" "$hash" /tmp/nextcloud.zip

	# Remove the current owncloud/Nextcloud
	rm -rf $CLOUD_DIR

	# Extract ownCloud/Nextcloud
	unzip -q /tmp/nextcloud.zip -d $INSTALL_DIR
	mv $INSTALL_DIR/nextcloud $CLOUD_DIR
	rm -f /tmp/nextcloud.zip

	# Empty the skeleton dir to save some space for each new user
	rm -rf $CLOUD_DIR/core/skeleton/*

	# The two apps we actually want are not in Nextcloud core. Download the releases from
	# their github repositories.
	mkdir -p $CLOUD_DIR/apps

	wget_verify "https://github.com/nextcloud-releases/contacts/archive/refs/tags/v$version_contacts.tar.gz" "$hash_contacts" /tmp/contacts.tgz
	tar xf /tmp/contacts.tgz -C $CLOUD_DIR/apps/
	rm /tmp/contacts.tgz

	wget_verify "https://github.com/nextcloud-releases/calendar/archive/refs/tags/v$version_calendar.tar.gz" "$hash_calendar" /tmp/calendar.tgz
	tar xf /tmp/calendar.tgz -C $CLOUD_DIR/apps/
	rm /tmp/calendar.tgz

	# Starting with Nextcloud 15, the app user_external is no longer included in Nextcloud core,
	# we will install from their github repository.
	if [ -n "$version_user_external" ]; then
		wget_verify "https://github.com/nextcloud-releases/user_external/releases/download/v$version_user_external/user_external-v$version_user_external.tar.gz" "$hash_user_external" /tmp/user_external.tgz
		tar -xf /tmp/user_external.tgz -C $CLOUD_DIR/apps/
		rm /tmp/user_external.tgz
	fi

	# Fix weird permissions.
	chmod 750 $CLOUD_DIR/{apps,config}

	# Create a symlink to the config.php in STORAGE_ROOT (for upgrades we're restoring the symlink we previously
	# put in, and in new installs we're creating a symlink and will create the actual config later).
	ln -sf "$STORAGE_ROOT/owncloud/config.php" $CLOUD_DIR/config/config.php

	# Make sure permissions are correct or the upgrade step won't run.
	# $STORAGE_ROOT/owncloud may not yet exist, so use -f to suppress
	# that error.
	chown -f -R www-data:www-data "$STORAGE_ROOT/owncloud" $INSTALL_DIR || /bin/true

	# If this isn't a new installation, immediately run the upgrade script.
	# Then check for success (0=ok and 3=no upgrade needed, both are success).
	if [ -e "$STORAGE_ROOT/owncloud/owncloud.db" ]; then
		# ownCloud 8.1.1 broke upgrades. It may fail on the first attempt, but
		# that can be OK.
		sudo -u www-data php $CLOUD_DIR/occ upgrade
		E=$?
		if [ $E -ne 0 ] && [ $E -ne 3 ]; then
			echo "Trying ownCloud upgrade again to work around ownCloud upgrade bug..."
			sudo -u www-data php $CLOUD_DIR/occ upgrade
			E=$?
			if [ $E -ne 0 ] && [ $E -ne 3 ]; then exit 1; fi
			sudo -u www-data php $CLOUD_DIR/occ maintenance:mode --off
			echo "...which seemed to work."
		fi

		# Add missing indices. NextCloud didn't include this in the normal upgrade because it might take some time.
		sudo -u www-data php"$PHP_VER" $CLOUD_DIR/occ db:add-missing-indices
		sudo -u www-data php"$PHP_VER" $CLOUD_DIR/occ db:add-missing-primary-keys

		# Run conversion to BigInt identifiers, this process may take some time on large tables.
		sudo -u www-data php"$PHP_VER" $CLOUD_DIR/occ db:convert-filecache-bigint --no-interaction
	fi
}

# Current Nextcloud Version, #1623
# Checking /usr/local/lib/nextcloud/cloud/version.php shows version of the Nextcloud application, not the DB
# $STORAGE_ROOT/owncloud is kept together even during a backup. It is better to rely on config.php than
# version.php since the restore procedure can leave the system in a state where you have a newer Nextcloud
# application version than the database.

# If config.php exists, get version number, otherwise CURRENT_NEXTCLOUD_VER is empty.
if [ -f "$STORAGE_ROOT/owncloud/config.php" ]; then
	CURRENT_NEXTCLOUD_VER=$(php"$PHP_VER" -r "include(\"$STORAGE_ROOT/owncloud/config.php\"); echo(\$CONFIG['version']);")
else
	CURRENT_NEXTCLOUD_VER=""
fi

# If the Nextcloud directory is missing (never been installed before, or the nextcloud version to be installed is different
# from the version currently installed, do the install/upgrade
if [ ! -d $CLOUD_DIR ] || [[ ! ${CURRENT_NEXTCLOUD_VER} =~ ^$nextcloud_ver ]]; then

	# Stop php-fpm if running. If they are not running (which happens on a previously failed install), dont bail.
	service php"$PHP_VER"-fpm stop &> /dev/null || /bin/true

	# Remove backups older than 30 days
	find "$STORAGE_ROOT/owncloud-backup" -mindepth 1 -maxdepth 1 -iname "*" -type d -ctime +30 -exec rm -rf {} + 2>/dev/null || true

	# Backup the existing ownCloud/Nextcloud.
	# Create a backup directory to store the current installation and database to
	BACKUP_DIRECTORY=$STORAGE_ROOT/owncloud-backup/$(date +"%Y-%m-%d-%T")
	mkdir -p "$BACKUP_DIRECTORY"
	if [ -d $CLOUD_DIR ]; then
		echo "Upgrading Nextcloud --- backing up existing installation, configuration, and database to directory to $BACKUP_DIRECTORY..."
		cp -r $CLOUD_DIR "$BACKUP_DIRECTORY/owncloud-install"
	fi
	if [ -e "$STORAGE_ROOT/owncloud/owncloud.db" ]; then
		cp "$STORAGE_ROOT/owncloud/owncloud.db" "$BACKUP_DIRECTORY"
	fi
	if [ -e "$STORAGE_ROOT/owncloud/config.php" ]; then
		cp "$STORAGE_ROOT/owncloud/config.php" "$BACKUP_DIRECTORY"
	fi

	# If ownCloud or Nextcloud was previously installed....
	if [ -n "${CURRENT_NEXTCLOUD_VER}" ]; then
		# Database migrations from ownCloud are no longer possible because ownCloud cannot be run under
		# PHP 7.

		if [ -e "$STORAGE_ROOT/owncloud/config.php" ]; then
			# Remove the read-onlyness of the config, which is needed for migrations, especially for v24
			sed -i -e '/config_is_read_only/d' "$STORAGE_ROOT/owncloud/config.php"
		fi

		if [[ ${CURRENT_NEXTCLOUD_VER} =~ ^[89] ]]; then
			echo "Upgrades from Mail-in-a-Box prior to v0.28 (dated July 30, 2018) with Nextcloud < 13.0.6 (you have ownCloud 8 or 9) are not supported. Upgrade to Mail-in-a-Box version v0.30 first. Setup will continue, but skip the Nextcloud migration."
			return 0
		elif [[ ${CURRENT_NEXTCLOUD_VER} =~ ^1[012] ]]; then
			echo "Upgrades from Mail-in-a-Box prior to v0.28 (dated July 30, 2018) with Nextcloud < 13.0.6 (you have ownCloud 10, 11 or 12) are not supported. Upgrade to Mail-in-a-Box version v0.30 first. Setup will continue, but skip the Nextcloud migration."
			return 0
		elif [[ ${CURRENT_NEXTCLOUD_VER} =~ ^1[3456789] ]]; then
			echo "Upgrades from Mail-in-a-Box prior to v60 with Nextcloud 19 or earlier are not supported. Upgrade to the latest Mail-in-a-Box version supported on your machine first. Setup will continue, but skip the Nextcloud migration."
			return 0
		fi

		# Hint: whenever you bump, remember this:
		# - Run a server with the previous version
		# - On a new if-else block, copy the versions/hashes from the previous version
		# - Run sudo ./setup/start.sh on the new machine. Upon completion, test its basic functionalities.

		if [[ ${CURRENT_NEXTCLOUD_VER} =~ ^20 ]]; then
			# Version 20 is the latest version from the 18.04 version of miab. To upgrade to version 21, install php8.0. This is
			# not supported by version 20, but that does not matter, as the InstallNextcloud function only runs the version 21 code.

			# Prevent installation of old packages
			apt-mark hold php7.0-apcu php7.1-apcu php7.2-apcu php7.3-apcu php7.4-apcu

			# Install older php version
			apt_install php8.0 php8.0-fpm php8.0-apcu php8.0-cli php8.0-sqlite3 php8.0-gd php8.0-imap \
				php8.0-curl php8.0-dev php8.0-xml php8.0-mbstring php8.0-zip

			PHP_VER=8.0

			management/editconf.py /etc/php/"$PHP_VER"/mods-available/apcu.ini -c ';' \
				apc.enabled=1	\
				apc.enable_cli=1

			# Install nextcloud, this also updates user_external to 2.1.0
			InstallNextcloud 21.0.7 f5c7079c5b56ce1e301c6a27c0d975d608bb01c9 4.0.7 45e7cf4bfe99cd8d03625cf9e5a1bb2e90549136 3.0.4 d0284b68135777ec9ca713c307216165b294d0fe 2.1.0 41d4c57371bd085d68421b52ab232092d7dfc882
			CURRENT_NEXTCLOUD_VER="21.0.7"
		fi
		if [[ ${CURRENT_NEXTCLOUD_VER} =~ ^21 ]]; then
			# Nextcloud version 22
			InstallNextcloud 22.2.3 58d2d897ba22a057aa03d29c762c5306211fefd2 4.0.7 45e7cf4bfe99cd8d03625cf9e5a1bb2e90549136 3.0.4 d0284b68135777ec9ca713c307216165b294d0fe 2.1.0 41d4c57371bd085d68421b52ab232092d7dfc882
			CURRENT_NEXTCLOUD_VER="22.2.3"
		fi
		if [[ ${CURRENT_NEXTCLOUD_VER} =~ ^22 ]]; then
			# Nextcloud version 23
			# When installing this, we also remove the old php version
			InstallNextcloud 23.0.12 d138641b8e7aabebe69bb3ec7c79a714d122f729 4.1.0 697f6b4a664e928d72414ea2731cb2c9d1dc3077 3.2.2 ce4030ab57f523f33d5396c6a81396d440756f5f 3.0.0 0df781b261f55bbde73d8c92da3f99397000972f
			CURRENT_NEXTCLOUD_VER="23.0.12"

			apt-get purge -qq -y php8.0 php8.0-fpm php8.0-apcu php8.0-cli php8.0-sqlite3 php8.0-gd \
				php8.0-imap php8.0-curl php8.0-dev php8.0-xml php8.0-mbstring php8.0-zip \
				php8.0-common php8.0-opcache php8.0-readline

			PHP_VER=8.1
		fi
		if [[ ${CURRENT_NEXTCLOUD_VER} =~ ^23 ]]; then
			# Install nextcloud 24
			InstallNextcloud 24.0.12 7aa5d61632c1ccf4ca3ff00fb6b295d318c05599 4.1.0 697f6b4a664e928d72414ea2731cb2c9d1dc3077 3.2.2 ce4030ab57f523f33d5396c6a81396d440756f5f 3.1.0 399fe1150b28a69aaf5bfcad3227e85706604a44
			CURRENT_NEXTCLOUD_VER="24.0.12"
		fi
		if [[ ${CURRENT_NEXTCLOUD_VER} =~ ^24 ]]; then
			# Install nextcloud 25
			InstallNextcloud 25.0.7 a5a565c916355005c7b408dd41a1e53505e1a080 5.3.0 4b0a6666374e3b55cfd2ae9b72e1d458b87d4c8c 4.4.2 21a42e15806adc9b2618760ef94f1797ef399e2f 3.2.0 67ce8cbf8990b9d6517523d7236dcfb7f74b0201
			CURRENT_NEXTCLOUD_VER="25.0.7"
		fi
		if [[ ${CURRENT_NEXTCLOUD_VER} =~ ^25 ]]; then
			# Install nextcloud 26
			InstallNextcloud 26.0.8 a8eacbd39cf4a34a6247d3bf479ff6efc0fef3c8 5.4.2 d38c9e16b377c05b5114e70b3b0c3d3f1f1d10f6 4.5.3 7c974d4f092886e8932c6c3ae34532c30a3fcea9 3.2.0 67ce8cbf8990b9d6517523d7236dcfb7f74b0201
			CURRENT_NEXTCLOUD_VER="26.0.8"
		fi
		if [[ ${CURRENT_NEXTCLOUD_VER} =~ ^26 ]]; then
			# Install nextcloud 27
			InstallNextcloud 27.1.9 4797a2f1f7ffcedca7c0917f913d983b75ed22fd 5.5.3 799550f38e46764d90fa32ca1a6535dccd8316e5 4.7.2 9222953e5654c151604e082c0d5907dcc651d3d7 3.3.0 49800e8ca61391965ce8a75eaaf92a8037185375
			CURRENT_NEXTCLOUD_VER="27.1.9"
		fi
		if [[ ${CURRENT_NEXTCLOUD_VER} =~ ^27 ]]; then
			# Install nextcloud 28
			InstallNextcloud 28.0.10 24edd63bdc005ff39607831ed6cc2cac7278d41a 5.5.3 799550f38e46764d90fa32ca1a6535dccd8316e5 4.7.16 1c39ce674027a8710800d056a7cdd0c5c974781d 3.4.0 7f9d8f4dd6adb85a0e3d7622d85eeb7bfe53f3b4
			CURRENT_NEXTCLOUD_VER="28.0.10"
		fi
	fi

# ### Document nextcloud versions, php versions and user_external plugin compatibility
# nextcloud version - supported php versions
#
# * 20                - 7.2, 7.3, 7.4
# * 21                - 7.3, 7.4, 8.0
# * 22                - 7.3, 7.4, 8.0
# * 23                - 7.3, 7.4, 8.0
# * 24                - 7.4, 8.0, 8.1
# * 25		    - 7.4, 8.0, 8.1
# * 26		    - 8.0, 8.1, 8.2
# * 27                - 8.0 (d), 8.1, 8.2 (r)
# * 28		    - 8.0 (d), 8.1, 8.2 (r), 8.3
# * 29		    - 8.0 (d), 8.1, 8.2 (r), 8.3
# * 30		    - 8.1 (d), 8.2, 8.3 (r)
# * ubuntu 18.04 has php 7.2
# * ubuntu 22.04 has php 8.1
# * ubuntu 24.04 has php 8.3
# * user_external 2.1.0 supports version 21-22
# * user_external 3.0.0 supports version 22-24
# * user_external 3.1.0 supports version 22-25
# * user_external 3.2.0 supports version 25-27
# * user_external 3.3.0 supports version 25-28
# * user_external 3.4.0 supports version 25-29

	# ### Install latest nextcloud
	InstallNextcloud $nextcloud_ver $nextcloud_hash $contacts_ver $contacts_hash $calendar_ver $calendar_hash $user_external_ver $user_external_hash
fi

# ### Configuring Nextcloud

# Setup Nextcloud if the Nextcloud database does not yet exist. Running setup when
# the database does exist wipes the database and user data.
if [ ! -f "$STORAGE_ROOT/owncloud/owncloud.db" ]; then
	# Create user data directory
	mkdir -p "$STORAGE_ROOT/owncloud"

	# Create an initial configuration file.
	instanceid=oc$(echo "$PRIMARY_HOSTNAME" | sha1sum | fold -w 10 | head -n 1)
	cat > "$STORAGE_ROOT/owncloud/config.php" <<EOF;
<?php
\$CONFIG = array (
  'datadirectory' => '$STORAGE_ROOT/owncloud',

  'instanceid' => '$instanceid',

  'forcessl' => true, # if unset/false, Nextcloud sends a HSTS=0 header, which conflicts with nginx config

  'overwritewebroot' => '/cloud',
  'overwrite.cli.url' => '/cloud',
  'user_backends' => array(
    array(
      'class' => '\OCA\UserExternal\IMAP',
          'arguments' => array(
        '127.0.0.1', 143, null, null, false, false
         ),
    ),
  ),
  'memcache.local' => '\OC\Memcache\APCu',
);
?>
EOF

	# Create an auto-configuration file to fill in database settings
	# when the install script is run. Make an administrator account
	# here or else the install can't finish.
	adminpassword=$(dd if=/dev/urandom bs=1 count=40 2>/dev/null | sha1sum | fold -w 30 | head -n 1)
	cat > $CLOUD_DIR/config/autoconfig.php <<EOF;
<?php
\$AUTOCONFIG = array (
  # storage/database
  'directory' => '$STORAGE_ROOT/owncloud',
  'dbtype' => 'sqlite3',

  # create an administrator account with a random password so that
  # the user does not have to enter anything on first load of Nextcloud
  'adminlogin'    => 'root',
  'adminpass'     => '$adminpassword',
);
?>
EOF

	# Set permissions
	chown -R www-data:www-data "$STORAGE_ROOT/owncloud" $CLOUD_DIR

	# Execute Nextcloud's setup step, which creates the Nextcloud sqlite database.
	# It also wipes it if it exists. And it updates config.php with database
	# settings and deletes the autoconfig.php file.
	(cd $CLOUD_DIR || exit; sudo -u www-data php"$PHP_VER" $CLOUD_DIR/index.php;)
fi

# Update config.php.
# * trusted_domains is reset to localhost by autoconfig starting with ownCloud 8.1.1,
#   so set it here. It also can change if the box's PRIMARY_HOSTNAME changes, so
#   this will make sure it has the right value.
# * Some settings weren't included in previous versions of Mail-in-a-Box.
# * We need to set the timezone to the system timezone to allow fail2ban to ban
#   users within the proper timeframe
# * We need to set the logdateformat to something that will work correctly with fail2ban
# * mail_domain' needs to be set every time we run the setup. Making sure we are setting
#   the correct domain name if the domain is being change from the previous setup.
# Use PHP to read the settings file, modify it, and write out the new settings array.
TIMEZONE=$(cat /etc/timezone)
CONFIG_TEMP=$(/bin/mktemp)
php"$PHP_VER" <<EOF > "$CONFIG_TEMP" && mv "$CONFIG_TEMP" "$STORAGE_ROOT/owncloud/config.php";
<?php
include("$STORAGE_ROOT/owncloud/config.php");

\$CONFIG['config_is_read_only'] = false;

\$CONFIG['trusted_domains'] = array('$PRIMARY_HOSTNAME');

\$CONFIG['memcache.local'] = '\OC\Memcache\APCu';
\$CONFIG['overwrite.cli.url'] = 'https://${PRIMARY_HOSTNAME}/cloud';

\$CONFIG['logtimezone'] = '$TIMEZONE';
\$CONFIG['logdateformat'] = 'Y-m-d H:i:s';
\$CONFIG['log_type'] = 'syslog';
\$CONFIG['syslog_tag'] = 'Nextcloud';

\$CONFIG['system_addressbook_exposed'] = 'no';
\$CONFIG['user_backends'] = array(
  array(
    'class' => '\OCA\UserExternal\IMAP',
    'arguments' => array(
      '127.0.0.1', 143, null, null, false, false
    ),
  ),
);

\$CONFIG['mail_domain'] = '$PRIMARY_HOSTNAME';
\$CONFIG['mail_from_address'] = 'administrator'; # just the local part, matches the required administrator alias on mail_domain/$PRIMARY_HOSTNAME
\$CONFIG['mail_smtpmode'] = 'sendmail';
\$CONFIG['mail_smtpauth'] = true; # if smtpmode is smtp
\$CONFIG['mail_smtphost'] = '127.0.0.1'; # if smtpmode is smtp
\$CONFIG['mail_smtpport'] = '587'; # if smtpmode is smtp
\$CONFIG['mail_smtpsecure'] = ''; # if smtpmode is smtp, must be empty string
\$CONFIG['mail_smtpname'] = ''; # if smtpmode is smtp, set this to a mail user
\$CONFIG['mail_smtppassword'] = ''; # if smtpmode is smtp, set this to the user's password

echo "<?php\n\\\$CONFIG = ";
var_export(\$CONFIG);
echo ";";
?>
EOF
chown www-data:www-data "$STORAGE_ROOT/owncloud/config.php"

# Enable/disable apps. Note that this must be done after the Nextcloud setup.
# The firstrunwizard gave Josh all sorts of problems, so disabling that.
# user_external is what allows Nextcloud to use IMAP for login. The contacts
# and calendar apps are the extensions we really care about here.
hide_output sudo -u www-data php"$PHP_VER" $CLOUD_DIR/console.php app:disable firstrunwizard
hide_output sudo -u www-data php"$PHP_VER" $CLOUD_DIR/console.php app:enable user_external
hide_output sudo -u www-data php"$PHP_VER" $CLOUD_DIR/console.php app:enable contacts
hide_output sudo -u www-data php"$PHP_VER" $CLOUD_DIR/console.php app:enable calendar

# When upgrading, run the upgrade script again now that apps are enabled. It seems like
# the first upgrade at the top won't work because apps may be disabled during upgrade?
# Check for success (0=ok, 3=no upgrade needed).
sudo -u www-data php"$PHP_VER" $CLOUD_DIR/occ upgrade
E=$?
if [ $E -ne 0 ] && [ $E -ne 3 ]; then exit 1; fi

# Disable default apps that we don't support
sudo -u www-data \
	php"$PHP_VER" $CLOUD_DIR/occ app:disable photos dashboard activity \
	weather_status logreader \
	| (grep -v "No such app enabled" || /bin/true)

# Install interesting apps
(sudo -u www-data php $CLOUD_DIR/occ app:install notes) || true

hide_output sudo -u www-data php $CLOUD_DIR/console.php app:enable notes

(sudo -u www-data php $CLOUD_DIR/occ app:install twofactor_totp) || true

hide_output sudo -u www-data php $CLOUD_DIR/console.php app:enable twofactor_totp

# upgrade apps
sudo -u www-data php $CLOUD_DIR/occ app:update --all

# Set PHP FPM values to support large file uploads
# (semicolon is the comment character in this file, hashes produce deprecation warnings)
management/editconf.py /etc/php/"$PHP_VER"/fpm/php.ini -c ';' \
	upload_max_filesize=16G \
	post_max_size=16G \
	output_buffering=16384 \
	memory_limit=512M \
	max_execution_time=600 \
	short_open_tag=On

# Set Nextcloud recommended opcache settings
management/editconf.py /etc/php/"$PHP_VER"/cli/conf.d/10-opcache.ini -c ';' \
	opcache.enable=1 \
	opcache.enable_cli=1 \
	opcache.interned_strings_buffer=16 \
	opcache.max_accelerated_files=10000 \
	opcache.memory_consumption=128 \
	opcache.save_comments=1 \
	opcache.revalidate_freq=1

# Migrate users_external data from <0.6.0 to version 3.0.0
# (see https://github.com/nextcloud/user_external).
# This version was probably in use in Mail-in-a-Box v0.41 (February 26, 2019) and earlier.
# We moved to v0.6.3 in 193763f8. Ignore errors - maybe there are duplicated users with the
# correct backend already.
sqlite3 "$STORAGE_ROOT/owncloud/owncloud.db" "UPDATE oc_users_external SET backend='127.0.0.1';" || /bin/true

# Set up a general cron job for Nextcloud.
# Also add another job for Calendar updates, per advice in the Nextcloud docs
# https://docs.nextcloud.com/server/24/admin_manual/groupware/calendar.html#background-jobs
cat > /etc/cron.d/mailinabox-nextcloud << EOF;
#!/bin/bash
# Mail-in-a-Box
*/5 * * * *	root	sudo -u www-data php$PHP_VER -f $CLOUD_DIR/cron.php
*/5 * * * *	root	sudo -u www-data php$PHP_VER -f $CLOUD_DIR/occ dav:send-event-reminders
EOF
chmod +x /etc/cron.d/mailinabox-nextcloud

# We also need to change the sending mode from background-job to occ.
# Or else the reminders will just be sent as soon as possible when the background jobs run.
hide_output sudo -u www-data php"$PHP_VER" -f $CLOUD_DIR/occ config:app:set dav sendEventRemindersMode --value occ

# Run the maintenance command
hide_output sudo -u www-data php"$PHP_VER" $CLOUD_DIR/occ maintenance:repair --include-expensive

# Now set the config to read-only.
# Do this only at the very bottom when no further occ commands are needed.
sed -i'' "s/'config_is_read_only'\s*=>\s*false/'config_is_read_only' => true/" "$STORAGE_ROOT/owncloud/config.php"

# Create nextcloud log in /var/log
hide_output install -m 644 conf/rsyslog/20-nextcloud.conf /etc/rsyslog.d/

# There's nothing much of interest that a user could do as an admin for Nextcloud,
# and there's a lot they could mess up, so we don't make any users admins of Nextcloud.
# But if we wanted to, we would do this:
# ```
# for user in $(management/cli.py user admins); do
#	 sqlite3 $STORAGE_ROOT/owncloud/owncloud.db "INSERT OR IGNORE INTO oc_group_user VALUES ('admin', '$user')"
# done
# ```

# Enable PHP modules and restart PHP.
restart_service php"$PHP_VER"-fpm
