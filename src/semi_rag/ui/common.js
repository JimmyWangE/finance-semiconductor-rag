window.ragIcon = function(name) {
  const icon=document.createElement('i');icon.className='icon';icon.dataset.icon=name;icon.setAttribute('aria-hidden','true');return icon;
};
document.querySelectorAll('.icon').forEach(icon=>icon.setAttribute('aria-hidden','true'));
document.getElementById('toggle-history')?.addEventListener('click',event=>{
  const open=document.body.classList.toggle('history-open');
  event.currentTarget.setAttribute('aria-expanded',String(open));
});
document.addEventListener('keydown',event=>{if(event.key==='Escape'){document.body.classList.remove('history-open');document.getElementById('toggle-history')?.setAttribute('aria-expanded','false')}});
