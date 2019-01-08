#!/usr/bin/env python
'''
Octo-DNS Reporter / exit non-zero on failure
'''

from __future__ import absolute_import, division, print_function, \
    unicode_literals

from concurrent.futures import ThreadPoolExecutor
from logging import getLogger
from sys import stdout
import sys
import re
from dns.exception import Timeout
from dns.resolver import NXDOMAIN, NoAnswer, NoNameservers, Resolver, query

from octodns.cmds.args import ArgumentParser
from octodns.manager import Manager
from octodns.zone import Zone

log = getLogger('check-zone')

class AsyncResolver(Resolver):
    """
    Create an DNS resolver using an async pool of workers
    """

    def __init__(self, num_workers, *args, **kwargs):
        super(AsyncResolver, self).__init__(*args, **kwargs)
        self.executor = ThreadPoolExecutor(max_workers=num_workers)

    def query(self, *args, **kwargs):
        return self.executor.submit(super(AsyncResolver, self).query, *args,
                                    **kwargs)

def configure_resolvers(dns_servers):
    """
    For each dns_server, configure an an async resolver
    Return a list of a AsyncResolvers
    """
    resolvers = []
    ip_addr_re = re.compile(r'^[\d\.]+$')
    for server in dns_servers:
        resolver = AsyncResolver(configure=False,
                                 num_workers=4)
        # We need to resolve this if it's not an IP
        if not ip_addr_re.match(server):
            server = unicode(query(server, 'A')[0])
        log.info('server=%s', server)
        resolver.nameservers = [server]
        resolver.lifetime = 8
        resolvers.append(resolver)
    return resolvers

def quote_cleanup(values):
    """
    Some cleanup for unicode u"'dns.domain.'" / u'"dns.domain."'
    """
    return [unicode(r).replace("'", "").replace('"', '') for r in values]


def record_value_list(record):
    """
    Get a list of configured record values
    (even for ones that only have one value)
    """
    if hasattr(record,'values'):
        return record.values
    else:
        return [record.value]

def record_response_values(record, response):
    """
    Verify a response, returning cleaned up values or None
    """
    try:
        return quote_cleanup(response.result())
    except NXDOMAIN:
        log.error('*** NXDOMAIN for: %s', record.fqdn)
        return None
    except Timeout:
        log.error('*** Timeout for: %s', record.fqdn)
        return None
    except (NoAnswer, NoNameservers):
        # FIXME: unsure why NS records come back: *** NoAnswer:
        # The DNS response does not contain an answer to the question: X. IN NS
        # However all other records we can compare
        # import ipdb ; ipdb.set_trace()
        if record._type == 'NS':
            log.info('*** NS Record with NoAnswer for: %s', record.fqdn)
            return []
        log.error('*** NoAnswer / NoNameservers for: %s', record.fqdn)
        return None

def verify_dns(queries):
    """
    Iterate over the queries comparing the responses to the record configuration
    """
    dns_error = False
    for record, responses in sorted(queries.items(), key=lambda d: d[0]):

        # Print out a log each record
        stdout.write(record.fqdn)
        stdout.write(',')
        stdout.write(record._type)
        stdout.write(',')
        stdout.write(unicode(record.ttl))

        # pull out the values we want to configure for this record
        record_values = record_value_list(record)

        # clean them up a bit
        configured_values = quote_cleanup(record_values)

        # ensure valid responses that match configured values
        for response in responses:
            stdout.write(',')

            # the responses need a quick test / cleanup
            response_values=record_response_values(record, response)
            stdout.write(' '.join(response_values))

            # NS Records will need to be handled differently
            if record._type == "NS":
                continue

            # If we didn't get a response, it's an error
            if not response_values:
                dns_error = True
                continue

            # All configured_values should be included in the response
            for configured_value in configured_values:
                if configured_value in response_values:
                    continue
                log.error('*** Configured Value not in response: %s', record.fqdn)
                dns_error = True

            # All reponses should be included in the configured_values
            for response_value in response_values:
                if response_value in configured_values:
                    continue
                log.error('*** Response not in configuration: %s', record.fqdn)
                dns_error = True

        if not dns_error:
            stdout.write(',True\n')
        else:
            stdout.write(',False\n')
    return dns_error

def main():
    """check-zone based on octodns config file and dns zone
    Will query all 4 DNS servers configured for the zone in GCP.
    """
    parser = ArgumentParser(description=__doc__.split('\n')[1])

    parser.add_argument('--config-file', required=True,
                        help='The OctoDNS configuration file to use')
    parser.add_argument('--zone', required=True, help='zone to check')

    args = parser.parse_args()

    manager = Manager(args.config_file)
    zone = Zone(args.zone, manager.configured_sub_zones(args.zone))
    gcp = manager.providers['gcp']
    project = gcp.gcloud_client.project
    gcp.populate(zone)

    # Retrieve the DNS Servers directly from the GCP configuration
    dns_servers = gcp.gcloud_zones[args.zone].name_servers
    print('Using GCP project {}'.format(project))
    print('name,type,ttl,{},consistent'.format(','.join(dns_servers)))

    # Configure Resolvers (one per DNS server)
    resolvers = configure_resolvers(dns_servers)

    # Populate the queries to make based on zone record configuration
    queries = {}
    for record in sorted(zone.records):
        queries[record] = [r.query(record.fqdn, record._type)
                           for r in resolvers]
    # No dns_error unless we find one
    dns_error = False

    dns_error = verify_dns(queries)

    if dns_error:
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == '__main__':
    main()
