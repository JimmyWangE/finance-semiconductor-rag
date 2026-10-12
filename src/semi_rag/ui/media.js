window.createMediaComposer=function({status,error,onAdded}={}){
  const tray=document.getElementById('attachments');
  const input=document.getElementById('media-input');
  const trigger=document.getElementById('attach-media');
  let items=[],uploading=false,disabled=false;
  const render=()=>{
    tray.replaceChildren();tray.hidden=!items.length;
    for(const item of items){
      const card=document.createElement('div');card.className='attachment-item';
      const media=document.createElement(item.kind==='video'?'video':'img');media.src=item.url;
      if(item.kind==='video'){media.muted=true;media.preload='metadata'}else media.alt=item.name;
      const name=document.createElement('span');name.className='attachment-name';name.textContent=item.name;
      const remove=document.createElement('button');remove.type='button';remove.className='attachment-remove';remove.title='移除附件';remove.setAttribute('aria-label',`移除 ${item.name}`);remove.append(ragIcon('x'));remove.disabled=disabled;
      remove.onclick=()=>{items=items.filter(value=>value.id!==item.id);render()};card.append(media,name,remove);tray.append(card);
    }
  };
  trigger.onclick=()=>{if(!disabled&&!uploading)input.click()};
  input.onchange=async()=>{
    const files=[...input.files];input.value='';if(!files.length)return;
    if(items.length+files.length>4){error?.('每次最多添加 4 个媒体文件');return}
    if(items.filter(item=>item.kind==='video').length+files.filter(file=>file.type.startsWith('video/')).length>1){error?.('每次最多添加 1 个视频');return}
    uploading=true;trigger.disabled=true;status?.(`正在上传 ${files.length} 个媒体文件…`);
    try{
      for(const file of files){
        const form=new FormData();form.append('file',file);
        const response=await fetch('/media',{method:'POST',body:form});let data;
        try{data=await response.json()}catch{data={detail:`上传失败（HTTP ${response.status}）`}}
        if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));
        if(!items.some(item=>item.id===data.id))items.push(data);
      }
      render();status?.(`已添加 ${items.length} 个媒体文件`);onAdded?.();
    }catch(reason){error?.(reason.message)}finally{uploading=false;trigger.disabled=disabled}
  };
  return {
    refs:()=>items.map(item=>({id:item.id})),
    list:()=>items.slice(),
    clear:()=>{items=[];render()},
    ready:()=>!uploading,
    setDisabled:value=>{disabled=value;trigger.disabled=value||uploading;input.disabled=value;render()},
  };
};

window.renderMessageMedia=function(attachments){
  if(!attachments?.length)return null;
  const tray=document.createElement('div');tray.className='message-media';
  for(const item of attachments){
    const link=document.createElement('a');link.href=item.url;link.target='_blank';link.rel='noopener';link.title=item.name;
    const media=document.createElement(item.kind==='video'?'video':'img');media.src=item.url;
    if(item.kind==='video'){media.controls=true;media.preload='metadata'}else media.alt=item.name;
    link.append(media);tray.append(link);
  }
  return tray;
};
