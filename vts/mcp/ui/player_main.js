(function() {
  // Entry point of ui://vts/player: connect to the host and mount the player
  // on the show_recording result.
  var bridge = VtsAppBridge.connect({ name: "vts-player", version: "1" });
  var root = document.querySelector("[data-player-root]");

  // The host sizes the iframe from what the view reports.
  function reportSize() {
    bridge.reportSize(document.documentElement.scrollWidth, document.documentElement.scrollHeight);
  }
  if (window.ResizeObserver) new ResizeObserver(reportSize).observe(document.body);

  bridge.on("tool-result", function(result) {
    if (result.isError) {
      VtsPlayerView.setStatusIn(root, ((result.content || [])[0] || {}).text || VtsPlayerView.msgs().failed);
      return;
    }
    VtsPlayerView.mount(bridge, root, result.structuredContent || {});
  });
  bridge.on("tool-cancelled", function() { VtsPlayerView.setStatusIn(root, VtsPlayerView.msgs().cancelled); });
  bridge.on("host-context", function() { VtsPlayerView.applyHostContext(bridge.hostContext); });

  bridge.ready.then(function() {
    VtsPlayerView.applyHostContext(bridge.hostContext);
    VtsPlayerView.setStatusIn(root, VtsPlayerView.msgs().loading);
  }).catch(function() { VtsPlayerView.setStatusIn(root, VtsPlayerView.msgs().failed); });
})();
