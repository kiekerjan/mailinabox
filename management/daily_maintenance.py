#!/usr/local/lib/mailinabox/env/bin/python

# Run daily maintenance tasks
########################################################################

from editconf import do_editconf
import logging, utils


def do_daily_maintenance(env):
	# This function collects all other maintenance functions

	logging.debug("Do daily maintenance")

	do_webmail_maintenance(env)


def do_webmail_maintenance(env):
	logging.debug("Do webmail maintenance")
	config = utils.load_settings(env)

	# Handle custom front logo of the Roundcube webmail
	skin_logo = config.get("webmail", {}).get("skin_logo", None)
	webmail_file = "/usr/local/lib/roundcubemail/config/config.inc.php"
	remove = True

	if skin_logo:
		if isinstance(skin_logo, str):
			do_editconf([webmail_file, f"$config['skin_logo']='{skin_logo}';"])
			remove = False
		else:
			logging.debug("webmail.skin_logo configured but not a string")

	if remove:
		do_editconf([webmail_file, "-e", "$config['skin_logo']="])


if __name__ == "__main__":
	env = utils.load_environment()

	do_daily_maintenance(env)
