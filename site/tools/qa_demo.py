#!/usr/bin/env python3
"""Demo regressions: synthetic clipboard events, mocked rich writes and memory quota."""
import hashlib
import json
import os
import unittest
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
            async zones() { return (await (await fetch('/api/zones')).json()).zones; },
            async clear() {
              for (const z of await this.zones()) for (const i of z.images)
                await fetch(`/api/zones/${z.id}/images/${i.filename}`, {method:'DELETE'});
            },
            async upload(size, name, zone, replace=false) {
              const form = new FormData();
              form.append('image', new File([new Uint8Array(size)], name, {type:'application/octet-stream'}));
              form.append('preserve_name','1');
              if (replace) form.append('replace','1');
              return (await fetch(`/api/zones/${zone}/images`, {method:'POST', body:form})).status;
            },
            async transfer(source_zone, target_zone, filenames, mode='copy') {
              return (await fetch('/api/transfers', {method:'POST',body:JSON.stringify({source_zone,target_zone,filenames,mode})})).status;
            },
            async used() { return (await this.zones()).flatMap(z=>z.images).reduce((n,i)=>n+i.size,0); }
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
          const item=(await demoTest.zones())[0].images.find(i=>i.filename.startsWith('demo-'));
          const response=await fetch(item.preview_url);
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
              const form=new FormData(); form.append('image',new Blob(['Synthetic text'],{type:mime}),'clipboard');
              const response=await fetch(`/api/zones/${zone}/images`,{method:'POST',body:form});
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
          return {statuses,full,same,used:await demoTest.used(),count:(await demoTest.zones())[0].images.length};
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
