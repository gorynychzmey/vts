// The MCP Apps bridge (io.modelcontextprotocol/ui, spec 2026-01-26): JSON-RPC
// 2.0 over postMessage between this view, running in the host's sandboxed
// iframe, and the host that loaded it.
//
// Written by hand rather than taken from @modelcontextprotocol/ext-apps so the
// views need no JS build step (spec §4.2.1). It covers what the views use:
//   view -> host  ui/initialize, ui/notifications/initialized, tools/call,
//                 ui/update-model-context, ui/notifications/size-changed
//   host -> view  ui/notifications/tool-input, ui/notifications/tool-result,
//                 ui/notifications/tool-cancelled,
//                 ui/notifications/host-context-changed, ui/resource-teardown
// The tests/ui scenario drives it against a host stub, and the spec's ordering
// rule (nothing from the host before `initialized`) is what it relies on.
var VtsAppBridge = (function() {
  var PROTOCOL_VERSION = "2026-01-26";

  function connect(appInfo) {
    var nextId = 1;
    var pending = {};
    var handlers = {};
    var target = window.parent;

    function send(message) {
      message.jsonrpc = "2.0";
      target.postMessage(message, "*");
    }

    function request(method, params) {
      var id = nextId++;
      return new Promise(function(resolve, reject) {
        pending[id] = { resolve: resolve, reject: reject };
        send({ id: id, method: method, params: params || {} });
      });
    }

    function notify(method, params) {
      send({ method: method, params: params || {} });
    }

    function emit(name, params) {
      (handlers[name] || []).forEach(function(fn) {
        try { fn(params); } catch (e) { /* one handler must not stop the rest */ }
      });
    }

    window.addEventListener("message", function(event) {
      // Only the frame that loaded us speaks the protocol.
      if (event.source !== target) return;
      var msg = event.data;
      if (!msg || msg.jsonrpc !== "2.0") return;
      if (msg.id !== undefined && !msg.method) {
        var waiter = pending[msg.id];
        if (!waiter) return;
        delete pending[msg.id];
        if (msg.error) waiter.reject(new Error(msg.error.message || "host error"));
        else waiter.resolve(msg.result);
        return;
      }
      switch (msg.method) {
        case "ui/notifications/tool-input": emit("tool-input", msg.params || {}); break;
        case "ui/notifications/tool-result": emit("tool-result", msg.params || {}); break;
        case "ui/notifications/tool-cancelled": emit("tool-cancelled", msg.params || {}); break;
        case "ui/notifications/host-context-changed": emit("host-context", msg.params || {}); break;
        case "ui/resource-teardown":
          emit("teardown", msg.params || {});
          if (msg.id !== undefined) send({ id: msg.id, result: {} });
          break;
        default:
          // An unknown request still gets an answer, so the host is not left
          // waiting on a view that will never reply.
          if (msg.id !== undefined) {
            send({ id: msg.id, error: { code: -32601, message: "Method not found: " + msg.method } });
          }
      }
    });

    var bridge = {
      hostContext: {},
      on: function(name, fn) { (handlers[name] = handlers[name] || []).push(fn); },
      // The tool's CallToolResult; rejects when the host or the tool failed.
      callTool: function(name, args) {
        return request("tools/call", { name: name, arguments: args || {} }).then(function(result) {
          if (result && result.isError) {
            var text = ((result.content || [])[0] || {}).text || "tool error";
            throw new Error(text);
          }
          return result || {};
        });
      },
      // What the user is looking at, for the model's next turn. Best effort:
      // a host that does not support it must not break the view.
      updateModelContext: function(text) {
        return request("ui/update-model-context", {
          content: [{ type: "text", text: String(text) }],
        }).catch(function() {});
      },
      reportSize: function(width, height) {
        notify("ui/notifications/size-changed", { width: width, height: height });
      },
    };

    bridge.on("host-context", function(ctx) {
      for (var k in ctx) bridge.hostContext[k] = ctx[k];
    });

    bridge.ready = request("ui/initialize", {
      protocolVersion: PROTOCOL_VERSION,
      // Sent under both names the published spec material uses for it;
      // a host ignores the one it does not read.
      appInfo: appInfo,
      clientInfo: appInfo,
      appCapabilities: {},
      capabilities: {},
    }).then(function(result) {
      bridge.hostContext = (result && result.hostContext) || {};
      notify("ui/notifications/initialized", {});
      return result;
    });

    return bridge;
  }

  return { connect: connect };
})();
