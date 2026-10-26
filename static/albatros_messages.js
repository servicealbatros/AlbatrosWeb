(() => {
  'use strict';
  if (window.AlbatrosAI) return;
  let dialog, queue = Promise.resolve();
  const tones = {
    success: { label: 'CONFIRMARE', title: 'Acțiune finalizată', icon: '<path d="m6 12 4 4 8-8"/>' },
    warning: { label: 'ATENȚIONARE', title: 'Verifică înainte de a continua', icon: '<path d="M12 7v6m0 4h.01"/><path d="m10.3 3.8-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3.2l-8-14a2 2 0 0 0-3.4 0Z"/>' },
    error: { label: 'ACȚIUNE NEFINALIZATĂ', title: 'Nu am putut finaliza acțiunea', icon: '<path d="m8 8 8 8m0-8-8 8"/><circle cx="12" cy="12" r="9"/>' },
    info: { label: 'MESAJ DE LA ROBOT', title: 'O informare pentru tine', icon: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>' }
  };
  function create() {
    if (dialog) return;
    const style = document.createElement('style');
    style.textContent = `
      .albatros-ai-dialog{box-sizing:border-box;width:min(520px,calc(100vw - 32px));max-height:calc(100dvh - 40px);padding:0;border:1px solid #ffffff90;border-radius:22px;background:#fff;color:#18323d;box-shadow:0 28px 90px #071c3660;font-family:Inter,Segoe UI,system-ui,sans-serif;overflow:auto}
      .albatros-ai-dialog::backdrop{background:#0b203a85;backdrop-filter:blur(5px)}
      .albatros-ai-dialog[open]{animation:albatros-ai-appear .16s ease-out}
      .albatros-ai-dialog .albatros-ai-header{display:flex;align-items:center;gap:13px;margin:0;padding:23px 26px;background:#112e3c;color:white}
      .albatros-ai-dialog .albatros-ai-logo{display:grid;place-items:center;width:44px;height:44px;flex:none;border:1px solid #73dac24d;border-radius:14px;background:#73dac21a;color:#83e2cd}
      .albatros-ai-dialog .albatros-ai-logo svg{width:28px;height:28px}
      .albatros-ai-dialog .albatros-ai-brand{display:block;font-size:18px;letter-spacing:1.6px;font-weight:800;line-height:1.4;color:white}
      .albatros-ai-dialog .albatros-ai-subtitle{display:block;margin-top:2px;font-size:12px;color:#b5cbd4;line-height:1.5}
      .albatros-ai-dialog button.albatros-ai-close{display:grid;place-items:center;margin:0 0 0 auto;padding:0;width:32px;height:32px;min-width:32px;border:0;border-radius:8px;background:transparent;color:#c1d4dd;font-size:26px;font-weight:400;line-height:1;cursor:pointer;box-shadow:none}
      .albatros-ai-dialog button.albatros-ai-close:hover{background:#ffffff15;color:white}
      .albatros-ai-dialog .albatros-ai-content{display:flex;gap:17px;padding:30px 26px 24px;align-items:flex-start}
      .albatros-ai-dialog .albatros-ai-status-icon{display:grid;place-items:center;width:46px;height:46px;flex:none;border-radius:15px;background:#e9f1fc;color:#37649a}
      .albatros-ai-dialog .albatros-ai-status-icon svg{width:25px;height:25px}
      .albatros-ai-dialog[data-tone=success] .albatros-ai-status-icon{background:#e3f6ee;color:#15805e}
      .albatros-ai-dialog[data-tone=warning] .albatros-ai-status-icon{background:#fff1d8;color:#ac6c0a}
      .albatros-ai-dialog[data-tone=error] .albatros-ai-status-icon{background:#fce9e8;color:#b43e37}
      .albatros-ai-dialog .albatros-ai-label{display:block;font-size:10px;letter-spacing:1.25px;font-weight:800;line-height:1.6;color:#6c8490}
      .albatros-ai-dialog h2{margin:7px 0 10px;padding:0;font-size:22px;font-weight:750;line-height:1.3;color:#18323d}
      .albatros-ai-dialog p{margin:0;padding:0;font-size:15px;line-height:1.7;color:#536d7a;white-space:pre-wrap;overflow-wrap:anywhere}
      .albatros-ai-dialog .albatros-ai-footer{display:flex;justify-content:flex-end;padding:0 26px 25px}
      .albatros-ai-dialog button.albatros-ai-acknowledge{margin:0;min-width:156px;padding:12px 20px;border:1px solid #147d70;border-radius:11px;background:#147d70;color:white;font:700 14px Inter,Segoe UI,system-ui,sans-serif;cursor:pointer;box-shadow:0 3px 8px #147d701a}
      .albatros-ai-dialog button.albatros-ai-acknowledge:hover{background:#10695e}
      .albatros-ai-dialog button:focus-visible{outline:3px solid #60cdb6;outline-offset:3px}
      @keyframes albatros-ai-appear{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
      @media(max-width:480px){.albatros-ai-dialog .albatros-ai-header{padding:20px}.albatros-ai-dialog .albatros-ai-content{padding:25px 20px 22px;gap:13px}.albatros-ai-dialog h2{font-size:20px}.albatros-ai-dialog .albatros-ai-footer{padding:0 20px 22px}.albatros-ai-dialog button.albatros-ai-acknowledge{width:100%}}
      @media(prefers-reduced-motion:reduce){.albatros-ai-dialog[open]{animation:none}}
    `;
    document.head.append(style);
    dialog = document.createElement('dialog');
    dialog.className = 'albatros-ai-dialog';
    dialog.setAttribute('role', 'alertdialog');
    dialog.setAttribute('aria-labelledby', 'albatros-ai-title');
    dialog.setAttribute('aria-describedby', 'albatros-ai-message');
    dialog.innerHTML = `<div class="albatros-ai-header"><span class="albatros-ai-logo" aria-hidden="true"><svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="10" width="20" height="17" rx="6"/><path d="M16 10V6M3 17v5m26-5v5m-17 1h8"/><circle cx="16" cy="4" r="2"/><circle cx="12" cy="17" r="1" fill="currentColor"/><circle cx="20" cy="17" r="1" fill="currentColor"/></svg></span><div><strong class="albatros-ai-brand">ALBATROS AI</strong><span class="albatros-ai-subtitle">Asistentul atelierului</span></div><button type="button" class="albatros-ai-close" aria-label="Închide mesajul">×</button></div><div class="albatros-ai-content"><span class="albatros-ai-status-icon" aria-hidden="true"></span><div><span class="albatros-ai-label"></span><h2 id="albatros-ai-title"></h2><p id="albatros-ai-message"></p></div></div><div class="albatros-ai-footer"><button type="button" class="albatros-ai-acknowledge" autofocus>Am înțeles</button></div>`;
    dialog.querySelector('.albatros-ai-close').onclick = () => dialog.close();
    dialog.querySelector('.albatros-ai-acknowledge').onclick = () => dialog.close();
    document.body.append(dialog);
  }
  function display(message, options) {
    create();
    const tone = Object.hasOwn(tones, options.tone) ? options.tone : 'info';
    const details = tones[tone];
    dialog.dataset.tone = tone;
    dialog.querySelector('.albatros-ai-label').textContent = details.label;
    dialog.querySelector('#albatros-ai-title').textContent = options.title || details.title;
    dialog.querySelector('#albatros-ai-message').textContent = String(message ?? '');
    dialog.querySelector('.albatros-ai-status-icon').innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${details.icon}</svg>`;
    const previous = document.activeElement;
    return new Promise(resolve => {
      dialog.addEventListener('close', () => {
        if (previous?.isConnected && !previous.disabled) previous.focus();
        resolve();
      }, { once: true });
      dialog.showModal();
    });
  }
  window.AlbatrosAI = Object.freeze({
    whenIdle() { return queue; },
    notify(message, options = { tone: 'warning' }) {
      const pending = queue.then(() => display(message, options));
      queue = pending.catch(() => {});
      return pending;
    }
  });
})();
