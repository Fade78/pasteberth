#!/usr/bin/env python3
"""Memory demo: generic item transport, absent validators, synthetic clipboard, quota and ZIP limits; not backend conditional-read verification."""
import base64
import hashlib
import io
import json
import os
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path=os.environ.get('CHROMIUM'), headless=True)
        type(self).browser_version = self.browser.version
        self.page = await self.browser.new_page()
        self.errors = []
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        await self.page.set_content((ROOT / 'demo.html').read_text(), wait_until='load')
        await self.page.locator('.zone').first.wait_for()
        await self.page.evaluate('''() => {
          window.demoTest = {
            async zones() { return (await (await fetch('/api/zones?schema=items')).json()).zones; },
            async clear() {
              for (const z of await this.zones()) for (const i of z.items)
                await fetch(`/api/zones/${z.id}/items/${i.filename}`, {method:'DELETE'});
            },
            async upload(size, name, zone, replace=false) {
              const form = new FormData();
              form.append('file', new File([new Uint8Array(size)], name, {type:'application/octet-stream'}));
              form.append('preserve_name','1');
              if (replace) form.append('replace','1');
              return (await fetch(`/api/zones/${zone}/items`, {method:'POST', body:form})).status;
            },
            async transfer(source_zone, target_zone, filenames, mode='copy') {
              return (await fetch('/api/transfers?schema=items', {method:'POST',body:JSON.stringify({source_zone,target_zone,filenames,mode})})).status;
            },
            async used() { return (await this.zones()).flatMap(z=>z.items).reduce((n,i)=>n+i.size,0); }
          };
        }''')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.assertEqual(self.errors, [])

    async def clipboard(self, mixed):
        await self.page.locator('.zone-select').first.click()
        await self.page.evaluate('''mixed => {
          window.__rich = null;
          Object.defineProperty(window,'isSecureContext',{configurable:true,value:true});
          window.ClipboardItem = class { constructor(flavors) { this.flavors=flavors; } };
          Object.defineProperty(navigator,'clipboard',{configurable:true,value:{
            writeText: async () => {},
            write: async items => {
              window.__rich = Object.fromEntries(await Promise.all(Object.entries(items[0].flavors).map(async ([k,v])=>[k,await v.text()])));
              document.documentElement.dataset.richCopied='true';
            }
          }});
          const data = new DataTransfer();
          if (mixed) {
            data.setData('text/plain','Mixed clipboard note');
            const bytes=Uint8Array.from(atob(PB_DEMO_SEED.files['atlas.png'].base64),c=>c.charCodeAt(0));
            data.items.add(new File([bytes],'clipboard.png',{type:'image/png'}));
          } else {
            data.setData('text/html','<p onclick="alert(1)">HTML clipboard note</p><script>alert(2)</script>');
          }
          window.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
        }''', mixed)
        await self.page.locator('.zone').first.locator('.download-btn[data-filename^="demo-"]').wait_for()
        item = await self.page.evaluate('''async () => {
          const item=(await demoTest.zones())[0].items.find(i=>i.filename.startsWith('demo-'));
          const response=await fetch(item.content_url);
          return {...item, previewMime:response.headers.get('content-type'), original:await response.text()};
        }''')
        self.assertEqual(item['mime'], 'text/html')
        self.assertEqual(item['previewMime'], 'text/html')
        self.assertTrue(item['filename'].endswith('.html'))
        self.assertEqual(item['kind'], 'text')
        if mixed:
            self.assertIn('data:image/png;base64,', item['original'])
        else:
            self.assertIn('onclick=', item['original'], 'Storage must preserve original HTML bytes')
        await self.page.locator('.zone').first.locator('.copy-image-btn').click()
        await self.page.locator('html[data-rich-copied="true"]').wait_for()
        rich = await self.page.evaluate('window.__rich')
        self.assertIn('text/html', rich)
        self.assertIn('clipboard note', rich['text/plain'])
        self.assertNotIn('onclick=', rich['text/html'])
        self.assertNotIn('<script', rich['text/html'])
        if mixed:
            self.assertIn('data:image/png;base64,', rich['text/html'])

    async def test_html_only_clipboard_uses_rich_copy(self):
        await self.clipboard(False)

    async def test_mixed_clipboard_uses_rich_copy(self):
        await self.clipboard(True)

    async def test_generic_seed_content_has_no_digest_authority(self):
        result = await self.page.evaluate('''async () => {
          const zones=await demoTest.zones(), reads=[];
          for (const zone of zones) {
            const listing=await (await fetch(`/api/zones/${zone.id}/items`)).json();
            for (const item of listing.items) {
              const route=`/api/zones/${zone.id}/items/${encodeURIComponent(item.filename)}/content`;
              for (const url of [item.content_url,route]) {
                const get=await fetch(url), head=await fetch(url,{method:'HEAD'});
                const bytes=new Uint8Array(await get.arrayBuffer());
                const refused=[];
                for (const method of ['GET','HEAD']) {
                  const response=await fetch(url,{method,headers:{'If-Match':'"unverified"'}});
                  refused.push([response.status,(await response.json()).error.code]);
                }
                reads.push({filename:item.filename,status:get.status,etag:get.headers.get('etag'),
                  base64:btoa(String.fromCharCode(...bytes)),head:head.status,
                  headSize:head.headers.get('content-length'),headBody:await head.text(),refused});
              }
            }
          }
          const missing=await fetch(`/api/zones/${zones[0].id}/items/missing/content`);
          return {zones,reads,missing:[missing.status,(await missing.json()).error.code]};
        }''')
        self.assertEqual(result['missing'], [404, 'unknown_item'])
        for zone in result['zones']:
            self.assertNotIn('images', zone)
            for item in zone['items']:
                self.assertNotIn('preview_url', item)
                self.assertIsNone(item['sha256'])
                self.assertIsNone(item['etag'])
                self.assertTrue(item['content_url'].startswith('blob:'))
        for read in result['reads']:
            expected = (ROOT / 'assets/examples' / read['filename']).read_bytes()
            self.assertEqual(base64.b64decode(read['base64']), expected)
            self.assertEqual(read['status'], 200)
            self.assertEqual(read['head'], 200)
            self.assertEqual(read['headSize'], str(len(expected)))
            self.assertEqual(read['headBody'], '')
            self.assertIsNone(read['etag'])
            self.assertEqual(read['refused'], [[501, 'not_implemented']] * 2)

    async def test_generic_mutations_keep_validators_null(self):
        result = await self.page.evaluate('''async () => {
          const [a,b]=await demoTest.zones(), filename='consumer note.txt';
          const path=`/api/zones/${a.id}/items`, child=`${path}/${encodeURIComponent(filename)}`;
          async function upload(text,replace=false) {
            const form=new FormData();form.append('file',new Blob([text],{type:'text/plain'}),filename);
            form.append('preserve_name','1');if(replace)form.append('replace','1');
            const response=await fetch(path,{method:'POST',body:form});
            return {status:response.status,...await response.json()};
          }
          const uploaded=await upload('First synthetic content');
          const commented=await (await fetch(child+'/comment',{method:'PATCH',body:JSON.stringify({comment:'Synthetic note'})})).json();
          const replaced=await upload('Different synthetic bytes',true);
          const bytes=await (await fetch(child+'/content')).text();
          const transfer=await (await fetch('/api/transfers?schema=items',{method:'POST',body:JSON.stringify({
            source_zone:a.id,target_zone:b.id,filenames:[filename],mode:'copy'})})).json();
          const copied=transfer.transferred[0];
          const copyBytes=await (await fetch(copied.content_url)).text();
          const deleted=await (await fetch(child,{method:'DELETE'})).json();
          const batch=await (await fetch(`/api/zones/${b.id}/items/batch-delete`,{method:'POST',body:JSON.stringify({filenames:[filename]})})).json();
          return {uploaded,commented,replaced,copied,bytes,copyBytes,deleted,batch};
        }''')
        for name in ('uploaded', 'commented', 'replaced', 'copied'):
            item = result[name]
            self.assertIsNone(item['sha256'])
            self.assertIsNone(item['etag'])
            self.assertNotIn('preview_url', item)
            self.assertTrue(item['content_url'].startswith('blob:'))
        self.assertEqual(result['uploaded']['status'], 201)
        self.assertEqual(result['replaced']['status'], 201)
        self.assertTrue(result['replaced']['replaced'])
        self.assertEqual(result['commented']['comment'], 'Synthetic note')
        self.assertEqual(result['copied']['created_at'], result['replaced']['created_at'])
        self.assertEqual(result['bytes'], 'Different synthetic bytes')
        self.assertEqual(result['copyBytes'], result['bytes'])
        self.assertEqual(result['deleted'], {'deleted': 'consumer note.txt'})
        self.assertEqual(result['batch'], {'deleted': ['consumer note.txt'], 'failed': []})

    async def test_declared_text_mime_and_unique_anonymous_names(self):
        types = [('text/html; charset=utf-8', 'text/html', '.html'),
                 ('text/markdown', 'text/markdown', '.md'), ('text/csv', 'text/csv', '.csv'),
                 ('application/json', 'application/json', '.json'),
                 ('application/xml', 'application/xml', '.xml'),
                 ('application/x-yaml', 'application/x-yaml', '.yaml'),
                 ('text/custom', 'text/custom', '.txt'),
                 ('application/octet-stream', 'text/plain', '.txt')]
        items = await self.page.evaluate('''async types => {
          const zone=(await demoTest.zones())[0].id;
          const now=Date.now; Date.now=()=>123456789;
          try {
            return await Promise.all(types.map(async ([mime])=>{
              const form=new FormData(); form.append('file',new Blob(['Synthetic text'],{type:mime}),'clipboard');
              const response=await fetch(`/api/zones/${zone}/items`,{method:'POST',body:form});
              return {status:response.status,...await response.json()};
            }));
          } finally { Date.now=now; }
        }''', types)
        self.assertEqual(len({item['filename'] for item in items}), len(types))
        for item, (_, mime, extension) in zip(items, types):
            self.assertEqual(item['status'], 201)
            self.assertEqual(item['kind'], 'text')
            self.assertEqual(item['mime'], mime)
            self.assertTrue(item['filename'].endswith(extension))

    async def test_five_concurrent_uploads_respect_total_limit(self):
        result = await self.page.evaluate('''async () => {
          await demoTest.clear(); const z=(await demoTest.zones())[0].id;
          const statuses=await Promise.all(Array.from({length:5},(_,i)=>demoTest.upload(8*1024**2,`file-${i}.bin`,z)));
          return {statuses,used:await demoTest.used()};
        }''')
        self.assertEqual(sorted(result['statuses']), [201, 201, 201, 201, 413])
        self.assertEqual(result['used'], 32 * 1024**2)

    async def test_concurrent_replacements_count_net_growth(self):
        result = await self.page.evaluate('''async () => {
          await demoTest.clear(); const z=(await demoTest.zones())[0].id, M=1024**2;
          for(const [name,size] of [['a',8],['b',8],['c',4],['left',4],['right',4]]) await demoTest.upload(size*M,name,z);
          const statuses=await Promise.all(['left','right'].map(name=>demoTest.upload(8*M,name,z,true)));
          const full=await demoTest.used();
          const winner=statuses[0]===201?'left':'right';
          const same=await Promise.all([demoTest.upload(8*M,winner,z,true),demoTest.upload(8*M,winner,z,true)]);
          return {statuses,full,same,used:await demoTest.used(),count:(await demoTest.zones())[0].items.length};
        }''')
        self.assertEqual(sorted(result['statuses']), [201, 413])
        self.assertEqual(result['full'], 32 * 1024**2)
        self.assertEqual(result['same'], [201, 201])
        self.assertEqual(result['used'], 32 * 1024**2)
        self.assertEqual(result['count'], 5)

    async def test_transfer_during_upload_and_move_at_capacity(self):
        result = await self.page.evaluate('''async () => {
          await demoTest.clear(); const [a,b,c]=(await demoTest.zones()).map(z=>z.id), M=1024**2;
          for(const name of ['a','b','c']) await demoTest.upload(8*M,name,a);
          const pending=demoTest.upload(8*M,'pending',b);
          const copy=await demoTest.transfer(a,c,['a']);
          const upload=await pending;
          const refused=await demoTest.transfer(a,c,['b']);
          const move=await demoTest.transfer(a,b,['b'],'move');
          return {copy,upload,refused,move,used:await demoTest.used()};
        }''')
        self.assertEqual(result, {'copy':200,'upload':413,'refused':413,'move':200,'used':32*1024**2})

    async def test_archive_adapter_enforces_advertised_count_before_reading(self):
        result = await self.page.evaluate('''async () => {
          await demoTest.clear(); const z=(await demoTest.zones())[0].id;
          const names=Array.from({length:65},(_,i)=>`zip-${i}.bin`);
          for (const name of names) await demoTest.upload(1,name,z);
          const overview=await (await fetch('/api/zones?schema=items')).json();
          const read=Blob.prototype.arrayBuffer; let reads=0;
          Blob.prototype.arrayBuffer=function(){reads++;return read.call(this);};
          try {
            const request=filenames=>fetch(`/api/zones/${z}/items/archive`,{method:'POST',body:JSON.stringify({filenames})});
            const refused=await request(names), error=await refused.json(), rejectedReads=reads;
            const accepted=await request(names.slice(0,64));
            return {limit:overview.max_archive_files,refused:refused.status,error:error.error.code,
              rejectedReads,accepted:accepted.status,mime:accepted.headers.get('content-type'),reads};
          } finally { Blob.prototype.arrayBuffer=read; }
        }''')
        self.assertEqual(result, {'limit':64,'refused':413,'error':'too_large','rejectedReads':0,
                                  'accepted':200,'mime':'application/zip','reads':64})

    async def test_archive_ui_preflights_65_and_downloads_64(self):
        # Expand only the synthetic seed; the adapter and product UI remain unchanged.
        adapter = '<script>' + (ROOT / 'assets/demo-adapter.js').read_text()
        fixture = '''<script>
          const seed=window.PB_DEMO_SEED, zone=seed.overview.zones[0], item=zone.items[0];
          zone.items=Array.from({length:65},(_,i)=>{
            const filename=`selection-${i}.png`;
            seed.files[filename]=seed.files[item.filename];
            return {...item,id:filename,filename};
          });
          zone.count=zone.items.length;
        </script>'''
        await self.page.set_content((ROOT / 'demo.html').read_text().replace(adapter, fixture + adapter, 1), wait_until='load')
        thumbs = self.page.locator('.zone').first.locator('.thumb-wrap')
        await thumbs.nth(64).wait_for()
        await self.page.evaluate('''() => {
          window.archiveSubmissions=0;
          const submit=HTMLFormElement.prototype.submit;
          HTMLFormElement.prototype.submit=function(){window.archiveSubmissions++;return submit.call(this);};
        }''')
        await thumbs.first.click()
        await thumbs.nth(64).click(modifiers=['Shift'])
        await self.page.get_by_role('button', name='Download 65 files as ZIP', exact=True).click()
        toast = self.page.locator('#toast')
        self.assertEqual(await toast.inner_text(), 'ZIP downloads allow a maximum of 64 files; 65 selected')
        self.assertTrue(await toast.is_visible())
        self.assertIn('error', await toast.get_attribute('class'))
        self.assertEqual(await self.page.evaluate('window.archiveSubmissions'), 0)
        self.assertEqual(await self.page.locator('iframe[name^="pb-archive-"]').count(), 0)
        await thumbs.nth(64).click(modifiers=['Control'])
        async with self.page.expect_download() as download:
            await self.page.get_by_role('button', name='Download 64 files as ZIP', exact=True).click()
        data = Path(await (await download.value).path()).read_bytes()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(set(archive.namelist()), {f'selection-{i}.png' for i in range(64)})
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read('selection-0.png'), (ROOT / 'assets/examples/atlas.png').read_bytes())
        self.assertEqual(await self.page.evaluate('window.archiveSubmissions'), 1)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DemoTests))
    (ROOT / 'qa').mkdir(exist_ok=True)
    report = {
        'scope': __doc__,
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'browser': getattr(DemoTests, 'browser_version', None),
        'demo_sha256': hashlib.sha256((ROOT / 'demo.html').read_bytes()).hexdigest(),
        'summary': {'passed': result.testsRun-len(result.failures)-len(result.errors), 'failed':len(result.failures)+len(result.errors)},
        'failures': [(str(test), detail) for test, detail in result.failures+result.errors],
    }
    (ROOT / 'qa/demo-report.json').write_text(json.dumps(report, indent=2)+'\n')
    raise SystemExit(not result.wasSuccessful())
