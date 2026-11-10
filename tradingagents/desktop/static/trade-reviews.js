'use strict';
(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const dimensions = {research:'研究方向',strategy:'策略规则',execution:'个人执行',uncertainty:'不确定性与其他解释（必填）'};
  const groups = {entry:'入场',filters:'过滤',exit:'退出',invalidation:'失效',position:'仓位',portfolio:'组合',exceptions:'例外'};
  const state = {busy:false,error:'',loaded:false,filter:'',ticker:null,offset:0,fills:null,trade:null,context:null,reviews:null,reviewOffset:0,selected:null,drafts:new Map(),rules:new Map()};
  const raw = (title, value) => `<details><summary>${title}</summary><pre>${esc(JSON.stringify(value,null,2))}</pre></details>`;
  const fillText = row => `${row.fill.ticker} · ${row.fill.side==='buy'?'买入':'卖出'} ${row.fill.quantity} × ${row.fill.price} ${row.fill.currency} · ${row.fill.executed_at}`;
  function draft() {
    const id=state.trade.trade_id;
    if(!state.drafts.has(id))state.drafts.set(id,{notes:{research:'',strategy:'',execution:'',uncertainty:''},request:null,saved:false});
    return state.drafts.get(id);
  }
  function ruleDraft() {
    if(!state.selected)return null;
    const id=state.selected.review_id;
    if(!state.rules.has(id))state.rules.set(id,{name:'',text:'',citation:'',group:'entry'});
    return state.rules.get(id);
  }
  function review(row) {
    if(!row)return '<p>尚未选择历史复盘。</p>';
    return `<section aria-label="复盘快照"><h3>个人复盘 · ${esc(row.recorded_at)}</h3><p>标识：${esc(row.review_id)}</p><p>${esc(fillText(row.fill_snapshot))}</p><p>个人意见；因果与成交时已知状态均未确立。单次卖后上涨或单笔盈亏不能直接证明研究、规则或执行错误。</p><dl>${Object.entries(dimensions).map(([key,label])=>`<dt>${label}</dt><dd>${row.notes[key]===null?'尚未评估':esc(row.notes[key])}</dd>`).join('')}</dl>${raw('登记时的成交和依据快照',row)}</section>`;
  }
  async function run(action) {
    if(state.busy)return;
    state.busy=true;state.error='';state.render();
    try {await action();} catch(error) {state.error=error.message;}
    finally {state.busy=false;state.render();}
  }
  window.StockSeekReviews={mount(container,api,openStrategy) {
    const panel=container.querySelector('[data-trade-reviews]');
    if(!panel)return;
    const loadFills=async(ticker,offset)=>{
      const rows=await api('list_actual_trades',ticker,20,offset);
      state.fills=rows;state.ticker=ticker;state.offset=offset;state.loaded=true;
    };
    const loadReviews=async offset=>{
      const rows=await api('list_trade_reviews',state.trade.trade_id,20,offset);
      state.reviews=rows;state.reviewOffset=offset;
    };
    function render() {
      if(!panel.isConnected)return;
      const d=state.trade?draft():null, r=ruleDraft(), disabled=state.busy?'disabled':'';
      panel.innerHTML=`<div class="page-head"><div><h1>成交复盘</h1><p>从已导入的实际成交逐项评估，保留未评估状态。所有评价由你填写。</p></div></div>${state.error?`<p role="alert">${esc(state.error)}；已有输入与快照保留，可重试。</p>`:''}${state.busy?'<p role="status">正在读取 / 保存…</p>':''}<form data-review-form="filter"><label>股票代码（留空查询全部）<input name="ticker" value="${esc(state.filter)}" maxlength="40" ${disabled}></label><button ${disabled}>查询成交</button></form><section aria-label="实际成交列表"><h2>实际成交</h2><p>调用者声明，未经券商独立核验；导入方式见 docs/trade-import.md。</p>${state.fills?`<p>共 ${state.fills.total} 条</p>${state.fills.items.map(row=>`<button type="button" data-review-trade="${esc(row.trade_id)}" ${disabled}>${esc(fillText(row))}</button>`).join('')||'<p>没有匹配的实际成交。</p>'}<div class="actions"><button type="button" data-review-action="fills-prev" ${state.busy||!state.offset?'disabled':''}>上一页成交</button><button type="button" data-review-action="fills-next" ${state.busy||state.offset+20>=state.fills.total?'disabled':''}>下一页成交</button></div>`:'<p>尚未读取成交。</p>'}</section>${d?`<section aria-label="当前成交"><h2>${esc(fillText(state.trade))}</h2><p>成交标识：${esc(state.trade.trade_id)}</p>${raw('实际成交原始记录',state.trade)}<p>${state.context?`当前关联 v${state.context.version}；保存复盘时后端采集当时最新关联快照。`:'尚无关联依据；不推断当时没有研究或规则。'}</p>${raw('已读取的当前成交依据',state.context)}<form data-review-form="record"><h3>追加个人复盘</h3><p>请根据当时可得证据分别评估；前三项留空表示尚未评估。每项为单段文字，最多 10000 字符。修正意见追加新复盘，历史快照保留。</p>${Object.entries(dimensions).map(([key,label])=>`<label>${label}<input name="${key}" value="${esc(d.notes[key])}" maxlength="10000" ${key==='uncertainty'?'required':''} ${disabled}></label>`).join('')}<button ${state.busy||d.saved?'disabled':''}>${d.saved?'复盘已保存；修改评价后可追加':'保存个人复盘'}</button></form></section><section aria-label="复盘历史"><h2>此成交的历史复盘</h2><button type="button" data-review-action="refresh" ${disabled}>读取 / 刷新历史</button>${state.reviews?`<p>共 ${state.reviews.total} 条</p>${state.reviews.items.map(row=>`<button type="button" data-review-id="${esc(row.review_id)}" ${disabled}>${esc(row.recorded_at)} · ${esc(row.review_id.slice(0,12))}</button>`).join('')||'<p>尚无复盘。</p>'}<div class="actions"><button type="button" data-review-action="reviews-prev" ${state.busy||!state.reviewOffset?'disabled':''}>上一页复盘</button><button type="button" data-review-action="reviews-next" ${state.busy||state.reviewOffset+20>=state.reviews.total?'disabled':''}>下一页复盘</button></div>`:'<p>尚未读取历史。</p>'}${review(state.selected)}</section>${state.selected?`<form data-review-form="rule"><h3>从所选复盘提出个人规则</h3><p>只保存你明确填写的待定义草案。到策略管理补齐适用范围、时序和规则，经具名确认后使用；不能追溯成为过去知识。</p><label>策略名称<input name="name" value="${esc(r.name)}" required maxlength="200" ${disabled}></label><label>规则分类<select name="group" ${disabled}>${Object.entries(groups).map(([key,label])=>`<option value="${key}" ${r.group===key?'selected':''}>${label}</option>`).join('')}</select></label><label>个人规则<textarea name="text" required maxlength="10000" ${disabled}>${esc(r.text)}</textarea></label><label>依据或假设说明<textarea name="citation" required maxlength="10000" ${disabled}>${esc(r.citation)}</textarea></label><button ${disabled}>保存待定义个人草案</button><button type="button" data-review-action="open-rule" ${disabled}>打开该复盘的规则草案</button><p>每次复盘仅建一个初始草案；已生成或保存响应中断时，可直接打开。后续修改在策略管理追加版本。</p></form>`:''}`:''}`;
      panel.querySelectorAll('form').forEach(form=>{form.noValidate=true;form.onsubmit=event=>{
        event.preventDefault();event.stopPropagation();
        const kind=form.dataset.reviewForm;
        run(async()=>{
          if(kind==='filter')await loadFills(state.filter.trim()||null,0);
          if(kind==='record'){
            const notes=Object.fromEntries(Object.entries(d.notes).map(([key,value])=>[key,value.trim()||null]));
            const key=JSON.stringify(notes);
            if(!d.request||d.request.key!==key)d.request={key,id:crypto.randomUUID()};
            const row=await api('record_trade_review',state.trade.trade_id,notes,d.request.id);
            state.selected=row;d.saved=true;
            // Publish the saved snapshot even if a subsequent history refresh fails.
            await loadReviews(0);
          }
          if(kind==='rule'){
            if(!r.name.trim()||!r.text.trim()||!r.citation.trim())throw new Error('请完整填写策略名称、个人规则和依据');
            const row=await api('create_review_strategy',state.selected.review_id,{name:r.name.trim(),rules:{[r.group]:[{text:r.text.trim(),citation:r.citation.trim(),kind:'personal',state:'pending'}]}});
            await openStrategy(row.strategy_id);
          }
        });
      };});
      panel.querySelector('input[name="ticker"]').oninput=event=>{state.filter=event.target.value;};
      panel.querySelectorAll('[data-review-form="record"] input').forEach(input=>{input.oninput=()=>{d.notes[input.name]=input.value;if(d.saved)d.request=null;d.saved=false;panel.querySelector('[data-review-form="record"] button').disabled=state.busy;};});
      panel.querySelectorAll('[data-review-form="rule"] input,[data-review-form="rule"] textarea,[data-review-form="rule"] select').forEach(input=>{input.oninput=()=>{r[input.name]=input.value;};});
      panel.querySelectorAll('[data-review-trade]').forEach(button=>{button.onclick=()=>run(async()=>{
        const id=button.dataset.reviewTrade;
        const [trade,context,reviews]=await Promise.all([api('get_actual_trade',id),api('get_trade_context',id),api('list_trade_reviews',id,20,0)]);
        state.trade=trade;state.context=context;state.reviews=reviews;state.reviewOffset=0;state.selected=null;
      });});
      panel.querySelectorAll('[data-review-id]').forEach(button=>{button.onclick=()=>run(async()=>{
        const row=await api('get_trade_review',button.dataset.reviewId);
        if(row.trade_id!==state.trade.trade_id)throw new Error('复盘不属于当前实际成交');
        state.selected=row;
      });});
      panel.querySelectorAll('[data-review-action]').forEach(button=>{button.onclick=()=>run(async()=>{
        const action=button.dataset.reviewAction;
        if(action==='fills-prev'||action==='fills-next')await loadFills(state.ticker,state.offset+(action==='fills-prev'?-20:20));
        if(action==='refresh')await loadReviews(0);
        if(action==='reviews-prev'||action==='reviews-next')await loadReviews(state.reviewOffset+(action==='reviews-prev'?-20:20));
        if(action==='open-rule')await openStrategy('review-'+state.selected.review_id);
      });});
    }
    state.render=render;
    render();
    if(!state.loaded&&!state.busy)run(()=>loadFills(null,0));
  }};
})();
