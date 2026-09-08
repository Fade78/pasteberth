#!/usr/bin/env python3
"""Test site v2.1 theme and locale selection locally with Chromium.

The browser is loaded in memory. Query strings and Storage are injected inputs,
not an assertion that the hosted URL or native persistent storage was tested.
No remote requests, external fonts or native clipboard permissions are needed.
"""
from __future__ import annotations
import asyncio, json, re, os, hashlib
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright
from rebuild import product_sources

ROOT = Path(__file__).resolve().parents[1]
KEY = 'pasteberth-site-language-choice'
report = {'scope': 'Website v2.1 locale and visual-theme regression',
          'input_model': 'Real page and production scripts; injected query-string and Storage inputs for deterministic locale tests.',
          'tests': [], 'page_errors': [], 'network_requests': []}

def record(name: str, ok: bool, detail=None):
    report['tests'].append({'name': name, 'passed': bool(ok), **({'detail': detail} if detail is not None else {})})
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)

def instrument(source: str, case: dict) -> str:
    fixture = json.dumps(case, ensure_ascii=True).replace('<', '\\u003c')
    script = '''<script>
const c=CASE;
window.__store={...(c.stored||{})}; window.__writes=[];
Object.defineProperty(window,'localStorage',{configurable:true,value:{
 getItem(k){if(c.readBlocked)throw new DOMException('Blocked','SecurityError');return window.__store[k]??null;},
 setItem(k,v){if(c.writeBlocked)throw new DOMException('Blocked','SecurityError');window.__store[k]=String(v);window.__writes.push([k,String(v)]);}
}});
const NativeParams=window.URLSearchParams;
window.URLSearchParams=class extends NativeParams{constructor(value){super(value===location.search?(c.query||''):value);}};
</script>'''.replace('CASE', fixture)
    return source.replace('<head>', '<head>'+script, 1)

