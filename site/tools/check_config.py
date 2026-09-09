"""Check browser-generated TOML with this repository's parser; temporary data only."""
import sys,json,tempfile,hashlib
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import replace
from scratch import scratch_directory
ROOT=Path(__file__).resolve().parents[1];SOURCE=ROOT.parent;sys.path.insert(0,str(SOURCE));sys.dont_write_bytecode=True
WORK=scratch_directory()
from PasteBerth.runtime.config import load_config
from PasteBerth.runtime.zone_collection import discover_zone_collections
prefix='''listen_address = "127.0.0.1"\nport = 8877\nallowed_hosts = ["localhost", "127.0.0.1"]\nallow_unauthenticated_local = true\n[auth]\nenabled = false\n'''
report={'scope':'Browser-generated snippets parsed by the current repository; temporary filesystem discovery, no daemon','tests':[]}
def record(name,ok,detail):report['tests'].append({'name':name,'passed':bool(ok),'detail':detail});print(name,ok)
for idx,snippet in enumerate(json.loads((WORK/'generated-configs.json').read_text())):
 with tempfile.TemporaryDirectory(prefix='config-',dir=WORK) as td:
  root=Path(td);path=root/'config.toml';path.write_text(prefix+snippet);cfg=load_config(path)
  record(f'Native TOML parser {idx+1}',len(cfg.zone_collections)==1 and cfg.zone_collections[0].id=='@projects',str(cfg.zone_collections[0].base_directory))
  rule=cfg.zone_collections[0];names=['atlas','Project-A','unit_2'];tails=['ignoredbygit/exchange','work/exchange','out'];name=names[idx];tail=tails[idx]
  base=root/'repos';target=base/name/tail;target.mkdir(parents=True)
  rule=replace(rule,base_directory=base)
  candidates,diagnostics=discover_zone_collections([rule])
  expected=f'{name}/{tail}'.lower().replace('/','-')
  record(f'Directory discovery {idx+1}',len(candidates)==1 and candidates[0].zone.id==expected,{'expected_id':expected,'actual_ids':[c.zone.id for c in candidates],'diagnostics':diagnostics})
  record(f'First-directory label without Git {idx+1}',len(candidates)==1 and candidates[0].zone.label==name and rule.label_mode=='first-directory',name)
  record(f'Explicit example retention {idx+1}',rule.retain==100,rule.retain)
  (target/'nested').mkdir();candidates,diagnostics=discover_zone_collections([rule]);record(f'Leaf directory requirement {idx+1}',not candidates,diagnostics)
  (base/'project.v2'/tail).mkdir(parents=True);candidates,diagnostics=discover_zone_collections([rule]);record(f'Invalid derived ID not silently rewritten {idx+1}',not candidates,diagnostics)
report['summary']={'passed':sum(t['passed'] for t in report['tests']),'failed':sum(not t['passed'] for t in report['tests'])}
report['checked_at']=datetime.now(timezone.utc).isoformat()
report['generated_configs_sha256']=hashlib.sha256((WORK/'generated-configs.json').read_bytes()).hexdigest()
(ROOT/'qa/native-config-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))

if report['summary']['failed']:
 raise SystemExit(1)
