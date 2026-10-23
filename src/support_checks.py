"""Conservative lexical review hints; never certify or rewrite biomedical claims."""
import re
from query_utils import contains_any_term,query_terms
from text_utils import split_sentences,SENTENCE_BOUNDARY_RE

CHECK_VERSION='support-hints-v3-coverage'
FLAG_LABELS={
    'target_gene_missing':'引用或支持原句中未明确出现目标基因，请核对指代',
    'disease_only_in_context':'疾病只在上下文出现，需核对该结果是否适用于目标疾病',
    'target_disease_missing':'输入片段未明确出现目标疾病或提供的别名',
    'scope_unclear':'关系范围未明确，不能只凭共现视为直接关系',
    'indirect_or_background':'输出标记为间接关系或背景说明',
    'model_notes_indirect':'模型备注提示非直接关系，请核对实际支持对象',
    'support_quote_missing':'尚无可核对的关系支持原句',
    'support_quote_invalid':'新增关系支持原句无法逐字对应输入',
    'support_gene_not_explicit':'关系支持原句未明确出现目标基因或提供的别名，请结合表达原句和上下文核对指代；不自动补写引文',
    'support_disease_not_explicit':'关系支持原句未明确出现目标疾病或提供的别名，请核对疾病模型或人群范围；不代表该结果一定错误',
    'healthy_population_hint':'出现健康受试者线索，尚未确认目标疾病研究人群',
    'population_unclear':'研究人群或模型未明确',
    'measurement_unclear':'测量对象未明确，需区分表达、丰度、定位和活性',
    'measurement_quote_invalid':'测量支持原句无法逐字对应输入',
    'measurement_quote_missing':'已标注测量类型，但缺少对应测量原句',
    'measurement_type_unsubstantiated':'测量原句中未找到与目标基因及该测量类型对应的明确词汇线索；模型标注仍需核对，不能仅凭“基因表达”推断为转录本或蛋白',
    'surface_measurement_hint':'引用涉及膜表面丰度，不应直接当作总表达量',
    'localization_expression_distinction':'上下文同时涉及总表达与定位/膜表面变化，需分开核对',
    'negative_endpoint_needs_review':'否定结论需核对具体基因、疾病和终点；信息不足不等于无关联',
    'non_expression_measurement':'定位或活性是独立测量，不应直接当作表达量变化',
}

# These are review cues, not assay identification or biomedical certification.
# Keep model values intact even when a cue is absent; pronouns and uncommon
# measurement descriptions may require a human to resolve them.
MEASUREMENT_CUES={
    'transcript':r'\b(?:mRNA|messenger\s+RNA|transcripts?|RNA\s+(?:levels?|abundance|quantit(?:y|ies)))\b',
    'total_protein':r'\bprotein\b',
    'circulating_protein':r'\b(?:serum|plasma|circulating)\b[^.!?]{0,100}\b(?:protein|concentrations?|levels?|abundance)\b',
    'surface_abundance':r'\b(?:sarcolemmal|sarcolemma|cell[- ]surface|plasma membrane|surface abundance)\b',
    'localization':r'\b(?:redistribution|trafficking|translocation|localization|sarcolemmal|sarcolemma)\b',
    'activity':r'\b(?:activity|activities|activation|catalytic)\b',
}


def normalized(text): return re.sub(r'\s+',' ',str(text or '')).strip()


def _measurement_has_cue(measurement,measurement_type,gene_terms):
    """Require a cue in a target-containing quote sentence, never model notes."""
    pattern=MEASUREMENT_CUES.get(measurement_type)
    return bool(pattern and any(contains_any_term(sentence,gene_terms) and re.search(pattern,sentence,re.I)
        for sentence in SENTENCE_BOUNDARY_RE.split(measurement)))


def check_support(row,context,gene,disease,gene_aliases=None,disease_aliases=None):
    text=normalized(context)
    quote=normalized(row.get('evidence_sentence'))
    support=normalized(row.get('support_sentence'))
    measurement=normalized(row.get('measurement_sentence'))
    support_valid=bool(len(support)>=12 and support in text)
    measurement_valid=bool(len(measurement)>=12 and measurement in text)
    grounded_support=support if support_valid else ''
    grounds=' '.join([quote,grounded_support])
    flags=[]
    gene_terms=query_terms(gene,gene_aliases)
    disease_terms=query_terms(disease,disease_aliases)
    if not contains_any_term(grounds,gene_terms): flags.append('target_gene_missing')
    if not contains_any_term(grounds,disease_terms):
        flags.append('disease_only_in_context' if contains_any_term(text,disease_terms) else 'target_disease_missing')
    scope=row.get('relation_scope','unclear')
    if scope in {'indirect','background'}: flags.append('indirect_or_background')
    elif scope=='unclear': flags.append('scope_unclear')
    if re.search(r'\bnot (?:a )?direct(?:ly)?\b',str(row.get('notes','')),re.I): flags.append('model_notes_indirect')
    if not support: flags.append('support_quote_missing')
    elif not support_valid: flags.append('support_quote_invalid')
    else:
        # Check this field independently: a gene in evidence_sentence must not
        # hide its absence from the relation-support passage, or vice versa.
        if not contains_any_term(support,gene_terms): flags.append('support_gene_not_explicit')
        if not contains_any_term(support,disease_terms): flags.append('support_disease_not_explicit')
    healthy=bool(re.search(r'\bhealthy (?:individuals|volunteers|subjects|participants)\b',text,re.I))
    disease_cohort=any(contains_any_term(sentence,disease_terms) and re.search(r'\b(?:patients?|cases?|cohort)\b',sentence,re.I)
        for sentence in split_sentences(text))
    if row.get('population_context')=='healthy_only' or (healthy and not disease_cohort):
        flags.append('healthy_population_hint')
    if row.get('population_context','unclear')=='unclear': flags.append('population_unclear')
    surface=bool(re.search(r'\b(?:sarcolemmal|sarcolemma|cell[- ]surface|plasma membrane|surface abundance)\b',quote,re.I))
    localization=bool(re.search(r'\b(?:redistribution|trafficking|translocation|localization|sarcolemmal|sarcolemma)\b',text,re.I))
    expression_distinction=bool(re.search(r'\bnot\b[^.!?]{0,100}\b(?:altered|changed|increased|decreased) expression\b',text,re.I))
    measurement_hint='surface_abundance' if surface else ('localization' if localization else 'unclear')
    if row.get('measurement_type','unclear')=='unclear': flags.append('measurement_unclear')
    elif not measurement: flags.append('measurement_quote_missing')
    if measurement and not measurement_valid: flags.append('measurement_quote_invalid')
    if measurement_valid and row.get('measurement_type','unclear')!='unclear':
        if not _measurement_has_cue(measurement,row['measurement_type'],gene_terms):
            flags.append('measurement_type_unsubstantiated')
    if surface: flags.append('surface_measurement_hint')
    if localization and expression_distinction: flags.append('localization_expression_distinction')
    if row.get('disease_effect')=='no_relation': flags.append('negative_endpoint_needs_review')
    if row.get('measurement_type') in {'localization','activity'}: flags.append('non_expression_measurement')
    return {'support_check_version':CHECK_VERSION,'support_status':'flagged' if flags else 'unassessed',
        'support_flags':flags,'support_messages':[FLAG_LABELS[k] for k in flags],
        'support_quote_verified':support_valid,'measurement_quote_verified':measurement_valid,
        'measurement_hint':measurement_hint,'relationship_validated':False}
