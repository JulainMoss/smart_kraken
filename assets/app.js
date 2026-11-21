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
    {key:"planowany_czas", label:"Planowany czas", c:"#ff8fb1", wide:false},
  ];

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
      const s = await (await fetch("api/status")).json(); S.status = s;
      el.className = "status " + (s.model_ready ? "ok" : s.online ? "warn" : "bad");
      $("statusText").textContent = s.model_ready ? `${s.model} · gotowy`
        : s.online ? `Brak modelu ${s.model}` : `Serwer modelu nie działa (${s.backend})`;
      el.title = s.model_ready ? `Model ${s.model} na ${s.url} (${s.backend})`
        : s.online ? `Pobierz model poleceniem: ollama pull ${s.model}\nDostępne: ${s.models.join(", ") || "brak"}`
        : `Nie można połączyć z ${s.url}. Uruchom Ollamę (ollama serve) albo inny serwer modelu.`;
      if(!s.model_ready && !S.result){
        banner(s.online
          ? `Lokalny serwer modelu działa, ale nie ma na nim modelu <b>${esc(s.model)}</b>. Pobierz go:<br><code>ollama pull ${esc(s.model)}</code><br>
             <small>Albo uruchom serwer z innym modelem: <code>python serwer.py --model NAZWA</code>. Dostępne: ${esc(s.models.join(", ") || "brak")}</small>`
          : `Nie ma połączenia z lokalnym modelem (${esc(s.url)}). Zainstaluj <a href="https://ollama.com/download" target="_blank" rel="noopener">Ollamę</a>, a potem:<br>
             <code>ollama pull ${esc(s.model)}</code>`, "warn");
      } else if(s.model_ready && !S.result) banner("");
      return s;
    }catch{
      el.className = "status bad"; $("statusText").textContent = "Brak połączenia z serwer.py";
      banner(`Strona nie łączy się z serwerem aplikacji. Uruchom <code>python serwer.py</code> i otwórz <code>http://127.0.0.1:8000</code>.`, "err");
      return null;
    }
  }

  // ---- analiza ---------------------------------------------------------------
  async function analyze(){
    const text = $("text").value.trim();
    if(text.length < 20){ toast("Wpisz dłuższy opis inwestycji."); $("text").focus(); return; }
    if(S.ctrl) return;
    S.ctrl = new AbortController();
    const t0 = Date.now();
    const btn = $("btnAnalyze"); btn.disabled = true; $("btnCancel").hidden = false;
    const tick = () => { btn.innerHTML = `<span class="spinner"></span> Analizuję… ${Math.round((Date.now()-t0)/1000)} s`; };
    tick(); S.timer = setInterval(tick, 1000);
    banner(""); $("meta").textContent = "";
    $("result").innerHTML = `<div class="fields">${FIELDS.map(()=>`<div class="skel"></div>`).join("")}</div><div class="skel" style="margin-top:12px;height:90px"></div>
      <p class="muted" style="font-size:12.5px;margin-top:10px">Model czyta tekst – na komputerze bez karty graficznej może to potrwać od kilkunastu sekund do kilku minut.</p>`;
    try{
      const r = await fetch("api/extract", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({text}), signal:S.ctrl.signal});
      const j = await r.json().catch(() => ({title:`HTTP ${r.status}`}));
      if(!r.ok) throw Object.assign(new Error(j.title || `HTTP ${r.status}`), {detail:j.detail, debug:j.debug_plik, raw:j.surowa_tresc});
      S.text = text; S.result = j.wynik; S.meta = j;
      render(); showMarked();
      if(j.ostrzezenia?.length) banner(j.ostrzezenia.map(esc).join("<br>"), "warn");
    }catch(e){
      if(e.name === "AbortError"){ banner("Analizę przerwano.", "warn"); }
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
    const val = key==="planowana_kwota" ? f.kwota!=null : key==="planowany_czas" ? (f.liczba!=null || f.termin_do) : !!f.wartosc;
    if(!val) return `<span class="src none">nie znaleziono</span>`;
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
        return `<div class="row"><input data-k="${key}" data-p="liczba" data-num="1" value="${f.liczba??""}" placeholder="liczba" style="flex:1">
          <select data-k="${key}" data-p="jednostka" style="flex:1.3">${opts(JEDN, f.jednostka, "jednostka")}</select>
          <input type="date" data-k="${key}" data-p="termin_do" value="${esc(f.termin_do)}" title="termin zakończenia" style="flex:1.5"></div>
          <div class="pretty" data-pretty="${key}"></div>`;
    }
  }
  function pretty(key){
    const f = S.result[key];
    if(key==="planowana_kwota"){
      return f.kwota!=null ? `<b>${esc(fmtMoney(f.kwota, f.waluta))}</b> ${f.vat!=="nieokreślone"?esc(f.vat):"<span class='muted'>(VAT nieokreślony)</span>"}` : "";
    }
    if(key==="planowany_czas"){
      const n = f.liczba, u = f.jednostka; const parts = [];
      if(n!=null && u){
        const days = Math.round(n * {dni:1,tygodnie:7,"miesiące":30.4375,lata:365.25}[u]); f.dni = days;
        parts.push(`<b>${esc(String(n).replace(".",","))} ${esc(plUnit(n, u))}</b> ≈ ${days.toLocaleString("pl-PL")} ${plDays(days)}` + (u!=="miesiące" && days>=60 ? ` (${(days/30.4375).toFixed(1).replace(".",",")} ${plMonths(days/30.4375)})` : ""));
      } else if(f.termin_do){
        const days = Math.round((new Date(f.termin_do) - new Date(new Date().toISOString().slice(0,10)))/864e5); f.dni = days;
        parts.push(`${days>=0?"zostało":"minęło"} ${Math.abs(days).toLocaleString("pl-PL")} ${plDays(Math.abs(days))}`);
      } else f.dni = null;
      if(f.termin_do) parts.push(`termin: <b>${f.termin_do.split("-").reverse().join(".")}</b>`);
      return parts.join(" · ");
    }
    return "";
  }

  function render(){
    const r = S.result; if(!r) return;
    $("btnCopy").disabled = $("btnDownload").disabled = false;
    const cards = FIELDS.map(F => {
      const f = r[F.key] || {};
      const hasVal = F.key==="planowana_kwota" ? f.kwota!=null : F.key==="planowany_czas" ? (f.liczba!=null||f.termin_do) : !!f.wartosc;
      return `<div class="field ${hasVal?"":"empty-val"}" style="--c:${F.c}">
        <div class="lab">${F.label}${srcBadge(f, F.key)}</div>
        ${fieldBody(F.key, f)}
        ${f.cytat ? `<div class="quote ${f.span?"":"nolink"}" data-span="${f.span?f.span.join(","):""}" title="${f.span?"Pokaż w tekście":"Cytatu nie znaleziono dosłownie w tekście"}">${esc(f.cytat)}</div>` : ""}
      </div>`;
    }).join("");
    const kws = r.slowa_kluczowe || [];
    const usedCats = [...new Set(kws.map(k=>k.kategoria))];
    $("result").innerHTML = `<div class="fields">${cards}</div>
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
      (m.debug_plik ? `<br>Zapis debug: <code>${esc(m.debug_plik)}</code>` : "") + rawBox(m.surowa_tresc);
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
    const el = e.target; const k = el.dataset.k, p = el.dataset.p; if(!k || !S.result) return;
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
  });
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
      planowany_czas: (r.planowany_czas.liczba!=null || r.planowany_czas.termin_do) ? {liczba:r.planowany_czas.liczba, jednostka:r.planowany_czas.jednostka||null,
        termin_do:r.planowany_czas.termin_do||null, dni:r.planowany_czas.dni ?? null} : null,
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
  async function readFile(file){
    if(!file) return;
    if(file.size > 3e6){ toast("Plik jest za duży (maks. 3 MB tekstu)."); return; }
    const buf = await file.arrayBuffer();
    let txt = new TextDecoder("utf-8").decode(buf);
    if(txt.includes("�")){ try{ txt = new TextDecoder("windows-1250").decode(buf); }catch{} }   // polskie pliki z Windows
    if(/\.html?$/i.test(file.name)){ const d = new DOMParser().parseFromString(txt, "text/html"); d.querySelectorAll("script,style").forEach(n=>n.remove()); txt = d.body.innerText || d.body.textContent; }
    $("text").value = txt.trim(); updateChars(); showEditor(); saveDraft(); toast(`Wczytano: ${file.name}`);
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
  $("btnCancel").addEventListener("click", () => S.ctrl?.abort());
  function updateChars(){ const n = $("text").value.length; $("chars").textContent = `${n.toLocaleString("pl-PL")} znaków`; }
  $("text").addEventListener("input", () => { updateChars(); clearTimeout(saveDraft._t); saveDraft._t = setTimeout(saveDraft, 400); });
  document.addEventListener("keydown", e => { if((e.ctrlKey||e.metaKey) && e.key==="Enter"){ e.preventDefault(); analyze(); } });

  // ---- start ------------------------------------------------------------------------
  $("text").value = loadDraft(); updateChars();
  checkStatus().then(s => { if(s && !s.model_ready){ const iv = setInterval(async () => { const r = await checkStatus(); if(r?.model_ready) clearInterval(iv); }, 15000); } });
})();
