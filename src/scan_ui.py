"""Budgeted scan controls, coverage and explicit continuation consent."""
import json
import streamlit as st
from baseline import PROFILE
from jobs import Query,TERMINAL
from scan_state import PAPER_LABELS,SCOPE_LABELS


def render_start(service,submit,papers,target,scope,can_extract):
    st.write(f'本次扫描候选清单共{len(papers)}篇；自动按每批5篇依次处理，不要求在上方逐篇勾选。')
    st.caption('批次大小只控制处理顺序。达到本轮篇数或请求上限会暂停，可保存后继续；未处理项不会算成没有证据。')
    per_paper=st.slider('扫描每篇最多新候选片段组',1,6,2,key='scan_per_paper')
    a,b=st.columns(2)
    paper_budget=a.number_input('本轮最多处理篇数',min_value=1,max_value=50,value=min(20,max(1,len(papers))),step=1,key='scan_papers')
    request_budget=b.number_input('本轮最多请求名额',min_value=1,max_value=120,value=40,step=1,key='scan_requests')
    fetch=st.checkbox('缺少正文时，尝试获取可用开放正文（不调用模型）',value=False,
        disabled=not service.settings.enable_literature or scope!='auto',key='scan_fetch')
    effective_fetch=bool(fetch and service.settings.enable_literature and scope=='auto')
    st.caption(f'每组正文最多9000字符、每次输出最多2048词元（模型计量文本长度的单位）。本轮最多{int(request_budget)}次模型请求，不自动重试；缓存命中可能更少。')
    st.caption('请求名额按每次片段处理保守扣减，缓存命中也占一个名额；实际请求次数和已获得用量另行记录，不能代替服务商账单。')
    fingerprint=(PROFILE,json.dumps(target,sort_keys=True),scope,int(per_paper),int(paper_budget),int(request_budget),effective_fetch,
        tuple((p['document_id'],p.get('abstract_sha256'),p.get('fulltext_sha256'),p.get('local_snapshot_sha256')) for p in papers))
    if fingerprint!=st.session_state.get('scan_start_fingerprint'):
        st.session_state.scan_start_consent=False;st.session_state.scan_start_fingerprint=fingerprint
    consent=st.checkbox('我同意按上述上限扫描候选并外发文字，了解可能产生模型费用。',key='scan_start_consent')
    if st.button('开始自动分批扫描',type='primary',disabled=not papers or not target or not can_extract or not consent):
        job=submit(Query((target['gene'],),target['disease'],source='library',action='scan',
            document_ids=tuple(p['document_id'] for p in papers),top_k=int(per_paper),evidence_scope=scope,
            gene_aliases=target['gene_aliases'],disease_aliases=target['disease_aliases'],task_profile=PROFILE,
            skip_reference_like=target.get('skip_reference_like',False),model_retries=0,consent=consent,
            scan_paper_budget=int(paper_budget),scan_request_budget=int(request_budget),scan_fetch_fulltext=effective_fetch))
        if job:
            st.session_state.scan_start_fingerprint=None
            st.rerun()


