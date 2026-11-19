/* PDF Tahrirchi — brauzer qismi (kutubxonalarsiz, oflayn ishlaydi) */
(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const PT = 96 / 72;                       // 1 punkt = 1.333 CSS piksel
  const MODE_HINTS = {
    text: "Matn ustiga bosing va yangisini yozing",
    image: "Rasm ustiga bosing: almashtirish yoki o'chirish",
    area: "Sichqoncha bilan hududni belgilang",
  };

  const state = {
    cfg: { fonts: [], ocr_langs: [] },
    doc: null, pages: [], mode: 'text',
    zoom: 1, fit: true, ocr: new Set(), busy: false,
    ai: { status: null, proposals: [], running: false, abort: null, scope: 'page' },
    pop: null, pendingImage: null,
  };

  const el = {
    empty: $('#empty'), work: $('#work'), viewer: $('#viewer'), aiPanel: $('#aiPanel'),
    docName: $('#docName'), docActions: $('#docActions'), pop: $('#pop'), toasts: $('#toasts'),
    filePdf: $('#filePdf'), fileImg: $('#fileImg'),
  };

  /* ------------------------------------------------------------ yordamchilar */
  function h(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else if (k === 'style') Object.assign(n.style, v);
      else if (k === 'dataset') Object.assign(n.dataset, v);
      else if (k in n && k !== 'list') n[k] = v;
      else n.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined) n.append(kid);
    return n;
  }

  async function api(path, opts = {}) {
    let res;
    try { res = await fetch(path, opts); }
    catch (e) {
      if (e.name === 'AbortError') throw e;
      throw new Error("Server bilan aloqa yo'q. Docker konteyneri ishlab turganini tekshiring.");
    }
    let data = null;
    try { data = await res.json(); } catch { /* bo'sh javob */ }
    if (!res.ok) {
      const d = data && data.detail;
      const err = new Error(typeof d === 'string' ? d : `Xatolik yuz berdi (${res.status}).`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function toast(msg, kind = '', ms = 5000) {
    const t = h('div', { class: `toast ${kind}`, text: msg });
    el.toasts.append(t);
    setTimeout(() => t.remove(), ms);
  }

  const jsonPost = (body) => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const docUrl = (rest = '') => `/api/doc/${state.doc.id}${rest}`;

  /* ------------------------------------------------------------ hujjat */
  async function uploadPdf(file) {
    if (!file) return;
    if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') {
      toast('Faqat PDF fayl yuklash mumkin.', 'error'); return;
    }
    const fd = new FormData();
    fd.append('file', file);
    toast('Fayl yuklanmoqda…', '', 1500);
    try {
      setDoc(await api('/api/upload', { method: 'POST', body: fd }));
    } catch (e) { toast(e.message, 'error'); }
  }

  function setDoc(doc) {
    closePop();
    state.doc = doc;
    state.ocr = new Set();
    clearProposals();
    try { localStorage.setItem('pdf-tahrirchi-doc', doc.id); } catch { /* ahamiyatsiz */ }
    el.empty.hidden = true;
    el.work.hidden = false;
    el.aiPanel.hidden = false;
    el.docActions.hidden = false;
    el.docName.textContent = doc.name;
    $('#pageTotal').textContent = `/ ${doc.pages.length}`;
    $('#pageInput').max = doc.pages.length;
    $('#aiScopeWrap').hidden = doc.pages.length < 2;
    buildPages();
    syncTop();
    refreshAiStatus();
  }

  function syncTop() {
    const d = state.doc;
    $('#btnUndo').disabled = !d || !d.can_undo || state.busy;
    $('#btnRedo').disabled = !d || !d.can_redo || state.busy;
  }

  function setBusy(b) {
    state.busy = b;
    document.body.style.cursor = b ? 'progress' : '';
    syncTop();
  }

  /* server javobidan keyin: o'zgargan sahifalarni qayta chizish */
  function afterChange(r, okMsg) {
    const prev = state.doc.version;
    state.doc = { id: r.id, name: r.name, version: r.version, pages: r.pages, can_undo: r.can_undo, can_redo: r.can_redo };
    const affected = r.affected ? new Set(r.affected) : null;
    for (const pg of state.pages) {
      if (affected && !affected.has(pg.n) && pg.v === prev) pg.v = r.version;   // o'zgarmagan sahifa
      else pg.v = -1;
    }
    clearProposals();
    syncTop();
    loadVisible();
    (r.warnings || []).forEach((w) => toast(w, 'warn', 8000));
    if (okMsg) toast(okMsg, 'ok', 1800);
  }

  async function refreshDoc() {
    try {
      const r = await api(docUrl());
      afterChange(r);
    } catch (e) { toast(e.message, 'error'); }
  }

  async function applyOps(ops, okMsg) {
    if (state.busy) return false;
    setBusy(true);
    try {
      const r = await api(docUrl('/edit'), jsonPost({ base: state.doc.version, ops }));
      afterChange(r, okMsg);
      return true;
    } catch (e) {
      toast(e.message, 'error');
      if (e.status === 409) await refreshDoc();
      return false;
    } finally { setBusy(false); }
  }

  async function sendImage(action, page, bbox, file, extra = {}) {
    if (state.busy) return;
    const fd = new FormData();
    fd.append('base', state.doc.version);
    fd.append('page', page);
    fd.append('action', action);
    fd.append('bbox', JSON.stringify(bbox));
    fd.append('xref', extra.xref || 0);
    fd.append('fit', extra.fit || 'cover');
    fd.append('file', file);
    setBusy(true);
    try {
      afterChange(await api(docUrl('/image'), { method: 'POST', body: fd }),
        action === 'replace' ? 'Rasm almashtirildi' : "Rasm qo'yildi");
    } catch (e) {
      toast(e.message, 'error');
      if (e.status === 409) await refreshDoc();
    } finally { setBusy(false); }
  }

  async function history(dir) {
    if (state.busy || !state.doc) return;
    closePop();
    setBusy(true);
    try { afterChange(await api(docUrl(`/${dir}`), { method: 'POST' })); }
    catch (e) { toast(e.message, 'error'); }
    finally { setBusy(false); }
  }

  /* ------------------------------------------------------------ sahifalar */
  let observer = null;
  const visible = new Set();

  function buildPages() {
    if (observer) observer.disconnect();
    visible.clear();
    el.viewer.innerHTML = '';
    state.pages = state.doc.pages.map((p, n) => {
      const img = h('img', { alt: `${n + 1}-sahifa`, draggable: false });
      const layer = h('div', { class: 'layer', dataset: { n } });
      const node = h('section', { class: 'page', dataset: { n } }, img, layer,
        h('div', { class: 'num', text: `${n + 1}-sahifa` }));
      node.style.aspectRatio = `${p.w} / ${p.h}`;
      el.viewer.append(node);
      return { n, node, img, layer, v: -1, scale: 0, ocr: false, items: null, token: 0 };
    });
    state.fit = true;
    applyZoom();
    observer = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const n = +e.target.dataset.n;
        if (e.isIntersecting) {
          visible.add(n);
          // tez aylantirganda oraliq sahifalar yuklanmasligi uchun biroz kutamiz
          setTimeout(() => { if (visible.has(n) && state.pages[n]) loadPage(state.pages[n]); }, 140);
        } else visible.delete(n);
      }
    }, { root: el.viewer, rootMargin: '500px 0px' });
    state.pages.forEach((pg) => observer.observe(pg.node));
    el.viewer.scrollTop = 0;
  }

  function fitZoom() {
    const maxW = Math.max(...state.doc.pages.map((p) => p.w));
    const avail = el.viewer.clientWidth - 48;
    return Math.max(0.3, Math.min(1.5, avail / (maxW * PT)));
  }

  function applyZoom() {
    if (!state.doc) return;
    if (state.fit) state.zoom = fitZoom();
    state.pages.forEach((pg, i) => { pg.node.style.width = `${state.doc.pages[i].w * PT * state.zoom}px`; });
    $('#zoomFit').textContent = `${Math.round(state.zoom * 100)}%`;
    $('#zoomFit').classList.toggle('on', state.fit);
    clearTimeout(applyZoom.t);
    applyZoom.t = setTimeout(loadVisible, 250);
    positionPop();
  }

  function wantScale() {
    const s = state.zoom * PT * (window.devicePixelRatio || 1);
    return Math.min(3, Math.max(1, Math.ceil(s * 4) / 4));
  }

  function loadVisible() { visible.forEach((n) => loadPage(state.pages[n])); }

  async function loadPage(pg) {
    const v = state.doc.version;
    const scale = wantScale();
    const ocr = state.ocr.has(pg.n);
    const needImg = pg.v !== v || pg.scale < scale;
    const needItems = pg.v !== v || pg.ocr !== ocr || !pg.items;
    if (!needImg && !needItems) return;
    const token = ++pg.token;
    pg.node.classList.add('loading');
    if (needImg) pg.img.src = docUrl(`/v/${v}/page/${pg.n}.png?scale=${scale}`);
    try {
      if (needItems) {
        const items = await api(docUrl(`/page/${pg.n}/items?ocr=${ocr ? 1 : 0}`));
        if (token !== pg.token) return;
        if (items.version !== state.doc.version) { pg.v = -1; return loadPage(pg); }
        pg.items = items;
        renderLayer(pg);
      }
      pg.v = v; pg.scale = Math.max(scale, needImg ? scale : pg.scale); pg.ocr = ocr;
    } catch (e) {
      if (token === pg.token) {
        if (ocr) { state.ocr.delete(pg.n); syncOcrBtn(); }
        toast(e.message, 'error');
      }
    } finally {
      if (token === pg.token) pg.node.classList.remove('loading');
    }
  }

  function boxStyle(bbox, p) {
    return {
      left: `${(bbox[0] / p.w) * 100}%`, top: `${(bbox[1] / p.h) * 100}%`,
      width: `${((bbox[2] - bbox[0]) / p.w) * 100}%`, height: `${((bbox[3] - bbox[1]) / p.h) * 100}%`,
    };
  }

  function renderLayer(pg) {
    const p = state.doc.pages[pg.n];
    const frag = document.createDocumentFragment();
    const area = (b) => (b[2] - b[0]) * (b[3] - b[1]);
    [...pg.items.images].sort((a, b) => area(b.bbox) - area(a.bbox)).forEach((im) => {
      frag.append(h('div', {
        class: `i-item${im.background ? ' bgimg' : ''}`, style: boxStyle(im.bbox, p),
        dataset: { id: im.id }, title: im.background ? 'Fon rasmi' : 'Rasm',
      }));
    });
    pg.items.texts.forEach((t) => {
      frag.append(h('div', {
        class: `t-item${t.source === 'ocr' ? ' ocr' : ''}`, style: boxStyle(t.bbox, p), dataset: { id: t.id },
      }));
    });
    pg.layer.replaceChildren(frag);
    pg.node.querySelector('.scanhint')?.remove();
    if (pg.items.scanned && !pg.items.ocr) {
      pg.node.append(h('div', { class: 'scanhint' }, 'Bu sahifa skanerlangan — matni rasm ichida.',
        h('button', { class: 'btn small primary', text: 'Matnni aniqlash (OCR)', onclick: () => toggleOcr(pg.n, true) })));
    }
  }

  function currentPage() {
    const vr = el.viewer.getBoundingClientRect();
    const mid = vr.top + vr.height * 0.4;
    let best = 0, bestD = Infinity;
    for (const pg of state.pages) {
      const r = pg.node.getBoundingClientRect();
      if (r.top <= mid && r.bottom >= mid) return pg.n;
      const d = Math.min(Math.abs(r.top - mid), Math.abs(r.bottom - mid));
      if (d < bestD) { bestD = d; best = pg.n; }
    }
    return best;
  }

  function goToPage(n, bbox) {
    const pg = state.pages[Math.max(0, Math.min(state.pages.length - 1, n))];
    if (!pg) return;
    const vr = el.viewer.getBoundingClientRect();
    const r = pg.node.getBoundingClientRect();
    let top = el.viewer.scrollTop + (r.top - vr.top) - 24;
    if (bbox) top += (bbox[1] / state.doc.pages[pg.n].h) * r.height - vr.height * 0.35;
    top = Math.max(0, top);
    const far = Math.abs(top - el.viewer.scrollTop) > vr.height * 2.5;
    el.viewer.scrollTo({ top, behavior: far ? 'auto' : 'smooth' });
  }

  function syncPager() {
    if (!state.doc) return;
    const n = currentPage();
    const inp = $('#pageInput');
    if (document.activeElement !== inp) inp.value = n + 1;
    syncOcrBtn();
  }

  /* ------------------------------------------------------------ OCR */
  function syncOcrBtn() {
    if (!state.doc) return;
    const on = state.ocr.has(currentPage());
    const b = $('#btnOcr');
    b.classList.toggle('on', on);
    b.textContent = on ? 'OCR yoqilgan' : 'OCR: matnni aniqlash';
    b.disabled = !state.cfg.ocr_langs.length;
  }

  function toggleOcr(n, force) {
    closePop();
    const on = force ?? !state.ocr.has(n);
    if (on) {
      state.ocr.add(n);
      toast("Matn aniqlanmoqda… bir necha soniya vaqt oladi.", '', 2500);
    } else state.ocr.delete(n);
    syncOcrBtn();
    loadPage(state.pages[n]);
  }

  /* ------------------------------------------------------------ qalqib chiquvchi oyna */
  function openPop(anchorEl, content, onClose) {
    closePop();
    state.pop = { anchorEl, onClose };
    el.pop.replaceChildren(...content.filter(Boolean));
    el.pop.hidden = false;
    anchorEl?.classList.add('active');
    positionPop();
  }

  function positionPop() {
    if (!state.pop || !state.pop.anchorEl) return;
    const a = state.pop.anchorEl.getBoundingClientRect();
    const pw = el.pop.offsetWidth, ph = el.pop.offsetHeight;
    let top = a.bottom + 8;
    if (top + ph > window.innerHeight - 8) top = a.top - ph - 8;
    if (top < 8) top = Math.max(8, window.innerHeight - ph - 8);
    let left = a.left + a.width / 2 - pw / 2;
    left = Math.max(8, Math.min(window.innerWidth - pw - 8, left));
    el.pop.style.top = `${top}px`;
    el.pop.style.left = `${left}px`;
  }

  function closePop() {
    if (!state.pop) return;
    state.pop.anchorEl?.classList.remove('active');
    const cb = state.pop.onClose;
    state.pop = null;
    el.pop.hidden = true;
    el.pop.replaceChildren();
    if (cb) cb();
  }

  /* uslub boshqaruvlari: o'lcham, qalin, kursiv, rang, tekislash, shrift */
  function styleControls(init) {
    const st = { ...init };
    const touched = new Set();
    const size = h('input', { type: 'number', min: 3, max: 300, step: 0.5, value: Math.round(init.size * 10) / 10,
      oninput: () => { st.size = parseFloat(size.value); touched.add('size'); } });
    const tog = (key, cls, label, title) => {
      const b = h('button', { class: `btn tog ${cls}${st[key] ? ' on' : ''}`, type: 'button', text: label, title,
        onclick: () => { st[key] = !st[key]; b.classList.toggle('on', st[key]); touched.add(key); } });
      return b;
    };
    const color = h('input', { type: 'color', value: init.color || '#000000', title: 'Matn rangi',
      oninput: () => { st.color = color.value; touched.add('color'); } });
    const align = h('div', { class: 'seg small' });
    [['left', 'Chap'], ['center', "O'rta"], ['right', "O'ng"]].forEach(([v, label]) => {
      align.append(h('button', { type: 'button', class: st.align === v ? 'on' : '', text: label,
        onclick: (e) => {
          st.align = v; touched.add('align');
          align.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === e.target));
        } }));
    });
    const font = h('select', { title: 'Shrift', onchange: () => { st.family = font.value || null; touched.add('family'); } },
      h('option', { value: '', text: init.autoLabel || 'Shrift: avto' }),
      state.cfg.fonts.map((f) => h('option', { value: f.id, text: f.label })));
    const rows = [
      h('div', { class: 'pop-row' },
        h('label', {}, "O'lcham", size), tog('bold', 'b', 'B', 'Qalin'), tog('italic', 'i', 'I', 'Kursiv'), color, align),
      h('div', { class: 'pop-row' }, font),
    ];
    const values = (all) => {
      const out = {};
      for (const k of ['size', 'bold', 'italic', 'color', 'align', 'family']) {
        if (!(all || touched.has(k))) continue;
        if (k === 'size' && !(st.size >= 3 && st.size <= 300)) continue;
        if (k === 'family' && !st.family) continue;
        out[k] = st[k];
      }
      return out;
    };
    return { rows, values };
  }

  /* --- matnni tahrirlash */
  function openTextPop(pg, item, node) {
    const ctl = styleControls({
      size: item.size, bold: item.bold, italic: item.italic, color: item.color || '#000000',
      align: item.align || 'left',
      autoLabel: item.source === 'pdf' ? `Shrift: asliga mos (${item.font || 'avto'})` : 'Shrift: avto',
    });
    const ta = h('textarea', { rows: 1, value: item.text, spellcheck: false });
    const grow = () => { ta.style.height = 'auto'; ta.style.height = `${Math.min(140, ta.scrollHeight + 2)}px`; };
    const save = async () => {
      const text = ta.value.replace(/\s+/g, ' ').trim();
      const style = ctl.values(false);
      if (text === item.text && !Object.keys(style).length) { closePop(); return; }
      closePop();
      await applyOps([{ type: 'text', page: pg.n, id: item.id, text, ...style }], text ? "Matn o'zgartirildi" : "Matn o'chirildi");
    };
    ta.addEventListener('input', grow);
    ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); save(); } });
    openPop(node, [
      h('div', { class: 'pop-title' }, "Matnni o'zgartirish",
        item.source === 'ocr' ? h('span', { class: 'badge', text: 'OCR', title: 'Rasm ichidan aniqlangan matn' }) : null),
      ta, ...ctl.rows,
      item.source === 'ocr'
        ? h('p', { class: 'pop-note', text: "Bu matn rasm ichida: eski yozuv fon rangi bilan yopiladi va ustiga yangisi yoziladi." })
        : null,
      h('div', { class: 'pop-actions' },
        h('button', { class: 'btn small danger', type: 'button', text: "O'chirish",
          onclick: async () => { closePop(); await applyOps([{ type: 'text', page: pg.n, id: item.id, text: '' }], "Matn o'chirildi"); } }),
        h('span', { class: 'spacer' }),
        h('button', { class: 'btn small', type: 'button', text: 'Bekor', onclick: closePop }),
        h('button', { class: 'btn small primary', type: 'button', text: 'Saqlash', onclick: save })),
    ]);
    grow(); positionPop();
    ta.focus(); ta.select();
  }

  /* --- rasm */
  function pickImage(cb) {
    state.pendingImage = cb;
    el.fileImg.value = '';
    el.fileImg.click();
  }

  function openImagePop(pg, im, node) {
    const fit = h('select', {},
      h('option', { value: 'cover', text: "To'ldirish (chetlari qirqilishi mumkin)" }),
      h('option', { value: 'contain', text: "Sig'dirish (rasm to'liq ko'rinadi)" }),
      h('option', { value: 'stretch', text: "Cho'zish (joyni aynan egallaydi)" }));
    openPop(node, [
      h('div', { class: 'pop-title' }, im.background ? 'Fon rasmi' : 'Rasm'),
      h('p', { class: 'pop-note', text: `Asl o'lchami: ${im.px[0]}×${im.px[1]} piksel. Yangi rasm aynan shu joyga qo'yiladi.` }),
      h('label', { class: 'field' }, h('span', { text: 'Yangi rasm qanday joylashsin' }), fit),
      h('div', { class: 'pop-actions' },
        h('button', { class: 'btn small danger', type: 'button', text: "O'chirish", disabled: !im.xref,
          onclick: async () => { closePop(); await applyOps([{ type: 'image_delete', page: pg.n, xref: im.xref }], "Rasm o'chirildi"); } }),
        h('span', { class: 'spacer' }),
        h('button', { class: 'btn small', type: 'button', text: 'Bekor', onclick: closePop }),
        h('button', { class: 'btn small primary', type: 'button', text: 'Rasm tanlash…',
          onclick: () => {
            const mode = fit.value;
            pickImage((file) => { closePop(); sendImage('replace', pg.n, im.bbox, file, { xref: im.xref, fit: mode }); });
          } })),
    ]);
  }

  /* --- hudud */
  function openAreaPop(pg, bbox, selNode) {
    const done = () => selNode.remove();
    const hasText = pg.items && pg.items.texts.some((t) => t.source === 'pdf'
      && t.bbox[0] < bbox[2] && t.bbox[2] > bbox[0] && t.bbox[1] < bbox[3] && t.bbox[3] > bbox[1]);
    const color = h('input', { type: 'color', value: '#ffffff', title: 'Rang tanlash' });
    let custom = false;
    color.addEventListener('input', () => { custom = true; });
    const item = (title, desc, onclick, extra) => h('button', { class: 'btn', type: 'button', onclick },
      h('div', {}, h('span', { text: title }), h('small', { text: desc })), extra || null);

    const menu = () => [
      h('div', { class: 'pop-title' }, 'Belgilangan hudud'),
      h('div', { class: 'pop-menu' },
        hasText ? item("Matnni o'chirish", 'Faqat matn olib tashlanadi, fon saqlanadi',
          async () => { closePop(); await applyOps([{ type: 'erase', page: pg.n, bbox }], "Matn o'chirildi"); }) : null,
        item('Rang bilan yopish', "Hudud fon rangiga bo'yaladi (rang avtomatik aniqlanadi)",
          async () => { closePop(); await applyOps([{ type: 'erase', page: pg.n, bbox, fill: custom ? color.value : 'auto' }], 'Hudud yopildi'); }),
        item('Matn yozish', "Shu joyga yangi matn qo'shiladi", () => { el.pop.replaceChildren(...textForm().filter(Boolean)); positionPop(); $('textarea', el.pop).focus(); }),
        item("Rasm qo'yish", 'Shu joyga rasm joylanadi (nisbati saqlanadi)',
          () => pickImage((file) => { closePop(); sendImage('add', pg.n, bbox, file); }))),
      h('div', { class: 'pop-row' }, h('label', {}, 'Yopish rangi (ixtiyoriy):', color)),
      h('div', { class: 'pop-actions' }, h('span', { class: 'spacer' }),
        h('button', { class: 'btn small', type: 'button', text: 'Bekor', onclick: closePop })),
    ];

    const textForm = () => {
      const ctl = styleControls({
        size: Math.max(6, Math.min(12, Math.round((bbox[3] - bbox[1]) * 0.7))), bold: false, italic: false,
        color: '#000000', align: 'left', autoLabel: 'Shrift: Arial uslubi',
      });
      const ta = h('textarea', { rows: 2, placeholder: 'Matnni yozing…', spellcheck: false });
      const save = async () => {
        if (!ta.value.trim()) { closePop(); return; }
        const text = ta.value;
        closePop();
        await applyOps([{ type: 'add_text', page: pg.n, bbox, text, ...ctl.values(true) }], "Matn qo'shildi");
      };
      ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); } });
      return [
        h('div', { class: 'pop-title' }, 'Yangi matn'), ta, ...ctl.rows,
        h('p', { class: 'pop-note', text: "Yangi qator — Enter, saqlash — Ctrl+Enter." }),
        h('div', { class: 'pop-actions' }, h('span', { class: 'spacer' }),
          h('button', { class: 'btn small', type: 'button', text: 'Bekor', onclick: closePop }),
          h('button', { class: 'btn small primary', type: 'button', text: "Qo'shish", onclick: save })),
      ];
    };
    openPop(selNode, menu(), done);
  }

  /* ------------------------------------------------------------ ko'rish maydonidagi hodisalar */
  function setMode(mode) {
    closePop();
    state.mode = mode;
    el.viewer.className = `viewer mode-${mode}`;
    document.querySelectorAll('#modes button').forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
    $('#modeHint').textContent = MODE_HINTS[mode];
  }

  el.viewer.addEventListener('click', (e) => {
    const node = e.target.closest('.t-item, .i-item');
    if (!node || state.busy) return;
    const pg = state.pages[+node.parentElement.dataset.n];
    if (!pg || !pg.items) return;
    if (node.classList.contains('t-item') && state.mode === 'text') {
      const item = pg.items.texts.find((t) => t.id === node.dataset.id);
      if (item) openTextPop(pg, item, node);
    } else if (node.classList.contains('i-item') && state.mode === 'image') {
      const im = pg.items.images.find((i) => i.id === node.dataset.id);
      if (im) openImagePop(pg, im, node);
    }
  });

  el.viewer.addEventListener('pointerdown', (e) => {
    if (state.mode !== 'area' || e.button !== 0 || state.busy) return;
    const layer = e.target.closest('.layer');
    if (!layer) return;
    e.preventDefault();
    closePop();
    const pg = state.pages[+layer.dataset.n];
    const p = state.doc.pages[pg.n];
    const r = layer.getBoundingClientRect();
    const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
    const pt = (ev) => [clamp((ev.clientX - r.left) / r.width, 0, 1), clamp((ev.clientY - r.top) / r.height, 0, 1)];
    const [sx, sy] = pt(e);
    const sel = h('div', { class: 'sel' });
    layer.append(sel);
    let box = [sx, sy, sx, sy];
    const move = (ev) => {
      const [x, y] = pt(ev);
      box = [Math.min(sx, x), Math.min(sy, y), Math.max(sx, x), Math.max(sy, y)];
      Object.assign(sel.style, {
        left: `${box[0] * 100}%`, top: `${box[1] * 100}%`,
        width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%`,
      });
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      const bbox = [box[0] * p.w, box[1] * p.h, box[2] * p.w, box[3] * p.h].map((v) => Math.round(v * 100) / 100);
      if (bbox[2] - bbox[0] < 4 || bbox[3] - bbox[1] < 4) { sel.remove(); return; }
      openAreaPop(pg, bbox, sel);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  });

  el.viewer.addEventListener('scroll', () => { positionPop(); syncPager(); }, { passive: true });

  document.addEventListener('pointerdown', (e) => {
    if (!state.pop) return;
    if (el.pop.contains(e.target) || state.pop.anchorEl?.contains(e.target)) return;
    closePop();
  }, true);

  /* ------------------------------------------------------------ AI */
  async function refreshAiStatus() {
    const box = $('#aiStatus');
    box.className = 'ai-status';
    $('.txt', box).textContent = 'Tekshirilmoqda…';
    let st;
    try { st = await api('/api/ai/status'); } catch { st = { ok: false, models: [] }; }
    state.ai.status = st;
    const sel = $('#aiModel');
    const prev = sel.value || (() => { try { return localStorage.getItem('pdf-tahrirchi-model'); } catch { return null; } })();
    sel.replaceChildren(...st.models.map((m) => h('option', { value: m, text: m })));
    if (st.ok && st.models.length) {
      sel.value = st.models.includes(prev) ? prev : st.default;
      box.classList.add('ok');
      $('.txt', box).textContent = `Ollama ulangan · ${st.models.length} ta model`;
    } else if (st.ok) {
      box.classList.add('bad');
      $('.txt', box).textContent = "Ollama ulangan, lekin model yo'q (ollama pull qwen2.5:7b)";
    } else {
      box.classList.add('bad');
      $('.txt', box).textContent = 'Ollama ulanmagan';
    }
    $('#aiHelp').hidden = st.ok;
    $('#aiBody').classList.toggle('off', !(st.ok && st.models.length));
  }

  function clearProposals() {
    state.ai.proposals = [];
    $('#proposals').hidden = true;
    $('#propList').replaceChildren();
  }

  function renderProposals() {
    const list = state.ai.proposals;
    $('#proposals').hidden = !list.length;
    $('#propTitle').textContent = `Takliflar: ${list.length} ta`;
    $('#propList').replaceChildren(...list.map((p) => {
      const cb = h('input', { type: 'checkbox', checked: p.on, onclick: (e) => e.stopPropagation(),
        onchange: () => { p.on = cb.checked; syncApply(); } });
      const inp = h('input', { type: 'text', value: p.new, spellcheck: false, onclick: (e) => e.stopPropagation(),
        oninput: () => { p.new = inp.value; } });
      return h('div', { class: 'prop', onclick: () => showProposal(p) }, cb,
        h('div', { class: 'old' },
          state.doc.pages.length > 1 ? h('span', { class: 'pg', text: `${p.page + 1}-sahifa` }) : null, p.old),
        inp);
    }));
    syncApply();
  }

  function syncApply() {
    const n = state.ai.proposals.filter((p) => p.on).length;
    const b = $('#propApply');
    b.disabled = !n;
    b.textContent = n ? `Tanlanganlarni qo'llash (${n})` : "Tanlanganlarni qo'llash";
  }

  function showProposal(p) {
    goToPage(p.page, p.bbox);
    setTimeout(() => {
      const node = state.pages[p.page].layer.querySelector(`[data-id="${p.id}"]`);
      if (node) { node.classList.remove('flash'); void node.offsetWidth; node.classList.add('flash'); }
    }, 350);
  }

  async function runAi() {
    const instruction = $('#aiText').value.trim();
    const model = $('#aiModel').value;
    if (!instruction) { toast("Avval nimani o'zgartirish kerakligini yozing.", 'warn'); $('#aiText').focus(); return; }
    if (!model || state.ai.running) return;
    try { localStorage.setItem('pdf-tahrirchi-model', model); } catch { /* ahamiyatsiz */ }
    const ai = state.ai;
    const prog = $('#aiProgress');
    ai.running = true;
    ai.abort = new AbortController();
    clearProposals();
    $('#aiRun').disabled = true;
    $('#aiCancel').hidden = false;
    const version = state.doc.version;
    const t0 = Date.now();
    const timer = setInterval(() => {
      prog.textContent = `${prog.dataset.label || 'Model o\'ylamoqda'}… ${Math.round((Date.now() - t0) / 1000)} s`;
    }, 500);
    try {
      let batches;
      if (ai.scope === 'page' || state.doc.pages.length < 2) batches = [[currentPage()]];
      else batches = (await api(docUrl('/ai/batches'), { signal: ai.abort.signal })).batches;
      if (!batches.length) { toast("Hujjatda matn topilmadi. Skanerlangan bo'lsa, avval OCR tugmasini bosing.", 'warn'); return; }
      let i = 0;
      for (const pages of batches) {
        i += 1;
        prog.dataset.label = batches.length > 1 ? `${i}/${batches.length}-qism` : "Model o'ylamoqda";
        const r = await api(docUrl('/ai/plan'), {
          ...jsonPost({ base: version, pages, instruction, model }), signal: ai.abort.signal,
        });
        if (state.doc.version !== version) return;
        ai.proposals.push(...r.proposals.map((p) => ({ ...p, on: true })));
        renderProposals();
      }
      if (!ai.proposals.length) {
        const where = batches.length === 1 && state.doc.pages.length > 1 ? ' Bu sahifada' : '';
        toast(`Mos keladigan matn topilmadi.${where} Ko'rsatmani aniqroq yozib ko'ring${state.doc.pages.length > 1 && ai.scope === 'page' ? " yoki «Butun hujjat»ni tanlang" : ''}.`, 'warn', 8000);
      }
    } catch (e) {
      if (e.name !== 'AbortError') {
        toast(e.message, 'error', 8000);
        if (e.status === 409) await refreshDoc();
        if (e.status === 502) refreshAiStatus();
      }
    } finally {
      clearInterval(timer);
      prog.textContent = '';
      delete prog.dataset.label;
      ai.running = false;
      $('#aiRun').disabled = false;
      $('#aiCancel').hidden = true;
      // model xotiradan chiqariladi — RAM bo'shaydi
      fetch('/api/ai/unload', jsonPost({ model })).catch(() => {});
    }
  }

  async function applyProposals() {
    const ops = state.ai.proposals.filter((p) => p.on)
      .map((p) => ({ type: 'text', page: p.page, id: p.id, text: p.new }));
    if (!ops.length) return;
    await applyOps(ops, `${ops.length} ta o'zgarish qo'llandi`);
  }

  /* ------------------------------------------------------------ tugmalar */
  $('#btnPick').onclick = $('#btnOpen').onclick = () => { el.filePdf.value = ''; el.filePdf.click(); };
  el.filePdf.onchange = () => uploadPdf(el.filePdf.files[0]);
  el.fileImg.onchange = () => {
    const f = el.fileImg.files[0];
    const cb = state.pendingImage;
    state.pendingImage = null;
    if (f && cb) cb(f);
  };
  $('#btnUndo').onclick = () => history('undo');
  $('#btnRedo').onclick = () => history('redo');
  $('#btnDownload').onclick = () => { window.location.href = docUrl('/download'); };
  $('#btnOcr').onclick = () => toggleOcr(currentPage());
  $('#modes').onclick = (e) => { const b = e.target.closest('button'); if (b) setMode(b.dataset.mode); };
  $('#zoomIn').onclick = () => { state.fit = false; state.zoom = Math.min(4, state.zoom * 1.2); applyZoom(); };
  $('#zoomOut').onclick = () => { state.fit = false; state.zoom = Math.max(0.25, state.zoom / 1.2); applyZoom(); };
  $('#zoomFit').onclick = () => { state.fit = true; applyZoom(); };
  $('#pageInput').onchange = (e) => goToPage((parseInt(e.target.value, 10) || 1) - 1);
  $('#aiRefresh').onclick = refreshAiStatus;
  $('#aiRun').onclick = runAi;
  $('#aiCancel').onclick = () => state.ai.abort?.abort();
  $('#aiScope').onclick = (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    state.ai.scope = b.dataset.scope;
    document.querySelectorAll('#aiScope button').forEach((x) => x.classList.toggle('on', x === b));
  };
  $('#aiText').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) runAi(); });
  $('#propClear').onclick = clearProposals;
  $('#propApply').onclick = applyProposals;

  window.addEventListener('resize', () => { if (state.doc) applyZoom(); });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closePop(); return; }
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
    if (typing || !(e.ctrlKey || e.metaKey)) return;
    const k = e.key.toLowerCase();
    if (k === 'z' && !e.shiftKey) { e.preventDefault(); history('undo'); }
    else if (k === 'y' || (k === 'z' && e.shiftKey)) { e.preventDefault(); history('redo'); }
  });

  /* faylni sudrab tashlash */
  let dragDepth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragenter', (e) => { if (hasFiles(e)) { dragDepth += 1; $('#dragveil').hidden = false; } });
  window.addEventListener('dragleave', () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $('#dragveil').hidden = true; });
  window.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    $('#dragveil').hidden = true;
    uploadPdf(e.dataTransfer.files[0]);
  });

  /* ------------------------------------------------------------ ishga tushirish */
  (async () => {
    setMode('text');
    try { state.cfg = await api('/api/config'); } catch (e) { toast(e.message, 'error'); }
    let last = null;
    try { last = localStorage.getItem('pdf-tahrirchi-doc'); } catch { /* ahamiyatsiz */ }
    if (last && /^[a-f0-9]{16}$/.test(last)) {
      try { setDoc(await api(`/api/doc/${last}`)); } catch { /* eski hujjat o'chgan */ }
    }
  })();
})();
