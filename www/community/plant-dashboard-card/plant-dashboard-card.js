class PlantDashboardCard extends HTMLElement {
  class PlantDashboardCard extends HTMLElement {
    setConfig(config) {
      this.config = config || {};
      if (!this.config.show_all && !this.config.entities) {
        throw new Error('You must define `entities` or set `show_all: true`.');
      }
      this._sortBy = this.config.sort_by || 'watering';
      this._query = '';
    }

    set hass(hass) {
      this._hass = hass;
      this._render();
    }

    _callPlantInfo(entityId) {
      return this._hass.callWS({ type: 'plant/get_info', entity_id: entityId });
    }

    async _render() {
      if (!this._hass) return;

      // Reuse shadow root if present
      if (!this.shadowRoot) {
        this.attachShadow({ mode: 'open' });
      }
      const root = this.shadowRoot;
      root.innerHTML = '';

      const style = document.createElement('style');
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
        .icon { font-size:18px; margin-right:6px }
      `;
      root.appendChild(style);

      const container = document.createElement('div');
      container.className = 'card';

      // Controls: search and sort
      const controls = document.createElement('div');
      controls.className = 'controls';
      const input = document.createElement('input');
      input.className = 'search';
      input.placeholder = this.config.search_placeholder || 'Filter plants by name or room...';
      input.value = this._query;
      input.addEventListener('input', (e) => {
        this._query = e.target.value.toLowerCase();
        this._render();
      });
      controls.appendChild(input);

      const select = document.createElement('select');
      [['watering','Time until watering'], ['name','Name'], ['nickname','Nickname']].forEach(([val, label]) => {
        const o = document.createElement('option'); o.value = val; o.textContent = label; if (val===this._sortBy) o.selected = true; select.appendChild(o);
      });
      select.addEventListener('change', (e) => { this._sortBy = e.target.value; this._render(); });
      controls.appendChild(select);

      container.appendChild(controls);

      // Determine plants to show
      let plants = [];
      if (this.config.show_all) {
        plants = Object.values(this._hass.states).filter(s => s.entity_id.startsWith('plant.'));
      } else if (this.config.entities) {
        plants = this.config.entities.map(eid => this._hass.states[eid]).filter(Boolean);
      }

      // Fetch plant infos in parallel
      const infos = await Promise.all(plants.map(p => this._callPlantInfo(p.entity_id).catch(()=>null)));

      const rows = [];
      for (let i=0;i<plants.length;i++){
        const plantState = plants[i];
        const info = infos[i];
        const plantInfo = info ? (info.result || info) : {};
        const wateringEntity = plantInfo.watering_sensor || null;
        const wateringState = wateringEntity && this._hass.states[wateringEntity] ? this._hass.states[wateringEntity].state : null;
        const wateringNum = wateringState ? parseFloat(wateringState) : null;
        rows.push({ plantState, plantInfo, wateringEntity, wateringState, wateringNum });
      }

      // filter
      const filtered = rows.filter(r => {
        if (!this._query) return true;
        const name = (r.plantState.attributes.friendly_name || r.plantState.entity_id).toLowerCase();
        const nick = (r.plantState.attributes.nickname||'').toLowerCase();
        return name.includes(this._query) || nick.includes(this._query);
      });

      // sort
      filtered.sort((a,b) => {
        if (this._sortBy==='name') return (a.plantState.attributes.friendly_name||a.plantState.entity_id).localeCompare(b.plantState.attributes.friendly_name||b.plantState.entity_id);
        if (this._sortBy==='nickname') return (a.plantState.attributes.nickname||'').localeCompare(b.plantState.attributes.nickname||'');
        // default: watering (nulls last)
        const av = a.wateringNum; const bv = b.wateringNum;
        if (av===null && bv===null) return 0; if (av===null) return 1; if (bv===null) return -1; return av - bv;
      });

      // build rows
      filtered.forEach(({plantState, plantInfo, wateringEntity, wateringState}) => {
        const plantEl = document.createElement('div');
        plantEl.className = 'plant';

        const img = document.createElement('img');
        const pic = plantState.attributes.entity_picture || plantInfo.plant_info?.entity_picture || '';
        img.src = pic || 'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="56" height="56"></svg>';
        plantEl.appendChild(img);

        const meta = document.createElement('div'); meta.className='meta';
        const name = document.createElement('div'); name.className='name'; name.textContent = plantState.attributes.friendly_name || plantState.entity_id; meta.appendChild(name);
        if (plantState.attributes.nickname) { const nick = document.createElement('div'); nick.className='nick'; nick.textContent = plantState.attributes.nickname; meta.appendChild(nick); }
        const stateDiv = document.createElement('div'); stateDiv.className='state';
        if (wateringEntity && this._hass.states[wateringEntity]) {
          stateDiv.textContent = `Hours until watering: ${wateringState}`;
        } else {
          stateDiv.textContent = this.config.no_watering_text || 'No watering sensor linked';
        }
        meta.appendChild(stateDiv);
        plantEl.appendChild(meta);

        const actions = document.createElement('div'); actions.className='actions';
        const btnDone = document.createElement('button'); btnDone.innerHTML = '✅ Done';
        btnDone.addEventListener('click', () => {
          const confirmMsg = this.config.confirm_mark_done || `Mark ${plantState.attributes.friendly_name || plantState.entity_id} as watered?`;
          if (this.config.confirm_before_done === false || window.confirm(confirmMsg)) {
            this._hass.callService('plant', 'mark_watered', { entity_id: plantState.entity_id });
          }
        });
        actions.appendChild(btnDone);

        const btnSnooze = document.createElement('button'); btnSnooze.innerHTML = '⏱️ Snooze 1h';
        btnSnooze.addEventListener('click', () => {
          this._hass.callService('plant', 'snooze', { entity_id: plantState.entity_id, hours: 1 });
        });
        actions.appendChild(btnSnooze);

        plantEl.appendChild(actions);
        container.appendChild(plantEl);
      });

      root.appendChild(container);
    }

    getCardSize() {
      // approximate card size based on visible plants
      const cnt = this.config.show_all ? Object.values(this._hass.states).filter(s=>s.entity_id.startsWith('plant.')).length : (this.config.entities||[]).length;
      return Math.min(Math.max(1, Math.ceil(cnt)), 10);
    }
  }

  customElements.define('plant-dashboard-card', PlantDashboardCard);
      actions.appendChild(btnSnooze);

      plantEl.appendChild(actions);

      container.appendChild(plantEl);
    }

    root.appendChild(container);
  }

  getCardSize() {
    return this.config.entities ? this.config.entities.length : 5;
  }
}

customElements.define("plant-dashboard-card", PlantDashboardCard);
