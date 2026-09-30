'use strict';
(function () {
  function mount(document, chrome) {
    const review = document.getElementById('review'), capture = document.getElementById('capture');
    const section = document.getElementById('disclosure'), preview = document.getElementById('preview');
    const status = document.getElementById('status');
    let port = null, phase = 'new';
    const unavailable = () => {
      phase = 'closed'; capture.disabled = true; section.hidden = true;
      preview.textContent = ''; status.textContent = 'Capture unavailable. Close this panel and start again.';
    };
    review.addEventListener('click', event => {
      if (!event.isTrusted || phase !== 'new') return;
      phase = 'awaiting';
      review.disabled = true;
      try {
        port = chrome.runtime.connect({name: 'wisp-public-disclosure'});
        port.onDisconnect.addListener(unavailable);
        port.onMessage.addListener(message => {
          if (phase === 'closed' || phase === 'completed') return;
          if (message.type === 'disclosure' && phase === 'awaiting') {
            // Only textContent: reviewed catalog text never becomes active HTML.
            preview.textContent = JSON.stringify(message.preview, null, 2);
            section.hidden = false; phase = 'disclosed'; capture.disabled = false;
            status.textContent = 'Review every listed value before capturing.';
          } else if (message.type === 'captured' && phase === 'consumed') {
            capture.disabled = true; phase = 'completed';
            status.textContent = 'Approved public manifest captured. Page coverage is partial.';
          } else unavailable();
        });
        port.postMessage({op: 'prepare'});
      } catch (_) { unavailable(); }
    });
    capture.addEventListener('click', event => {
      if (!event.isTrusted || phase !== 'disclosed' || capture.disabled || section.hidden) return;
      phase = 'consumed'; capture.disabled = true;
      try { port.postMessage({op: 'capture'}); } catch (_) { unavailable(); }
    });
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {mount};
  else mount(document, chrome);
})();
