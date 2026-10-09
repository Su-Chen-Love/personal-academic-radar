import {page,css,client} from './ui.js';
const h={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY'};
const json=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{...h,'Content-Type':'application/json; charset=utf-8'}});
const fail=(message,status=400)=>{throw Object.assign(new Error(message),{status});};
const hash=async text=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text)))).map(n=>n.toString(16).padStart(2,'0')).join('');
const owner=(r,e)=>Boolean(e.OWNER_EMAIL&&r.headers.get('oai-authenticated-user-email')?.toLowerCase()===e.OWNER_EMAIL.toLowerCase());
async function body(r){if(!(r.headers.get('content-type')||'').includes('application/json'))fail('需要 JSON 请求',415);const t=await r.text();if(t.length>2000000)fail('请求过大',413);try{return JSON.parse(t);}catch{fail('JSON 格式无效');}}
const gid=v=>{if(!/^[a-f0-9]{64}$/.test(v||''))fail('快照编号无效');return v;};
async function active(db){return(await db.prepare("SELECT value FROM pointers WHERE key='active'").first())?.value;}
async function record(db,g,k,key){const r=await db.prepare('SELECT data FROM records WHERE generation=? AND kind=? AND key=?').bind(g,k,key).first();return r?JSON.parse(r.data):null;}
async function all(db,g,k){return(await db.prepare('SELECT data FROM records WHERE generation=? AND kind=? ORDER BY key').bind(g,k).all()).results.map(r=>JSON.parse(r.data));}
async function feedback(db,g){const m=new Map((await all(db,g,'feedback')).map(r=>[r.identity,r]));for(const r of(await db.prepare('SELECT * FROM cloud_feedback ORDER BY seq').all()).results){const p=m.get(r.identity);if(!p||Date.parse(r.updated_at)>Date.parse(p.updated_at))m.set(r.identity,r);}return m;}
async function sync(r,e,u){
 if(!e.SYNC_TOKEN||await hash(r.headers.get('Authorization')?.replace(/^Bearer /,'')||'')!==await hash(e.SYNC_TOKEN))fail('同步凭据无效',401);
 const db=e.DB;
 if(u.pathname==='/api/sync/feedback'&&r.method==='GET'){const n=Number(u.searchParams.get('after')||0);if(!Number.isSafeInteger(n)||n<0)fail('游标无效');const a=(await db.prepare('SELECT * FROM cloud_feedback WHERE seq>? ORDER BY seq LIMIT 500').bind(n).all()).results;return json({events:a,cursor:a.length?a.at(-1).seq:n,more:a.length===500});}
 if(r.method!=='POST')fail('不支持此方法',405);const d=await body(r),g=gid(d.generation);
 if(u.pathname==='/api/sync/begin'){
  if(!Number.isSafeInteger(d.count)||d.count<1||d.count>100000)fail('记录数量无效');
  await db.prepare('INSERT INTO generations(id,count,created_at) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING').bind(g,d.count,new Date().toISOString()).run();
  const existing=await db.prepare('SELECT count,status FROM generations WHERE id=?').bind(g).first();if(existing.count!==d.count)fail('快照不一致',409);
  if(await active(db)===g)return json({generation:g,active:true});
  if(d.manifest){
   if(!Array.isArray(d.manifest)||d.manifest.length!==d.count)fail('快照清单无效');
   const keys=new Set();for(const m of d.manifest){if(!['paper','history','feedback','private','meta'].includes(m.kind)||typeof m.key!=='string'||m.key.length>1000||!/^[a-f0-9]{64}$/.test(m.checksum||''))fail('快照清单无效');const key=m.kind+':'+m.key;if(keys.has(key))fail('重复记录');keys.add(key);}
   const base=await active(db);
   if(base&&existing.status==='staging')await db.prepare(`INSERT OR IGNORE INTO records(generation,kind,key,data,checksum)
     SELECT ?,r.kind,r.key,r.data,r.checksum FROM json_each(?) m JOIN records r
     ON r.generation=? AND r.kind=json_extract(m.value,'$.kind') AND r.key=json_extract(m.value,'$.key')
     AND r.checksum=json_extract(m.value,'$.checksum')`).bind(g,JSON.stringify(d.manifest),base).run();
   const ready=new Set((await db.prepare('SELECT kind,key FROM records WHERE generation=?').bind(g).all()).results.map(r=>r.kind+':'+r.key));
   return json({generation:g,active:false,missing:d.manifest.filter(m=>!ready.has(m.kind+':'+m.key)).map(m=>({kind:m.kind,key:m.key}))});
  }
  return json({generation:g,active:false});
 }
 const generation=await db.prepare('SELECT * FROM generations WHERE id=?').bind(g).first();if(!generation)fail('请先建立快照');
 if(u.pathname==='/api/sync/chunk'){
  if(!Array.isArray(d.records)||!d.records.length||d.records.length>40)fail('批次无效');const statements=[];
  for(const row of d.records){if(!['paper','history','feedback','private','meta'].includes(row.kind)||typeof row.key!=='string'||row.key.length>1000||typeof row.data!=='string'||row.data.length>1000000)fail('快照记录无效');if(await hash(row.data)!==row.checksum)fail('记录校验失败');const p=JSON.parse(row.data);if(row.kind==='paper'&&p.identity!==row.key)fail('身份不一致');statements.push(db.prepare('INSERT INTO records(generation,kind,key,data,checksum) VALUES(?,?,?,?,?) ON CONFLICT(generation,kind,key) DO NOTHING').bind(g,row.kind,row.key,row.data,row.checksum));}
  if(generation.status==='staging')await db.batch(statements);return json({accepted:d.records.length});
 }
 if(u.pathname==='/api/sync/finish'){
  const rows=(await db.prepare('SELECT kind,key,checksum FROM records WHERE generation=? ORDER BY kind,key').bind(g).all()).results;
  if(rows.length!==generation.count||await hash(rows.map(r=>`${r.kind}:${r.key}:${r.checksum}`).join('\n'))!==g)fail('快照尚不完整，原数据仍可访问',409);
  await db.batch([db.prepare("UPDATE generations SET status='complete' WHERE id=?").bind(g),db.prepare("INSERT INTO pointers(key,value) VALUES('active',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(g),db.prepare("INSERT INTO pointers(key,value) VALUES('synced_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(new Date().toISOString())]);
  const keep=(await db.prepare("SELECT id FROM generations WHERE status='complete' ORDER BY created_at DESC LIMIT 2").all()).results.map(r=>r.id);
  for(const s of(await db.prepare("SELECT id FROM generations WHERE id<>? AND (status='complete' OR created_at<?)").bind(g,new Date(Date.now()-7*86400000).toISOString()).all()).results){if(keep.includes(s.id))continue;await db.batch([db.prepare('DELETE FROM records WHERE generation=?').bind(s.id),db.prepare('DELETE FROM generations WHERE id=?').bind(s.id)]);}
  return json({generation:g,count:rows.length});
 }
 fail('同步操作不存在',404);
}
async function view(r,e,u){
 const db=e.DB,g=await active(db),own=owner(r,e);if(!g)return json({ready:false,owner:own,message:'数据库正在首次同步，请稍后刷新。'},503);
 const meta=await record(db,g,'meta','overview'),result={ready:true,owner:own,overview:meta,generation:g,synced_at:(await db.prepare("SELECT value FROM pointers WHERE key='synced_at'").first())?.value};
 const v=u.searchParams.get('view')||'today';if(['profile','feedback','status'].includes(v)&&!own)fail('请使用站点所有者账号登录',401);
 if(v==='profile'){result.profile=await record(db,g,'private','profile');return json(result);}
 if(v==='sources'||v==='status'){if(!own)result.overview.sources=meta.sources.map(s=>({...s,health:{status:s.health?.status,last_success_at:s.health?.last_success_at}}));return json(result);}
 let papers=await all(db,g,'paper');
 if(v==='today'){const today=new Date().toLocaleDateString('en-CA',{timeZone:meta.timezone||'Asia/Shanghai'}),day=u.searchParams.get('day')||today;let history=await record(db,g,'history',day===today?'current':day);if(day===today&&history?.day!==today)history=null;const index=new Map(papers.map(p=>[p.identity,p]));result.day=day;result.batches=history?.batches||0;result.incomplete=history?.incomplete||false;papers=(history?.papers||[]).filter(p=>index.has(p.identity)).map(p=>({...index.get(p.identity),...p}));}
 else if(v==='library')papers=papers.filter(p=>p.eligibility_status==='eligible'&&p.score>=(meta.threshold??.70));else if(v!=='feedback')fail('页面不存在',404);
 const f=own?await feedback(db,g):new Map();
 const publicFields=['identity','doi','title','abstract','venue','published','published_precision','url','authors_json','abstract_source','abstract_source_url','abstract_retrieved_at','eligibility_status','publication_type','score','reasons','confidence','rubric_version','needs_rescreen'];
 result.papers=papers.map(p=>own?{...p,...(f.has(p.identity)?{feedback:f.get(p.identity)}:{})}:Object.fromEntries(publicFields.filter(k=>k in p).map(k=>[k,p[k]])));
 if(v==='feedback')result.papers=result.papers.filter(p=>p.feedback);return json(result);
}
async function saveFeedback(r,e,u){
 if(!owner(r,e))fail('请使用站点所有者账号登录',401);if(r.headers.get('Origin')!==u.origin)fail('请求来源无效',403);
 const d=await body(r),g=await active(e.DB);if(!g||!await record(e.DB,g,'paper',d.identity))fail('论文不存在',404);
 if(![null,'interested','not_interested'].includes(d.interest)||![0,1].includes(d.favorite)||!['unread','read','read_later'].includes(d.reading_status))fail('反馈格式无效');const reason=String(d.reason||'').trim();if(reason.length>3000||d.interest&&!reason)fail('请填写具体偏好理由');const stamp=new Date().toISOString();
 await e.DB.prepare('INSERT INTO cloud_feedback(identity,interest,reason,favorite,reading_status,updated_at) VALUES(?,?,?,?,?,?)').bind(d.identity,d.interest,reason||null,d.favorite,d.reading_status,stamp).run();return json({saved:true,updated_at:stamp});
}
export default {async fetch(r,e){const u=new URL(r.url);try{
 if(u.pathname==='/app.css')return new Response(css,{headers:{...h,'Content-Type':'text/css; charset=utf-8'}});
 if(u.pathname==='/app.js')return new Response(client,{headers:{...h,'Content-Type':'text/javascript; charset=utf-8'}});
 if(u.pathname==='/healthz')return json({ok:!!e.DB,ready:!!(e.DB&&await active(e.DB))});
 if(!e.DB&&u.pathname.startsWith('/api/'))fail('云端数据库暂时不可用',503);
 if(u.pathname.startsWith('/api/sync/'))return await sync(r,e,u);
 if(u.pathname==='/api/view'&&r.method==='GET')return await view(r,e,u);
 if(u.pathname==='/api/feedback'&&r.method==='POST')return await saveFeedback(r,e,u);
 if(!['/','/library','/sources','/profile','/feedback','/status'].includes(u.pathname))fail('页面不存在',404);
 return new Response(page,{headers:{...h,'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"}});
 }catch(error){if(!error.status)console.error('radar storage error',error.name);return json({error:error.status?error.message:'数据暂时无法读取或保存，请稍后重试'},error.status||503);}}};
