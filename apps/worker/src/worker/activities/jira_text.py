"""Textes des commentaires JIRA, écrits pour des humains (QA, PO, devs).

Constat (TOOLLIVE-3601 à 3608) : le pipeline postait 4 à 6 commentaires par
ticket, remplis de commandes `python scripts/...`, de chemins `/tmp/...` et de
diffs brutes. Règles ici : un commentaire = une information utile, en
français ; le détail technique reste dans Mistral Studio (exécution, logs).
Fonctions pures : appelables depuis le workflow (sandbox Temporal).
"""

from __future__ import annotations

import re

from worker.activities.qa_report import _extract_acceptance_criteria, _extract_summary

STUDIO = "Mistral Studio"

# Lignes techniques à ne jamais montrer dans JIRA.
_TECH_LINE = re.compile(r"python scripts/|/tmp/|^diff --git|^@@ |^index [0-9a-f]+\.\.|^[-+]{3} [ab/]")


def strip_technical(text: str) -> str:
    """Retire les lignes techniques (commandes, chemins /tmp, diff brute)."""
    kept = [line for line in (text or "").splitlines() if not _TECH_LINE.search(line)]
    out = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def start_text(key: str, summary: str, execution_id: str, transition_ok: bool) -> str:
    lines = [f"🤖 Le pipeline traite ce ticket ({key} — {summary}).",
             f"Suivi dans {STUDIO}, exécution `{execution_id[-8:]}`."]
    if not transition_ok:
        lines.append("Statut du ticket non modifié (détail dans Studio).")
    return "\n".join(lines)


def question_text(question: str) -> str:
    return (f"❓ L'agent a une question avant d'écrire la spec :\n\n> {question.strip()}\n\n"
            f"Répondre dans {STUDIO} (signal « reply »).")


def spec_ready_text(spec_content: str) -> str:
    """Spec prête : on montre son contenu utile (la SPEC.md n'est pas lisible
    ailleurs : elle reste dans l'espace de travail du worker)."""
    lines = ["📝 Spec prête — à valider dans " + STUDIO + " (« approve » ou « reject »)."]
    summary = _extract_summary(spec_content) if spec_content else ""
    if summary:
        lines += ["", f"**Ce qui va être fait** : {summary}"]
    criteria = _extract_acceptance_criteria(spec_content) if spec_content else []
    if criteria:
        lines += ["", "**Critères d'acceptation** :"]
        lines += [f"- {c['scenario']}" for c in criteria]
    if not summary and not criteria:
        lines += ["", "(Contenu de la spec non lisible — voir l'exécution dans Studio.)"]
    return "\n".join(lines)


def review_fix_text(round_no: int, mr_url: str | None) -> str:
    return (f"🔁 Correction après review (tour {round_no}) poussée sur la MR"
            + (f" : {mr_url}" if mr_url else "") + ".")


def gate_fallback_text(subject: str, payload: str) -> str:
    """Pour les autres alertes (timeouts, escalades) : sujet + texte nettoyé."""
    body = strip_technical(payload)
    return f"🔔 {subject}" + (f"\n\n{body[:1500]}" if body else "") + f"\n\nDétail dans {STUDIO}."


def doc_to_check_text(pages: list[dict]) -> str:
    """Aval : pages Confluence à relire / compléter après le changement (le
    pipeline n'écrit pas dans Confluence ; un humain décide)."""
    if not pages:
        return ""
    lines = ["📚 Documentation Confluence peut-être à mettre à jour :"]
    for p in pages:
        lines.append(f"- [{p['title']}]({p['url']})" if p.get("url") else f"- {p['title']}")
    return "\n".join(lines)
