'use strict';
(() => {
  const cache = new Map();
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const statuses = {met:'满足',not_met:'未满足',manual:'需人工判断',insufficient_data:'数据不足',stale_data:'数据已过期',not_implemented:'未实现',invalid_rule:'定义校验失败'};
  const groups = {entry:'入场',filters:'过滤',exit:'退出',invalidation:'失效',position:'仓位',portfolio:'组合',exceptions:'例外'};
  const operators = {gt:'>',ge:'≥',lt:'<',le:'≤',eq:'='};
  const json = value => escape(JSON.stringify(value, null, 2));
  function evidence(operand, data, label) {
    if (!operand) return '';
    const freshness = operand.freshness;
    const age = freshness ? `<p>观测新鲜度：${freshness.status==='not_evaluated'?'未评估（规则未配置阈值）':freshness.status==='stale'?'数据已过期':'在所设年龄范围内'} · 最新观测：${escape(freshness.latest_observed_at)} · 年龄：${escape(freshness.age_seconds)} 秒${freshness.max_age_seconds===null?'':` · 上限：${escape(freshness.max_age_seconds)} 秒`}</p>` : '';
    return `${age}<details><summary>${label}：${escape(operand.value)} ${escape(operand.unit)}</summary><p>来源：${escape(operand.source || data.source)} · 修订：${escape(data.revision)} · 价格口径：${escape(data.price_basis)}</p><pre>${json(operand)}</pre></details>`;
  }
  function snapshot(row) {
    if (!row) return '<p>尚未选择检查快照。</p>';
    return `<section aria-label="条件检查快照"><h4>${escape(row.data.symbol)} · v${row.version} · 截止 ${escape(row.as_of)}</h4><p>数据来源：${escape(row.data.source)} · 修订：${escape(row.data.revision)} · 价格口径：${escape(row.data.price_basis)}</p><p>调用者提供，来源和可得时间未经独立核验。逐条条件满足不代表整体策略通过、有效或可成交。</p><p>检查标识：${escape(row.check_id)}</p>${row.conditions.map(c => `<article><h4>${escape(groups[c.group] || c.group)} ${c.index + 1} · ${escape(statuses[c.status] || c.status)}</h4><p>${escape(c.rule.text)}</p><p>规则出处：${escape(c.rule.citation)}</p>${c.reason ? `<p>${escape(c.reason)}</p>` : `<p>${escape(c.left.value)} ${escape(c.left.unit)} ${escape(operators[c.op] || c.op)} ${escape(c.right.value)} ${escape(c.right.unit)}</p>`}${evidence(c.left,row.data,'左侧计算值及采用的观测')}${evidence(c.right,row.data,'右侧计算值及采用的观测')}</article>`).join('')}<details><summary>限制与完整不可变快照</summary><ul>${row.limitations.map(item=>`<li>${escape(item)}</li>`).join('')}</ul><pre>${json(row)}</pre></details></section>`;
  }
  window.StockSeekConditions = {snapshot, mount(container, doc, api) {
    if (!doc) return;
    const parent = container.querySelector('[aria-label="策略版本详情"]');
    if (!parent) return;
    window.StockSeekTiming?.edit(parent, doc, api, Boolean(container.querySelector('#strategy-form')));
    const key = `${doc.strategy_id}:${doc.version}`;
    if (!cache.has(key)) cache.set(key, {asOf:'',input:'',history:null,selected:null,busy:false,error:'',token:0});
    const state = cache.get(key);
    const panel = document.createElement('section');
    panel.className = 'condition-panel'; panel.setAttribute('aria-label','策略条件核对');
    parent.append(panel);
    function render() {
      if (!panel.isConnected) return;
      panel.innerHTML = `<h3>v${doc.version} 的条件核对</h3><p>离线数据检查；不自动请求行情或模型，不产生交易。过期仅按规则显式配置的最大观测年龄判断，未配置时新鲜度未评估。时序、环境与成交限制需另行核对，不自动使策略失效。</p>${doc.status === 'confirmed' ? `<form class="condition-form"><label>检查截止时刻（包含时区）<input name="as_of" placeholder="2026-11-10T15:10:00+08:00" value="${escape(state.asOf)}" required></label><label>粘贴离线数据快照（JSON）<textarea name="data" required spellcheck="false">${escape(state.input)}</textarea></label><p>数据须声明证券、市场、周期、价格口径、来源、修订和观测可得时间。格式见 docs/strategy-conditions.md；不会自动补值或生成缺失规则。</p><button type="submit" ${state.busy?'disabled':''}>保存并核对本版本条件</button></form>` : '<p>草稿尚未确认，不能执行条件检查；可查询此版本历史。</p>'}<button type="button" data-condition-action="history" ${state.busy?'disabled':''}>读取 / 刷新本版本检查历史</button>${state.busy?'<p role="status">正在核对 / 读取…</p>':''}${state.error?`<p role="alert">${escape(state.error)}；输入与已有快照已保留，可重试。</p>`:''}<div class="condition-history">${state.history === null?'<p>尚未读取历史。</p>':state.history.length?state.history.map(row=>`<button type="button" data-check-id="${escape(row.check_id)}" ${state.busy||state.selected?.check_id===row.check_id?'disabled':''}>${escape(row.as_of)} · ${escape(row.check_id.slice(0,12))}</button>`).join(''):'<p>本版本尚无检查记录。</p>'}</div>${snapshot(state.selected)}`;
      const form = panel.querySelector('form');
      if (form) {
        form.addEventListener('input', () => {state.asOf=form.elements.as_of.value;state.input=form.elements.data.value;});
        form.addEventListener('submit', event => {
          event.preventDefault(); event.stopPropagation();
          state.asOf=form.elements.as_of.value;state.input=form.elements.data.value;
          run(async () => {
            let data;
            try {data=JSON.parse(state.input);} catch {throw new Error('数据快照不是有效 JSON');}
            const row=await api('check_strategy_conditions',doc.strategy_id,doc.version,state.asOf,data);
            state.selected=row;
            // Publish success before any optional history read can fail.
            state.history=[row,...(state.history||[]).filter(item=>item.check_id!==row.check_id)];
          });
        });
      }
      panel.querySelector('[data-condition-action="history"]').onclick = () => run(async () => {state.history=await api('strategy_check_history',doc.strategy_id,doc.version);});
      panel.querySelectorAll('[data-check-id]').forEach(button => {button.onclick=()=>run(async()=>{
        const row=await api('get_strategy_check',button.dataset.checkId);
        if(row.strategy_id!==doc.strategy_id||row.version!==doc.version)throw new Error('快照不属于当前策略版本');
        state.selected=row;
      });});
      window.StockSeekTiming?.mount(panel,doc,state.selected,api);
      window.StockSeekComparisons?.mount(panel,doc,state.selected,api);
      window.StockSeekTracking?.mount(panel,doc,state.selected,api);
    }
    async function run(action) {
      if(state.busy)return;
      state.busy=true;state.error='';const token=++state.token;render();
      try {await action();} catch(error) {if(state.token===token)state.error=error.message;}
      finally {if(state.token===token){state.busy=false;state.render();}}
    }
    state.render=render;
    render();
  }};
})();
