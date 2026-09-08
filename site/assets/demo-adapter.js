/* Local website demo adapter. The original Pasteberth UI follows unchanged.
 * This is NOT the Pasteberth backend or a security/transaction simulator.
 * No network requests. All files live in this document's memory.
 */
(() => {
'use strict';
const original = window.PB_DEMO_SEED;
const state = structuredClone(original.overview);
const files = new Map();
const objectURLs = new Set();
const limit = 8 * 1024 * 1024, totalLimit = 32 * 1024 * 1024;
let anonymousId = 0;
const textExtensions = {'text/plain':'.txt','text/html':'.html','text/markdown':'.md',
 'text/css':'.css','text/javascript':'.js','text/csv':'.csv','text/x-python':'.py',
 'text/x-shellscript':'.sh','application/json':'.json','application/xml':'.xml','application/x-yaml':'.yaml'};
const toBytes = b64 => Uint8Array.from(atob(b64), c => c.charCodeAt(0));
const key = (z,f) => z + '/' + f;
function attach(zone,item,blob) {
  const url=URL.createObjectURL(blob);objectURLs.add(url);
  files.set(key(zone.id,item.filename),{blob,url});
  item.preview_url=url;
  item.reference=`@/repo/${zone.label}/ignoredbygit/exchange/${item.filename}`;
  return item;
}
for(const zone of state.zones) for(const item of zone.images) {
 const source=original.files[item.filename];
 attach(zone,item,new Blob([toBytes(source.base64)],{type:source.mime}));
}
function json(data,status=200){return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});}
function error(code,message,status=400){return json({error:{code,message}},status);}
function remove(zone,filename){
 const index=zone.images.findIndex(i=>i.filename===filename);if(index<0)return false;
 zone.images.splice(index,1);const f=files.get(key(zone.id,filename));if(f){URL.revokeObjectURL(f.url);objectURLs.delete(f.url);}files.delete(key(zone.id,filename));zone.count=zone.images.length;return true;
}
function refreshCounts(){for(const z of state.zones)z.count=z.images.length;}
function totalSize(){return [...files.values()].reduce((s,f)=>s+f.blob.size,0);}
function safeName(name){return !!name && !/[\/\\\x00-\x1f\x7f]/.test(name) && !['.','..'].includes(name) && name.length<=200;}
// A small ZIP writer using stored entries (no compression, UTF-8 filenames).
function archive(entries){
 const enc=new TextEncoder();let offset=0;const local=[],central=[];
 const table=Array.from({length:256},(_,n)=>{let c=n;for(let i=0;i<8;i++)c=c&1?0xedb88320^(c>>>1):c>>>1;return c>>>0;});
 function crc(bytes){let c=0xffffffff;for(const b of bytes)c=table[(c^b)&255]^(c>>>8);return (c^0xffffffff)>>>0;}
 function record(size){const b=new Uint8Array(size);return [b,new DataView(b.buffer)];}
 for(const {name,bytes} of entries){
  const n=enc.encode(name),sum=crc(bytes);let [h,d]=record(30+n.length);
  d.setUint32(0,0x04034b50,true);d.setUint16(4,20,true);d.setUint16(6,0x800,true);d.setUint16(12,0x21,true);
  d.setUint32(14,sum,true);d.setUint32(18,bytes.length,true);d.setUint32(22,bytes.length,true);d.setUint16(26,n.length,true);h.set(n,30);
  local.push(h,bytes);
  let [ch,cd]=record(46+n.length);cd.setUint32(0,0x02014b50,true);cd.setUint16(4,20,true);cd.setUint16(6,20,true);cd.setUint16(8,0x800,true);cd.setUint16(14,0x21,true);
  cd.setUint32(16,sum,true);cd.setUint32(20,bytes.length,true);cd.setUint32(24,bytes.length,true);cd.setUint16(28,n.length,true);cd.setUint32(42,offset,true);ch.set(n,46);central.push(ch);offset+=h.length+bytes.length;
 }
 const centralSize=central.reduce((s,a)=>s+a.length,0);const [end,ed]=record(22);ed.setUint32(0,0x06054b50,true);ed.setUint16(8,entries.length,true);ed.setUint16(10,entries.length,true);ed.setUint32(12,centralSize,true);ed.setUint32(16,offset,true);
 return new Blob([...local,...central,end],{type:'application/zip'});
}
window.fetch=async function(url,options={}) {
 if(options.signal?.aborted)throw new DOMException('Aborted','AbortError');
 const str=String(url);
 // Preview/download bytes are read directly from memory; never via native fetch.
 if(str.startsWith('blob:')){
  const f=[...files.values()].find(x=>x.url===str.split('?')[0]);
  return f?new Response(f.blob,{headers:{'Content-Type':f.blob.type}}):error('unknown_image','Not in this local demo',404);
 }
 const path=new URL(str,'https://demo.invalid').pathname,method=options.method||'GET';
 if(path==='/api/zones' && method==='GET'){refreshCounts();return json(state);}
 if(path==='/api/groups')return json({groups:state.groups});
 if(path==='/api/health')return json({status:'ok',demo:true});
 if(path==='/api/transfers' && method==='POST'){
  const b=JSON.parse(options.body),s=state.zones.find(z=>z.id===b.source_zone),t=state.zones.find(z=>z.id===b.target_zone);
  if(!s||!t||s===t||!['move','copy'].includes(b.mode))return error('invalid_request','Invalid demo transfer');
   if(!Array.isArray(b.filenames)||b.filenames.some(n=>!s.images.some(i=>i.filename===n)))return error('unknown_image','Unknown file',404);
   if(new Set(b.filenames).size!==b.filenames.length)return error('invalid_request','Duplicate demo transfer filenames');
  if(b.filenames.some(n=>t.images.some(i=>i.filename===n)))return error('storage_conflict','A filename already exists in the destination',409);
  if(b.mode==='copy' && totalSize()+b.filenames.reduce((sum,n)=>sum+files.get(key(s.id,n)).blob.size,0)>totalLimit)return error('too_large','Local demo limit: 32 MiB of files in total',413);
  const transferred=[];
  for(const name of b.filenames){const source=s.images.find(i=>i.filename===name),copy=attach(t,structuredClone(source),files.get(key(s.id,name)).blob);t.images.unshift(copy);transferred.push(copy);if(b.mode==='move')remove(s,name);}
  refreshCounts();return json({transferred,failed:[],retention_deleted:[]});
 }
 const match=path.match(/^\/api\/zones\/([^/]+)\/images(?:\/(.*))?$/);
 if(!match)return error('not_found','This action is outside the local demo',404);
 const z=state.zones.find(i=>i.id===decodeURIComponent(match[1]));if(!z)return error('unknown_zone','Unknown zone',404);
 const tail=match[2]?decodeURIComponent(match[2]):'';
 if(method==='GET'&&!tail)return json({images:z.images});
 if(method==='POST'&&!tail){
  const form=options.body,file=form.get('image');
  if(!(file instanceof Blob)||!file.size)return error('empty_upload','The file is empty');
   if(file.size>limit)return error('too_large','Local demo limit: 8 MiB per file',413);
   const named=form.get('preserve_name')==='1';
   if(named&&!safeName(file.name))return error('invalid_filename','Unsupported name');
   const declared=file.type.split(';')[0].trim().toLowerCase();
   const bytes=new Uint8Array(await file.arrayBuffer());let kind='binary',mime='application/octet-stream',extension='.bin',width=null,height=null,format=null;
   if(/^image\/(png|jpeg|webp)$/.test(declared)){try{const image=await createImageBitmap(file);width=image.width;height=image.height;image.close();kind='image';mime=declared;format=mime.split('/')[1];extension='.'+format;}catch{}}
   if(kind==='binary'){try{new TextDecoder('utf-8',{fatal:true}).decode(bytes);if(!bytes.includes(0)){kind='text';mime=declared.startsWith('text/')||['application/json','application/xml','application/x-yaml'].includes(declared)?declared:'text/plain';extension=textExtensions[mime]||'.txt';}}catch{}}
   const filename=named?file.name:`demo-${Date.now()}-${++anonymousId}${extension}`;
   // Reads/decoding yield. Re-read conflicts and count net bytes here; nothing
   // between this check and commit awaits, including the transfer branch above.
   const old=z.images.find(i=>i.filename===filename);
   if(old&&form.get('replace')!=='1')return error('replacement_required','Existing demo file',428);
   if(totalSize()-(old?files.get(key(z.id,filename)).blob.size:0)+file.size>totalLimit)return error('too_large','Local demo limit: 32 MiB of files in total',413);
  if(old)remove(z,filename);
  const item=attach(z,{id:filename,filename,created_at:new Date().toISOString(),changed_at:null,size:file.size,kind,mime,format,width,height,comment:'',creation_method:form.get('creation_method')||'web_mouse_drop',replaced:!!old},new Blob([bytes],{type:mime}));
  z.images.unshift(item);const deleted=[];while(z.images.length>z.retain){const n=z.images.at(-1).filename;remove(z,n);deleted.push(n);}refreshCounts();
  return json({...item,retention_deleted:deleted},201);
 }
 if(method==='POST' && tail==='archive'){
  const names=JSON.parse(options.body).filenames||[],entries=[];
  for(const name of names){const f=files.get(key(z.id,name));if(!f)return error('unknown_image','Unknown file',404);entries.push({name,bytes:new Uint8Array(await f.blob.arrayBuffer())});}
  return new Response(archive(entries),{headers:{'Content-Type':'application/zip','Content-Disposition':'attachment; filename="pasteberth-demo.zip"'}});
 }
 if(method==='POST' && tail==='batch-delete'){
  const names=JSON.parse(options.body).filenames||[];return json({deleted:names.filter(n=>remove(z,n)),failed:[]});
 }
 if(method==='PATCH' && tail.endsWith('/comment')){
  const name=tail.slice(0,-8),item=z.images.find(i=>i.filename===name);if(!item)return error('unknown_image','Unknown file',404);
  item.comment=String(JSON.parse(options.body).comment||'').slice(0,1000);return json(item);
 }
 if(method==='DELETE'){return remove(z,tail)?json({deleted:tail}):error('unknown_image','Unknown file',404);}
 return error('invalid_request','Unsupported operation in this local demo',400);
};
// The real UI submits ZIP requests through a hidden form. Redirect that
// transport into the same memory adapter, without allowing any form network IO.
HTMLFormElement.prototype.submit=function(){
 const path=this.getAttribute('action')||'';
 if(!/^\/api\/zones\/[^/]+\/images\/archive$/.test(path))return;
 const names=new FormData(this).getAll('filename');
 window.fetch(path,{method:'POST',body:JSON.stringify({filenames:names})}).then(async response=>{
  if(!response.ok)throw new Error('Unable to create demo ZIP');
  const blob=await response.blob(),url=URL.createObjectURL(blob);objectURLs.add(url);
  const a=document.createElement('a');a.href=url;a.download='pasteberth-selection.zip';document.body.append(a);a.click();a.remove();
 }).catch(error=>{const toast=document.getElementById('toast');toast.textContent=error.message;toast.hidden=false;});
};
function group(name){[...document.querySelectorAll('.group-tab')].find(b=>b.textContent.startsWith(name))?.click();}
function focus(){group('Projects');const first=document.querySelector('.tab-zone-link');if(first?.getAttribute('aria-expanded')!=='true')first?.click();}
function scene(name){
 if(name==='overview'){group('Overview');return;}
 focus();
 const thumbs=document.querySelectorAll('.zone .thumb-wrap');
 if(name==='selection'&&thumbs.length>3){thumbs[2].click();document.querySelectorAll('.zone .thumb-wrap')[3]?.dispatchEvent(new MouseEvent('click',{bubbles:true,ctrlKey:true}));}
 else if(thumbs.length)thumbs[0].click();
}
window.addEventListener('message',e=>{if(e.source!==parent||e.data?.type!=='pb-demo')return;scene(e.data.scene);});
const timer=setInterval(()=>{if(document.querySelector('.tab-zone-link')){clearInterval(timer);focus();parent.postMessage({type:'pb-demo-ready'},'*');}},60);
window.addEventListener('pagehide',()=>{for(const url of objectURLs)URL.revokeObjectURL(url);});
})();
