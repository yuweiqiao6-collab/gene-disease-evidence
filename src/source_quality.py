"""Conservative text-source hints, never a biological relation classifier."""
import re


def source_flags(chunk):
    text=chunk.get('context','')
    dois=len(re.findall(r'(?:doi\s*:\s*|doi\.org/)10\.',text,re.I))
    numbered=len(re.findall(r'(?:^|\s)\d{1,3}\.\s+[A-Z][A-Za-z\u00c0-\u024f-]+\b',text))
    heading=bool(re.match(r'^\s*(?:references|bibliography|literature cited|参考文献)\b',text,re.I))
    flags=[]
    if (dois>=4 and numbered>=3) or (heading and numbered>=2):flags.append('reference_like')
    types=chunk.get('publication_types',[])
    if re.search(r'\b(?:review|meta-analysis|systematic review)\b',chunk.get('title',''),re.I) or (isinstance(types,list) and any(re.search(r'\b(?:review|meta-analysis)\b',str(t),re.I) for t in types)):
        flags.append('review_title')
    if chunk.get('publication_status')=='preprint' or (isinstance(types,list) and any('preprint' in str(t).lower() for t in types)):
        flags.append('preprint')
    return flags


def annotate_source(chunk):
    return {**chunk,'source_quality_flags':source_flags(chunk)}
