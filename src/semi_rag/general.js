const $=id=>document.getElementById(id);
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
  let response;
  for(let attempt=0;attempt<4;attempt++){
    response=await fetch(path,options);
    if(response.status!==503||attempt===3)break;
    $('status').textContent='当前有其他请求正在生成，正在排队等待…';
    await wait(Math.max(1000,Number(response.headers.get('Retry-After')||3)*1000));
  }
  let data;try{data=await response.json()}catch{data={detail:`请求失败（HTTP ${response.status}）`}}
  if(!response.ok)throw errorFrom(response,data);
  return data;
}
async function streamRequest(path,body,onDelta){
  let response;
  for(let attempt=0;attempt<4;attempt++){
    response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(response.status!==503||attempt===3)break;
    $('status').textContent='当前有其他请求正在生成，正在排队等待…';
    await wait(Math.max(1000,Number(response.headers.get('Retry-After')||3)*1000));
  }
  if(!response.ok){let data;try{data=await response.json()}catch{data={detail:`请求失败（HTTP ${response.status}）`}}throw errorFrom(response,data)}
  if(!response.body)throw new Error('浏览器不支持流式响应');
  const reader=response.body.getReader();const decoder=new TextDecoder();let buffer='',completed=null;
  while(true){
    const {value,done}=await reader.read();buffer+=decoder.decode(value||new Uint8Array(),{stream:!done});
    const lines=buffer.split('\n');buffer=lines.pop();
    for(const line of lines){
      if(!line.trim())continue;const event=JSON.parse(line);
      if(event.type==='delta')onDelta(event.text);
      else if(event.type==='done')completed=event.response;
      else if(event.type==='error'){const error=new Error(event.detail||'流式生成失败');error.status=event.status;throw error}
    }
    if(done)break;
  }
  if(!completed)throw new Error('流式响应意外中断，请重试');
  return completed;
}
function element(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node}
function fail(error){$('error').textContent=error.message}
const media=createMediaComposer({status:text=>$('status').textContent=text,error:text=>$('error').textContent=text});
function setBusy(value){
  busy=value;$('query-form').setAttribute('aria-busy',String(value));
  $('submit').replaceChildren(ragIcon(value?'loader-circle':'arrow-up'));$('submit').classList.toggle('loading',value);
  for(const id of ['submit','new-conversation','rename-conversation','delete-conversation','query'])$(id).disabled=value||(['rename-conversation','delete-conversation'].includes(id)&&!active);
  media.setDisabled(value);
  for(const button of document.querySelectorAll('.session'))button.disabled=value;
}
function renderWelcome(){
  const welcome=element('div',undefined,'welcome');welcome.id='answer';
  const mark=element('div',undefined,'welcome-icon');mark.append(ragIcon('message-square'));
  welcome.append(mark,element('h2','通用问答工作台'),element('p','分析、写作、编程与开放问题','subtitle'));
  const list=element('div',undefined,'suggestions');
  for(const [category,question] of [['概念解释','用通俗语言解释什么是注意力机制'],['写作整理','把一段会议记录整理成行动清单'],['编程分析','解释 Python 生成器的适用场景'],['任务规划','为一项两周研究任务制定执行计划']]){
    const button=element('button',undefined,'suggestion');button.type='button';const text=element('span');text.append(element('small',category),document.createTextNode(question));button.append(text,ragIcon('arrow-up-right'));
    button.onclick=()=>{if(!busy){$('query').value=question;$('query').focus()}};list.append(button);
  }
  welcome.append(list);$('thread').append(welcome);
}
function renderThread(){
  const thread=$('thread');thread.replaceChildren();
  if(!active?.turns?.length){renderWelcome();return}
  for(const [index,turn] of active.turns.entries()){
    const result=turn.response;const item=element('article',undefined,'turn');item.dataset.turn=turn.number;
    const meta=element('div',undefined,'turn-meta');const label=element('span',`第 ${turn.number} 轮`,'meta');label.prepend(ragIcon('message-square'));meta.append(label);
    if(result.status==='truncated')meta.append(element('span','回答已截断','turn-status'));
    const answer=element('div',result.answer,'answer');if(index===active.turns.length-1)answer.id='answer';
    item.append(meta);const attachments=renderMessageMedia(result.attachments);if(attachments)item.append(attachments);item.append(element('h3',turn.query,'question'),answer);thread.append(item);
  }
  thread.scrollTop=thread.scrollHeight;
}
async function refreshList(){
  const data={conversations:[],total:0};
  for(let offset=0;offset<listLimit;offset+=100){const batch=await request(`/general/conversations?limit=${Math.min(listLimit-offset,100)}&offset=${offset}`);data.conversations.push(...batch.conversations);data.total=batch.total;if(data.conversations.length>=data.total)break}
  $('conversations').replaceChildren();$('session-total').textContent=data.total||'';
  if(!data.conversations.length)$('conversations').append(element('p','暂无通用会话','history-empty'));
  for(const conversation of data.conversations){
    const button=element('button',undefined,'session'+(active?.id===conversation.id?' active':''));button.append(ragIcon('message-square'),element('span',conversation.title,'session-title'));button.dataset.id=conversation.id;button.disabled=busy;
    if(active?.id===conversation.id)button.setAttribute('aria-current','true');
    button.append(element('small',new Date(conversation.updated_at).toLocaleString('zh-CN')));button.onclick=()=>selectConversation(conversation.id).catch(fail);$('conversations').append(button);
  }
  $('more-conversations').hidden=data.total<=data.conversations.length;
}
async function selectConversation(id){
  if(busy)return;const ticket=++selection;if(active)drafts.set(active.id,$('query').value);setBusy(true);
  let conversation;try{conversation=await request('/general/conversations/'+id)}finally{setBusy(false)}
  if(ticket!==selection)return;
  active=conversation;pending=null;localStorage.setItem('general_active_conversation',id);$('conversation-title').textContent=active.title;
  document.body.classList.remove('history-open');$('toggle-history').setAttribute('aria-expanded','false');
  $('query').value=drafts.get(id)||'';media.clear();$('error').textContent='';$('status').textContent='';renderThread();setBusy(false);await refreshList();
}
async function createConversation(){
  const conversation=await request('/general/conversations','POST',{});active={...conversation,turns:[]};pending=null;
  localStorage.setItem('general_active_conversation',active.id);$('conversation-title').textContent=active.title;renderThread();await refreshList();
}
$('new-conversation').onclick=async()=>{
  if(busy)return;if(active)drafts.set(active.id,$('query').value);setBusy(true);
  try{await createConversation();$('query').value='';media.clear();$('status').textContent='';$('error').textContent='';$('query').focus()}catch(error){fail(error)}finally{setBusy(false)}
};
$('more-conversations').onclick=()=>{listLimit+=50;refreshList().catch(fail)};
$('query-form').addEventListener('submit',async event=>{
  event.preventDefault();if(busy||!media.ready())return;const query=$('query').value.trim();const attachments=media.refs();if(!query&&!attachments.length)return;
  setBusy(true);$('error').textContent='';$('status').textContent='正在生成回答…';
  let streamingTurn=null;
  try{
    if(!active)await createConversation();
    streamingTurn=element('article',undefined,'turn streaming-turn');
    const meta=element('div',undefined,'turn-meta');const label=element('span',`第 ${active.turns.length+1} 轮`,'meta');label.prepend(ragIcon('message-square'));meta.append(label);
    const streamedAnswer=element('div','正在等待模型响应…','answer streaming-answer');
    streamingTurn.append(meta);const preview=renderMessageMedia(media.list());if(preview)streamingTurn.append(preview);streamingTurn.append(element('h3',query||'请分析附件内容。','question'),streamedAnswer);$('thread').append(streamingTurn);$('thread').scrollTop=$('thread').scrollHeight;
    const signature=JSON.stringify({query,attachments,conversation_id:active.id});
    if(!pending||pending.signature!==signature)pending={signature,body:{query,attachments,conversation_id:active.id,request_id:requestId(),expected_revision:active.revision}};
    let received=false;
    const data=await streamRequest('/general/ask/stream',pending.body,delta=>{
      if(!received){streamedAnswer.textContent='';received=true}
      streamedAnswer.textContent+=delta;$('thread').scrollTop=$('thread').scrollHeight;
    });
    active=await request('/general/conversations/'+active.id);pending=null;
    $('conversation-title').textContent=active.title;drafts.delete(active.id);$('query').value='';media.clear();renderThread();
    $('status').textContent=`${data.status==='truncated'?'回答已截断':'已完成'} · ${(data.latency_ms/1000).toFixed(2)} 秒`;await refreshList();
  }catch(error){
    fail(error);$('status').textContent='发送失败，问题已保留';
    if(error.status===409&&active){pending=null;try{active=await request('/general/conversations/'+active.id);renderThread();await refreshList()}catch{}}
  }finally{streamingTurn?.remove();setBusy(false)}
});
$('rename-conversation').onclick=()=>{if(!active||busy)return;$('rename-title').value=active.title;$('rename-dialog').showModal();$('rename-title').focus()};
$('cancel-rename').onclick=()=>$('rename-dialog').close();
$('rename-form').onsubmit=async event=>{
  event.preventDefault();try{const data=await request('/general/conversations/'+active.id,'PATCH',{title:$('rename-title').value.trim(),expected_revision:active.revision});active={...active,...data};pending=null;$('conversation-title').textContent=active.title;$('rename-dialog').close();await refreshList()}catch(error){$('rename-dialog').close();fail(error)}
};
$('delete-conversation').onclick=async()=>{
  if(!active||busy||!confirm(`删除会话“${active.title}”？`))return;
  try{await request(`/general/conversations/${active.id}?expected_revision=${active.revision}`,'DELETE');drafts.delete(active.id);active=null;pending=null;localStorage.removeItem('general_active_conversation');$('conversation-title').textContent='新会话';$('query').value='';media.clear();renderThread();setBusy(false);await refreshList()}catch(error){fail(error)}
};
async function initialize(){
  setBusy(true);
  try{
    try{const health=await request('/health');$('health').textContent=health.status==='ok'?'服务在线':'生成服务异常';$('health').dataset.state=health.status==='ok'?'online':'offline'}catch{$('health').textContent='生成服务暂不可用';$('health').dataset.state='offline'}
    setBusy(false);const saved=localStorage.getItem('general_active_conversation');if(saved){try{await selectConversation(saved)}catch(error){if(error.status===404)localStorage.removeItem('general_active_conversation');else throw error}}
    await refreshList();
  }catch(error){fail(error);$('health').textContent='连接异常'}finally{setBusy(false)}
}
initialize();renderThread();
