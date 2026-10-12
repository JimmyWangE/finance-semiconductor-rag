const $=id=>document.getElementById(id);
const filterKeys=['org','date_from','date_to','as_of','kind'];
const typeNames={text:'正文',table:'表格',chart:'图表转述'};
let active=null,busy=false,listLimit=50,pending=null,selection=0;
const drafts=new Map();
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function requestId(){
  if(window.crypto&&typeof window.crypto.randomUUID==='function')return window.crypto.randomUUID();
  const bytes=new Uint8Array(16);
  if(window.crypto&&typeof window.crypto.getRandomValues==='function')window.crypto.getRandomValues(bytes);
  else for(let index=0;index<bytes.length;index++)bytes[index]=Math.floor(Math.random()*256);
  bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
  const hex=[...bytes].map(value=>value.toString(16).padStart(2,'0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
function errorFrom(response,data){const error=new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data));error.status=response.status;return error}
async function request(path,method='GET',body){
  const options={method};
  if(body!==undefined){options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(body)}
  let r;
  for(let attempt=0;attempt<4;attempt++){
    r=await fetch(path,options);
    if(r.status!==503||attempt===3)break;
    $('status').textContent='当前有其他请求正在生成，正在排队等待…';
    await wait(Math.max(1000,Number(r.headers.get('Retry-After')||3)*1000));
  }
  let data;try{data=await r.json()}catch{data={detail:`请求失败（HTTP ${r.status}）`}}
  if(!r.ok)throw errorFrom(r,data);
  return data;
}
function element(tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el}
function fail(error){$('error').textContent=error.message}
const media=createMediaComposer({status:text=>$('status').textContent=text,error:text=>$('error').textContent=text,
  onAdded:()=>{document.querySelector('input[name=mode][value=ask]').checked=true}});
function setBusy(value){
  busy=value;
  $('query-form').setAttribute('aria-busy',String(value));
  $('submit').replaceChildren(ragIcon(value?'loader-circle':'arrow-up'));
  $('submit').classList.toggle('loading',value);
  for(const id of ['submit','new-conversation','rename-conversation','delete-conversation','query',...filterKeys])$(id).disabled=value||(['rename-conversation','delete-conversation'].includes(id)&&!active);
  media.setDisabled(value);
  for(const input of document.querySelectorAll('input[name=mode]'))input.disabled=value;
  for(const button of document.querySelectorAll('.session'))button.disabled=value;
}
function readFilters(){const filters={};for(const key of filterKeys)if($(key).value)filters[key]=$(key).value;if(active?.filters.doc_ids)filters.doc_ids=active.filters.doc_ids;return filters}
function restoreFilters(filters){for(const key of filterKeys){if(key==='org'&&filters[key]&&![...$('org').options].some(o=>o.value===filters[key])){const o=element('option',filters[key]);o.value=filters[key];$('org').append(o)}$(key).value=filters[key]||''}updateFilterCount()}
function updateFilterCount(){const n=filterKeys.filter(key=>$(key).value).length;$('filter-count').textContent=n?String(n):''}
filterKeys.forEach(key=>$(key).addEventListener('change',updateFilterCount));
function renderWelcome(){
  const welcome=element('div',undefined,'welcome');welcome.id='answer';
  const mark=element('div',undefined,'welcome-icon');mark.append(ragIcon('microchip'));
  welcome.append(mark,element('h2','半导体研报研究台'),element('p','晶圆制造、设备材料、芯片设计与产业趋势','subtitle'));
  const list=element('div',undefined,'suggestions');
  for(const [category,question] of [['晶圆制造','中芯国际2025年第三季度产能利用率是多少？'],['存储周期','DRAM价格变化反映了哪些供需趋势？'],['设备材料','光刻胶在芯片制造中起什么作用？'],['风险观察','东海证券对半导体行业提示了哪些风险？']]){
    const button=element('button',undefined,'suggestion');button.type='button';const text=element('span');text.append(element('small',category),document.createTextNode(question));button.append(text,ragIcon('arrow-up-right'));
    button.onclick=()=>{if(!busy){$('query').value=question;$('query').focus()}};list.append(button);
  }
  welcome.append(list);$('thread').append(welcome);
}
function renderSources(sources){
  $('sources').replaceChildren();$('sources').className='';$('source-count').textContent=sources.length?`(${sources.length})`:'';
  if(!sources.length){const empty=element('div',undefined,'evidence-empty');empty.append(ragIcon('files'),element('h3','暂无引用来源'),element('p','原始研报 · 页码定位 · 图表原文'));$('sources').append(empty);return}
  for(const [index,s] of sources.entries()){
    const item=element('article',undefined,'evidence');item.id=s.source_id||`S${index+1}`;
    item.append(element('div',`${item.id} · ${s.org} · ${s.publish_date} · ${s.page?'第 '+s.page+' 页':'页码未定位'} · ${typeNames[s.kind]}`,'meta'));
    const title=element('a',s.title,'title');title.href=s.pdf_url;title.target='_blank';title.rel='noopener';item.append(title,element('div',s.text,'source-text'));
    if(s.caption_warning)item.append(element('div',s.caption_warning,'meta'));
    for(const [url,label] of [[s.page_image_url,'定位原文'],[s.figure_url,'查看原图']]){
      if(!url)continue;const details=element('details');details.append(element('summary',label));
      const img=element('img');img.alt=s.title+'，第'+s.page+'页';img.loading='lazy';img.src=url;details.append(img);item.append(details);
    }
    $('sources').append(item);
  }
}
function showEvidence(result,number){renderSources(result.sources||result.hits||[]);$('source-count').textContent=`第 ${number} 轮 · ${(result.sources||result.hits||[]).length} 条`}
function renderThread(){
  const thread=$('thread');thread.replaceChildren();
  if(!active?.turns?.length){renderWelcome();renderSources([]);return}
  for(const [index,turn] of active.turns.entries()){
    const result=turn.response;const item=element('article',undefined,'turn');item.dataset.turn=turn.number;
    const meta=element('div',undefined,'turn-meta');const turnLabel=element('span',`第 ${turn.number} 轮`,'meta');turnLabel.prepend(ragIcon('microchip'));meta.append(turnLabel);
    const evidence=element('button','查看依据','quiet');evidence.prepend(ragIcon('quote'));evidence.type='button';evidence.onclick=()=>showEvidence(result,turn.number);meta.append(evidence);
    const answer=element('div',result.answer,'answer');if(index===active.turns.length-1)answer.id='answer';
    item.append(meta);const attachments=renderMessageMedia(result.attachments);if(attachments)item.append(attachments);item.append(element('h3',turn.query,'question'),answer);
    if(result.resolved_query&&result.resolved_query!==turn.query){const details=element('details');details.append(element('summary','本轮检索问题'),element('div',result.resolved_query,'resolved'));item.append(details)}
    const quotes=element('details',undefined,'quote-details');quotes.append(element('summary','引用摘录'));if(index===active.turns.length-1)quotes.id='quotes';
    for(const claim of result.claims||[])for(const cite of claim.citations){
      const block=element('blockquote',cite.quote+' ');const link=element('a','['+cite.source_id+']');link.href='#'+cite.source_id;
      link.onclick=()=>showEvidence(result,turn.number);block.append(link);quotes.append(block);
    }
    if((result.claims||[]).length)item.append(quotes);thread.append(item);
  }
  showEvidence(active.turns.at(-1).response,active.turns.at(-1).number);thread.scrollTop=thread.scrollHeight;
}
async function refreshList(){
  const data={conversations:[],total:0};
  for(let offset=0;offset<listLimit;offset+=100){const batch=await request(`/conversations?limit=${Math.min(listLimit-offset,100)}&offset=${offset}`);data.conversations.push(...batch.conversations);data.total=batch.total;if(data.conversations.length>=data.total)break}
  $('conversations').replaceChildren();
  $('session-total').textContent=data.total||'';
  if(!data.conversations.length){$('conversations').append(element('p','暂无研究会话','history-empty'))}
  for(const conversation of data.conversations){
    const button=element('button',undefined,'session'+(active?.id===conversation.id?' active':''));button.append(ragIcon('message-square'),element('span',conversation.title,'session-title'));button.dataset.id=conversation.id;button.disabled=busy;
    if(active?.id===conversation.id)button.setAttribute('aria-current','true');
    button.append(element('small',new Date(conversation.updated_at).toLocaleString('zh-CN')));button.onclick=()=>selectConversation(conversation.id).catch(fail);$('conversations').append(button);
  }
  $('more-conversations').hidden=data.total<=data.conversations.length;
}
async function selectConversation(id){
  if(busy)return;const ticket=++selection;if(active)drafts.set(active.id,$('query').value);
  setBusy(true);
  let conversation;try{conversation=await request('/conversations/'+id)}finally{setBusy(false)}
  if(ticket!==selection)return;
  active=conversation;pending=null;localStorage.setItem('rag_active_conversation',id);$('conversation-title').textContent=active.title;
  document.body.classList.remove('history-open');$('toggle-history').setAttribute('aria-expanded','false');
  $('query').value=drafts.get(id)||'';media.clear();restoreFilters(active.filters);$('error').textContent='';$('status').textContent='';renderThread();setBusy(false);await refreshList();
}
async function createConversation(){
  const conversation=await request('/conversations','POST',{});active={...conversation,turns:[]};pending=null;
  localStorage.setItem('rag_active_conversation',active.id);$('conversation-title').textContent=active.title;renderThread();await refreshList();
}
$('new-conversation').onclick=async()=>{
  if(busy)return;if(active)drafts.set(active.id,$('query').value);
  setBusy(true);
  try{await createConversation();restoreFilters({});$('query').value='';media.clear();$('status').textContent='';$('error').textContent='';$('query').focus()}catch(error){fail(error)}finally{setBusy(false)}
};
$('more-conversations').onclick=()=>{listLimit+=50;refreshList().catch(fail)};
$('query-form').addEventListener('submit',async event=>{
  event.preventDefault();if(busy||!media.ready())return;const query=$('query').value.trim();const attachments=media.refs();if(!query&&!attachments.length)return;
  const filters=readFilters();const mode=document.querySelector('input[name=mode]:checked').value;if(mode==='search'&&attachments.length){fail(new Error('图片和视频请使用研究问答模式'));return}
  setBusy(true);$('error').textContent='';$('status').textContent=mode==='ask'?'理解问题与检索中…':'检索中…';
  document.querySelector('.filter-panel').open=false;
  const pendingTurn=element('div',undefined,'pending-turn');pendingTurn.append(ragIcon('loader-circle'),element('span',query||'正在分析附件'));$('thread').append(pendingTurn);$('thread').scrollTop=$('thread').scrollHeight;
  try{
    if(mode==='search'){const data=await request('/search','POST',{query,filters,top_k:6});renderSources(data.hits);$('status').textContent=`检索完成 · ${(data.latency_ms/1000).toFixed(2)} 秒`;return}
    if(!active)await createConversation();
    const signature=JSON.stringify({query,attachments,filters,top_k:6,conversation_id:active.id});
    if(!pending||pending.signature!==signature)pending={signature,body:{query,attachments,filters,top_k:6,conversation_id:active.id,request_id:requestId(),expected_revision:active.revision}};
    const data=await request('/ask','POST',pending.body);active=await request('/conversations/'+active.id);pending=null;
    $('conversation-title').textContent=active.title;drafts.delete(active.id);$('query').value='';media.clear();renderThread();
    $('status').textContent=`${data.status==='needs_clarification'?'待补充问题':'已完成'} · ${(data.latency_ms/1000).toFixed(2)} 秒`;await refreshList();
  }catch(error){
    fail(error);$('status').textContent='发送失败，问题已保留';
    if(error.status===409&&active){pending=null;try{active=await request('/conversations/'+active.id);restoreFilters(active.filters);renderThread();await refreshList()}catch{}}
  }finally{pendingTurn.remove();setBusy(false)}
});
$('rename-conversation').onclick=()=>{if(!active||busy)return;$('rename-title').value=active.title;$('rename-dialog').showModal();$('rename-title').focus()};
$('cancel-rename').onclick=()=>$('rename-dialog').close();
$('rename-form').onsubmit=async event=>{
  event.preventDefault();try{const data=await request('/conversations/'+active.id,'PATCH',{title:$('rename-title').value.trim(),expected_revision:active.revision});active={...active,...data};pending=null;$('conversation-title').textContent=active.title;$('rename-dialog').close();await refreshList()}catch(error){$('rename-dialog').close();fail(error)}
};
$('delete-conversation').onclick=async()=>{
  if(!active||busy||!confirm(`删除会话“${active.title}”？`))return;
  try{await request(`/conversations/${active.id}?expected_revision=${active.revision}`,'DELETE');drafts.delete(active.id);active=null;pending=null;localStorage.removeItem('rag_active_conversation');$('conversation-title').textContent='新会话';$('query').value='';media.clear();restoreFilters({});renderThread();setBusy(false);await refreshList()}catch(error){fail(error)}
};
async function initialize(){
  setBusy(true);
  try{
    try{const health=await request('/health');$('health').textContent=health.status==='ok'?'服务在线':'生成服务异常';$('health').dataset.state=health.status==='ok'?'online':'offline'}catch{$('health').textContent='生成服务暂不可用';$('health').dataset.state='offline'}
    const stats=await request('/stats');const r=stats.report;$('coverage').textContent=`${r.documents} 份研报 · ${r.chunks.toLocaleString()} 条证据 · ${r.date_min} 至 ${r.date_max}`;
    const docs=await request('/documents?limit=1');for(const org of Object.keys(docs.organizations).sort((a,b)=>a.localeCompare(b,'zh-CN'))){const option=element('option',org);option.value=org;$('org').append(option)}
    setBusy(false);const saved=localStorage.getItem('rag_active_conversation');if(saved){try{await selectConversation(saved)}catch(error){if(error.status===404)localStorage.removeItem('rag_active_conversation');else throw error}}
    await refreshList();
  }catch(error){fail(error);$('health').textContent='连接异常'}finally{setBusy(false)}
}
initialize();
renderThread();
