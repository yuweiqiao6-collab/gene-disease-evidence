"""Disease-versus-normal scope checks; lexical gates are not medical validation."""
import re
from query_utils import contains_any_term,query_terms

PROFILE='disease_vs_normal_v1'
COMPARISON_VALUES={'disease_vs_normal','intervention','within_disease','other','unclear'}
SIGNIFICANCE_VALUES={'significant','not_significant','not_reported','unclear'}
NORMAL=re.compile(r'\b(?:healthy|normal|unaffected|sham|uninfected|wild[- ]type|non[- ]?(?:tumou?r|malignant|diseased?))\b',re.I)
FLAG_LABELS={
 'comparison_outside_disease_vs_normal':'比较属于干预、患者内部亚组或其他范围',
 'comparison_type_unconfirmed':'尚未明确疾病组与正常对照的比较类型',
 'comparison_quote_missing_or_not_contiguous':'比较原句缺失、无法核对或跨片段拼接',
 'disease_group_not_supported_by_comparison_quote':'疾病组名称不在所给比较原句中',
 'normal_group_not_supported_by_comparison_quote':'正常对照名称不在所给比较原句中',
 'normal_control_not_explicit':'对照名称没有明确正常、健康或正常模型含义',
 'target_disease_not_explicit_in_comparison':'比较原句未明确目标疾病或已提供别名',
 'target_gene_not_explicit_in_expression_quote':'表达原句未明确目标基因或已提供别名',
 'not_an_expression_quantity':'测量为活性或定位，不能直接作为表达量差异',
 'reference_list_source':'来源具有参考文献列表格式',
 'secondary_source_requires_original_study_check':'可能为综述转述，需要核对原始研究',
 'preprint_source_requires_review':'来源标为预印本，需要核对论文版本与研究质量'}

def normalized(value):return re.sub(r'\s+',' ',str(value or '')).strip()

def segments(chunk):
    return chunk.get('context_segments') or [chunk]

def quote_segment(quote,chunk):
    quote=normalized(quote)
    if len(quote)<12:return None
    return next((s for s in segments(chunk) if quote in normalized(s.get('context'))),None)

def bind_primary_source(raw,chunk):
    source=quote_segment(raw.get('evidence_sentence'),chunk)
    if source is None:return None
    return {**chunk,**{k:source[k] for k in ('chunk_id','page','source_location','file_name','section','paragraph') if k in source}}

def assess(row):
    """Keep unsupported scope in a review queue; never invent missing comparisons."""
    if row.get('task_profile')!=PROFILE:return row
    row=dict(row);flags=[];status='eligible'
    kind=row.get('comparison_type','unclear')
    if kind in {'intervention','within_disease','other'}:
        status='out_of_scope';flags.append('comparison_outside_disease_vs_normal')
    elif kind!='disease_vs_normal':flags.append('comparison_type_unconfirmed')
    chunk={'context':row.get('source_context',''),'context_segments':row.get('context_segments',[])}
    quote=normalized(row.get('comparison_sentence'))
    source=quote_segment(quote,chunk)
    if source is None:flags.append('comparison_quote_missing_or_not_contiguous')
    for field in ('disease_group','normal_group'):
        value=normalized(row.get(field))
        if not value or value not in quote:flags.append(field+'_not_supported_by_comparison_quote')
    if not NORMAL.search(normalized(row.get('normal_group'))):flags.append('normal_control_not_explicit')
    if not contains_any_term(quote,query_terms(row.get('disease',''),row.get('target_disease_aliases',[]))):
        flags.append('target_disease_not_explicit_in_comparison')
    if not contains_any_term(row.get('evidence_sentence',''),query_terms(row.get('gene_or_target',''),row.get('target_gene_aliases',[]))):
        flags.append('target_gene_not_explicit_in_expression_quote')
    if row.get('measurement_type') in {'activity','localization'}:
        status='out_of_scope';flags.append('not_an_expression_quantity')
    if 'reference_like' in row.get('source_quality_flags',[]):
        status='out_of_scope';flags.append('reference_list_source')
    if 'review_title' in row.get('source_quality_flags',[]):flags.append('secondary_source_requires_original_study_check')
    if 'preprint' in row.get('source_quality_flags',[]):flags.append('preprint_source_requires_review')
    if flags and status=='eligible':status='needs_scope_review'
    row.update(baseline_status=status,baseline_flags=flags,baseline_messages=[FLAG_LABELS[f] for f in flags],comparison_quote_verified=source is not None,
        comparison_source_location=source.get('source_location','') if source else '',
        comparison_page=source.get('page') if source else None,baseline_check_version='baseline-scope-v1')
    return row


def summarize(rows):
    """Descriptive grouping only; never vote on a universal gene-disease direction."""
    groups={}
    for row in rows:
        if row.get('baseline_status')!='eligible' or row.get('review_status')=='rejected':continue
        key=tuple(row.get(k,'unclear') for k in ('gene_or_target','disease','species','tissue_or_cell','measurement_type','disease_group','normal_group'))
        group=groups.setdefault(key,{'gene':key[0],'disease':key[1],'species':key[2],'tissue':key[3],
            'measurement':key[4],'disease_group':key[5],'normal_group':key[6],'papers':set(),'directions':set()})
        group['papers'].add(row['paper_id']);group['directions'].add(row['expression_direction'])
    return [{**g,'papers':sorted(g['papers']),'directions':sorted(g['directions']),
             'mixed_directions':len(g['directions']-{'unclear'})>1} for g in groups.values()]
