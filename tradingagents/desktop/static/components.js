'use strict';
/* Accessible themed controls. Native selects remain the source of form values. */
window.StockSeekControls = (() => {
  let active=null, sequence=0;
  const arrows='<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg>';
  function close(restore=false){if(!active)return;const {button,menu}=active;menu.remove();button.setAttribute('aria-expanded','false');active=null;if(restore&&button.isConnected)button.focus();}
  function enhance(root=document){
    root.querySelectorAll('select:not([data-enhanced])').forEach(select=>{
      select.dataset.enhanced='true';select.classList.add('select-native');select.tabIndex=-1;select.setAttribute('aria-hidden','true');
      const wrap=document.createElement('div');wrap.className='select-control';select.parentNode.insertBefore(wrap,select);wrap.append(select);
      const button=document.createElement('button');button.type='button';button.className='select-trigger';button.setAttribute('aria-haspopup','listbox');button.setAttribute('aria-expanded','false');
      const label=select.getAttribute('aria-label')||[...(select.closest('label')?.childNodes||[])].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent.trim()).join(' ')||'选择选项';
      button.setAttribute('aria-label',label);const text=document.createElement('span');button.append(text);button.insertAdjacentHTML('beforeend',arrows);wrap.append(button);
      const sync=()=>{text.textContent=select.selectedOptions[0]?.textContent||'请选择';button.disabled=select.disabled;};select.addEventListener('change',sync);sync();
      function open(){
        if(active?.button===button){close();return;}close();
        const menu=document.createElement('div');menu.id=`stockseek-select-${++sequence}`;button.setAttribute('aria-controls',menu.id);menu.className='select-menu';menu.setAttribute('role','listbox');menu.setAttribute('aria-label',label);
        let index=Math.max(0,select.selectedIndex);const options=[...select.options];const items=[];
        function focus(i){index=Math.max(0,Math.min(options.length-1,i));items.forEach((item,n)=>{item.classList.toggle('focused',n===index);item.tabIndex=n===index?0:-1;});items[index]?.focus({preventScroll:true});items[index]?.scrollIntoView({block:'nearest'});}
        function choose(i){if(options[i].disabled)return;select.selectedIndex=i;sync();close(true);select.dispatchEvent(new Event('input',{bubbles:true}));select.dispatchEvent(new Event('change',{bubbles:true}));}
        options.forEach((option,i)=>{const item=document.createElement('button');item.type='button';item.className='select-option';item.setAttribute('role','option');item.setAttribute('aria-selected',String(i===select.selectedIndex));item.disabled=option.disabled;item.textContent=option.textContent;item.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();choose(i);});items.push(item);menu.append(item);});
        const owner=select.closest('dialog')||document.body;owner.append(menu);
        const rect=button.getBoundingClientRect();const width=Math.max(180,rect.width);menu.style.width=Math.min(width,innerWidth-24)+'px';menu.style.left=Math.max(12,Math.min(rect.left,innerWidth-width-12))+'px';
        const available=innerHeight-rect.bottom-12;
        if(available>=Math.min(240,menu.scrollHeight)){menu.style.top=rect.bottom+6+'px';menu.style.maxHeight=Math.min(300,available-6)+'px';}else{menu.style.bottom=innerHeight-rect.top+6+'px';menu.style.maxHeight=Math.max(100,Math.min(300,rect.top-18))+'px';}
        active={button,menu};button.setAttribute('aria-expanded','true');focus(index);
        let search='',timer;
        menu.addEventListener('keydown',event=>{
          if(['ArrowDown','ArrowUp','Home','End','Enter',' ','Escape','Tab'].includes(event.key)){
            if(event.key!=='Tab')event.preventDefault();
            if(event.key==='ArrowDown')focus(index+1);else if(event.key==='ArrowUp')focus(index-1);else if(event.key==='Home')focus(0);else if(event.key==='End')focus(options.length-1);else if(event.key==='Enter'||event.key===' ')choose(index);else close(event.key==='Escape'||event.key==='Tab');
          }else if(event.key.length===1){search+=event.key.toLowerCase();clearTimeout(timer);timer=setTimeout(()=>search='',600);const found=options.findIndex(o=>o.textContent.toLowerCase().startsWith(search));if(found>=0)focus(found);}
        });
      }
      button.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();open();});
      button.addEventListener('keydown',event=>{if(['ArrowDown','ArrowUp','Enter',' '].includes(event.key)){event.preventDefault();if(!active)open();}});
    });
  }
  document.addEventListener('pointerdown',event=>{if(active&&!active.menu.contains(event.target)&&!active.button.contains(event.target))close();});
  window.addEventListener('resize',()=>close());
  document.addEventListener('scroll',event=>{if(active&&!active.menu.contains(event.target))close();},true);
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&active){event.preventDefault();close(true);}},true);
  return {enhance,close,sync:root=>root.querySelectorAll('select[data-enhanced]').forEach(s=>s.dispatchEvent(new Event('change')))};
})();
