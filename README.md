Modifications are go
====================

This is not the original Mail-in-a-Box. See https://github.com/mail-in-a-box/mailinabox for the real deal! Many thanks to [@JoshData](https://github.com/JoshData) and other  [contributors](https://github.com/mail-in-a-box/mailinabox/graphs/contributors).
I made a number of modifications to the original Mail-in-a-Box, some to fix bugs, some to ease maintenance for my personal installation, to learn and to be used as a statging aread for adding functionality.

Functionality changes and additions
* Add geoipblocking on the admin web console  
  This applies geoip filtering on acces to the admin panel of the box. Order of filtering: block continents that are not allowed, block countries that are not allowed, allow countries that are allowed (overriding continent filtering). Edit /etc/nginx/conf.d/10-geoblock.conf to configure.
* Add geoipblocking for ssh access  
  This applies geoip filtering for access to the ssh server. Edit /etc/geoiplookup.conf. All countries defined in this file are allowed. Works for alternate ssh ports.  
  This uses goiplookup from https://github.com/axllent/goiplookup
* Make fail2ban more strict  
  enable postfix filters, lengthen bantime and findtime
* Add fail2ban jails for both above mentioned geoipblocking filters
* Add fail2ban filters for web scanners and badbots
* Add xapian full text searching to dovecot (from https://github.com/grosjo/fts-xapian)
* Add rkhunter for malware scanning
* Configure domain names for which only www will be hosted  
  Edit settings.yaml under the user-data folder to configure. The box will handle incoming traffic asking for these domain names. The DNS entries are entered in an external DNS provider! If you want this box to handle the DNS entries, simply add a mail alias. (existing functionality of the vanilla Mail-in-a-Box)
* Add some munin plugins
* Update nextcloud to 29.0.8
  And updated calendar and contacts apps
* Add nextcloud notes app
* Update roundcube to 1.6.9
* Add roundcube context menu plugin
* Add roundcube two factor authentication plugin
* Option to use shorter TTL values in the DNS server  
  To be used for example just before changing IP addresses. Shorter TTL values will make DNS records cached for a shorter time. Changes will thus apply faster. For reference, default TTL is 1 day, short TTL is 5 minutes.
* Option to use the box as a Hidden Master in the DNS system  
  Thus only the secondary DNS servers are used as public DNS servers. When using a hidden master, no glue records are necessary at your domain hoster. To use, first setup secondary DNS servers via the Custom DNS administration page. At least two secondary servers should be set. Then enable the Hidden Master option.
* Both hidden master DNS and shorter TTL are configurable through the admin web portal.
* Daily ip blacklist check  
  Using check-dnsbl.py from https://github.com/gsauthof/utility
* Updated ssl security for web and email  
  Removed older cryptos following internet.nl recommendations, this installation has 100% score on the mail test on internet.nl
* Replace opendkim with dkimpy (https://launchpad.net/dkimpy-milter)
  Added support for Ed25519 signing
* Replace bind9 with unbound DNS resolver https://www.nlnetlabs.nl/projects/unbound/about/
* Make backup target folder configurable
  set BACKUP_ROOT to the backup target folder (default is same as STORAGE_ROOT)

Bug fixes
* Munin error report fixed [see github issue](https://github.com/mail-in-a-box/mailinabox/issues/1555)
* Correct nextcloud carddav url [see github issue](https://github.com/mail-in-a-box/mailinabox/issues/1918) (update: included in upstream) 
* Take into account non-standard spamhaus return codes (update: included in upstream)

Maintenance (personal)
* Automatically clean spam and trash folders after 120 days
* Removed Z-Push
* After a backup, restarting of services is moved to before the execution of the after-backup script. This enables mail delivery while the after-backup script runs.
* Add weekly pflogsumm log analysis
* Enable mail delivery to root, forwarded to administrator
* Remove nextcloud skeleton to save disk space
* Use actual configuration as reported by sshd instead of parsing the config file, for more robust configuration extraction. (update: included in upstream)

Fun
* Add option to define ADMIN_IP_ADDRESS  
  Currently only used to ignore fail2ban jails
* Add dynamic dns tools in the tools directory  
  Can be used to control DNS entries on the mail-in-a-box to point to a machine with a non-fixed (e.g. residential) ip address

Original mailinabox content starts here:

Mail-in-a-Box
=============

By [@JoshData](https://github.com/JoshData) and [contributors](https://github.com/mail-in-a-box/mailinabox/graphs/contributors).

Mail-in-a-Box helps individuals take back control of their email by defining a one-click, easy-to-deploy SMTP+everything else server: a mail server in a box.

**Please see [https://mailinabox.email](https://mailinabox.email) for the project's website and setup guide!**

* * *

Our goals are to:

* Make deploying a good mail server easy.
* Promote [decentralization](http://redecentralize.org/), innovation, and privacy on the web.
* Have automated, auditable, and [idempotent](https://web.archive.org/web/20190518072631/https://sharknet.us/2014/02/01/automated-configuration-management-challenges-with-idempotency/) configuration.
* **Not** make a totally unhackable, NSA-proof server.
* **Not** make something customizable by power users.

Additionally, this project has a [Code of Conduct](CODE_OF_CONDUCT.md), which supersedes the goals above. Please review it when joining our community.


In The Box
----------

Mail-in-a-Box turns a fresh Ubuntu 22.04 LTS 64-bit machine into a working mail server by installing and configuring various components.

It is a one-click email appliance. There are no user-configurable setup options. It "just works."

The components installed are:

* SMTP ([postfix](http://www.postfix.org/)), IMAP ([Dovecot](http://dovecot.org/)), CardDAV/CalDAV ([Nextcloud](https://nextcloud.com/)), and Exchange ActiveSync ([z-push](http://z-push.org/)) servers
* Webmail ([Roundcube](http://roundcube.net/)), mail filter rules (thanks to Roundcube and Dovecot), and email client autoconfig settings (served by [nginx](http://nginx.org/))
* Spam filtering ([spamassassin](https://spamassassin.apache.org/)) and greylisting ([postgrey](http://postgrey.schweikert.ch/))
* DNS ([nsd4](https://www.nlnetlabs.nl/projects/nsd/)) with [SPF](https://en.wikipedia.org/wiki/Sender_Policy_Framework), DKIM ([OpenDKIM](http://www.opendkim.org/)), [DMARC](https://en.wikipedia.org/wiki/DMARC), [DNSSEC](https://en.wikipedia.org/wiki/DNSSEC), [DANE TLSA](https://en.wikipedia.org/wiki/DNS-based_Authentication_of_Named_Entities), [MTA-STS](https://tools.ietf.org/html/rfc8461), and [SSHFP](https://tools.ietf.org/html/rfc4255) policy records automatically set
* TLS certificates are automatically provisioned using [Let's Encrypt](https://letsencrypt.org/) for protecting https and all of the other services on the box
* Backups ([duplicity](http://duplicity.nongnu.org/)), firewall ([ufw](https://launchpad.net/ufw)), intrusion protection ([fail2ban](http://www.fail2ban.org/wiki/index.php/Main_Page)), and basic system monitoring ([munin](http://munin-monitoring.org/))

It also includes system management tools:

* Comprehensive health monitoring that checks each day that services are running, ports are open, TLS certificates are valid, and DNS records are correct
* A control panel for adding/removing mail users, aliases, custom DNS records, configuring backups, etc.
* An API for all of the actions on the control panel

Internationalized domain names are supported and configured easily (but SMTPUTF8 is not supported, unfortunately).

It also supports static website hosting since the box is serving HTTPS anyway. (To serve a website for your domains elsewhere, just add a custom DNS "A" record in you Mail-in-a-Box's control panel to point domains to another server.)

For more information on how Mail-in-a-Box handles your privacy, see the [security details page](security.md).


Installation
------------

See the [setup guide](https://mailinabox.email/guide.html) for detailed, user-friendly instructions.

For experts, start with a completely fresh (really, I mean it) Ubuntu 22.04 LTS 64-bit machine. On the machine...

Clone this repository and checkout the tag corresponding to the most recent release (which you can find in the tags or releases lists on GitHub):

	$ git clone https://github.com/mail-in-a-box/mailinabox
	$ cd mailinabox
	$ git checkout TAGNAME

Begin the installation.

	$ sudo setup/start.sh

The installation will install, uninstall, and configure packages to turn the machine into a working, good mail server.

For help, DO NOT contact Josh directly --- I don't do tech support by email or tweet (no exceptions).

Post your question on the [discussion forum](https://discourse.mailinabox.email/) instead, where maintainers and Mail-in-a-Box users may be able to help you.

Note that while we want everything to "just work," we can't control the rest of the Internet. Other mail services might block or spam-filter email sent from your Mail-in-a-Box.
This is a challenge faced by everyone who runs their own mail server, with or without Mail-in-a-Box. See our discussion forum for tips about that.


Contributing and Development
----------------------------

Mail-in-a-Box is an open source project. Your contributions and pull requests are welcome. See [CONTRIBUTING](CONTRIBUTING.md) to get started. 


The Acknowledgements
--------------------

This project was inspired in part by the ["NSA-proof your email in 2 hours"](http://sealedabstract.com/code/nsa-proof-your-e-mail-in-2-hours/) blog post by Drew Crawford, [Sovereign](https://github.com/sovereign/sovereign) by Alex Payne, and conversations with <a href="https://twitter.com/shevski" target="_blank">@shevski</a>, <a href="https://github.com/konklone" target="_blank">@konklone</a>, and <a href="https://github.com/gregelin" target="_blank">@GregElin</a>.

Mail-in-a-Box is similar to [iRedMail](http://www.iredmail.org/) and [Modoboa](https://github.com/tonioo/modoboa).


The History
-----------

* In 2007 I wrote a relatively popular Mozilla Thunderbird extension that added client-side SPF and DKIM checks to mail to warn users about possible phishing: [add-on page](https://addons.mozilla.org/en-us/thunderbird/addon/sender-verification-anti-phish/), [source](https://github.com/JoshData/thunderbird-spf).
* In August 2013 I began Mail-in-a-Box by combining my own mail server configuration with the setup in ["NSA-proof your email in 2 hours"](http://sealedabstract.com/code/nsa-proof-your-e-mail-in-2-hours/) and making the setup steps reproducible with bash scripts.
* Mail-in-a-Box was a semifinalist in the 2014 [Knight News Challenge](https://www.newschallenge.org/challenge/2014/submissions/mail-in-a-box), but it was not selected as a winner.
* Mail-in-a-Box hit the front page of Hacker News in [April](https://news.ycombinator.com/item?id=7634514) 2014, [September](https://news.ycombinator.com/item?id=8276171) 2014, [May](https://news.ycombinator.com/item?id=9624267) 2015, and [November](https://news.ycombinator.com/item?id=13050500) 2016.
* FastCompany mentioned Mail-in-a-Box a [roundup of privacy projects](http://www.fastcompany.com/3047645/your-own-private-cloud) on June 26, 2015.
