"""Chinese evidence workbench. Background workers never call Streamlit APIs."""
from __future__ import annotations
import secrets
import json
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import streamlit as st
from jobs import JobManager, Query, TERMINAL
from uploads import Upload
from web_settings import WebSettings
from schemas import EXPRESSION_VALUES, DISEASE_EFFECT_VALUES, RELATION_TYPE_VALUES, RELATION_SCOPE_VALUES, POPULATION_VALUES, MEASUREMENT_VALUES
from literature_ui import render_literature,render_documents
from config import get_llm_settings
from corpus_store import CorpusStore
from baseline import PROFILE as BASELINE_PROFILE,COMPARISON_VALUES,SIGNIFICANCE_VALUES,summarize
from scan_ui import render_coverage
from showcase import render_demo
from result_overview import render_overview
from ui_theme import apply_theme,sidebar_brand,header,comparison_groups,evidence_tags

st.set_page_config(page_title='基因—疾病证据工作台', page_icon='🧬', layout='wide')
apply_theme()


@st.cache_resource
def manager():
    return JobManager(WebSettings.from_env())


service = manager()
settings = service.settings
try:
    model_config = get_llm_settings()
    model_configured = bool(model_config['api_key'] and model_config['model'])
except (OSError, ValueError):
    model_configured = False
if 'owner' not in st.session_state:
    st.session_state.owner = secrets.token_hex(32)
    st.session_state.job_ids = []
    st.session_state.active_job = None
    st.session_state.authorized = False

LABELS = {'increased':'升高','decreased':'降低','unchanged':'未变化','unclear':'不明确',
    'risk_increase':'风险增加','risk_decrease':'风险降低','progression_increase':'进展加重',
    'progression_decrease':'进展减轻','poor_prognosis':'预后较差','better_prognosis':'预后较好',
    'biomarker_only':'仅表达差异或标志物','no_relation':'未发现所述关联',
    'associated':'相关性','causal':'实验支持的因果关系','mechanistic':'机制证据',
    'predictive':'预测关联','therapeutic_target':'治疗靶点',
    'human':'人','mouse':'小鼠','rat':'大鼠','human_cohort':'人群研究','animal_model':'动物模型',
    'cell_experiment':'细胞实验','meta_analysis':'荟萃分析',
    'synthetic_demo':'人工教学案例','pubmed_abstract':'PubMed 摘要','uploaded_pdf':'上传论文','local_pdf':'本地PDF全文'}
LABELS.update(unreviewed='待复核',accepted='接受',rejected='拒绝',corrected='已修正',
    completed='已完成',partial='部分完成',failed='失败',no_candidates='无候选',no_relations='无所述关系',
    interrupted='已中断',running='运行中',queued='排队中',paused='已暂停，可继续')
LABELS.update(direct='直接关系',indirect='间接关系',background='背景说明',
    disease_cohort='疾病研究人群',healthy_only='仅健康人',animal_disease_model='动物疾病模型',
    cell_disease_model='细胞疾病模型',mixed='混合人群或模型',transcript='转录本数量',
    total_protein='总蛋白数量',circulating_protein='循环蛋白浓度',surface_abundance='膜表面丰度',
    localization='定位或转运',activity='活性')
LABELS.update(no_relations='未提取到证据',disease_vs_normal='疾病组与正常对照',intervention='干预条件比较',
    within_disease='患者内部亚组',other='其他比较',eligible='符合范围的机器候选',needs_scope_review='比较依据待核对',
    out_of_scope='不在本版范围',significant='有统计显著性',not_significant='未见统计显著差异',not_reported='未报告统计显著性')


def translate(value):
    return LABELS.get(str(value), str(value))


def submit(query, uploads=()):
    try:
        job_id = service.submit(st.session_state.owner, query, uploads,
            authorized=st.session_state.authorized)
        st.session_state.job_ids.append(job_id)
        st.session_state.active_job = job_id
        st.session_state.task_selector = job_id
        return job_id
    except (ValueError, PermissionError) as exc:
        st.error(str(exc))
        return None


