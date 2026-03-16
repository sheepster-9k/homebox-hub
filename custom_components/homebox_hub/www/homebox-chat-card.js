/**
 * Homebox Chat Card — Lovelace custom card for inventory queries.
 *
 * Calls the homebox_hub conversation agent or search service
 * and displays results inline.
 */

class HomeboxChatCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = {};
    this._messages = [];
    this._loading = false;
  }

  set hass(hass) {
    this._hass = hass;
  }

  setConfig(config) {
    this._config = config;
    this._render();
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return { title: "Inventory Search" };
  }

  _render() {
    const title = this._config.title || "Inventory Search";

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
        }
        ha-card {
          padding: 16px;
        }
        .header {
          font-size: 1.1em;
          font-weight: 500;
          margin-bottom: 12px;
          display: flex;
          align-items: center;
          gap: 8px;
        }
        .header ha-icon {
          color: var(--primary-color);
        }
        .input-row {
          display: flex;
          gap: 8px;
          margin-bottom: 12px;
        }
        .input-row input {
          flex: 1;
          padding: 8px 12px;
          border: 1px solid var(--divider-color, #e0e0e0);
          border-radius: 8px;
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color);
          font-size: 14px;
          outline: none;
        }
        .input-row input:focus {
          border-color: var(--primary-color);
        }
        .input-row button {
          padding: 8px 16px;
          border: none;
          border-radius: 8px;
          background: var(--primary-color);
          color: var(--text-primary-color, #fff);
          cursor: pointer;
          font-size: 14px;
          white-space: nowrap;
        }
        .input-row button:disabled {
          opacity: 0.5;
          cursor: default;
        }
        .messages {
          max-height: 300px;
          overflow-y: auto;
          display: flex;
          flex-direction: column;
          gap: 8px;
        }
        .msg {
          padding: 8px 12px;
          border-radius: 8px;
          font-size: 14px;
          line-height: 1.4;
        }
        .msg.user {
          background: var(--primary-color);
          color: var(--text-primary-color, #fff);
          align-self: flex-end;
          max-width: 80%;
        }
        .msg.assistant {
          background: var(--secondary-background-color, #f5f5f5);
          color: var(--primary-text-color);
          align-self: flex-start;
          max-width: 90%;
        }
        .msg.assistant pre {
          margin: 4px 0;
          white-space: pre-wrap;
          font-family: inherit;
        }
        .loading {
          color: var(--secondary-text-color);
          font-style: italic;
          font-size: 13px;
        }
        .empty {
          color: var(--secondary-text-color);
          font-size: 13px;
          text-align: center;
          padding: 16px 0;
        }
      </style>
      <ha-card>
        <div class="header">
          <ha-icon icon="mdi:package-variant-closed"></ha-icon>
          ${title}
        </div>
        <div class="input-row">
          <input type="text" id="query" placeholder="Ask about your inventory..." />
          <button id="send">Search</button>
        </div>
        <div class="messages" id="messages">
          <div class="empty">Ask me where things are, what's in a location, or search your inventory.</div>
        </div>
      </ha-card>
    `;

    const input = this.shadowRoot.getElementById("query");
    const sendBtn = this.shadowRoot.getElementById("send");

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !this._loading) {
        this._send();
      }
    });

    sendBtn.addEventListener("click", () => {
      if (!this._loading) this._send();
    });
  }

  async _send() {
    const input = this.shadowRoot.getElementById("query");
    const query = input.value.trim();
    if (!query || !this._hass) return;

    input.value = "";
    this._messages.push({ role: "user", text: query });
    this._loading = true;
    this._renderMessages();

    try {
      // Try conversation agent first
      const result = await this._hass.callWS({
        type: "conversation/process",
        text: query,
        agent_id: this._findAgentId(),
      });

      const response =
        result?.response?.speech?.plain?.speech ||
        result?.response?.speech?.plain ||
        "No response";

      this._messages.push({ role: "assistant", text: response });
    } catch {
      // Fallback to search service
      try {
        const searchResult = await this._hass.callService(
          "homebox_hub",
          "search",
          { query },
          {},
          false,
          true
        );

        if (searchResult?.items?.length) {
          const lines = searchResult.items
            .map((i) => `- ${i.name}`)
            .join("\n");
          this._messages.push({
            role: "assistant",
            text: `Found ${searchResult.items.length} item(s):\n${lines}`,
          });
        } else {
          this._messages.push({
            role: "assistant",
            text: `No items found matching "${query}".`,
          });
        }
      } catch (err) {
        this._messages.push({
          role: "assistant",
          text: "Sorry, I couldn't search right now. Check integration status.",
        });
      }
    }

    this._loading = false;
    this._renderMessages();
  }

  _findAgentId() {
    // Try to find the homebox_hub conversation agent
    // The agent entity ID follows the pattern: conversation.homebox_inventory_assistant
    return "conversation.homebox_inventory_assistant";
  }

  _renderMessages() {
    const container = this.shadowRoot.getElementById("messages");
    if (!container) return;

    let html = "";
    for (const msg of this._messages) {
      const escaped = msg.text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
      html += `<div class="msg ${msg.role}"><pre>${escaped}</pre></div>`;
    }
    if (this._loading) {
      html += '<div class="loading">Searching inventory...</div>';
    }
    container.innerHTML = html;
    container.scrollTop = container.scrollHeight;
  }
}

customElements.define("homebox-chat-card", HomeboxChatCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "homebox-chat-card",
  name: "Homebox Chat",
  description: "Search and chat with your Homebox inventory",
  preview: true,
});
