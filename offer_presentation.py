"""Presentation only: no catalogue, price, selection or approval mutations."""
from html import escape

STYLE = '''
body{font-family:"Segoe UI",Arial,sans-serif}
.parts-workspace{margin-top:16px}.parts-tools{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.parts-tools label{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:600;cursor:pointer}
.part-group{border:1px solid #e2e8f0;border-left:4px solid #d8a445;border-radius:12px;background:#fff;margin:10px 0;overflow:hidden}
.part-group[data-selected="true"]{border-left-color:#269564}
.part-group>summary{display:flex;justify-content:space-between;align-items:center;gap:12px;cursor:pointer;padding:16px;background:#fff8ec;font-weight:700;list-style:none}
.part-group[data-selected="true"]>summary{background:#edf9f2}
.part-group>summary::before{content:"▸";color:#637286}.part-group[open]>summary::before{content:"▾"}
.part-group-title{flex:1}.part-group-status{font-size:12px;color:#805d22}.part-group[data-selected="true"] .part-group-status{color:#13784c}
.part-group table{margin:0;min-width:790px}.part-group th{background:#f5f7fa;color:#627084;font-size:11px;text-transform:uppercase;letter-spacing:.4px;padding:12px}
.part-group td{font-size:13px;padding:14px 12px;vertical-align:top;line-height:1.5}.part-group td:first-child{display:none}.part-group th:first-child{display:none}
.part-group .selected-product td{background:#edf9f2;border-top:1px solid #a2d9ba;border-bottom:1px solid #a2d9ba}
.part-group td:last-child{min-width:160px}.part-group td:last-child a{display:block;font-size:12px;margin-bottom:8px}
.part-description{max-width:240px}.part-description summary{cursor:pointer;color:#53667c;font-size:12px}.part-table-scroll{overflow-x:auto}
.parts-missing{border:1px solid #efb2b2;background:#fff1f1;color:#a12e2e;padding:14px;border-radius:10px;margin:12px 0}
.parts-progress{color:#526174;font-size:14px}.parts-filter-empty{padding:20px;color:#21805a;background:#eefaf3;border-radius:10px}
.offer-dock{position:fixed;bottom:0;left:0;right:0;z-index:100;background:#192433;color:#fff;border-top:3px solid #d7ae54;padding:12px 24px;display:flex;justify-content:space-between;align-items:center;gap:20px;box-shadow:0 -4px 20px #13223620}
.offer-dock span{font-size:13px;color:#c8d2dd}.offer-dock strong{font-size:20px;white-space:nowrap}.offer-dock a{color:#efc570;font-size:13px;text-decoration:none}.wrap{margin-bottom:120px}
.part-group:focus-within{outline:2px solid #a2b8d1}.parts-tools input{accent-color:#1d8558;width:18px;height:18px}
@media(max-width:650px){.offer-dock{padding:10px 14px;gap:10px;flex-wrap:wrap}.offer-dock strong{font-size:17px}.part-group>summary{padding:12px}.part-group-status{max-width:130px;text-align:right}.parts-tools{align-items:flex-start}}
'''

SCRIPT = '''
document.addEventListener('DOMContentLoaded',()=>{
 const filter=document.getElementById('partsOutstandingOnly');
 const groups=[...document.querySelectorAll('.part-group')];
 const storageKey='albatros-parts-filter:'+location.pathname;
 try{filter.checked=sessionStorage.getItem(storageKey)==='true'}catch(e){}
 function apply(){let shown=0;groups.forEach(g=>{g.hidden=filter.checked&&g.dataset.selected==='true';if(!g.hidden)shown++});document.getElementById('partsFilterEmpty').hidden=shown>0||groups.length===0;try{sessionStorage.setItem(storageKey,String(filter.checked))}catch(e){}}
 filter.addEventListener('change',apply);apply();
 const total=document.getElementById('total_value');
 const mirror=()=>{document.getElementById('dockTotal').textContent=total?.textContent||'—';const val=id=>{const n=Number(document.getElementById(id)?.value);return Number.isFinite(n)?n:0};const money=n=>n.toLocaleString('ro-RO',{minimumFractionDigits:2,maximumFractionDigits:2})+' RON';document.getElementById('dockLabour').textContent=money(val('labour_price_ron'));};
 if(total)new MutationObserver(mirror).observe(total,{childList:true,characterData:true,subtree:true});document.addEventListener('input',mirror);mirror();
});
'''


def render_parts_workspace(groups, missing, *, checklist=None):
    from offer_parts_checklist import line_gaps
    checklist = checklist or []
    checklist_notice = ''
    if checklist:
        pending = line_gaps(checklist)
        checklist_notice = (f'<div class="parts-progress"><b>Necesar editabil: {len(checklist)} poziții</b>'
                            f' · {len(pending)} de verificat. '
                            '<a href="#quote_parts_checklist">Vezi codurile, prețurile și verificările de mai sus ↑</a>'
                            '<br><small>Lista de mai jos conține variante suplimentare de la furnizori. '
                            'Lipsa variantelor nu înseamnă că necesarul editabil este gol. '
                            'Completarea manuală nu dovedește identificarea automată în catalog.</small></div>')
    selected = sum(bool(group['selected']) for group in groups)
    sections = []
    for group in groups:
        done = bool(group['selected'])
        count = len(group['rows'])
        sections.append(f'''<details class="part-group" data-selected="{str(done).lower()}" {'' if done else 'open'}>
          <summary><span class="part-group-title">{escape(group['label'])}</span>
          <span class="part-group-status">{'✓ Piesă selectată' if done else 'De ales'} · {count} variante</span></summary>
          <div class="part-table-scroll"><table><thead><tr><th>Tip</th><th>Marcă / furnizor</th><th>Detalii</th><th>Cod piesă</th><th>Preț</th><th>Stoc</th><th>Alegere</th></tr></thead>
          <tbody>{''.join(group['rows'])}</tbody></table></div></details>''')
    missing_html = ('<div class="parts-missing"><b>Rezultate lipsă:</b> ' + escape(', '.join(map(str, missing))) + '</div>') if missing else ''
    progress = (f'<b>{selected} / {len(groups)}</b> grupe cu variantă selectată' if groups
                else 'Nu există variante suplimentare de la furnizori.' if checklist else 'Nu există încă rezultate de piese.')
    return f'''<style>{STYLE}</style><section class="parts-workspace" id="partsWorkspace">
      {checklist_notice}<div class="parts-tools"><div class="parts-progress">{progress}<br><small>Selecția pieselor nu înlocuiește verificarea ofertei.</small></div>
      <label><input id="partsOutstandingOnly" type="checkbox">Arată doar piesele neselectate</label></div>
      {missing_html}{''.join(sections)}
      <div id="partsFilterEmpty" class="parts-filter-empty" hidden>Toate grupele cu rezultate au o piesă selectată. Verifică și eventualele rezultate lipsă de mai sus.</div>
      </section><aside class="offer-dock"><div><b>{f'Necesar: {len(checklist)} poziții' if checklist else f'Piese: {selected}/{len(groups)} grupe'}</b> <span>· Manoperă: <span id="dockLabour">—</span></span></div>
      <a href="#partsWorkspace">Înapoi la piese ↑</a><div><span>Total estimativ </span><strong id="dockTotal">—</strong></div></aside><script>{SCRIPT}</script>'''
