// The websocket of the participant page.
//
// It connects with the page id (kept per tab, see app.js) so that a reload
// replaces the old connection. After a drop it tries again every second,
// except when the server answered `second_window`: that is final.

const RETRY_MS = 1000;

export function openSocket({ pageId, onMessage, onStatus }) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const url = `${scheme}://${location.host}/ws?role=participant&page=${encodeURIComponent(pageId)}`;
  let socket = null;
  let final = false;

  function connect() {
    socket = new WebSocket(url);
    socket.addEventListener("open", () => onStatus("open"));
    socket.addEventListener("message", (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      if (message.t === "error" && message.kind === "second_window") final = true;
      onMessage(message);
    });
    socket.addEventListener("close", () => {
      if (final) return;
      onStatus("lost");
      setTimeout(connect, RETRY_MS);
    });
  }

  connect();
  return {
    /** Sends a command; false if the socket is not open. */
    send(command) {
      if (!socket || socket.readyState !== WebSocket.OPEN) return false;
      socket.send(JSON.stringify(command));
      return true;
    },
  };
}
