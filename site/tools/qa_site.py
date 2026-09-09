"""Reproducible local tests of the website, not of upstream storage guarantees."""
from pathlib import Path
from datetime import datetime, timezone
from html import unescape
import asyncio,json,re,zipfile,io,hashlib,sys,tempfile,os
from playwright.async_api import async_playwright
from scratch import scratch_directory
OUT=Path(__file__).resolve().parents[1];WORK=scratch_directory()
report={'scope':'Pasteberth website v2.1, Chromium local in-memory rendering','tests':[],'errors':[],'network_requests':[]}
def record(name,ok,detail=None):report['tests'].append({'name':name,'passed':bool(ok),**({'detail':detail}if detail is not None else {})});print(('PASS ' if ok else 'FAIL ')+name,flush=True)
async def run():
 async with async_playwright() as p:
  browser=await p.chromium.launch(executable_path=os.environ.get('CHROMIUM'),headless=True,args=['--no-sandbox','--disable-gpu','--disable-dev-shm-usage'])
  report['browser']=browser.version
  page=await browser.new_page(viewport={'width':1600,'height':1000},device_scale_factor=1)
  page.set_default_timeout(3500)
  page.on('pageerror',lambda e:report['errors'].append(str(e)))
  page.on('request',lambda r:report['network_requests'].append(r.url))
  page.on('dialog',lambda d:asyncio.create_task(d.accept()))
  await page.set_content((OUT/'preview.html').read_text(),wait_until='load')
  static_copy=unescape(re.search(r'<code id="method-code">(.*?)</code>',(OUT/'index.html').read_text(),re.S)[1])
  record('Dynamic copy example matches guarded static HTML',await page.locator('#method-code').inner_text()==static_copy)
  await page.add_style_tag(content='html{scroll-behavior:auto!important}')
  for lang in ['fr','en']:
   await page.locator(f'[data-lang="{lang}"]').click()
   for width in [320,360,390,430,768,1024,1280,1600,1920]:
    await page.set_viewport_size({'width':width,'height':900})
    dims=await page.evaluate('({vw:innerWidth,sw:document.documentElement.scrollWidth})')
    record(f'No horizontal overflow {lang} {width}',dims['sw']<=dims['vw'],dims)
  await page.set_viewport_size({'width':1600,'height':1000});await page.locator('[data-lang="fr"]').click()
  # Language completeness: static locale spans and dynamic panels are both switched.
  record('French title', 'Vos fichiers' in await page.title())
  await page.locator('[data-lang="en"]').click();record('English title','Your files' in await page.title())
  for profile in ['personal','documents','tools','projects']:
   await page.locator('[data-profile="'+profile+'"]').click();record('Audience tab '+profile,await page.locator('#audience-panel').get_attribute('aria-labelledby')=='use-'+profile)
  await page.locator('[data-profile="personal"]').focus();await page.keyboard.press('ArrowRight');record('Keyboard tab navigation',await page.locator('[data-profile="documents"]').get_attribute('aria-selected')=='true')
  for kind in ['image','text','pdf','xlsx']:
   await page.locator('[data-kind="'+kind+'"]').click();record('Content copy availability '+kind,await page.locator('#copy-content').is_disabled()==(kind in ['pdf','xlsx']))
  for kind,filename in [('pdf','atlas-report.pdf'),('xlsx','atlas.xlsx'),('image','atlas.png'),('text','notes.txt')]:
   await page.locator('[data-kind="'+kind+'"]').click()
   async with page.expect_download() as download:await page.locator('#download-sample').click()
   d=await download.value;data=Path(await d.path()).read_bytes();expected=(OUT/'assets/examples'/filename).read_bytes();record('Download exact bytes '+filename,data==expected and d.suggested_filename==filename)
  # Exercise accepted and refused clipboard paths explicitly, not native OS clipboard interoperability.
  await page.evaluate('''()=>{window.__copied=[];Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>window.__copied.push(text),write:async()=>{window.__imageCopied=true}}});}''')
  await page.locator('[data-kind="text"]').click();await page.locator('#copy-reference').click();record('Reference copied with exact fictional path',(await page.evaluate('window.__copied.at(-1)'))=='@/repo/atlas/ignoredbygit/exchange/notes.txt')
  await page.locator('#copy-content').click();record('Text content copy mocked success','ATLAS' in await page.evaluate('window.__copied.at(-1)'))
  await page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new Error('denied')}}})")
  await page.locator('#copy-reference').click();record('Clipboard refusal opens manual fallback',await page.locator('#copy-dialog').is_visible());await page.locator('#copy-dialog .close-dialog').click()
  # Configuration generator: produce actual snippets for native parser checks.
  configs=[]
  for root,pattern,name in [('/repo','{project}/ignoredbygit/exchange','atlas'),('/srv/workspaces','{project}/work/exchange','Project-A'),(str(WORK/'a "quote"'),'{project}/out','unit_2')]:
   for field,value in [('root-path',root),('path-pattern',pattern),('project-name',name)]:await page.locator('#'+field).fill(value)
   valid=not await page.locator('#copy-config').is_disabled();configs.append(await page.locator('#generated-config').inner_text());record('Generator valid '+name,valid)
  for field,value in [('root-path','relative/path'),('root-path','/srv/../etc'),('path-pattern','{project}/../exchange'),('path-pattern','x/{project}'),('project-name','project.v2'),('project-name','<script>alert(1)</script>'),('project-name','a'*80)]:
   await page.locator('#root-path').fill('/repo');await page.locator('#path-pattern').fill('{project}/ignoredbygit/exchange');await page.locator('#project-name').fill('atlas');await page.locator('#'+field).fill(value)
   record('Generator rejects '+value,await page.locator('#copy-config').is_disabled() and await page.locator('#config-error').is_visible())
  await page.locator('#root-path').fill('/repo');await page.locator('#path-pattern').fill('{project}/ignoredbygit/exchange');await page.locator('#project-name').fill('atlas')
  async with page.expect_download() as download:await page.locator('#download-config').click()
  d=await download.value;record('TOML snippet download',d.suggested_filename=='pasteberth-zones.toml');(WORK/'generated-configs.json').write_text(json.dumps(configs))
  await page.locator('#add-project').click();await page.wait_for_timeout(900);record('Directory discovery simulation adds matching zone',await page.locator('#new-zone-pill').is_visible() and '4 zones' in await page.locator('#zone-count').inner_text())
  await page.locator('#add-project').click();record('Discovery replay resets',not await page.locator('#new-zone-pill').is_visible())
  for method in ['local','cli','mcp']:
   await page.locator('[data-method="'+method+'"]').click();record('Publication method '+method,await page.locator('#method-panel').get_attribute('aria-labelledby')=='method-'+method)
  for method in ['local','configured']:
   await page.locator('[data-start="'+method+'"]').click();record('Installation tab '+method,await page.locator('#start-code-panel').get_attribute('aria-labelledby')=='start-'+method)
  # Actual upstream frontend, offline adapter.
  await page.locator('#launch-demo').click();f=page.frame_locator('#demo-frame');await f.locator('.zone').first.wait_for()
  record('Real frontend boots in local sandbox',await f.locator('.thumb-wrap').count()==6)
  await page.locator('[data-scene="overview"]').click();await page.wait_for_timeout(80);record('Demo overview has 3 zones',await f.locator('.zone').count()==3)
  await page.locator('[data-scene="selection"]').click();await page.wait_for_timeout(80)
  record('Demo scene selects two items',await f.locator('.bulk-summary').inner_text()=='2 files selected')
  async with page.expect_download() as download:await f.get_by_role('button',name='Download 2 files as ZIP').click()
  d=await download.value;zipbytes=Path(await d.path()).read_bytes()
  with zipfile.ZipFile(io.BytesIO(zipbytes)) as z:
   record('ZIP has exact selected PDF and Excel',set(z.namelist())=={'atlas-report.pdf','atlas.xlsx'} and z.testzip() is None and z.read('atlas.xlsx')==(OUT/'assets/examples/atlas.xlsx').read_bytes())
  # Named input; copy/delete actions affect memory only.
  await page.locator('[data-scene="focus"]').click()
  async with page.expect_file_chooser() as chooser:await f.locator('.zone-upload-btn').first.click()
  ch=await chooser.value;await ch.set_files({'name':'demo-incoming.txt','mimeType':'text/plain','buffer':b'Fictional incoming file.'})
  await page.wait_for_timeout(220)
  record('File picker upload is visible',await f.locator('.zone').first.get_by_text('demo-incoming.txt',exact=True).count()>0)
  record('Upload preview is memory-only',await f.locator('.zone').first.locator('.download-btn').first.get_attribute('data-preview') is not None)
  # Comment endpoint exercised through the original UI.
  await f.get_by_role('button',name='Comment for demo-incoming.txt',exact=True).click()
  comment=f.get_by_role('textbox',name='Comment for demo-incoming.txt',exact=True)
  await comment.fill('Fictional review note')
  # Use the documented Ctrl+Enter submit shortcut from the source editor.
  await comment.press('Control+Enter');await f.locator('.comment-text').filter(has_text='Fictional review note').wait_for()
  record('Comment is preserved in demo', 'Fictional review note' in await f.locator('.zone').first.inner_text())
  # Copy newly named item to orbit, where it does not conflict.
  await f.locator('.transfer-target').first.select_option('orbit-ignoredbygit-exchange')
  await f.locator('.copy-transfer-btn').first.click();await page.wait_for_timeout(180)
  await f.locator('.tab-zone-link[data-zone="orbit-ignoredbygit-exchange"]').click()
  record('Demo inter-zone copy',await f.locator('.zone[data-zone="orbit-ignoredbygit-exchange"]').get_by_text('demo-incoming.txt',exact=True).count()>0)
  await page.locator('#reset-demo').click();await page.wait_for_timeout(500)
  record('Reset discards added file',await f.get_by_text('demo-incoming.txt',exact=True).count()==0)
  # Mobile navigation and dialog.
  await page.set_viewport_size({'width':390,'height':844});await page.locator('#menu-toggle').click();record('Mobile menu opens',await page.locator('#mobile-nav').is_visible());await page.keyboard.press('Escape');record('Escape closes mobile menu',not await page.locator('#mobile-nav').is_visible())
  await page.locator('.hero-shot').click();record('Screenshot opens in dialog',await page.locator('#image-dialog').is_visible());await page.keyboard.press('Escape');record('Escape closes screenshot',not await page.locator('#image-dialog').is_visible())
  record('No JavaScript errors',not report['errors'],report['errors'])
  external=[u for u in report['network_requests'] if re.match('https?://',u)]
  record('No external HTTP requests during interaction',not external,external)
  # Final previews are made from fresh contexts with deliberate initial UI state.
  for lang,width in ([('fr',1600),('fr',390),('en',1600),('en',390)] if '--screenshots' in sys.argv else []):
   (OUT/'previews').mkdir(exist_ok=True)
   pg=await browser.new_page(viewport={'width':width,'height':1000 if width>720 else 844},device_scale_factor=1)
   await pg.set_content((OUT/'preview.html').read_text(),wait_until='load');await pg.add_style_tag(content='html{scroll-behavior:auto!important}')
   await pg.locator('[data-lang="'+lang+'"]').click();await pg.locator('#launch-demo').click();await pg.frame_locator('#demo-frame').locator('.zone').first.wait_for();await pg.evaluate('window.scrollTo(0,0)');await pg.wait_for_timeout(80)
   filename=('desktop' if width>720 else 'mobile')+'-'+lang+'.png';await pg.screenshot(path=str(OUT/'previews'/filename),full_page=True)
   if width==1600:await pg.screenshot(path=str(OUT/'previews'/('hero-'+lang+'.png')))
   await pg.close()
  await browser.close()
try:asyncio.run(run())
except Exception as e:record('Test runner completed',False,str(e))
report['network_requests']=[u for u in report['network_requests'] if re.match('https?://',u)]
report['summary']={'passed':sum(t['passed']for t in report['tests']),'failed':sum(not t['passed']for t in report['tests'])}
report['checked_at']=datetime.now(timezone.utc).isoformat()
report['preview_sha256']=hashlib.sha256((OUT/'preview.html').read_bytes()).hexdigest()
(OUT/'qa/report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
print(report['summary'])

if report['summary']['failed']:
 raise SystemExit(1)
