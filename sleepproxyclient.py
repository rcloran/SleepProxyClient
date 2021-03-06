#
# This file is part of SleepProxyClient.
#
# @license   http://www.gnu.org/licenses/gpl.html GPL Version 3
# @author    Andreas Weinlein <andreas.dev@weinlein.info>
# @copyright Copyright (c) 2012 Andreas Weinlein
#
# SleepProxyClient is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# SleepProxyClient is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with SleepProxyClient. If not, see <http://www.gnu.org/licenses/>.

import dns.update
import dns.query
import dns.rdtypes
import dns.rdata
import dns.edns
import dns.rrset
from dns.exception import DNSException

from IPy import IP # used to get reverseName from IP
import netifaces # network interface handling
import argparse
import struct
import subprocess
import socket

#
#	A Python Wake On Demand client implementation (SleepProxyClient)
#


# TTL (lease time) for sleep client.
# A Wake-On-Lan-Packet will be sent after this period.
# can be overwritten by specifying the corresponding argument!
TTL = 7200 # 2 h

# TTL for ddns update request ressource records
# see http://tools.ietf.org/html/draft-cheshire-dnsext-multicastdns-08#section-11
# should NOT be changed
TTL_long = 4500 # 75 min
# TTL is used for some other records
# should NOT be changed
TTL_short = 120 # 2 min

# debug flag
DEBUG = False



def main() :

	args = readArgs()

	# check interfaces
	sysIfaces = netifaces.interfaces()

	interfaces = args.interfaces
	if args.interfaces == "all" :
		interfaces = sysIfaces

	if (DEBUG) :
		print "Interfaces: ", ", ".join(interfaces)


	for iface in interfaces :
		if iface not in sysIfaces :
			print "Invalid interface specified: ", iface
		elif "lo" not in iface:
			sendUpdateForInterface(iface)



def sendUpdateForInterface(interface) :
# send update request per interface
	if (DEBUG) :
		print "-sendUpdateForInterface: ", interface


	# get IPs for given interface
	ifaddrs = netifaces.ifaddresses(interface)

	ipArr = []
	if (netifaces.AF_INET in ifaddrs) :
		for ipEntry in ifaddrs[netifaces.AF_INET] :
			ipArr.append(ipEntry['addr'])

	if (netifaces.AF_INET6 in ifaddrs) :
		for ipEntry in ifaddrs[netifaces.AF_INET6] :
			ipArr.append(ipEntry['addr'].split('%')[0]) # fix trailing %<iface>

	if (len(ipArr) == 0) :
		print "No IPv4 or IPv6 Addresses found for interface: ", interface
		return

	if (DEBUG) :
		print "-sendUpdateForInterface: IPs: ", ", ".join(ipArr)


	# get HW Addr
	if ":" in interface : # handle virtual interfaces
		hwAddr = netifaces.ifaddresses(interface.rsplit(':')[0])[netifaces.AF_LINK][0]['addr']
	else:
		hwAddr = ifaddrs[netifaces.AF_LINK][0]['addr']

	if (DEBUG) :
		print "-sendUpdateForInterface: HW-Addr: ", hwAddr


	# get all available sleep proxies
	proxy = discoverSleepProxyForInterface(interface)
	if (proxy == False) :
		print "No sleep proxy available for interface: ", interface
		return


	# get hostname
	host = socket.gethostname()
	host_local = host + ".local"

	if (DEBUG) :
		print "-sendUpdateForInterface: Host: " + host_local


	# create update request
	update = dns.update.Update("")

	## add some host stuff
	for currIP in ipArr :
		
		ipAddr = IP(currIP)
		ipVersion = ipAddr.version()
		
		if ipVersion == 4 :
			dnsDatatype = dns.rdatatype.A
		elif ipVersion == 6 :
			dnsDatatype = dns.rdatatype.AAAA
		else :
			continue			
		
		update.add(ipAddr.reverseName(), TTL_short, dns.rdatatype.PTR, host_local)
		update.add(host_local, TTL_short, dnsDatatype,  currIP)


	## add services
	for service in discoverServices(ipArr) :

		service_type = service[0] + ".local"
		service_type_host = host + "." + service_type
		port = service[1]

		# add the service
	 	txtrecord = ""
		if (len(service) == 2 or service[2] == "") :
			txtrecord = chr(0)
		else :
			for i in range(2,len(service)) :
					txtrecord += " " + service[i]

		if (DEBUG) :
			txtrecord += " SPC_STATE=sleeping"


		update.add(service_type_host, TTL_long, dns.rdatatype.TXT, txtrecord)

		# device-info service gets a txt record only
		if (service_type != "device-info._tcp.local") :
			update.add('_services._dns-sd._udp.local', TTL_long, dns.rdatatype.PTR, service_type)
			update.add(service_type, TTL_long, dns.rdatatype.PTR, service_type_host)
			update.add(service_type_host, TTL_short, dns.rdatatype.SRV, "0 0 " + port + " " + host_local)


	## add edns options

	# http://files.dns-sd.org/draft-sekar-dns-ul.txt
	# 2: Lease Time in seconds
	leaseTimeOption = dns.edns.GenericOption(2, struct.pack("!L", TTL))

	# http://tools.ietf.org/id/draft-cheshire-edns0-owner-option-00.txt
	# 4: edns owner option (MAC addr for WOL Magic packet)
	cleanMAC = hwAddr.replace(":", "")
	ownerOption = dns.edns.GenericOption(4, ("0000" + cleanMAC).decode('hex_codec'))

	update.use_edns(edns=True, ednsflags=TTL_long, options=[leaseTimeOption, ownerOption])

	if (DEBUG) :
		print "-sendUpdateForInterface: request: ", update


	# send request to proxy
	try:
		if (DEBUG) :
			print "-sendUpdateForInterface: sending update to " + proxy['ip']

		response = dns.query.udp(update, proxy['ip'], timeout=10, port=int(proxy['port']))

		if (DEBUG) :
			print "-sendUpdateForInterface: response: ", response

		rcode = response.rcode()
		if rcode != dns.rcode.NOERROR:
			print "Unable to register with SleepProxy " + proxy['name'] + " (" + proxy['ip'] + ":" + proxy['port'] + ") - Errcode: " + rcode
			print response

	except DNSException, e:
		print "Unable to register with SleepProxy " + proxy['name'] + " (" + proxy['ip'] + ":" + proxy['port'] + ")"
		print e.__class__, e



