/* MV3 wiring seam. Trusted bootstrap must explicitly call install; no runtime
 * host, installable manifest, or automatic startup is supplied by this slice. */
'use strict';
(function () {
  function install(chrome, helper) {
    const popupURL = chrome.runtime.getURL('chrome/disclosure.html');
    chrome.runtime.onConnect.addListener(port => {
      const sender = port.sender;
      if (port.name !== 'wisp-public-disclosure' || !sender || sender.id !== chrome.runtime.id ||
          sender.url !== popupURL || sender.tab !== undefined ||
          sender.origin !== 'chrome-extension://' + chrome.runtime.id) {
        port.disconnect(); return;
      }
      let handle;
      try { handle = helper.connect(); } catch (_) { port.disconnect(); return; }
      let phase = 'new', disclosure = null;
      const close = () => { phase = 'closed'; disclosure = null; helper.disconnect(handle); };
      port.onDisconnect.addListener(close);
      port.onMessage.addListener(message => {
        try {
          if (!message || typeof message !== 'object' || Array.isArray(message) ||
              Object.keys(message).length !== 1 || typeof message.op !== 'string') throw new Error();
          if (message.op === 'prepare' && phase === 'new') {
            const preview = JSON.parse(helper.prepare(handle));
            disclosure = preview.disclosure_id; phase = 'preview';
            port.postMessage({type: 'disclosure', preview: preview.preview});
          } else if (message.op === 'capture' && phase === 'preview') {
            phase = 'consumed';
            helper.capture(handle, JSON.stringify({disclosure_id: disclosure}));
            disclosure = null;
            port.postMessage({type: 'captured'});
          } else throw new Error();
        } catch (_) { close(); port.postMessage({type: 'unavailable'}); port.disconnect(); }
      });
    });
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {install};
  else globalThis.WispChromeAcquisition = Object.freeze({install});
})();
