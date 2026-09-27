"""Индекс доверия 0–100.

Trust = 100 × Σ(wᵢ·sᵢ) / Σwᵢ, где wᵢ — важность утверждения (1–3),
sᵢ: подтверждено = 1, спорно = 0.5, нет данных = 0.4, опровергнуто = 0.
Мнения и советы не учитываются.
Штрафы: −15 за каждую ссылку с несуществующим DOI или DOI чужой работы,
−5 за ссылку, не найденную в научных базах.
Потолок 60, если опровергнуто хотя бы одно ключевое утверждение (важность 3).

Индекс — НЕ вероятность правды, а доля утверждений, для которых нашлось подтверждение.
"""
from __future__ import annotations

from ..schemas import Claim, ClaimResult, ClaimType, CitationResult, CitationStatus, Status, TrustReport

SCORE = {Status.supported: 1.0, Status.disputed: 0.5, Status.unverifiable: 0.4, Status.contradicted: 0.0}


def is_fabricated(r: CitationResult) -> bool:
    return r.status == CitationStatus.doi_not_found or "doi_other_work" in r.notes


def compute_trust(claims: list[Claim], results: list[ClaimResult],
                  citations: list[CitationResult] | None = None) -> TrustReport:
    citations = citations or []
    by_id = {c.id: c for c in claims}
    counts = {s.value: 0 for s in Status}
    counts["opinion"] = 0
    num = den = 0.0
    capped = False
    for r in results:
        c = by_id.get(r.claim_id)
        if c is None:
            continue
        if c.type in (ClaimType.opinion, ClaimType.advice):
            counts["opinion"] += 1
            continue
        counts[r.status.value] += 1
        w = float(c.importance)
        num += w * SCORE[r.status]
        den += w
        if r.status == Status.contradicted and c.importance == 3:
            capped = True

    fabricated = sum(1 for r in citations if is_fabricated(r))
    not_found = sum(1 for r in citations if r.status == CitationStatus.not_found and not is_fabricated(r))

    if den == 0:
        # нечего проверять фактами — индекс не показываем, но сообщаем о выдуманных ссылках
        return TrustReport(trust_index=0, band="na", counts=counts, fabricated_citations=fabricated)

    score = 100.0 * num / den
    score -= 15 * fabricated + 5 * not_found
    if capped:
        score = min(score, 60.0)
    score = int(round(max(0.0, min(100.0, score))))
    band = "high" if score >= 80 else "medium" if score >= 50 else "low"
    return TrustReport(trust_index=score, band=band, counts=counts,
                       fabricated_citations=fabricated, capped=capped)
