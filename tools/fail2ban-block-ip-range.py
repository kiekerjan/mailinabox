#!/usr/bin/python3

from ipaddress import IPv4Network
from collections import defaultdict
from tempfile import mkstemp
from datetime import datetime

import os
import subprocess
import math

# define the scan function
def scanTheLog(script, countLimit, tmpf):

    subprocess.call(script, shell=True)

    #
    # PART 2: reads the ip list detected and iterate:
    #
    file1 = open(tmpf[1], 'r')
    Lines = file1.readlines()

    mylist = defaultdict(lambda: defaultdict(int))
    finalList = defaultdict(lambda: defaultdict(int))

    # 2.1) iterate ips and count
    for line in Lines:
        splitVect = line.strip().split(" ")
        count = splitVect[0].strip()
        jail = splitVect[1].strip()
        ip = splitVect[2].strip()

        # 2.2) iterate from cidr/32 down to 23 (descending)
        for cidr in range(32, 23, -1):
            ipnet  = IPv4Network(ip + "/" + str(cidr), False)
            index = str(ipnet.network_address) + "/" + str(cidr)

            # 2.3) add the network, jail and count of events into a dictionary
            mylist[jail][index] += int(count)

    #pprint(mylist)

    #
    # PART 3:  iterate IPs again, and get best choice network range
    #
    for line in Lines:
        splitVect = line.strip().split(" ")
        count = splitVect[0].strip()
        jail = splitVect[1].strip()
        ip = splitVect[2].strip()
        maxCount = 0
        nextIndex = False

            # 3.2 iterate CIDR (now in ascending order)
        for cidr in range(22, 33):
            ipnet  = IPv4Network(ip + "/" + str(cidr), False)
            index = str(ipnet.network_address) + "/" + str(cidr)
            curCount = mylist[jail][index]
            if(curCount >= maxCount):
                maxCount = curCount
                netIndex = index

            # 3.3 if count decreases, than we've already got our best range
            if(curCount < maxCount):
                # found good network
                continue

        # 3.4 if netIndex is set and maxCount is above countLimit (set above), add range to list
        if(netIndex and maxCount > countLimit):
          finalList[jail][netIndex] = maxCount
    
    return finalList
    
#
# crontab suggestion:
#
# */5 * * * * /usr/bin/fail2ban-block-ip-range.py
#

# PART 1: system script call, filtering messages and IPs
#
# 1.1) this script searches for fail2ban.log for detections (1000 last lines)
# 1.2) then it egreps the IPs, sort and uniq and count, and sort again.
# 1.3) the IP result list is output to /tmp

# Create temporary file, close it and let shell command write into it
tmpf = mkstemp()
os.close(tmpf[0])

# Script collects all found ips
# Script collects all banned ips

# fail2ban.log is rotated every week, so limit to last 2500 lines
script = r'tail -n 2500 /var/log/fail2ban.log | grep -E "fail2ban.filter.*\[[0-9]+\]:.*\[[^]]+\] Found ([0-9]{1,3}\.){3}[0-9]{1,3}" -o | sed -re "s/fail2ban.filter\s+\[[0-9]+\]:\sINFO\s+\[//; s/\]//; s/Found //;" | sort | uniq -c > ' + tmpf[1]
# script = 'cat /var/log/fail2ban.log | grep -E "fail2ban.filter.*\[[0-9]+\]:.*\[[^]]+\] Found ([0-9]{1,3}\.){3}[0-9]{1,3}" -o | sed -re "s/fail2ban.filter\s+\[[0-9]+\]:\sINFO\s+\[//; s/\]//; s/Found //;" | sort | uniq -c > ' + tmpf[1]
countLimit = 8  # If we find 100 ips in a jail filter, ban the subnet

finalList = scanTheLog(script, countLimit, tmpf)

#
# PART 4: call fail2ban  (you can also call IPTABLES directly)
#
fail2ban_command = "fail2ban-client set {} banip {}"

#pprint(finalList) # show the final list

