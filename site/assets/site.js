/* Pasteberth website v2.1 — no dependencies, analytics or network API calls. */
(() => {
'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const state={lang:'en',kind:'image',profile:'personal',method:'local',start:'local',scene:'focus',demoStarted:false,projectAdded:false};
const tr=(fr,en)=>state.lang==='fr'?fr:en;
let toastTimer,discoveryTimer;
function notify(message){clearTimeout(toastTimer);$('#toast').textContent=message;$('#toast').hidden=false;toastTimer=setTimeout(()=>$('#toast').hidden=true,5000);}
function setTabs(selector,attr,value){$$(selector).forEach(b=>{const yes=b.dataset[attr]===value;b.setAttribute('aria-selected',yes);b.tabIndex=yes?0:-1;});}
function bytes(name){const f=window.PB_EXAMPLES[name];if(!f)throw new Error('Unknown demo file');return Uint8Array.from(atob(f.base64),c=>c.charCodeAt(0));}
function fileBlob(name){return new Blob([bytes(name)],{type:window.PB_EXAMPLES[name].mime});}
function save(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);}
async function copyText(text){
 try{if(!navigator.clipboard?.writeText)throw new Error('Clipboard unavailable');await navigator.clipboard.writeText(text);notify(tr('Copié dans le presse-papiers.','Copied to the clipboard.'));return true;}
 catch{const d=$('#copy-dialog');$('#copy-value').value=text;d.showModal();$('#copy-value').focus();$('#copy-value').select();return false;}
}
$$('.close-dialog').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
$$('dialog').forEach(d=>d.addEventListener('click',e=>{if(e.target!==d)return;const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)d.close();}));
$('#select-copy').addEventListener('click',()=>{$('#copy-value').focus();$('#copy-value').select();});
$$('[data-image]').forEach(b=>b.addEventListener('click',()=>{$('#enlarged-image').src=b.dataset.image;$('#image-dialog').showModal();}));
function menu(open){$('#mobile-nav').hidden=!open;$('#menu-toggle').setAttribute('aria-expanded',String(open));}
$('#menu-toggle').addEventListener('click',()=>menu($('#mobile-nav').hidden));
$$('#mobile-nav a').forEach(a=>a.addEventListener('click',()=>menu(false)));
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#mobile-nav').hidden){menu(false);$('#menu-toggle').focus();}});
document.addEventListener('click',e=>{if(!e.target.closest('.site-header'))menu(false);});
// Shared accessible keyboard behaviour for all single-select tablists.
$$('[role=tablist]').forEach(list=>list.addEventListener('keydown',e=>{
 if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
 const tabs=[...list.querySelectorAll('[role=tab]')],i=tabs.indexOf(document.activeElement);if(i<0)return;e.preventDefault();
 const j=e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;tabs[j].click();tabs[j].focus();
}));
const sampleFiles={image:'atlas.png',text:'notes.txt',pdf:'atlas-report.pdf',xlsx:'atlas.xlsx'};
function renderSample(){
 setTabs('[data-kind]','kind',state.kind);$('#sample-panel').setAttribute('aria-labelledby','kind-'+state.kind);
 const name=sampleFiles[state.kind],canCopy=['image','text'].includes(state.kind);
 $('#sample-name').textContent=name;$('#sample-kind-label').textContent=(state.kind==='image'?'PNG':state.kind==='text'?'TXT':state.kind.toUpperCase())+' / '+tr('FICHIER D’EXEMPLE','EXAMPLE FILE');
 $('#sample-path').textContent=`@/repo/atlas/…/${name}`;
 $('#copy-content').disabled=!canCopy;
 $('#copy-content-label').textContent=state.kind==='image'?tr('Copier l’image','Copy image'):state.kind==='text'?tr('Copier le texte','Copy text'):tr('Non applicable','Not applicable');
 $('#sample-description').textContent=state.kind==='image'?tr('Une image, un fichier et un chemin.','An image, a file and a path.'):state.kind==='text'?tr('Une note prête à être recollée.','A note ready to paste again.'):tr('Un document à déposer et à récupérer.','A document to drop off and pick up.');
 const thumb=$('#sample-thumbnail');thumb.replaceChildren();
 if(state.kind==='image'){const img=document.createElement('img');img.src='data:image/png;base64,'+window.PB_EXAMPLES['atlas.png'].base64;img.alt=tr('Document Atlas fictif','Fictional Atlas document');thumb.append(img);}
 else {const strong=document.createElement('strong');strong.textContent=state.kind==='text'?'TXT':state.kind.toUpperCase();thumb.append(strong);}
 $('#sample-footnote').textContent=canCopy?tr('La recopie dépend du format et des permissions du navigateur. Les chemins de cette démo sont fictifs.','Copying depends on the format and browser permissions. Paths in this demo are fictional.'):tr('Le PDF et l’Excel restent des fichiers : Pasteberth ne les édite pas et ne les recopie pas comme du texte.','PDF and Excel remain files: Pasteberth does not edit them or copy them as text.');
}
$$('[data-kind]').forEach(b=>b.addEventListener('click',()=>{state.kind=b.dataset.kind;renderSample();}));
$('#copy-reference').addEventListener('click',()=>copyText('@/repo/atlas/ignoredbygit/exchange/'+sampleFiles[state.kind]));
$('#download-sample').addEventListener('click',()=>{const n=sampleFiles[state.kind];save(fileBlob(n),n);});
$('#copy-content').addEventListener('click',async()=>{
 if(state.kind==='text'){await copyText(new TextDecoder().decode(bytes('notes.txt')));return;}
 if(state.kind!=='image')return;
 try{if(!window.ClipboardItem||!navigator.clipboard?.write)throw new Error('Unavailable');await navigator.clipboard.write([new ClipboardItem({'image/png':fileBlob('atlas.png')})]);notify(tr('Image copiée dans le presse-papiers.','Image copied to the clipboard.'));}
 catch{notify(tr('Copie d’image indisponible ici. Utilisez « Son fichier » pour récupérer le PNG.','Image copying is unavailable here. Use “The file” to download the PNG.'));}
});
const profiles={
 personal:{label:['POUR VOTRE PROCHAIN CONTEXTE','FOR YOUR NEXT CONTEXT'],title:['Collez maintenant.<br>Recopiez plus tard.','Paste now.<br>Copy again later.'],copy:['Une capture, une note, un document : gardez-les dans une zone, sans dépendre du dernier élément de votre presse-papiers.','A screenshot, a note, a document: keep them in a zone instead of relying on the last item in your clipboard.'],link:['Un contenu, plusieurs sorties','One item, several ways forward'],href:'#ways',a:['Coller','Paste'],as:['Maintenant','Now'],b:['Recopier','Copy again'],bs:['Plus tard','Later'],zone:['Votre zone','Your zone'],ia:'copy',ib:'copy'},
 documents:{label:['SANS IMPOSER QUI UTILISE QUOI','NO ASSIGNED SIDES'],title:['Des documents entrent.<br>Le travail ressort.','Documents come in.<br>Work comes back.'],copy:['Un PDF, un Excel, une archive : déposez les sources, puis récupérez les résultats. Entre personnes ou outils, l’accès web ne nécessite pas de compte SSH.','A PDF, a spreadsheet, an archive: drop off the sources, then pick up the results. Between people or tools, Web access needs no SSH account.'],link:['Essayer avec des fichiers','Try it with files'],href:'#interface',a:['Déposer','Drop off'],as:['PDF · XLSX','PDF · XLSX'],b:['Récupérer','Pick up'],bs:['Rapport · ZIP','Report · ZIP'],zone:['Zone du projet','Project zone'],ia:'file',ib:'download'},
 tools:{label:['UNE SORTIE COMMUNE À VOS OUTILS','ONE SHARED OUTPUT FOR YOUR TOOLS'],title:['Vos scripts produisent.<br>Pasteberth présente.','Your scripts produce.<br>Pasteberth presents.'],copy:['Un benchmark, un agent, un traitement de données : publiez des fichiers ou du contenu sans développer une interface de récupération pour chaque outil.','A benchmark, an agent, a data pipeline: publish files or content without building a retrieval interface for every tool.'],link:['Les chemins de publication','Publication methods'],href:'#filesystem',a:['Produire','Produce'],as:['CLI · MCP','CLI · MCP'],b:['Reprendre','Pick up'],bs:['Fichier · contenu','File · content'],zone:['Vrais fichiers','Real files'],ia:'terminal',ib:'download'},
 projects:{label:['UN ESPACE PAR CONTEXTE','A SPACE FOR EVERY CONTEXT'],title:['Créez le projet.<br>Retrouvez sa zone.','Create the project.<br>Find its zone.'],copy:['Une arborescence répétable, une règle de découverte. Vos espaces d’échange suivent vos projets, sans nouvelle déclaration manuelle dans Pasteberth.','A repeatable directory layout, one discovery rule. Your exchange spaces follow your projects, with no new manual declaration in Pasteberth.'],link:['Générer votre configuration','Generate your configuration'],href:'#projects',a:['Créer','Create'],as:['Répertoire','Directory'],b:['Retrouver','Discover'],bs:['Bon groupe','Right group'],zone:['Découverte','Discovery'],ia:'folder',ib:'grid'}
};
function renderProfile(){
 const p=profiles[state.profile],lang=state.lang==='fr'?0:1;
 setTabs('[data-profile]','profile',state.profile);$('#audience-panel').setAttribute('aria-labelledby','use-'+state.profile);
 $('#audience-label').textContent=p.label[lang];$('#audience-title').innerHTML=p.title[lang];$('#audience-copy').textContent=p.copy[lang];$('#audience-link-label').textContent=p.link[lang];$('#audience-link').href=p.href;
 for(const [id,prop] of [['flow-a','a'],['flow-a-small','as'],['flow-b','b'],['flow-b-small','bs'],['flow-zone','zone']])$('#'+id).textContent=p[prop][lang];
 $('#flow-icon-a').innerHTML=`<svg class="icon" aria-hidden="true"><use href="#i-${p.ia}"></use></svg>`;$('#flow-icon-b').innerHTML=`<svg class="icon" aria-hidden="true"><use href="#i-${p.ib}"></use></svg>`;
}
$$('[data-profile]').forEach(b=>b.addEventListener('click',()=>{state.profile=b.dataset.profile;renderProfile();}));
function renderMethod(){
 setTabs('[data-method]','method',state.method);$('#method-panel').setAttribute('aria-labelledby','method-'+state.method);
 const snippets={
 local:'ZONE=/repo/atlas/ignoredbygit/exchange\nDEST="$ZONE/report-new.pdf"\n[ ! -e "$DEST" ] && [ ! -L "$DEST" ] &&\n[ ! -e "$DEST.json" ] && [ ! -L "$DEST.json" ] &&\ncp -T --update=none-fail -- report.pdf "$DEST" &&\n  pasteberth register "$DEST"',
 cli:'pasteberth drop \\\n  /repo/atlas/ignoredbygit/exchange \\\n  report.pdf atlas.xlsx',
 mcp:JSON.stringify({zone:'atlas-ignoredbygit-exchange',items:[{content:tr('Résultat de démonstration.','Demo result.'),filename:'notes.txt'}]},null,2)};
 $('#method-code').textContent=snippets[state.method];$('#method-language').textContent=state.method==='mcp'?tr('ARGUMENTS DE L’OUTIL drop','drop TOOL ARGUMENTS'):'SHELL';
 const notes={local:['Choisissez des noms de fichier et de sidecar inutilisés. Requiert GNU cp avec -T et --update=none-fail. Les tests refusent aussi les répertoires et liens, mais ne réservent pas les noms contre un autre producteur. Sans écritures concurrentes ; sinon utilisez drop. register ne remplace pas les données. Pour un remplacement géré, utilisez drop --replace.','Choose unused data and sidecar names. Requires GNU cp with -T and --update=none-fail. The checks also reject directories and links, but do not reserve names against another writer. Use without concurrent writers; otherwise use drop. register does not replace data. Use drop --replace for managed replacement.'],cli:['drop contacte toujours le daemon. Le staging direct peut éviter le transfert HTTP des octets en local ; sinon, l’upload passe par HTTP.','drop always contacts the daemon. Direct staging can avoid an HTTP transfer of the bytes locally; otherwise it uses HTTP upload.'],mcp:['L’adaptateur MCP expose drop : chemins locaux, texte ou base64. Il utilise HTTP et nécessite de connaître la zone cible.','The MCP adapter exposes drop: local paths, text or base64. It uses HTTP and requires the target zone to be known.']};
 $('#method-note').textContent=notes[state.method][state.lang==='fr'?0:1];
}
$$('[data-method]').forEach(b=>b.addEventListener('click',()=>{state.method=b.dataset.method;renderMethod();}));
$$('.copy-code').forEach(b=>b.addEventListener('click',()=>copyText($('#'+b.dataset.copyTarget).textContent)));
function renderStart(){
 setTabs('[data-start]','start',state.start);$('#start-code-panel').setAttribute('aria-labelledby','start-'+state.start);
 $('#start-code').textContent=state.start==='local'?'git clone https://github.com/Fade78/pasteberth.git\ncd pasteberth\n./PasteBerth/pasteberth':tr('# Depuis la racine du dépôt\n','# From the repository root\n')+'./PasteBerth/pasteberth --generate-config\n'+tr('# Éditer ~/.config/pasteberth/config.toml\n','# Edit ~/.config/pasteberth/config.toml\n')+'./PasteBerth/pasteberth passwd\n./PasteBerth/pasteberth audit\n./PasteBerth/pasteberth';
 $('#start-instruction').innerHTML=state.start==='local'?tr('Sans configuration existante, ouvrez <code>http://127.0.0.1:8765</code>. Le mode minimal est local et sans authentification : ne l’exposez pas à un réseau ou à un proxy.','With no existing configuration, open <code>http://127.0.0.1:8765</code>. The minimal mode is local and unauthenticated: do not expose it to a network or proxy.'):tr('Définissez les zones, les accès et le mot de passe avant l’usage partagé. Pour HTTPS, TLS ou un reverse proxy, suivez le guide de déploiement.','Set up zones, access and a password before shared use. For HTTPS, TLS or a reverse proxy, follow the deployment guide.');
}
$$('[data-start]').forEach(b=>b.addEventListener('click',()=>{state.start=b.dataset.start;renderStart();}));
function validateConfig(){
 const root=$('#root-path').value.trim(),pattern=$('#path-pattern').value.trim(),name=$('#project-name').value.trim();
 let bad='',field='';
 if(!root.startsWith('/')||/[\\\x00-\x1f\x7f]/.test(root)||root.split('/').some(c=>c==='.'||c==='..')||root.length>2000){bad=tr('Utilisez un chemin absolu POSIX, sans « . », « .. », antislash ou caractère de contrôle.','Use an absolute POSIX path without “.”, “..”, backslashes or control characters.');field='root-path';}
 const parts=pattern.split('/');
 if(!bad&&(parts.length<2||parts[0]!=='{project}'||parts.slice(1).some(p=>! /^[a-zA-Z0-9_-]+$/.test(p)))){bad=tr('Commencez par {project}/, puis des répertoires composés de lettres, chiffres, tirets ou underscores. Exemple : {project}/work/exchange.','Start with {project}/, followed by directories using letters, numbers, hyphens or underscores. Example: {project}/work/exchange.');field='path-pattern';}
 const id=[name,...parts.slice(1)].join('-').toLowerCase();
 if(!bad&&(!name||name.includes('/')||! /^[a-z0-9][a-z0-9_-]{0,63}$/.test(id))){bad=tr('L’ID résultant doit comporter 1 à 64 caractères : lettres a–z, chiffres, tirets et underscores, avec une lettre ou un chiffre au début. Les points ne sont pas pris en charge.','The resulting ID must have 1–64 characters: a–z, digits, hyphens and underscores, starting with a letter or digit. Dots are not supported.');field='project-name';}
 for(const f of ['root-path','path-pattern','project-name'])$('#'+f).setAttribute('aria-invalid',String(f===field));
 $('#config-error').hidden=!bad;$('#config-error').textContent=bad;$('#config-result').hidden=!!bad;$('#copy-config').disabled=!!bad;$('#download-config').disabled=!!bad;
 if(bad){$('#generated-config').textContent=tr('# Corrigez les champs pour générer un extrait.','# Fix the inputs to generate a snippet.');return false;}
 const base=root==='/'?'':root.replace(/\/+$/,''),suffix=parts.slice(1).join('/');
 $('#generated-id').textContent=id;$('#generated-path').textContent=`${base}/${name}/${suffix}`;
 const q=JSON.stringify;
  const code=[tr('# Extrait de documentation : à ajouter une seule fois.','# Documentation snippet: append once.'),'[[zone_collection]]','id = "@projects"',`base_directory = ${q(base||'/')}`,`pattern = '^[^/]+/${suffix}$'`,`max_depth = ${parts.length}`,'label_mode = "first-directory"',tr('# Choix pour cet exemple ; défaut du runtime : 10.','# Example choice; runtime default: 10.'),'retain = 100','reference_prefix = "@"','','[[groups]]',`name = ${q(tr('Projets','Projects'))}`,'selection = "pattern"',"pattern = ['^@projects$']",'layout = "tab"'].join('\n');
 $('#generated-config').textContent=code;return true;
}
$('#config-form').addEventListener('submit',e=>e.preventDefault());
$$('#config-form input').forEach(i=>i.addEventListener('input',validateConfig));
$('#copy-config').addEventListener('click',()=>{if(validateConfig())copyText($('#generated-config').textContent);});
$('#download-config').addEventListener('click',()=>{if(validateConfig())save(new Blob([$('#generated-config').textContent+'\n'],{type:'text/plain;charset=utf-8'}),'pasteberth-zones.toml');});
function projectLabel(){
 $('#add-project-label').textContent=state.projectAdded?tr('Rejouer la découverte','Replay discovery'):tr('Créer un projet d’exemple','Create an example project');
 $('#zone-count').textContent=(state.projectAdded?4:3)+' zones';
 if(state.projectAdded)$('#discovery-status').textContent=tr('nova a rejoint le groupe Projets.','nova joined the Projects group.');
}
$('#add-project').addEventListener('click',()=>{
 clearTimeout(discoveryTimer);
 if(state.projectAdded){state.projectAdded=false;$('#new-tree-line').hidden=true;$('#new-zone-pill').hidden=true;$('#discovery-status').textContent='';projectLabel();return;}
 $('#new-tree-line').hidden=false;$('#discovery-status').textContent=tr('Répertoire créé. Scan simulé…','Directory created. Simulated scan…');$('#add-project').disabled=true;
 discoveryTimer=setTimeout(()=>{state.projectAdded=true;$('#new-zone-pill').hidden=false;$('#add-project').disabled=false;projectLabel();},800);
});
function sendScene(){
 $$('[data-scene]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.scene===state.scene)));
 $('#demo-frame').contentWindow?.postMessage({type:'pb-demo',scene:state.scene},'*');
}
function launchDemo(){
 if(!state.demoStarted){const frame=$('#demo-frame');if(window.PB_DEMO_HTML)frame.srcdoc=window.PB_DEMO_HTML;else frame.src='demo.html';frame.hidden=false;$('#demo-poster').hidden=true;$('#demo-launch').hidden=true;state.demoStarted=true;}
 sendScene();
}
$('#launch-demo').addEventListener('click',launchDemo);
$$('[data-scene]').forEach(b=>b.addEventListener('click',()=>{state.scene=b.dataset.scene;launchDemo();sendScene();}));
window.addEventListener('message',e=>{if(e.source!==$('#demo-frame').contentWindow)return;if(e.data?.type==='pb-demo-ready')sendScene();});
$('#reset-demo').addEventListener('click',()=>{
 if(!state.demoStarted){state.scene='focus';sendScene();return;}
 const frame=$('#demo-frame');state.scene='focus';
 if(window.PB_DEMO_HTML)frame.srcdoc=window.PB_DEMO_HTML;else frame.src='demo.html';
 sendScene();notify(tr('Démo réinitialisée. Les fichiers ajoutés ont été retirés.','Demo reset. Added files have been removed.'));
});
const exit=document.createElement('button');exit.type='button';exit.className='exit-fullscreen';exit.textContent='×';exit.setAttribute('aria-label','Exit full screen');exit.dataset.ariaLabelFr='Quitter le plein écran';exit.dataset.ariaLabelEn='Exit full screen';$('#demo-container').prepend(exit);exit.addEventListener('click',()=>document.exitFullscreen?.());
$('#expand-demo').addEventListener('click',async()=>{launchDemo();try{if(!$('#demo-container').requestFullscreen)throw new Error('Unavailable');await $('#demo-container').requestFullscreen();}catch{notify(tr('Le plein écran n’est pas disponible dans ce navigateur. La démo reste utilisable ici.','Full screen is unavailable in this browser. You can still use the demo here.'));}});
function setLanguage(lang, persist=false){
 state.lang=lang==='fr'?'fr':'en';document.documentElement.lang=state.lang;
 $$('[data-lang]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.lang===state.lang)));
 document.title=tr('Pasteberth — Vos fichiers, prêts pour la suite.','Pasteberth — Your files, ready for what’s next.');
 $('meta[name=description]').content=tr('Collez, déposez et retrouvez vos fichiers entre navigateur, presse-papiers et filesystem.','Paste, drop and pick up files across browsers, clipboards and filesystems.');
 for(const attribute of ['alt','aria-label','title']) {
   $$('[data-'+attribute+'-en]').forEach(el=>el.setAttribute(attribute,el.getAttribute('data-'+attribute+'-'+state.lang)));
 }
 if(persist){
   document.documentElement.dataset.languageSource='choice';
   try{localStorage.setItem('pasteberth-site-language-choice',state.lang);}catch{}
 }
 renderSample();renderProfile();renderMethod();renderStart();validateConfig();projectLabel();
}
$$('[data-lang]').forEach(b=>b.addEventListener('click',()=>setLanguage(b.dataset.lang,true)));
setLanguage(document.documentElement.lang);
})();
