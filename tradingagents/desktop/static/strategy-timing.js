'use strict';
(() => {
  const cache = new Map();
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const json = value => esc(JSON.stringify(value, null, 2));
  const statuses = {consistent:'时序一致',inconsistent:'时序不一致',insufficient_data:'证据不足'};
  window.StockSeekTiming = {mount(parent, doc, check, api) {
    if (!check) return;
    const key = check.check_id;
    if (!cache.has(key)) cache.set(key, {input:'',history:null,selected:null,busy:false,error:''});
    const state = cache.get(key);
    const panel = document.createElement('section');
    panel.className = 'condition-panel'; panel.setAttribute('aria-label','策略时序审计');
    parent.append(panel);
    function verify(row) {
      if (row.check_id!==key || row.strategy_id!==doc.strategy_id || row.version!==doc.version) throw new Error('审计不属于当前条件检查和策略版本');
      return row;
    }
    function render() {
      if (!panel.isConnected) return;
      const row = state.selected;
      panel.innerHTML = `<h3>v${doc.version} 的时序审计</h3><p>绑定条件检查：${esc(key)}</p><p>调用者提供的日历与价格未经独立核验。时序一致不代表条件整体通过、可成交或策略有效；不自动获取数据或执行交易。</p><details><summary>本版本时序契约</summary><pre>${json(doc.definition.timing || null)}</pre></details>${doc.definition.timing ? `<form><label>粘贴日历与执行价格证据（JSON）<textarea name="timing_evidence" required spellcheck="false">${esc(state.input)}</textarea></label><p>格式见 docs/strategy-timing.md；所有时间须包含时区，可明确提交空执行价格。</p><button type="submit" ${state.busy?'disabled':''}>保存本次时序审计</button></form>` : '<p>本版本未定义时序契约；请通过策略 API 另存草稿并确认后检查。</p>'}<button type="button" data-timing-history ${state.busy?'disabled':''}>读取 / 刷新本次检查的审计历史</button>${state.busy?'<p role="status">正在审计 / 读取…</p>':''}${state.error?`<p role="alert">${esc(state.error)}；输入与已有审计已保留，可重试。</p>`:''}<div class="condition-history">${state.history===null?'<p>尚未读取审计历史。</p>':state.history.length?state.history.map(item=>`<button type="button" data-audit-id="${esc(item.audit_id)}" ${state.busy||row?.audit_id===item.audit_id?'disabled':''}>${esc(statuses[item.result.status]||item.result.status)} · ${esc(item.audit_id.slice(0,12))}</button>`).join(''):'<p>本次检查尚无时序审计。</p>'}</div>${row?`<section aria-label="时序审计快照"><h4>${esc(statuses[row.result.status]||row.result.status)}</h4><dl><dt>观测时刻</dt><dd>${esc(row.result.observation_at||'未知')}</dd><dt>最晚输入可得时刻</dt><dd>${esc(row.result.latest_input_available_at||'未知')}</dd><dt>确认时刻</dt><dd>${esc(row.result.confirmed_at)}</dd><dt>预期执行时刻</dt><dd>${esc(row.result.expected_execution_at||'未知')}</dd></dl><ul>${row.result.reasons.map(reason=>`<li>${esc(reason)}</li>`).join('')}</ul><details><summary>原始条件计数、证据及完整不可变快照</summary><pre>${json(row)}</pre></details></section>`:''}`;
      const form = panel.querySelector('form');
      if(form) {
        form.oninput=()=>{state.input=form.elements.timing_evidence.value;};
        form.onsubmit=event=>{
          event.preventDefault();event.stopPropagation();
          state.input=form.elements.timing_evidence.value;
          run(async()=>{
            let evidence;
            try {evidence=JSON.parse(state.input);} catch {throw new Error('时序证据不是有效 JSON');}
            const saved=verify(await api('assess_strategy_timing',key,evidence));
            state.selected=saved;
            state.history=[saved,...(state.history||[]).filter(item=>item.audit_id!==saved.audit_id)];
          });
        };
      }
      panel.querySelector('[data-timing-history]').onclick=()=>run(async()=>{
        const rows=await api('strategy_timing_history',key);
        rows.forEach(verify);state.history=rows;
      });
      panel.querySelectorAll('[data-audit-id]').forEach(button=>{button.onclick=()=>run(async()=>{
        state.selected=verify(await api('get_strategy_timing',button.dataset.auditId));
      });});
    }
    async function run(action) {
      if(state.busy)return;
      state.busy=true;state.error='';render();
      try {await action();} catch(error) {state.error=error.message;}
      finally {state.busy=false;state.render();}
    }
    state.render=render;
    render();
  }};
})();
