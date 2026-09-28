/* A07 staging target. No page permission, capture, startup message or host call. */
'use strict';
importScripts('contracts.js', 'page-extractor.js');
// Loading the A01/A05 shared modules is a packaging compatibility check only.
if (!globalThis.WispBrowserContracts || !globalThis.WispPageExtractor) {
  throw new Error('Shared browser modules unavailable');
}
