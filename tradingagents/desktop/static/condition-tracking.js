'use strict';
(() => {
  const cache = new Map();
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const groups = {entry:'入场',filters:'过滤',exit:'退出',invalidation:'失效',position:'仓位',portfolio:'组合',exceptions:'例外'};
  const statuses = {observing:'积累中',triggered:'本次达到阈值',sustained:'持续满足（不重复命中）',waiting_for_observation:'等待独立新观测',not_met:'未满足',paused:'暂停判断',expired:'计划已到期'};
  const reasons = {distinct_observation:'独立观测',all_metric_operands_must_advance:'所有指标操作数均须有更新观测',not_met:'条件未满足',freshness_not_evaluated:'规则未评估数据新鲜度',no_observation:'没有指标观测',observation_regressed:'观测时间倒退',manual:'需人工判断',insufficient_data:'数据不足',stale_data:'数据已过期',not_implemented:'条件未实现',invalid_rule:'规则校验失败'};
  const label = plan => `${groups[plan.group] || plan.group} ${plan.index+1} · ${plan.required_consecutive} 次 · ${plan.created_by}${plan.cancellation?' · 已停止':''}`;
  const json = value => escape(JSON.stringify(value,null,2));
  window.StockSeekTracking = {mount(parent, doc, check, api) {
    const panel = document.createElement('section');
    panel.setAttribute('aria-label','条件跟踪');
    parent.append(panel);
    const key = `${doc.strategy_id}:${doc.version}`;
    if (!cache.has(key)) cache.set(key,{condition:'',count:'2',expiry:'',by:'',plans:null,plan:null,history:null,event:null,stopBy:'',stopReason:'',busy:false,error:''});
    const state = cache.get(key);
    const belongs = plan => plan.strategy_id===doc.strategy_id && plan.version===doc.version;
    function render() {
      if (!panel.isConnected) return;
      const plan = state.plan;
      const latest = state.history?.[0];
      panel.innerHTML = `<h3>v${doc.version} 的条件跟踪</h3><p>仅在你提交检查时计算；不会自动巡检、获取行情、发送通知或生成交易。单条条件命中不代表整体策略通过。连续计数只涵盖提交的独立观测；缺失、过期或未评估新鲜度将暂停并清零。</p>${check?`<p>当前检查：${escape(check.check_id)} · ${escape(check.data.symbol)} · 截止 ${escape(check.as_of)}</p><form><fieldset ${state.busy?'disabled':''}><legend>从当前检查创建固定版本的计划</legend><label>跟踪条件<select name="condition" required><option value="">请选择一条条件</option>${check.conditions.map(c=>`<option value="${escape(c.group)}:${c.index}" ${state.condition===`${c.group}:${c.index}`?'selected':''}>${escape(groups[c.group]||c.group)} ${c.index+1} · ${escape(c.rule.text)}</option>`).join('')}</select></label><label>连续独立观测次数<input name="count" type="number" min="1" max="100" step="1" value="${escape(state.count)}" required></label><label>到期时刻（包含时区）<input name="expiry" placeholder="2026-12-01T16:00:00+08:00" value="${escape(state.expiry)}" required></label><label>计划创建人<input name="by" maxlength="200" value="${escape(state.by)}" required></label><button type="submit">创建跟踪计划（不自动评估）</button></fieldset></form>`:'<p>先在上方选择条件检查，再创建计划或提交评估；仍可读取本版本已有计划。</p>'}<button type="button" data-tracking="plans" ${state.busy?'disabled':''}>读取 / 刷新本版本跟踪计划</button>${state.busy?'<p role="status">正在读取 / 保存跟踪…</p>':''}${state.error?`<p role="alert">${escape(state.error)}；输入、计划与已有历史已保留，可重试。</p>`:''}<div class="tracking-plans">${state.plans===null?'':state.plans.length?state.plans.map(p=>`<button type="button" data-plan-id="${escape(p.plan_id)}" ${state.busy?'disabled':''} aria-pressed="${p.plan_id===plan?.plan_id}">${escape(label(p))} · ${escape(p.plan_id.slice(0,12))}</button>`).join(''):'<p>本版本尚无跟踪计划。</p>'}</div>${plan?`<section aria-label="所选跟踪计划"><h4>${escape(label(plan))}</h4><p>计划标识：${escape(plan.plan_id)}</p><p>证券：${escape(plan.context.symbol)} · 周期：${escape(plan.context.timeframe)} · 价格口径：${escape(plan.context.price_basis)} · 来源：${escape(plan.context.source)}</p><p>检查范围：[${escape(plan.starts_at)}, ${escape(plan.expires_at)})；到期以提交检查的截止时刻判断，不表示此刻仍有效。</p><p>种子检查：${escape(plan.seed_check_id)}。创建计划不计入第一次观测。</p><button type="button" data-tracking="evaluate" ${state.busy||!check||plan.cancellation?'disabled':''}>将当前检查提交给此计划</button><button type="button" data-tracking="history" ${state.busy?'disabled':''}>读取 / 刷新此计划历史</button>${plan.cancellation?`<section aria-label="停止跟踪记录"><h4>此计划已停止</h4><p>${escape(plan.cancellation.cancelled_at)} · ${escape(plan.cancellation.cancelled_by)} · ${escape(plan.cancellation.reason)}</p><p>停止后不接受新评估。下方历史命中仅代表当时结果；重试已有检查只返回原记录。</p><details><summary>完整停止记录</summary><pre>${json(plan.cancellation)}</pre></details></section>`:`<form data-tracking-stop><fieldset ${state.busy?'disabled':''}><legend>停止此计划（停止后须另建不同配置的计划）</legend><label>停止操作人<input name="stopBy" maxlength="200" value="${escape(state.stopBy)}" required></label><label>停止原因<textarea name="stopReason" maxlength="2000" required>${escape(state.stopReason)}</textarea></label><button type="submit">保存原因并停止跟踪</button></fieldset></form>`}${latest?`<p aria-label="最近跟踪状态">最近提交：${escape(latest.check.as_of)} · ${escape(statuses[latest.result.status]||latest.result.status)} · 连续 ${latest.result.consecutive}/${plan.required_consecutive} · ${latest.result.hit?'本次命中':'本次未命中'} · ${escape(reasons[latest.result.reason]||latest.result.reason)}</p>`:'<p>尚未读取历史或尚无评估，不能据此推断条件状态。</p>'}<div class="tracking-events">${state.history===null?'':state.history.length?state.history.map(row=>`<button type="button" data-event-id="${escape(row.event_id)}" ${state.busy?'disabled':''}>${escape(row.check.as_of)} · ${escape(statuses[row.result.status]||row.result.status)} · ${row.result.consecutive}/${plan.required_consecutive}</button>`).join(''):'<p>此计划尚无评估记录。</p>'}</div><details><summary>完整计划、限制及生命周期记录</summary><pre>${json(plan)}</pre></details>${state.event?`<section aria-label="跟踪历史证据"><h4>所选历史记录 · ${escape(statuses[state.event.result.status]||state.event.result.status)}</h4><p>连续 ${state.event.result.consecutive}/${plan.required_consecutive} · ${state.event.result.hit?'本次命中':'本次未命中'} · ${escape(reasons[state.event.result.reason]||state.event.result.reason)}</p>${window.StockSeekConditions.snapshot(state.event.check)}<details><summary>完整事件、水位及前序链</summary><pre>${json(state.event)}</pre></details></section>`:''}</section>`:''}`;
      const form = panel.querySelector(':scope > form');
      if (form) {
        form.oninput=()=>{state.condition=form.elements.condition.value;state.count=form.elements.count.value;state.expiry=form.elements.expiry.value;state.by=form.elements.by.value;};
        form.onsubmit=event=>{event.preventDefault();event.stopPropagation();run(async()=>{
          const [group,index] = state.condition.split(':');
          const created = await api('create_condition_tracking',check.check_id,group,Number(index),Number(state.count),state.expiry,state.by);
          if(!belongs(created))throw new Error('计划不属于当前策略版本');
          state.plan=created;state.history=null;state.event=null;state.stopBy='';state.stopReason='';
          state.plans=[created,...(state.plans||[]).filter(p=>p.plan_id!==created.plan_id)];
        });};
      }
      panel.querySelector('[data-tracking="plans"]').onclick=()=>run(async()=>{
        state.plans=(await api('list_condition_tracking')).filter(belongs);
        if(state.plan)state.plan=state.plans.find(p=>p.plan_id===state.plan.plan_id)||state.plan;
      });
      panel.querySelectorAll('[data-plan-id]').forEach(button=>{button.onclick=()=>run(async()=>{
        const selected = await api('get_condition_tracking',button.dataset.planId);
        if(!belongs(selected))throw new Error('计划不属于当前策略版本');
        state.plan=selected;state.history=null;state.event=null;state.stopBy='';state.stopReason='';
      });});
      if (plan) {
        const stopForm=panel.querySelector('[data-tracking-stop]');
        if(stopForm){
          stopForm.oninput=()=>{state.stopBy=stopForm.elements.stopBy.value;state.stopReason=stopForm.elements.stopReason.value;};
          stopForm.onsubmit=event=>{event.preventDefault();event.stopPropagation();run(async()=>{
            const cancellation=await api('cancel_condition_tracking',plan.plan_id,state.stopBy,state.stopReason);
            if(cancellation.plan_id!==plan.plan_id)throw new Error('停止记录与所选计划不一致');
            state.plan={...plan,cancellation};
            if(state.plans)state.plans=state.plans.map(p=>p.plan_id===plan.plan_id?state.plan:p);
          });};
        }
        panel.querySelector('[data-tracking="evaluate"]').onclick=()=>run(async()=>{
          if(!check)throw new Error('请先选择当前版本的条件检查');
          const row=await api('evaluate_condition_tracking',plan.plan_id,check.check_id);
          if(row.plan_id!==plan.plan_id||row.check_id!==check.check_id)throw new Error('跟踪记录与所选计划或检查不一致');
          state.event=row;
          // Old idempotent retries must not replace the newest status or imply full history.
          if(state.history!==null)state.history=[row,...state.history.filter(item=>item.event_id!==row.event_id)].sort((a,b)=>Date.parse(b.check.as_of)-Date.parse(a.check.as_of));
        });
        panel.querySelector('[data-tracking="history"]').onclick=()=>run(async()=>{
          const rows=await api('condition_tracking_history',plan.plan_id);
          if(rows.some(row=>row.plan_id!==plan.plan_id))throw new Error('历史不属于所选计划');
          state.history=rows;
        });
        panel.querySelectorAll('[data-event-id]').forEach(button=>{button.onclick=()=>{
          state.event=state.history.find(row=>row.event_id===button.dataset.eventId);render();
        };});
      }
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
