import idna, logging
from utils import load_settings

def get_dns_domains(domains_to_skip, env):
    # Returns the domain names of all the fomains that are configured to serve dns for on the system
    domains = []
    
    config = load_settings(env)
    dns_entries = config.get("hostother", {}).get("dns", {})

    try:    
        if isinstance(dns_entries, list) or isinstance(dns_entries, dict):
            for val in dns_entries:
                dns_domain = get_domain(val, as_unicode=False)
                if dns_domain not in domains_to_skip:
                     domains.append(dns_domain)
    except:
        logging.debug("Error reading hosted dns from settings")
        
    return set(domains)


def get_www_domains(domains_to_skip, env):
    # Returns the domain names (IDNA-encoded) of all of the domains that are configured to serve www
    # on the system. 
    domains = []
    
    config = load_settings(env)
    www_entries = config.get("hostother", {}).get("www", {})

    try:    
        if isinstance(www_entries, list) or isinstance(www_entries, dict):
            for val in www_entries:
                www_domain = get_domain(val, as_unicode=False)
                if www_domain not in domains_to_skip:
                     domains.append(www_domain)
    except:
        logging.debug("Error reading hosted www from settings")
        
    return set(domains)


def get_domain(domaintxt, as_unicode=True):
    ret = domaintxt.rstrip()
    if as_unicode:
        try:
            ret = idna.decode(ret.encode('ascii'))
        except (ValueError, UnicodeError, idna.IDNAError):
            pass

    return ret