# Prepare timestamp logging
logstamp = 'logging ' + datetime.now().strftime("%Y %m %d - %H:%M\n")
timestamplogged = False
f = open('/var/log/subnetblocker.log', 'a', encoding="utf-8")

for jail in finalList:
    for ip in finalList[jail]:
        subnetSize = int(ip[-2:])
        foundCount = finalList[jail][ip]

        if not timestamplogged:
            f.write(logstamp)
            timestamplogged = True
                    
        # Only log interesting results
        if subnetSize < 32 and foundCount > 10:
            f.write('Found ip ' + ip + ' , count: ' + str(foundCount) + '\n')
            
        # Ignore small subnets
        if subnetSize > 29 or foundCount < math.pow(2, 30-subnetSize):
            f.write('Ignoring small Found subnet ' + str(subnetSize) + ' ' + str(foundCount) + '\n')
            continue
        
        # Log the command
        banIP_command = fail2ban_command.format(jail, ip)
        f.write(banIP_command + '\n')

        try:
            subprocess.call(banIP_command, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        except Exception as e:
            f.write(repr(e) + '\n')

        try:
            # Lookup the country and log it
            geoResult = subprocess.run(['goiplookup', ip[:-3], '-c'], stdout=subprocess.PIPE)
            f.write('Geo ip lookup: ' + geoResult.stdout.decode('utf-8'))
        except Exception as e:
            f.write(repr(e) + '\n')


# Look for ban actions and use them to ban a subnet in the recidive jail
#script =  'cat /var/log/fail2ban.log | grep -E "fail2ban.actions.*\[[0-9]+\]:.*\[[^]]+\] Ban ([0-9]{1,3}\.){3}[0-9]{1,3}" -o | sed -re "s/fail2ban.actions\s+\[[0-9]+\]:\sNOTICE\s+\[//; s/\]//; s/Ban //;" | sort | uniq -c > ' + tmpf[1]
script =  r'tail -n 5000 /var/log/fail2ban.log | grep -E "fail2ban.actions.*\[[0-9]+\]:.*\[[^]]+\] Ban ([0-9]{1,3}\.){3}[0-9]{1,3}" -o | sed -re "s/fail2ban.actions\s+\[[0-9]+\]:\sNOTICE\s+\[//; s/\]//; s/Ban //;" | sort | uniq -c > ' + tmpf[1]
countLimit = 4   # If we banned x ips from a subnet, ban the subnet

finalList = scanTheLog(script, countLimit, tmpf)

fail2ban_command = "fail2ban-client set recidive banip {}"

ipList = set()

# Loop through the found ips
for jail in finalList:
    for ip in finalList[jail]:
        subnetSize = int(ip[-2:])
        foundCount = finalList[jail][ip]
        
        if not timestamplogged:
            f.write(logstamp)
            timestamplogged = True

        # Only log interesting results
        if subnetSize < 32 and foundCount > 4:
            f.write('Banned ip ' + ip + ' , count: ' + str(foundCount) + '\n')
            
        # Ignore small subnets
        if subnetSize > 29 or foundCount < math.pow(2, 29-subnetSize):
            f.write('Ignoring small Banned subnet ' + str(subnetSize) + ' ' + str(foundCount) + '\n')
            continue

        ipList.add(ip)

# Only needed if ipList is not empty
if len(ipList) > 0:
    for ip in ipList:
        # Get the ip
        banIP_command = fail2ban_command.format(ip)
        
        # Block the ip
        try:
            subprocess.call(banIP_command, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        except Exception as e:
            f.write(repr(e) + '\n')
        
        # Log the command
        f.write(banIP_command + '\n')
        
        try:    
            # Lookup the country and log it
            geoResult = subprocess.run(['goiplookup', ip[:-3], '-c'], stdout=subprocess.PIPE)
            f.write('Geo ip lookup: ' + geoResult.stdout.decode('utf-8'))
        except Exception as e:
            f.write(repr(e) + '\n')

# Close the logfile
f.close()

# delete temporary file
os.remove(tmpf[1])

# Done

