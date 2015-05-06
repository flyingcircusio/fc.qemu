#!/vagrant/bin/python
import consulate
import sys
import string

vm_name = sys.argv[1]
vm_host = sys.argv[2]
vm_id = int(filter(lambda x: x in string.digits, vm_name), 10)
online = bool(int(sys.argv[3]))

vm = {
    'classes': ['role::appserver', 'role::backupclient', 'role::generic',
                'role::postgresql90', 'role::pspdf', 'role::webproxy'],
    'name': vm_name,
    'parameters': {
        'profile': 'generic',
        'environment': 'staging',
        'directory_ring': 1,
  'cores': 1,
  'resource_group': 'test',
  'reverses': {},
  'interfaces': {
    'srv': {
      'mac': '02:00:00:03:11:63',
      'networks': {
        '2a02:238:f030:103::/64':
            ['2a02:238:f030:103::1d'],
        '172.16.48.0/20': [],
        '212.122.41.160/27':
            ['212.122.41.186']}},
    'fe': {
      'mac': '02:00:00:02:11:63',
      'networks': {
        '212.122.41.128/27': [],
        '2a02:238:f030:102::/64':
            ['2a02:238:f030:102::20']}}},
  'online': True,
  'memory': 512,
  'kvm_host': vm_host,
  'directory_password': '1jmGY3yjpFlLqg63RHie',
  'machine': 'virtual',
  'production': False,
  'servicing': True,
  'service_description': 'asdf',
  'timezone': 'Europe/Berlin',
  'resource_group_parent': '',
  'disk': 15,
  'id': vm_id,
  'location': 'whq'}}


session = consulate.Consul()
session.kv['vm/test/{}'.format(vm['name'])] = vm
