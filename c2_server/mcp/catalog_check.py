"""Catalog sanity check for c2_project_mcp parsers (no MCP transport)."""
import sys
sys.path.insert(0, 'mcp')
import c2_project_mcp as m

s = m.parse_server_modules()
a = m.parse_agent_modules()

assert len(s) == 4, f"expected 4 server modules, got {len(s)}"
assert len(a) == 7, f"expected 7 agent namespaces, got {len(a)}"

ssh = next(x for x in s if x['class'] == 'SSHModule')
assert 'connect' in ssh['actions'] and 'exec' in ssh['actions'], ssh['actions'].keys()
cp = ssh['actions']['connect']['params']
assert 'host' in cp and 'username' in cp and 'key_path' in cp, cp

act = next(x for x in s if x['class'] == 'ActivityModule')
assert set(['processes', 'system_stats', 'start_monitor']) <= set(act['actions']), act['actions'].keys()

reg = next(x for x in s if x['class'] == 'RegistryModule')
assert 'write_value' in reg['actions'] and reg.get('availability','').startswith('Windows'), reg.get('availability')

sh = next(x for x in a if x['module'] == 'shell')
acts = [t['action'] for t in sh['actions']]
assert 'start' in acts and 'stop' in acts, acts
assert 'lhost' in sh['params_all'] and 'lport' in sh['params_all'], sh['params_all']
assert sh['wired_in_dispatch'] is True

de = next(x for x in a if x['module'] == 'defender')
assert [t['action'] for t in de['actions']] == ['status', 'disable', 'enable', 'add_exclusion',
                                                 'remove_exclusion', 'list_exclusions'], de['actions']
assert de['wired_in_dispatch'] is False, "defender must be reported as NOT wired into dispatch"

nt = next(x for x in a if x['module'] == 'notify')
assert 'send_popup' in [t['action'] for t in nt['actions']]
assert nt['wired_in_dispatch'] is True

print("server modules:", [x['class'] for x in s])
print("agent modules:", [(x['module'], x['wired_in_dispatch']) for x in a])
print("SSH connect params:", ssh['actions']['connect']['params'])
print("shell params:", sh['params_all'])
print("defender actions:", [t['action'] for t in de['actions']], "wired:", de['wired_in_dispatch'])
print("CATALOG CHECK: ALL PASS")