with st.sidebar:
    sidebar_brand()
    with st.expander('研究空间与历史'):
        st.caption('恢复码可访问你的历史与复核记录，请自行保存，勿公开。刷新页面或重启后可重新输入。')
        st.code(st.session_state.owner, language=None)
        with st.form('restore_workspace'):
            recovery_code = st.text_input('研究空间恢复码', type='password')
            if st.form_submit_button('恢复历史'):
                if not re.fullmatch('[a-f0-9]{64}', recovery_code.strip()):
                    st.error('恢复码应为 64 位字母数字，请检查复制是否完整。')
                else:
                    records = service.list_history(recovery_code.strip())
                    if not records and not service.literature.list(recovery_code.strip()):
                        st.warning('未找到该恢复码的历史记录。')
                    else:
                        st.session_state.owner = recovery_code.strip()
                        st.session_state.job_ids = [r['id'] for r in reversed(records)]
                        st.session_state.active_job = records[0]['id'] if records else None
                        if records: st.session_state.task_selector = records[0]['id']
                        st.session_state.pop('lit_selected',None)
                        for key in ('lit_target','lit_previous_selection','lit_model_consent',
                                    'lit_gene','lit_disease','lit_gene_aliases','lit_disease_aliases','lit_collection_scope','lit_payload_settings',
                                    'lit_preview_document','lit_previous_process_mode','scan_start_consent','scan_start_fingerprint'):
                            st.session_state.pop(key,None)
                        st.session_state.authorized = False
                        st.rerun()
    st.divider()
    st.markdown('**模型访问权限**')
    if settings.live_ready:
        st.caption('示例体验开放，真实抽取需要访问口令。')
        if not st.session_state.authorized:
            with st.form('access_form'):
                password = st.text_input('真实抽取访问口令', type='password')
                if st.form_submit_button('验证口令'):
                    st.session_state.authorized = service.authenticate(st.session_state.owner, password)
                    if st.session_state.authorized:
                        st.rerun()
                    else:
                        st.error('口令不正确或尝试过于频繁。')
        else:
            st.success('已开启本会话真实抽取')
            if st.button('退出真实抽取'):
                st.session_state.authorized = False
                st.rerun()
    else:
        st.info('真实模型抽取未启用。人工示例无需密钥；PubMed检索由独立开关控制。')
        if settings.enable_live:
            st.warning('管理员尚未设置至少 12 字符的访问口令。')
    with st.expander('使用自己的模型密钥'):
        st.write('下载项目并自行部署后，在私有 .env 配置文件中填写自己的接口地址、模型名称和API key（接口密钥）。')
        st.caption('当前使用服务端统一配置，不支持访客在公共页面临时填入个人密钥。访问口令不是模型密钥。')
        st.code('RAG_ENABLE_LIVE=true\nRAG_ENABLE_LITERATURE=true\nRAG_ACCESS_PASSWORD=\nLLM_API_KEY=\nLLM_BASE_URL=\nLLM_MODEL=',language=None)
        st.caption('以上为空值模板。访问口令至少12字符；模型接口需兼容本项目的聊天请求格式。改好配置后完整重启自己的应用进程，不要上传 .env。')
    st.divider()
    with st.expander('数据保存与证据说明'):
        st.caption('历史与复核结果持续保存，可用恢复码找回。上传原文和临时索引最长保留 24 小时，'
            '繁忙时可能提前清理；请自己备份重要原文与运行数据。')
        st.caption('批量扫描的候选文字、进度和调用检查点持续保存，删除对应任务时一起删除。')
        st.caption('科研辅助工具。引用核对通过只表示原句存在，不代表关系判断已经正确。')


try:
    local_info=CorpusStore(settings.corpus_db).fingerprint() if settings.corpus_db and settings.corpus_db.is_file() else {}
except (OSError,ValueError,RuntimeError,sqlite3.Error):
    local_info={}
header(local_info,online=settings.enable_literature,configured=model_configured,
    authorized=st.session_state.authorized,live_ready=settings.live_ready)

demo_tab,literature_tab,upload_tab=st.tabs(['人工示例体验','真实文献与模型抽取','上传论文与本地库'])
with literature_tab:
    render_literature(service,submit,model_configured=model_configured)

