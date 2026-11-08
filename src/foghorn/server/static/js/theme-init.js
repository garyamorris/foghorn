// Runs before first paint (classic script) so a manually chosen theme never flashes.
(function () {
  try {
    var t = JSON.parse(localStorage.getItem('foghorn.theme') || 'null');
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
  } catch (e) {
    /* storage unavailable: follow the OS */
  }
})();
