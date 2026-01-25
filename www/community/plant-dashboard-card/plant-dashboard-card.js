/*
  plant-dashboard-card.js
  Lightweight Plant Dashboard card with live updates, search, sort, and inline actions.
*/
    [
      ["watering", _localize('sort_watering') || 'Time until watering'],
      ["name", _localize('sort_name') || 'Name'],
    ].forEach(([val, label]) => {
      throw new Error("You must define `entities` or set `show_all: true`.");
    }
    this._sortBy = this.config.sort_by || "watering";
    this._query = "";
    this._debounce = null;
    this._unsub = null;
  }

  set hass(hass) {
    this._hass = hass;
    // subscribe once to state_changed events to update card live
    if (!this._unsub && this._hass && this._hass.connection) {
      try {
        this._unsub = this._hass.connection.subscribeEvents((ev) => {
          const eid = ev.data && ev.data.entity_id;
          if (!eid) return;
          if (
            eid.startsWith("plant.") ||
            eid.includes("watering") ||
            eid.startsWith("sensor.")
          ) {
            // debounce rapid events
            if (this._debounce) clearTimeout(this._debounce);
            this._debounce = setTimeout(() => this._render(), 300);
          }
        }, "state_changed");
      } catch (e) {
        // ignore subscription errors
        this._unsub = null;
      }
    }

    this._render();
  }

  disconnectedCallback() {
    if (this._unsub) {
      try {
        this._unsub();
      } catch (e) {}
      this._unsub = null;
    }
  }

  _callPlantInfo(entityId) {
    return this._hass.callWS({ type: "plant/get_info", entity_id: entityId });
  }

  async _render() {
    if (!this._hass) return;

    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const root = this.shadowRoot;
    root.innerHTML = "";

    const style = document.createElement("style");
    style.textContent = `
      .card { font-family: var(--ha-card-font-family, inherit); padding:8px }
      .controls { display:flex; gap:8px; align-items:center; margin-bottom:8px }
      input.search { flex:1; padding:6px; border:1px solid var(--divider-color); border-radius:6px }
      select { padding:6px; border:1px solid var(--divider-color); border-radius:6px }
      .plant { display:flex; align-items:center; gap:12px; padding:10px; border-radius:8px; background:var(--card-background-color); margin-bottom:8px; box-shadow:var(--ha-card-box-shadow, none) }
      img { width:56px; height:56px; object-fit:cover; border-radius:6px }
      .meta { flex:1 }
      .name { font-weight:600 }
      .nick { color:var(--secondary-text-color); font-size:0.9em }
      .state { font-size:0.95em; margin-top:4px }
      .actions { display:flex; gap:8px }
      button { background:var(--paper-card-background-color); border:1px solid var(--divider-color); padding:6px 8px; border-radius:6px; cursor:pointer }
      button:hover { transform: translateY(-1px) }
    `;
    root.appendChild(style);

    const container = document.createElement("div");
    container.className = "card";

    // Controls: search and sort
    const controls = document.createElement("div");
    controls.className = "controls";
    const input = document.createElement("input");
    input.className = "search";
    input.setAttribute("aria-label", "Search plants");
    const _localize = (k, vars) => {
      try {
        const key = `component.plant.card.${k}`;
        const localized = this._hass.localize(key);
        if (localized) {
          if (vars && typeof vars === "object") {
            let out = localized;
            Object.keys(vars).forEach((p) => {
              out = out.replace(`{${p}}`, vars[p]);
            });
            return out;
          }
          return localized;
        }
      } catch (e) {}
      return null;
    };

    input.placeholder =
      this.config.search_placeholder ||
      _localize("search_placeholder") ||
      "Filter plants by name or room...";
    input.value = this._query;
    input.addEventListener("input", (e) => {
      this._query = e.target.value.toLowerCase();
      this._render();
    });
    controls.appendChild(input);

    const select = document.createElement("select");
    select.setAttribute("aria-label", "Sort plants");
    [
      ["watering", _localize("sort_watering") || "Time until watering"],
      ["name", _localize("sort_name") || "Name"],
    ].forEach(([val, label]) => {
      const o = document.createElement("option");
      o.value = val;
      o.textContent = label;
      if (val === this._sortBy) o.selected = true;
      select.appendChild(o);
    });
    select.addEventListener("change", (e) => {
      this._sortBy = e.target.value;
      this._render();
    });
    controls.appendChild(select);

    container.appendChild(controls);

    // Determine plants to show
    let plants = [];
    if (this.config.show_all) {
      plants = Object.values(this._hass.states).filter((s) =>
        s.entity_id.startsWith("plant."),
      );
    } else if (this.config.entities) {
      plants = this.config.entities
        .map((eid) => this._hass.states[eid])
        .filter(Boolean);
    }

    // Fetch plant infos in parallel
    const infos = await Promise.all(
      plants.map((p) => this._callPlantInfo(p.entity_id).catch(() => null)),
    );

    const rows = [];
    for (let i = 0; i < plants.length; i++) {
      const plantState = plants[i];
      const info = infos[i];
      const plantInfo = info ? info.result || info : {};
      const wateringEntity = plantInfo.watering_sensor || null;
      const wateringState =
        wateringEntity && this._hass.states[wateringEntity]
          ? this._hass.states[wateringEntity].state
          : null;
      const wateringNum = wateringState ? parseFloat(wateringState) : null;
      rows.push({
        plantState,
        plantInfo,
        wateringEntity,
        wateringState,
        wateringNum,
      });
    }

    // filter
    const filtered = rows.filter((r) => {
      if (!this._query) return true;
      const name = (r.plantState.attributes.friendly_name || r.plantState.entity_id).toLowerCase();
      return name.includes(this._query);
    });

    // sort
    filtered.sort((a, b) => {
      if (this._sortBy === "name")
        return (
          a.plantState.attributes.friendly_name || a.plantState.entity_id
        ).localeCompare(
          b.plantState.attributes.friendly_name || b.plantState.entity_id,
        );

      // default: watering (nulls last)
      const av = a.wateringNum;
      const bv = b.wateringNum;
      if (av === null && bv === null) return 0;
      if (av === null) return 1;
      if (bv === null) return -1;
      return av - bv;
    });

    // build rows
    filtered.forEach(
      ({ plantState, plantInfo, wateringEntity, wateringState }) => {
        const plantEl = document.createElement("div");
        plantEl.className = "plant";

        const img = document.createElement("img");
        img.setAttribute(
          "alt",
          plantState.attributes.friendly_name || plantState.entity_id,
        );
        const pic =
          plantState.attributes.entity_picture ||
          plantInfo.plant_info?.entity_picture ||
          "";
        img.src =
          pic ||
          'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="56" height="56"></svg>';
        plantEl.appendChild(img);

        const meta = document.createElement("div");
        meta.className = "meta";
        const name = document.createElement("div");
        name.className = "name";
        name.textContent =
          plantState.attributes.friendly_name || plantState.entity_id;
        meta.appendChild(name);

        const stateDiv = document.createElement("div");
        stateDiv.className = "state";
        if (wateringEntity && this._hass.states[wateringEntity]) {
          stateDiv.textContent = `${_localize("time_until_watering") || "Hours until watering:"} ${wateringState}`;
        } else {
          stateDiv.textContent =
            this.config.no_watering_text ||
            _localize("no_watering_text") ||
            "No watering sensor linked";
        }
        meta.appendChild(stateDiv);
        plantEl.appendChild(meta);

        const actions = document.createElement("div");
        actions.className = "actions";
        const btnDone = document.createElement("button");
        btnDone.innerHTML =
          this.config.done_label || _localize("done") || "✅ Done";
        btnDone.setAttribute("aria-label", "Mark watered");
        btnDone.addEventListener("click", () => {
          const plantName =
            plantState.attributes.friendly_name || plantState.entity_id;
          const localizedConfirm = _localize("confirm_mark_done", {
            name: plantName,
          });
          const confirmMsg =
            this.config.confirm_mark_done ||
            localizedConfirm ||
            `Mark ${plantName} as watered?`;
          if (
            this.config.confirm_before_done === false ||
            window.confirm(confirmMsg)
          ) {
            this._hass.callService("plant", "mark_watered", {
              entity_id: plantState.entity_id,
            });
          }
        });
        actions.appendChild(btnDone);

        const btnSnooze = document.createElement("button");
        btnSnooze.innerHTML =
          this.config.snooze_label || _localize("snooze_1h") || "⏱️ Snooze 1h";
        btnSnooze.setAttribute("aria-label", "Snooze 1 hour");
        btnSnooze.addEventListener("click", () => {
          this._hass.callService("plant", "snooze", {
            entity_id: plantState.entity_id,
            hours: 1,
          });
        });
        actions.appendChild(btnSnooze);

        plantEl.appendChild(actions);
        container.appendChild(plantEl);
      },
    );

    root.appendChild(container);
  }

  getCardSize() {
    const visible = this.config.show_all
      ? Object.values(this._hass.states).filter((s) =>
          s.entity_id.startsWith("plant."),
        ).length
      : (this.config.entities || []).length;
    return Math.min(Math.max(1, visible), 12);
  }
}

customElements.define("plant-dashboard-card", PlantDashboardCard);