def render_coverage(snapshot,service,active,*,authorized=False,model_configured=False):
    documents=snapshot['documents'];scan=snapshot['provenance'].get('scan',{});coverage=scan.get('coverage',{})
    st.write(f"候选{coverage.get('total',len(documents))}篇 · 未完成{coverage.get('pending',0)}篇 · 摘要不足待查正文{coverage.get('needs_fulltext',0)}篇 · 失败或中断{coverage.get('failed',0)}篇 · 缺文字或未命中{coverage.get('no_text',0)}篇")
    st.caption(f"已处理范围：仅摘要{coverage.get('abstract_only',0)}篇，含正文候选{coverage.get('body_processed',0)}篇。处理过候选片段不等于读完整篇，更不代表已检索全部相关研究。")
    if coverage.get('needs_context'):st.caption(f"另有{coverage['needs_context']}篇当前片段不足或无新增命中，保留为待补查或人工核对。")
    if scan.get('unknown_request_outcomes'):st.warning('有中断调用的结果或计费情况不确定；默认不会重发。')
    if documents:
        st.dataframe([{'论文编号':d['paper_id'],'标题':d['title'],'处理状态':('已部分处理，待继续' if d['status']=='pending' and d['successful_chunks'] else PAPER_LABELS.get(d['status'],d['status'])),
            '已处理范围':SCOPE_LABELS.get(d['scope'],d['scope']),'成功片段组':d['successful_chunks'],
            '待处理片段组':d['remaining_chunks'],'失败片段组':d['failed_chunks'],'符合范围候选':d['eligible'],
            '说明':'；'.join(d['messages'])} for d in documents],hide_index=True,width='stretch',height=min(380,40+35*len(documents)))
    runs=scan.get('runs',[])
    if runs:
        last=runs[-1]
        st.caption(f"第{last['number']}轮上限{last['paper_budget']}篇、{last['request_budget']}个请求名额；本轮已开始{last['papers_started']}篇、使用{last['slots_used']}个名额。")
    if snapshot['state'] not in TERMINAL:
        if st.button('暂停扫描并保存进度',key='pause_'+active):
            try:service.pause_scan(st.session_state.owner,active)
            except (ValueError,PermissionError) as exc:st.error(str(exc))
            else:st.rerun()
        st.caption('暂停会等待当前请求结束，无法撤回已经发送的文字或费用。')
        return
    with st.expander('继续未完成扫描或重试未解决项',expanded=snapshot['state']=='paused'):
        a,b=st.columns(2)
        papers=a.number_input('继续这轮最多处理篇数',1,50,20,key='resume_papers_'+active)
        requests=b.number_input('继续这轮最多请求名额',1,120,40,key='resume_requests_'+active)
        retry=st.checkbox('同时重试失败、中断、摘要不足或待补查项（可能再次计费）',key='retry_scan_'+active)
        include_body=st.checkbox('继续时使用本地或已获取的正文',value=snapshot['provenance'].get('evidence_scope')=='auto',key='resume_body_'+active)
        fetch=st.checkbox('继续时尝试获取缺少的开放正文（不调用模型）',value=bool(snapshot['provenance'].get('fetch_open_fulltext')),
            disabled=not include_body or not service.settings.enable_literature,key='resume_fetch_'+active)
        scope='auto' if include_body else 'abstract';fetch=bool(fetch and include_body and service.settings.enable_literature)
        st.caption('默认仅处理尚未完成的候选组，跳过已成功组；勾选重试后可补查摘要不足或当前片段未检出的论文。新组可能含相邻重复文字。模型设置和任务规则必须与原扫描一致。')
        summaries={p['document_id']:p for p in service.literature.list(st.session_state.owner,limit=1000)}
        revision=(len(runs),int(papers),int(requests),bool(retry),scope,fetch,tuple((d['document_id'],
            summaries.get(d['document_id'],{}).get('abstract_sha256'),summaries.get(d['document_id'],{}).get('fulltext_sha256'),
            summaries.get(d['document_id'],{}).get('local_snapshot_sha256')) for d in documents))
        key='resume_consent_'+active
        if revision!=st.session_state.get('resume_settings_'+active):
            st.session_state[key]=False;st.session_state['resume_settings_'+active]=revision
        consent=st.checkbox('我同意本次继续扫描的上限、文字外发及可能产生的新费用。',key=key)
        available=service.settings.live_ready and authorized and model_configured
        if st.button('按本轮上限继续扫描',disabled=not consent or not available,key='resume_'+active):
            try:service.resume_scan(st.session_state.owner,active,paper_budget=int(papers),request_budget=int(requests),
                consent=consent,authorized=authorized,retry_unresolved=retry,evidence_scope=scope,fetch_fulltext=fetch)
            except (ValueError,PermissionError) as exc:st.error(str(exc))
            else:st.rerun()
    st.download_button('下载逐篇覆盖与调用记录',service.download(st.session_state.owner,active,'coverage.json'),
        file_name=active[:8]+'_coverage.json',mime='application/json',key='coverage_'+active)
