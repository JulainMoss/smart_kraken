/* Risk Miner – interfejs użytkownika (bez zależności zewnętrznych). */
(() => {
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const app = () => $("#app");

const SUB = {siec_wod:"Sieć wodociągowa", kan_san:"Kanalizacja sanitarna", oczyszczalnia:"Oczyszczalnia ścieków",
  suw:"Uzdatnianie i zaopatrzenie w wodę", deszcz:"Kanalizacja deszczowa i retencja", inne_wk:"Inne obiekty wod-kan"};
const PROV = {PL02:"dolnośląskie",PL04:"kujawsko-pomorskie",PL06:"lubelskie",PL08:"lubuskie",PL10:"łódzkie",PL12:"małopolskie",PL14:"mazowieckie",
  PL16:"opolskie",PL18:"podkarpackie",PL20:"podlaskie",PL22:"pomorskie",PL24:"śląskie",PL26:"świętokrzyskie",PL28:"warmińsko-mazurskie",PL30:"wielkopolskie",PL32:"zachodniopomorskie"};
const SECT = {"wod-kan":"Gospodarka wodno-ściekowa", drogi:"Drogi i mosty", kubatura:"Budynki (szkoły, hale, termomodernizacja)",
  energetyka:"Energetyka i OZE", kolej:"Kolej i tramwaje", inne:"Inne"};
const WORKS = ["budowa","rozbudowa","przebudowa","modernizacja","remont/renowacja"];
const FEATS = {przepompownia:"przepompownia ścieków", tloczny:"rurociąg tłoczny", grawitacyjna:"kanalizacja grawitacyjna", podcisnieniowa:"kanalizacja podciśnieniowa",
  przylacza:"przyłącza / sięgacze", bezwykopowo:"technologia bezwykopowa", droga_kraj_woj:"przejście pod drogą woj./krajową", kolej:"skrzyżowanie z koleją",
  ciek:"przekroczenie cieku / rzeki", natura2000:"obszar chroniony / Natura 2000", wody_gruntowe:"wysokie wody gruntowe / trudne grunty",
  tereny_prywatne:"grunty prywatne", nawierzchnia:"odtworzenie nawierzchni", obszar_wiejski:"obszar wiejski", miasto:"teren miejski",
  rozruch:"rozruch technologiczny", dofinansowanie_ue:"dofinansowanie UE/KPO", duze_srednice:"duże średnice / magistrala"};
const STATUS_EV = {pending:["niezweryfikowane","grey"], approved:["zatwierdzone","ok"], rejected:["odrzucone","bad"], corrected:["poprawione","ok"]};
const LINK_ST = {auto:["pewne","ok"], approved:["zatwierdzone","ok"], uncertain:["niepewne – do decyzji","warn"], rejected:["odrzucone","bad"]};
const KIND_TAG = {DANE:'<span class="tag src" title="Dane z ogłoszeń i dokumentów – z linkiem do źródła">Dane źródłowe</span>',
  OBLICZ:'<span class="tag calc" title="Obliczenia statystyczne na danych – z liczebnością próby">Obliczenia</span>',
  "OBLICZ+DANE":'<span class="tag calc">Obliczenia</span> <span class="tag src">Dane źródłowe</span>',
  WNIOSEK:'<span class="tag concl" title="Wniosek z reguł eksperckich lub modelu – wymaga oceny eksperta">Wniosek / rekomendacja</span>'};

let CATALOG = null;
const S = { lastReport: null, analysisText: "" };

/* ---------------------------------------------------------------- narzędzia */
function toast(msg, ms = 2800){ const t = $("#toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", ms); }
async function api(path, opts = {}){
  const o = {...opts};
  if(o.json !== undefined){ o.method = o.method || "POST"; o.headers = {...(o.headers||{}), "Content-Type":"application/json"}; o.body = JSON.stringify(o.json); delete o.json; }
  const r = await fetch(path, o);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if(!r.ok) throw new Error((data && data.error) || `Błąd ${r.status}`);
  return data;
}
const pln = v => v == null || isNaN(v) ? "b.d." : (Math.abs(v) >= 1e6 ? (v/1e6).toLocaleString("pl-PL",{maximumFractionDigits:2}) + " mln zł" :
  Math.abs(v) >= 1e3 ? Math.round(v/1e3).toLocaleString("pl-PL") + " tys. zł" : Math.round(v).toLocaleString("pl-PL") + " zł");
const pct = (v, d = 0) => v == null ? "b.d." : (v*100).toLocaleString("pl-PL",{maximumFractionDigits:d}) + "%";
const sgn = (v, d = 1) => v == null ? "b.d." : (v > 0 ? "+" : "") + v.toLocaleString("pl-PL",{maximumFractionDigits:d}) + "%";
const days = d => d == null ? "b.d." : (d >= 60 ? `${Math.round(d)} dni (≈${(d/30.44).toLocaleString("pl-PL",{maximumFractionDigits:1})} mies.)` : `${Math.round(d)} dni`);
const tag = (txt, cls) => `<span class="tag ${cls}">${esc(txt)}</span>`;
const ic = (n, cls = "") => `<svg class="ic ${cls}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const cnt = n => `<span class="counter">${n}</span>`;
const evTag = st => tag(...(STATUS_EV[st] || [st, "grey"]));
const yesno = v => v === 1 || v === true ? "tak" : v === 0 || v === false ? "nie" : "b.d.";
function openModal(title, html){ $("#modalTitle").textContent = title; $("#modalBody").innerHTML = html; $("#modal").hidden = false; }
function closeModal(){ $("#modal").hidden = true; }
$("#modalClose").onclick = closeModal;
$("#modal").addEventListener("click", e => { if(e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", e => { if(e.key === "Escape") closeModal(); });
function riskName(code){ return (CATALOG||[]).find(r => r.code === code)?.name || (code === "BRAK" ? "do klasyfikacji" : code); }
async function loadCatalog(){ CATALOG = await api("/api/catalog"); return CATALOG; }
function bar(share, ci){ return `<div class="bar" title="${pct(share)}${ci?` (90% CI ${pct(ci[0])}–${pct(ci[1])})`:""}">${ci?`<span class="ci" style="left:${ci[0]*100}%;width:${(ci[1]-ci[0])*100}%"></span>`:""}<span style="width:${Math.max(2,share*100)}%"></span></div>`; }
function download(url){ const a = document.createElement("a"); a.href = url; a.click(); }

/* ---------------------------------------------------------------- nawigacja */
const ROUTES = {
  "analiza": viewAnalysis, "raport": viewSavedReport, "projekty": viewProjects, "projekt": viewProject,
  "baza": viewCases, "ekspert": viewExpert, "import": viewImport, "o-systemie": viewAbout, "przetworz": viewProcess,
};
async function route(){
  const h = location.hash.replace(/^#\/?/, "") || "analiza";
  const [name, ...rest] = h.split("/");
  $$("#nav a").forEach(a => a.classList.toggle("active", a.dataset.route === name || (name === "projekt" && a.dataset.route === "projekty") || (name === "raport" && a.dataset.route === "analiza")));
  const fn = ROUTES[name] || viewAnalysis;
  window.scrollTo(0, 0);
  try { await fn(...rest.map(decodeURIComponent)); }
  catch(e){ app().innerHTML = `<div class="notice bad"><b>Wystąpił błąd:</b> ${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", route);

async function refreshStatus(){
  try{
    const s = await api("/api/status");
    const d = s.db, L = s.llm || {};
    S.llm = L;
    const model = L.enabled ? `<span class="status" title="${esc(L.data_location||"")}"><span class="dot ${L.online?(L.fallback_for?"warn":"ok"):"bad"}"></span>${esc(L.model)}${L.fallback_for?" (zapasowy)":""}${L.online?"":" – niedostępny"}</span>`
      : `<span class="status" title="Parametry odczytują reguły eksperckie"><span class="dot"></span>bez modelu językowego</span>`;
    $("#dbinfo").innerHTML = `<span><b>${d.procedures.toLocaleString("pl-PL")}</b> postępowań</span><span><b>${d.completed.toLocaleString("pl-PL")}</b> zakończonych</span>`+
      `<span><b>${d.events.toLocaleString("pl-PL")}</b> zdarzeń ryzyka</span>${model}`;
    const b = $("#badgeReview");
    b.hidden = !d.links_uncertain; b.textContent = d.links_uncertain;
    return s;
  }catch{ $("#dbinfo").textContent = "brak połączenia z serwerem"; }
}

/* ================================================================ NOWA ANALIZA */
const EXAMPLES = [
  {t:"Kanalizacja w gminie wiejskiej – pełny opis", x:`Gmina Rydzyna (powiat leszczyński, województwo wielkopolskie) przygotowuje inwestycję pn. „Budowa sieci kanalizacji sanitarnej wraz z przyłączami w miejscowości Kłoda”. Zakres obejmuje wykonanie ok. 3,6 km kanalizacji grawitacyjnej z rur PVC-U DN 200, 1,2 km rurociągu tłocznego PE DN 110, dwóch sieciowych przepompowni ścieków oraz 85 przyłączy do granic działek. Część odcinków pod drogą wojewódzką nr 309 zostanie wykonana metodą przewiertu sterowanego.

Zadanie będzie realizowane w formule „zaprojektuj i wybuduj” – wykonawca opracuje dokumentację projektową i uzyska pozwolenie na budowę. Wartość szacunkowa zamówienia to 4,2 mln zł netto; gmina zabezpieczyła środki w WPF na lata 2027–2028 i ubiega się o dofinansowanie z programu FEnIKS. Planowany termin realizacji: 14 miesięcy od dnia podpisania umowy.

Znane trudności: wysoki poziom wód gruntowych w rejonie ul. Polnej, część trasy przebiega przez działki prywatne.`},
  {t:"Zbyt mały budżet i zbyt krótki termin", x:`Budowa kanalizacji sanitarnej grawitacyjnej i tłocznej o długości 15 km z 5 przepompowniami w gminie Kłodawa, woj. lubuskie. Budżet 3 mln zł netto, realizacja w 6 miesięcy.`},
  {t:"Opis potoczny, jak w mailu", x:`Dzień dobry, chcemy w przyszłym roku zbudować kanalizację w naszej gminie – mniej więcej 5 km sieci i ze 40 przyłączy do domów. Mamy na to około 6 mln zł brutto i rok na realizację. Część trasy idzie wzdłuż drogi powiatowej. Na co powinniśmy uważać?`},
  {t:"Oczyszczalnia ścieków – zaprojektuj i wybuduj", x:`Gmina Wadowice (woj. małopolskie) planuje modernizację i rozbudowę oczyszczalni ścieków do przepustowości 1200 m3/d w formule zaprojektuj i wybuduj. Wymagany rozruch technologiczny i osiągnięcie parametrów oczyszczania. Wartość 24 mln zł netto, czas realizacji 30 miesięcy.`},
  {t:"Stacja uzdatniania wody z wodociągiem", x:`Budowa stacji uzdatniania wody wraz z siecią wodociągową o długości 4 km w gminie Juchnowiec Kościelny, woj. podlaskie. Budżet 5 mln zł netto, 10 miesięcy.`},
  {t:"Rzadka technologia: kanalizacja podciśnieniowa", x:`Budowa kanalizacji podciśnieniowej ze stacją próżniową dla 80 budynków w gminie nadmorskiej, woj. pomorskie. Długość sieci 2,5 km, budżet 4 mln zł, realizacja 12 miesięcy.`},
  {t:"Kanalizacja deszczowa i retencja", x:`Budowa kanalizacji deszczowej ze zbiornikiem retencyjnym i separatorem w mieście Łowicz, woj. łódzkie. Długość kolektorów 3,2 km, budżet 9 mln zł netto, 14 miesięcy. Inwestycja z dofinansowaniem z programu FEnIKS.`},
  {g:"Inne sektory", t:"Przebudowa drogi powiatowej z chodnikiem", x:`Powiat Wołomiński planuje przebudowę drogi powiatowej nr 4310W na odcinku 3,2 km wraz z budową chodnika, ścieżki rowerowej, odwodnienia i przebudową przepustów. Wartość 9 mln zł brutto, realizacja 12 miesięcy w formule zaprojektuj i wybuduj. Na trasie kolizje z siecią gazową i energetyczną, konieczna wycinka drzew i czasowa organizacja ruchu.`},
  {g:"Inne sektory", t:"Budowa mostu na rzece", x:`Rozbiórka i budowa nowego mostu w ciągu drogi gminnej na rzece Wieprz w miejscowości Krasnobród, długość obiektu 42 m, nośność 50 t. Budżet 7 mln zł brutto, 14 miesięcy. Wymagane pozwolenie wodnoprawne i objazd na czas robót.`},
  {g:"Inne sektory", t:"Termomodernizacja szkoły", x:`Gmina Krzeszowice planuje termomodernizację budynku szkoły podstawowej: docieplenie ścian i stropodachu, wymianę okien i drzwi, modernizację instalacji c.o. z pompą ciepła oraz instalację fotowoltaiczną 40 kW. Wartość 4,5 mln zł brutto, czas realizacji 10 miesięcy; część robót w trakcie zajęć szkolnych.`},
  {g:"Inne sektory", t:"Fotowoltaika na budynkach gminy", x:`Budowa instalacji fotowoltaicznych o łącznej mocy 600 kW na 12 budynkach użyteczności publicznej gminy Brzeg Dolny wraz z magazynem energii. Budżet 3,8 mln zł netto, 8 miesięcy, dofinansowanie z programu FEnIKS.`}];

async function viewAnalysis(){
  const draft = (() => { try{ return localStorage.getItem("rm_text") || ""; }catch{ return ""; } })();
  const L = S.llm || {};
  app().innerHTML = `
  <div class="page-title"><div><h1>Nowa analiza inwestycji</h1>
    <p>Opisz planowaną inwestycję własnymi słowami albo wczytaj dokument. System znajdzie podobne, zakończone inwestycje w Polsce,
    sprawdzi, jakie problemy w nich wystąpiły (opóźnienia, wzrost kosztów, zmiany umów), i przygotuje raport z rekomendacjami.</p></div>
    <a class="btn" href="#/raport">${ic("history")}Zapisane raporty</a></div>
  <div class="steps"><div class="step active"><span class="n">1</span>Opis inwestycji</div><div class="step"><span class="n">2</span>Sprawdzenie parametrów</div><div class="step"><span class="n">3</span>Raport</div></div>
  <div class="grid layout-side">
    <div>
      <div class="card">
        <div class="card-head"><h2>Opis planowanej inwestycji</h2><span class="muted" id="chars"></span></div>
        <textarea id="txt" aria-label="Opis planowanej inwestycji" placeholder="Np.: Gmina planuje budowę 3,6 km kanalizacji sanitarnej z dwiema przepompowniami i 85 przyłączami w formule zaprojektuj i wybuduj. Budżet 4,2 mln zł netto, termin 14 miesięcy…">${esc(draft)}</textarea>
        <div class="row" style="margin-top:12px">
          <label class="btn" for="fileIn">${ic("file")}Wczytaj dokument</label><span class="muted">DOCX, PDF, TXT</span>
          <input type="file" id="fileIn" hidden accept=".docx,.pdf,.txt,.doc,.odt,.rtf,.md,.html">
          <div class="spacer"></div>
          <button class="primary btn-lg" id="go">Analizuj opis</button>
        </div>
      </div>
      <p class="muted" style="font-size:12px">Skrót: Ctrl+Enter. Szkic opisu zapisuje się w tej przeglądarce.</p>
    </div>
    <aside>
      <div class="help"><h4>Co warto napisać</h4>
        <ul><li>rodzaj inwestycji (kanalizacja, droga, most, szkoła, fotowoltaika…)</li><li>skalę: długość sieci lub drogi, powierzchnię, moc, liczbę przyłączy</li>
          <li>planowany budżet i termin</li><li>formułę: gotowy projekt czy „zaprojektuj i wybuduj”</li>
          <li>miejsce i znane trudności (drogi, rzeki, grunty prywatne)</li></ul>
        <small>Brakujące informacje system wskaże w raporcie.</small></div>
      <div class="help"><h4>Przykłady</h4>${["Woda i kanalizacja","Inne sektory"].map(g => `<div class="muted" style="font-size:12px;margin:6px 0 2px">${g}</div>
        <ul class="side-list">${EXAMPLES.map((e,i)=>({...e,i})).filter(e=>(e.g||"Woda i kanalizacja")===g).map(e=>`<li><button class="link-btn" data-ex="${e.i}">${ic("file")}${esc(e.t)}</button></li>`).join("")}</ul>`).join("")}</div>
      <div class="help" style="border:none"><h4>Odczyt opisu</h4>
        <div class="model-box">${ic("cpu")}<div>${L.enabled ? `Parametry odczytuje model językowy <b>${esc(L.model)}</b>
          ${L.online ? tag("dostępny","ok") : tag("niedostępny – użyte zostaną reguły","warn")}<br><small>${esc(L.data_location || "")}</small>`
          : `Parametry odczytują reguły eksperckie (bez modelu językowego).<br><small>Model można włączyć w pliku <code>config.toml</code>.</small>`}</div></div></div>
    </aside>
  </div>`;
  const txt = $("#txt");
  const upd = () => { $("#chars").textContent = `${txt.value.length.toLocaleString("pl-PL")} znaków`; try{ localStorage.setItem("rm_text", txt.value); }catch{} };
  txt.addEventListener("input", upd); upd();
  $$("[data-ex]").forEach(b => b.onclick = () => { txt.value = EXAMPLES[+b.dataset.ex].x; upd(); txt.focus(); });
  $("#fileIn").onchange = async e => {
    const f = e.target.files[0]; if(!f) return;
    toast(`Wczytuję ${f.name}…`);
    try{
      const r = await api("/api/upload?purpose=analysis", {method:"POST", body:f, headers:{"X-Filename":encodeURIComponent(f.name)}});
      txt.value = r.text; upd(); toast(`Wczytano ${f.name}: ${r.pages} str., ${r.chars.toLocaleString("pl-PL")} znaków`);
    }catch(err){ toast("Nie udało się odczytać pliku: " + err.message, 5000); }
  };
  $("#go").onclick = () => runUnderstand(txt.value);
  txt.addEventListener("keydown", e => { if((e.ctrlKey||e.metaKey) && e.key === "Enter") runUnderstand(txt.value); });
}

async function runUnderstand(text){
  text = (text||"").trim();
  if(text.length < 15){ toast("Napisz kilka zdań o planowanej inwestycji."); return; }
  S.analysisText = text;
  const btn = $("#go"); btn.disabled = true;
  const useModel = S.llm?.enabled && S.llm?.online;
  const t0 = Date.now();
  const label = () => `<span class="spinner"></span> ${useModel ? `Model czyta opis… ${Math.round((Date.now()-t0)/1000)} s` : "Analizuję opis…"}`;
  btn.innerHTML = label();
  const timer = setInterval(() => { btn.innerHTML = label(); }, 1000);
  try{
    const q = await api("/api/understand", {json:{text}});
    viewParams(q);
  }catch(e){ toast(e.message, 5000); btn.disabled = false; btn.textContent = "Analizuj opis"; }
  finally{ clearInterval(timer); }
}

// szczegóły ekstrakcji modelem (riskminer/ekstrakcja.py): zakres rzeczowy w liczbach i wyliczenie czasu realizacji
function extractionHtml(e){
  if(!e) return "";
  const num = x => Number(x).toLocaleString("pl-PL", {maximumFractionDigits: 3});
  const rows = (e.zakres_rzeczowy || []).map(x => {
    const flag = x.z_reguly ? tag("z tekstu (reguła)", "grey") : (x.zrodlo === "brak" || x.liczba_w_cytacie === false) ? tag("sprawdź", "warn") : tag("model", "model");
    const fix = x.poprawka ? ` ${tag("jedn. poprawiona", "warn")}` : "";
    const base = x.jednostka !== x.jednostka_bazowa ? ` <span class="muted">(${num(x.ilosc_bazowa)} ${esc(x.jednostka_bazowa)})</span>` : "";
    return `<tr><td>${esc(x.element)}${x.opis && x.opis !== x.element ? `<div class="muted">${esc(x.opis)}</div>` : ""}</td>
      <td style="text-align:right;white-space:nowrap">${num(x.ilosc)} ${esc(x.jednostka)}${base}</td><td>${esc(x.parametry || "")}</td>
      <td>${flag}${fix}${x.cytat ? ` <span class="q">„${esc(x.cytat.slice(0, 110))}”</span>` : ""}</td></tr>`;
  }).join("");
  const sums = Object.entries(e.zakres_suma || {}).flatMap(([el, u]) => Object.entries(u).map(([j, v]) => `${esc(el)}: <b>${num(v)} ${esc(j)}</b>`));
  const t = e.planowany_czas || {};
  const time = [t.data_podpisania && `podpisanie umowy ${esc(t.data_podpisania)}`, t.liczba != null && t.jednostka && `czas trwania ${num(t.liczba)} ${esc(t.jednostka)}`,
    (t.termin_do || t.termin_wyliczony) && `koniec ${esc(t.termin_do || t.termin_wyliczony)}${t.termin_do ? "" : " (wyliczony)"}`,
    t.termin_pierwotny && `termin pierwotny ${esc(t.termin_pierwotny)}`, t.przedluzenie_dni && `przedłużenie o ${t.przedluzenie_dni} dni`].filter(Boolean);
  return `<div style="margin-top:16px"><label class="field">Zakres rzeczowy w liczbach</label>
    ${rows ? `<div class="table-wrap"><table><thead><tr><th>Element</th><th style="text-align:right">Ilość</th><th>Parametry</th><th>Źródło</th></tr></thead><tbody>${rows}</tbody></table></div>
      ${sums.length ? `<p class="muted" style="margin:6px 0 0">Razem (km → m): ${sums.join(" · ")}</p>` : ""}`
      : `<p class="muted" style="margin:0">Model nie znalazł w opisie ilości (długości, sztuk, powierzchni).</p>`}</div>
    ${t.dni != null ? `<div style="margin-top:12px"><label class="field">Czas realizacji – wyliczenie</label><div><b>${t.dni} dni</b> ${time.length ? "· " + time.join(" · ") : ""}
      <div class="muted">${esc(t.podstawa || "")}</div></div></div>` : ""}`;
}

const SRC_TAG = {"model": ["model", "model"], "model i reguły": ["model i reguły – zgodne", "ok"], "reguła": ["reguły", "grey"], "użytkownik": ["poprawione", "src"]};
function viewParams(q){
  const P = q.params, v = k => P[k]?.value, C = q.conflicts || {};
  const has = k => { const x = v(k); return x != null && x !== "" && x !== false && !(Array.isArray(x) && !x.length); };
  const src = k => { const p = P[k]; if(!p) return "";
    const [lab, cls] = SRC_TAG[p.source] || [p.source, "grey"];
    const conflict = C[k] ? `<div class="field-help conflict">${ic("alert")} Rozbieżność – ${esc(C[k])}. Sprawdź w opisie.</div>` : "";
    if(!has(k) && p.source !== "model") return `<div class="field-help">Nie podano w opisie – uzupełnij, jeśli znasz.</div>${conflict}`;
    return `<div class="field-help">${tag(lab, cls)}${p.quote ? ` <span class="q">„${esc(p.quote.slice(0,110))}”</span>` : ""}</div>${conflict}`; };
  const fc = k => has(k) ? "filled" : "";
  const months = v("duration_days") ? Math.round(v("duration_days")/30.44*10)/10 : "";
  const feats = new Set(v("features") || []);
  const li = q.llm_info;
  app().innerHTML = `
  <div class="page-title"><div><h1>Sprawdź parametry inwestycji</h1><p>Tak odczytaliśmy Twój opis. Popraw lub uzupełnij wartości – od nich zależy dobór podobnych inwestycji.</p></div></div>
  <div class="steps"><div class="step done"><span class="n">${ic("check")}</span>Opis inwestycji</div><div class="step active"><span class="n">2</span>Sprawdzenie parametrów</div><div class="step"><span class="n">3</span>Raport</div></div>
  ${(q.warnings||[]).map((w,i) => `<div class="flag ${q.vague && i===0 ? "red" : "amber"}">${ic(q.vague && i===0 ? "stop" : "alert")}<div>${esc(w)}</div></div>`).join("")}
  ${li ? `<div class="notice model"><div class="model-box">${ic("cpu")}<div><b>Formularz wypełnił model językowy ${esc(li.model)}</b> (${esc(li.where || "lokalnie")}, ${String(li.seconds).replace(".",",")} s).
    Każda wartość ma cytat z opisu, sprawdzony przez system; liczby porównano z regułami.${q.summary_llm ? `<br>Streszczenie: ${esc(q.summary_llm)}` : ""}</div></div></div>` : ""}
  <div class="card">
    <div class="card-head"><h2>Parametry wyszukiwania</h2><span class="legend">Źródło wartości: ${tag("model","model")} ${tag("model i reguły – zgodne","ok")} ${tag("reguły","grey")}</span></div>
    <div class="form-grid">
      <div class="${has("subsector") || (v("sector") && v("sector") !== "inne") ? "filled" : ""}"><label class="field" for="p_sub">Rodzaj inwestycji</label><select id="p_sub">
        <optgroup label="Gospodarka wodno-ściekowa">${Object.entries(SUB).map(([k,n])=>`<option value="${k}" ${v("subsector")===k?"selected":""}>${n}</option>`).join("")}</optgroup>
        <optgroup label="Inne sektory">${["drogi","kubatura","energetyka","kolej"].map(k=>`<option value="sector:${k}" ${!v("subsector") && v("sector")===k?"selected":""}>${SECT[k]}</option>`).join("")}
        <option value="" ${!v("subsector") && (!v("sector") || v("sector")==="inne" || v("sector")==="wod-kan")?"selected":""}>inny / nie wiem</option></optgroup></select>${src(has("subsector") ? "subsector" : "sector")}</div>
      <div class="${fc("works_type")}"><label class="field" for="p_wt">Rodzaj robót</label><select id="p_wt"><option value="">nie określono</option>${WORKS.map(w=>`<option ${v("works_type")===w?"selected":""}>${w}</option>`).join("")}</select>${src("works_type")}</div>
      <div class="${fc("budget")}" style="grid-column:span 2"><label class="field" for="p_budget">Budżet (zł) <span id="p_budget_fmt" class="muted" style="font-weight:400">${v("budget") ? "= " + pln(v("budget")) : ""}</span></label><div class="row" style="flex-wrap:nowrap"><input id="p_budget" type="number" min="0" step="1000" value="${v("budget") ?? ""}" placeholder="np. 4200000" style="flex:1"><select id="p_vat" style="width:150px" aria-label="VAT">${["netto","brutto","nieokreślone"].map(x=>`<option ${v("budget_vat")===x?"selected":""}>${x}</option>`).join("")}</select></div>${src("budget")}</div>
      <div class="${fc("duration_days")}"><label class="field" for="p_dur">Czas realizacji (miesiące)</label><input id="p_dur" type="number" min="0" step="0.5" value="${months}">${src("duration_days")}</div>
      <div class="${fc("length_km")}"><label class="field" for="p_len">Długość (km) – sieci, drogi, torów</label><input id="p_len" type="number" min="0" step="0.1" value="${v("length_km") ?? ""}">${src("length_km")}</div>
      <div class="${fc("connections")}"><label class="field" for="p_con">Liczba przyłączy</label><input id="p_con" type="number" min="0" step="1" value="${v("connections") ?? ""}">${src("connections")}</div>
      <div class="${fc("province")}"><label class="field" for="p_prov">Województwo</label><select id="p_prov"><option value="">nie określono</option>${Object.entries(PROV).map(([k,n])=>`<option value="${k}" ${v("province")===k?"selected":""}>${n}</option>`).join("")}</select>${src("province")}</div>
      <div><label class="field">Formuła realizacji</label><label class="check" style="margin-top:6px"><input type="checkbox" id="p_db" ${v("design_build")?"checked":""}> „zaprojektuj i wybuduj”</label>${src("design_build")}</div>
      ${has("org") || has("city") ? `<div><label class="field">Zamawiający / miejscowość</label><div style="padding-top:6px">${esc(v("org") || "–")}${v("city") ? ` · ${esc(v("city"))}` : ""}</div>${src(has("org") ? "org" : "city")}</div>` : ""}
    </div>
    <div style="margin-top:16px"><label class="field">Elementy techniczne i warunki</label><p class="muted" style="margin:0 0 8px">Zaznaczone zostały odczytane z opisu. Popraw, jeśli trzeba.</p>
      <div>${Object.entries(FEATS).map(([k,n])=>`<label class="chip ${feats.has(k)?"sel":""}"><input type="checkbox" data-feat="${k}" ${feats.has(k)?"checked":""}> ${n}</label>`).join("")}</div></div>
    ${q.search_terms?.length ? `<div style="margin-top:16px"><label class="field">Frazy do wyszukiwania</label><div>${q.search_terms.map(t=>`<span class="chip">${esc(t)}</span>`).join("")}</div></div>` : ""}
    ${extractionHtml(q.ekstrakcja)}
  </div>
  <div class="row"><button id="back">${ic("arrow-left")}Wróć do opisu</button><div class="spacer"></div>
    <span class="muted">Metoda: ${esc(q.method)}</span><button class="primary btn-lg" id="run">Wygeneruj raport</button></div>`;
  $$("[data-feat]").forEach(c => c.onchange = () => c.parentElement.classList.toggle("sel", c.checked));
  $("#back").onclick = () => viewAnalysis();
  $("#p_budget").oninput = () => { const x = Number($("#p_budget").value); $("#p_budget_fmt").textContent = x ? "= " + pln(x) : ""; };
  $("#run").onclick = async () => {
    const num = id => { const x = $(id).value; return x === "" ? null : Number(x); };
    const sv = $("#p_sub").value, isSect = sv.startsWith("sector:");
    const overrides = { subsector: isSect ? "" : sv, sector: isSect ? sv.slice(7) : (sv ? "wod-kan" : "inne"), works_type: $("#p_wt").value, budget: num("#p_budget"), budget_vat: $("#p_vat").value,
      duration_days: num("#p_dur") != null ? Math.round(num("#p_dur")*30.44) : null, length_km: num("#p_len"), connections: num("#p_con"),
      province: $("#p_prov").value || null, design_build: $("#p_db").checked, features: $$("[data-feat]").filter(c=>c.checked).map(c=>c.dataset.feat) };
    const btn = $("#run"); btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Szukam podobnych inwestycji…`;
    try{
      const rep = await api("/api/analyze", {json:{text:S.analysisText, query:q, overrides}});
      S.lastReport = rep; location.hash = `#/raport/${rep.analysis_id}`;
    }catch(e){ toast(e.message, 6000); btn.disabled = false; btn.textContent = "Wygeneruj raport"; }
  };
}

async function viewSavedReport(id){
  if(!id){
    const list = await api("/api/analyses");
    app().innerHTML = `<div class="page-title"><div><h1>Zapisane raporty</h1><p>Każda analiza jest zapisywana jako rekord – można ją otworzyć i wyeksportować.</p></div><a class="btn primary" href="#/analiza">${ic("plus")}Nowa analiza</a></div>
      <div class="card">${list.length ? `<div class="table-wrap"><table><thead><tr><th>Data</th><th>Tytuł</th><th>Identyfikator</th></tr></thead><tbody>
      ${list.map(a=>`<tr class="click" data-id="${a.analysis_id}"><td>${esc(a.created_at.replace("T"," "))}</td><td>${esc(a.title)}</td><td><code>${a.analysis_id}</code></td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty">Brak zapisanych raportów.</div>`}</div>`;
    $$("tr[data-id]").forEach(tr => tr.onclick = () => location.hash = `#/raport/${tr.dataset.id}`);
    return;
  }
  const rep = (S.lastReport && S.lastReport.analysis_id === id) ? S.lastReport : await api(`/api/analyses/${id}`);
  if(!CATALOG) await loadCatalog().catch(()=>{});
  renderReport(rep);
}

function renderReport(r){
  const F = r.forecast, t = F.time, c = F.cost, b = F.budget, td = F.tender, n = r.sample.n;
  const P = Object.fromEntries(Object.entries(r.query.params).map(([k,v])=>[k,v.value]));
  const dp = t.delay_probability;
  const lvl = x => x == null ? "" : x >= .55 ? "bad" : x >= .35 ? "warn" : "ok";
  const ch = c.change_pct || {};
  app().innerHTML = `
  <div class="page-title"><div><h1>Raport: ${esc(r.title)}</h1>
    <p>Analiza <code>${r.analysis_id}</code> · ${esc(r.created_at.replace("T"," "))} · porównano z <b>${n}</b> podobnymi, zakończonymi inwestycjami (baza: ${r.sample.db_completed_cases.toLocaleString("pl-PL")} zakończonych postępowań)</p></div>
    <div class="row no-print">
      <button onclick="window.print()">${ic("print")}Drukuj / PDF</button>
      <a class="btn" href="/api/analyses/${r.analysis_id}/export?format=md">${ic("download")}Markdown</a>
      <a class="btn" href="/api/analyses/${r.analysis_id}/export?format=csv">${ic("download")}CSV (Excel)</a>
      <a class="btn" href="/api/analyses/${r.analysis_id}/export?format=json">${ic("download")}JSON</a>
      <a class="btn primary" href="#/analiza">${ic("plus")}Nowa analiza</a></div></div>
  <div class="steps no-print"><div class="step done"><span class="n">${ic("check")}</span>Opis inwestycji</div><div class="step done"><span class="n">${ic("check")}</span>Sprawdzenie parametrów</div><div class="step active"><span class="n">3</span>Raport</div></div>
  <div class="legend" style="margin-bottom:10px">Oznaczenia w raporcie: ${KIND_TAG.DANE} ${KIND_TAG.OBLICZ} ${KIND_TAG.WNIOSEK}</div>
  ${r.sample.low_data ? `<div class="notice warn"><b>Mało danych o podobnych inwestycjach (N = ${n}).</b> Wyniki są orientacyjne. Zalecamy kontakt z zamawiającymi, którzy realizowali podobne zadania –
    <a href="#" id="goContacts">zobacz listę kontaktów</a>.</div>` : ""}
  <div class="grid g4" style="margin-bottom:16px">
    <div class="metric ${lvl(dp)}"><div class="lbl">Ryzyko opóźnienia</div><div class="val">${pct(dp)}</div>
      <div class="sub">${t.delay_k} z ${t.delay_n} umów po terminie<br>90% przedział: ${pct(t.delay_ci90[0])}–${pct(t.delay_ci90[1])}</div></div>
    <div class="metric ${c.share_increase >= .4 ? "warn" : ""}"><div class="lbl">Zmiana kosztu umowy</div><div class="val">${sgn(ch.median)}</div>
      <div class="sub">mediana · w ${pct(c.share_increase)} umów wzrost<br>10% najgorszych: ${sgn(ch.p90)} (N=${ch.n||0})</div></div>
    <div class="metric"><div class="lbl">Rzeczywisty czas robót</div><div class="val">${t.actual_days?.median ? (t.actual_days.median/30.44).toLocaleString("pl-PL",{maximumFractionDigits:1}) + " mies." : "b.d."}</div>
      <div class="sub">mediana · połowa trwała od ${days(t.actual_days?.p25)} do ${days(t.actual_days?.p75)}</div></div>
    <div class="metric ${b.verdict === "niski" ? "bad" : b.verdict === "wysoki" ? "warn" : b.verdict ? "ok" : ""}"><div class="lbl">Ocena budżetu</div>
      <div class="val">${b.verdict ? b.verdict : "—"}</div><div class="sub">${b.user_per_km ? `${pln(b.user_per_km)}/km wobec mediany ${pln(b.per_km.median)}/km` : (b.per_km?.n ? `koszt 1 km: mediana ${pln(b.per_km.median)}` : "podaj budżet i długość (sieci, drogi)")}</div></div>
  </div>
  <div class="grid g2">
    <div class="card"><div class="card-head"><h2>Podsumowanie</h2>${KIND_TAG.OBLICZ}</div>
      <ul style="margin:0;padding-left:1.2rem">${r.summary.map(s=>`<li>${esc(s)}</li>`).join("")}</ul>
      ${r.llm_summary?.text ? `<div class="notice" style="margin-top:12px">${KIND_TAG.WNIOSEK} <b>Podsumowanie modelu (${esc(r.llm_summary.model)}):</b><br>${esc(r.llm_summary.text)}</div>` : ""}</div>
    <div class="card"><div class="card-head"><h2>Na co uważać</h2>${cnt(r.flags.length)}</div>
      ${r.flags.length ? r.flags.map(f=>`<div class="flag ${f.level}">${ic(f.level==="red"?"stop":"alert")}<div>${esc(f.text)} ${KIND_TAG[f.kind]||""}<small>podstawa: ${esc(f.basis)}</small></div></div>`).join("") : `<div class="notice ok">Nie wykryto szczególnych sygnałów ostrzegawczych.</div>`}</div>
  </div>
  <div class="card print-all">
    <div class="tabs" id="rtabs">
      <button data-tab="risks" class="active">Problemy w podobnych inwestycjach${cnt(r.risks.length)}</button>
      <button data-tab="recs">Rekomendacje i zapisy umowne</button>
      <button data-tab="cases">Podobne inwestycje${cnt(r.comparables.length)}</button>
      <button data-tab="contacts">Do kogo się zwrócić${cnt(r.contacts.length)}</button>
      <button data-tab="check">Lista kontrolna</button>
      <button data-tab="forecast">Prognozy – szczegóły</button>
      <button data-tab="method">Parametry i metoda</button>
    </div>
    <div data-tabpane="risks">${renderRisks(r)}</div>
    <div data-tabpane="recs" hidden>${renderRecs(r)}</div>
    <div data-tabpane="cases" hidden>${renderCases(r)}</div>
    <div data-tabpane="contacts" hidden>${renderContacts(r)}</div>
    <div data-tabpane="check" hidden>${renderChecklist(r)}</div>
    <div data-tabpane="forecast" hidden>${renderForecast(r)}</div>
    <div data-tabpane="method" hidden>${renderMethod(r, P)}</div>
  </div>`;
  const tabs = $("#rtabs");
  const show = name => { $$("button", tabs).forEach(b=>b.classList.toggle("active", b.dataset.tab===name)); $$("[data-tabpane]").forEach(p=>p.hidden = p.dataset.tabpane !== name); };
  $$("button", tabs).forEach(b => b.onclick = () => show(b.dataset.tab));
  $("#goContacts")?.addEventListener("click", e => { e.preventDefault(); show("contacts"); $("#rtabs").scrollIntoView({behavior:"smooth"}); });
  bindEvidence(app());
  $$("[data-case]").forEach(el => el.onclick = e => { if(e.target.closest("a")) return; showCase(el.dataset.case); });
}

function renderRisks(r){
  if(!r.risks.length) return `<div class="empty">W grupie porównawczej nie znaleziono udokumentowanych problemów. To dobry znak, ale sprawdź listę kontrolną.</div>`;
  return `<p class="muted">Jak często dany problem wystąpił w podobnych inwestycjach (pasek: udział przypadków; jaśniejsze tło: 90% przedział niepewności). Każdy wniosek ma dowód – kliknij cytat, aby zobaczyć ogłoszenie źródłowe.</p>` +
    r.risks.map(x => `<div class="risk">
      <div class="risk-head"><span class="risk-code ${x.catalog_status==="proposed"?"proposed":""}">${esc(x.code)}</span><h3 style="margin:0">${esc(x.name)}</h3>
        ${x.catalog_status==="proposed"?tag("nowa kategoria – do zatwierdzenia","warn"):""}<div class="spacer"></div>
        <div class="risk-share">${bar(x.share, x.ci90)}<small>${x.cases} z ${r.sample.n} inwestycji (${pct(x.share)}) · ${x.events} zdarzeń, ${x.approved_events} zatwierdzonych</small></div></div>
      <p class="muted" style="margin:.4rem 0">${esc(x.effects||"")}</p>
      <div class="facts">${x.median_time_days?`<span>${ic("clock")} typowe wydłużenie: <b>${days(x.median_time_days)}</b></span>`:""}${x.median_cost_pln?`<span>${ic("cost")} typowa wartość zmiany: <b>${pln(x.median_cost_pln)}</b></span>`:""}
        ${x.actions_seen?.length?`<span>${ic("tool")} podjęte działania: ${x.actions_seen.map(([a,k])=>`${esc(a)} (${k})`).join(", ")}</span>`:""}</div>
      ${x.examples.map(e => `<div class="quote">„${esc(e.quote)}”<div class="meta">${KIND_TAG.DANE} ${esc(e.case_org||"")} – <i>${esc((e.case_title||"").slice(0,100))}</i> ·
        <button class="link-btn" data-ev-notice="${esc(e.source_ref)}" data-ev-quote="${esc((e.quote||"").slice(0,80))}">${esc(e.source_ref)}</button> (${esc(e.locator||"")}) · ${evTag(e.status)}
        ${e.action_effectiveness?` · skuteczność działań: ${esc(e.action_effectiveness)}`:""}</div></div>`).join("")}
      <details><summary>Jak ograniczyć to ryzyko</summary><ul>${x.mitigations.map(m=>`<li>${esc(m)}</li>`).join("")}</ul>
        ${x.clauses.length?`<b>Propozycje zapisów umownych:</b><ul>${x.clauses.map(m=>`<li>${esc(m)}</li>`).join("")}</ul>`:""}</details>
    </div>`).join("");
}
function renderRecs(r){
  const R = r.recommendations;
  return `<div class="grid g3">
    <div><h3>Do uzupełnienia przed przetargiem ${KIND_TAG.WNIOSEK}</h3><p class="muted">Informacje z listy kontrolnej MFiPR, których brakuje w opisie.</p>
      <ul class="clean check-list">${R.before_tender.map(x=>`<li>${ic("circle","no")}<div>${esc(x.text)}<br><small>${esc(x.ref||"")}</small></div></li>`).join("") || "<li>Opis zawiera wszystkie kluczowe informacje.</li>"}</ul></div>
    <div><h3>Rekomendowane działania ${KIND_TAG.WNIOSEK}</h3><p class="muted">Wynikają z problemów w podobnych inwestycjach.</p>
      <ul class="clean">${R.actions.map(x=>`<li>${esc(x.text)}<br><small>podstawa: ${esc(x.basis)}${x.evidence.length?` · dowody: ${x.evidence.map(n=>`<button class="link-btn" data-ev-notice="${esc(n)}">${esc(n)}</button>`).join(", ")}`:""}</small></li>`).join("") || "<li>—</li>"}</ul></div>
    <div><h3>Propozycje zapisów umownych ${KIND_TAG.WNIOSEK}</h3><p class="muted">Do rozważenia w PPU / SWZ.</p>
      <ul class="clean">${R.clauses.map(x=>`<li>${esc(x.text)}<br><small>podstawa: ${esc(x.basis)}</small></li>`).join("") || "<li>—</li>"}</ul></div></div>`;
}
function renderCases(r){
  const row = x => `<tr class="click" data-case="${esc(x.tender_id)}"><td><div class="simbar"><div class="bar"><span style="width:${x.similarity_pct||0}%"></span></div><b>${x.similarity_pct||0}%</b></div></td>
    <td><b>${esc(x.title)}</b>${x.exemplary?" "+tag("wzorcowy","ok"):""}<div class="why">${(x.why||[]).map(w=>`<span>${esc(w)}</span>`).join("")}</div></td>
    <td>${esc(x.org)}<br><small>${esc(x.province)} · ${x.year||""}</small></td><td class="num">${pln(x.contract_value)}</td>
    <td class="num">${x.cost_change_pct==null?"b.d.":sgn(x.cost_change_pct)}</td>
    <td>${x.delayed===1?tag(`po terminie${x.delay_days?` (${x.delay_days} dni)`:""}`,"bad"):x.delayed===0?tag("w terminie","ok"):tag("b.d.","grey")}</td>
    <td class="num">${x.n_changes||0}</td><td>${(x.risk_codes||[]).map(c=>`<span class="chip" title="${esc(riskName(c))}">${esc(c)}</span>`).join("")}</td></tr>`;
  return `<p class="muted">${KIND_TAG.DANE} Zakończone inwestycje najbardziej zbliżone do Twojej. Zielone etykiety wyjaśniają, <b>dlaczego</b> uznano je za podobne. Kliknij wiersz, aby zobaczyć pełny przebieg (ogłoszenia, umowy, zmiany).</p>
    <div class="table-wrap"><table><thead><tr><th>Podobieństwo</th><th>Inwestycja i powody podobieństwa</th><th>Zamawiający</th><th>Wartość umowy</th><th>Zmiana kosztu</th><th>Termin</th><th>Zmiany umowy</th><th>Ryzyka</th></tr></thead>
    <tbody>${r.comparables.map(row).join("")}</tbody></table></div>
    ${r.other_similar?.length?`<details><summary>Kolejne podobne (spoza grupy porównawczej)</summary><div class="table-wrap"><table><tbody>${r.other_similar.map(row).join("")}</tbody></table></div></details>`:""}`;
}
function renderContacts(r){
  if(!r.contacts.length) return `<div class="empty">Brak danych kontaktowych.</div>`;
  return `<p class="muted">${KIND_TAG.DANE} Zamawiający, którzy zrealizowali najbardziej podobne inwestycje. Dane kontaktowe pochodzą z ogłoszeń w BZP. Przycisk „Napisz e-mail” przygotuje gotową wiadomość z prośbą o konsultację.</p>
  <div class="grid g2">${r.contacts.map(x=>`<div class="contact"><h4>${esc(x.org)}</h4><small>${esc(x.city||"")}, woj. ${esc(x.province)} · podobieństwo ${x.similarity_pct||"–"}%</small>
    <p style="margin:.5rem 0">${x.success?tag("sukces: w terminie, bez wzrostu kosztów","ok"):tag("doświadczenie z problemami","warn")} ${esc(x.why)}.</p>
    <p style="margin:.3rem 0"><small>Inwestycja: „${esc((x.case_title||"").slice(0,120))}” (${x.year||""})${x.problems?.length?` · problemy: ${x.problems.filter(c=>c!=="BRAK").map(c=>esc(riskName(c))).join(", ")}`:""}</small></p>
    <div class="row">${x.mailto?`<a class="btn primary small" href="${esc(x.mailto)}">${ic("mail")}Napisz e-mail</a>`:""}${x.email?`<span>${esc(x.email)}</span>`:""}${x.phone?`<span class="row" style="gap:4px">${ic("phone")}${esc(x.phone)}</span>`:""}
      ${x.www?`<a href="${esc(/^https?:/.test(x.www)?x.www:"https://"+x.www)}" target="_blank" rel="noopener">strona / BIP</a>`:""}</div></div>`).join("")}</div>`;
}
function renderChecklist(r){
  const miss = r.checklist.filter(x=>!x.present).length;
  return `<p class="muted">Lista kontrolna przygotowania inwestycji na podstawie materiałów eksperckich MFiPR. Zaznaczone – informacja jest w opisie; puste kółko – warto ją uzupełnić (brak w opisie nie oznacza, że nie została przygotowana).</p>
  <div class="notice ${miss>4?"warn":"ok"}">W opisie brakuje ${miss} z ${r.checklist.length} pozycji.</div>
  <ul class="clean check-list">${r.checklist.map(x=>`<li>${x.present?ic("check-circle","yes"):ic("circle","no")}<div>${esc(x.text)} <small class="muted">(${esc(x.ref||"")})</small></div></li>`).join("")}</ul>`;
}
function renderForecast(r){
  const F = r.forecast, t = F.time, c = F.cost, b = F.budget, td = F.tender;
  const st = (s, f = x=>x) => s && s.n ? `mediana <b>${f(s.median)}</b> · P25–P75: ${f(s.p25)} – ${f(s.p75)} · P90: ${f(s.p90)} · N=${s.n}` : "brak danych";
  return `<div class="grid g2">
    <div><h3>Czas ${KIND_TAG.OBLICZ}</h3><dl class="kv">
      <dt>Prawdopodobieństwo opóźnienia</dt><dd>${pct(t.delay_probability)} (${t.delay_k}/${t.delay_n}; 90% CI ${pct(t.delay_ci90[0])}–${pct(t.delay_ci90[1])})</dd>
      <dt>Planowany czas (umowy)</dt><dd>${st(t.planned_days, days)}</dd>
      <dt>Rzeczywisty czas</dt><dd>${st(t.actual_days, days)}</dd>
      <dt>Opóźnienie, gdy wystąpiło</dt><dd>${st(t.delay_days_when_late, days)}</dd>
      <dt>Czas rzeczywisty / planowany</dt><dd>${st(t.ratio_actual_to_planned, x=>x==null?"b.d.":x.toLocaleString("pl-PL",{maximumFractionDigits:2})+"×")}</dd>
      ${t.expected_days_range?`<dt>Twoje ${days(t.user_planned_days)} realnie</dt><dd><b>${days(t.expected_days_range[0])} – ${days(t.expected_days_range[1])}</b></dd>`:""}</dl></div>
    <div><h3>Koszt ${KIND_TAG.OBLICZ}</h3><dl class="kv">
      <dt>Zmiana kosztu umowy</dt><dd>${st(c.change_pct, x=>sgn(x))}</dd>
      <dt>Udział umów ze wzrostem</dt><dd>${pct(c.share_increase)}${c.share_increase_ci90?` (90% CI ${pct(c.share_increase_ci90[0])}–${pct(c.share_increase_ci90[1])})`:""}</dd>
      ${c.expected_final_range?`<dt>Spodziewany koszt końcowy</dt><dd><b>${pln(c.expected_final_range[0])} – ${pln(c.expected_final_range[1])}</b> (mediana – P90, przy budżecie ${pln(c.user_budget_gross)} brutto)</dd>`:""}
      <dt>Koszt 1 km sieci</dt><dd>${st(b.per_km, pln)}</dd>
      ${b.typical_budget_range?`<dt>Typowy budżet dla Twojej długości</dt><dd>${pln(b.typical_budget_range[0])} – ${pln(b.typical_budget_range[1])} (P25–P75)</dd>`:""}
      <dt>Wartości umów</dt><dd>${st(b.values, pln)}</dd></dl></div>
    <div><h3>Przetarg ${KIND_TAG.OBLICZ}</h3><dl class="kv">
      <dt>Cena oferty vs szacunek</dt><dd>${st(td.price_vs_estimate_pct, x=>sgn(x,0))}</dd>
      <dt>Liczba ofert</dt><dd>${st(td.offers, x=>x==null?"b.d.":x.toLocaleString("pl-PL",{maximumFractionDigits:1}))}</dd>
      <dt>Unieważnienia</dt><dd>${td.cancellations?.n?`${pct(td.cancellations.share)} (${td.cancellations.k}/${td.cancellations.n}); z powodu ceny > budżetu: ${td.cancellations.over_budget}`:"b.d."}</dd></dl></div>
    <div class="help"><h4>Jak czytać liczby?</h4>Mediana – połowa przypadków ma wartość niższą, połowa wyższą. P25–P75 – zakres „typowy” (środkowe 50% przypadków).
      P90 – wartość przekraczana tylko w 10% najgorszych przypadków. 90% CI – zakres, w którym z 90% pewnością mieści się prawdziwy udział (im mniejsza próba, tym szerszy).
      Kwoty umów i wypłat są brutto; szacunki zamawiających netto (do porównań ×1,23).</div></div>`;
}
function renderMethod(r, P){
  const rowp = (k, lab, f = x=>x) => { const p = r.query.params[k]; if(!p) return ""; const v = p.value;
    return `<tr><td>${lab}</td><td>${v==null||v===""||(Array.isArray(v)&&!v.length)?"—":esc(f(v))}</td><td>${esc(p.source||"")}</td><td><small>${esc((p.quote||"").slice(0,140))}</small></td></tr>`; };
  return `<h3>Parametry użyte do wyszukiwania</h3><div class="table-wrap"><table><thead><tr><th>Parametr</th><th>Wartość</th><th>Źródło</th><th>Cytat z opisu</th></tr></thead><tbody>
    ${rowp("sector","Sektor",v=>SECT[v]||v)}${rowp("subsector","Podsektor wod-kan",v=>SUB[v]||v)}${rowp("works_type","Rodzaj robót")}${rowp("design_build","Zaprojektuj i wybuduj",v=>v?"tak":"nie")}
    ${rowp("budget","Budżet",pln)}${rowp("budget_vat","VAT")}${rowp("duration_days","Czas (dni)")}${rowp("length_km","Długość (km)")}
    ${rowp("connections","Przyłącza")}${rowp("province","Województwo",v=>PROV[v]||v)}${rowp("features","Elementy",v=>v.map(x=>FEATS[x]||x).join(", "))}</tbody></table></div>
    <h3 style="margin-top:16px">Metoda</h3><ul>${Object.entries(r.method).map(([k,v])=>`<li><b>${esc(k)}</b>: ${esc(v)}</li>`).join("")}
    <li>Wagi podobieństwa ustawia ekspert w panelu „Weryfikacja ekspercka → Ustawienia” (bez zmian w kodzie).</li>
    <li>Zdarzenia oznaczone „niezweryfikowane” pochodzą z automatycznej analizy i czekają na decyzję eksperta; odrzucone nie są brane pod uwagę.</li></ul>
    <details><summary>Pełny tekst opisu</summary><div class="srctext">${esc(r.query.text)}</div></details>`;
}

/* -------------------------------------------- podgląd dowodu (ogłoszenie / dokument) */
function bindEvidence(root){
  $$("[data-ev-notice]", root).forEach(b => b.onclick = e => { e.stopPropagation(); showNotice(b.dataset.evNotice, b.dataset.evQuote); });
}
async function showNotice(num, quote){
  if(!num) return;
  if(/^DOC-|^BIP-|^KIO-/.test(num)) return showDocument(num);
  openModal(`Ogłoszenie ${num}`, `<div class="empty"><span class="spinner dark"></span> Wczytuję…</div>`);
  try{
    const n = await api(`/api/notice?n=${encodeURIComponent(num)}`);
    let text = esc(n.text || "(brak zapisanej treści – zobacz oryginał w BZP)");
    if(quote){ const q = esc(quote.split("—")[0].trim().slice(0,60)); if(q.length > 8){ const i = text.indexOf(q); if(i >= 0) text = text.slice(0,i) + "<mark id='hl'>" + text.slice(i, i+q.length) + "</mark>" + text.slice(i+q.length); } }
    $("#modalBody").innerHTML = `<p>${KIND_TAG.DANE} <b>${esc(n.title)}</b><br><small>${esc(n.notice_type)} · opublikowano ${esc(n.pub_date)} · <a href="${esc(n.url)}" target="_blank" rel="noopener">otwórz oryginał w e-Zamówieniach ${ic("external")}</a></small></p>
      <div class="srctext">${text}</div>`;
    $("#hl")?.scrollIntoView({block:"center"});
  }catch(e){ $("#modalBody").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; }
}
async function showDocument(id){
  openModal(`Dokument`, `<div class="empty"><span class="spinner dark"></span></div>`);
  try{
    const d = await api(`/api/documents/${encodeURIComponent(id)}`);
    $("#modalTitle").textContent = d.title;
    $("#modalBody").innerHTML = `<p>${KIND_TAG.DANE} ${esc(d.source_system)} · ${esc(d.doc_type)} · pobrano ${esc(d.fetched_at)} ${d.url?`· <a href="${esc(d.url)}" target="_blank" rel="noopener">źródło ${ic("external")}</a>`:""}<br><small>SHA-256: ${esc(d.sha256||"")}</small></p>
      ${d.events.length?`<h4>Fragmenty wskazujące na ryzyko (${d.events.length})</h4>${d.events.slice(0,20).map(e=>`<div class="quote"><b>${esc(e.risk_code)}</b> ${esc(riskName(e.risk_code))}: „${esc(e.quote)}”<div class="meta">${esc(e.locator)} · ${evTag(e.status)}</div></div>`).join("")}`:""}
      <h4>Treść</h4>${d.pages.map(([p,t])=>`<div class="muted">— strona ${p} —</div><div class="srctext" style="max-height:none;margin-bottom:8px">${esc(t)}</div>`).join("")}`;
  }catch(e){ $("#modalBody").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; }
}
async function showCase(tid){
  openModal("Przebieg inwestycji", `<div class="empty"><span class="spinner dark"></span></div>`);
  const p = await api(`/api/procedures/${encodeURIComponent(tid)}`);
  $("#modalTitle").textContent = p.title || tid;
  const o = p.organization || {};
  $("#modalBody").innerHTML = `
    <div class="row" style="margin-bottom:8px">${tag(SUB[p.subsector]||SECT[p.sector]||p.sector||"","grey")}${tag(p.status||"","grey")}${p.exemplary?tag("wzorcowy","ok"):""}<div class="spacer"></div>
      <button class="small" id="exBtn">${p.exemplary?"Usuń oznaczenie wzorcowego":`${ic("star")}Oznacz jako wzorcowy`}</button>
      ${tid.startsWith("ocds")?`<a class="btn small" target="_blank" rel="noopener" href="https://ezamowienia.gov.pl/mp-client/search/list/${esc(tid)}">${ic("external")}e-Zamówienia</a>`:""}</div>
    <dl class="kv"><dt>Zamawiający</dt><dd>${esc(p.org_name)} (NIP ${esc(p.nip||"—")}) · ${esc(PROV[p.province]||"")} ${o.email?`· ${esc(o.email)}`:""} ${o.phone?`· tel. ${esc(o.phone)}`:""}</dd>
      <dt>Identyfikator postępowania</dt><dd><code>${esc(tid)}</code></dd>
      <dt>Szacunek (netto)</dt><dd>${pln(p.estimated_value_net)}</dd><dt>Wartość umów (brutto)</dt><dd>${pln(p.contract_value)}</dd>
      <dt>Wypłacono</dt><dd>${pln(p.paid_value)} ${p.cost_change_pct!=null?`(${sgn(p.cost_change_pct)})`:""}</dd>
      <dt>Czas planowany / rzeczywisty</dt><dd>${days(p.planned_days)} / ${days(p.actual_days)} ${p.delayed===1?tag(`po terminie ${p.delay_days?p.delay_days+" dni":""}`,"bad"):p.delayed===0?tag("w terminie","ok"):""}</dd>
      <dt>Długość sieci</dt><dd>${p.length_km?p.length_km.toLocaleString("pl-PL")+" km":"b.d."}</dd>
      <dt>Elementy</dt><dd>${(p.features||[]).map(f=>`<span class="chip">${esc(FEATS[f]||f)}</span>`).join("")||"—"}</dd></dl>
    <h4 style="margin-top:12px">Opis</h4><div class="srctext" style="max-height:160px">${esc(p.description||"—")}</div>
    <h4 style="margin-top:12px">Ogłoszenia ${KIND_TAG.DANE}</h4><div class="timeline">${p.notices.map(n=>`<div class="ti"><b>${esc(n.pub_date)}</b> · ${esc({ContractNotice:"Ogłoszenie o zamówieniu",TenderResultNotice:"Wynik postępowania",AgreementUpdateNotice:"Zmiana umowy",ContractPerformingNotice:"Wykonanie umowy"}[n.notice_type]||n.notice_type)} ·
      <button class="link-btn" data-ev-notice="${esc(n.notice_number)}">${esc(n.notice_number)}</button></div>`).join("")}</div>
    <h4>Umowy</h4><div class="table-wrap"><table><thead><tr><th>Wykonawca</th><th>Data</th><th>Wartość</th><th>Wypłacono</th><th>Wykonana</th><th>W terminie</th><th>Należycie</th><th>Zakończenie</th></tr></thead><tbody>
      ${p.contracts.map(c=>`<tr><td>${esc(c.contractor||"")}</td><td>${esc(c.contract_date||"")}</td><td class="num">${pln(c.contract_value)}</td><td class="num">${pln(c.paid_value)}</td><td>${yesno(c.executed)}</td><td>${yesno(c.on_time)}</td><td>${yesno(c.properly)}</td><td>${esc(c.completion_date||"")}</td></tr>`).join("")||`<tr><td colspan="8" class="muted">brak danych o umowach</td></tr>`}</tbody></table></div>
    <h4 style="margin-top:12px">Zmiany umów (${p.changes.length})</h4>${p.changes.map(c=>`<div class="quote">${esc(c.reason||"")}${c.description?` — ${esc(c.description)}`:""}<div class="meta">${esc(c.legal_basis||"")} ${c.basis_label?`(${esc(c.basis_label)})`:""} · wartość: ${c.value!=null?pln(c.value):"—"} ·
      <button class="link-btn" data-ev-notice="${esc(c.source_notice)}" data-ev-quote="${esc((c.reason||"").slice(0,60))}">${esc(c.source_notice)}</button></div></div>`).join("")||"<p class='muted'>brak zmian w ogłoszeniach</p>"}
    <h4 style="margin-top:12px">Zdarzenia ryzyka (${p.events.length})</h4>${p.events.map(e=>`<div class="quote"><b>${esc(e.risk_code)}</b> ${esc(e.risk_name||"")} · ${esc(e.event_type||"")} ${evTag(e.status)}<br>„${esc((e.quote||"").slice(0,300))}”<div class="meta">${esc(e.locator||"")} · pewność ${e.confidence}</div></div>`).join("")||"<p class='muted'>brak</p>"}`;
  bindEvidence($("#modalBody"));
  $("#exBtn").onclick = async () => { await api(`/api/procedures/${encodeURIComponent(tid)}/exemplary`, {json:{exemplary:!p.exemplary}}); toast("Zapisano decyzję eksperta"); showCase(tid); };
}

/* ================================================================ PROJEKTY */
async function viewProjects(){
  const list = await api("/api/projects");
  app().innerHTML = `
  <div class="page-title"><div><h1>Projekty</h1><p>Projekty z rekordów MFiPR i plików użytkownika, automatycznie powiązane z ogłoszeniami BZP, TED, orzeczeniami KIO i dokumentami z BIP.</p></div>
    <div class="row"><a class="btn" href="/api/export/projects">${ic("download")}Eksport (JSON)</a><a class="btn primary" href="#/import">${ic("upload")}Importuj projekty</a></div></div>
  ${list.length ? `<div class="card"><div class="table-wrap"><table><thead><tr><th>ID</th><th>Przedsięwzięcie</th><th>Odbiorca</th><th>Identyfikator</th><th>Powiązania BZP</th><th>Przebieg</th><th>Zdarzenia ryzyka</th><th>Dane</th></tr></thead><tbody>
  ${list.map(p => { const s = p.summary || {};
    return `<tr class="click" data-pid="${esc(p.project_id)}"><td><code>${esc(p.project_id)}</code></td><td><b>${esc(p.title)}</b><br><small>${esc(SUB[p.subsector]||"")} ${p.programme?"· "+esc(p.programme):""}</small></td>
      <td>${esc(p.org_name)}<br><small>${esc(PROV[p.province]||"")}</small></td><td><small>${esc(p.public_id_type)}<br>${esc(p.public_id)}</small></td>
      <td>${!p.enriched_at?tag("nie wzbogacono","grey"):s.procedures?`${s.procedures} postęp.${s.uncertain_links?" "+tag(s.uncertain_links+" niepewne","warn"):" "+tag("pewne","ok")}`:s.uncertain_links?tag("kandydat – do decyzji eksperta","warn"):tag("brak w BZP","bad")}</td>
      <td>${s.procedures?`${s.delayed?tag("po terminie","bad"):tag("w terminie","ok")}${s.cost_change_pct!=null?" "+sgn(s.cost_change_pct):""}<br><small>${s.changes||0} zmian umów</small>`:"—"}</td>
      <td>${s.events?`${s.events} <small>(${s.pending_events} do weryfikacji)</small>`:"—"}</td>
      <td>${p.validation_issues?tag(p.validation_issues+" uwag","warn"):tag("OK","ok")}</td></tr>`; }).join("")}
  </tbody></table></div></div>` : `<div class="card empty"><h3>Brak projektów</h3><p>Zaimportuj plik z rekordami projektów (np. Risk_Miner_dane_projekty.xlsx).</p><a class="btn primary" href="#/import">Przejdź do importu</a></div>`}`;
  $$("tr[data-pid]").forEach(tr => tr.onclick = () => location.hash = `#/projekt/${encodeURIComponent(tr.dataset.pid)}`);
}

async function viewProject(pid){
  if(!CATALOG) await loadCatalog().catch(()=>{});
  const rec = await api(`/api/projects/${encodeURIComponent(pid)}`);
  const p = rec.project, s = p.summary || {};
  const links = rec.links;
  app().innerHTML = `
  <div class="page-title"><div><a class="crumb" href="#/projekty">Projekty</a><h1>${esc(p.title)}</h1><p><code>${esc(p.project_id)}</code> · ${esc(p.org_name)} · ${esc(PROV[p.province]||"")} · ${esc(p.public_id_type)}: <b>${esc(p.public_id)}</b></p></div>
    <div class="row"><button id="enrich" class="primary">${ic("sync")}Wzbogać ponownie</button>
      <a class="btn" href="/api/projects/${encodeURIComponent(pid)}/export?format=json">${ic("download")}JSON</a><a class="btn" href="/api/projects/${encodeURIComponent(pid)}/export?format=md">${ic("download")}Markdown</a>
      <a class="btn" href="/api/projects/${encodeURIComponent(pid)}/export?format=csv">${ic("download")}CSV</a></div></div>
  <div id="jobbox"></div>
  <div class="grid g4" style="margin-bottom:16px">
    <div class="metric"><div class="lbl">Postępowania BZP</div><div class="val">${s.procedures??"–"}</div><div class="sub">${s.contracts??0} umów · ${s.changes??0} zmian</div></div>
    <div class="metric ${s.delayed?"bad":s.procedures?"ok":""}"><div class="lbl">Termin</div><div class="val">${s.procedures?(s.delayed?"opóźnienie":"w terminie"):"–"}</div><div class="sub">${s.delay_days?s.delay_days+" dni po terminie":""}</div></div>
    <div class="metric"><div class="lbl">Koszt</div><div class="val">${s.cost_change_pct!=null?sgn(s.cost_change_pct):"–"}</div><div class="sub">umowy ${pln(s.contract_value)} · wypłacono ${pln(s.paid_value)}</div></div>
    <div class="metric ${s.pending_events?"warn":""}"><div class="lbl">Zdarzenia ryzyka</div><div class="val">${s.events??0}</div><div class="sub">${s.pending_events??0} czeka na weryfikację</div></div>
  </div>
  <div class="card">
    <div class="tabs" id="ptabs"><button data-tab="links" class="active">Powiązania ze źródłami${cnt(links.length)}</button><button data-tab="course">Przebieg${cnt(rec.procedures.length)}</button>
      <button data-tab="events">Zdarzenia ryzyka${cnt(rec.risk_events.length)}</button><button data-tab="docs">Dokumenty${cnt(rec.documents.length)}</button><button data-tab="data">Braki i walidacja${(s.gaps||[]).length?cnt(s.gaps.length):""}</button><button data-tab="hist">Historia</button></div>
    <div data-tabpane="links">${renderLinks(links)}</div>
    <div data-tabpane="course" hidden>${rec.procedures.map(renderProcedure).join("") || `<div class="empty">Brak powiązanych postępowań.</div>`}</div>
    <div data-tabpane="events" hidden>${renderEventList(rec.risk_events, true)}</div>
    <div data-tabpane="docs" hidden>
      <div class="drop" id="docDrop">${ic("clip","lg")}<h3>Dodaj dokument do projektu</h3><br><small>PDF, DOCX, DOC, XLSX, TXT – np. aneks, protokół, umowa, wystąpienie pokontrolne. System znajdzie fragmenty wskazujące na ryzyko (ze stroną).</small><input type="file" id="docIn" hidden></div>
      <div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>Źródło</th><th>Typ</th><th>Tytuł</th><th>Pobrano</th><th>Znaki</th></tr></thead><tbody>
      ${rec.documents.map(d=>`<tr class="click" data-doc="${esc(d.doc_id)}"><td>${esc(d.source_system)}</td><td>${esc(d.doc_type)}</td><td>${esc(d.title)}</td><td>${esc((d.fetched_at||"").slice(0,16))}</td><td class="num">${(d.chars||0).toLocaleString("pl-PL")}</td></tr>`).join("")||`<tr><td colspan="5" class="muted">brak dokumentów</td></tr>`}</tbody></table></div></div>
    <div data-tabpane="data" hidden>
      ${(s.gaps||[]).length?`<h3>Braki w pakiecie danych</h3><p class="muted">Brak dokumentu w pakiecie nie oznacza, że zdarzenie nie wystąpiło – poniżej: czego brakuje, dlaczego to ważne i skąd to uzupełnić.</p>
        <ul class="clean" style="margin-bottom:14px">${s.gaps.map(g=>`<li>${tag({error:"brak krytyczny",warning:"ważne",info:"uzupełnij"}[g.level]||g.level,{error:"bad",warning:"warn",info:"grey"}[g.level]||"grey")} <b>${esc(g.item)}</b> – ${esc(g.why)}<br><small>Skąd: ${esc(g.where)}</small></li>`).join("")}</ul>`:""}
      <h3>Walidacja identyfikatorów</h3><ul class="clean">${(p.validation||[]).map(v=>`<li>${tag(v.level, {ok:"ok",info:"grey",warning:"warn",error:"bad"}[v.level])} <b>${esc(v.field)}</b>: ${esc(v.message)}</li>`).join("")}</ul>
      <h3 style="margin-top:14px">Rekord źródłowy ${KIND_TAG.DANE}</h3><dl class="kv">${Object.entries(p.raw||{}).map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl></div>
    <div data-tabpane="hist" hidden>${rec.expert_history.length?`<ul class="clean">${rec.expert_history.map(h=>`<li>${esc(h.ts)} · <b>${esc(h.reviewer)}</b> · ${esc(h.entity_type)} ${esc(h.entity_id)} · ${esc(h.action)} ${esc(h.comment||"")}</li>`).join("")}</ul>`:`<div class="empty">Brak decyzji eksperta dla tego projektu.</div>`}</div>
  </div>`;
  const tabs = $("#ptabs");
  $$("button", tabs).forEach(b => b.onclick = () => { $$("button", tabs).forEach(x=>x.classList.toggle("active", x===b)); $$("[data-tabpane]").forEach(x=>x.hidden = x.dataset.tabpane !== b.dataset.tab); });
  bindEvidence(app()); bindDecisions(app(), () => viewProject(pid));
  $$("[data-case]").forEach(el => el.onclick = () => showCase(el.dataset.case));
  $$("tr[data-doc]").forEach(tr => tr.onclick = () => showDocument(tr.dataset.doc));
  $("#enrich").onclick = async () => { const r = await api(`/api/projects/${encodeURIComponent(pid)}/enrich`, {json:{live:true, external:true}}); followJob(r.job, $("#jobbox"), () => viewProject(pid)); };
  const drop = $("#docDrop"), inp = $("#docIn");
  drop.onclick = () => inp.click();
  ["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("on"); }));
  ["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("on"); }));
  const up = async f => { toast(`Wgrywam ${f.name}…`); try{ const r = await api(`/api/upload?purpose=document&project_id=${encodeURIComponent(pid)}`, {method:"POST", body:f, headers:{"X-Filename":encodeURIComponent(f.name)}});
    toast(`Dodano dokument: ${r.pages} str., znaleziono ${r.events} fragmentów wskazujących na ryzyko`, 5000); viewProject(pid); }catch(e){ toast(e.message, 5000); } };
  drop.addEventListener("drop", e => { if(e.dataTransfer.files[0]) up(e.dataTransfer.files[0]); });
  inp.onchange = e => { if(e.target.files[0]) up(e.target.files[0]); };
}
function renderLinks(links){
  if(!links.length) return `<div class="empty">Projekt nie był jeszcze wzbogacany – kliknij „Wzbogać ponownie”.</div>`;
  return `<p class="muted">Każde powiązanie ma podstawę dopasowania i poziom pewności. Niepewne dopasowania zatwierdza lub odrzuca ekspert – decyzja jest zapisywana w historii.</p>
  <div class="table-wrap"><table><thead><tr><th>Źródło</th><th>Powiązany rekord</th><th>Podstawa dopasowania</th><th>Pewność</th><th>Status</th><th></th></tr></thead><tbody>
  ${links.map(l=>`<tr><td>${esc(l.source_system)}<br><small>${esc(l.target_type)}</small></td>
    <td>${l.target_type==="procedure"?`<button class="link-btn" data-case="${esc(l.target_id)}">${esc(l.title||l.target_id)}</button>`:l.target_type==="document"?`<button class="link-btn" data-ev-notice="${esc(l.target_id)}">${esc(l.title||l.target_id)}</button>`:l.url?`<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.title||l.target_id)}</a>`:esc(l.title||l.target_id)}
      <br><small>${(l.evidence||[]).map(esc).join(" · ")}</small></td><td>${esc(l.match_basis)}</td>
    <td><div class="bar" style="width:80px"><span style="width:${l.confidence*100}%"></span></div><small>${(l.confidence*100).toFixed(0)}%</small></td>
    <td>${tag(...(LINK_ST[l.status]||[l.status,"grey"]))}</td>
    <td class="no-print">${l.target_type!=="missing"?`<div class="row" style="flex-wrap:nowrap"><button class="small ok-btn" data-link="${l.link_id}" data-act="approve" title="Potwierdź powiązanie">${ic("check")}</button><button class="small bad-btn" data-link="${l.link_id}" data-act="reject" title="Odrzuć powiązanie">${ic("x")}</button></div>`:""}</td></tr>`).join("")}</tbody></table></div>`;
}
function renderProcedure(pr){
  return `<div class="card flat"><div class="card-head"><h3><button class="link-btn" data-case="${esc(pr.tender_id)}">${esc(pr.title)}</button></h3>${tag(pr.status||"","grey")}</div>
    <dl class="kv"><dt>Postępowanie</dt><dd><code>${esc(pr.tender_id)}</code></dd><dt>Szacunek / umowy / wypłacono</dt><dd>${pln(pr.estimated_value_net)} netto / ${pln(pr.contract_value)} / ${pln(pr.paid_value)} ${pr.cost_change_pct!=null?`(${sgn(pr.cost_change_pct)})`:""}</dd>
      <dt>Czas planowany / rzeczywisty</dt><dd>${days(pr.planned_days)} / ${days(pr.actual_days)} ${pr.delayed===1?tag("po terminie","bad"):pr.delayed===0?tag("w terminie","ok"):""}</dd></dl>
    <div class="timeline" style="margin-top:10px">${pr.notices.map(n=>`<div class="ti">${esc(n.pub_date)} · ${esc(n.notice_type)} · <button class="link-btn" data-ev-notice="${esc(n.notice_number)}">${esc(n.notice_number)}</button> <small>(pobrano ${esc((n.fetched_at||"").slice(0,10))})</small></div>`).join("")}</div>
    ${pr.changes.length?`<details open><summary>Zmiany umowy (${pr.changes.length})</summary>${pr.changes.map(c=>`<div class="quote">${esc(c.reason||"")}${c.description?" — "+esc(c.description):""}<div class="meta">${esc(c.legal_basis||"")} ${c.basis_label?"("+esc(c.basis_label)+")":""} · sekcja ${esc(c.section||"")} · <button class="link-btn" data-ev-notice="${esc(c.source_notice)}" data-ev-quote="${esc((c.reason||"").slice(0,60))}">${esc(c.source_notice)}</button></div></div>`).join("")}</details>`:""}</div>`;
}
function renderEventList(evs, compact){
  if(!evs.length) return `<div class="empty">Brak zdarzeń.</div>`;
  const opts = cur => (CATALOG||[]).filter(c=>c.status!=="rejected").map(c=>`<option value="${c.code}" ${c.code===cur?"selected":""}>${c.code} ${esc(c.name)}</option>`).join("") + `<option value="BRAK" ${cur==="BRAK"?"selected":""}>BRAK – do klasyfikacji</option>`;
  return evs.map(e => `<div class="risk" data-evrow="${e.event_id}">
    <div class="risk-head"><span class="risk-code ${String(e.risk_code).startsWith("N")?"proposed":""}">${esc(e.risk_code)}</span><b>${esc(e.risk_name||riskName(e.risk_code))}</b>
      <span class="muted">· ${esc(e.event_type||"")}</span>${evTag(e.status)}${e.exemplary?tag("wzorcowe","ok"):""}<div class="spacer"></div><small class="muted">pewność ${e.confidence ?? "–"} · ${esc(e.method||"")}</small></div>
    ${e.case_title?`<div style="margin-top:4px"><button class="link-btn" data-case="${esc(e.tender_id)}">${esc(e.case_title)}</button> <small class="muted">${esc(e.case_org||"")} ${e.case_year||""}</small></div>`:""}
    <div class="quote">„${esc(e.quote||"")}”<div class="meta">${KIND_TAG.DANE} źródło: ${e.source_ref?`<button class="link-btn" data-ev-notice="${esc(e.doc_id||e.source_ref)}" data-ev-quote="${esc((e.quote||"").slice(0,60))}">${esc(e.source_ref)}</button>`:""} ${esc(e.locator||"")}
      ${e.matched_terms?.length?` · dopasowane frazy: <i>${e.matched_terms.map(esc).join(", ")}</i>`:""}</div></div>
    <div class="row" style="font-size:.86rem">${e.effect_types?.length?`<span>skutek: <b>${e.effect_types.map(esc).join(", ")}</b></span>`:""}${e.effect_time_days?`<span class="row" style="gap:4px">${ic("clock")}${days(e.effect_time_days)}</span>`:""}${e.effect_cost_pln?`<span class="row" style="gap:4px">${ic("cost")}${pln(e.effect_cost_pln)}</span>`:""}
      ${e.actions?.length?`<span>działania: ${e.actions.map(esc).join(", ")}</span>`:""}${e.action_effectiveness?`<span>skuteczność: ${esc(e.action_effectiveness)}</span>`:""}</div>
    <div class="row no-print" style="margin-top:8px">
      <button class="small ok-btn" data-ev="${e.event_id}" data-act="approve">${ic("check")}Zatwierdź</button>
      <select class="small" style="width:auto;max-width:330px" data-code-for="${e.event_id}">${opts(e.risk_code)}</select>
      <button class="small" data-ev="${e.event_id}" data-act="correct">${ic("pencil")}Zmień kategorię</button>
      <button class="small bad-btn" data-ev="${e.event_id}" data-act="reject">${ic("x")}Odrzuć</button>
      <label class="check" style="font-size:.85rem"><input type="checkbox" data-exemplary="${e.event_id}" ${e.exemplary?"checked":""}> wzorcowe</label>
      <input class="small" style="flex:1;min-width:160px" placeholder="komentarz eksperta (opcjonalnie)" data-comment="${e.event_id}" value="${esc(e.expert_comment||"")}">
    </div></div>`).join("");
}
function bindDecisions(root, after){
  $$("[data-ev]", root).forEach(b => b.onclick = async () => {
    const id = b.dataset.ev, act = b.dataset.act;
    const body = {action:act, comment:$(`[data-comment="${id}"]`, root)?.value || "", exemplary:$(`[data-exemplary="${id}"]`, root)?.checked};
    if(act === "correct") body.risk_code = $(`[data-code-for="${id}"]`, root).value;
    try{ await api(`/api/events/${id}`, {json:body}); toast({approve:"Zatwierdzono zdarzenie",reject:"Odrzucono zdarzenie",correct:"Zmieniono kategorię"}[act]);
      const row = $(`[data-evrow="${id}"]`, root); if(row){ row.style.opacity = .45; row.querySelector(".risk-head").insertAdjacentHTML("beforeend", " " + evTag({approve:"approved",reject:"rejected",correct:"corrected"}[act])); }
      refreshStatus(); }catch(e){ toast(e.message, 5000); }
  });
  $$("[data-link]", root).forEach(b => b.onclick = async () => {
    try{ await api(`/api/links/${b.dataset.link}`, {json:{action:b.dataset.act}}); toast(b.dataset.act==="approve"?"Powiązanie zatwierdzone":"Powiązanie odrzucone"); refreshStatus(); after && after(); }catch(e){ toast(e.message, 5000); }
  });
}

function followJob(jid, box, done, onResult, onError){
  box.innerHTML = `<div class="card"><div class="card-head"><h3><span class="spinner dark"></span> Trwa przetwarzanie…</h3></div><div class="joblog" id="jl">uruchamiam…</div></div>`;
  const tick = async () => {
    try{
      const j = await api(`/api/jobs/${jid}`);
      const jl = $("#jl", box); if(jl){ jl.textContent = j.log.join("\n"); jl.scrollTop = jl.scrollHeight; }
      if(j.status === "running") return setTimeout(tick, 1200);
      box.querySelector("h3").innerHTML = j.status === "done" ? `${ic("check-circle","yes")} Zakończono` : `${ic("alert")} Zakończono z błędem`;
      if(j.status === "done"){ toast("Zakończono"); refreshStatus(); done && setTimeout(done, 700); onResult && onResult(j.result); }
      else { onError && onError(j); }
    }catch(e){ setTimeout(tick, 2500); }
  };
  tick();
}

/* ================================================================ BAZA INWESTYCJI */
async function viewCases(){
  if(!CATALOG) await loadCatalog().catch(()=>{});
  app().innerHTML = `
  <div class="page-title"><div><h1>Baza inwestycji</h1><p>Wszystkie postępowania z BZP i innych źródeł z wynikami realizacji. Filtruj, przeglądaj przebieg i eksportuj do Excela.</p></div>
    <a class="btn" id="expCases" href="#">${ic("download")}Eksport wyników (CSV)</a></div>
  <div class="card"><div class="form-grid">
    <div style="grid-column:span 2"><label class="field">Szukaj w nazwach i opisach</label><input id="f_q" placeholder="np. przepompownia przewiert Kłodzko"></div>
    <div><label class="field">Podsektor</label><select id="f_sub"><option value="">wszystkie</option>${Object.entries(SUB).map(([k,n])=>`<option value="${k}">${n}</option>`).join("")}</select></div>
    <div><label class="field">Województwo</label><select id="f_prov"><option value="">wszystkie</option>${Object.entries(PROV).map(([k,n])=>`<option value="${k}">${n}</option>`).join("")}</select></div>
    <div><label class="field">Sektor</label><select id="f_sector"><option value="">wszystkie</option><option value="wod-kan">wod-kan</option><option value="drogi">drogi</option><option value="kubatura">kubatura</option><option value="energetyka">energetyka</option><option value="kolej">kolej</option><option value="inne">inne</option></select></div>
    <div><label class="field">Status</label><select id="f_status"><option value="">wszystkie</option><option>zakończone</option><option>w realizacji</option><option>unieważnione</option><option>ogłoszone</option></select></div>
    <div><label class="field">Termin</label><select id="f_del"><option value="">dowolny</option><option value="1">po terminie</option><option value="0">w terminie</option></select></div>
    <div><label class="field">Ryzyko</label><select id="f_risk"><option value="">dowolne</option>${(CATALOG||[]).map(c=>`<option value="${c.code}">${c.code} ${esc(c.name)}</option>`).join("")}</select></div>
    <div><label class="field">&nbsp;</label><label class="check"><input type="checkbox" id="f_ex"> tylko wzorcowe</label></div>
  </div><div class="row" style="margin-top:10px"><button class="primary" id="f_go">Szukaj</button><span class="muted" id="f_info"></span></div></div>
  <div class="card"><div id="caseTable"><div class="empty"><span class="spinner dark"></span></div></div></div>`;
  const params = () => { const p = new URLSearchParams(); const set = (k, id) => { const v = $(id).value; if(v) p.set(k, v); };
    set("q","#f_q"); set("subsector","#f_sub"); set("province","#f_prov"); set("sector","#f_sector"); set("status","#f_status"); set("delayed","#f_del"); set("risk","#f_risk");
    if($("#f_ex").checked) p.set("exemplary","1"); return p; };
  const load = async () => {
    const p = params(); p.set("limit","200");
    $("#caseTable").innerHTML = `<div class="empty"><span class="spinner dark"></span></div>`;
    const r = await api(`/api/cases?${p}`);
    $("#f_info").textContent = `${r.total.toLocaleString("pl-PL")} wyników${r.total>200?" (pokazano 200)":""}`;
    $("#caseTable").innerHTML = r.items.length ? `<div class="table-wrap"><table><thead><tr><th>Inwestycja</th><th>Zamawiający</th><th>Rok</th><th>Wartość umów</th><th>Zmiana kosztu</th><th>Termin</th><th>Zmiany</th><th>Ryzyka</th></tr></thead><tbody>
      ${r.items.map(x=>`<tr class="click" data-case="${esc(x.tender_id)}"><td><b>${esc(x.title)}</b>${x.exemplary?" "+tag("wzorcowy","ok"):""}<br><small>${esc(SUB[x.subsector]||SECT[x.sector]||x.sector||"")} · ${esc(x.status||"")}</small></td>
        <td>${esc(x.org_name)}<br><small>${esc(PROV[x.province]||"")}</small></td><td>${x.year||""}</td><td class="num">${pln(x.contract_value)}</td>
        <td class="num">${x.cost_change_pct==null?"–":sgn(x.cost_change_pct)}</td><td>${x.delayed===1?tag("po terminie","bad"):x.delayed===0?tag("w terminie","ok"):"–"}</td>
        <td class="num">${x.n_changes||0}</td><td>${x.risk_codes.map(c=>`<span class="chip" title="${esc(riskName(c))}">${esc(c)}</span>`).join("")}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">Brak wyników.</div>`;
    $$("[data-case]").forEach(el => el.onclick = () => showCase(el.dataset.case));
  };
  $("#f_go").onclick = load; $("#f_q").addEventListener("keydown", e => { if(e.key==="Enter") load(); });
  $("#expCases").onclick = e => { e.preventDefault(); const p = params(); p.set("limit","5000"); download(`/api/export/cases?${p}`); };
  load();
}

/* ================================================================ PANEL EKSPERTA */
async function viewExpert(tab = "events"){
  await loadCatalog();
  app().innerHTML = `
  <div class="page-title"><div><h1>Weryfikacja ekspercka</h1><p>Każde zdarzenie i powiązanie zaproponowane automatycznie może zostać zatwierdzone, poprawione lub odrzucone. Decyzje są zapisywane z historią i wykorzystywane w kolejnych analizach.</p></div>
    <div class="row"><a class="btn" href="/api/export/events">${ic("download")}Eksport zdarzeń (CSV)</a><button id="reanalyze">${ic("sync")}Ponowna analiza (nowe reguły)</button></div></div>
  <div id="jobbox"></div>
  <div class="card"><div class="tabs" id="etabs">
    <button data-tab="events">Zdarzenia ryzyka</button><button data-tab="links">Niepewne powiązania</button><button data-tab="catalog">Katalog ryzyk</button>
    <button data-tab="settings">Ustawienia wyszukiwania</button><button data-tab="log">Historia decyzji</button></div><div id="epane"></div></div>`;
  const tabs = $("#etabs");
  const show = async name => { $$("button", tabs).forEach(b=>b.classList.toggle("active", b.dataset.tab===name)); await EXPERT_TABS[name]($("#epane")); };
  $$("button", tabs).forEach(b => b.onclick = () => show(b.dataset.tab));
  $("#reanalyze").onclick = async () => { const r = await api("/api/reanalyze", {json:{scope:"projects"}}); followJob(r.job, $("#jobbox")); };
  show(tab);
}
const EXPERT_TABS = {
  async events(el){
    el.innerHTML = `<div class="form-grid" style="margin-bottom:12px">
      <div><label class="field">Status</label><select id="e_st"><option value="pending">niezweryfikowane</option><option value="approved">zatwierdzone</option><option value="corrected">poprawione</option><option value="rejected">odrzucone</option><option value="all">wszystkie</option></select></div>
      <div><label class="field">Kategoria</label><select id="e_code"><option value="">wszystkie</option>${CATALOG.map(c=>`<option value="${c.code}">${c.code} ${esc(c.name)} (${c.events})</option>`).join("")}<option value="BRAK">BRAK – niesklasyfikowane</option></select></div>
      <div><label class="field">Zakres</label><select id="e_scope"><option value="projects">projekty z rekordów MFiPR</option><option value="">cała baza</option></select></div>
      <div><label class="field">Szukaj w cytatach</label><input id="e_q" placeholder="np. kolizja, geolog"></div></div>
      <div class="row" style="margin-bottom:10px"><button class="primary" id="e_go">Pokaż</button><span class="muted" id="e_info"></span></div><div id="e_list"></div>`;
    const load = async () => {
      const p = new URLSearchParams({type:"events", status:$("#e_st").value, limit:"40"});
      if($("#e_code").value) p.set("code", $("#e_code").value); if($("#e_scope").value) p.set("scope", $("#e_scope").value); if($("#e_q").value) p.set("q", $("#e_q").value);
      $("#e_list").innerHTML = `<div class="empty"><span class="spinner dark"></span></div>`;
      const r = await api(`/api/review?${p}`);
      $("#e_info").textContent = `${r.total.toLocaleString("pl-PL")} zdarzeń (pokazano ${r.items.length}; najpierw powiązane z projektami i najmniej pewne)`;
      $("#e_list").innerHTML = renderEventList(r.items);
      bindEvidence(el); bindDecisions(el);
      $$("[data-case]", el).forEach(x => x.onclick = () => showCase(x.dataset.case));
    };
    $("#e_go").onclick = load; load();
  },
  async links(el){
    const r = await api("/api/review?type=links&status=uncertain&limit=100");
    el.innerHTML = `<p class="muted">Dopasowania przybliżone (bez jednoznacznego identyfikatora) lub z rozbieżnościami danych. Zatwierdź właściwe, odrzuć błędne.</p>` + (r.items.length ? `<div class="table-wrap"><table><thead><tr><th>Projekt</th><th>Proponowane powiązanie</th><th>Podstawa i uzasadnienie</th><th>Pewność</th><th></th></tr></thead><tbody>
      ${r.items.map(l=>`<tr><td><a href="#/projekt/${encodeURIComponent(l.project_id)}">${esc(l.project_id)}</a><br><small>${esc(l.project_title)}</small><br><small>${esc(l.project_org)} · ${esc(l.public_id||"")}</small></td>
        <td>${l.target_type==="procedure"?`<button class="link-btn" data-case="${esc(l.target_id)}">${esc(l.title||l.target_id)}</button><br><small>${esc(l.target_org||"")} · ${l.target_year||""} · ${pln(l.target_value)}</small>`:esc(l.title||l.target_id)}</td>
        <td>${esc(l.match_basis)}<br><small>${(l.evidence||[]).map(esc).join(" · ")}</small></td><td>${(l.confidence*100).toFixed(0)}%</td>
        <td><div class="row" style="flex-wrap:nowrap">${l.target_type!=="missing"?`<button class="small ok-btn" data-link="${l.link_id}" data-act="approve">${ic("check")}Zatwierdź</button><button class="small bad-btn" data-link="${l.link_id}" data-act="reject">${ic("x")}Odrzuć</button>`:tag("brak dopasowania","bad")}</div></td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">Brak niepewnych powiązań.</div>`);
    bindDecisions(el, () => EXPERT_TABS.links(el)); $$("[data-case]", el).forEach(x => x.onclick = () => showCase(x.dataset.case));
  },
  async catalog(el){
    const cat = await loadCatalog();
    el.innerHTML = `<p class="muted">Katalog jest otwarty: system proponuje nowe kategorie (oznaczone „propozycja”), a ekspert je zatwierdza, odrzuca lub edytuje. Wzorce to wyrażenia rozpoznające ryzyko w tekście (małe litery, bez polskich znaków).
      Po zmianie wzorców użyj „Ponowna analiza”, aby zastosować je do danych – decyzje eksperta zostaną zachowane.</p>
      <div class="row" style="margin-bottom:10px"><button class="primary" id="newCat">${ic("plus")}Dodaj kategorię</button></div>
      <div class="table-wrap"><table><thead><tr><th>Kod</th><th>Nazwa</th><th>Status</th><th>Zdarzeń</th><th>Zatwierdzonych</th><th></th></tr></thead><tbody>
      ${cat.map(c=>`<tr><td><b>${esc(c.code)}</b></td><td>${esc(c.name)}<br><small>${esc(c.effects||"")}</small></td><td>${c.status==="proposed"?tag("propozycja systemu","warn"):c.status==="active"?tag("aktywna","ok"):tag(c.status,"bad")}</td>
        <td class="num">${c.events.toLocaleString("pl-PL")}</td><td class="num">${c.approved}</td>
        <td><div class="row" style="flex-wrap:nowrap">${c.status==="proposed"?`<button class="small ok-btn" data-cat-act="active" data-code="${c.code}">${ic("check")}Zatwierdź</button><button class="small bad-btn" data-cat-act="rejected" data-code="${c.code}" title="Odrzuć">${ic("x")}</button>`:""}<button class="small" data-edit="${c.code}">${ic("pencil")}Edytuj</button></div></td></tr>`).join("")}</tbody></table></div><div id="catEdit"></div>`;
    const edit = code => {
      const c = cat.find(x=>x.code===code) || {code:"", name:"", status:"active", effects:"", patterns:[], mitigations:[], clauses:[], checklist:[]};
      $("#catEdit").innerHTML = `<div class="card" style="margin-top:14px"><h3>${code?"Edycja kategorii "+esc(code):"Nowa kategoria"}</h3><div class="form-grid">
        <div><label class="field">Kod</label><input id="c_code" value="${esc(c.code)}" ${code?"readonly":""} placeholder="np. R16"></div>
        <div style="grid-column:span 2"><label class="field">Nazwa</label><input id="c_name" value="${esc(c.name)}"></div>
        <div><label class="field">Status</label><select id="c_st">${["active","proposed","rejected"].map(s=>`<option ${c.status===s?"selected":""}>${s}</option>`).join("")}</select></div></div>
        <label class="field" style="margin-top:10px">Typowe skutki</label><textarea id="c_eff" style="min-height:60px">${esc(c.effects||"")}</textarea>
        <label class="field" style="margin-top:10px">Wzorce rozpoznawania (po jednym w linii)</label><textarea id="c_pat" style="min-height:110px;font-family:monospace">${esc((c.patterns||[]).join("\n"))}</textarea>
        <label class="field" style="margin-top:10px">Działania zaradcze (po jednym w linii)</label><textarea id="c_mit" style="min-height:80px">${esc((c.mitigations||[]).join("\n"))}</textarea>
        <label class="field" style="margin-top:10px">Propozycje zapisów umownych (po jednym w linii)</label><textarea id="c_cl" style="min-height:80px">${esc((c.clauses||[]).join("\n"))}</textarea>
        <div class="row" style="margin-top:10px"><button class="primary" id="c_save">Zapisz</button><button id="c_cancel">Anuluj</button></div></div>`;
      $("#catEdit").scrollIntoView({behavior:"smooth"});
      $("#c_cancel").onclick = () => $("#catEdit").innerHTML = "";
      $("#c_save").onclick = async () => {
        const lines = id => $(id).value.split("\n").map(s=>s.trim()).filter(Boolean);
        try{ await api("/api/catalog", {json:{code:$("#c_code").value, name:$("#c_name").value, status:$("#c_st").value, effects:$("#c_eff").value,
          patterns:lines("#c_pat"), mitigations:lines("#c_mit"), clauses:lines("#c_cl"), checklist:c.checklist}}); toast("Zapisano katalog"); EXPERT_TABS.catalog(el); }catch(e){ toast(e.message, 5000); }
      };
    };
    $("#newCat").onclick = () => edit("");
    $$("[data-edit]", el).forEach(b => b.onclick = () => edit(b.dataset.edit));
    $$("[data-cat-act]", el).forEach(b => b.onclick = async () => { const c = cat.find(x=>x.code===b.dataset.code);
      await api("/api/catalog", {json:{...c, status:b.dataset.catAct}}); toast(b.dataset.catAct==="active"?"Kategoria zatwierdzona":"Kategoria odrzucona"); EXPERT_TABS.catalog(el); });
  },
  async settings(el){
    const s = await api("/api/settings");
    const w = s["search.weights"] || {};
    const names = {text:"Treść opisu (słowa)", subsector:"Ten sam podsektor", scale:"Skala (koszt, długość)", works_type:"Rodzaj robót", formula:"Formuła „zaprojektuj i wybuduj”",
      features:"Elementy techniczne", region:"To samo województwo", recency:"Aktualność (rok)"};
    el.innerHTML = `<p class="muted">Wagi decydują, które cechy przesądzają o podobieństwie inwestycji. Zmiany działają od razu dla nowych analiz.</p>
      <div class="form-grid">${Object.entries(names).map(([k,n])=>`<div><label class="field">${n}: <b id="wv_${k}">${(w[k]??0).toFixed(2)}</b></label><input type="range" min="0" max="0.6" step="0.01" value="${w[k]??0}" data-w="${k}"></div>`).join("")}
      <div><label class="field">Wielkość grupy porównawczej</label><input type="number" id="s_topk" min="5" max="100" value="${s["search.top_k"]??25}"></div>
      <div><label class="field">Próg ostrzeżenia „mało danych” (N)</label><input type="number" id="s_min" min="1" max="50" value="${s["search.min_sample_warning"]??8}"></div>
      <div><label class="field">Premia dla przypadków wzorcowych</label><input type="number" id="s_boost" min="0" max="0.5" step="0.01" value="${s["report.exemplary_boost"]??0.05}"></div></div>
      <div class="row" style="margin-top:12px"><button class="primary" id="s_save">Zapisz ustawienia</button></div>`;
    $$("[data-w]", el).forEach(r => r.oninput = () => $(`#wv_${r.dataset.w}`).textContent = Number(r.value).toFixed(2));
    $("#s_save").onclick = async () => { const nw = {}; $$("[data-w]", el).forEach(r => nw[r.dataset.w] = Number(r.value));
      await api("/api/settings", {json:{"search.weights":nw, "search.top_k":Number($("#s_topk").value), "search.min_sample_warning":Number($("#s_min").value), "report.exemplary_boost":Number($("#s_boost").value)}}); toast("Zapisano ustawienia"); };
  },
  async log(el){
    const l = await api("/api/expert_log");
    el.innerHTML = l.length ? `<div class="table-wrap"><table><thead><tr><th>Czas</th><th>Ekspert</th><th>Obiekt</th><th>Decyzja</th><th>Komentarz</th><th>Przed → po</th></tr></thead><tbody>
      ${l.map(h=>`<tr><td>${esc(h.ts)}</td><td>${esc(h.reviewer)}</td><td>${esc(h.entity_type)} ${esc(h.entity_id)}</td><td>${esc(h.action)}</td><td>${esc(h.comment||"")}</td><td><small>${esc((h.before||"").slice(0,120))} → ${esc((h.after||"").slice(0,120))}</small></td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">Brak decyzji.</div>`;
  },
};

/* ================================================================ PRZETWÓRZ PLIKI (pakiet: rekord projektu + opis inwestycji) */
const fileB64 = f => new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res({name:f.name, b64:String(r.result).split(",")[1]}); r.onerror = rej; r.readAsDataURL(f); });
const kb = n => n >= 1048576 ? (n/1048576).toLocaleString("pl-PL",{maximumFractionDigits:1}) + " MB" : Math.max(1, Math.round(n/1024)) + " kB";
async function viewProcess(){
  const L = S.llm || {};
  app().innerHTML = `
  <div class="page-title"><div><h1>Przetwórz pliki</h1>
    <p>Dodaj rekord projektu i/lub opis nowej inwestycji. System wykona cały proces bez dodatkowych kroków i przygotuje pliki wyników:
    <b>eksport rekordu projektu</b> (powiązania z BZP, TED, KIO, BIP, NIK, przebieg, zdarzenia ryzyka z dowodami) oraz <b>analizę nowej inwestycji</b>
    (podobne inwestycje, prognozy, ryzyka, rekomendacje, kontakty).</p></div></div>
  <div class="grid g2">
    <div class="card"><div class="card-head"><h2>1. Rekord projektu</h2><span class="muted">XLSX, XLS, CSV</span></div>
      <div class="drop" id="dropP">${ic("projects","lg")}<h3 id="nameP">Przeciągnij plik albo kliknij</h3><p class="muted" style="margin:0">Układ jak w rekordach MFiPR – kolumny są rozpoznawane automatycznie.</p></div>
      <input type="file" id="inP" hidden accept=".xlsx,.xls,.csv,.ods"></div>
    <div class="card"><div class="card-head"><h2>2. Opis nowej inwestycji</h2><span class="muted">DOCX, PDF, TXT</span></div>
      <div class="drop" id="dropI">${ic("file","lg")}<h3 id="nameI">Przeciągnij plik albo kliknij</h3><p class="muted" style="margin:0">Kilka stron tekstu – opis, notatka, fragment wniosku.</p></div>
      <input type="file" id="inI" hidden accept=".docx,.doc,.pdf,.txt,.odt,.rtf,.md,.html">
      <details style="margin-top:8px"><summary>albo wklej tekst opisu</summary><textarea id="txtI" style="min-height:140px" placeholder="Treść opisu nowej inwestycji…"></textarea></details></div>
  </div>
  <div class="card"><div class="row">
    <label class="check"><input type="checkbox" id="optExt" checked> przeszukaj źródła zewnętrzne (TED, KIO, BIP, NIK) – dokładniej, ok. 1–2 min</label>
    <div class="spacer"></div><span class="muted">${L.enabled ? `Opis odczyta model ${esc(L.model)} ${L.online ? "" : "(niedostępny – reguły)"}` : "Opis odczytają reguły eksperckie"}</span>
    <button class="primary btn-lg" id="runP">${ic("sync")}Przetwórz i przygotuj pliki</button></div></div>
  <div id="jobbox"></div><div id="procResult"></div>
  <div class="card"><div class="card-head"><h2>Poprzednie przebiegi</h2></div><div id="prevRuns"><div class="empty"><span class="spinner dark"></span></div></div></div>`;
  const pick = {P:null, I:null};
  const bindDrop = (k, accept) => {
    const drop = $("#drop"+k), inp = $("#in"+k);
    const set = f => { pick[k] = f; $("#name"+k).innerHTML = `${ic("check-circle","yes")} ${esc(f.name)} <span class="muted" style="font-weight:400">(${kb(f.size)})</span>`; drop.classList.add("on"); };
    drop.onclick = () => inp.click();
    ["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("on"); }));
    ["dragleave"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); if(!pick[k]) drop.classList.remove("on"); }));
    drop.addEventListener("drop", e => { e.preventDefault(); if(e.dataTransfer.files[0]) set(e.dataTransfer.files[0]); });
    inp.onchange = e => { if(e.target.files[0]) set(e.target.files[0]); };
  };
  bindDrop("P"); bindDrop("I");
  const loadPrev = async () => {
    const runs = await api("/api/results");
    $("#prevRuns").innerHTML = runs.length ? `<div class="table-wrap"><table><thead><tr><th>Przebieg</th><th>Pliki</th></tr></thead><tbody>${runs.map(r=>`<tr><td><code>${esc(r.run)}</code></td>
      <td>${r.files.map(f=>`<a href="/api/results/${esc(r.run)}/${esc(f.name)}">${esc(f.name)}</a> <small>${kb(f.size)}</small>`).join("<br>")}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">Brak poprzednich przebiegów.</div>`;
  };
  loadPrev();
  $("#runP").onclick = async () => {
    const text = $("#txtI").value.trim();
    if(!pick.P && !pick.I && text.length < 15){ toast("Dodaj rekord projektu i/lub opis inwestycji."); return; }
    const btn = $("#runP"); btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Przetwarzanie…`;
    try{
      const body = {external: $("#optExt").checked, text};
      if(pick.P) body.project = await fileB64(pick.P);
      if(pick.I) body.investment = await fileB64(pick.I);
      const r = await api("/api/process", {json: body});
      followJob(r.job, $("#jobbox"), null, res => { showProcessResult(res); loadPrev(); btn.disabled = false; btn.innerHTML = `${ic("sync")}Przetwórz ponownie`; },
        () => { btn.disabled = false; btn.innerHTML = `${ic("sync")}Spróbuj ponownie`; });
    }catch(e){ toast(e.message, 6000); btn.disabled = false; btn.innerHTML = `${ic("sync")}Przetwórz i przygotuj pliki`; }
  };
}
function showProcessResult(res){
  const f = res.files || [], main = f.filter(x=>x.main), rest = f.filter(x=>!x.main);
  const link = x => `/api/results/${esc(res.run)}/${esc(x.name)}`;
  $("#procResult").innerHTML = `<div class="card"><div class="card-head"><h2>${ic("check-circle","yes")} Pliki wyników gotowe</h2>
      <a class="btn primary" href="${link({name:res.zip})}">${ic("download")}Pobierz wszystko (ZIP)</a></div>
    <ul class="clean">${main.map(x=>`<li class="row"><a class="btn" href="${link(x)}">${ic("download")}${esc(x.name)}</a><span>${esc(x.label)}</span><span class="muted">${kb(x.size)}</span></li>`).join("")}</ul>
    <details><summary>Te same wyniki w innych formatach (${rest.length})</summary><ul class="clean">${rest.map(x=>`<li class="row"><a href="${link(x)}">${esc(x.name)}</a><span class="muted">${esc(x.label)} · ${kb(x.size)}</span></li>`).join("")}</ul></details>
    <div class="row" style="margin-top:12px">${(res.projects||[]).map(p=>`<a class="btn" href="#/projekt/${encodeURIComponent(p.project_id)}">${ic("projects")}Otwórz projekt ${esc(p.project_id)}</a>`).join("")}
      ${res.analysis?`<a class="btn" href="#/raport/${esc(res.analysis.analysis_id)}">${ic("search")}Otwórz raport analizy</a>`:""}</div>
    <p class="muted" style="margin:12px 0 0">Folder na dysku: <code>${esc(res.dir)}</code></p></div>`;
}

/* ================================================================ IMPORT */
async function viewImport(){
  app().innerHTML = `
  <div class="page-title"><div><h1>Import danych</h1><p>Przeciągnij plik – system sam rozpozna, co to jest. Tabele projektów zostaną zaimportowane i automatycznie powiązane z danymi publicznymi; dokumenty – przeanalizowane pod kątem ryzyk.</p></div></div>
  <div class="grid" style="grid-template-columns:minmax(0,2fr) minmax(0,1fr)">
    <div>
      <div class="card"><div class="drop" id="drop">${ic("upload","lg")}<h3>Przeciągnij plik tutaj lub kliknij, aby wybrać</h3>
        <p class="muted">Tabela projektów: <b>XLSX, XLS, CSV</b> (np. rekordy MFiPR) · Dokumenty: <b>PDF, DOCX, DOC, ODT, TXT, HTML</b></p><input type="file" id="fin" hidden multiple></div>
        <div class="row" style="margin-top:12px"><label class="check"><input type="checkbox" id="o_enrich" checked> automatycznie wzbogać projekty (BZP, TED, KIO, BIP, NIK)</label>
          <label class="check"><input type="checkbox" id="o_ext" checked> przeszukaj źródła zewnętrzne (wolniej)</label></div></div>
      <div id="jobbox"></div><div id="importResult"></div>
      <div class="card"><div class="card-head"><h2>Dodaj dane z BIP</h2><span class="muted">strona BIP, protokół, aneks, zarządzenie</span></div>
        <p class="muted" style="margin-top:0">Wklej adres strony z Biuletynu Informacji Publicznej (albo bezpośredni link do PDF/DOCX). System pobierze stronę
          i jej załączniki, zapisze je z adresem źródła i znajdzie fragmenty wskazujące na ryzyko – z numerem strony, do weryfikacji eksperta.</p>
        <label class="field" for="bipUrls">Adresy stron BIP (jeden w wierszu)</label>
        <textarea id="bipUrls" style="min-height:72px" placeholder="https://bip.gmina.pl/…/informacja-z-otwarcia-ofert&#10;https://bip.gmina.pl/…/aneks-nr-2.pdf"></textarea>
        <details style="margin:8px 0"><summary>albo wklej treść skopiowaną ze strony BIP</summary>
          <input id="bipTitle" placeholder="Tytuł (np. Zarządzenie nr 12/2025 Wójta Gminy…)" style="margin-bottom:8px"><textarea id="bipText" style="min-height:120px" placeholder="Treść strony lub dokumentu…"></textarea></details>
        <div class="form-grid" style="margin-top:8px">
          <div><label class="field" for="bipProj">Przypnij do projektu (opcjonalnie)</label><select id="bipProj"><option value="">— bez projektu —</option></select></div>
          <div><label class="field">&nbsp;</label><label class="check"><input type="checkbox" id="bipAtt" checked> pobierz też załączniki ze strony</label></div></div>
        <div class="row" style="margin-top:12px"><div class="spacer"></div><button class="primary" id="bipGo">${ic("download")}Pobierz i przeanalizuj</button></div>
        <div id="bipBox"></div><div id="bipResult"></div></div>
    </div>
    <div>
      <div class="help"><h4>Co dzieje się po imporcie tabeli?</h4><ol style="margin:.3rem 0 0 1.1rem;padding:0;font-size:.9rem">
        <li>Rozpoznanie kolumn (także w innych układach niż wzór MFiPR).</li><li>Walidacja: suma kontrolna NIP, format numeru BZP, zgodność numeru KPO z województwem.</li>
        <li>Łączenie z BZP po numerze ogłoszenia (pewne) lub po NIP + nazwie (z poziomem pewności).</li>
        <li>Pobranie wszystkich ogłoszeń postępowania: wynik, umowy, zmiany, wykonanie.</li><li>Wyszukanie w TED, KIO, BIP zamawiającego i NIK.</li>
        <li>Rozpoznanie zdarzeń ryzyka z dowodami – do weryfikacji eksperta.</li></ol></div>
      <div class="card" style="margin-top:12px"><h3>Monitorowanie inwestycji</h3><p class="muted">Sprawdza, czy dla trwających postępowań pojawiły się nowe ogłoszenia (zmiana lub wykonanie umowy) i aktualizuje analizę.</p>
        <button id="mon">${ic("sync")}Sprawdź nowe ogłoszenia</button><div id="monList" style="margin-top:10px"></div></div>
      <div class="card"><h3>Źródła zewnętrzne</h3><p class="muted">Wczytaj zasoby pobrane przez harvestery (orzeczenia KIO, ogłoszenia TED, BZP z innych sektorów, KPOŚK).</p>
        <button id="extras">${ic("download")}Wczytaj źródła z data_lake</button></div>
    </div>
  </div>
  <div class="card"><h3>Rejestr operacji i źródeł</h3><div id="srcTable"></div></div>`;
  const drop = $("#drop"), fin = $("#fin");
  drop.onclick = () => fin.click();
  ["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("on"); }));
  ["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("on"); }));
  const handle = async files => { for(const f of files) await uploadOne(f); };
  drop.addEventListener("drop", e => handle(e.dataTransfer.files));
  fin.onchange = e => handle(e.target.files);
  $("#extras").onclick = async () => { const r = await api("/api/extras", {json:{}}); followJob(r.job, $("#jobbox")); };
  $("#mon").onclick = async () => { const r = await api("/api/monitor", {json:{}}); followJob(r.job, $("#jobbox"), loadMon); };
  const loadMon = async () => { const ev = await api("/api/monitor"); $("#monList").innerHTML = ev.length ? `<ul class="clean">${ev.map(e=>`<li>${esc(e.ts.slice(0,16))} · <b>${esc(e.kind)}</b> ${esc(e.notice_number)} ${e.project_id?`· <a href="#/projekt/${esc(e.project_id)}">${esc(e.project_id)}</a>`:""}<br><small>${esc(e.summary||"")}</small></li>`).join("")}</ul>` : `<small class="muted">Brak nowych zdarzeń monitoringu.</small>`; };
  loadMon();
  api("/api/projects").then(ps => { $("#bipProj").insertAdjacentHTML("beforeend", ps.map(p=>`<option value="${esc(p.project_id)}">${esc(p.project_id)} – ${esc((p.title||"").slice(0,70))}</option>`).join("")); }).catch(()=>{});
  $("#bipGo").onclick = async () => {
    const body = {urls:$("#bipUrls").value, text:$("#bipText").value, title:$("#bipTitle").value, project_id:$("#bipProj").value, attachments:$("#bipAtt").checked};
    try{
      const r = await api("/api/bip", {json: body});
      followJob(r.job, $("#bipBox"), null, res => {
        $("#bipResult").innerHTML = `<div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>Dokument</th><th>Rodzaj</th><th>Strony</th><th>Fragmenty ryzyka</th></tr></thead><tbody>
          ${res.documents.map(d=>`<tr class="click" data-doc="${esc(d.doc_id)}"><td><b>${esc(d.title)}</b><br><small>${esc(d.url||"")}</small></td><td>${esc(d.kind)}</td><td class="num">${d.pages}</td>
          <td class="num">${d.events ? tag(d.events + " do weryfikacji","warn") : tag("brak","grey")}</td></tr>`).join("")}</tbody></table></div>
          ${res.errors?.length ? `<div class="flag amber" style="margin-top:8px">${ic("alert")}<div>${res.errors.map(esc).join("<br>")}</div></div>` : ""}`;
        $$("tr[data-doc]", $("#bipResult")).forEach(tr => tr.onclick = () => showDocument(tr.dataset.doc));
      });
    }catch(e){ toast(e.message, 6000); }
  };
  const src = await api("/api/sources");
  $("#srcTable").innerHTML = `<div class="table-wrap" style="max-height:340px"><table><thead><tr><th>Czas</th><th>Źródło</th><th>Operacja</th><th>Rekordów</th><th>Adres</th></tr></thead><tbody>
    ${src.map(s=>`<tr><td>${esc((s.finished_at||"").slice(0,16))}</td><td>${esc(s.source_system)}</td><td>${esc(s.operation)}</td><td class="num">${s.records??""}</td><td><small>${esc((s.url||"").slice(0,80))}</small></td></tr>`).join("")}</tbody></table></div>`;
}
async function uploadOne(f){
  const enrich = $("#o_enrich").checked ? "1" : "0", ext = $("#o_ext").checked ? "1" : "0";
  toast(`Wgrywam ${f.name}…`);
  try{
    const r = await api(`/api/upload?enrich=${enrich}&external=${ext}`, {method:"POST", body:f, headers:{"X-Filename":encodeURIComponent(f.name)}});
    if(r.kind === "table"){
      $("#importResult").innerHTML = `<div class="notice ok"><b>Zaimportowano ${r.imported.length} projektów</b> z pliku ${esc(f.name)}. Rozpoznane kolumny: ${Object.entries(r.columns).map(([k,v])=>`<code>${esc(v)}</code>`).join(", ")}.
        <br><a href="#/projekty">Zobacz projekty</a></div>`;
      if(r.job) followJob(r.job, $("#jobbox"));
    } else {
      $("#importResult").innerHTML = `<div class="notice ok"><b>Dodano dokument ${esc(f.name)}</b>: ${r.pages} str., ${r.chars.toLocaleString("pl-PL")} znaków; znaleziono ${r.events} fragmentów wskazujących na ryzyko (czekają na weryfikację eksperta).
        <br><button class="link-btn" data-ev-notice="${esc(r.doc_id)}">Podgląd dokumentu</button></div>`;
      bindEvidence($("#importResult"));
    }
    refreshStatus();
  }catch(e){ $("#importResult").innerHTML = `<div class="notice bad"><b>Nie udało się zaimportować ${esc(f.name)}:</b> ${esc(e.message)}</div>`; }
}

/* ================================================================ O SYSTEMIE */
async function viewAbout(){
  const s = await api("/api/status");
  const d = s.db;
  app().innerHTML = `
  <div class="page-title"><div><h1>O systemie</h1><p>Risk Miner buduje cyfrową pamięć instytucjonalną z rozproszonych, publicznych danych o inwestycjach i udostępnia ją przygotowującym nowe projekty.</p></div></div>
  <div class="grid g4" style="margin-bottom:16px">
    <div class="metric"><div class="lbl">Ogłoszenia</div><div class="val">${d.notices.toLocaleString("pl-PL")}</div><div class="sub">BZP i TED</div></div>
    <div class="metric"><div class="lbl">Postępowania</div><div class="val">${d.procedures.toLocaleString("pl-PL")}</div><div class="sub">${d.completed.toLocaleString("pl-PL")} z wynikiem realizacji</div></div>
    <div class="metric"><div class="lbl">Zmiany umów</div><div class="val">${d.changes.toLocaleString("pl-PL")}</div><div class="sub">${d.contracts.toLocaleString("pl-PL")} umów</div></div>
    <div class="metric"><div class="lbl">Zdarzenia ryzyka</div><div class="val">${d.events.toLocaleString("pl-PL")}</div><div class="sub">${d.events_reviewed.toLocaleString("pl-PL")} zweryfikowanych przez eksperta</div></div></div>
  <div class="grid g2">
    <div class="card"><h2>Jak to działa?</h2><ol>
      <li><b>Zasilanie bazy</b> – ogłoszenia BZP/e-Zamówień (o zamówieniu, wyniku, zmianie i wykonaniu umowy), TED, orzeczenia KIO, dokumenty z BIP i NIK, pliki użytkownika.</li>
      <li><b>Łączenie</b> – po numerze ogłoszenia, OCID, NIP; gdy brak klucza – po nazwie, wartości i czasie, z poziomem pewności.</li>
      <li><b>Zdarzenia ryzyka</b> – reguły katalogu eksperckiego R01–R15 rozpoznają przyczyny zmian umów, opóźnień i unieważnień; każde zdarzenie ma cytat i źródło.</li>
      <li><b>Weryfikacja ekspercka</b> – ekspert zatwierdza, poprawia lub odrzuca; decyzje wpływają na kolejne analizy.</li>
      <li><b>Analiza nowej inwestycji</b> – opis w języku naturalnym → podobne zakończone inwestycje → prognozy, flagi, rekomendacje i kontakty.</li></ol></div>
    <div class="card"><h2>Model językowy i dane</h2><dl class="kv">
      <dt>Tryb</dt><dd>${s.llm.enabled ? `${esc(s.llm.provider)} · ${esc(s.llm.model)} ${s.llm.online?tag("dostępny","ok"):tag("niedostępny","bad")}` : "reguły eksperckie + statystyka (bez modelu – działa offline)"}</dd>
      <dt>Przetwarzanie danych</dt><dd>${esc(s.llm.data_location || "lokalnie")}</dd>
      <dt>Zmiana modelu</dt><dd>plik <code>config.toml</code> (sekcja [llm]) – bez zmian w kodzie; obsługa Ollama (np. Bielik) i API zgodnych z OpenAI</dd>
      <dt>Ostatnia aktualizacja</dt><dd>${esc(d.last_update||"")}</dd>
      <dt>Sektory w bazie</dt><dd><ul class="clean">${d.sectors.map(x=>`<li>${esc(SECT[x.sector]||x.sector)}: <b>${x.n.toLocaleString("pl-PL")}</b> postępowań, ${(x.completed||0).toLocaleString("pl-PL")} z wynikiem realizacji</li>`).join("")}</ul></dd></dl>
      <p class="muted" style="margin-top:10px">Liczby i wnioski są zawsze oznaczone: ${KIND_TAG.DANE} ${KIND_TAG.OBLICZ} ${KIND_TAG.WNIOSEK}</p></div>
  </div>`;
}

/* ---------------------------------------------------------------- start */
loadCatalog().catch(()=>{});
refreshStatus().finally(route);
setInterval(refreshStatus, 60000);
})();
