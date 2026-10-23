from __future__ import annotations

import math

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


ExpressionDirection = Literal["increased", "decreased", "unchanged", "unclear"]
DiseaseEffect = Literal[
    "risk_increase",
    "risk_decrease",
    "progression_increase",
    "progression_decrease",
    "poor_prognosis",
    "better_prognosis",
    "biomarker_only",
    "no_relation",
    "unclear",
]
RelationType = Literal[
    "associated",
    "causal",
    "mechanistic",
    "predictive",
    "therapeutic_target",
    "unclear",
]

EXPRESSION_VALUES = {"increased", "decreased", "unchanged", "unclear"}
DISEASE_EFFECT_VALUES = {
    "risk_increase",
    "risk_decrease",
    "progression_increase",
    "progression_decrease",
    "poor_prognosis",
    "better_prognosis",
    "biomarker_only",
    "no_relation",
    "unclear",
}
RELATION_TYPE_VALUES = {
    "associated",
    "causal",
    "mechanistic",
    "predictive",
    "therapeutic_target",
    "unclear",
}

RELATION_SCOPE_VALUES={'direct','indirect','background','unclear'}
POPULATION_VALUES={'disease_cohort','healthy_only','animal_disease_model','cell_disease_model','mixed','unclear'}
MEASUREMENT_VALUES={'transcript','total_protein','circulating_protein','surface_abundance','localization','activity','unclear'}


@dataclass
class PageRecord:
    paper_id: str
    file_name: str
    page: int
    text: str
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChunkRecord:
    chunk_id: str
    paper_id: str
    file_name: str
    source_type: str
    page: int | None
    section: str
    sentence: str
    context: str
    source_location: str
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExtractionResult:
    paper_id: str = ""
    title: str = ""
    gene_or_target: str = ""
    disease: str = ""
    species: str = "unclear"
    tissue_or_cell: str = "unclear"
    expression_direction: str = "unclear"
    disease_effect: str = "unclear"
    relation_type: str = "unclear"
    evidence_type: str = "unclear"
    statistical_evidence: dict[str, Any] = field(default_factory=dict)
    evidence_sentence: str = ""
    source_location: str = ""
    confidence: float = 0.0
    needs_review: bool = True
    notes: str = ""
    relation_scope: str = 'unclear'
    population_context: str = 'unclear'
    measurement_type: str = 'unclear'
    support_sentence: str = ''
    measurement_sentence: str = ''
    comparison_type: str = 'unclear'
    disease_group: str = ''
    normal_group: str = ''
    comparison_sentence: str = ''
    significance: str = 'unclear'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_result(raw: dict[str, Any]) -> ExtractionResult:
    result = ExtractionResult()
    for key in result.to_dict():
        if key in raw and raw[key] is not None:
            setattr(result, key, raw[key])

    if not isinstance(result.expression_direction, str) or result.expression_direction not in EXPRESSION_VALUES:
        result.expression_direction = "unclear"
    if not isinstance(result.disease_effect, str) or result.disease_effect not in DISEASE_EFFECT_VALUES:
        result.disease_effect = "unclear"
    if not isinstance(result.relation_type, str) or result.relation_type not in RELATION_TYPE_VALUES:
        result.relation_type = "unclear"
    for key,allowed in [('relation_scope',RELATION_SCOPE_VALUES),('population_context',POPULATION_VALUES),('measurement_type',MEASUREMENT_VALUES)]:
        value=getattr(result,key)
        if not isinstance(value,str) or value not in allowed: setattr(result,key,'unclear')

    try:
        result.confidence = float(result.confidence)
    except (TypeError, ValueError):
        result.confidence = 0.0
    result.confidence = max(0.0, min(1.0, result.confidence)) if math.isfinite(result.confidence) else 0.0
    for key in ("paper_id", "title", "gene_or_target", "disease", "species", "tissue_or_cell",
                "evidence_type", "evidence_sentence", "source_location", "notes","support_sentence","measurement_sentence"):
        value = getattr(result, key)
        setattr(result, key, value if isinstance(value, str) else "")
    result.needs_review = result.needs_review is not False

    if not isinstance(result.statistical_evidence, dict):
        result.statistical_evidence = {}
    from baseline import COMPARISON_VALUES,SIGNIFICANCE_VALUES
    if not isinstance(result.comparison_type,str) or result.comparison_type not in COMPARISON_VALUES:result.comparison_type='unclear'
    if not isinstance(result.significance,str) or result.significance not in SIGNIFICANCE_VALUES:result.significance='unclear'
    for field in ('disease_group','normal_group','comparison_sentence'):
        if not isinstance(getattr(result,field),str):setattr(result,field,'')

    return result

