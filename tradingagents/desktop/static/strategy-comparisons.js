'use strict';
(() => {
  const cache = new Map();
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const verdicts = {not_assessed:'未评估',conflict_identified:'人工识别到分歧',no_conflict_identified:'人工未识别到分歧'};
  const fields = {symbol:'证券',market:'市场',timeframe:'周期',price_basis:'价格口径',source:'数据来源',revision:'数据修订',as_of:'截止时刻',series:'输入序列（含单位、观测与可得时间）'};
  function entry(item) {
    const definition = item.strategy.definition;
    return `<article><h4>${escape(definition.name)} · v${item.strategy.version}</h4><p>策略标识：${escape(item.strategy.strategy_id)}</p><p>来源：${escape(definition.source)} · 适用环境：${escape(definition.environment)} · 持有期：${escape(definition.holding_period)}</p>${window.StockSeekConditions.snapshot(item.check)}</article>`;
  }
  const entries = items => `<div class="strategy-comparison-entries">${items.map(entry).join('')}</div>`;
  const context = differences => `<p>上下文差异: ${differences.map(field=>escape(fields[field]||field)).join(' / ')||'未发现所检查字段的差异; 规则含义与环境仍需人工判断'}</p>${differences.length?'<p>上下文存在差异, 不能将结果差异直接解释为策略冲突。</p>':''}`;
  window.StockSeekComparisons = {mount(parent, doc, check, api) {
    const panel = document.createElement('section');
    panel.setAttribute('aria-label','策略独立对照');
    parent.append(panel);
    if (!check) {panel.innerHTML='<h3>策略独立对照</h3><p>先选择本版本的条件检查快照，再与另一个检查对照。</p>';return;}
    const key = check.check_id;
    if (!cache.has(key)) cache.set(key,{otherId:'',other:null,verdict:'not_assessed',note:'',by:'',history:null,record:null,busy:false,error:''});
    const state = cache.get(key);
    function render() {
      if (!panel.isConnected) return;
      panel.innerHTML = `<h3>策略独立对照</h3><p>各体系分别检查，不平均评分，不合成买卖结论。分歧及适用环境由你具名判断；上下文一致也不代表规则等价。</p><p>当前检查：${escape(key)}</p><form><label>另一检查标识<input name="otherId" value="${escape(state.otherId)}" required></label><button type="button" data-compare="preview" ${state.busy?'disabled':''}>读取另一检查证据</button><label>人工冲突判断<select name="verdict">${Object.entries(verdicts).map(([value,label])=>`<option value="${value}" ${state.verdict===value?'selected':''}>${label}</option>`).join('')}</select></label><label>依据、分歧条件与适用环境<textarea name="note" maxlength="10000">${escape(state.note)}</textarea></label><label>判断人<input name="by" value="${escape(state.by)}" maxlength="200" required></label><button type="submit" ${state.busy||!state.other?'disabled':''}>追加保存具名对照快照</button></form><button type="button" data-compare="history" ${state.busy?'disabled':''}>读取 / 刷新此检查的对照历史</button>${state.busy?'<p role="status">正在读取 / 保存对照…</p>':''}${state.error?`<p role="alert">${escape(state.error)}；已有证据与输入已保留，可重试。</p>`:''}<div>${state.history===null?'':state.history.length?state.history.map(row=>`<button type="button" data-comparison-id="${escape(row.comparison_id)}" ${state.busy?'disabled':''}>${escape(row.created_at)} · ${escape(verdicts[row.verdict])} · ${escape(row.assessed_by)}</button>`).join(''):'<p>此检查尚无对照记录。</p>'}</div>${state.record?`<section aria-label="已保存策略对照"><h4>${escape(verdicts[state.record.verdict])} · ${escape(state.record.assessed_by)}</h4><p>${escape(state.record.note||'未填写说明')}</p>${context(state.record.context_differences)}${entries(state.record.entries)}<details><summary>限制与不可变对照快照</summary><pre>${escape(JSON.stringify(state.record,null,2))}</pre></details></section>`:''}${state.other?`<section aria-label="待保存对照证据"><h4>待保存的两项独立证据</h4><p>请核对两边的证券、时点、周期、来源、口径、规则及适用条件。</p>${context(state.other.context_differences)}${entries([{strategy:doc,check},state.other])}</section>`:''}`;
      const form = panel.querySelector('form');
      form.oninput=()=>{state.otherId=form.elements.otherId.value;state.verdict=form.elements.verdict.value;state.note=form.elements.note.value;state.by=form.elements.by.value;};
      panel.querySelector('[data-compare="preview"]').onclick=()=>run(async()=>{
        if(state.otherId===key)throw new Error('请选择两个不同的条件检查快照');
        const requested=state.otherId;
        const preview=await api('preview_strategy_comparison',key,requested);
        const other=preview.entries[1].check;
        if(other.check_id!==requested)throw new Error('返回的检查标识不一致');
        state.other={...preview.entries[1],context_differences:preview.context_differences};
      });
      form.onsubmit=event=>{event.preventDefault();event.stopPropagation();run(async()=>{
        if(!state.other||state.other.check.check_id!==state.otherId)throw new Error('请先读取当前填写的另一检查证据');
        const row=await api('save_strategy_comparison',key,state.otherId,state.verdict,state.note,state.by);
        state.record=row;
        state.history=[row,...(state.history||[]).filter(item=>item.comparison_id!==row.comparison_id)];
      });};
      panel.querySelector('[data-compare="history"]').onclick=()=>run(async()=>{state.history=await api('strategy_comparison_history',key);});
      panel.querySelectorAll('[data-comparison-id]').forEach(button=>{button.onclick=()=>run(async()=>{
        const row=await api('get_strategy_comparison',button.dataset.comparisonId);
        if(!row.check_ids.includes(key))throw new Error('对照不属于当前检查');
        state.record=row;
      });});
    }
    async function run(action) {
      if(state.busy)return;
      state.busy=true;state.error='';render();
      try{await action();}catch(error){state.error=error.message;}
      finally{state.busy=false;state.render();}
    }
    state.render=render;
    render();
  }};
})();
