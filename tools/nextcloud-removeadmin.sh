#!/bin/bash
#
# This script will remove administrative access to the Nextcloud
# instance running here.
#
# Run this at your own risk. This is for testing & experimentation
# purposes only. After this point you are on your own.

source /etc/mailinabox.conf # load global vars

ADMIN=$(./management/cli.py user admins | head -n 1)
test -z "$1" || ADMIN=$1

echo "I am going to remove admin features for $ADMIN."
echo "You can provide another user to lock as the first argument of this script."
echo
echo "If in doubt, press CTRL-C to cancel."
echo
echo "Press enter to continue."
read

sudo -u nextcloud_php php /usr/local/lib/nextcloud/cloud/occ group:removeuser admin "$ADMIN" && echo "Done."