def discoverServices(ipArray) :
# discover all currently announced services from given IPs
	if (DEBUG) :
		print "-discoverServices: IPs: " + ", ".join(ipArray)

	services = []
	cmd = "avahi-browse --all --resolve --parsable --no-db-lookup --terminate 2>/dev/null | grep '^=;'"

	p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
	for line in p.stdout.readlines() :
		lineArr = line.rsplit(";")

		# check length
		if (len(lineArr) < 10) :
			p.terminate()
			print "discovering services failed for: " + str(ipArray)
			break

		else :
			#extract service details
			if (lineArr[7] in ipArray) :
				service = lineArr[4]
				port = lineArr[8]
				txtRecords = lineArr[9].replace('" "', ';').replace('\n', '').replace('"', '').rsplit(';')
				serviceEntry = [service, port] + txtRecords
				if (serviceEntry not in services) : # check for duplicates due to IPv4/6 dual stack
					services.append(serviceEntry)

	# wait for cmd to terminate
	p.wait()

	if (DEBUG) :
		print "-discoverServices: discovered Services: ", services

	return services



def discoverSleepProxyForInterface(interface) :

	if (DEBUG) :
		print "-discoverSleepProxyForInterface: Interface: ", interface

	cmd = "avahi-browse --resolve --parsable --no-db-lookup --terminate _sleep-proxy._udp 2>/dev/null | grep '^=;" + interface.rsplit(":")[0] + "'"

	# the best proxy found
	proxy = False
	# the best proxy properties found
	minProperties = ""

	# get all sleep proxies for the given interface an check for duplicates (IPv4/IPv6)
	p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
	for line in p.stdout.readlines() :
		lineArr = line.rsplit(";")

		# check length
		if (len(lineArr) < 10) :
			p.terminate()
			break


		currProxy = { "name" : lineArr[6], "ip" : lineArr[7], "port" : lineArr[8] }
		properties = lineArr[3].rsplit(" ")[0]

		if (DEBUG) :
			print "-discoverSleepProxyForInterface: available proxy: ", currProxy, " with properties: ", properties

		# choose the server with lowest properties and prefer none 169.254.X.X addresses
		if (minProperties == "" or minProperties > properties or (proxy and proxy['ip'].startswith('169.254.'))):
			minProperties = properties
			proxy = currProxy

	# wait for cmd to terminate
	p.wait()

	if (DEBUG) :
		print "-discoverSleepProxyForInterface: selected proxy: ", proxy, " with properties: ", minProperties

	return proxy



def readArgs() :
# parse arguments
	global TTL
	global DEBUG

	parser = argparse.ArgumentParser(description='SleepProxyClient')
	parser.add_argument('--interfaces', nargs='+', action='store', help="A list of network interfaces to use, seperated by ','", default="all")
	parser.add_argument('--ttl', action='store', type=int, help='TTL for the update in seconds. Client will be woken up after this period.', default=TTL_long)
	parser.add_argument('--debug', action='store_true', help='Debug switch for verbose output.', default=False)

	result = parser.parse_args()

	# update some global vars
	TTL = result.ttl
	DEBUG = result.debug

	return result

if __name__ == '__main__':
	# call main
	main()
