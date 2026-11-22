(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

  const WOJ = ["dolnośląskie","kujawsko-pomorskie","lubelskie","lubuskie","łódzkie","małopolskie","mazowieckie","opolskie",
    "podkarpackie","podlaskie","pomorskie","śląskie","świętokrzyskie","warmińsko-mazurskie","wielkopolskie","zachodniopomorskie"];
  const RODZAJ = ["roboty budowlane","dostawy","usługi"];
  const JEDN = ["dni","tygodnie","miesiące","lata"];
  const KAT = {zakres:"#3ee08a", obiekt:"#5cc8ff", technologia:"#b38cff", lokalizacja:"#ffb35c", finansowanie:"#e8c25a",
    harmonogram:"#ff8fb1", "formuła":"#7fe0d0", ryzyko:"#f07a6a", inne:"#86a392"};
  // kolory pól (pasek karty i podkreślenie w tekście)
  const FIELDS = [
    {key:"zamawiajacy", label:"Zamawiający", c:"#3ee08a"},
    {key:"rodzaj_zamowienia", label:"Rodzaj zamówienia", c:"#5cc8ff"},
    {key:"miejscowosc", label:"Miejscowość", c:"#ffb35c"},
    {key:"wojewodztwo", label:"Województwo", c:"#b38cff"},
    {key:"planowana_kwota", label:"Planowana kwota", c:"#e8c25a", wide:false},
    {key:"planowany_czas", label:"Planowany czas realizacji", c:"#ff8fb1", wide:true},
  ];
  // zakres rzeczowy – te same listy co ELEMENTY / JEDNOSTKI_ZAKRESU w serwer.py
  const ELEM = ["kanalizacja sanitarna grawitacyjna","kanalizacja sanitarna tłoczna","kanalizacja sanitarna (inna/nieokreślona)",
    "kanalizacja deszczowa","sieć wodociągowa","przyłącza kanalizacyjne","przyłącza wodociągowe","przepompownia ścieków",
    "oczyszczalnia ścieków","stacja uzdatniania wody","zbiornik","studnia głębinowa","studnie/studzienki",
    "urządzenia i wyposażenie","nawierzchnie i drogi","inne"];
  const UNITS = ["km","m","szt.","m2","m3","m3/d","RLM","kW","inne"];
  const SCOPE_C = "#7fe0d0";

  const APP_VERSION = "2026-11-22.5";   // ta sama co VERSION w serwer.py
  const S = { result:null, text:"", meta:null, ctrl:null, timer:null, status:null };

  // ---- pomocnicze ----------------------------------------------------------
  function toast(msg){ const t=$("toast"); t.textContent=msg; t.style.display="block"; clearTimeout(t._h); t._h=setTimeout(()=>t.style.display="none",2600); }
  const fmtMoney = (v, cur) => v==null ? "" : v.toLocaleString("pl-PL",{minimumFractionDigits:v%1?2:0,maximumFractionDigits:2}) + " " + (cur==="PLN"||!cur ? "zł" : cur);
  const plDays = n => n===1 ? "dzień" : "dni";
  // polska odmiana: 1 miesiąc, 2–4 miesiące, 5+ miesięcy (ułamki: „miesiąca”)
  const plural = (n, one, few, many, frac) => { if(n % 1) return frac; n = Math.abs(n); return n===1 ? one : (n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20)) ? few : many; };
  const UNIT_FORMS = {dni:["dzień","dni","dni","dnia"], tygodnie:["tydzień","tygodnie","tygodni","tygodnia"],
    "miesiące":["miesiąc","miesiące","miesięcy","miesiąca"], lata:["rok","lata","lat","roku"]};
  const plUnit = (n, u) => plural(n, ...UNIT_FORMS[u]);
  const plMonths = n => plural(Math.round(n), "miesiąc", "miesiące", "miesięcy", "miesiąca");
  function banner(html, kind=""){ $("banner").innerHTML = html ? `<div class="banner ${kind}">${html}</div>` : ""; }
  function saveDraft(){ try{ localStorage.setItem("sk_text", $("text").value); }catch{} }
  function loadDraft(){ try{ return localStorage.getItem("sk_text") || ""; }catch{ return ""; } }

  // ---- stan modelu ---------------------------------------------------------
  async function checkStatus(){
    const el = $("status");
    try{
      const s = await (await fetch("api/status")).json(); S.status = s; updateChars();
      if(s.wersja !== APP_VERSION){
        banner(`Serwer aplikacji działa w starszej wersji (${esc(s.wersja || "sprzed 21.11 22:55")}), a strona jest nowsza (${APP_VERSION}).
          Zatrzymaj serwer (Ctrl+C w jego oknie) i uruchom ponownie <code>python serwer.py</code>, potem odśwież stronę (Ctrl+F5).`, "err");
        S.staleServer = true; $("btnAnalyze").disabled = true; $("btnAnalyze").title = "Serwer działa w starszej wersji – uruchom go ponownie";
      }
      el.className = "status " + (s.model_ready ? "ok" : s.online ? "warn" : "bad");
      $("statusText").textContent = s.model_ready ? `${s.model} · gotowy`
        : s.online ? `Brak modelu ${s.model}` : `Serwer modelu nie działa (${s.backend})`;
      el.title = s.model_ready ? `Model ${s.model} na ${s.url} (${s.backend})`
        : s.online ? `Pobierz model poleceniem: ollama pull ${s.model}\nDostępne: ${s.models.join(", ") || "brak"}`
        : `Nie można połączyć z ${s.url}. Uruchom Ollamę (ollama serve) albo inny serwer modelu.`;
      if(S.staleServer){ /* komunikat o starym serwerze ma pierwszeństwo */ }
      else if(!s.model_ready && !S.result){
        banner(s.online
          ? `Lokalny serwer modelu działa, ale nie ma na nim modelu <b>${esc(s.model)}</b>. Pobierz go:<br><code>ollama pull ${esc(s.model)}</code><br>
             <small>Albo uruchom serwer z innym modelem: <code>python serwer.py --model NAZWA</code>. Dostępne: ${esc(s.models.join(", ") || "brak")}</small>`
          : `Nie ma połączenia z lokalnym modelem (${esc(s.url)}). Zainstaluj <a href="https://ollama.com/download" target="_blank" rel="noopener">Ollamę</a>, a potem:<br>
             <code>ollama pull ${esc(s.model)}</code>`, "warn");
      } else if(s.model_ready && !S.result && !S.staleServer) banner("");
      return s;
    }catch{
      el.className = "status bad"; $("statusText").textContent = "Brak połączenia z serwer.py";
      banner(`Strona nie łączy się z serwerem aplikacji. Uruchom <code>python serwer.py</code> i otwórz <code>http://127.0.0.1:8000</code>.`, "err");
      return null;
    }
  }

  // ---- analiza ---------------------------------------------------------------
  async function analyze(){
    if(S.staleServer){ toast("Najpierw uruchom ponownie serwer (python serwer.py) – działa starsza wersja."); return; }
    const text = $("text").value.trim();
    if(text.length < 20){ toast("Wpisz dłuższy opis inwestycji."); $("text").focus(); return; }
    if(S.ctrl) return;
    S.ctrl = new AbortController();
    S.reqId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/[^\w-]/g, "");
    const t0 = Date.now();
    const btn = $("btnAnalyze"); btn.disabled = true; $("btnCancel").hidden = false;
    const tick = () => { btn.innerHTML = `<span class="spinner"></span> Analizuję… ${Math.round((Date.now()-t0)/1000)} s`; };
    tick(); S.timer = setInterval(tick, 1000);
    banner(""); $("meta").textContent = "";
    $("result").innerHTML = `<div class="fields">${FIELDS.map(()=>`<div class="skel"></div>`).join("")}</div><div class="skel" style="margin-top:12px;height:90px"></div>
      <p class="muted" style="font-size:12.5px;margin-top:10px">Model czyta tekst – na komputerze bez karty graficznej może to potrwać od kilkunastu sekund do kilku minut.</p>`;
    try{
      const r = await fetch("api/extract", {method:"POST", headers:{"Content-Type":"application/json", "X-Request-Id":S.reqId}, body:JSON.stringify({text}), signal:S.ctrl.signal});
      const j = await r.json().catch(() => ({title:`HTTP ${r.status}`}));
      if(!r.ok) throw Object.assign(new Error(j.title || `HTTP ${r.status}`), {detail:j.detail, debug:j.debug_plik, raw:j.surowa_tresc});
      S.text = text; S.result = j.wynik; S.meta = j;
      render(); showMarked();
      if(j.ostrzezenia?.length) banner(j.ostrzezenia.map(esc).join("<br>"), "warn");
    }catch(e){
      if(e.name === "AbortError"){ banner("Analizę przerwano – model zatrzymany.", "warn"); }
      else banner(`<b>${esc(e.message)}</b>${e.detail?`<br><small>${esc(String(e.detail).slice(0,400))}</small>`:""}` +
        (e.debug ? `<br><small>Zapis debug: <code>${esc(e.debug)}</code></small>` : "") + rawBox(e.raw), "err");
      if(!S.result) $("result").innerHTML = ""; else render();
      checkStatus();
    }finally{
      clearInterval(S.timer); S.ctrl = null; btn.disabled = false; $("btnCancel").hidden = true;
      btn.innerHTML = `Analizuj <kbd>Ctrl</kbd><kbd>Enter</kbd>`;
    }
  }

  // ---- wynik ------------------------------------------------------------------
  function srcBadge(f, key){
    const val = key==="planowana_kwota" ? f.kwota!=null : key==="planowany_czas" ? (f.liczba!=null || f.termin_do || f.data_podpisania) : !!f.wartosc;
    if(!val) return `<span class="src none">nie znaleziono</span>`;
    if(f.z_reguly === true) return `<span class="src inf" title="Model nie podał poprawnej wartości – odczytano z tekstu regułą (np. etykieta „Zamawiający:”)">z tekstu (reguła)</span>`;
    if(key==="wojewodztwo" && f.wywnioskowane && f.zrodlo==="brak") return `<span class="src inf" title="Nie pada w tekście – model wywnioskował z miejscowości lub zamawiającego">wywnioskowane</span>`;
    if(f.zrodlo==="dokladny") return `<span class="src ok" title="Cytat znaleziony w tekście">✓ w tekście</span>`;
    if(f.zrodlo==="czesciowy") return `<span class="src part" title="Cytat zgadza się z tekstem tylko częściowo">≈ w tekście</span>`;
    return `<span class="src part" title="Model nie podał cytatu albo cytatu nie ma w tekście – sprawdź wartość">sprawdź</span>`;
  }
  const opts = (list, cur, empty="—") => `<option value="">${empty}</option>` + list.map(o => `<option${o===cur?" selected":""}>${esc(o)}</option>`).join("");

  function fieldBody(key, f){
    switch(key){
      case "zamawiajacy": case "miejscowosc":
        return `<input data-k="${key}" data-p="wartosc" value="${esc(f.wartosc)}" placeholder="nie znaleziono">`;
      case "rodzaj_zamowienia":
        return `<select data-k="${key}" data-p="wartosc">${opts(RODZAJ, f.wartosc)}</select>`;
      case "wojewodztwo":
        return `<select data-k="${key}" data-p="wartosc">${opts(WOJ, f.wartosc)}</select>`;
      case "planowana_kwota":
        return `<div class="row"><input data-k="${key}" data-p="kwota" data-num="1" value="${f.kwota??""}" placeholder="kwota" style="flex:2">
          <input data-k="${key}" data-p="waluta" value="${esc(f.waluta)}" placeholder="PLN" style="flex:.8">
          <select data-k="${key}" data-p="vat" style="flex:1.3">${["nieokreślone","netto","brutto"].map(v=>`<option${v===f.vat?" selected":""}>${v}</option>`).join("")}</select></div>
          <div class="pretty" data-pretty="${key}"></div>`;
      case "planowany_czas":
        return `<div class="row time-row">
            <label class="mini">Czas trwania<span class="row"><input data-k="${key}" data-p="liczba" data-num="1" value="${f.liczba??""}" placeholder="np. 20" style="flex:1">
              <select data-k="${key}" data-p="jednostka" style="flex:1.4">${opts(JEDN, f.jednostka, "jednostka")}</select></span></label>
            <label class="mini">Podpisanie umowy<input type="date" data-k="${key}" data-p="data_podpisania" value="${esc(f.data_podpisania)}"></label>
            <label class="mini">Termin zakończenia<input type="date" data-k="${key}" data-p="termin_do" value="${esc(f.termin_do)}"></label>
            <label class="mini">Termin pierwotny<input type="date" data-k="${key}" data-p="termin_pierwotny" value="${esc(f.termin_pierwotny)}" title="wcześniejszy termin, jeśli został zmieniony (aneks, przedłużenie)"></label>
          </div>
          <div class="pretty" data-pretty="${key}"></div>`;
    }
  }
  function pretty(key){
    const f = S.result[key];
    if(key==="planowana_kwota"){
      return f.kwota!=null ? `<b>${esc(fmtMoney(f.kwota, f.waluta))}</b> ${f.vat!=="nieokreślone"?esc(f.vat):"<span class='muted'>(VAT nieokreślony)</span>"}` : "";
    }
    if(key==="planowany_czas"){
      computeTime(f);
      const d = x => x ? x.split("-").reverse().join(".") : "";
      const lines = [];
      if(f.dni != null){
        const dur = f.liczba!=null && f.jednostka ? ` (${esc(String(f.liczba).replace(".",","))} ${esc(plUnit(f.liczba, f.jednostka))})` : "";
        const end = f.termin_do || f.termin_wyliczony;
        lines.push(`<b>${f.dni.toLocaleString("pl-PL")} ${plDays(Math.abs(f.dni))}</b>${dur}` +
          (f.data_podpisania && end ? ` · od podpisania <b>${d(f.data_podpisania)}</b> do <b>${d(end)}</b>${f.termin_do ? "" : " <span class='muted'>(wyliczony)</span>"}` :
           end ? ` · do <b>${d(end)}</b>` : "") +
          `<br><span class="muted">${esc(f.podstawa)}</span>`);
      }
      if(f.przedluzenie_dni != null) lines.push(`<span class="${f.przedluzenie_dni>0?"warn-t":""}">Termin zmieniony z ${d(f.termin_pierwotny)} na ${d(f.termin_do)}: <b>${f.przedluzenie_dni>0?"+":""}${f.przedluzenie_dni} ${plDays(Math.abs(f.przedluzenie_dni))}</b></span>`);
      if(f.uwaga) lines.push(`<span class="warn-t">⚠ ${esc(f.uwaga)}</span>`);
      return lines.join("<br>");
    }
    return "";
  }

  // ---- czas realizacji: to samo co compute_time() w serwer.py (przeliczane po każdej poprawce) ----
  const toD = x => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(x || ""); return m ? new Date(Date.UTC(+m[1], +m[2]-1, +m[3])) : null; };
  const iso = d => d ? d.toISOString().slice(0,10) : "";
  const days = (a, b) => Math.round((b - a) / 864e5);
  function addDuration(start, n, u){
    const d = new Date(start);
    if(u==="dni"){ d.setUTCDate(d.getUTCDate() + Math.round(n)); return d; }
    if(u==="tygodnie"){ d.setUTCDate(d.getUTCDate() + Math.round(n*7)); return d; }
    const months = u==="lata" ? n*12 : n, whole = Math.trunc(months), day = d.getUTCDate();
    d.setUTCDate(1); d.setUTCMonth(d.getUTCMonth() + whole);
    const last = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth()+1, 0)).getUTCDate();
    d.setUTCDate(Math.min(day, last)); d.setUTCDate(d.getUTCDate() + Math.round((months-whole)*30.4375));
    return d;
  }
  function computeTime(f){
    const sign = toD(f.data_podpisania), end = toD(f.termin_do), orig = toD(f.termin_pierwotny);
    const hasDur = f.liczba != null && f.jednostka;
    const calc = sign && hasDur ? addDuration(sign, f.liczba, f.jednostka) : null;
    f.termin_wyliczony = iso(calc); f.dni = null; f.podstawa = ""; f.przedluzenie_dni = null; f.uwaga = "";
    const koniec = end || calc;
    if(sign && koniec){ f.dni = days(sign, koniec); f.podstawa = "od podpisania umowy do " + (end ? "terminu zakończenia" : "terminu wyliczonego z czasu trwania"); }
    else if(hasDur){ f.dni = Math.round(f.liczba * {dni:1,tygodnie:7,"miesiące":30.4375,lata:365.25}[f.jednostka]); f.podstawa = "z czasu trwania (brak daty podpisania umowy)"; }
    else if(end){ f.dni = days(toD(new Date().toISOString().slice(0,10)), end); f.podstawa = "od dziś do terminu zakończenia (brak daty podpisania umowy)"; }
    if(end && orig) f.przedluzenie_dni = days(orig, end);
    const explained = orig && calc && Math.abs(days(calc, orig)) <= 15;   // różnicę tłumaczy przedłużenie terminu
    if(end && calc && Math.abs(days(calc, end)) > 15 && !explained) f.uwaga = `Termin zakończenia (${iso(end)}) różni się o ${Math.abs(days(calc, end))} dni od wyliczonego z czasu trwania (${iso(calc)}).`;
    if(f.dni != null && f.dni < 0 && sign) f.uwaga = "Termin zakończenia jest wcześniejszy niż data podpisania umowy – sprawdź daty.";
  }

  // ---- zakres rzeczowy ----------------------------------------------------------
  function scopeBase(it){
    const v = it.ilosc;
    if(v == null || Number.isNaN(v)){ it.ilosc_bazowa = null; it.jednostka_bazowa = it.jednostka; return; }
    if(it.jednostka === "km"){ it.ilosc_bazowa = Math.round(v*1000*1000)/1000; it.jednostka_bazowa = "m"; }
    else { it.ilosc_bazowa = v; it.jednostka_bazowa = it.jednostka; }
  }
  function scopeTotals(){
    const tot = {};
    for(const it of S.result.zakres_rzeczowy || []){
      scopeBase(it); if(it.ilosc_bazowa == null) continue;
      const d = tot[it.element] ||= {}; d[it.jednostka_bazowa] = Math.round(((d[it.jednostka_bazowa] || 0) + it.ilosc_bazowa) * 1000) / 1000;
    }
    S.result.zakres_suma = tot; return tot;
  }
  const fmtN = v => v == null ? "" : v.toLocaleString("pl-PL", {maximumFractionDigits:3});
  function scopeHtml(){
    const items = S.result.zakres_rzeczowy || [];
    const rows = items.map((it, i) => {
      const flag = !it.cytat ? `<span class="src none" title="Pozycja dodana ręcznie albo bez cytatu">bez cytatu</span>`
        : (it.zrodlo === "brak" || it.liczba_w_cytacie === false) ? `<span class="src part" title="${it.liczba_w_cytacie===false ? "Liczba nie występuje w cytacie" : "Cytatu nie ma w tekście"} – sprawdź">sprawdź</span>`
        : `<span class="src ok">✓</span>`;
      const extra = (it.z_reguly ? ` <span class="src rule" title="Model pominął tę pozycję – znaleziona w tekście regułą (liczba + jednostka + opis)">z tekstu (reguła)</span>` : "")
        + (it.poprawka ? ` <span class="src part" title="${esc(it.poprawka)}">jedn. poprawiona</span>` : "");
      return `<tr>
        <td><select data-scope="${i}" data-p="element">${ELEM.map(e => `<option${e===it.element?" selected":""}>${esc(e)}</option>`).join("")}</select>
          ${it.opis && it.opis !== it.element ? `<div class="muted small">${esc(it.opis)}</div>` : ""}</td>
        <td><input data-scope="${i}" data-p="ilosc" data-num="1" value="${it.ilosc ?? ""}" class="num"></td>
        <td><select data-scope="${i}" data-p="jednostka">${UNITS.map(u => `<option${u===it.jednostka?" selected":""}>${u}</option>`).join("")}</select></td>
        <td><input data-scope="${i}" data-p="parametry" value="${esc(it.parametry || "")}" placeholder="np. DN 200, PVC"></td>
        <td class="q">${flag}${extra} ${it.cytat ? `<span class="quote ${it.span?"":"nolink"}" data-span="${it.span ? it.span.join(",") : ""}">${esc(it.cytat)}</span>` : ""}</td>
        <td><button type="button" class="x" data-sdel="${i}" title="Usuń pozycję">×</button></td></tr>`;
    }).join("");
    return `<div class="scope-box" style="--c:${SCOPE_C}"><div class="lab">Zakres rzeczowy w liczbach <span class="muted" style="text-transform:none;letter-spacing:0">(${items.length})</span></div>
      ${items.length ? `<div class="scope-wrap"><table class="scope"><thead><tr><th>Element</th><th>Ilość</th><th>Jedn.</th><th>Parametry</th><th>Źródło</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="muted small" style="margin:4px 0 8px">Model nie znalazł w tekście ilości (długości, sztuk, powierzchni…). Możesz dodać je ręcznie.</p>`}
      <div class="scope-sum" id="scopeSum">${scopeSumHtml()}</div>
      <button type="button" class="ghost small" id="scopeAdd">+ Dodaj pozycję</button></div>`;
  }
  function scopeSumHtml(){
    const tot = scopeTotals(), parts = [];
    for(const [el, units] of Object.entries(tot)) for(const [u, v] of Object.entries(units)) parts.push(`${esc(el)}: <b>${fmtN(v)} ${esc(u)}</b>`);
    return parts.length ? "Razem (w jednostkach bazowych, km → m): " + parts.join(" · ") : "";
  }

  function render(){
    const r = S.result; if(!r) return;
    $("btnCopy").disabled = $("btnDownload").disabled = false;
    const cards = FIELDS.map(F => {
      const f = r[F.key] || {};
      const hasVal = F.key==="planowana_kwota" ? f.kwota!=null : F.key==="planowany_czas" ? (f.liczba!=null||f.termin_do||f.data_podpisania) : !!f.wartosc;
      return `<div class="field ${hasVal?"":"empty-val"} ${F.wide?"wide":""}" style="--c:${F.c}">
        <div class="lab">${F.label}${srcBadge(f, F.key)}</div>
        ${fieldBody(F.key, f)}
        ${f.cytat ? `<div class="quote ${f.span?"":"nolink"}" data-span="${f.span?f.span.join(","):""}" title="${f.span?"Pokaż w tekście":"Cytatu nie znaleziono dosłownie w tekście"}">${esc(f.cytat)}</div>` : ""}
        ${notesHtml(F.key, f)}
      </div>`;
    }).join("");
    const kws = r.slowa_kluczowe || [];
    const usedCats = [...new Set(kws.map(k=>k.kategoria))];
    S.result.zakres_rzeczowy ||= [];
    $("result").innerHTML = `<div class="fields">${cards}</div>
      ${scopeHtml()}
      <div class="kw-box"><div class="lab">Słowa kluczowe inwestycji <span class="muted" style="text-transform:none;letter-spacing:0">(${kws.length})</span></div>
        <div class="chips" id="chips">${chipsHtml()}</div>
        <div class="legend">${usedCats.map(c=>`<span style="--c:${KAT[c]||KAT.inne}">${esc(c)}</span>`).join("")}</div>
        <div class="kw-add"><input id="kwNew" placeholder="Dodaj słowo kluczowe…"><select id="kwCat">${Object.keys(KAT).map(c=>`<option>${c}</option>`).join("")}</select><button type="button" id="kwAdd">Dodaj</button></div>
      </div>`;
    document.querySelectorAll("[data-pretty]").forEach(el => el.innerHTML = pretty(el.dataset.pretty));
    const m = S.meta;
    if(m) $("meta").innerHTML = `Model <b>${esc(m.model)}</b> (${esc(m.backend)}) · ${m.czas_s} s` +
      (m.statystyki?.tokens_in ? ` · tokeny: ${m.statystyki.tokens_in} → ${m.statystyki.tokens_out}` : "") +
      ` · ${m.dlugosc_tekstu.toLocaleString("pl-PL")} znaków. Pola można poprawić – eksport zawiera poprawione wartości.` +
      (m.wynik_plik ? `<br>Zapisano wynik: <code>${esc(m.wynik_plik)}</code>` : "") +
      (m.debug_plik ? `<br>Zapis debug: <code>${esc(m.debug_plik)}</code>` : "") + rawBox(m.surowa_tresc);
  }
  // uwagi weryfikacji: co odrzucono z odpowiedzi modelu i co uzupełniono regułą z tekstu
  const RULE_LABEL = {data_podpisania:"data podpisania umowy", termin_pierwotny:"termin pierwotny", termin_do:"nowy termin zakończenia"};
  function notesHtml(key, f){
    const out = [];
    if(f.odrzucone) out.push(`<div class="rej" title="Odpowiedź modelu nie przeszła weryfikacji z tekstem">✕ Odrzucono odpowiedź modelu: „${esc(f.odrzucone.wartosc)}” – ${esc(f.odrzucone.powod)}</div>`);
    if(Array.isArray(f.z_reguly) && f.z_reguly.length) out.push(`<div class="rule">↳ Uzupełniono z tekstu regułą: ${f.z_reguly.map(x => esc(RULE_LABEL[x] || x)).join(", ")}</div>`);
    if(key === "wojewodztwo" && f.wywnioskowane && f.wartosc) out.push(`<div class="rule">Województwo nie pada w tekście – to wniosek modelu, sprawdź je.</div>`);
    return out.join("");
  }
  // surowa odpowiedź modelu (debug)
  function rawBox(content){
    if(content == null) return "";
    let pretty = content;
    try{ pretty = JSON.stringify(JSON.parse(content), null, 2); }catch{}
    return `<details class="raw"><summary>Surowa odpowiedź modelu (debug)</summary><pre>${esc(pretty)}</pre></details>`;
  }
  function chipsHtml(){
    return (S.result.slowa_kluczowe||[]).map((k,i) => `<span class="chip ${k.span?"link":""}" style="--c:${KAT[k.kategoria]||KAT.inne}" data-i="${i}" title="${esc(k.kategoria)}${k.span?" – kliknij, aby pokazać w tekście":""}">
      ${esc(k.fraza)} <small>${esc(k.kategoria)}</small><button type="button" class="x" data-del="${i}" title="Usuń">×</button></span>`).join("");
  }

  // ---- tekst z zaznaczeniami ------------------------------------------------
  function showMarked(){
    const r = S.result, t = S.text; if(!r) return;
    const spans = [];
    FIELDS.forEach(F => { const s = r[F.key]?.span; if(s) spans.push({a:s[0], b:s[1], c:F.c, label:F.label, cls:""}); });
    (r.zakres_rzeczowy||[]).forEach(z => { if(z.span) spans.push({a:z.span[0], b:z.span[1], c:SCOPE_C, label:`${z.element}: ${z.ilosc} ${z.jednostka}`, cls:""}); });
    (r.slowa_kluczowe||[]).forEach(k => { if(k.span) spans.push({a:k.span[0], b:k.span[1], c:KAT[k.kategoria]||KAT.inne, label:k.fraza, cls:"kw"}); });
    spans.sort((x,y) => x.a-y.a || (y.b-y.a)-(x.b-x.a));
    let out = "", pos = 0;
    for(const s of spans){
      if(s.a < pos) continue;            // nakładające się fragmenty – zostaje pierwszy
      out += esc(t.slice(pos, s.a)) + `<mark class="${s.cls}" style="--c:${s.c}" data-a="${s.a}" title="${esc(s.label)}">${esc(t.slice(s.a, s.b))}</mark>`;
      pos = s.b;
    }
    out += esc(t.slice(pos));
    $("marked").innerHTML = out;
    $("marked").hidden = false; $("text").style.display = "none"; $("btnEdit").hidden = false;
  }
  function showEditor(){ $("marked").hidden = true; $("text").style.display = ""; $("btnEdit").hidden = true; $("text").focus(); }
  function flash(a){
    if($("marked").hidden) showMarked();
    const m = $("marked").querySelector(`mark[data-a="${a}"]`);
    if(m){ m.scrollIntoView({behavior:"smooth", block:"center"}); m.classList.remove("flash"); void m.offsetWidth; m.classList.add("flash"); }
  }

  // ---- edycja wyników --------------------------------------------------------
  $("result").addEventListener("input", e => {
    const el = e.target;
    if(el.dataset.scope != null && S.result){
      const it = S.result.zakres_rzeczowy[+el.dataset.scope]; let v = el.value;
      if(el.dataset.num){ v = v.trim()==="" ? null : Number(v.replace(/\s/g,"").replace(",", ".")); if(Number.isNaN(v)) return; }
      it[el.dataset.p] = v; $("scopeSum").innerHTML = scopeSumHtml(); return;
    }
    const k = el.dataset.k, p = el.dataset.p; if(!k || !S.result) return;
    let v = el.value;
    if(el.dataset.num){ v = v.trim()==="" ? null : Number(String(v).replace(/\s/g,"").replace(",", ".")); if(Number.isNaN(v)) return; }
    S.result[k][p] = v;
    if(k==="wojewodztwo") S.result[k].wywnioskowane = false;
    const pr = document.querySelector(`[data-pretty="${k}"]`); if(pr) pr.innerHTML = pretty(k);
    el.closest(".field").classList.toggle("empty-val", v===null || v==="");
  });
  $("result").addEventListener("click", e => {
    const q = e.target.closest(".quote[data-span]"); if(q && q.dataset.span){ flash(q.dataset.span.split(",")[0]); return; }
    const del = e.target.closest("[data-del]");
    if(del){ S.result.slowa_kluczowe.splice(+del.dataset.del, 1); $("chips").innerHTML = chipsHtml(); showMarked(); return; }
    const chip = e.target.closest(".chip.link"); if(chip){ const k = S.result.slowa_kluczowe[+chip.dataset.i]; if(k?.span) flash(k.span[0]); return; }
    if(e.target.id === "kwAdd") addKw();
    const sdel = e.target.closest("[data-sdel]");
    if(sdel){ S.result.zakres_rzeczowy.splice(+sdel.dataset.sdel, 1); rerenderScope(); showMarked(); return; }
    if(e.target.id === "scopeAdd"){
      S.result.zakres_rzeczowy.push({element:"inne", opis:"", ilosc:null, jednostka:"m", parametry:"", cytat:"", span:null});
      rerenderScope(); document.querySelector(`[data-scope="${S.result.zakres_rzeczowy.length-1}"][data-p="element"]`)?.focus();
    }
  });
  function rerenderScope(){ const box = document.querySelector(".scope-box"); if(box) box.outerHTML = scopeHtml(); }
  $("result").addEventListener("change", e => { if(e.target.tagName === "SELECT" || e.target.type === "date") e.target.dispatchEvent(new Event("input", {bubbles:true})); });
  $("result").addEventListener("keydown", e => { if(e.target.id==="kwNew" && e.key==="Enter"){ e.preventDefault(); addKw(); } });
  function addKw(){
    const fr = $("kwNew").value.trim(); if(!fr) return;
    const low = S.text.toLowerCase(), i = low.indexOf(fr.toLowerCase());
    S.result.slowa_kluczowe.push({fraza:fr, kategoria:$("kwCat").value, w_tekscie:i>=0, span:i>=0?[i,i+fr.length]:null});
    $("kwNew").value = ""; $("chips").innerHTML = chipsHtml(); showMarked();
  }
  $("marked").addEventListener("dblclick", showEditor);

  // ---- eksport ------------------------------------------------------------------
  function exportJson(){
    const r = S.result;
    return {
      zamawiajacy: r.zamawiajacy.wartosc || null,
      rodzaj_zamowienia: r.rodzaj_zamowienia.wartosc || null,
      rodzaj_zamowienia_kod: ({"roboty budowlane":"Works", dostawy:"Delivery", "usługi":"Services"})[r.rodzaj_zamowienia.wartosc] || null,
      miejscowosc: r.miejscowosc.wartosc || null,
      wojewodztwo: r.wojewodztwo.wartosc || null,
      wojewodztwo_kod: r.wojewodztwo.wartosc ? "PL" + String((WOJ.indexOf(r.wojewodztwo.wartosc)+1)*2).padStart(2,"0") : null,
      wojewodztwo_wywnioskowane: !!r.wojewodztwo.wywnioskowane,
      planowana_kwota: r.planowana_kwota.kwota!=null ? {kwota:r.planowana_kwota.kwota, waluta:r.planowana_kwota.waluta||"PLN", vat:r.planowana_kwota.vat} : null,
      planowany_czas: (c => (c.liczba!=null || c.termin_do || c.data_podpisania) ? (computeTime(c), {
        liczba:c.liczba, jednostka:c.jednostka||null, data_podpisania:c.data_podpisania||null, termin_do:c.termin_do||null,
        termin_wyliczony:c.termin_wyliczony||null, termin_pierwotny:c.termin_pierwotny||null, przedluzenie_dni:c.przedluzenie_dni,
        dni:c.dni ?? null, podstawa:c.podstawa||null, uwaga:c.uwaga||null}) : null)(r.planowany_czas),
      zakres_rzeczowy: (r.zakres_rzeczowy||[]).filter(z => z.ilosc != null).map(z => (scopeBase(z), {element:z.element, opis:z.opis||null,
        ilosc:z.ilosc, jednostka:z.jednostka, ilosc_bazowa:z.ilosc_bazowa, jednostka_bazowa:z.jednostka_bazowa, parametry:z.parametry||null, cytat:z.cytat||null})),
      zakres_suma: scopeTotals(),
      slowa_kluczowe: (r.slowa_kluczowe||[]).map(k => ({fraza:k.fraza, kategoria:k.kategoria})),
      cytaty: Object.fromEntries(FIELDS.map(F => [F.key, r[F.key]?.cytat || null])),
      model: S.meta?.model, data_analizy: new Date().toISOString(),
    };
  }
  $("btnCopy").addEventListener("click", () => navigator.clipboard?.writeText(JSON.stringify(exportJson(), null, 2)).then(() => toast("Skopiowano JSON")));
  $("btnDownload").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify({...exportJson(), tekst:S.text}, null, 2)], {type:"application/json"});
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
    const name = (S.result.zamawiajacy.wartosc || "inwestycja").replace(/[^\p{L}\p{N}]+/gu, "_").slice(0,40);
    a.download = `smart_kraken_${name}.json`; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href), 2000);
  });

  // ---- wejście: plik, przykład, skróty ------------------------------------------
  // .txt/.md/.csv/.html czyta przeglądarka; .pdf/.docx/.odt – serwer (api/file), lokalnie, bez wysyłania w świat
  const TEXT_EXT = ["txt","md","csv","html","htm"], SERVER_EXT = ["pdf","docx","odt"];
  // udział „dziwnych” znaków – krzaki z czcionek PDF, dane binarne czytane jako tekst
  function garbledRatio(t){
    const sample = (t || "").slice(0, 20000); if(!sample.trim()) return 1;
    const odd = (sample.match(/[\uE000-\uF8FF\uFFFD\x00-\x08\x0B\x0C\x0E-\x1F]|\(cid:\d+\)/g) || []).join("").length;
    const ok = (sample.match(/[\p{L}\p{N}\s.,;:!?()\[\]{}"'„”«»\-–—\/\\%&*+=<>#@§|°€$~^`]/gu) || []).length;
    return Math.max(odd / sample.length, 1 - ok / sample.length);
  }
  async function readFile(file){
    if(!file) return;
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    if(ext === "doc"){ toast("Stary format Word (.doc) nie jest obsługiwany – zapisz plik jako .docx."); return; }
    if(![...TEXT_EXT, ...SERVER_EXT].includes(ext)){ toast(`Nieobsługiwany typ pliku (.${ext}). Obsługiwane: PDF, DOCX, ODT, TXT, MD, CSV, HTML.`); return; }
    let txt, info = "";
    if(SERVER_EXT.includes(ext)){
      if(file.size > 30e6){ toast("Plik jest za duży (maks. 30 MB)."); return; }
      const lab = document.querySelector('label[for="fileInput"]'), old = lab.textContent;
      lab.innerHTML = `<span class="spinner" style="border-color:var(--line2);border-top-color:var(--acc)"></span> Czytam ${ext.toUpperCase()}…`; lab.title = "Skany PDF są rozpoznawane przez OCR – to może potrwać kilka sekund na stronę";
      try{
        const r = await fetch("api/file", {method:"POST", headers:{"X-Filename": encodeURIComponent(file.name), "Content-Type":"application/octet-stream"}, body:file});
        const j = await r.json().catch(() => ({title:`HTTP ${r.status}`}));
        if(!r.ok){ banner(`<b>Nie udało się wczytać pliku ${esc(file.name)}</b><br>${esc(j.title || "")}${j.detail?`<br><small>${esc(j.detail)}</small>`:""}`, "err"); return; }
        txt = j.tekst;
        info = (j.strony ? `${j.strony} ${j.strony===1?"strona":(j.strony%10>=2&&j.strony%10<=4&&(j.strony%100<10||j.strony%100>=20))?"strony":"stron"}, ` : "") + `${j.znaki.toLocaleString("pl-PL")} znaków`;
        const notes = [...(j.ostrzezenia || [])];
        if(j.ocr) notes.unshift(`Ten PDF to obraz strony – tekst rozpoznano przez OCR${j.ocr_strony ? ` (pierwsze ${j.ocr_strony} stron)` : ""}. Sprawdź go przed analizą: OCR może pomylić litery, liczby i kwoty.`);
        banner(notes.length ? notes.map(esc).join("<br>") : "", "warn");
        if(j.ocr) info = "OCR, " + info;
      }catch{
        banner("Nie udało się wysłać pliku do serwera aplikacji. Czy działa <code>python serwer.py</code>?", "err"); return;
      }finally{ lab.textContent = old; }
    } else {
      if(file.size > 3e6){ toast("Plik jest za duży (maks. 3 MB tekstu)."); return; }
      const buf = await file.arrayBuffer();
      txt = new TextDecoder("utf-8").decode(buf);
      if(txt.includes("\uFFFD")){ try{ txt = new TextDecoder("windows-1250").decode(buf); }catch{} }   // polskie pliki z Windows
      if(/^html?$/.test(ext)){ const d = new DOMParser().parseFromString(txt, "text/html"); d.querySelectorAll("script,style").forEach(n=>n.remove()); txt = d.body.innerText || d.body.textContent; }
      txt = txt.trim();
    }
    const bad = garbledRatio(txt);
    if(bad > 0.1){
      banner(`<b>Tekst z pliku ${esc(file.name)} wygląda na nieczytelny</b> (${Math.round(bad*100)}% nietypowych znaków), więc nie został wczytany.
        ${TEXT_EXT.includes(ext) ? "Plik ma rozszerzenie tekstowe, ale zawiera dane binarne – sprawdź, czy to na pewno plik tekstowy." :
          "Otwórz plik, zaznacz tekst i wklej go do pola. Jeśli skopiowany tekst też jest nieczytelny, dokument jest skanem lub ma czcionki bez mapowania liter – potrzebne OCR."}`, "err");
      return;
    }
    $("text").value = txt; updateChars(); showEditor(); saveDraft();
    toast(`Wczytano: ${file.name}${info ? ` (${info})` : ""}`);
  }
  $("fileInput").addEventListener("change", e => { readFile(e.target.files[0]); e.target.value = ""; });
  const wrap = document.querySelector(".editor-wrap");
  ["dragenter","dragover"].forEach(ev => wrap.addEventListener(ev, e => { e.preventDefault(); $("drop").classList.add("on"); }));
  ["dragleave","drop"].forEach(ev => wrap.addEventListener(ev, e => { e.preventDefault(); $("drop").classList.remove("on"); }));
  wrap.addEventListener("drop", e => readFile(e.dataTransfer.files[0]));

  const EXAMPLE = `Gmina Rydzyna (powiat leszczyński) przygotowuje inwestycję pn. „Budowa sieci kanalizacji sanitarnej wraz z przyłączami w miejscowości Kłoda”. Zakres obejmuje wykonanie ok. 3,6 km kanalizacji grawitacyjnej z rur PVC-U DN 200, 1,2 km rurociągu tłocznego PE DN 110, dwóch sieciowych przepompowni ścieków oraz 85 przyłączy do granic działek. Część odcinków pod drogą wojewódzką nr 309 zostanie wykonana metodą przewiertu sterowanego.

Zadanie będzie realizowane w formule „zaprojektuj i wybuduj” – wykonawca opracuje dokumentację projektową i uzyska pozwolenie na budowę. Wartość szacunkowa zamówienia to 4,2 mln zł netto; gmina zabezpieczyła środki w WPF na lata 2027–2028 i ubiega się o dofinansowanie z programu FEnIKS. Planowany termin realizacji: 20 miesięcy od dnia podpisania umowy.

Ryzyka: kolizje z istniejącą siecią gazową, wysoki poziom wód gruntowych w rejonie ul. Polnej oraz konieczność uzyskania zgody zarządcy drogi wojewódzkiej.`;
  $("btnExample").addEventListener("click", () => { $("text").value = EXAMPLE; updateChars(); showEditor(); saveDraft(); });
  $("btnClear").addEventListener("click", () => { $("text").value = ""; updateChars(); showEditor(); saveDraft(); });
  $("btnEdit").addEventListener("click", showEditor);
  $("btnAnalyze").addEventListener("click", analyze);
  // „Przerwij”: serwer zamyka połączenie z modelem, więc Ollama przestaje liczyć (wentylatory cichną)
  $("btnCancel").addEventListener("click", () => {
    if(S.reqId) fetch("api/cancel", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id:S.reqId})}).catch(()=>{});
    S.ctrl?.abort();
  });
  function updateChars(){
    const n = $("text").value.length, max = S.status?.max_znakow, el = $("chars");
    el.textContent = `${n.toLocaleString("pl-PL")}${max ? " / ~" + max.toLocaleString("pl-PL") : ""} znaków`;
    el.style.color = max && n > max ? "var(--warn)" : "";
    el.title = max ? (n > max ? `Model zmieści ok. ${max.toLocaleString("pl-PL")} znaków (okno ${S.status.ctx} tokenów) – reszta zostanie pominięta.` :
      `Limit wynika z okna kontekstu modelu (${S.status.ctx} tokenów).`) : "";
  }
  $("text").addEventListener("input", () => { updateChars(); clearTimeout(saveDraft._t); saveDraft._t = setTimeout(saveDraft, 400); });
  document.addEventListener("keydown", e => { if((e.ctrlKey||e.metaKey) && e.key==="Enter"){ e.preventDefault(); analyze(); } });

  // ---- start ------------------------------------------------------------------------
  $("text").value = loadDraft(); updateChars();
  checkStatus().then(s => { if(s && !s.model_ready){ const iv = setInterval(async () => { const r = await checkStatus(); if(r?.model_ready) clearInterval(iv); }, 15000); } });
})();
