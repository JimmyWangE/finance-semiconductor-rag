const $=id=>document.getElementById(id);
const state={sources:[],articles:[],source:'',query:'',unread:false,selected:null};

async function api(path,options={}){
  const response=await fetch(path,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(data.detail||`请求失败（${response.status}）`);
  return data;
}
async function waitTask(task){
  if(!task||!task.id||!task.type)return task;
  for(let count=0;count<1200;count++){
    const current=await api(`/tasks/${encodeURIComponent(task.id)}`);
    if(current.status==='succeeded')return current.result||{};
    if(current.status==='failed'||current.status==='cancelled')throw new Error(current.error?.message||`任务${current.status==='failed'?'失败':'已取消'}`);
    setStatus(`更新任务${current.status==='running'?'执行中':'排队中'}`,'loading');
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
  throw new Error('更新任务等待超时，可稍后在任务中心查看');
}

function node(tag,text,className){const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(className)item.className=className;return item}
function dateLabel(value){
  if(!value)return '日期未识别';
  const parsed=new Date(value.replace(/年|月/g,'-').replace(/日/g,''));
  if(Number.isNaN(parsed.getTime()))return value;
  return new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit'}).format(parsed);
}
function setStatus(text,status='ready'){$('sync-state').textContent=text;$('sync-state').dataset.state=status==='error'?'offline':''}

function renderSources(){
  $('source-total').textContent=`${state.sources.length} 个`;
  $('import-source').replaceChildren();$('feed-source').replaceChildren();
  const total=state.sources.reduce((sum,item)=>sum+item.article_count,0);
  const configured=state.sources.filter(item=>item.feed_url).length;
  $('feed-summary').textContent=`${configured}/${state.sources.length} 个订阅源已配置`;
  $('all-count').textContent=total;
  $('source-list').replaceChildren();
  for(const source of state.sources){
    const button=node('button',undefined,'source-row');button.type='button';button.dataset.source=source.id;button.setAttribute('aria-pressed',String(state.source===source.id));
    if(state.source===source.id)button.classList.add('active');
    const mark=node('span',source.name.slice(0,1),'source-monogram');
    const text=node('span');text.append(node('strong',source.name),node('small',source.last_error?'更新失败':source.last_synced_at?`更新 ${dateLabel(source.last_synced_at)}`:source.feed_url?'等待首次更新':'待配置订阅源'));
    button.append(mark,text,node('b',String(source.article_count)));
    button.addEventListener('click',()=>selectSource(source.id,source.name));$('source-list').append(button);
    if(source.id==='icsmart'||source.id==='semi-insights'){
      const option=node('option',source.name);option.value=source.id;$('import-source').append(option);
    }
    const feedOption=node('option',source.name);feedOption.value=source.id;$('feed-source').append(feedOption);
  }
  const all=document.querySelector('.source-row[data-source=""]');all.classList.toggle('active',state.source==='');all.setAttribute('aria-pressed',String(state.source===''));
}

function renderArticles(){
  const target=$('article-list');target.replaceChildren();
  let articles=[...state.articles];
  if($('sort-order').value==='imported')articles.sort((a,b)=>b.imported_at.localeCompare(a.imported_at));
  $('result-count').textContent=`${articles.length} 篇`;
  if(!articles.length){
    const empty=node('div',undefined,'list-empty');const inner=node('div');inner.append(ragIcon('newspaper'),node('strong','尚无符合条件的文章'));empty.append(inner);target.append(empty);return;
  }
  for(const article of articles){
    const row=node('button',undefined,'article-row');row.type='button';row.setAttribute('role','listitem');row.dataset.id=article.id;
    if(article.read)row.classList.add('read');if(article.id===state.selected)row.classList.add('active');
    const meta=node('div',undefined,'article-meta');meta.append(node('span',article.source_name),node('time',dateLabel(article.published_at||article.imported_at)),node('span','原文链接','content-state'));
    row.append(meta,node('h3',article.title));if(article.description)row.append(node('p',article.description));
    row.addEventListener('click',()=>openArticle(article.id));target.append(row);
  }
}

async function loadSources(){const data=await api('/article-api/sources');state.sources=data.sources;renderSources()}
async function loadArticles(){
  const params=new URLSearchParams({limit:'500'});if(state.source)params.set('source_id',state.source);if(state.query)params.set('query',state.query);if(state.unread)params.set('unread','true');
  const data=await api('/article-api/items?'+params);state.articles=data.articles;renderArticles();return data;
}
async function refresh(showStatus=true){
  if(showStatus)setStatus('正在刷新');
  try{await Promise.all([loadSources(),loadArticles()]);if(showStatus)setStatus('资料已就绪')}
  catch(error){setStatus('刷新失败','error');console.error(error)}
}
async function syncArticles(){
  const button=$('sync-articles');button.disabled=true;button.classList.add('loading');setStatus('正在检查订阅源');
  try{
    let result=await api('/article-api/sync',{method:'POST',body:JSON.stringify({source_id:''})});
    result=await waitTask(result);
    await refresh(false);setStatus(`更新完成 · 新增 ${result.created} 篇，已有 ${result.skipped} 篇${result.failed?`，失败 ${result.failed} 篇`:''}`,result.failed?'error':'ready');
  }catch(error){setStatus(error.message,'error')}
  finally{button.disabled=false;button.classList.remove('loading')}
}
function selectSource(id,name){state.source=id;$('current-source').textContent=name;renderSources();loadArticles().catch(error=>setStatus(error.message,'error'))}

function openArticle(id){
  const article=state.articles.find(value=>value.id===id);
  if(!article){setStatus('未找到文章','error');return}
  window.open(article.url,'_blank','noopener,noreferrer');
  state.selected=id;article.read=1;renderArticles();setStatus('已打开原文');
  api(`/article-api/items/${id}`).then(loadSources).catch(()=>{});
}
function closeReader(){document.body.classList.remove('article-open');history.replaceState(null,'','/articles')}

let searchTimer;
$('article-search').addEventListener('input',event=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.query=event.target.value.trim();loadArticles().catch(error=>setStatus(error.message,'error'))},220)});
$('unread-only').addEventListener('change',event=>{state.unread=event.target.checked;loadArticles().catch(error=>setStatus(error.message,'error'))});
$('sort-order').addEventListener('change',renderArticles);
document.querySelector('.source-row[data-source=""]').addEventListener('click',()=>selectSource('','全部文章'));
$('sync-articles').addEventListener('click',syncArticles);
$('reader-back').addEventListener('click',closeReader);