async def run():
    source=(ROOT/'preview.html').read_text(encoding='utf-8')
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=os.environ.get('CHROMIUM'),headless=True,args=['--no-sandbox','--disable-gpu','--disable-dev-shm-usage'])
        report['browser']=browser.version
        try:
            cases=[
              {'name':'First visit, French browser','locale':'fr-FR','expected':'en','source':'default'},
              {'name':'First visit, English browser','locale':'en-US','expected':'en','source':'default'},
              {'name':'First visit, Japanese browser','locale':'ja-JP','expected':'en','source':'default'},
              {'name':'Explicit saved French','stored':{KEY:'fr'},'expected':'fr','source':'saved'},
              {'name':'Explicit saved English','stored':{KEY:'en'},'expected':'en','source':'saved'},
              {'name':'French URL overrides English choice','stored':{KEY:'en'},'query':'?lang=fr','expected':'fr','source':'url','writes':1},
              {'name':'English URL overrides French choice','stored':{KEY:'fr'},'query':'?lang=en','expected':'en','source':'url','writes':1},
              {'name':'Invalid URL falls back to saved French','stored':{KEY:'fr'},'query':'?lang=de','expected':'fr','source':'saved'},
              {'name':'Invalid URL falls back to English','query':'?lang=invalid','expected':'en','source':'default'},
              {'name':'Invalid stored preference ignored','stored':{KEY:'de'},'expected':'en','source':'default'},
              {'name':'Old v2 automatic French ignored','stored':{'pasteberth-site-language':'fr'},'expected':'en','source':'default'},
              {'name':'Blocked Storage defaults to English','readBlocked':True,'expected':'en','source':'default'},
              {'name':'URL works even when Storage read is blocked','readBlocked':True,'query':'?lang=fr','expected':'fr','source':'url','writes':1},
              {'name':'URL works even when Storage write is blocked','writeBlocked':True,'query':'?lang=fr','expected':'fr','source':'url'},
              {'name':'Empty URL value does not override choice','stored':{KEY:'fr'},'query':'?lang=','expected':'fr','source':'saved'},
            ]
            for case in cases:
                context=await browser.new_context(locale=case.get('locale','en-US'),viewport={'width':1600,'height':1000})
                page=await context.new_page()
                page.on('pageerror',lambda e:report['page_errors'].append(str(e)))
                page.on('request',lambda r:report['network_requests'].append(r.url))
                await page.set_content(instrument(source,case),wait_until='load')
                observed=await page.evaluate("({lang:document.documentElement.lang,source:document.documentElement.dataset.languageSource,writes:window.__writes,title:document.title})")
                record(case['name'],observed['lang']==case['expected'] and observed['source']==case['source'],observed)
                record(case['name']+' / only explicit writes',len(observed['writes'])==case.get('writes',0))
                opposite='fr' if case['expected']=='en' else 'en'
                count=await page.locator('.'+opposite+':visible').count()
                record(case['name']+' / no wrong-language spans',count==0,{'wrong_language_visible':count})
                await context.close()
            context=await browser.new_context(locale='fr-FR',viewport={'width':1600,'height':1000})
            page=await context.new_page()
            await page.set_content(instrument(source,{}),wait_until='load')
            await page.locator('[data-lang="fr"]').click()
            record('FR button persists explicit preference',await page.evaluate("document.documentElement.lang==='fr' && window.__store['"+KEY+"']==='fr'"))
            record('French screen-reader image description', (await page.locator('.hero-shot img').get_attribute('alt')).startswith('Interface réelle'))
            await page.locator('[data-lang="en"]').click()
            record('EN button persists explicit preference',await page.evaluate("document.documentElement.lang==='en' && window.__store['"+KEY+"']==='en'"))
            record('English screen-reader image description', (await page.locator('.hero-shot img').get_attribute('alt')).startswith('Actual Pasteberth'))
            record('English tab pressed', await page.locator('[data-lang="en"]').get_attribute('aria-pressed')=='true')
            record('English title', 'Your files' in await page.title())
            # Calculated color pairs; these checks do not constitute a full accessibility audit.
            pairs=[
              ('body text','#16263d','#f5f7fb'),('secondary text','#53657b','#f5f7fb'),
              ('primary CTA','#ffffff','#1762b3'),('primary CTA hover','#ffffff','#124b8e'),
              ('headline blue','#1762b3','#f5f7fb'),('raspberry PDF label','#a32a4a','#fff5f8'),
              ('mint XLSX label','#155a40','#f2fbf7'),('local operation badge','#155a40','#ddf8ed'),
              ('project CTA','#102d22','#36c98b'),('blue heading on dark','#4da3ff','#101b2b'),
              ('terminal code','#e4edf9','#182b44'),('config code','#ddf8ed','#101e32'),
            ]
            def lum(hex):
                v=[int(hex[i:i+2],16)/255 for i in (1,3,5)]
                v=[a/12.92 if a<=.04045 else ((a+.055)/1.055)**2.4 for a in v]
                return sum(a*w for a,w in zip(v,[.2126,.7152,.0722]))
            for name,fg,bg in pairs:
                a,b=lum(fg),lum(bg);contrast=(max(a,b)+.05)/(min(a,b)+.05)
                record('Contrast '+name,contrast>=4.5,{'foreground':fg,'background':bg,'ratio':round(contrast,2),'target':4.5})
            colors=await page.evaluate("Object.fromEntries(['--brand-blue','--brand-raspberry','--brand-mint'].map(k=>[k,getComputedStyle(document.documentElement).getPropertyValue(k).trim()]))")
            record('CSS contains exact icon palette',colors=={'--brand-blue':'#4da3ff','--brand-raspberry':'#c33d60','--brand-mint':'#36c98b'},colors)
            await context.close()
            context=await browser.new_context(java_script_enabled=False,locale='fr-FR')
            page=await context.new_page();await page.set_content(source,wait_until='load')
            record('Without JavaScript: English document and title',await page.locator('html').get_attribute('lang')=='en' and 'Your files' in await page.title())
            record('Without JavaScript: English hero only','Your files.' in await page.locator('#hero-title').inner_text() and await page.locator('.fr:visible').count()==0)
            await context.close()
            manifest=product_sources()
            for entry in manifest['frontend']:
                record('Current frontend equals runtime '+entry['file'],True,entry['sha256'])
            record('No JavaScript errors',not report['page_errors'],report['page_errors'])
            report['network_requests']=[u for u in report['network_requests'] if re.match(r'https?://',u)]
            record('No HTTP requests',not report['network_requests'],report['network_requests'])
        finally:
            await browser.close()

if __name__=='__main__':
    (ROOT/'qa').mkdir(exist_ok=True)
    try:asyncio.run(run())
    except Exception as e:record('Test runner completed',False,str(e))
    report['summary']={'passed':sum(t['passed'] for t in report['tests']),'failed':sum(not t['passed'] for t in report['tests'])}
    report['checked_at']=datetime.now(timezone.utc).isoformat()
    report['preview_sha256']=hashlib.sha256((ROOT/'preview.html').read_bytes()).hexdigest()
    (ROOT/'qa/brand-language-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(report['summary'])
    raise SystemExit(bool(report['summary']['failed']))