with demo_tab:
    render_demo(submit)
    with st.expander('更多教学案例（保留原有操作示例）'):
        examples = [('IL6','rheumatoid arthritis','IL6 · 类风湿关节炎'),
                    ('ABCC8','type 2 diabetes','ABCC8 · 2 型糖尿病'),
                    ('TP53','breast cancer','TP53 · 乳腺癌')]
        for column, (gene,disease,label) in zip(st.columns(3), examples):
            with column:
                if st.button(label, width='stretch'):
                    submit(Query((gene,),disease,task_profile=BASELINE_PROFILE))

with upload_tab:
    st.subheader('使用已有材料抽取')
    st.caption('无需再检索PubMed。支持可读取文字的PDF（便携文档格式）；预建文献库需部署者配置。')
    sources={'上传自己的论文':'upload'}
    if settings.corpus_db and settings.corpus_db.is_file():
        sources['管理员预建文献库']='corpus'
    source=sources[st.selectbox('文献来源',list(sources),key='source')]
    can_extract=settings.live_ready and st.session_state.authorized and model_configured
    if not can_extract:
        st.info('请先配置并启用模型，再在侧栏验证访问口令。这里的真实抽取会发送候选文本并可能产生费用。')
    with st.form('query_form'):
        left,right=st.columns(2)
        gene_text=left.text_input('材料抽取基因',value='IL6',help='本版每次查询一个基因与一个疾病。')
        disease=right.text_input('材料抽取疾病英文名称',value='rheumatoid arthritis',help='rheumatoid arthritis 表示类风湿关节炎。')
        top_k=st.slider('每个基因最多抽取的候选数',min_value=1,max_value=12,value=6)
        retry=st.checkbox('材料抽取失败后自动重试1次（可能增加费用）',value=False)
        skip_references=st.checkbox('跳过明显的参考文献列表片段（材料抽取）',value=True)
        st.caption('每个候选片段发起1次模型请求，勾选重试后最多2次；每次输出上限2048词元（模型计量文本长度的单位）。')
        with st.expander('补充别名（可选）'):
            gene_alias=st.text_input('材料基因英文别名，逗号分隔，仅用于单基因查询')
            disease_alias=st.text_input('材料疾病英文别名，逗号分隔')
        files=st.file_uploader('上传可读取文字的 PDF 论文',type=['pdf'],accept_multiple_files=True) if source=='upload' else []
        if source=='upload':st.caption('最多5篇，每篇10 MB（兆字节）、100页；上传内容只进入本次任务。')
        consent=st.checkbox('我确认有权处理这些文本，并同意将候选文本发送到配置的模型服务。')
        clicked=st.form_submit_button('调用模型抽取材料',type='primary',disabled=not can_extract)
    if clicked:
        genes=tuple(dict.fromkeys(g.strip() for g in gene_text.split(',') if g.strip()))
        aliases=lambda text:tuple(a.strip() for a in text.split(',') if a.strip())
        uploads=tuple(Upload(f.name,f.getvalue()) for f in (files or []))
        submit(Query(genes,disease.strip(),source,int(top_k),aliases(gene_alias),aliases(disease_alias),consent,
            model_retries=int(retry),skip_reference_like=skip_references,task_profile=BASELINE_PROFILE),uploads)


# Refresh progress while the selected task runs; leave finished review forms still.
_records_for_polling=service.list_history(st.session_state.owner)
_poll_record=next((r for r in _records_for_polling if r['id']==st.session_state.active_job),
    _records_for_polling[0] if _records_for_polling else None)
poll_results=bool(_poll_record and _poll_record['state'] not in TERMINAL)