const dialog=$('import-dialog');
function closeImport(){dialog.close();$('import-error').textContent='';$('import-form').reset()}
$('import-open').addEventListener('click',()=>dialog.showModal());$('import-close').addEventListener('click',closeImport);$('import-cancel').addEventListener('click',closeImport);
dialog.addEventListener('click',event=>{if(event.target===dialog)closeImport()});
$('import-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('import-submit');button.disabled=true;button.classList.add('loading');button.lastChild.textContent=' 正在导入';$('import-error').textContent='';
  try{
    const article=await api('/article-api/import',{method:'POST',body:JSON.stringify({source_id:$('import-source').value,url:$('import-url').value})});
    closeImport();await refresh();setStatus(article.created?'已导入文章，可从列表打开原文':'文章已存在，可从列表打开原文');
  }catch(error){$('import-error').textContent=error.message}
  finally{button.disabled=false;button.classList.remove('loading');button.lastChild.textContent=' 导入文章'}
});

const feedDialog=$('feed-dialog');
function selectedFeed(){return state.sources.find(source=>source.id===$('feed-source').value)}
function fillFeed(){const source=selectedFeed();$('feed-url').value=source?.feed_url||'';$('feed-error').textContent=''}
function closeFeed(){feedDialog.close();$('feed-error').textContent=''}
$('feed-open').addEventListener('click',()=>{if(state.source)$('feed-source').value=state.source;fillFeed();feedDialog.showModal()});
$('feed-source').addEventListener('change',fillFeed);$('feed-close').addEventListener('click',closeFeed);$('feed-cancel').addEventListener('click',closeFeed);
feedDialog.addEventListener('click',event=>{if(event.target===feedDialog)closeFeed()});
$('feed-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('feed-submit');button.disabled=true;$('feed-error').textContent='';
  try{
    await api(`/article-api/sources/${encodeURIComponent($('feed-source').value)}/feed`,{method:'PUT',body:JSON.stringify({feed_url:$('feed-url').value.trim()})});
    closeFeed();await loadSources();setStatus('订阅源配置已保存');
  }catch(error){$('feed-error').textContent=error.message}
  finally{button.disabled=false}
});

refresh().catch(error=>setStatus(error.message,'error'));
