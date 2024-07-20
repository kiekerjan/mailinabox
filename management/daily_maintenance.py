#!/usr/local/lib/mailinabox/env/bin/python

# Run daily maintenance tasks
########################################################################

from ..tools.editconf import do_editconf
from utils import load_settings
import logging

def do_daily_maintenance(env):
	# This function collects all other maintenance functions

	logging.debug("Do daily maintenance")

	do_webmail_maintenance(env)


def do_webmail_maintenance(env):
	logging.debug("Do webmail maintenance")
	config = load_settings(env)

	skin_logo = config.get("webmail", {}).get("skin_logo", None)

	if skin_logo:
		if isinstance(skin_logo, str):
			do_editconf("/usr/local/lib/roundcubemail/config/config.inc.php",
			f"$config['skin_logo']='{skin_logo}';")
		else:
			logging.debug("webmail.skin_logo configured but not a string")

if __name__ == "__main__":
	from utils import load_environment
	env = load_environment()

	do_daily_maintenance(env)