@st.fragment(run_every=2 if poll_results else None)
def results_panel():
    service.cleanup()
    st.divider()
    st.subheader('任务进度与证据结果')
    records = service.list_history(st.session_state.owner)
    ids = [r['id'] for r in reversed(records)]
    st.session_state.job_ids = ids
    if not ids:
        st.info('从上方检索文献、选择人工示例或提交材料后，任务进度和结果会显示在这里。')
        return
    # Keep selection in session state, with each task retrieved through its owner token.
    active = st.session_state.active_job
    if active not in ids:
        active = ids[-1]
        st.session_state.active_job = active
        st.session_state.task_selector = active
    record_map = {r['id']:r for r in records}
    def task_label(task_id):
        record=record_map[task_id]
        date=datetime.fromtimestamp(record['created'],ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')
        action={'search':'文献检索','fulltext':'开放全文','extract':'关系抽取','scan':'批量覆盖扫描'}.get(record['query'].get('action','extract'),'关系抽取')
        return date+' · '+action+' · '+','.join(record['query']['genes'])+' · '+record['query']['disease']+' · '+translate(record['state'])
    if len(ids)>1:
        selected = st.selectbox('研究空间历史任务（最近 200 条）', ids, index=ids.index(active),
            format_func=task_label, key='task_selector')
        st.session_state.active_job = selected
        active = selected
    try:
        snapshot = service.snapshot(st.session_state.owner, active)
    except PermissionError:
        st.info('该任务已清理，请重新提交。')
        return
    st.caption(' / '.join(snapshot['genes'])+' · '+snapshot['disease']+' · 任务 '+active[:8])
    if snapshot['synthetic']:
        st.warning('教学演示：以下句子和标注均为人工编写，没有对应真实论文。')
    state = snapshot['state']
    if (state not in TERMINAL) != poll_results:
        st.rerun()
    if state not in TERMINAL:
        st.progress(snapshot['progress'], text=snapshot['message'])
        if snapshot['action']=='scan':render_coverage(snapshot,service,active,authorized=st.session_state.authorized,model_configured=model_configured)
        return
    if state=='completed':
        st.success(snapshot['message'])
    elif state in {'failed','partial'}:
        st.warning(snapshot['message'])
    else:
        st.info(snapshot['message'])
    with st.expander('本次运行版本与来源记录'):
        st.json(snapshot['provenance'])
    for issue in snapshot['issues']:
        st.warning(issue)
    for url in snapshot['query_urls']:
        st.link_button('查看对应来源检索', url)
    if snapshot['action']=='scan':render_coverage(snapshot,service,active,authorized=st.session_state.authorized,model_configured=model_configured)
    if snapshot['action'] in {'search','fulltext'}:
        if snapshot['provenance'].get('engine')=='original':
            from original_ui import render_ranked
            render_ranked(snapshot)
        else:
            render_documents(snapshot,service,active)
        confirm=st.checkbox('我确认删除本次文献任务记录，保留个人收藏',key='delete_literature_'+active)
        if st.button('删除文献任务记录',disabled=not confirm,key='delete_literature_button_'+active):
            service.delete_history(st.session_state.owner,active)
            st.session_state.active_job=None
            st.rerun()
        return
    baseline_task=snapshot['provenance'].get('task_profile')==BASELINE_PROFILE
    if baseline_task:render_overview(snapshot,translate)
    with st.expander('本次处理统计与调用记录'):
        counts = snapshot['counts']
        a,b,c = st.columns(3)
        a.metric('候选片段', counts.get('candidates',0))
        b.metric('通过原句核对的证据', counts.get('evidence',0))
        c.metric('剔除的无依据引用', counts.get('rejected_quotes',0))
    rows = snapshot['rows']
    st.subheader('逐条证据与原文')
    baseline_only=False
    if baseline_task:
        st.caption('任务范围：疾病状态相对正常对照。比较组与原句检查通过仍需人工判断，不是医学认证。')
        st.write(f"符合范围{sum(r.get('baseline_status')=='eligible' for r in rows)}条；比较依据待核对{sum(r.get('baseline_status')=='needs_scope_review' for r in rows)}条；范围外{sum(r.get('baseline_status')=='out_of_scope' for r in rows)}条。")
        view=st.radio('结果范围',['符合范围的机器候选','比较依据待核对','不在本版范围','全部输出'],horizontal=True,key='baseline_view_'+active)
        selected_status={'符合范围的机器候选':'eligible','比较依据待核对':'needs_scope_review','不在本版范围':'out_of_scope'}.get(view)
        display_rows=[r for r in rows if not selected_status or r.get('baseline_status')==selected_status]
        if not rows:st.info('模型未输出适用证据。可能是片段不足或没有符合范围的比较，不代表基因与疾病无关。')
        elif not display_rows:st.info('当前范围暂无结果，可切换到待核对区或全部输出查看原因。')
        with st.expander('查看机器候选的条件分组（包含未复核结果）'):
            groups=summarize(rows)
            if groups:
                st.dataframe([{'物种':translate(g['species']),'组织或细胞':g['tissue'],'测量对象':translate(g['measurement']),
                    '疾病组':g['disease_group'],'正常对照':g['normal_group'],
                    '观察方向':'、'.join(translate(v) for v in g['directions']),'论文记录数':len(g['papers']),
                    '方向是否不一致':'是' if g['mixed_directions'] else '否'} for g in groups],hide_index=True,width='stretch')
            else:st.caption('暂无符合范围的候选。')
            st.caption('只按当前字段整理，不进行多数投票或医学综合判断；未知研究条件不能当作已确认相同。')
    else:
        display_rows=rows
        st.caption('这是旧任务范围的历史记录，尚未按疾病—正常对照规则重新评定。')
    if not snapshot['synthetic']:
        st.caption(f"本次模型请求{counts.get('model_calls',0)}次；失败请求{counts.get('failed_requests',0)}次。")
        usage=snapshot['provenance'].get('model_usage',{})
        if usage:
            st.caption(f"服务商返回的用量：输入{usage.get('prompt_tokens',0)}词元，输出{usage.get('completion_tokens',0)}词元，总计{usage.get('total_tokens',0)}词元。失败或中断的请求可能缺少用量，此处不是完整账单。")
        coverage=snapshot['provenance'].get('selected_document_coverage',[])
        if coverage:
            missing=sum(c['candidate_count']==0 for c in coverage)
            if missing:st.warning(f'所选文献中有{missing}篇没有片段进入模型；本次不能判断这些论文是否存在目标关系。')
            with st.expander('所选论文实际进入模型的候选范围'):
                st.dataframe([{'论文':c['title'],'候选片段数':c['candidate_count']} for c in coverage],hide_index=True,width='stretch')
                st.caption('候选范围表示交给抽取流程的片段；缓存命中不产生新请求。片段被选中或原句存在，都不代表论文结论得到医学确认。')
    if counts.get('support_flagged'):
        st.caption(f"{counts['support_flagged']} 条结果存在自动核查提示；原句存在与关系正确是两回事。")
    if display_rows:
        col_a,col_b,col_c = st.columns(3)
        species = col_a.selectbox('研究物种', ['全部']+sorted(set(r['species'] for r in display_rows)),
            format_func=translate, key='species_'+active)
        expression = col_b.selectbox('表达方向', ['全部']+sorted(set(r['expression_direction'] for r in display_rows)),
            format_func=translate, key='expression_'+active)
        review_status = col_c.selectbox('复核状态',['全部','unreviewed','accepted','corrected','rejected'],
            format_func=translate,key='review_status_'+active)
        visible = [r for r in display_rows if (species=='全部' or r['species']==species)
            and (expression=='全部' or r['expression_direction']==expression)
            and (review_status=='全部' or r['review_status']==review_status)]
        for number,row in enumerate(visible,1):
            direction_label='未见显著差异' if baseline_task and row.get('significance')=='not_significant' and row['expression_direction']=='unchanged' else translate(row['expression_direction'])
            title = f"{number}. {row['gene_or_target']} · {direction_label}"+(' · 疾病组相对正常对照' if baseline_task else ' · '+translate(row['disease_effect']))
            with st.expander(title, expanded=number==1):
                if baseline_task:evidence_tags(row,direction_label,translate)
                st.text(row['evidence_sentence'])
                st.caption('来源：'+row['title']+' / '+row['source_location'])
                if baseline_task:
                    comparison_groups(row.get('disease_group'),row.get('normal_group'))
                    st.write('比较范围：'+translate(row.get('baseline_status','needs_scope_review'))+'；统计说明：'+translate(row.get('significance','unclear')))
                    if row.get('comparison_sentence') == row['evidence_sentence']:
                        st.caption('这条表达原句同时给出了比较双方。')
                    elif row.get('comparison_sentence'):
                        st.text('比较依据原句：'+row['comparison_sentence'])
                    if row.get('comparison_source_location'):st.caption('比较原句位置：'+row['comparison_source_location'])
                    if row.get('baseline_messages'):st.warning('；'.join(row['baseline_messages']))
                if 'reference_like' in row.get('source_quality_flags',[]):st.warning('此来源片段具有参考文献列表格式，不能直接当作该论文的研究结果。')
                if 'review_title' in row.get('source_quality_flags',[]):st.caption('标题提示这可能是综述或汇总研究，请核对它与原始研究的对应关系。')
                st.caption('证据范围：'+{'abstract':'摘要','fulltext':'正文段落','snippet':'来源片段'}.get(row.get('content_level'),'来源片段'))
                if row['source_url']:
                    st.link_button('打开原文记录', row['source_url'])
                st.write('研究物种：'+translate(row['species'])+'；组织或细胞：'+row['tissue_or_cell'])
                if not baseline_task:st.write('关系：'+translate(row['relation_type']))
                st.write('研究类型：'+translate(row['evidence_type']))
                st.write('研究人群：'+translate(row.get('population_context','unclear')))
                st.write('测量对象：'+translate(row.get('measurement_type','unclear')))
                if row.get('measurement_hint') not in {None,'unclear'}:
                    st.caption('词汇检查线索：'+translate(row['measurement_hint'])+'，仍需核对具体测量。')
                if row.get('support_messages'):
                    st.warning('自动核查提示：'+'；'.join(row['support_messages']))
                with st.expander('详细支持依据与机器备注'):
                    st.caption('原句存在不证明解释正确；自动提示保留机器输出时的状态，人工修正不会抹掉原记录。')
                    if row.get('support_sentence'):st.text('关系支持原句：'+row['support_sentence'])
                    if row.get('measurement_sentence'):st.text('测量支持原句：'+row['measurement_sentence'])
                    st.text(row['notes'])
                if row['review_status'] in {'accepted','corrected'}:
                    st.info('已保存人工复核；请结合研究条件使用，不代表经过独立医学验证。')
                elif row['review_status']=='rejected':
                    st.warning('此条证据已被人工拒绝，不会进入“仅导出人工接受或修正”的结果。')
                else:
                    st.info('原句核对通过；关系、物种和适用条件仍需人工复核。')
                with st.expander('阅读保留的原文上下文'):
                    st.text(row['source_context'])
                st.write('人工复核：'+translate(row['review_status']))
                if row['reviewer']:
                    st.caption('复核人：'+row['reviewer']+'；理由：'+row['review_reason'])
                with st.expander('核对或修正这条证据'):
                    widget_key=active+'_'+row['evidence_id']+'_'+str(row['review_revision'])
                    decision=st.selectbox('复核决定',['unreviewed','accepted','rejected','corrected'],
                        index=['unreviewed','accepted','rejected','corrected'].index(row['review_status']),
                        format_func=translate,key='decision_'+widget_key)
                    with st.form('review_'+widget_key):
                        reviewer=st.text_input('复核人（姓名或代号）',value=row['reviewer'])
                        reason=st.text_area('复核理由',value=row['review_reason'])
                        changes={}
                        if decision=='corrected':
                            if baseline_task:
                                for field,label,allowed in [('comparison_type','修正比较类型',COMPARISON_VALUES),('significance','修正统计说明',SIGNIFICANCE_VALUES)]:
                                    choices=sorted(allowed);value=st.selectbox(label,choices,index=choices.index(row.get(field,'unclear')),format_func=translate)
                                    if value!=row['machine_result'].get(field,'unclear'):changes[field]=value
                                for field,label in [('disease_group','修正疾病组（复制原文名称）'),('normal_group','修正正常对照（复制原文名称）'),('comparison_sentence','修正比较原句（复制连续原文）')]:
                                    value=st.text_area(label,value=row.get(field,''))
                                    if value!=row['machine_result'].get(field,''):changes[field]=value
                            for field,label,allowed in [
                                ('expression_direction','修正表达方向',EXPRESSION_VALUES),
                                ('disease_effect','修正疾病关系',DISEASE_EFFECT_VALUES),
                                ('relation_type','修正关系类型',RELATION_TYPE_VALUES),
                                ('relation_scope','修正关系范围',RELATION_SCOPE_VALUES),
                                ('population_context','修正研究人群',POPULATION_VALUES),
                                ('measurement_type','修正测量对象',MEASUREMENT_VALUES)]:
                                if baseline_task and field in {'disease_effect','relation_type','relation_scope'}:continue
                                choices=sorted(allowed)
                                value=st.selectbox(label,choices,index=choices.index(row.get(field,'unclear')),format_func=translate)
                                if value!=row['machine_result'].get(field,'unclear'): changes[field]=value
                            for field,label in [('species','修正物种'),('tissue_or_cell','修正组织或细胞'),('evidence_type','修正研究类型')]:
                                value=st.text_input(label,value=row[field])
                                if value!=row['machine_result'][field]: changes[field]=value
                        if st.form_submit_button('保存复核'):
                            try:
                                service.review(st.session_state.owner,active,row['evidence_id'],decision,
                                    changes=changes,reason=reason,reviewer=reviewer,expected_revision=row['review_revision'])
                            except (ValueError,PermissionError) as exc:
                                st.error(str(exc))
                            else:
                                st.rerun()
                    with st.expander('保留的机器原始结果'):
                        st.json(row['machine_result'])
        if not visible:
            st.info('当前筛选条件下没有证据。')
        table = [{'基因':r['gene_or_target'],'疾病':r['disease'],'表达方向':translate(r['expression_direction']),
            '疾病关系':translate(r['disease_effect']),'物种':translate(r['species']),'原句':r['evidence_sentence'],
            '来源位置':r['source_location'],'复核状态':translate(r['review_status']),
            '测量对象':translate(r.get('measurement_type','unclear')),
            '关系范围':translate(r.get('relation_scope','unclear'))} for r in visible]
        if baseline_task:
            for item,r in zip(table,visible):
                item.pop('疾病关系',None);item.pop('关系范围',None)
                if r.get('significance')=='not_significant' and r['expression_direction']=='unchanged':item['表达方向']='未见显著差异'
                item.update(疾病组=r.get('disease_group',''),正常对照=r.get('normal_group',''),比较原句=r.get('comparison_sentence',''),
                    比较状态=translate(r.get('baseline_status','needs_scope_review')),统计说明=translate(r.get('significance','unclear')))
        st.dataframe(pd.DataFrame(table), width='stretch', hide_index=True)
    st.subheader('导出本次整理结果')
    confirmed_only=st.checkbox('仅导出人工接受或修正的证据',key='confirmed_'+active)
    if baseline_task:baseline_only=st.checkbox('只导出符合疾病—正常对照范围的候选',value=True,key='baseline_export_'+active)
    st.caption('下载范围由上方导出选项决定，不跟随物种或表达方向筛选。结构化下载保留机器原值与复核信息。')
    for column,(filename,label,mime) in zip(st.columns(4),[
        ('evidence.xlsx','下载电子表格（Excel）','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
        ('evidence.csv','下载 CSV 文本表格','text/csv'),
        ('evidence.json','下载 JSON 结构化数据','application/json'),
        ('status.json','下载处理状态','application/json')]):
        try:
            data = service.download(st.session_state.owner,active,filename,confirmed_only=confirmed_only,baseline_only=baseline_only)
        except FileNotFoundError:
            continue
        with column:
            st.download_button(label,data,file_name=active[:8]+'_'+filename,mime=mime,key=active+filename)
    with st.expander('复核修改记录与历史管理'):
        audit=service.review_audit(st.session_state.owner,active)
        if audit:
            st.json(audit)
            st.download_button('下载复核修改记录',service.download(st.session_state.owner,active,'reviews.json'),
                file_name=active[:8]+'_reviews.json',mime='application/json',key='audit_'+active)
        else:
            st.caption('尚未保存人工复核。')
        confirm_delete=st.checkbox('我确认删除本任务历史、复核记录和剩余临时文件',key='delete_check_'+active)
        if st.button('删除本任务',disabled=not confirm_delete,key='delete_'+active):
            service.delete_history(st.session_state.owner,active)
            st.session_state.active_job=None
            st.rerun()


results_panel()
st.caption('RAG（检索增强生成）：检索资料后再交给模型抽取。独立检索只获取文献；模型抽取需单独启用。')
