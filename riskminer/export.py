"""Eksport wyników: JSON (pełny rekord), CSV (tabele do filtrowania w Excelu), Markdown (czytelny raport tekstowy)."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from .textutil import fmt_pln

SUB = {"siec_wod": "Sieć wodociągowa", "kan_san": "Kanalizacja sanitarna", "oczyszczalnia": "Oczyszczalnia ścieków",
       "suw": "Uzdatnianie i zaopatrzenie w wodę", "deszcz": "Kanalizacja deszczowa i retencja", "inne_wk": "Inne obiekty wod-kan"}
TAG = {"DANE": "[DANE ŹRÓDŁOWE]", "OBLICZ": "[OBLICZENIA]", "WNIOSEK": "[WNIOSEK / REKOMENDACJA]", "OBLICZ+DANE": "[OBLICZENIA + DANE]"}


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _pct(x):
    return "b.d." if x is None else f"{x * 100:.0f}%"


def _d(x):
    return "b.d." if x is None else f"{int(round(x))} dni"


def _s(x, d=1):
    return "b.d." if x is None else (f"{x:+.{d}f}%").replace(".", ",")


def report_markdown(r: dict[str, Any]) -> str:
    P = {k: v.get("value") for k, v in r["query"]["params"].items()}
    L = []
    a = L.append
    a(f"# Raport Risk Miner: {r['title']}")
    a(f"\nIdentyfikator analizy: `{r['analysis_id']}` · data: {r['created_at']} · próba porównawcza: **N = {r['sample']['n']}** "
      f"(z {r['sample']['db_completed_cases']} zakończonych postępowań w bazie)\n")
    a("> Oznaczenia: [DANE ŹRÓDŁOWE] – z ogłoszeń/dokumentów (z linkiem); [OBLICZENIA] – statystyka na danych (z liczebnością próby); "
      "[WNIOSEK / REKOMENDACJA] – reguły eksperckie lub model językowy, wymagają oceny eksperta.\n")
    a("## 1. Podsumowanie " + TAG["OBLICZ"])
    for s in r.get("summary", []):
        a(f"- {s}")
    if r.get("llm_summary", {}).get("text"):
        a(f"\n**Podsumowanie modelu językowego** {TAG['WNIOSEK']} ({r['llm_summary'].get('model')}):\n\n{r['llm_summary']['text']}")
    a("\n## 2. Parametry planowanej inwestycji (rozpoznane z opisu)")
    a("| Parametr | Wartość | Źródło | Cytat z opisu |\n|---|---|---|---|")
    names = {"subsector": "Podsektor", "works_type": "Rodzaj robót", "design_build": "Formuła „zaprojektuj i wybuduj”", "budget": "Budżet",
             "budget_vat": "VAT", "duration_days": "Planowany czas (dni)", "length_km": "Długość sieci (km)", "connections": "Liczba przyłączy",
             "province": "Województwo", "org": "Zamawiający", "city": "Miejscowość", "features": "Elementy techniczne"}
    for k, label in names.items():
        v = r["query"]["params"].get(k)
        if not v:
            continue
        val = v.get("value")
        if k == "subsector":
            val = SUB.get(val, val)
        if k == "province":
            from .query import PROVINCES
            val = PROVINCES.get(val or "", (val,))[0]
        if k == "features" and val:
            from .catalog import FEATURES
            fn = {f["code"]: f["name"] for f in FEATURES}
            val = [fn.get(x, x) for x in val]
        if k == "budget" and val:
            val = fmt_pln(val)
        if isinstance(val, list):
            val = ", ".join(map(str, val)) or "—"
        if val in (None, "", False):
            val = "—" if val is not False else "nie"
        if val is True:
            val = "tak"
        a(f"| {label} | {val} | {v.get('source', '')} | {(v.get('quote') or '').replace('|', '/')[:120]} |")
    F = r["forecast"]
    t, c, b, td = F["time"], F["cost"], F["budget"], F["tender"]
    a("\n## 3. Prognoza czasu i kosztu " + TAG["OBLICZ"])
    a(f"- Prawdopodobieństwo wykonania po pierwotnym terminie: **{_pct(t.get('delay_probability'))}** "
      f"(90% CI {_pct(t['delay_ci90'][0])}–{_pct(t['delay_ci90'][1])}; {t['delay_k']} z {t['delay_n']} umów)")
    ad = t.get("actual_days", {})
    if ad.get("n"):
        a(f"- Rzeczywisty czas realizacji podobnych umów: mediana {_d(ad['median'])}, P25–P75: {_d(ad['p25'])}–{_d(ad['p75'])}, P90: {_d(ad['p90'])} (N={ad['n']})")
    dl = t.get("delay_days_when_late", {})
    if dl.get("n"):
        a(f"- Opóźnienie, gdy wystąpiło: mediana {_d(dl['median'])}, P75 {_d(dl['p75'])} (N={dl['n']})")
    if t.get("expected_days_range"):
        a(f"- Dla planowanych {_d(t['user_planned_days'])}: realistycznie {_d(t['expected_days_range'][0])}–{_d(t['expected_days_range'][1])} (mediana–P80 stosunku czas rzeczywisty/planowany)")
    ch = c.get("change_pct", {})
    if ch.get("n"):
        a(f"- Zmiana kosztu (wypłacono vs wartość umowy): mediana {_s(ch['median'])}, P75 {_s(ch['p75'])}, P90 {_s(ch['p90'])}; "
          f"wzrost w {_pct(c['share_increase'])} umów (N={ch['n']})")
    if c.get("expected_final_range"):
        a(f"- Dla budżetu {fmt_pln(c['user_budget_gross'])} brutto: spodziewany koszt końcowy {fmt_pln(c['expected_final_range'][0])} – {fmt_pln(c['expected_final_range'][1])} (mediana–P90)")
    pk = b.get("per_km", {})
    if pk.get("n"):
        a(f"- Koszt 1 km sieci w podobnych inwestycjach: mediana {fmt_pln(pk['median'])}, P25–P75 {fmt_pln(pk['p25'])}–{fmt_pln(pk['p75'])} (N={pk['n']})")
    if b.get("user_per_km"):
        a(f"- Twój budżet na 1 km: {fmt_pln(b['user_per_km'])} → ocena: **{b.get('verdict')}**; typowy budżet dla tej długości: "
          f"{fmt_pln(b['typical_budget_range'][0])} – {fmt_pln(b['typical_budget_range'][1])}")
    pve = td.get("price_vs_estimate_pct", {})
    if pve.get("n"):
        a(f"- Cena wybranej oferty vs szacunek zamawiającego (brutto): mediana {_s(pve['median'], 0)} (N={pve['n']})")
    of = td.get("offers", {})
    if of.get("n"):
        a(f"- Średnia liczba ofert: mediana {str(round(of['median'], 1)).replace('.', ',')} (N={of['n']})")
    cn = td.get("cancellations", {})
    if cn.get("n"):
        a(f"- Unieważnienia w podobnych postępowaniach: {_pct(cn.get('share'))} ({cn['k']} z {cn['n']}), w tym z powodu ceny powyżej budżetu: {cn.get('over_budget', 0)}")
    a("\n## 4. Czerwone flagi")
    for f in r["flags"]:
        a(f"- **{'CZERWONA' if f['level'] == 'red' else 'UWAGA'}** {TAG.get(f['kind'], '')}: {f['text']} _(podstawa: {f['basis']})_")
    if not r["flags"]:
        a("- brak")
    a("\n## 5. Ryzyka w podobnych inwestycjach " + TAG["OBLICZ+DANE"])
    for x in r["risks"][:10]:
        a(f"\n### {x['code']} {x['name']} — {x['cases']} z {r['sample']['n']} przypadków ({_pct(x['share'])}, 90% CI {_pct(x['ci90'][0])}–{_pct(x['ci90'][1])})")
        if x.get("catalog_status") == "proposed":
            a("_Kategoria zaproponowana przez system – oczekuje na zatwierdzenie eksperta._")
        a(f"Typowe skutki: {x.get('effects') or ''}")
        if x.get("median_time_days"):
            a(f"- Mediana wydłużenia terminu: {_d(x['median_time_days'])}")
        if x.get("median_cost_pln"):
            a(f"- Mediana wartości zmiany: {fmt_pln(x['median_cost_pln'])}")
        if x.get("actions_seen"):
            a("- Działania podjęte w tych przypadkach: " + ", ".join(f"{k} ({v})" for k, v in x["actions_seen"]))
        a("- Dowody " + TAG["DANE"] + ":")
        for e in x["examples"]:
            st = {"approved": "zatwierdzone przez eksperta", "corrected": "poprawione przez eksperta"}.get(e["status"], "niezweryfikowane")
            a(f"  - „{e['quote'][:300]}” — {e['case_org']}, *{(e['case_title'] or '')[:90]}*; ogłoszenie {e['source_ref']} ({e['locator']}) "
              f"[{st}; pewność {e['confidence']:.2f}] {e.get('source_url') or ''}")
    a("\n## 6. Rekomendacje " + TAG["WNIOSEK"])
    R = r["recommendations"]
    a("### Do sprawdzenia / uzupełnienia przed ogłoszeniem przetargu")
    for x in R["before_tender"]:
        a(f"- {x['text']} _({x.get('ref') or ''})_")
    a("### Rekomendowane działania")
    for x in R["actions"]:
        a(f"- {x['text']} _(podstawa: {x['basis']}; dowody: {', '.join(x['evidence'])})_")
    a("### Propozycje zapisów umownych")
    for x in R["clauses"]:
        a(f"- {x['text']} _(podstawa: {x['basis']})_")
    a("\n## 7. Grupa porównawcza – podobne zakończone inwestycje " + TAG["DANE"])
    a("| # | Podobieństwo | Inwestycja | Zamawiający | Rok | Wartość umowy | Zmiana kosztu | Po terminie | Zmiany | Dlaczego podobna |\n|---|---|---|---|---|---|---|---|---|---|")
    for i, x in enumerate(r["comparables"], 1):
        ccp = _s(x.get("cost_change_pct"))
        a(f"| {i} | {x.get('similarity_pct')}% | {(x['title'] or '')[:80]} | {x['org']} ({x['province']}) | {x['year']} | {fmt_pln(x.get('contract_value'))} | "
          f"{ccp} | "
          f"{'tak' if x.get('delayed') else 'nie' if x.get('delayed') == 0 else 'b.d.'}"
          f"{(' (' + str(x['delay_days']) + ' dni)') if x.get('delay_days') else ''} | {x.get('n_changes') or 0} | {'; '.join(x['why'][:3])} |")
    a("\n## 8. Do kogo się zwrócić (zamawiający z podobnymi inwestycjami) " + TAG["DANE"])
    if r["sample"]["low_data"]:
        a("**Danych jest mało – rekomendujemy kontakt z poniższymi zamawiającymi przed podjęciem decyzji.**")
    for x in r["contacts"]:
        a(f"- **{x['org']}** ({x.get('city') or ''}, {x['province']}) — {x['why']}. Inwestycja: „{(x['case_title'] or '')[:90]}” ({x['year']}). "
          f"Kontakt: {x.get('email') or 'brak e-mail'}{', tel. ' + x['phone'] if x.get('phone') else ''}{', ' + x['www'] if x.get('www') else ''}")
    a("\n## 9. Lista kontrolna przygotowania (materiał ekspercki MFiPR)")
    for x in r["checklist"]:
        a(f"- [{'x' if x['present'] else ' '}] {x['text']} _({x.get('ref') or ''})_")
    a("\n## 10. Metoda i ograniczenia")
    for k, v in r["method"].items():
        a(f"- {k}: {v}")
    a("- Brak informacji w ogłoszeniu nie oznacza braku zdarzenia; zdarzenia „niezweryfikowane” wymagają akceptacji eksperta.")
    a("- Kwoty: szacunki zamawiających są netto, wartości umów i wypłat – brutto (porównujemy brutto, szacunek ×1,23).")
    return "\n".join(L) + "\n"


def report_csv(r: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["sekcja", "id", "nazwa", "zamawiajacy", "wojewodztwo", "rok", "podobienstwo_%", "wartosc_umowy", "wyplacono", "zmiana_kosztu_%",
                "po_terminie", "opoznienie_dni", "liczba_zmian", "kody_ryzyk", "dlaczego_podobna", "link"])
    for x in r["comparables"]:
        w.writerow(["grupa_porownawcza", x["tender_id"], x["title"], x["org"], x["province"], x["year"], x.get("similarity_pct"), x.get("contract_value"),
                    x.get("paid_value"), x.get("cost_change_pct"), x.get("delayed"), x.get("delay_days"), x.get("n_changes"),
                    ",".join(x.get("risk_codes") or []), " | ".join(x["why"]), x.get("url")])
    w.writerow([])
    w.writerow(["sekcja", "kod", "ryzyko", "przypadki", "N", "udzial", "ci90_od", "ci90_do", "zdarzen", "zatwierdzonych", "mediana_dni", "mediana_kosztu", "dowody"])
    for x in r["risks"]:
        w.writerow(["ryzyko", x["code"], x["name"], x["cases"], r["sample"]["n"], round(x["share"], 3), round(x["ci90"][0], 3), round(x["ci90"][1], 3),
                    x["events"], x["approved_events"], x.get("median_time_days"), x.get("median_cost_pln"),
                    " | ".join(f"{e['source_ref']}: {e['quote'][:150]}" for e in x["examples"])])
    w.writerow([])
    w.writerow(["sekcja", "poziom", "tresc", "podstawa"])
    for f in r["flags"]:
        w.writerow(["flaga", f["level"], f["text"], f["basis"]])
    return buf.getvalue()


def project_markdown(rec: dict[str, Any]) -> str:
    p = rec["project"]
    L = []
    a = L.append
    a(f"# Rekord projektu {p['project_id']}: {p['title']}")
    a(f"\nWyeksportowano: {rec['exported_at']} · źródło rekordu: {p.get('source_file')} · wzbogacono: {p.get('enriched_at') or 'nie'}\n")
    a("## Dane projektu")
    for k, lab in (("org_name", "Odbiorca"), ("org_id", "NIP"), ("province", "Województwo"), ("city", "Siedziba"), ("public_id_type", "Rodzaj identyfikatora"),
                   ("public_id", "Identyfikator publiczny"), ("scope", "Zakres rekordu"), ("status", "Status"), ("programme", "Program"),
                   ("project_value", "Wartość przedsięwzięcia"), ("grant_value", "Dofinansowanie"), ("subsector", "Podsektor")):
        v = p.get(k)
        if k in ("project_value", "grant_value") and v:
            v = fmt_pln(v)
        if k == "province" and v:
            from .query import PROVINCES
            v = PROVINCES.get(v, (v,))[0]
        if k == "subsector":
            v = SUB.get(v, v)
        a(f"- **{lab}:** {v if v not in (None, '') else '—'}")
    a("\n## Walidacja identyfikatorów")
    for v in p.get("validation") or []:
        a(f"- [{v['level']}] {v['field']}: {v['message']}")
    gaps = (p.get("summary") or {}).get("gaps") or []
    if gaps:
        a("\n## Braki w pakiecie danych (brak dokumentu ≠ brak zdarzenia)")
        for g in gaps:
            a(f"- [{g['level']}] **{g['item']}** – {g['why']}. Skąd: {g['where']}")
    a("\n## Powiązania ze źródłami (dopasowanie i pewność)")
    a("| Typ | Identyfikator | Nazwa | Podstawa dopasowania | Pewność | Status | Uzasadnienie |\n|---|---|---|---|---|---|---|")
    for l in rec["links"]:
        a(f"| {l['target_type']} | {l['target_id']} | {(l.get('title') or '')[:70]} | {l['match_basis']} | {l['confidence']:.2f} | {l['status']} | {'; '.join(l['evidence'])[:200]} |")
    for pr in rec["procedures"]:
        a(f"\n## Postępowanie {pr['tender_id']}")
        a(f"**{pr.get('title')}** — {pr.get('org_name')} (NIP {pr.get('nip')}) · status: {pr.get('status')} · podsektor: {SUB.get(pr.get('subsector'), pr.get('subsector'))}")
        a(f"- Wartość szacunkowa (netto): {fmt_pln(pr.get('estimated_value_net'))}; wartość umów (brutto): {fmt_pln(pr.get('contract_value'))}; "
          f"wypłacono: {fmt_pln(pr.get('paid_value'))}; zmiana kosztu: {pr.get('cost_change_pct') if pr.get('cost_change_pct') is not None else 'b.d.'}%")
        a(f"- Planowany czas: {pr.get('planned_days') or 'b.d.'} dni; rzeczywisty: {pr.get('actual_days') or 'b.d.'} dni; po terminie: "
          f"{'tak' if pr.get('delayed') else 'nie' if pr.get('delayed') == 0 else 'b.d.'}; opóźnienie: {pr.get('delay_days') or 0} dni")
        a("\n**Ogłoszenia:**")
        for n in pr["notices"]:
            a(f"- {n['pub_date']} · {n['notice_type']} · {n['notice_number']} · {n['url']} (pobrano: {n['fetched_at']})")
        a("\n**Umowy:**")
        for c in pr["contracts"]:
            yn = lambda v: "tak" if v == 1 else "nie" if v == 0 else "b.d."
            a(f"- {c.get('contractor')} · umowa {c.get('contract_date')} · wartość {fmt_pln(c.get('contract_value'))} · wykonana: {yn(c.get('executed'))} · "
              f"w terminie: {yn(c.get('on_time'))} · należycie: {yn(c.get('properly'))} · wypłacono {fmt_pln(c.get('paid_value'))} · zakończenie {c.get('completion_date')}")
        if pr["changes"]:
            a("\n**Zmiany umów:**")
            for ch in pr["changes"]:
                a(f"- [{ch.get('source_notice')}, sekcja {ch.get('section')}] {ch.get('legal_basis') or ''} ({ch.get('basis_label') or ''}): "
                  f"{(ch.get('reason') or '')[:250]} — {(ch.get('description') or '')[:250]} · wartość: {ch.get('value')}")
    a("\n## Zdarzenia ryzyka (do weryfikacji eksperckiej)")
    a("| Kod | Kategoria | Fakt | Przyczyna / cytat | Skutek | Działania i skuteczność | Źródło | Pewność | Status |\n|---|---|---|---|---|---|---|---|---|")
    for e in rec["risk_events"]:
        eff = ", ".join(e["effect_types"]) + (f"; {e['effect_time_days']} dni" if e.get("effect_time_days") else "") + (f"; {fmt_pln(e['effect_cost_pln'])}" if e.get("effect_cost_pln") else "")
        a(f"| {e['risk_code']} | {e['risk_name']} | {e.get('event_type') or ''} | {(e.get('quote') or '')[:200].replace('|', '/')} | {eff} | "
          f"{', '.join(e['actions'])} {('– ' + e['action_effectiveness']) if e.get('action_effectiveness') else ''} | {e.get('source_ref')} ({e.get('locator')}) | "
          f"{e.get('confidence')} | {e['status']} |")
    if rec["documents"]:
        a("\n## Dokumenty")
        for d in rec["documents"]:
            a(f"- {d['source_system']} · {d['doc_type']} · {d['title']} · {d.get('url') or ''} · pobrano {d['fetched_at']} · sha256 {d['sha256'][:16]}…")
    if rec["expert_history"]:
        a("\n## Historia decyzji eksperta")
        for h in rec["expert_history"]:
            a(f"- {h['ts']} · {h['reviewer']} · {h['entity_type']} {h['entity_id']} · {h['action']} {h.get('comment') or ''}")
    return "\n".join(L) + "\n"


def project_csv(rec: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    p = rec["project"]
    w.writerow(["typ_rekordu", "project_id", "id", "nazwa", "data", "wartosc", "status", "pewnosc", "zrodlo", "link", "szczegoly"])
    w.writerow(["projekt", p["project_id"], p.get("public_id"), p["title"], p.get("record_date"), p.get("project_value"), p.get("status"), "", p.get("source_file"), "", p.get("org_name")])
    for l in rec["links"]:
        w.writerow(["powiazanie", p["project_id"], l["target_id"], l.get("title"), l.get("created_at"), "", l["status"], l["confidence"], l["source_system"], l.get("url"), "; ".join(l["evidence"])])
    for pr in rec["procedures"]:
        w.writerow(["postepowanie", p["project_id"], pr["tender_id"], pr.get("title"), pr.get("first_pub_date"), pr.get("contract_value"), pr.get("status"), "", "BZP", "", pr.get("org_name")])
        for n in pr["notices"]:
            w.writerow(["ogloszenie", p["project_id"], n["notice_number"], n.get("title"), n["pub_date"], "", n["notice_type"], "", n.get("source_system"), n["url"], ""])
        for c in pr["contracts"]:
            w.writerow(["umowa", p["project_id"], c["contract_id"], c.get("contractor"), c.get("contract_date"), c.get("contract_value"),
                        f"wykonana={c.get('executed')} w_terminie={c.get('on_time')} nalezycie={c.get('properly')}", "", "BZP", "", f"wyplacono={c.get('paid_value')}"])
        for ch in pr["changes"]:
            w.writerow(["zmiana_umowy", p["project_id"], ch["change_id"], ch.get("reason"), ch.get("change_date"), ch.get("value"), ch.get("legal_basis"), "",
                        ch.get("source_notice"), "", ch.get("description")])
    for e in rec["risk_events"]:
        w.writerow(["zdarzenie_ryzyka", p["project_id"], e["event_id"], f"{e['risk_code']} {e['risk_name']}", e.get("created_at"), e.get("effect_cost_pln"),
                    e["status"], e.get("confidence"), e.get("source_ref"), e.get("source_url"), e.get("quote")])
    for d in rec["documents"]:
        w.writerow(["dokument", p["project_id"], d["doc_id"], d["title"], d["fetched_at"], "", d["status"], "", d["source_system"], d.get("url"), d["doc_type"]])
    return buf.getvalue()


def rows_csv(rows: list[dict], columns: list[str] | None = None) -> str:
    buf = io.StringIO()
    if not rows:
        return ""
    cols = columns or list(rows[0].keys())
    w = csv.writer(buf, delimiter=";")
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c) for c in cols])
    return buf.getvalue()
